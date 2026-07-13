from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.services import prediction_snapshot as snapshot_service

pytestmark = pytest.mark.unit


class _FakeSession:
    """Minimal async-context-manager stand-in for an AsyncSession."""

    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


def _session_factory_recording(sessions: list[_FakeSession]):
    """Build a session_factory that records every session it hands out."""

    def factory() -> _FakeSession:
        s = _FakeSession()
        sessions.append(s)
        return s

    return factory


def _dummy_session_factory():
    return _FakeSession()


@pytest.mark.asyncio
async def test_get_prediction_snapshot_returns_no_model_without_predictor():
    snapshot = await snapshot_service.get_prediction_snapshot(
        instrument_key="NSE_EQ|INE002A01018",
        timeframe="1d",
        predictor=None,
        session_factory=_dummy_session_factory,
        redis=object(),
    )

    assert snapshot["available"] is False
    assert snapshot["unavailable_reason"] == "no_model"
    assert snapshot["prediction_generated_at"]
    assert snapshot["updated_at"]


@pytest.mark.asyncio
async def test_get_prediction_snapshot_returns_insufficient_data(monkeypatch):
    feature_loader = AsyncMock()
    feature_loader.load_features.side_effect = ValueError("missing candles")

    monkeypatch.setattr(
        snapshot_service,
        "FeatureLoader",
        lambda **_: feature_loader,
    )

    predictor = type(
        "Predictor",
        (),
        {
            "sequence_length": 60,
            "n_features": 37,
            "feature_names": ("close",),
        },
    )()

    snapshot = await snapshot_service.get_prediction_snapshot(
        instrument_key="NSE_EQ|INE002A01018",
        timeframe="1d",
        predictor=predictor,
        session_factory=_dummy_session_factory,
        redis=object(),
    )

    assert snapshot["available"] is False
    assert snapshot["unavailable_reason"] == "insufficient_data"


@pytest.mark.asyncio
async def test_get_prediction_snapshot_serializes_prediction(monkeypatch):
    feature_loader = AsyncMock()
    feature_loader.load_features.return_value = ("tabular", "sequence", 2450.0, 0.021)

    monkeypatch.setattr(
        snapshot_service,
        "FeatureLoader",
        lambda **_: feature_loader,
    )

    predictor = type(
        "Predictor",
        (),
        {
            "sequence_length": 60,
            "n_features": 37,
            "feature_names": ("close",),
            "predict": AsyncMock(
                return_value={
                    "direction_label": "BUY",
                    "confidence": 0.9123,
                    "conviction_scale": 0.81,
                    "threshold": 0.6,
                    "probabilities": {"buy": 0.91, "sell": 0.05, "hold": 0.04},
                    "entry_price": 2450.12,
                    "stop_loss": 2401.55,
                    "tp1": 2500.0,
                    "tp2": 2550.0,
                    "tp3": 2600.0,
                    "volatility": 0.0212,
                    "models": {},
                    "metadata": {
                        "timeframe": "1d",
                        "predicted_at": "2026-07-10T09:15:00+00:00",
                        "xgboost_version": "xgb_v2",
                    },
                }
            ),
        },
    )()

    snapshot = await snapshot_service.get_prediction_snapshot(
        instrument_key="NSE_EQ|INE002A01018",
        timeframe="1d",
        predictor=predictor,
        session_factory=_dummy_session_factory,
        redis=object(),
    )

    assert snapshot["available"] is True
    assert snapshot["direction"] == "BUY"
    assert snapshot["confidence"] == pytest.approx(0.9123)
    assert snapshot["predicted_at"] == "2026-07-10T09:15:00+00:00"
    assert snapshot["prediction_generated_at"]
    assert snapshot["updated_at"]


@pytest.mark.asyncio
async def test_get_prediction_snapshot_coalesces_inflight_calls(monkeypatch):
    calls = 0

    async def fake_compute(**_: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"available": True, "direction": "BUY"}

    monkeypatch.setattr(snapshot_service, "_compute_prediction_snapshot", fake_compute)

    first, second = await asyncio.gather(
        snapshot_service.get_prediction_snapshot(
            instrument_key="NSE_EQ|INE002A01018",
            timeframe="1d",
            predictor=object(),
            session_factory=_dummy_session_factory,
            redis=object(),
        ),
        snapshot_service.get_prediction_snapshot(
            instrument_key="NSE_EQ|INE002A01018",
            timeframe="1d",
            predictor=object(),
            session_factory=_dummy_session_factory,
            redis=object(),
        ),
    )

    assert calls == 1
    assert first == second == {"available": True, "direction": "BUY"}


