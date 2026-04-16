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
    """Redis pub/sub channel constants."""
    SIGNALS_SYMBOL = "cai:signals:{symbol}"
    SIGNALS_ALL = "cai:signals:all"
    REGIME_SYMBOL = "cai:regime:{symbol}"
    REGIME_ALL = "cai:regime:all"
    EVENTS_HIGH_IMPACT = "cai:events:high_impact"
    EVENTS_ALL = "cai:events:all"
    SAFETY_KILL_SWITCHES = "cai:safety:kill_switches"
    SAFETY_TRIGGERS = "cai:safety:triggers"
    MODELS_STATE_CHANGES = "cai:models:state_changes"
    MODELS_DRIFT_ALERTS = "cai:models:drift_alerts"

    @staticmethod
    def signals_for_symbol(symbol: str) -> str:
        return RedisChannels.SIGNALS_SYMBOL.format(symbol=symbol)

    @staticmethod
    def regime_for_symbol(symbol: str) -> str:
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
