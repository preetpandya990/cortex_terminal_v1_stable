"""
Tests for app.core.worker_client
==================================
Validates the fail-open contract: WorkerClient always returns None on failure
and never raises, regardless of whether the circuit is open, the network
times out, or the server returns an error status.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from aiobreaker import CircuitBreakerError

from app.core.worker_client import WorkerClient


# ── Fixtures ───────────────────────────────────────────────────────────────────

_MOCK_SETTINGS = MagicMock(
    WORKER_BASE_URL="http://worker:8001",
    WORKER_HTTP_TIMEOUT=3.0,
    INTERNAL_API_SECRET="a" * 64,
)


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch):
    monkeypatch.setattr("app.core.worker_client.settings", _MOCK_SETTINGS)


@pytest.fixture
def worker_client():
    return WorkerClient()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _mock_response(status_code: int = 200, json_data: dict | None = None) -> httpx.Response:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = json_data or {"status": "ok"}
    if status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=response
        )
    else:
        response.raise_for_status.return_value = None
    return response


# ── Fail-open: returns None on all errors ─────────────────────────────────────

class TestFailOpen:
    @pytest.mark.asyncio
    async def test_returns_none_on_http_error(self, worker_client):
        with patch.object(
            worker_client._client,
            "request",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            result = await worker_client.get_health()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_circuit_open(self, worker_client):
        worker_client._breaker = MagicMock()
        worker_client._breaker.call_async = AsyncMock(
            side_effect=CircuitBreakerError()
        )
        result = await worker_client.get_health()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_4xx_response(self, worker_client):
        with patch.object(
            worker_client._client,
            "request",
            return_value=_mock_response(404),
        ):
            result = await worker_client.get_health()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_5xx_response(self, worker_client):
        with patch.object(
            worker_client._client,
            "request",
            return_value=_mock_response(503),
        ):
            result = await worker_client.get_health()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self, worker_client):
        with patch.object(
            worker_client._client,
            "request",
            side_effect=httpx.TimeoutException("timed out"),
        ):
            result = await worker_client.get_tasks()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_json_decode_error(self, worker_client):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.raise_for_status.return_value = None
        response.json.side_effect = ValueError("invalid json")
        with patch.object(worker_client._client, "request", return_value=response):
            result = await worker_client.get_health()
        assert result is None


# ── Successful path ────────────────────────────────────────────────────────────

class TestSuccess:
    @pytest.mark.asyncio
    async def test_returns_json_on_success(self, worker_client):
        expected = {"status": "ok"}
        with patch.object(
            worker_client._client,
            "request",
            return_value=_mock_response(200, expected),
        ):
            result = await worker_client.get_health()
        assert result == expected

    @pytest.mark.asyncio
    async def test_get_tasks_returns_dict(self, worker_client):
        payload = {"count": 13, "tasks": {"heartbeat": {"status": "running"}}}
        with patch.object(
            worker_client._client,
            "request",
            return_value=_mock_response(200, payload),
        ):
            result = await worker_client.get_tasks()
        assert result == payload
        assert result["count"] == 13

    @pytest.mark.asyncio
    async def test_pause_task_sends_post(self, worker_client):
        with patch.object(
            worker_client._client,
            "request",
            return_value=_mock_response(200, {"task": "heartbeat", "status": "paused"}),
        ) as mock_req:
            result = await worker_client.pause_task("heartbeat")
        assert result is not None
        call_args = mock_req.call_args
        assert call_args[0][0] == "POST"
        assert "/tasks/heartbeat/pause" in call_args[0][1]


# ── aclose ─────────────────────────────────────────────────────────────────────

class TestAclose:
    @pytest.mark.asyncio
    async def test_aclose_closes_client(self, worker_client):
        worker_client._client.aclose = AsyncMock()
        await worker_client.aclose()
        worker_client._client.aclose.assert_awaited_once()