@pytest.mark.asyncio
async def test_caller_timeout_does_not_cancel_shared_task_for_other_awaiters(monkeypatch):
    """
    Regression test for the cancellation-cascade bug: one caller's own
    wait_for/shield timeout must never cancel the shared computation for a
    concurrent caller on the same instrument/timeframe key.
    """
    calls = 0

    async def fake_compute(**_: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"available": True, "direction": "SELL"}

    monkeypatch.setattr(snapshot_service, "_compute_prediction_snapshot", fake_compute)

    async def short_timeout_caller():
        with pytest.raises(asyncio.TimeoutError):
            await snapshot_service.get_prediction_snapshot(
                instrument_key="NSE_EQ|INE002A01018",
                timeframe="1d",
                predictor=object(),
                session_factory=_dummy_session_factory,
                redis=object(),
                timeout=0.01,  # fires well before fake_compute's 0.05s sleep
            )

    async def long_timeout_caller():
        return await snapshot_service.get_prediction_snapshot(
            instrument_key="NSE_EQ|INE002A01018",
            timeframe="1d",
            predictor=object(),
            session_factory=_dummy_session_factory,
            redis=object(),
            timeout=1.0,
        )

    short_task = asyncio.create_task(short_timeout_caller())
    long_task = asyncio.create_task(long_timeout_caller())

    await short_task
    result = await long_task

    assert calls == 1
    assert result == {"available": True, "direction": "SELL"}


@pytest.mark.asyncio
async def test_shared_task_uses_its_own_session_independent_of_callers(monkeypatch):
    """
    Regression test: the coalesced computation must open its own DB session
    via session_factory, never reuse or depend on any caller-supplied
    session object — a caller's own request-scoped session may be closed
    (e.g. on that caller's cancellation) while other awaiters still need
    the shared computation to keep running.
    """
    feature_loader = AsyncMock()
    feature_loader.load_features.return_value = ("tabular", "sequence", 100.0, 0.01)

    seen_sessions: list[_FakeSession] = []

    def fake_feature_loader(**kwargs):
        seen_sessions.append(kwargs["db"])
        return feature_loader

    monkeypatch.setattr(snapshot_service, "FeatureLoader", fake_feature_loader)

    predictor = type(
        "Predictor",
        (),
        {
            "sequence_length": 60,
            "n_features": 37,
            "feature_names": ("close",),
            "predict": AsyncMock(
                return_value={
                    "direction_label": "HOLD",
                    "confidence": 0.5,
                    "probabilities": {},
                    "models": {},
                    "metadata": {},
                }
            ),
        },
    )()

    factory_sessions: list[_FakeSession] = []
    session_factory = _session_factory_recording(factory_sessions)

    result = await snapshot_service.get_prediction_snapshot(
        instrument_key="NSE_EQ|INE002A01018",
        timeframe="1d",
        predictor=predictor,
        session_factory=session_factory,
        redis=object(),
    )

    assert result["available"] is True
    # Exactly one session was opened via the factory, and it's the same
    # object FeatureLoader received — proving the task owns its own session
    # lifecycle rather than depending on a caller-supplied one.
    assert len(factory_sessions) == 1
    assert seen_sessions == factory_sessions
    assert factory_sessions[0].closed is True


@pytest.mark.asyncio
async def test_internal_compute_timeout_returns_unavailable_and_cleans_up(monkeypatch):
    monkeypatch.setattr(snapshot_service, "_TASK_COMPUTE_TIMEOUT_SECS", 0.01)

    async def hanging_inner(**_: object) -> dict[str, object]:
        await asyncio.sleep(1.0)
        return {"available": True}

    monkeypatch.setattr(snapshot_service, "_compute_prediction_snapshot_inner", hanging_inner)

    snapshot = await snapshot_service.get_prediction_snapshot(
        instrument_key="NSE_EQ|INE002A01018",
        timeframe="1d",
        predictor=object(),
        session_factory=_dummy_session_factory,
        redis=object(),
    )

    assert snapshot["available"] is False
    assert snapshot["unavailable_reason"] == "timeout"
    # The in-flight registry entry must be cleaned up once the task
    # actually finishes, so a later call starts fresh rather than hanging
    # off a stale/removed key forever.
    assert ("NSE_EQ|INE002A01018", "1d") not in snapshot_service._inflight_tasks
