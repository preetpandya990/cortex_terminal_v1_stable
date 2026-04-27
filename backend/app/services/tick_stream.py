"""
Cortex AI — Tick Stream Service
=================================
Maintains a single authenticated WebSocket connection to the Upstox v3
market-data feed and fans out decoded ticks to all registered subscribers.

Protocol: binary protobuf frames (MarketDataFeedV3.proto).
Authentication: Bearer token injected as HTTP header during the WS handshake.
Subscription: JSON-encoded payload sent as a binary frame (Upstox requirement).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Coroutine
from typing import Any
from fastapi import Request as FastAPIRequest

from app.services.base_websocket import BaseWebSocketService
from app.services.upstox_client import UpstoxClient
from app.services.proto.MarketDataFeedV3_pb2 import FeedResponse

logger = logging.getLogger(__name__)

TickCallback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]


def _extract_ltp(feed: Any) -> float | None:
    """Extract last traded price from a decoded Feed protobuf object."""
    which = feed.WhichOneof("FeedUnion")
    if which == "ltpc":
        return feed.ltpc.ltp or None
    if which == "fullFeed":
        full = feed.fullFeed
        inner = full.WhichOneof("FullFeedUnion")
        if inner == "marketFF":
            return full.marketFF.ltpc.ltp or None
        if inner == "indexFF":
            return full.indexFF.ltpc.ltp or None
    if which == "firstLevelWithGreeks":
        return feed.firstLevelWithGreeks.ltpc.ltp or None
    return None


def _feed_to_dict(feed: Any) -> dict[str, Any]:
    """Normalise a Feed protobuf object into a plain dict for subscribers."""
    which = feed.WhichOneof("FeedUnion")

    if which == "ltpc":
        ltpc = feed.ltpc
        return {
            "ltpc": {
                "ltp": ltpc.ltp,
                "ltt": ltpc.ltt,
                "ltq": ltpc.ltq,
                "cp": ltpc.cp,
            }
        }

    if which == "fullFeed":
        full = feed.fullFeed
        inner = full.WhichOneof("FullFeedUnion")
        if inner == "marketFF":
            mff = full.marketFF
            ltpc = mff.ltpc
            return {
                "ltpc": {"ltp": ltpc.ltp, "ltt": ltpc.ltt, "ltq": ltpc.ltq, "cp": ltpc.cp},
                "fullFeed": {
                    "marketFF": {
                        "atp": mff.atp,
                        "vtt": mff.vtt,
                        "oi": mff.oi,
                        "iv": mff.iv,
                        "tbq": mff.tbq,
                        "tsq": mff.tsq,
                    }
                },
            }
        if inner == "indexFF":
            iff = full.indexFF
            ltpc = iff.ltpc
            return {
                "ltpc": {"ltp": ltpc.ltp, "ltt": ltpc.ltt, "ltq": ltpc.ltq, "cp": ltpc.cp},
                "fullFeed": {"indexFF": {}},
            }

    if which == "firstLevelWithGreeks":
        flg = feed.firstLevelWithGreeks
        ltpc = flg.ltpc
        return {
            "ltpc": {"ltp": ltpc.ltp, "ltt": ltpc.ltt, "ltq": ltpc.ltq, "cp": ltpc.cp},
        }

    return {}


class TickStreamService(BaseWebSocketService):
    """
    Manages a single Upstox market-data WebSocket connection and fans out
    decoded ticks to all registered per-instrument async callbacks.
    """

    def __init__(self, upstox_client: UpstoxClient) -> None:
        super().__init__()  # No static URL - will fetch dynamically
        self._upstox_client = upstox_client
        self._subscribers: dict[str, TickCallback] = {}
        self._subscribed_symbols: set[str] = set()

    # ── Dynamic WebSocket URL ──────────────────────────────────────────────────
    async def _get_websocket_url(self) -> str:
        """
        Fetch fresh one-time-use WebSocket authorization URL from Upstox API v3.
        
        This URL contains a single-use authentication code and must be obtained
        before each connection attempt. The URL expires after use.
        
        Raises:
            UpstoxAPIError: If authorization fails (no token, 401, 403, etc.)
            UpstoxConnectionError: If network/timeout error occurs
        """
        if not self._upstox_client.has_token:
            raise UpstoxConnectionError(
                "Cannot connect to Upstox WebSocket: No access token configured. "
                "Please set UPSTOX_ACCESS_TOKEN in environment or authenticate via OAuth."
            )
        
        return await self._upstox_client.get_websocket_auth_url()

    # ── Auth header injection (DEPRECATED for v3) ─────────────────────────────
    def _get_connection_headers(self) -> dict[str, str]:
        """
        Return empty headers for Upstox v3.
        
        Note: Upstox API v3 embeds authentication in the WebSocket URL itself
        (via the 'code' query parameter). No Bearer token header is needed.
        """
        return {}

    # ── Subscriber management ──────────────────────────────────────────────────
    def subscribe(self, subscriber_id: str, callback: TickCallback) -> None:
        self._subscribers[subscriber_id] = callback
        logger.info("Tick subscriber registered: %s", subscriber_id)

    def unsubscribe(self, subscriber_id: str) -> None:
        self._subscribers.pop(subscriber_id, None)
        logger.info("Tick subscriber removed: %s", subscriber_id)

    # ── BaseWebSocketService hooks ─────────────────────────────────────────────
    async def on_connected(self) -> None:
        """Re-subscribe all tracked symbols on every (re)connect."""
        if self._subscribed_symbols:
            await self._send_subscription(list(self._subscribed_symbols))

    async def on_message(self, message: bytes | str) -> None:
        """
        Decode binary protobuf frame, normalise, fan out to all subscribers.
        Upstox v3 sends binary protobuf — text frames are not expected.
        """
        if isinstance(message, str):
            logger.debug("Received unexpected text frame from Upstox feed — skipping")
            return

        try:
            response = FeedResponse()
            response.ParseFromString(message)
        except Exception:
            logger.warning("Failed to decode protobuf tick message — skipping frame")
            return

        if not response.feeds or not self._subscribers:
            return

        # Build normalised payload once; all subscribers receive the same dict
        tick_payload: dict[str, Any] = {
            "type": response.type,
            "currentTs": response.currentTs,
            "feeds": {
                key: _feed_to_dict(feed)
                for key, feed in response.feeds.items()
                if _feed_to_dict(feed)
            },
        }

        if not tick_payload["feeds"]:
            return

        await asyncio.gather(
            *(cb(tick_payload) for cb in self._subscribers.values()),
            return_exceptions=True,
        )

    # ── Symbol subscription ────────────────────────────────────────────────────
    async def add_symbols(self, instrument_keys: list[str]) -> None:
        new_keys = set(instrument_keys) - self._subscribed_symbols
        if not new_keys:
            return
        self._subscribed_symbols.update(new_keys)
        if self._ws and not self._ws.closed:
            await self._send_subscription(list(new_keys))

    async def _send_subscription(self, instrument_keys: list[str]) -> None:
        # Upstox requires the subscription message to be sent as a binary frame.
        payload = json.dumps(
            {
                "guid": str(uuid.uuid4()),
                "method": "sub",
                "data": {"mode": "ltpc", "instrumentKeys": instrument_keys},
            }
        ).encode("utf-8")
        await self.send(payload)
        logger.info("Subscribed %d instruments on Upstox market feed", len(instrument_keys))


# ── FastAPI dependency ─────────────────────────────────────────────────────────
def get_tick_service(request: FastAPIRequest) -> TickStreamService:
    return request.app.state.tick_service
