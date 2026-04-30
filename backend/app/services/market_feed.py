"""
Cortex AI — Market Feed Service
=================================
Single authenticated WebSocket connection to Upstox Market Data Feed V3
(ltpc mode). Decodes protobuf frames, throttles per-instrument, and
publishes decoded price ticks to Redis for fan-out to all API processes.

Design principles:
  - One Upstox WS connection shared across all subscribers (ref-counted)
  - Per-instrument 250 ms throttle before Redis publish (configurable)
  - Mock mode (GBM) when no Upstox token is present — same Redis path
  - Prometheus metrics for every observable failure and throughput signal
  - Graceful shutdown: unsub all, close WS with code 1000

Redis channel: cai:market-feed:ltpc
  Payload: {"type":"ltpc","instrument_key":"...","ltp":2851.5,"cp":2840.0,"ts":1704067200500}
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
_GBM_VOLATILITY  = 0.0002   # 0.02% per tick — realistic intraday noise
_GBM_BASE_PRICE  = 1_000.0
_GBM_INTERVAL_S  = 0.25     # matches 250 ms throttle


# ── Service ────────────────────────────────────────────────────────────────────

class MarketFeedService(BaseWebSocketService):
    """
    Owns the single Upstox Market Data Feed V3 WebSocket connection.

    Lifecycle:
      start()  — registers the service; does not open Upstox WS yet
      subscribe_many(keys) — increments ref counts; opens WS on first subscriber
      unsubscribe_many(keys) — decrements ref counts; sends unsub frames at zero
      stop()   — graceful shutdown: unsubscribes all, closes WS
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

        # asyncio tasks for GBM mock producers
        self._mock_tasks: dict[str, asyncio.Task[None]] = {}

        self._connect_task: asyncio.Task[None] | None = None

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Called during app lifespan startup. No-op; connection is lazy."""
        logger.info("MarketFeedService ready (throttle=%dms)", self._throttle_ms)

    async def stop(self) -> None:
        """Graceful shutdown — unsubscribe all instruments, close Upstox WS."""
        async with self._ref_lock:
            active_keys = list(self._ref_counts.keys())
            self._ref_counts.clear()
            self._last_emitted.clear()

        # Cancel mock tasks
        for task in self._mock_tasks.values():
            task.cancel()
        if self._mock_tasks:
            await asyncio.gather(*self._mock_tasks.values(), return_exceptions=True)
        self._mock_tasks.clear()

        # Send unsub to Upstox then close cleanly
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
        Increment ref counts for instrument keys.
        Keys reaching count 1 are added to the Upstox feed subscription.
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
                await self._send_sub(newly_active)
        else:
            for key in newly_active:
                self._start_mock_task(key)

    async def unsubscribe_many(self, keys: list[str]) -> None:
        """
        Decrement ref counts for instrument keys.
        Keys reaching count 0 are removed from the Upstox feed subscription.
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

        _market_feed_subscriptions.set(len(self._ref_counts))
        logger.debug("Unsubscribing %d instruments: %s", len(newly_inactive), newly_inactive[:5])

        if self._upstox_client.has_token:
            if self._ws and not self._ws.closed:
                await self._send_unsub(newly_inactive)
        else:
            for key in newly_inactive:
                task = self._mock_tasks.pop(key, None)
                if task:
                    task.cancel()

    @property
    def active_subscription_count(self) -> int:
        return len(self._ref_counts)

    # ── BaseWebSocketService hooks ─────────────────────────────────────────────

    async def _get_websocket_url(self) -> str:
        _market_feed_reconnects.inc()
        return await self._upstox_client.get_websocket_auth_url()

    def _get_connection_headers(self) -> dict[str, str]:
        # Upstox v3 embeds auth in the URL — no header needed
        return {}

    async def on_connected(self) -> None:
        """Re-subscribe all active instruments after every (re)connect."""
        async with self._ref_lock:
            active = list(self._ref_counts.keys())
        if active:
            await self._send_sub(active)
            logger.info("Re-subscribed %d instruments after reconnect", len(active))

    async def on_message(self, message: bytes | str) -> None:
        """Decode protobuf frame, throttle, publish to Redis."""
        if isinstance(message, str):
            return  # Upstox v3 only sends binary frames

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

            # Per-instrument throttle
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
        """Start the Upstox WS connection loop if not already running."""
        if self._running and self._connect_task and not self._connect_task.done():
            return
        self._connect_task = asyncio.create_task(
            self.connect(), name="market_feed_upstox_ws"
        )

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
            logger.warning("MarketFeedService: Redis publish error for %s: %s", instrument_key, exc)

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
