from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.workers.watchlist_context_scheduler import WatchlistContextScheduler

pytestmark = pytest.mark.unit


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        return self._result


class _SessionFactory:
    def __init__(self, results):
        self._results = list(results)

    def __call__(self):
        return _Session(self._results.pop(0))


class _FakeShutdown:
    """Stand-in for asyncio.Event — synchronous is_set(), settable from tests."""

    def __init__(self, set_after_n_checks: int | None = None) -> None:
        self._set = False
        self._checks = 0
        self._set_after_n_checks = set_after_n_checks

    def is_set(self) -> bool:
        self._checks += 1
        if self._set_after_n_checks is not None and self._checks > self._set_after_n_checks:
            self._set = True
        return self._set


class _FakePause:
    """Stand-in for PauseToken — checkpoint() is a no-op that never blocks."""

    async def checkpoint(self) -> None:
        return None


def _settings(**overrides):
    base = dict(
        EXPLANATION_ON_DEMAND=False,
        WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES=30,
        WATCHLIST_SCHEDULER_BATCH_CAP=200,
        WATCHLIST_SCHEDULER_CHUNK_SIZE=20,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_scheduler(session_factory, *, shutdown=None, pause=None, predictor=object()):
    return WatchlistContextScheduler(
        session_factory=session_factory,
        redis=object(),
        predictor=predictor,
        shutdown=shutdown or _FakeShutdown(),
        pause=pause or _FakePause(),
        trigger=object(),
    )


@pytest.mark.asyncio
async def test_scheduler_enqueues_prediction_snapshot(monkeypatch):
    scheduler = _make_scheduler(
        _SessionFactory(
            [
                _RowsResult([("NSE_EQ|INE002A01018",)]),
                _RowsResult([]),
            ]
        )
    )

    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.get_settings",
        lambda: _settings(),
    )

    snapshot = {"available": True, "direction": "BUY", "confidence": 0.93}
    get_prediction_snapshots_batch = AsyncMock(
        return_value={"NSE_EQ|INE002A01018": snapshot}
    )
    kafka_publish = AsyncMock()

    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.get_prediction_snapshots_batch",
        get_prediction_snapshots_batch,
    )
    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.kafka_publish",
        kafka_publish,
    )

    await scheduler._run_batch(triggered=True)

    get_prediction_snapshots_batch.assert_awaited_once()
    kafka_publish.assert_awaited_once()
    published = kafka_publish.await_args.args[1]
    assert json.loads(published["prediction_data"]) == snapshot


@pytest.mark.asyncio
async def test_scheduler_chunks_large_batches(monkeypatch):
    """45 stale instruments with chunk size 20 -> 3 batch calls (20/20/5),
    never one call per instrument."""
    instrument_keys = [f"NSE_EQ|SYM{i:03d}" for i in range(45)]

    scheduler = _make_scheduler(
        _SessionFactory(
            [
                _RowsResult([(k,) for k in instrument_keys]),
                _RowsResult([]),
            ]
        )
    )

    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.get_settings",
        lambda: _settings(WATCHLIST_SCHEDULER_CHUNK_SIZE=20),
    )

    async def fake_batch(*, instrument_keys, **_kwargs):
        return {
            k: {"available": True, "direction": "HOLD"} for k in instrument_keys
        }

    get_prediction_snapshots_batch = AsyncMock(side_effect=fake_batch)
    kafka_publish = AsyncMock()

    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.get_prediction_snapshots_batch",
        get_prediction_snapshots_batch,
    )
    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.kafka_publish",
        kafka_publish,
    )

    await scheduler._run_batch(triggered=True)

    assert get_prediction_snapshots_batch.await_count == 3
    chunk_sizes = [
        len(call.kwargs["instrument_keys"])
        for call in get_prediction_snapshots_batch.await_args_list
    ]
    assert chunk_sizes == [20, 20, 5]
    assert kafka_publish.await_count == 45


