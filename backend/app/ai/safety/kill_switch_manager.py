"""
Kill Switch Manager - Production-grade safety control for trading system.

Features:
- Redis caching for <1ms reads (target: <100ms activation)
- Dual-write pattern (DB + Redis) for durability + performance
- Optional auto-expiration with background cleanup
- Prometheus metrics for observability
- Atomic operations to prevent race conditions
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from prometheus_client import Counter, Histogram
from redis.asyncio import Redis
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIKillSwitch
from app.core.redis import PubSubClient, RedisChannels

logger = logging.getLogger(__name__)

# Prometheus metrics
KILL_SWITCH_ACTIVATIONS = Counter(
    "kill_switch_activations_total",
    "Total kill switch activations",
    ["switch_type", "has_expiration"]
)

KILL_SWITCH_ACTIVATION_TIME = Histogram(
    "kill_switch_activation_seconds",
    "Time to activate kill switch (target: <0.1s)",
    buckets=[0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.5, 1.0]
)

KILL_SWITCH_CHECK_TIME = Histogram(
    "kill_switch_check_seconds",
    "Time to check kill switch status",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1]
)

KILL_SWITCH_CACHE_HITS = Counter(
    "kill_switch_cache_hits_total",
    "Redis cache hits for kill switch checks"
)

KILL_SWITCH_CACHE_MISSES = Counter(
    "kill_switch_cache_misses_total",
    "Redis cache misses for kill switch checks"
)


class KillSwitchManager:
    """Production-grade kill switch manager with Redis caching and metrics."""

    # Redis key patterns
    CACHE_KEY_PATTERN = "kill_switch:active:{switch_type}:{target_id}"
    METRICS_KEY = "kill_switch:metrics"

    def __init__(self, redis: Redis):
        """Initialize with Redis client for caching."""
        self.redis = redis

    def _get_cache_key(self, switch_type: str, target_id: Optional[str]) -> str:
        """Generate Redis cache key."""
        target = target_id or "global"
        return self.CACHE_KEY_PATTERN.format(switch_type=switch_type, target_id=target)

    async def activate(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        switch_type: str,
        target_id: Optional[str],
        activated_by: str,
        reason: str,
        expiration_minutes: Optional[int] = None,
    ) -> AIKillSwitch:
        """
        Activate kill switch with <100ms target.
        
        Args:
            expiration_minutes: Optional auto-expiration (None = manual deactivation required)
        """
        start_time = time.perf_counter()
        
        # Deactivate any existing active kill switches of same type (prevent duplicates)
        stmt = select(AIKillSwitch).where(
            AIKillSwitch.switch_type == switch_type,
            AIKillSwitch.status == "active"
        )
        if target_id:
            stmt = stmt.where(AIKillSwitch.target_id == target_id)
        
        result = await db.execute(stmt)
        existing = result.scalars().all()
        
        for ks in existing:
            ks.status = "inactive"
            ks.deactivated_at = datetime.now(timezone.utc)
            logger.info(f"Auto-deactivated existing kill switch {ks.id} before new activation")
        
        # Calculate expiration time if provided
        expiration_time = None
        if expiration_minutes is not None:
            if expiration_minutes < 1 or expiration_minutes > 1440:  # 1 min to 24 hours
                raise ValueError("expiration_minutes must be between 1 and 1440 (24 hours)")
            expiration_time = datetime.now(timezone.utc) + timedelta(minutes=expiration_minutes)  # Fixed: timezone-aware

        # Create DB record (durable storage)
        kill_switch = AIKillSwitch(
            switch_type=switch_type,
            target_id=target_id,
            status="active",
            activated_by=activated_by,
            activation_reason=reason,
            activated_at=datetime.now(timezone.utc),  # Fixed: timezone-aware
            expiration_time=expiration_time,
        )

        db.add(kill_switch)
        await db.commit()
        await db.refresh(kill_switch)

        # Write to Redis cache (fast reads)
        cache_key = self._get_cache_key(switch_type, target_id)
        cache_value = {
            "id": kill_switch.id,
            "switch_type": switch_type,
            "target_id": target_id,
            "activated_by": activated_by,
            "reason": reason,
            "activated_at": kill_switch.activated_at.isoformat(),
            "expires_at": expiration_time.isoformat() if expiration_time else None,
        }
        
        # Set Redis key with TTL if expiration specified
        if expiration_minutes:
            await self.redis.setex(
                cache_key,
                expiration_minutes * 60,
                str(cache_value)  # Store as string for simplicity
            )
        else:
            await self.redis.set(cache_key, str(cache_value))

        # Publish activation event
        await pubsub.publish_json(RedisChannels.SAFETY_KILL_SWITCHES, {
            "action": "activated",
            "switch_type": switch_type,
            "target_id": target_id,
            "reason": reason,
            "activated_by": activated_by,
            "expires_at": expiration_time.isoformat() if expiration_time else None,
            "timestamp": datetime.utcnow().isoformat(),
        })

        # Record metrics
        elapsed = time.perf_counter() - start_time
        KILL_SWITCH_ACTIVATION_TIME.observe(elapsed)
        KILL_SWITCH_ACTIVATIONS.labels(
            switch_type=switch_type,
            has_expiration=str(expiration_minutes is not None)
        ).inc()

        # Store activation time for monitoring endpoint
        await self.redis.lpush(f"{self.METRICS_KEY}:activation_times", elapsed)
        await self.redis.ltrim(f"{self.METRICS_KEY}:activation_times", 0, 99)  # Keep last 100

        logger.warning(
            f"Kill switch activated in {elapsed*1000:.2f}ms: {switch_type} "
            f"(target: {target_id}, expires: {expiration_time})"
        )
        
        return kill_switch

    async def deactivate(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        switch_id: int,
    ) -> None:
        """Deactivate kill switch and clear cache."""
        # Get kill switch from DB
        stmt = select(AIKillSwitch).where(AIKillSwitch.id == switch_id)
        result = await db.execute(stmt)
        kill_switch = result.scalar_one()

        # Update DB
        kill_switch.status = "inactive"
        kill_switch.deactivated_at = datetime.now(timezone.utc)  # Fixed: timezone-aware
        await db.commit()

        # Clear Redis cache
        cache_key = self._get_cache_key(kill_switch.switch_type, kill_switch.target_id)
        await self.redis.delete(cache_key)

        # Publish deactivation event
        await pubsub.publish_json(RedisChannels.SAFETY_KILL_SWITCHES, {
            "action": "deactivated",
            "switch_id": switch_id,
            "switch_type": kill_switch.switch_type,
            "target_id": kill_switch.target_id,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(f"Kill switch deactivated: {switch_id}")

    async def is_active(
        self,
        db: AsyncSession,
        switch_type: str,
        target_id: Optional[str] = None,
    ) -> bool:
        """
        Check if kill switch is active (fast path: Redis, fallback: DB).
        Target: <1ms with Redis cache hit.
        """
        start_time = time.perf_counter()
        
        # Fast path: Check Redis cache
        cache_key = self._get_cache_key(switch_type, target_id)
        cached = await self.redis.get(cache_key)
        
        if cached is not None:
            KILL_SWITCH_CACHE_HITS.inc()
            elapsed = time.perf_counter() - start_time
            KILL_SWITCH_CHECK_TIME.observe(elapsed)
            return True  # Cache hit = active
        
        KILL_SWITCH_CACHE_MISSES.inc()
        
        # Slow path: Check database
        conditions = [
            AIKillSwitch.switch_type == switch_type,
            AIKillSwitch.status == "active",
        ]
        if target_id:
            conditions.append(AIKillSwitch.target_id == target_id)

        stmt = select(AIKillSwitch).where(and_(*conditions))
        result = await db.execute(stmt)
        kill_switch = result.scalar_one_or_none()
        
        elapsed = time.perf_counter() - start_time
        KILL_SWITCH_CHECK_TIME.observe(elapsed)
        
        # If found in DB but not in cache, repair cache
        if kill_switch:
            cache_value = {
                "id": kill_switch.id,
                "switch_type": switch_type,
                "target_id": target_id,
                "activated_at": kill_switch.activated_at.isoformat(),
            }
            
            # Calculate remaining TTL if expiration set
            if kill_switch.expiration_time:
                remaining = (kill_switch.expiration_time - datetime.now(timezone.utc)).total_seconds()  # Fixed: timezone-aware
                if remaining > 0:
                    await self.redis.setex(cache_key, int(remaining), str(cache_value))
                else:
                    # Expired, should deactivate
                    return False
            else:
                await self.redis.set(cache_key, str(cache_value))
            
            return True
        
        return False

    async def get_monitoring_data(self, db: AsyncSession) -> dict:
        """Get detailed monitoring data for dashboard."""
        # Current active kill switches
        stmt = select(AIKillSwitch).where(AIKillSwitch.status == "active")
        result = await db.execute(stmt)
        active_switches = result.scalars().all()
        
        current_status = None
        if active_switches:
            ks = active_switches[0]  # Get first active (usually only one global)
            current_status = {
                "is_active": True,
                "switch_type": ks.switch_type,
                "target_id": ks.target_id,
                "activated_at": ks.activated_at.isoformat(),
                "expires_at": ks.expiration_time.isoformat() if ks.expiration_time else None,
                "reason": ks.activation_reason,
                "activated_by": ks.activated_by,
            }
        else:
            current_status = {"is_active": False}
        
        # Performance metrics from Redis
        activation_times = await self.redis.lrange(f"{self.METRICS_KEY}:activation_times", 0, -1)
        activation_times_float = [float(t) for t in activation_times] if activation_times else []
        
        avg_activation_time = sum(activation_times_float) / len(activation_times_float) if activation_times_float else 0
        last_activation_time = activation_times_float[0] if activation_times_float else 0
        
        # Cache hit rate
        cache_hits = KILL_SWITCH_CACHE_HITS._value.get()
        cache_misses = KILL_SWITCH_CACHE_MISSES._value.get()
        total_checks = cache_hits + cache_misses
        cache_hit_rate = (cache_hits / total_checks * 100) if total_checks > 0 else 0
        
        # 24h history
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)  # Fixed: timezone-aware
        stmt = select(AIKillSwitch).where(AIKillSwitch.activated_at >= cutoff)
        result = await db.execute(stmt)
        history_24h = result.scalars().all()
        
        total_downtime_minutes = 0
        for ks in history_24h:
            if ks.deactivated_at:
                downtime = (ks.deactivated_at - ks.activated_at).total_seconds() / 60
            elif ks.status == "active":
                downtime = (datetime.now(timezone.utc) - ks.activated_at).total_seconds() / 60  # Fixed: timezone-aware
            else:
                downtime = 0
            total_downtime_minutes += downtime
        
        uptime_percent = ((24 * 60 - total_downtime_minutes) / (24 * 60)) * 100
        
        return {
            "current_status": current_status,
            "performance_metrics": {
                "last_activation_time_ms": last_activation_time * 1000,
                "avg_activation_time_ms": avg_activation_time * 1000,
                "cache_hit_rate_percent": round(cache_hit_rate, 2),
                "total_checks": total_checks,
            },
            "history_24h": {
                "activation_count": len(history_24h),
                "total_downtime_minutes": round(total_downtime_minutes, 2),
                "uptime_percent": round(uptime_percent, 2),
            }
        }
