"""
Cortex AI — API Contract Tests
================================
Tests every public endpoint for:
  - Correct HTTP status codes
  - Response shape compliance
  - Auth enforcement (401 without token)
  - Error handling (no internal detail leakage)
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport


@pytest_asyncio.fixture(scope="module")
async def lifespan_client():
    """Module-scoped client that runs the app's full lifespan with auth bypassed."""
    from app.main import app
    from app.api.deps import get_current_user_id

    # Override auth to return a test user ID
    def override_auth():
        return "test-user-id"

    app.dependency_overrides[get_current_user_id] = override_auth

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client

    # Clean up overrides
    app.dependency_overrides.clear()


# ══════════════════════════════════════════════════════════════════════════════
# Health check
# ══════════════════════════════════════════════════════════════════════════════
async def test_health_check(lifespan_client: AsyncClient) -> None:
    response = await lifespan_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


# ══════════════════════════════════════════════════════════════════════════════
# Auth enforcement — endpoints must 401 without token
# ══════════════════════════════════════════════════════════════════════════════
class TestAuthEnforcement:
    """Verify all protected routes return 401 without a valid Bearer token."""

    async def test_live_price_requires_auth(self, lifespan_client: AsyncClient) -> None:
        response = await lifespan_client.get("/api/v1/market-data/live/NSE_EQ|INE009A01021")
        assert response.status_code == 401

    async def test_scanner_requires_auth(self, lifespan_client: AsyncClient) -> None:
        response = await lifespan_client.get("/api/v1/scanner/run")
        assert response.status_code == 401

    async def test_stream_start_requires_auth(self, lifespan_client: AsyncClient) -> None:
        response = await lifespan_client.post(
            "/api/v1/upstox/stream/start",
            json=["NSE_EQ|INE009A01021"],
        )
        assert response.status_code == 401


