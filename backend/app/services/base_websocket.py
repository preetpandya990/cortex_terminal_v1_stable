"""
Cortex AI — Base WebSocket Service
====================================
Eliminates the maintenance fork between TickStreamService and
UpstoxStreamIngestionService by extracting the shared WebSocket lifecycle
into a proper abstract base class with hook methods for subclass behaviour.
"""
from __future__ import annotations

import asyncio
import logging
import ssl
from abc import ABC, abstractmethod

import websockets
from websockets.client import WebSocketClientProtocol

from app.core.config import get_settings
from app.exceptions import UpstoxConnectionError

logger = logging.getLogger(__name__)
settings = get_settings()


class BaseWebSocketService(ABC):
    """
    Abstract base providing connect/subscribe/handle/disconnect lifecycle.
    Subclasses implement on_message() and optionally on_connected().

    TLS: default ssl.create_default_context() — certificate verification fully enabled.
    No CERT_NONE, no check_hostname=False.
    """

    def __init__(self, ws_url: str | None = None) -> None:
        self._ws_url = ws_url or settings.UPSTOX_WS_URL
        self._ws: WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0

    # ── Abstract interface ─────────────────────────────────────────────────────
    @abstractmethod
    async def on_message(self, message: bytes | str) -> None:
        """Called for every inbound message. Implement in subclass."""

    async def on_connected(self) -> None:
        """Called after successful connection. Override to send subscriptions."""

    # ── Lifecycle ──────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Connect and start the message loop with exponential back-off."""
        self._running = True
        delay = self._reconnect_delay

        while self._running:
            try:
                ssl_context = ssl.create_default_context()  # CERT_REQUIRED, verify ON
                async with websockets.connect(
                    self._ws_url,
                    ssl=ssl_context,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    delay = self._reconnect_delay  # reset on successful connect
                    logger.info("%s connected to %s", self.__class__.__name__, self._ws_url)
                    await self.on_connected()
                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            await self.on_message(message)
                        except Exception:
                            logger.exception(
                                "%s error processing message", self.__class__.__name__
                            )
            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                if not self._running:
                    break
                logger.warning(
                    "%s disconnected (%s). Reconnecting in %.1fs",
                    self.__class__.__name__,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)
            except Exception:
                logger.exception("%s unexpected error", self.__class__.__name__)
                if not self._running:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def disconnect(self) -> None:
        """Signal the loop to stop and close the WebSocket."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
            logger.info("%s disconnected", self.__class__.__name__)

    async def send(self, payload: bytes | str) -> None:
        """Send a message to the server. Raises UpstoxConnectionError if not connected."""
        if self._ws is None or self._ws.closed:
            raise UpstoxConnectionError("WebSocket not connected")
        await self._ws.send(payload)