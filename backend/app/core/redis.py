"""
Cortex AI — Redis Client with Pub/Sub
======================================
Async Redis with connection pooling, cache service, and pub/sub for real-time events.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
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
    
    # ── LLM Explanation Pipeline ───────────────────────────────────────────────
    LLM_EXPLANATION_PENDING = "cortex:llm:explanation:pending"
    """
    Trigger channel — published by the correlation engine immediately after a
    trade suggestion is committed to DB.  The explanation worker is the sole
    subscriber.

    Payload:
        {
            "suggestion_id": "uuid-string",
            "id": 123              # integer PK (used as reference_id in audit log)
        }
    """

    LLM_EXPLANATION_READY = "cortex:llm:explanation:ready:{suggestion_id}"
    """
    Per-suggestion notification published by the explanation worker once the
    LLM explanation has been written to the DB.  The SSE stream subscribes to
    the ``cortex:llm:explanation:ready:*`` pattern and emits an analysis_update
    immediately on receipt, bypassing the polling cycle.

    Channel name: substitute {suggestion_id} with the UUID string.
    Payload:
        {
            "suggestion_id": "uuid-string",
            "llm_summary":   "2-3 sentence summary...",
            "model":         "nim/qwen3.5-122b-a10b",
            "generated_at":  "2026-06-04T12:00:00Z",
            "sources": [
                {
                    "source_name": "Economic Times Markets",
                    "as_of":       "2026-06-04T10:30:00+00:00",
                    "source_url":  "https://..."
                }
            ]
        }
    """

    LLM_CONTEXT_PENDING = "cortex:llm:context:pending"
    """
    Trigger channel published by the SSE stream when an instrument has no active
    suggestion AND no valid cached context.  The explanation worker is the sole
    subscriber and dispatches to ``_generate_instrument_context``.

    A Redis distributed lock (SET NX EX 120) on
    ``cortex:instrument_context:generating:{instrument_key}`` prevents duplicate
    generation requests from concurrent SSE connections on the same instrument.

    Payload:
        {
            "instrument_key":  "NSE_EQ|INE002A01018",
            "symbol":          "RELIANCE",          # may be null
            "prediction_data": { ... } | null       # current ML snapshot from SSE state
        }
    """

    LLM_CONTEXT_READY = "cortex:llm:context:ready:{instrument_key}"
    """
    Per-instrument notification published by the explanation worker after a
    successful ``_generate_instrument_context`` run.  The SSE stream subscribes to
    the ``cortex:llm:context:ready:*`` pattern and emits an analysis_update
    immediately on receipt.

    Channel name: substitute {instrument_key} with the full instrument key string
    (URL-safe; the colons and pipe in "NSE_EQ|..." are fine in Redis channel names).

    Payload:
        {
            "instrument_key":  "NSE_EQ|INE002A01018",
            "context_summary": "2-3 sentence market context...",
            "context_full":    "Full narrative with citations...",
            "model":           "nim/qwen3.5-122b-a10b",
            "generated_at":    "2026-06-06T12:00:00Z",
            "sources": [
                {
                    "source_name": "Economic Times Markets",
                    "as_of":       "2026-06-06T10:30:00+00:00",
                    "source_url":  "https://..."
                }
            ]
        }
    """


    # ── Gemini Quota ──────────────────────────────────────────────────────────
    GEMINI_QUOTA_RESET = "cai:gemini:quota:reset"
    """
    Published by GeminiRequestManager after midnight PT quota reset clears all
    open circuits.  The explanation worker subscribes and auto-requeues any
    ``gemini_quota_exhausted`` DLQ entries from the previous quota day.

    Payload:
        {
            "reset_at":    "2026-06-30T00:15:00Z",   # UTC timestamp of the reset
            "keys_reset":  3                          # number of circuits cleared
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

    MARKET_FEED_HEALTH = "cai:market-feed:health"
    """
    Upstream Upstox WebSocket health-status events.

    Published by MarketFeedService whenever the Upstox WS connects or drops.
    Subscribed by /upstox/market-feed/ws handlers and forwarded unconditionally
    to all connected frontend clients so they can display live/stale indicators.

    Payload:
        {
            "type": "upstream_status",
            "status": "connected" | "reconnecting",
            "ts": 1704067200500   (Unix epoch ms)
        }
    """

    # ── Correlation Events ─────────────────────────────────────────────────────
    CORRELATIONS_STARTED = "cai:correlations:started"
    """
    ML correlation pipeline started for a symbol.
    Published immediately when the engine picks up a scanner anomaly or news
    event, before any signal gathering begins.  Powers the ML Activity live feed.

    Payload:
        {
            "correlation_id": "<uuid>",
            "symbol":         "<instrument_key>",
            "trading_symbol": "<nse_ticker>",
            "trigger_type":   "SCANNER_ANOMALY" | "NEWS_EVENT",
            "started_at":     "<iso8601>"
        }
    """

    CORRELATIONS_COMPLETED = "cai:correlations:completed"
    """
    Correlation analysis completed — consensus reached, suggestion committed.

    Payload:
        {
            "correlation_id":  "<uuid>",
            "suggestion_id":   "<uuid>",
            "symbol":          "<instrument_key>",
            "trading_symbol":  "<nse_ticker>",
            "trigger_type":    "SCANNER_ANOMALY" | "NEWS_EVENT",
            "consensus_score": <float 0-100>,
            "latencies":       {"scanner_ms": <int>, "ai_ms": <int>, "ml_ms": <int>},
            "completed_at":    "<iso8601>"
        }
    """

    CORRELATIONS_REJECTED = "cai:correlations:rejected"
    """
    Correlation analysis rejected — consensus not reached.

    Payload:
        {
            "correlation_id":   "<uuid>",
            "symbol":           "<instrument_key>",
            "trading_symbol":   "<nse_ticker>",
            "trigger_type":     "SCANNER_ANOMALY" | "NEWS_EVENT",
            "rejection_reason": "NEUTRAL_SIGNAL" | "ML_NEUTRAL" | "DIRECTION_MISMATCH"
                                | "LOW_CONFIDENCE" | "DUPLICATE_SUPPRESSED"
                                | "TIMEOUT" | "PROCESSING_ERROR",
            "consensus_score":  <float 0-100>,
            "rejected_at":      "<iso8601>"
        }
    """

    # ── ML Feedback ────────────────────────────────────────────────────────────
    ML_FEEDBACK_ERRORS = "cai:ml:feedback_errors"
    """
    ML feedback computation failed after all retry attempts.

    Payload:
        {
            "outcome_id": "uuid",
            "symbol": "RELIANCE",
            "portfolio_id": "uuid",
            "user_id": 42,
            "error_message": "...",
            "error_type": "sqlalchemy.exc.OperationalError",
            "attempt_count": 3,
            "first_attempt_at": "2026-05-13T12:00:00Z",
            "last_attempt_at": "2026-05-13T12:00:33Z",
            "ml_feedback_error_id": "uuid"
        }
    """

    ML_REGIME_ERRORS = "cai:ml:regime_detection_errors"
    """
    Regime detection batch run encountered a critical failure.

    Payload:
        {
            "error": "...",
            "symbols_attempted": 100,
            "symbols_processed": 42,
            "run_date": "2026-05-13",
            "timestamp": "2026-05-13T10:35:00Z"
        }
    """

    # ── Paper Trading ──────────────────────────────────────────────────────────
    PAPER_PENDING_ORDERS_UPDATED = "cai:paper:pending_orders_updated"
    """
    Signals the pending-order matching engine to rebuild its in-memory cache
    for one or more instrument keys.

    Published after:
      - A new LIMIT/SL/SL-M order is placed (order_service.place_order)
      - An order is cancelled (order_service.cancel_order)
      - An order is filled by the engine (self-invalidation is skipped via SKIP_LOCKED)

    Payload:
        {
            "instrument_key": "NSE_EQ|INE002A01018"   // single instrument to refresh
        }
    Or, for a full cache rebuild (e.g. on startup recovery):
        {
            "instrument_key": null
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


# ── LLM pipeline Redis keys ────────────────────────────────────────────────────
class RedisStreams:
    """
    Redis key helpers for the LLM explanation pipeline's non-queue state.

    Durable job queueing moved to Kafka topics (see app/core/kafka.py); Redis
    keeps the per-job SSE event stores (full payloads, TTL-bound) and the
    in-flight dedup keys.  Pub/sub remains the lightweight wakeup signal.

    Key namespaces
    --------------
    cortex:sse:events:*       Per-job SSE event stores (TTL-bound, full payloads)
    cortex:llm:inflight:*     In-flight dedup keys (TTL=150s, SET NX)
    """

    # ── SSE event stores (per-job, TTL-bound) ──────────────────────────────────

    @staticmethod
    def sse_explanation_key(suggestion_id: str) -> str:
        """Stream key for the per-suggestion SSE event store. TTL: 86 400 s (24 h)."""
        return f"cortex:sse:events:{suggestion_id}"

    @staticmethod
    def sse_context_key(instrument_key: str) -> str:
        """Stream key for the per-instrument context event store. TTL: 3 600 s (1 h)."""
        return f"cortex:sse:events:ctx:{instrument_key}"

    # ── In-flight dedup ────────────────────────────────────────────────────────

    @staticmethod
    def inflight_key(suggestion_id: str) -> str:
        """SET NX key that prevents concurrent workers from duplicating an LLM call."""
        return f"cortex:llm:inflight:{suggestion_id}"


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


# ── Redis metrics helper ───────────────────────────────────────────────────────
@asynccontextmanager
async def _track_redis(operation: str):
    """Async context manager that records redis_operations_total and duration."""
    from app.core.metrics import redis_operations_total, redis_operation_duration_seconds
    start = time.perf_counter()
    try:
        yield
        redis_operations_total.labels(operation=operation, status="success").inc()
    except Exception:
        redis_operations_total.labels(operation=operation, status="error").inc()
        raise
    finally:
        redis_operation_duration_seconds.labels(operation=operation).observe(
            time.perf_counter() - start
        )


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
        async with _track_redis("get"):
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
        async with _track_redis("set"):
            await self._redis.setex(key, ttl, json.dumps(value, default=str))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
    )
    async def delete(self, key: str) -> None:
        async with _track_redis("delete"):
            await self._redis.delete(key)

    async def delete_pattern(self, pattern: str) -> int:
        async with _track_redis("delete_pattern"):
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