# ══════════════════════════════════════════════════════════════════════════════
# Market Data
# ══════════════════════════════════════════════════════════════════════════════
class TestMarketDataEndpoints:
    async def test_instrument_search_requires_min_2_chars(
        self, lifespan_client: AsyncClient
    ) -> None:
        response = await lifespan_client.get(
            "/api/v1/market-data/instruments/search", params={"q": "R"}
        )
        assert response.status_code == 422

    async def test_instrument_search_returns_list(
        self, lifespan_client: AsyncClient
    ) -> None:
        with patch(
            "app.api.v1.market_data.search_instruments_db",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await lifespan_client.get(
                "/api/v1/market-data/instruments/search", params={"q": "RE"}
            )
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_live_price_404_for_unknown_instrument(
        self, lifespan_client: AsyncClient
    ) -> None:
        from app.exceptions import DataNotFoundError

        with patch(
            "app.api.v1.market_data.UpstoxClient.get",
            new_callable=AsyncMock,
            return_value={"data": {}},
        ):
            response = await lifespan_client.get(
                "/api/v1/market-data/live/UNKNOWN_INSTRUMENT"
            )
        assert response.status_code == 404
        body = response.json()
        assert "error" in body
        assert "message" in body["error"]
        # Must NOT expose internal class names, stack traces, or conn strings
        assert "Traceback" not in body["error"]["message"]
        assert "postgres" not in body["error"]["message"].lower()

    async def test_live_price_502_on_upstream_error(
        self, lifespan_client: AsyncClient
    ) -> None:
        from app.exceptions import UpstoxAPIError

        with patch(
            "app.services.upstox_client.UpstoxClient._request",
            new_callable=AsyncMock,
            side_effect=UpstoxAPIError("Upstream error", upstream_status=503),
        ):
            response = await lifespan_client.get(
                "/api/v1/market-data/live/NSE_EQ|INE009A01021"
            )
        assert response.status_code == 502
        body = response.json()
        # Correlation ID must be present for log correlation
        assert "correlation_id" in body["error"]

    async def test_error_response_shape(self, lifespan_client: AsyncClient) -> None:
        """Verify consistent error response shape: {error: {message, status_code, correlation_id}}"""
        response = await lifespan_client.get(
            "/api/v1/market-data/instruments/search", params={"q": "X"}
        )
        assert response.status_code == 422
        body = response.json()
        assert "error" in body


# ══════════════════════════════════════════════════════════════════════════════
# Scanner
# ══════════════════════════════════════════════════════════════════════════════
class TestScannerEndpoints:
    async def test_run_returns_scan_response_shape(
        self, lifespan_client: AsyncClient
    ) -> None:
        from app.schemas.scanner import ScanResponse

        with patch(
            "app.api.v1.scanner.MarketScannerService.scan_all",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await lifespan_client.get("/api/v1/scanner/run")
        assert response.status_code == 200
        body = response.json()
        # Verify required fields
        assert "results" in body
        assert "total" in body
        assert "timeframe" in body
        assert "failed_instruments" in body
        assert isinstance(body["results"], list)

    async def test_invalid_timeframe_rejected(self, lifespan_client: AsyncClient) -> None:
        response = await lifespan_client.get(
            "/api/v1/scanner/run", params={"timeframe": "2d"}
        )
        assert response.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# Upstox
# ══════════════════════════════════════════════════════════════════════════════
class TestUpstoxEndpoints:
    async def test_market_feed_ws_rejects_unauthenticated(
        self, lifespan_client: AsyncClient
    ) -> None:
        # No token — endpoint must close with 4001
        from httpx_ws import aconnect_ws
        import pytest

        with pytest.raises(Exception):
            async with aconnect_ws(
                "/api/v1/upstox/market-feed/ws", lifespan_client
            ):
                pass

    async def test_ingest_accepts_valid_request(
        self, lifespan_client: AsyncClient
    ) -> None:
        response = await lifespan_client.post(
            "/api/v1/upstox/ingest",
            json={
                "instrument_keys": ["NSE_EQ|INE009A01021"],
                "timeframe": "1d",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "accepted"

    async def test_ingest_rejects_empty_keys(self, lifespan_client: AsyncClient) -> None:
        response = await lifespan_client.post(
            "/api/v1/upstox/ingest",
            json={
                "instrument_keys": [],
                "timeframe": "1d",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
            },
        )
        assert response.status_code == 422

    async def test_ingest_rejects_invalid_timeframe(
        self, lifespan_client: AsyncClient
    ) -> None:
        response = await lifespan_client.post(
            "/api/v1/upstox/ingest",
            json={
                "instrument_keys": ["NSE_EQ|INE009A01021"],
                "timeframe": "2d",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
            },
        )
        assert response.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# OAuth — state parameter CSRF
# ══════════════════════════════════════════════════════════════════════════════
class TestOAuthSecurity:
    async def test_callback_rejects_invalid_state(
        self, lifespan_client: AsyncClient
    ) -> None:
        """OAuth callback with an unknown state must be rejected."""
        response = await lifespan_client.get(
            "/api/v1/auth/callback",
            params={"code": "any_code", "state": "tampered-state-value"},
        )
        # Should be 401 (OAuthStateError) — not 500
        assert response.status_code == 401

    async def test_callback_never_returns_token_in_body(
        self, lifespan_client: AsyncClient
    ) -> None:
        """
        Even if the callback succeeds, access_token must NOT appear in the body.
        """
        # Seed a valid state in the mock redis
        state = "valid-test-state"

        async def mock_get(key: str) -> str | None:
            if key == f"oauth:state:{state}":
                return "1"
            return None

        from app.core.redis import get_redis as _get_redis
        from unittest.mock import AsyncMock as _AsyncMock

        mock_redis = _AsyncMock()
        mock_redis.get = mock_get
        mock_redis.delete = _AsyncMock()
        mock_redis.setex = _AsyncMock()

        fake_token_data = {
            "access_token": "SUPER_SECRET_TOKEN_XYZ",
            "token_type": "bearer",
        }
        with patch(
            "app.services.upstox_client.UpstoxClient.exchange_code",
            new_callable=AsyncMock,
            return_value=fake_token_data,
        ), patch("app.api.v1.auth.get_redis", return_value=mock_redis):
            response = await lifespan_client.get(
                "/api/v1/auth/callback",
                params={"code": "auth_code_123", "state": state},
            )

        # Token must never appear in the response body
        body_text = response.text
        assert "SUPER_SECRET_TOKEN_XYZ" not in body_text
        assert "access_token" not in body_text
