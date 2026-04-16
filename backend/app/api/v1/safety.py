"""
Safety API - Production-grade Kill Switch Management

Features:
- <100ms activation with Redis caching
- Optional auto-expiration (1 min to 24 hours)
- Detailed monitoring endpoint with performance metrics
- Prometheus instrumentation
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.safety.kill_switch_manager import KillSwitchManager
from app.api.deps import get_db
from app.core.auth import require_role
from app.core.limiter import limiter
from app.core.redis import PubSubClient, get_redis

router = APIRouter()


class ActivateRequest(BaseModel):
    """Kill switch activation request with optional expiration."""
    reason: str = Field(..., description="Reason for activation (required for audit)")
    switch_type: str = Field(default="global", description="Type: global, symbol, strategy")
    target_id: Optional[str] = Field(default=None, description="Target ID (e.g., symbol name)")
    expiration_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        le=1440,
        description="Auto-expiration in minutes (1-1440). None = manual deactivation required"
    )


@router.post("/kill-switch/activate")
@limiter.limit("5/minute")
async def activate_kill_switch(
    request: Request,
    body: ActivateRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(require_role("admin")),
):
    """
    Activate kill switch (admin only).
    
    Target: <100ms activation time.
    
    Examples:
    - Temporary halt: expiration_minutes=15 (auto-resumes after 15 min)
    - Critical issue: expiration_minutes=None (requires manual deactivation)
    """
    manager = KillSwitchManager(redis)
    pubsub = PubSubClient(redis)
    
    try:
        kill_switch = await manager.activate(
            db=db,
            pubsub=pubsub,
            switch_type=body.switch_type,
            target_id=body.target_id,
            activated_by=str(current_user.id),  # Changed from user_id to id
            reason=body.reason,
            expiration_minutes=body.expiration_minutes,
        )
        
        return {
            "status": "activated",
            "kill_switch_id": kill_switch.id,
            "switch_type": body.switch_type,
            "target_id": body.target_id,
            "reason": body.reason,
            "expires_at": kill_switch.expiration_time.isoformat() if kill_switch.expiration_time else None,
            "activated_at": kill_switch.activated_at.isoformat(),
        }
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Kill switch activation failed: {str(e)}"
        )


@router.post("/kill-switch/deactivate")
async def deactivate_kill_switch(
    switch_id: int = Query(..., description="Kill switch ID to deactivate"),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(require_role("admin")),
):
    """Deactivate kill switch (admin only)."""
    manager = KillSwitchManager(redis)
    pubsub = PubSubClient(redis)
    
    try:
        await manager.deactivate(db, pubsub, switch_id=switch_id)
        return {"status": "deactivated", "switch_id": switch_id}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Kill switch deactivation failed: {str(e)}"
        )


@router.get("/kill-switch/status")
async def get_kill_switch_status(
    switch_type: str = Query("global"),
    target_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(require_role("trader")),
):
    """
    Get kill switch status (fast path: Redis cache).
    
    Target: <1ms with cache hit, <50ms with cache miss.
    """
    manager = KillSwitchManager(redis)
    
    try:
        is_active = await manager.is_active(db, switch_type=switch_type, target_id=target_id)
        return {
            "is_active": is_active,
            "switch_type": switch_type,
            "target_id": target_id,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Status check failed: {str(e)}"
        )


@router.get("/kill-switch/monitor")
async def get_kill_switch_monitoring(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user=Depends(require_role("trader")),
):
    """
    Get detailed monitoring data for kill switch dashboard.
    
    Returns:
    - Current status (active/inactive, reason, expiration)
    - Performance metrics (activation time, cache hit rate)
    - 24h history (activation count, downtime, uptime %)
    """
    manager = KillSwitchManager(redis)
    
    try:
        monitoring_data = await manager.get_monitoring_data(db)
        return monitoring_data
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Monitoring data retrieval failed: {str(e)}"
        )
