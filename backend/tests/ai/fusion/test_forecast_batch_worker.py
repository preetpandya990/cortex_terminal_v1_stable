"""
Tests for the demand-driven dispatch additions to forecast_batch_worker.

Covers:
  - flush_pending_forecasts() drains the Redis queue fully and reports
    correct dispatched/calls_made counts.
  - flush_pending_forecasts() stops early and raises RuntimeError the moment
    a batch outcome is budget_throttled/error, leaving remaining queue items
    untouched (never LPOPed).
  - pending_forecast_count() reflects LLEN.
  - forecast_batch_loop() never calls LPOP when FORECAST_AUTO_DISPATCH is
    False, but still polls LLEN for the gauge.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.fusion.forecast_batch_worker import (
    flush_pending_forecasts,
    forecast_batch_loop,
    pending_forecast_count,
)


def _payload(symbol: str) -> str:
    return json.dumps({"symbol": symbol, "cache_key": f"cortex:news_forecast:{symbol}:x"})


@pytest.fixture
def mock_settings():
    return MagicMock(
        NEWS_FORECAST_BATCH_SIZE=5,
        NEWS_FORECAST_BATCH_WINDOW_SECS=60.0,
        FORECAST_AUTO_DISPATCH=True,
    )


@pytest.fixture
def mock_session_factory():
    return MagicMock()


# ── pending_forecast_count ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_forecast_count_calls_llen():
    redis = AsyncMock()
    redis.llen.return_value = 7
    assert await pending_forecast_count(redis) == 7
    redis.llen.assert_awaited_once()


# ── flush_pending_forecasts ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flush_pending_forecasts_drains_fully(mock_settings, mock_session_factory):
    redis = AsyncMock()
    # First LPOP returns a batch of 2, second returns empty (queue drained).
    redis.lpop.side_effect = [
        [_payload("TCS").encode(), _payload("INFY").encode()],
        [],
    ]

    with patch("app.ai.fusion.forecast_batch_worker.get_settings", return_value=mock_settings):
        with patch(
            "app.ai.fusion.forecast_batch_worker._flush_batch",
            new=AsyncMock(return_value={"outcome": "success", "valid_count": 2, "total": 2}),
        ) as mock_flush:
            result = await flush_pending_forecasts(redis, mock_session_factory)

    assert result == {"dispatched": 2, "calls_made": 1}
    mock_flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_flush_pending_forecasts_stops_early_on_budget_throttled(
    mock_settings, mock_session_factory
):
    redis = AsyncMock()
    # Two batches available; the first comes back budget_throttled.
    redis.lpop.side_effect = [
        [_payload("TCS").encode()],
        [_payload("INFY").encode()],
    ]

    with patch("app.ai.fusion.forecast_batch_worker.get_settings", return_value=mock_settings):
        with patch(
            "app.ai.fusion.forecast_batch_worker._flush_batch",
            new=AsyncMock(
                return_value={"outcome": "budget_throttled", "valid_count": 0, "total": 1}
            ),
        ) as mock_flush:
            with pytest.raises(RuntimeError, match="budget_throttled"):
                await flush_pending_forecasts(redis, mock_session_factory)

    # Only the first popped batch was ever processed — the second LPOP never happens.
    mock_flush.assert_awaited_once()
    assert redis.lpop.await_count == 1


@pytest.mark.asyncio
async def test_flush_pending_forecasts_empty_queue(mock_settings, mock_session_factory):
    redis = AsyncMock()
    redis.lpop.return_value = []

    with patch("app.ai.fusion.forecast_batch_worker.get_settings", return_value=mock_settings):
        result = await flush_pending_forecasts(redis, mock_session_factory)

    assert result == {"dispatched": 0, "calls_made": 0}


# ── forecast_batch_loop gating ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_skips_lpop_when_auto_dispatch_disabled():
    settings = MagicMock(
        NEWS_FORECAST_BATCH_SIZE=5,
        NEWS_FORECAST_BATCH_WINDOW_SECS=60.0,
        FORECAST_AUTO_DISPATCH=False,
    )
    redis = AsyncMock()
    redis.llen.return_value = 3
    shutdown = asyncio.Event()

    with patch("app.ai.fusion.forecast_batch_worker.get_settings", return_value=settings):
        task = asyncio.ensure_future(
            forecast_batch_loop(redis, MagicMock(), shutdown)
        )
        await asyncio.sleep(0.1)
        shutdown.set()
        await asyncio.wait_for(task, timeout=2.0)

    redis.lpop.assert_not_called()
    redis.llen.assert_awaited()


@pytest.mark.asyncio
async def test_loop_calls_lpop_when_auto_dispatch_enabled():
    settings = MagicMock(
        NEWS_FORECAST_BATCH_SIZE=5,
        NEWS_FORECAST_BATCH_WINDOW_SECS=60.0,
        FORECAST_AUTO_DISPATCH=True,
    )
    redis = AsyncMock()
    redis.lpop.return_value = []
    redis.llen.return_value = 0
    shutdown = asyncio.Event()

    with patch("app.ai.fusion.forecast_batch_worker.get_settings", return_value=settings):
        task = asyncio.ensure_future(
            forecast_batch_loop(redis, MagicMock(), shutdown)
        )
        await asyncio.sleep(0.1)
        shutdown.set()
        await asyncio.wait_for(task, timeout=2.0)

    redis.lpop.assert_awaited()
