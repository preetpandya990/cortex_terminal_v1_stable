"""
Cortex AI — Base WebSocket Service
====================================
Abstract base providing connect/reconnect/send/disconnect lifecycle for
services that maintain a persistent upstream WebSocket connection.

Subclasses implement on_message() and optionally on_connected().
Supports dynamic WebSocket URLs via _get_websocket_url() hook for services
that require fresh authorization URLs (e.g., Upstox v3 one-time-use URLs).
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
    
    Supports dynamic WebSocket URLs via _get_websocket_url() hook for services
    that require fresh authorization URLs (e.g., Upstox v3).
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
        """Called for every inbound message. Implement in subclass."""

    async def on_connected(self) -> None:
        """Called after successful connection. Override to send subscriptions."""

    def _get_connection_headers(self) -> dict[str, str]:
        """Return extra HTTP headers for the WebSocket handshake. Override in subclass."""
        return {}

    async def _get_websocket_url(self) -> str:
        """
        Return WebSocket URL to connect to. Override for dynamic URLs.
        
        Default: Returns static URL from constructor.
        Override: Fetch fresh authorization URL (e.g., Upstox v3).
        """
        if not self._ws_url:
            raise UpstoxConnectionError("WebSocket URL not configured")
        return self._ws_url

    # ── Lifecycle ──────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        """Connect and start the message loop with exponential back-off."""
        self._running = True
        delay = self._reconnect_delay

        while self._running:
            try:
                # Fetch fresh WebSocket URL (may be dynamic for Upstox v3)
                ws_url = await self._get_websocket_url()
                
                ssl_context = ssl.create_default_context()  # CERT_REQUIRED, verify ON
                # websockets 13.x ships the legacy client on Python 3.11;
                # the legacy client uses `extra_headers`, not `additional_headers`.
                async with websockets.connect(
                    ws_url,
                    ssl=ssl_context,
                    extra_headers=self._get_connection_headers(),
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    delay = self._reconnect_delay  # reset on successful connect
                    logger.info("%s connected to WebSocket", self.__class__.__name__)
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
            except websockets.InvalidStatusCode as exc:
                # Authentication failure (401) or permission denied (403)
                if exc.status_code == 401:
                    logger.warning(
                        "%s authentication failed (401). Token may be expired or invalid. Waiting 30s...",
                        self.__class__.__name__,
                    )
                    # Don't spam reconnect attempts on auth failure
                    await asyncio.sleep(30)
                elif exc.status_code == 403:
                    logger.warning(
                        "%s permission denied (403). App may not be approved for WebSocket access. Waiting 60s...",
                        self.__class__.__name__,
                    )
                    # Longer wait for permission issues
                    await asyncio.sleep(60)
                else:
                    logger.warning(
                        "%s connection failed with status %d. Reconnecting in %.1fs",
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
