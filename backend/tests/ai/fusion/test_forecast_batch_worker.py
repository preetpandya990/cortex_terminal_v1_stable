"""
Tests for the demand-driven dispatch additions to forecast_batch_worker.

Covers:
  - flush_pending_forecasts() drains the Kafka topic to its end-offset
    snapshot and reports correct dispatched/calls_made counts, committing
    per successful group.
  - flush_pending_forecasts() stops early and raises RuntimeError the moment
    a batch outcome is budget_throttled/error, WITHOUT committing that group
    and after rewinding the shared consumer to the committed offset.
  - pending_forecast_count() reflects consumer-group lag.
  - forecast_batch_loop() never consumes when FORECAST_AUTO_DISPATCH is
    False, but still feeds the pending-count gauge from lag.

The shared module-level ForecastQueueConsumer is replaced with a scripted
fake — broker-backed behaviour is covered by tests/integration/kafka/.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.fusion.forecast_batch_worker import (
    ForecastQueueConsumer,
    flush_pending_forecasts,
    forecast_batch_loop,
    pending_forecast_count,
)


def _payload(symbol: str) -> dict:
    return {"symbol": symbol, "cache_key": f"cortex:news_forecast:{symbol}:x"}


class _FakeQueueConsumer:
    """Scripted stand-in for the module-level ForecastQueueConsumer."""

    def __init__(self, batches: list[list[dict]]) -> None:
        self.lock = asyncio.Lock()
        self._batches = list(batches)
        self._end = sum(len(b) for b in batches)
        self._pos = 0
        self.commit_count = 0
        self.rewind_count = 0
        self.drain_count = 0
        self.stopped = False

        tp = ForecastQueueConsumer._TP
        self._consumer = MagicMock()
        self._consumer.end_offsets = AsyncMock(return_value={tp: self._end})

        async def _position(_tp):
            return self._pos

        self._consumer.position = _position

    async def ensure_started(self):
        return self._consumer

    async def drain(self, max_items: int, timeout_ms: int) -> list[dict]:
        self.drain_count += 1
        if not self._batches:
            return []
        batch = self._batches.pop(0)
        self._pos += len(batch)
        return batch

    async def commit(self) -> None:
        self.commit_count += 1

    async def rewind_to_committed(self) -> None:
        self.rewind_count += 1
        self._pos = 0

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def mock_settings():
    return MagicMock(
        NEWS_FORECAST_BATCH_SIZE=5,
        NEWS_FORECAST_BATCH_WINDOW_SECS=0.05,
        FORECAST_AUTO_DISPATCH=True,
    )


@pytest.fixture
def mock_session_factory():
    return MagicMock()


# ── pending_forecast_count ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_forecast_count_uses_consumer_lag():
    with patch(
        "app.ai.fusion.forecast_batch_worker.pending_count",
        new=AsyncMock(return_value=7),
    ) as mock_lag:
        assert await pending_forecast_count() == 7
    mock_lag.assert_awaited_once()


# ── flush_pending_forecasts ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_flush_pending_forecasts_drains_fully(mock_settings, mock_session_factory):
    fake = _FakeQueueConsumer([[_payload("TCS"), _payload("INFY")]])
    redis = AsyncMock()

    with patch("app.ai.fusion.forecast_batch_worker.get_settings", return_value=mock_settings):
        with patch("app.ai.fusion.forecast_batch_worker._queue_consumer", fake):
            with patch(
                "app.ai.fusion.forecast_batch_worker._flush_batch",
                new=AsyncMock(return_value={"outcome": "success", "valid_count": 2, "total": 2}),
            ) as mock_flush:
                result = await flush_pending_forecasts(redis, mock_session_factory)

    assert result == {"dispatched": 2, "calls_made": 1}
    mock_flush.assert_awaited_once()
    assert fake.commit_count == 1
    assert fake.rewind_count == 0


@pytest.mark.asyncio
async def test_flush_pending_forecasts_stops_early_on_budget_throttled(
    mock_settings, mock_session_factory
):
    # Two groups available; the first comes back budget_throttled.
    fake = _FakeQueueConsumer([[_payload("TCS")], [_payload("INFY")]])
    redis = AsyncMock()

    with patch("app.ai.fusion.forecast_batch_worker.get_settings", return_value=mock_settings):
        with patch("app.ai.fusion.forecast_batch_worker._queue_consumer", fake):
            with patch(
                "app.ai.fusion.forecast_batch_worker._flush_batch",
                new=AsyncMock(
                    return_value={"outcome": "budget_throttled", "valid_count": 0, "total": 1}
                ),
            ) as mock_flush:
                with pytest.raises(RuntimeError, match="budget_throttled"):
                    await flush_pending_forecasts(redis, mock_session_factory)

    # Only the first group was processed; nothing committed, consumer rewound
    # so the remainder stays pending for the next dispatch.
    mock_flush.assert_awaited_once()
    assert fake.commit_count == 0
    assert fake.rewind_count == 1


@pytest.mark.asyncio
async def test_flush_pending_forecasts_empty_queue(mock_settings, mock_session_factory):
    fake = _FakeQueueConsumer([])
    redis = AsyncMock()

    with patch("app.ai.fusion.forecast_batch_worker.get_settings", return_value=mock_settings):
        with patch("app.ai.fusion.forecast_batch_worker._queue_consumer", fake):
            result = await flush_pending_forecasts(redis, mock_session_factory)

    assert result == {"dispatched": 0, "calls_made": 0}


# ── forecast_batch_loop gating ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_loop_never_consumes_when_auto_dispatch_disabled():
    settings = MagicMock(
        NEWS_FORECAST_BATCH_SIZE=5,
        NEWS_FORECAST_BATCH_WINDOW_SECS=0.05,
        FORECAST_AUTO_DISPATCH=False,
    )
    fake = _FakeQueueConsumer([[_payload("TCS")]])
    shutdown = asyncio.Event()

    with patch("app.ai.fusion.forecast_batch_worker.get_settings", return_value=settings):
        with patch("app.ai.fusion.forecast_batch_worker._queue_consumer", fake):
            with patch(
                "app.ai.fusion.forecast_batch_worker.pending_count",
                new=AsyncMock(return_value=3),
            ) as mock_lag:
                task = asyncio.ensure_future(
                    forecast_batch_loop(AsyncMock(), MagicMock(), shutdown)
                )
                await asyncio.sleep(0.1)
                shutdown.set()
                await asyncio.wait_for(task, timeout=2.0)

    assert fake.drain_count == 0
    mock_lag.assert_awaited()


@pytest.mark.asyncio
async def test_loop_consumes_and_commits_when_auto_dispatch_enabled(mock_settings):
    fake = _FakeQueueConsumer([[_payload("TCS")]])
    shutdown = asyncio.Event()

    with patch("app.ai.fusion.forecast_batch_worker.get_settings", return_value=mock_settings):
        with patch("app.ai.fusion.forecast_batch_worker._queue_consumer", fake):
            with patch(
                "app.ai.fusion.forecast_batch_worker.pending_count",
                new=AsyncMock(return_value=1),
            ):
                with patch(
                    "app.ai.fusion.forecast_batch_worker._flush_batch",
                    new=AsyncMock(
                        return_value={"outcome": "success", "valid_count": 1, "total": 1}
                    ),
                ) as mock_flush:
                    task = asyncio.ensure_future(
                        forecast_batch_loop(AsyncMock(), MagicMock(), shutdown)
                    )
                    await asyncio.sleep(0.3)
                    shutdown.set()
                    await asyncio.wait_for(task, timeout=2.0)

    assert fake.drain_count >= 1
    mock_flush.assert_awaited_once()
    # Commit happens only AFTER the flush returned (the crash-safety contract).
    assert fake.commit_count >= 1
