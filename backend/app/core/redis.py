"""
Cortex AI — Redis Client with Pub/Sub
======================================
Async Redis with connection pooling, cache service, and pub/sub for real-time events.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncGenerator

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.asyncio.connection import ConnectionPool
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from redis.exceptions import ConnectionError, TimeoutError

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Singleton state ────────────────────────────────────────────────────────────
_pool: ConnectionPool | None = None
_client: Redis | None = None


# ── Redis Channels ─────────────────────────────────────────────────────────────
class RedisChannels:
    """
    Redis pub/sub channel constants for real-time event streaming.
    
    Channel Naming Convention:
    - Format: `cai:<category>:<subcategory>[:<detail>]`
    - Namespace: `cai` (Cortex AI)
    - Category: signals, regime, events, safety, models, suggestions, correlations
    - Subcategory: Specific event type or entity
    
    Message Format:
    - All messages are JSON-serialized dictionaries
    - Use `PubSubClient.publish_json()` for publishing
    - Subscribers receive parsed JSON via `PubSubClient.listen()`
    
    Best Practices:
    - Fire-and-forget: Pub/sub does NOT persist messages
    - At-most-once delivery: Lost if no subscriber is active
    - Use Redis Streams for durable messaging requirements
    - Pattern matching: Use `psubscribe` for wildcard subscriptions
    
    Example Usage:
        # Publishing
        await pubsub.publish_json(
            RedisChannels.SUGGESTIONS_NEW,
            {"suggestion_id": str(uuid), "symbol": "AAPL", "direction": "BUY"}
        )
        
        # Subscribing
        ps = await pubsub.subscribe(RedisChannels.SUGGESTIONS_NEW)
        async for message in pubsub.listen(ps):
            print(message["channel"], message["data"])
    """
    
    # ── Trading Signals ────────────────────────────────────────────────────────
    SIGNALS_SYMBOL = "cai:signals:{symbol}"
    """Per-symbol trading signals. Payload: {signal_id, symbol, score, confidence}"""
    
    SIGNALS_ALL = "cai:signals:all"
    """All trading signals broadcast. Payload: {signal_id, symbol, score, confidence}"""
    
    # ── Market Regime ──────────────────────────────────────────────────────────
    REGIME_SYMBOL = "cai:regime:{symbol}"
    """Per-symbol regime changes. Payload: {symbol, regime, confidence, timestamp}"""
    
    REGIME_ALL = "cai:regime:all"
    """All regime changes broadcast. Payload: {symbol, regime, confidence, timestamp}"""
    
    # ── News Events ────────────────────────────────────────────────────────────
    EVENTS_HIGH_IMPACT = "cai:events:high_impact"
    """High-impact news events. Payload: {event_id, type, impact_score, symbols}"""
    
    EVENTS_ALL = "cai:events:all"
    """All classified news events. Payload: {event_id, type, impact_score, symbols}"""
    
    # ── Safety & Risk Management ───────────────────────────────────────────────
    SAFETY_KILL_SWITCHES = "cai:safety:kill_switches"
    """Kill switch activations/deactivations. Payload: {switch_id, status, reason}"""
    
    SAFETY_TRIGGERS = "cai:safety:triggers"
    """Safety trigger events. Payload: {trigger_id, type, severity, action}"""
    
    # ── Model Governance ───────────────────────────────────────────────────────
    MODELS_STATE_CHANGES = "cai:models:state_changes"
    """Model state transitions. Payload: {model_id, old_state, new_state, reason}"""
    
    MODELS_DRIFT_ALERTS = "cai:models:drift_alerts"
    """Model drift detection alerts. Payload: {model_id, metric, threshold, value}"""
    
    # ── Trade Suggestions ──────────────────────────────────────────────────────
    SUGGESTIONS_NEW = "cai:suggestions:new"
    """
    New trade suggestion generated.
    
    Payload:
        {
            "suggestion_id": "uuid",
            "symbol": "AAPL",
            "signal_direction": "BUY" | "SELL",
            "consensus_score": 85.5,
            "confidence_level": "HIGH" | "MEDIUM" | "LOW",
            "trigger_type": "SCANNER_ANOMALY" | "NEWS_EVENT",
            "generated_at": "2026-04-22T12:00:00Z"
        }
    """
    
    SUGGESTIONS_EXPIRED = "cai:suggestions:expired"
    """
    Trade suggestion expired (TTL reached).
    
    Payload:
        {
            "suggestion_id": "uuid",
            "symbol": "AAPL",
            "expired_at": "2026-04-22T12:30:00Z",
            "reason": "TTL_EXPIRED" | "MARKET_CLOSED" | "MANUAL_EXPIRY"
        }
    """
    
    # ── Market Feed ────────────────────────────────────────────────────────────
    MARKET_FEED_LTPC = "cai:market-feed:ltpc"
    """
    Real-time ltpc ticks from Upstox Market Data Feed V3.

    Published by MarketFeedService after per-instrument throttling (250 ms).
    Subscribed by /upstox/market-feed/ws handlers to fan out to frontend clients.

    Payload:
        {
            "type": "ltpc",
            "instrument_key": "NSE_EQ|INE002A01018",
            "ltp": 2851.50,
            "cp": 2840.00,
            "ts": 1704067200500
        }
    """

    # ── Correlation Events ─────────────────────────────────────────────────────
    CORRELATIONS_COMPLETED = "cai:correlations:completed"
    """
    Correlation analysis completed successfully.
    
    Payload:
        {
            "correlation_id": "uuid",
            "trigger_type": "SCANNER_ANOMALY" | "NEWS_EVENT",
            "suggestion_id": "uuid",
            "consensus_score": 85.5,
            "latencies": {"scanner_ms": 45, "ai_ms": 120, "ml_ms": 230},
            "completed_at": "2026-04-22T12:00:00Z"
        }
    """
    
    CORRELATIONS_REJECTED = "cai:correlations:rejected"
    """
    Correlation analysis rejected (low consensus).
    
    Payload:
        {
            "correlation_id": "uuid",
            "trigger_type": "SCANNER_ANOMALY" | "NEWS_EVENT",
            "rejection_reason": "LOW_CONSENSUS" | "CONFLICTING_SIGNALS" | "TIMEOUT",
            "consensus_score": 45.2,
            "rejected_at": "2026-04-22T12:00:00Z"
        }
    """

    @staticmethod
    def signals_for_symbol(symbol: str) -> str:
        """Get per-symbol signals channel."""
        return RedisChannels.SIGNALS_SYMBOL.format(symbol=symbol)

    @staticmethod
    def regime_for_symbol(symbol: str) -> str:
        """Get per-symbol regime channel."""
        return RedisChannels.REGIME_SYMBOL.format(symbol=symbol)


# ── Lifecycle ──────────────────────────────────────────────────────────────────
async def init_redis() -> None:
    """Initialize Redis connection pool."""
    global _pool, _client
    _pool = ConnectionPool.from_url(
        str(settings.REDIS_URL),
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        decode_responses=True,
    )
    _client = Redis(connection_pool=_pool)
    await _client.ping()
    logger.info("Redis connection pool initialized")


async def close_redis() -> None:
    """Close Redis connection pool."""
    global _pool, _client
    if _client:
        await _client.aclose()
    if _pool:
        await _pool.aclose()
    _client = None
    _pool = None
    logger.info("Redis connection pool closed")


def get_redis() -> Redis:
    """Return Redis client singleton."""
    if _client is None:
        raise RuntimeError("Redis not initialized")
    return _client


# ── Cache Service ──────────────────────────────────────────────────────────────
class CacheService:
    """Cache service with JSON serialization and retry logic."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def get(self, key: str) -> Any | None:
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Cache decode error for key {key}")
            return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def set(self, key: str, value: Any, ttl: int) -> None:
        await self._redis.setex(key, ttl, json.dumps(value, default=str))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def delete(self, key: str) -> None:
        await self._redis.delete(key)

    async def delete_pattern(self, pattern: str) -> int:
        keys = await self._redis.keys(pattern)
        if not keys:
            return 0
        return await self._redis.delete(*keys)

    async def get_cache_stats(self) -> dict:
        info = await self._redis.info("stats")
        return {
            "hits": info.get("keyspace_hits", 0),
            "misses": info.get("keyspace_misses", 0),
            "connected": True,
        }


# ── Pub/Sub Client ─────────────────────────────────────────────────────────────
class PubSubClient:
    """Pub/sub client for real-time event streaming."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def publish(self, channel: str, message: str) -> int:
        """Publish message to channel. Returns subscriber count."""
        return await self._redis.publish(channel, message)

    async def publish_json(self, channel: str, data: dict[str, Any]) -> int:
        """Publish JSON data to channel."""
        return await self.publish(channel, json.dumps(data, default=str))

    async def subscribe(self, *channels: str) -> PubSub:
        """Subscribe to channels and return PubSub object."""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(*channels)
        return pubsub

    async def listen(self, pubsub: PubSub) -> AsyncGenerator[dict[str, Any], None]:
        """Listen for messages on subscribed channels."""
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    yield {"channel": message["channel"], "data": data}
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON in message: {message['data']}")


# ── Dependencies ───────────────────────────────────────────────────────────────
def get_cache_service() -> CacheService:
    """FastAPI dependency for CacheService."""
    return CacheService(get_redis())


def get_pubsub_client() -> PubSubClient:
    """FastAPI dependency for PubSubClient."""
    return PubSubClient(get_redis())
