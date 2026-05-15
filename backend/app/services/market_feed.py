"""
Cortex AI — Market Feed Service
=================================
Single authenticated WebSocket connection to Upstox Market Data Feed V3
(ltpc mode). Decodes protobuf frames, throttles per-instrument, and
publishes decoded price ticks to Redis for fan-out to all API processes.

Design principles:
  - One Upstox WS connection shared across all subscribers (ref-counted)
  - Per-instrument 250 ms throttle before Redis publish (configurable)
  - Deferred unsubscription with grace period — prevents the unsub-then-resub
    window that causes stale data on browser restart
  - Upstream health status published to Redis on every connect/disconnect
  - Mock mode (GBM) when no Upstox token is present — same Redis path
  - Prometheus metrics for every observable failure and throughput signal
  - Graceful shutdown: cancels deferred-unsub tasks, unsubscribes all,
    closes WS with code 1000

Redis channels:
  cai:market-feed:ltpc   — per-tick price data (fire-and-forget)
  cai:market-feed:health — upstream WS health events (connected / reconnecting)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
import uuid
from typing import Any

from prometheus_client import Counter, Gauge
from redis.asyncio import Redis

from app.core.redis import RedisChannels
from app.services.base_websocket import BaseWebSocketService
from app.services.proto.MarketDataFeedV3_pb2 import FeedResponse
from app.services.upstox_client import UpstoxClient

logger = logging.getLogger(__name__)

# ── Tuning constants ───────────────────────────────────────────────────────────

# How long to wait before actually sending an unsub to Upstox after ref count
# drops to zero.  If a new subscriber arrives within this window (e.g., browser
# restart) the deferred unsub is cancelled and the Upstox subscription remains
# live — eliminating the stale-data window entirely.
_UNSUB_GRACE_PERIOD_S: float = 5.0

# ── Prometheus metrics ─────────────────────────────────────────────────────────
_market_feed_subscriptions = Gauge(
    "market_feed_subscriptions_active",
    "Number of instrument keys currently subscribed on the Upstox market feed",
)
_market_feed_ticks_received = Counter(
    "market_feed_ticks_received_total",
    "Total tick frames received from Upstox market feed",
)
_market_feed_ticks_emitted = Counter(
    "market_feed_ticks_emitted_total",
    "Tick frames published to Redis after passing the throttle",
)
_market_feed_ticks_discarded = Counter(
    "market_feed_ticks_discarded_total",
    "Tick frames dropped by the per-instrument throttle",
)
_market_feed_redis_errors = Counter(
    "market_feed_redis_publish_errors_total",
    "Failures publishing tick frames to Redis",
)
_market_feed_reconnects = Counter(
    "market_feed_upstox_reconnects_total",
    "Number of reconnection attempts to the Upstox market feed WebSocket",
)

# ── Protobuf helpers ───────────────────────────────────────────────────────────

def _extract_ltpc(feed: Any) -> tuple[float, float] | None:
    """Return (ltp, cp) from a decoded Feed protobuf object, or None."""
    which = feed.WhichOneof("FeedUnion")
    if which == "ltpc":
        ltp = feed.ltpc.ltp
        cp  = feed.ltpc.cp
        return (ltp, cp) if ltp else None
    if which == "fullFeed":
        full  = feed.fullFeed
        inner = full.WhichOneof("FullFeedUnion")
        if inner == "marketFF":
            ltp = full.marketFF.ltpc.ltp
            cp  = full.marketFF.ltpc.cp
            return (ltp, cp) if ltp else None
        if inner == "indexFF":
            ltp = full.indexFF.ltpc.ltp
            cp  = full.indexFF.ltpc.cp
            return (ltp, cp) if ltp else None
    if which == "firstLevelWithGreeks":
        ltp = feed.firstLevelWithGreeks.ltpc.ltp
        cp  = feed.firstLevelWithGreeks.ltpc.cp
        return (ltp, cp) if ltp else None
    return None


# ── Mock GBM parameters ────────────────────────────────────────────────────────
_GBM_VOLATILITY  = 0.0002   # 0.02 % per tick — realistic intraday noise
_GBM_BASE_PRICE  = 1_000.0
_GBM_INTERVAL_S  = 0.25     # matches 250 ms throttle


# ── Service ────────────────────────────────────────────────────────────────────

class MarketFeedService(BaseWebSocketService):
    """
    Owns the single Upstox Market Data Feed V3 WebSocket connection.

    Lifecycle:
        start()             — registers the service; connection is lazy.
        subscribe_many(keys)  — increments ref counts; opens WS on first subscriber.
        unsubscribe_many(keys)— decrements ref counts; schedules a deferred
                                unsub (grace period: _UNSUB_GRACE_PERIOD_S).
                                If a new subscriber arrives within the grace
                                period, the Upstox unsub is suppressed — the
                                connection stays live and ticks resume immediately
                                on browser restart without any stale window.
        stop()              — graceful shutdown: cancels deferred-unsub tasks,
                              unsubscribes all active instruments, closes WS.

    Upstream health:
        on_connected()      — publishes {"type":"upstream_status","status":"connected"}
                              to MARKET_FEED_HEALTH and re-subscribes active instruments.
        on_disconnected()   — publishes {"type":"upstream_status","status":"reconnecting"}
                              to MARKET_FEED_HEALTH.
        is_upstream_connected — True when the Upstox WS is currently live.
    """

    def __init__(
        self,
        upstox_client: UpstoxClient,
        redis: Redis,
        throttle_ms: int = 250,
    ) -> None:
        super().__init__()
        self._upstox_client  = upstox_client
        self._redis          = redis
        self._throttle_ms    = throttle_ms

        # instrument_key → active subscriber count
        self._ref_counts: dict[str, int] = {}
        self._ref_lock = asyncio.Lock()

        # instrument_key → last emit timestamp (ms)
        self._last_emitted: dict[str, float] = {}

        # asyncio tasks for GBM mock producers (no-token dev mode)
        self._mock_tasks: dict[str, asyncio.Task[None]] = {}

        # The long-running task that drives the Upstox WS connection loop
        self._connect_task: asyncio.Task[None] | None = None

        # Deferred-unsub tasks: tracked so stop() can cancel them cleanly.
        # The set shrinks as each task completes or is cancelled.
        self._pending_unsub_tasks: set[asyncio.Task[None]] = set()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Called during app lifespan startup. Connection is lazy — no-op."""
        logger.info("MarketFeedService ready (throttle=%d ms)", self._throttle_ms)

    async def stop(self) -> None:
        """
        Graceful shutdown:
          1. Cancel all pending deferred-unsub tasks.
          2. Unsubscribe all active instruments from Upstox.
          3. Disconnect from the Upstox WebSocket.
          4. Cancel the connection loop task.
        """
        # 1. Cancel deferred-unsub tasks so they don't fire after shutdown.
        if self._pending_unsub_tasks:
            for task in list(self._pending_unsub_tasks):
                task.cancel()
            await asyncio.gather(*self._pending_unsub_tasks, return_exceptions=True)
            self._pending_unsub_tasks.clear()

        # 2. Cancel mock producers.
        for task in self._mock_tasks.values():
            task.cancel()
        if self._mock_tasks:
            await asyncio.gather(*self._mock_tasks.values(), return_exceptions=True)
        self._mock_tasks.clear()

        # 3. Snapshot active keys before clearing state.
        async with self._ref_lock:
            active_keys = list(self._ref_counts.keys())
            self._ref_counts.clear()
            self._last_emitted.clear()

        # 4. Send unsub to Upstox then close.
        if active_keys and self._ws and not self._ws.closed:
            try:
                await self._send_unsub(active_keys)
            except Exception:
                pass

        await self.disconnect()

        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except asyncio.CancelledError:
                pass

        _market_feed_subscriptions.set(0)
        logger.info("MarketFeedService stopped")

    async def subscribe_many(self, keys: list[str]) -> None:
        """
        Increment ref counts for the given instrument keys.

        Keys whose count reaches 1 (first subscriber) are added to the live
        Upstox subscription.  If the Upstox WS is not yet connected the sub
        is queued via _ref_counts and sent by on_connected() on the next
        successful handshake.
        """
        if not keys:
            return

        newly_active: list[str] = []
        async with self._ref_lock:
            for key in keys:
                count = self._ref_counts.get(key, 0) + 1
                self._ref_counts[key] = count
                if count == 1:
                    newly_active.append(key)

        if not newly_active:
            return

        _market_feed_subscriptions.set(len(self._ref_counts))
        logger.debug("Subscribing %d new instruments: %s", len(newly_active), newly_active[:5])

        if self._upstox_client.has_token:
            await self._ensure_connected()
            if self._ws and not self._ws.closed:
                # Upstox WS is already live — send immediately.
                await self._send_sub(newly_active)
            # else: Upstox WS is reconnecting; on_connected() will re-subscribe
            # from _ref_counts once the handshake completes.
        else:
            for key in newly_active:
                self._start_mock_task(key)

    async def unsubscribe_many(self, keys: list[str]) -> None:
        """
        Decrement ref counts for the given instrument keys.

        Keys whose count reaches 0 (last subscriber) schedule a deferred
        unsub after _UNSUB_GRACE_PERIOD_S seconds.  If a new subscriber
        arrives within that window (e.g., browser restart), the Upstox unsub
        is suppressed — the connection stays live and ticks resume immediately
        without a stale window.
        """
        if not keys:
            return

        newly_inactive: list[str] = []
        async with self._ref_lock:
            for key in keys:
                count = max(0, self._ref_counts.get(key, 0) - 1)
                if count == 0:
                    self._ref_counts.pop(key, None)
                    self._last_emitted.pop(key, None)
                    newly_inactive.append(key)
                else:
                    self._ref_counts[key] = count

        if not newly_inactive:
            return

        logger.debug(
            "Scheduling deferred unsub for %d instruments (grace=%.0f s): %s",
            len(newly_inactive), _UNSUB_GRACE_PERIOD_S, newly_inactive[:5],
        )
        task = asyncio.create_task(
            self._deferred_unsub(newly_inactive),
            name=f"mf_deferred_unsub_{newly_inactive[0]}",
        )
        self._pending_unsub_tasks.add(task)
        task.add_done_callback(self._pending_unsub_tasks.discard)

    @property
    def active_subscription_count(self) -> int:
        return len(self._ref_counts)

    @property
    def is_upstream_connected(self) -> bool:
        """True when the Upstox WebSocket is currently live and authenticated."""
        return self.is_connected  # delegates to BaseWebSocketService

    # ── BaseWebSocketService hooks ─────────────────────────────────────────────

    async def _get_websocket_url(self) -> str:
        _market_feed_reconnects.inc()
        return await self._upstox_client.get_websocket_auth_url()

    def _get_connection_headers(self) -> dict[str, str]:
        # Upstox v3 embeds auth in the URL — no extra headers needed.
        return {}

    async def on_connected(self) -> None:
        """
        Called by BaseWebSocketService after every successful Upstox handshake.

        1. Publishes connected health status to MARKET_FEED_HEALTH so all
           frontend clients immediately see the upstream is live.
        2. Re-subscribes all currently tracked instruments (handles the case
           where clients subscribed while the WS was reconnecting).
        """
        # Publish upstream health — non-fatal if Redis is momentarily unavailable.
        try:
            await self._redis.publish(
                RedisChannels.MARKET_FEED_HEALTH,
                json.dumps({
                    "type": "upstream_status",
                    "status": "connected",
                    "ts": int(time.time() * 1000),
                }),
            )
        except Exception:
            logger.exception("MarketFeedService: failed to publish connected health status")

        # Re-subscribe every instrument that accumulated in _ref_counts while
        # the WS was down.  This is the authoritative "catch-all" recovery path.
        async with self._ref_lock:
            active = list(self._ref_counts.keys())
        if active:
            await self._send_sub(active)
            logger.info(
                "MarketFeedService: re-subscribed %d instruments after reconnect",
                len(active),
            )

    async def on_disconnected(self) -> None:
        """
        Called by BaseWebSocketService each time the Upstox WS drops
        (before the reconnect sleep).

        Publishes reconnecting health status to MARKET_FEED_HEALTH so all
        frontend clients can immediately display a staleness indicator.
        """
        try:
            await self._redis.publish(
                RedisChannels.MARKET_FEED_HEALTH,
                json.dumps({
                    "type": "upstream_status",
                    "status": "reconnecting",
                    "ts": int(time.time() * 1000),
                }),
            )
        except Exception:
            logger.exception("MarketFeedService: failed to publish disconnected health status")

    async def on_message(self, message: bytes | str) -> None:
        """Decode protobuf frame, apply per-instrument throttle, publish to Redis."""
        if isinstance(message, str):
            return  # Upstox v3 sends binary frames only

        _market_feed_ticks_received.inc()

        try:
            response = FeedResponse()
            response.ParseFromString(message)
        except Exception:
            logger.warning("MarketFeedService: failed to decode protobuf frame — skipping")
            return

        if not response.feeds:
            return

        now_ms = time.time() * 1000
        ts     = response.currentTs or int(now_ms)

        for instrument_key, feed in response.feeds.items():
            result = _extract_ltpc(feed)
            if result is None:
                continue

            ltp, cp = result

            # Per-instrument throttle — discard ticks arriving faster than
            # _throttle_ms to avoid Redis saturation under heavy market activity.
            if now_ms - self._last_emitted.get(instrument_key, 0) < self._throttle_ms:
                _market_feed_ticks_discarded.inc()
                continue

            self._last_emitted[instrument_key] = now_ms
            asyncio.create_task(
                self._publish(instrument_key, ltp, cp, ts),
                name=f"mf_publish_{instrument_key}",
            )

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _ensure_connected(self) -> None:
        """Start the Upstox WS connection loop if it is not already running."""
        if self._running and self._connect_task and not self._connect_task.done():
            return
        self._connect_task = asyncio.create_task(
            self.connect(), name="market_feed_upstox_ws"
        )

    async def _deferred_unsub(self, keys: list[str]) -> None:
        """
        Wait _UNSUB_GRACE_PERIOD_S then send unsub to Upstox only for
        instruments still at ref count 0.

        If a new subscriber arrives within the grace window (e.g., browser
        restart), subscribe_many() increments the ref count back to ≥ 1 and
        this task silently becomes a no-op — the Upstox subscription stays
        live and ticks resume without interruption.
        """
        try:
            await asyncio.sleep(_UNSUB_GRACE_PERIOD_S)
        except asyncio.CancelledError:
            return  # stop() cancelled this task during shutdown — nothing to do

        async with self._ref_lock:
            # Only unsub instruments that are still at ref count 0.
            still_zero = [k for k in keys if k not in self._ref_counts]

        if not still_zero:
            # All instruments were resubscribed within the grace window.
            logger.debug(
                "Deferred unsub suppressed — %d instrument(s) resubscribed within grace window",
                len(keys),
            )
            return

        _market_feed_subscriptions.set(len(self._ref_counts))
        logger.debug(
            "Deferred unsub executing for %d instrument(s): %s",
            len(still_zero), still_zero[:5],
        )

        if self._upstox_client.has_token:
            if self._ws and not self._ws.closed:
                await self._send_unsub(still_zero)
        else:
            for key in still_zero:
                task = self._mock_tasks.pop(key, None)
                if task:
                    task.cancel()

    async def _send_sub(self, keys: list[str]) -> None:
        payload = json.dumps({
            "guid": str(uuid.uuid4()),
            "method": "sub",
            "data": {"mode": "ltpc", "instrumentKeys": keys},
        }).encode()
        await self.send(payload)

    async def _send_unsub(self, keys: list[str]) -> None:
        payload = json.dumps({
            "guid": str(uuid.uuid4()),
            "method": "unsub",
            "data": {"instrumentKeys": keys},
        }).encode()
        await self.send(payload)

    async def _publish(
        self, instrument_key: str, ltp: float, cp: float, ts: int
    ) -> None:
        """Publish an ltpc tick to Redis. Fire-and-forget; errors are counted."""
        payload = json.dumps({
            "type": "ltpc",
            "instrument_key": instrument_key,
            "ltp": ltp,
            "cp": cp,
            "ts": ts,
        })
        try:
            await self._redis.publish(RedisChannels.MARKET_FEED_LTPC, payload)
            _market_feed_ticks_emitted.inc()
        except Exception as exc:
            _market_feed_redis_errors.inc()
            logger.warning(
                "MarketFeedService: Redis publish error for %s: %s",
                instrument_key, exc,
            )

    # ── Mock mode (no Upstox token) ────────────────────────────────────────────

    def _start_mock_task(self, instrument_key: str) -> None:
        if instrument_key in self._mock_tasks:
            return
        task = asyncio.create_task(
            self._mock_producer(instrument_key),
            name=f"mf_mock_{instrument_key}",
        )
        self._mock_tasks[instrument_key] = task

    async def _mock_producer(self, instrument_key: str) -> None:
        """
        Emits synthetic GBM ticks to Redis at the throttle interval.
        Uses the same Redis publish path as live mode — frontend is unaware.
        """
        price = _GBM_BASE_PRICE
        cp    = price  # mock previous close = starting price

        try:
            while True:
                price *= math.exp(random.gauss(0, _GBM_VOLATILITY))
                ts = int(time.time() * 1000)
                await self._publish(instrument_key, round(price, 2), round(cp, 2), ts)
                await asyncio.sleep(_GBM_INTERVAL_S)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Mock producer error for %s", instrument_key)


# ── FastAPI dependency ─────────────────────────────────────────────────────────
from fastapi import Request as FastAPIRequest  # noqa: E402


def get_market_feed_service(request: FastAPIRequest) -> MarketFeedService:
    return request.app.state.market_feed_service
