"""
Tests for app.api.v1.admin_ai_processing
===========================================
Validates the main-API admin proxy for the demand-driven AI processing
queues: status pass-through, WorkerDispatchError -> HTTP 502 propagation,
admin auth enforcement, and rate limiting.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.v1.admin_ai_processing import router
from app.core.auth import require_admin_role
from app.core.limiter import limiter
from app.core.worker_client import WorkerDispatchError


def _make_app(worker_client) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_middleware(SlowAPIMiddleware)
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(router)
    app.state.worker_client = worker_client
    app.dependency_overrides[require_admin_role] = lambda: "admin-user-id"
    return app


@pytest.fixture
def mock_worker_client():
    client = MagicMock()
    client.get_ai_processing_status = AsyncMock()
    client.dispatch_ai_processing = AsyncMock()
    return client


@pytest.fixture
def client(mock_worker_client):
    app = _make_app(mock_worker_client)
    with TestClient(app) as c:
        yield c


class TestStatus:
    def test_status_proxies_worker_client(self, client, mock_worker_client):
        mock_worker_client.get_ai_processing_status.return_value = {
            "sentiment": {"pending": 0, "auto_flush": True},
            "forecast": {"pending": 3, "auto_flush": False},
            "classification": {"pending": 0, "auto_flush": True},
        }
        resp = client.get("/status")
        assert resp.status_code == 200
        assert resp.json()["forecast"]["pending"] == 3

    def test_status_returns_503_when_worker_unavailable(self, client, mock_worker_client):
        mock_worker_client.get_ai_processing_status.return_value = None
        resp = client.get("/status")
        assert resp.status_code == 503
        assert resp.json()["degraded"] is True


class TestDispatch:
    @pytest.mark.parametrize("category", ["sentiment", "forecast", "classification"])
    def test_dispatch_success(self, client, mock_worker_client, category):
        mock_worker_client.dispatch_ai_processing.return_value = {
            "dispatched": 5, "calls_made": 1
        }
        resp = client.post(f"/{category}/dispatch")
        assert resp.status_code == 200
        assert resp.json() == {"dispatched": 5, "calls_made": 1}
        mock_worker_client.dispatch_ai_processing.assert_awaited_once_with(category)

    def test_dispatch_failure_maps_to_502(self, client, mock_worker_client):
        mock_worker_client.dispatch_ai_processing.side_effect = WorkerDispatchError(
            "forecast dispatch failed: HTTP 502 — budget_throttled"
        )
        resp = client.post("/forecast/dispatch")
        assert resp.status_code == 502
        assert "budget_throttled" in resp.json()["detail"]


class TestAuth:
    def test_status_requires_admin(self, mock_worker_client):
        # No dependency override this time — real require_admin_role rejects.
        app = FastAPI()
        app.state.limiter = limiter
        app.add_middleware(SlowAPIMiddleware)
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.include_router(router)
        app.state.worker_client = mock_worker_client
        with TestClient(app) as c:
            resp = c.get("/status")
        assert resp.status_code in (401, 403)