@pytest.mark.asyncio
async def test_scheduler_stops_mid_batch_on_shutdown(monkeypatch):
    """Shutdown signalled after the first chunk -> batch stops, no further
    chunks processed."""
    instrument_keys = [f"NSE_EQ|SYM{i:03d}" for i in range(45)]

    # is_set() is called once per chunk-loop iteration (before each chunk).
    # Allow the first check (chunk 1) to pass, then report shutdown.
    shutdown = _FakeShutdown(set_after_n_checks=1)

    scheduler = _make_scheduler(
        _SessionFactory(
            [
                _RowsResult([(k,) for k in instrument_keys]),
                _RowsResult([]),
            ]
        ),
        shutdown=shutdown,
    )

    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.get_settings",
        lambda: _settings(WATCHLIST_SCHEDULER_CHUNK_SIZE=20),
    )

    async def fake_batch(*, instrument_keys, **_kwargs):
        return {
            k: {"available": True, "direction": "HOLD"} for k in instrument_keys
        }

    get_prediction_snapshots_batch = AsyncMock(side_effect=fake_batch)
    kafka_publish = AsyncMock()

    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.get_prediction_snapshots_batch",
        get_prediction_snapshots_batch,
    )
    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.kafka_publish",
        kafka_publish,
    )

    await scheduler._run_batch(triggered=True)

    # Only the first chunk (20 instruments) should have been processed.
    assert get_prediction_snapshots_batch.await_count == 1
    assert kafka_publish.await_count == 20


@pytest.mark.asyncio
async def test_scheduler_always_publishes_even_on_unavailable_snapshot(monkeypatch):
    """A degraded (unavailable) snapshot for one instrument must still be
    published, not silently dropped."""
    scheduler = _make_scheduler(
        _SessionFactory(
            [
                _RowsResult([("NSE_EQ|GOOD", ), ("NSE_EQ|BAD",)]),
                _RowsResult([]),
            ]
        )
    )

    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.get_settings",
        lambda: _settings(),
    )

    async def fake_batch(*, instrument_keys, **_kwargs):
        return {
            "NSE_EQ|GOOD": {"available": True, "direction": "BUY"},
            "NSE_EQ|BAD": {"available": False, "unavailable_reason": "insufficient_data"},
        }

    get_prediction_snapshots_batch = AsyncMock(side_effect=fake_batch)
    kafka_publish = AsyncMock()
    unavailable_counter = MagicMock()

    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.get_prediction_snapshots_batch",
        get_prediction_snapshots_batch,
    )
    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.kafka_publish",
        kafka_publish,
    )
    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.watchlist_scheduler_snapshot_unavailable_total",
        unavailable_counter,
    )

    await scheduler._run_batch(triggered=True)

    assert kafka_publish.await_count == 2
    published_payloads = {
        call.args[1]["instrument_key"]: json.loads(call.args[1]["prediction_data"])
        for call in kafka_publish.await_args_list
    }
    assert published_payloads["NSE_EQ|GOOD"]["available"] is True
    assert published_payloads["NSE_EQ|BAD"]["available"] is False
    unavailable_counter.labels.assert_called_once_with(reason="insufficient_data")
    unavailable_counter.labels.return_value.inc.assert_called_once()


@pytest.mark.asyncio
async def test_scheduler_isolates_publish_failure_per_instrument(monkeypatch):
    """Kafka publish failing for one instrument must not prevent other
    instruments in the same chunk from publishing."""
    scheduler = _make_scheduler(
        _SessionFactory(
            [
                _RowsResult([("NSE_EQ|A",), ("NSE_EQ|B",)]),
                _RowsResult([]),
            ]
        )
    )

    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.get_settings",
        lambda: _settings(),
    )

    async def fake_batch(*, instrument_keys, **_kwargs):
        return {
            k: {"available": True, "direction": "HOLD"} for k in instrument_keys
        }

    async def flaky_publish(_topic, payload, key):
        if payload["instrument_key"] == "NSE_EQ|A":
            raise RuntimeError("broker unreachable")

    get_prediction_snapshots_batch = AsyncMock(side_effect=fake_batch)
    kafka_publish = AsyncMock(side_effect=flaky_publish)
    publish_failed_counter = MagicMock()

    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.get_prediction_snapshots_batch",
        get_prediction_snapshots_batch,
    )
    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.kafka_publish",
        kafka_publish,
    )
    monkeypatch.setattr(
        "app.workers.watchlist_context_scheduler.watchlist_scheduler_publish_failed_total",
        publish_failed_counter,
    )

    await scheduler._run_batch(triggered=True)

    assert kafka_publish.await_count == 2
    published_keys = {call.args[1]["instrument_key"] for call in kafka_publish.await_args_list}
    assert published_keys == {"NSE_EQ|A", "NSE_EQ|B"}
    publish_failed_counter.inc.assert_called_once()
