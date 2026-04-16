"""
Cortex AI — Upstox HTTP Client
================================
Single shared httpx.AsyncClient with:
  - Persistent TCP connection pool (no fresh TLS handshake per call)
  - Proper timeout configuration
  - Typed errors surfaced as UpstoxAPIError
  - Token stored in-process (never returned to end clients)
"""
from __future__ import annotations

import logging
from typing import Any
from fastapi import Request as FastAPIRequest

import httpx

from app.core.config import get_settings
from app.exceptions import UpstoxAPIError, UpstoxConnectionError

logger = logging.getLogger(__name__)
settings = get_settings()

# Retry-after and other sensitive headers to strip from logged errors
_SENSITIVE_HEADERS = {"authorization", "x-api-key"}


class UpstoxClient:
    """
    Application-scoped Upstox API client.
    Lifecycle: start() on lifespan startup, stop() on lifespan shutdown.
    Retrieved from app.state.upstox_client via FastAPI dependency.
    """

    def __init__(self) -> None:
        self._access_token: str | None = None
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Initialise the shared connection pool."""
        self._client = httpx.AsyncClient(
            base_url=settings.UPSTOX_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=20,
                keepalive_expiry=30,
            ),
            headers={"Accept": "application/json"},
            # TLS: default ssl.create_default_context() — CERT_REQUIRED, hostname verification ON
        )
        
        # Load token from settings
        if settings.UPSTOX_ACCESS_TOKEN:
            self._access_token = settings.UPSTOX_ACCESS_TOKEN
            logger.info("UpstoxClient HTTP pool started with access token")
        else:
            logger.warning("⚠️  UpstoxClient started WITHOUT access token - will use mock data in development")
        
        logger.info("UpstoxClient HTTP pool started")

    async def stop(self) -> None:
        """Gracefully close the connection pool."""
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("UpstoxClient HTTP pool closed")

    # ── Token management ────────────────────────────────────────────────────
    def set_access_token(self, token: str) -> None:
        """Store the access token in memory. Never expose to API responses."""
        self._access_token = token
        logger.info("Upstox access token updated")

    @property
    def has_token(self) -> bool:
        return self._access_token is not None

    def _auth_headers(self) -> dict[str, str]:
        if not self._access_token:
            raise UpstoxAPIError("No Upstox access token configured")
        return {"Authorization": f"Bearer {self._access_token}"}

    def _get_mock_response(self, path: str) -> dict[str, Any]:
        """
        Return mock data for development when Upstox API is unavailable.
        Production-grade mock data that matches Upstox API schema.
        """
        import random
        from datetime import datetime, timezone
        
        # Market quote LTP
        if "/market-quote/ltp" in path:
            return {
                "status": "success",
                "data": {
                    "NSE_EQ|INE002A01018": {
                        "instrument_token": "NSE_EQ|INE002A01018",
                        "last_price": round(2850.50 + random.uniform(-50, 50), 2),
                    }
                }
            }
        
        # Market quote OHLC
        if "/market-quote/ohlc" in path:
            base_price = 2850.50
            return {
                "status": "success",
                "data": {
                    "NSE_EQ|INE002A01018": {
                        "ohlc": {
                            "open": round(base_price - 20, 2),
                            "high": round(base_price + 30, 2),
                            "low": round(base_price - 25, 2),
                            "close": round(base_price, 2),
                        },
                        "last_price": round(base_price, 2),
                        "volume": random.randint(1000000, 5000000),
                    }
                }
            }
        
        # Historical candles
        if "/historical-candle" in path or "/intraday-candle" in path:
            candles = []
            base_price = 2850.0
            for i in range(100):
                open_price = base_price + random.uniform(-10, 10)
                close_price = open_price + random.uniform(-5, 5)
                high_price = max(open_price, close_price) + random.uniform(0, 3)
                low_price = min(open_price, close_price) - random.uniform(0, 3)
                
                candles.append([
                    datetime.now(timezone.utc).isoformat(),
                    round(open_price, 2),
                    round(high_price, 2),
                    round(low_price, 2),
                    round(close_price, 2),
                    random.randint(10000, 50000),
                    0
                ])
            
            return {
                "status": "success",
                "data": {
                    "candles": candles
                }
            }
        
        # Instruments list
        if "/instruments" in path:
            return {
                "status": "success",
                "data": [
                    {
                        "instrument_key": "NSE_EQ|INE002A01018",
                        "exchange_token": "2885",
                        "trading_symbol": "RELIANCE",
                        "name": "Reliance Industries Limited",
                        "last_price": 2850.50,
                        "expiry": "",
                        "strike": 0,
                        "tick_size": 0.05,
                        "lot_size": 1,
                        "instrument_type": "EQ",
                        "option_type": "",
                        "exchange": "NSE"
                    }
                ]
            }
        
        # Default fallback
        return {
            "status": "success",
            "data": {},
            "message": "Mock data - Upstox API unavailable"
        }

    # ── HTTP helpers ────────────────────────────────────────────────────────
    async def get(self, path: str, **kwargs: Any) -> Any:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Any:
        return await self._request("POST", path, **kwargs)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._client is None:
            raise UpstoxConnectionError("HTTP client not started")
        
        # Development mode: Return mock data if no token or token expired
        if not self._access_token and settings.ENVIRONMENT == "development":
            logger.warning(f"Upstox token not configured - returning mock data for {method} {path}")
            return self._get_mock_response(path)
        
        try:
            response = await self._client.request(
                method,
                path,
                headers={**self._auth_headers(), **kwargs.pop("headers", {})},
                **kwargs,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            # If 401 in development, return mock data
            if exc.response.status_code == 401 and settings.ENVIRONMENT == "development":
                logger.warning(f"Upstox 401 error - returning mock data for {method} {path}")
                return self._get_mock_response(path)
            
            logger.error(
                "Upstox API error: %s %s → %d",
                method,
                path,
                exc.response.status_code,
            )
            raise UpstoxAPIError(
                f"Upstream API returned {exc.response.status_code}",
                upstream_status=exc.response.status_code,
            )
        except httpx.TimeoutException:
            logger.error("Upstox API timeout: %s %s", method, path)
            raise UpstoxConnectionError("Request to market data API timed out")
        except httpx.ConnectError:
            logger.error("Upstox connection error: %s %s", method, path)
            raise UpstoxConnectionError("Cannot reach market data API")

    # ── OAuth exchange ──────────────────────────────────────────────────────
    async def exchange_code(self, code: str) -> dict[str, Any]:
        """Exchange an OAuth authorization code for tokens (server-side only)."""
        if self._client is None:
            raise UpstoxConnectionError("HTTP client not started")
        try:
            response = await self._client.post(
                "/login/auth/token",
                data={
                    "code": code,
                    "client_id": settings.UPSTOX_API_KEY,
                    "client_secret": settings.UPSTOX_API_SECRET,
                    "redirect_uri": str(settings.UPSTOX_REDIRECT_URI),
                    "grant_type": "authorization_code",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            raise UpstoxAPIError(
                "OAuth token exchange failed",
                upstream_status=exc.response.status_code,
            )


# ── FastAPI dependency ─────────────────────────────────────────────────────────
def get_upstox_client(request: FastAPIRequest) -> UpstoxClient:
    """Retrieve the singleton UpstoxClient from app.state."""
    client: UpstoxClient = request.app.state.upstox_client
    return client