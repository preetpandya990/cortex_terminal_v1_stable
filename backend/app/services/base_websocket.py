"""
Cortex AI — Base WebSocket Service
====================================
Abstract base providing connect/reconnect/send/disconnect lifecycle for
services that maintain a persistent upstream WebSocket connection.

Subclasses implement on_message() and optionally on_connected() /
on_disconnected(). Supports dynamic WebSocket URLs via _get_websocket_url()
for services that require fresh authorization URLs (e.g., Upstox v3
one-time-use URLs).
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
    Subclasses implement on_message() and optionally on_connected() /
    on_disconnected().

    TLS: default ssl.create_default_context() — CERT_REQUIRED, hostname
    verification fully enabled. No CERT_NONE, no check_hostname=False.

    Supports dynamic WebSocket URLs via _get_websocket_url() for services
    that require fresh authorization URLs (e.g., Upstox v3).

    Connection lifecycle hooks (all optional to override):
        on_connected()    — called once per successful handshake, before the
                            message loop starts.  Use to send initial frames
                            (e.g., subscriptions).
        on_disconnected() — called every time a live connection drops, before
                            the reconnect delay.  Use to propagate upstream
                            health status downstream.
        on_message()      — called for every inbound binary/text frame.
                            MUST be implemented by subclasses.
    """

    def __init__(self, ws_url: str | None = None) -> None:
        self._ws_url = ws_url
        self._ws: WebSocketClientProtocol | None = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0

    # ── Abstract interface ─────────────────────────────────────────────────────

    @abstractmethod
    async def on_message(self, message: bytes | str) -> None:
        """Called for every inbound frame. Implement in subclass."""

    async def on_connected(self) -> None:
        """
        Called after each successful WebSocket handshake, before the message
        loop starts.  Override to send initial subscription frames.
        """

    async def on_disconnected(self) -> None:
        """
        Called every time a live connection drops (before the reconnect
        sleep).  Override to propagate upstream health status downstream,
        e.g., publish a Redis health-status event.

        NOT called when the service is shutting down intentionally (i.e.,
        after disconnect() sets _running = False), and not called when a
        connection attempt fails before the handshake completes.
        """

    def _get_connection_headers(self) -> dict[str, str]:
        """Return extra HTTP headers for the WebSocket handshake. Override in subclass."""
        return {}

    async def _get_websocket_url(self) -> str:
        """
        Return WebSocket URL to connect to.  Override for dynamic URLs.

        Default: returns static URL from constructor.
        Override: fetch a fresh one-time-use authorization URL (e.g., Upstox v3).
        """
        if not self._ws_url:
            raise UpstoxConnectionError("WebSocket URL not configured")
        return self._ws_url

    # ── Public properties ──────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """True when the upstream WebSocket is live and not closed."""
        return self._ws is not None and not self._ws.closed

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """
        Connect to the upstream WebSocket and start the message loop.

        Reconnects automatically with exponential back-off (1 s → 60 s) on
        any connection failure or unexpected closure.  Calls on_connected()
        after every successful handshake and on_disconnected() after every
        live-connection drop (not on auth failure before handshake, and not
        on intentional shutdown).
        """
        self._running = True
        delay = self._reconnect_delay

        while self._running:
            try:
                # Fetch a fresh URL each attempt (no-op for static URLs;
                # essential for Upstox v3 one-time-use auth URLs).
                ws_url = await self._get_websocket_url()

                ssl_context = ssl.create_default_context()  # CERT_REQUIRED, verify ON
                async with websockets.connect(
                    ws_url,
                    ssl=ssl_context,
                    extra_headers=self._get_connection_headers(),
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    delay = self._reconnect_delay  # reset backoff on success
                    logger.info("%s connected to upstream WebSocket", self.__class__.__name__)

                    try:
                        await self.on_connected()
                        async for message in ws:
                            if not self._running:
                                break
                            try:
                                await self.on_message(message)
                            except Exception:
                                logger.exception(
                                    "%s error processing message",
                                    self.__class__.__name__,
                                )
                    finally:
                        # Explicit clear so is_connected returns False the moment
                        # the socket drops, before the reconnect sleep begins.
                        self._ws = None
                        # Only notify downstream if this was an unexpected drop —
                        # not on intentional shutdown (_running = False).
                        if self._running:
                            try:
                                await self.on_disconnected()
                            except Exception:
                                logger.exception(
                                    "%s error in on_disconnected",
                                    self.__class__.__name__,
                                )

            except websockets.InvalidStatusCode as exc:
                # Handshake failed — we were never connected, so on_disconnected
                # is not appropriate here.
                if exc.status_code == 401:
                    logger.warning(
                        "%s authentication failed (401). Token may be expired. "
                        "Waiting 30 s before retry.",
                        self.__class__.__name__,
                    )
                    await asyncio.sleep(30)
                elif exc.status_code == 403:
                    logger.warning(
                        "%s permission denied (403). App may not be approved for "
                        "WebSocket access. Waiting 60 s before retry.",
                        self.__class__.__name__,
                    )
                    await asyncio.sleep(60)
                else:
                    logger.warning(
                        "%s handshake failed (status=%d). Reconnecting in %.1f s.",
                        self.__class__.__name__,
                        exc.status_code,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self._max_reconnect_delay)

            except (websockets.ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                if not self._running:
                    break
                logger.warning(
                    "%s disconnected (%s). Reconnecting in %.1f s.",
                    self.__class__.__name__,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

            except Exception:
                logger.exception("%s unexpected error in connection loop", self.__class__.__name__)
                if not self._running:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_reconnect_delay)

    async def disconnect(self) -> None:
        """Signal the message loop to stop and close the upstream WebSocket."""
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
            logger.info("%s upstream connection closed", self.__class__.__name__)

    async def send(self, payload: bytes | str) -> None:
        """
        Send a frame to the upstream WebSocket.

        Raises:
            UpstoxConnectionError: if the connection is not currently live.
        """
        if self._ws is None or self._ws.closed:
            raise UpstoxConnectionError("WebSocket not connected")
        await self._ws.send(payload)
