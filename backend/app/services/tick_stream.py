"""
Cortex AI — Tick Stream Service
=================================
Real-time tick fan-out to registered subscribers.
Extends BaseWebSocketService — no duplicated connection lifecycle code.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any
from fastapi import Request as FastAPIRequest

from app.services.base_websocket import BaseWebSocketService
from app.services.upstox_client import UpstoxClient

logger = logging.getLogger(__name__)

TickCallback = Callable[[dict], Coroutine[Any, Any, None]]


class TickStreamService(BaseWebSocketService):
    """
    Manages a single Upstox market-data WebSocket connection and fans out
    incoming ticks to all registered async subscriber callbacks.
    """

    def __init__(self, upstox_client: UpstoxClient) -> None:
        super().__init__()
        self._upstox_client = upstox_client
        self._subscribers: dict[str, TickCallback] = {}
        self._subscribed_symbols: set[str] = set()

    # ── Subscriber management ──────────────────────────────────────────────────
    def subscribe(self, subscriber_id: str, callback: TickCallback) -> None:
        self._subscribers[subscriber_id] = callback
        logger.info("Tick subscriber registered: %s", subscriber_id)

    def unsubscribe(self, subscriber_id: str) -> None:
        self._subscribers.pop(subscriber_id, None)
        logger.info("Tick subscriber removed: %s", subscriber_id)

    # ── BaseWebSocketService hooks ─────────────────────────────────────────────
    async def on_connected(self) -> None:
        """Re-subscribe all active instrument symbols on (re)connect."""
        if self._subscribed_symbols:
            await self._send_subscription(list(self._subscribed_symbols))

    async def on_message(self, message: bytes | str) -> None:
        """Parse tick and fan out to all subscribers concurrently."""
        try:
            tick = json.loads(message) if isinstance(message, str) else json.loads(message.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Received non-JSON tick message — skipping")
            return

        if not self._subscribers:
            return

        await asyncio.gather(
            *(cb(tick) for cb in self._subscribers.values()),
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
        payload = json.dumps(
            {
                "guid": "cortex-tick-sub",
                "method": "sub",
                "data": {"mode": "full", "instrumentKeys": instrument_keys},
            }
        )
        await self.send(payload)
        logger.info("Subscribed to %d instruments", len(instrument_keys))


# ── FastAPI dependency ─────────────────────────────────────────────────────────
def get_tick_service(request: FastAPIRequest) -> TickStreamService:
    return request.app.state.tick_service