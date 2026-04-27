"""
Cortex AI — Upstox HTTP Client
================================
Single shared httpx.AsyncClient with:
  - Persistent TCP connection pool (no fresh TLS handshake per call)
  - Proper timeout configuration
  - Typed errors surfaced as UpstoxAPIError
  - Token stored in-process (never returned to end clients)
  - Circuit breaker protection against cascading failures
"""
from __future__ import annotations

import logging
from typing import Any
from fastapi import Request as FastAPIRequest

import httpx

from app.core.config import get_settings
from app.core.circuit_breaker import upstox_breaker
from app.exceptions import UpstoxAPIError, UpstoxConnectionError, UpstoxInvalidInstrumentError, UpstoxRateLimitError

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
        if "market-quote/ltp" in path:
            base = round(2850.50 + random.uniform(-50, 50), 2)
            return {
                "status": "success",
                "data": {
                    "NSE_EQ:INE002A01018": {
                        "instrument_token": "NSE_EQ|INE002A01018",
                        "last_price": base,
                        "cp": round(base * random.uniform(0.95, 1.05), 2),  # previous close
                        "volume": random.randint(100_000, 5_000_000),
                        "ltq": random.randint(1, 100),
                    }
                }
            }
        
        # Market quote OHLC
        if "market-quote/ohlc" in path:
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
        
        # Historical and intraday candles (intraday path also starts with historical-candle/)
        if "historical-candle" in path:
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
                },
                "message": "Mock data - Upstox API unavailable"
            }
        
        # Instruments list
        if "instruments" in path:
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

    @upstox_breaker
    async def _make_request(
        self,
        method: str,
        encoded_path: str,
        headers: dict,
        **kwargs: Any
    ) -> Any:
        """
        Make HTTP request guarded by the global upstox_breaker circuit breaker.

        Used for real-time endpoints (market-quote, feed/authorize, etc.) where
        a pattern of failures is a genuine signal that the Upstox API is degraded.
        Opens after 5 consecutive qualifying failures; recovers after 60 seconds.

        Historical-candle ingestion requests use _make_request_ingestion() instead
        so that bulk backfill errors never affect the real-time scanner circuit.
        """
        return await self._execute_request(method, encoded_path, headers, **kwargs)

    async def _make_request_ingestion(
        self,
        method: str,
        encoded_path: str,
        headers: dict,
        **kwargs: Any,
    ) -> Any:
        """
        Make HTTP request WITHOUT the global circuit breaker.

        Used exclusively for historical-candle ingestion calls.  The ingestion
        worker has its own local CircuitBreaker — ingestion failures must never
        open the global circuit and block the real-time scanner.
        """
        return await self._execute_request(method, encoded_path, headers, **kwargs)

    async def _execute_request(
        self,
        method: str,
        encoded_path: str,
        headers: dict,
        **kwargs: Any,
    ) -> Any:
        """Shared HTTP execution logic used by both request variants."""
        try:
            response = await self._client.request(
                method,
                encoded_path,
                headers=headers,
                **kwargs,
            )
            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code

            # 401 in development — return mock data instead of failing
            if status == 401 and settings.ENVIRONMENT == "development":
                logger.warning("Upstox 401 - returning mock data for %s %s", method, encoded_path)
                return self._get_mock_response(encoded_path)

            body = exc.response.text[:300] if hasattr(exc.response, "text") else ""
            logger.error(
                "Upstox API error: %s %s → %d | Response: %s",
                method, encoded_path, status, body,
            )

            # 429 = rate limited — excluded from both circuit breakers
            if status == 429:
                raise UpstoxRateLimitError(upstream_status=429)

            # 400 = invalid instrument key — excluded from both circuit breakers
            if status == 400:
                raise UpstoxInvalidInstrumentError(
                    "Invalid instrument key rejected by Upstox (UDAPI100011)",
                    upstream_status=400,
                )

            raise UpstoxAPIError(
                f"Upstream API returned {status}",
                upstream_status=status,
            )

        except httpx.TimeoutException:
            logger.error("Upstox API timeout: %s %s", method, encoded_path)
            raise UpstoxConnectionError("Request to market data API timed out")

        except httpx.ConnectError:
            logger.error("Upstox connection error: %s %s", method, encoded_path)
            raise UpstoxConnectionError("Cannot reach market data API")
    
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """
        Public request method with circuit breaker error handling.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            path: API endpoint path
            **kwargs: Additional request parameters
        
        Returns:
            JSON response from API
        
        Raises:
            UpstoxConnectionError: Circuit breaker open or connection error
            UpstoxAPIError: HTTP error from API
        """
        if self._client is None:
            raise UpstoxConnectionError("HTTP client not started")
        
        # Development mode: Return mock data if no token or token expired
        if not self._access_token and settings.ENVIRONMENT == "development":
            logger.warning(f"Upstox token not configured - returning mock data for {method} {path}")
            return self._get_mock_response(path)
        
        # Build a relative URL so httpx correctly resolves it under base_url (/v3/).
        # Leading slash would cause httpx to treat it as absolute (dropping /v3/).
        from urllib.parse import quote
        encoded_path = '/'.join(
            quote(segment, safe='') for segment in path.split('/') if segment
        )
        
        # Prepare headers
        headers = {**self._auth_headers(), **kwargs.pop("headers", {})}
        
        # Import circuit breaker dependencies
        from aiobreaker import CircuitBreakerError
        from app.core.circuit_breaker import handle_circuit_breaker_error
        
        # Historical-candle ingestion bypasses the global circuit breaker.
        # All other endpoints (market-quote, feed, etc.) use it.
        is_ingestion = "historical-candle" in path

        try:
            if is_ingestion:
                return await self._make_request_ingestion(method, encoded_path, headers, **kwargs)
            else:
                return await self._make_request(method, encoded_path, headers, **kwargs)

        except CircuitBreakerError as e:
            # Only reachable for non-ingestion paths (global circuit open)
            handle_circuit_breaker_error("upstox", e)

            if settings.ENVIRONMENT == "development":
                logger.warning("Circuit breaker open - returning mock data for %s %s", method, path)
                return self._get_mock_response(path)
            
            # In production, raise error
            raise UpstoxConnectionError(
                "Upstox API circuit breaker is open - service temporarily unavailable"
            )

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

    # ── WebSocket v3 Authorization ─────────────────────────────────────────────
    async def get_websocket_auth_url(self) -> str:
        """
        Fetch one-time-use WebSocket authorization URL from Upstox API v3.
        
        This URL is required for WebSocket v3 connections and contains a
        single-use authentication code. Must be called before each connection.
        
        Returns:
            Authorized WebSocket URL (wss://...)
        
        Raises:
            UpstoxAPIError: If authorization fails (401, 403, etc.)
            UpstoxConnectionError: If network/timeout error occurs
        
        Reference:
            https://upstox.com/developer/api-documentation/get-market-data-feed-authorize-v3
        """
        if not self._access_token:
            raise UpstoxAPIError("Cannot authorize WebSocket: No access token configured")
        
        try:
            response = await self._request(
                "GET",
                "feed/market-data-feed/authorize",
                headers={"Api-Version": "3.0"}
            )
            
            # Extract authorized URL from response
            if response.get("status") == "success":
                data = response.get("data", {})
                auth_url = data.get("authorized_redirect_uri")
                
                if not auth_url:
                    raise UpstoxAPIError("Missing authorized_redirect_uri in response")
                
                logger.info("✓ Obtained fresh WebSocket authorization URL")
                return auth_url
            else:
                raise UpstoxAPIError(f"Authorization failed: {response.get('message', 'Unknown error')}")
        
        except UpstoxAPIError:
            raise
        except Exception as exc:
            logger.error("Failed to get WebSocket authorization URL: %s", exc)
            raise UpstoxConnectionError(f"WebSocket authorization failed: {exc}")


# ── FastAPI dependency ─────────────────────────────────────────────────────────
def get_upstox_client(request: FastAPIRequest) -> UpstoxClient:
    """Retrieve the singleton UpstoxClient from app.state."""
    client: UpstoxClient = request.app.state.upstox_client
    return client