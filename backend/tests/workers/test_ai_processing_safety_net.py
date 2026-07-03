"""
Tests for AIProcessingSafetyNet — the daily fallback dispatcher for the 3
demand-driven AI processing queues.

Covers:
  - Per-category threshold logic: a category dispatches only when its
    pending count is >= its configured threshold.
  - Per-category isolation: one category raising an exception does not
    prevent the other two from being checked and dispatched.
  - _run_batch() records success/error metrics and never raises itself.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.ai_processing_safety_net import AIProcessingSafetyNet
from app.workers.supervisor import PauseToken, TriggerToken


@pytest.fixture
def mock_settings():
    return MagicMock(
        AI_SAFETY_NET_SENTIMENT_THRESHOLD=100,
        AI_SAFETY_NET_EVENTS_THRESHOLD=50,
        AI_SAFETY_NET_FORECAST_THRESHOLD=20,
    )


@pytest.fixture
def safety_net():
    return AIProcessingSafetyNet(
        session_factory=MagicMock(),
        redis=AsyncMock(),
        shutdown=asyncio.Event(),
        pause=PauseToken(),
        trigger=TriggerToken(),
    )


# ── Per-category threshold logic ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_sentiment_dispatches_when_over_threshold(safety_net, mock_settings):
    with patch("app.ai.intelligence.nlp_engine.NLPEngine.pending_sentiment_count", AsyncMock(return_value=150)):
        with patch(
            "app.ai.intelligence.nlp_engine.NLPEngine.flush_pending_sentiment",
            AsyncMock(return_value={"dispatched": 150, "calls_made": 10}),
        ) as mock_flush:
            dispatched = await safety_net._check_sentiment(mock_settings)

    assert dispatched is True
    mock_flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_sentiment_skips_when_under_threshold(safety_net, mock_settings):
    with patch("app.ai.intelligence.nlp_engine.NLPEngine.pending_sentiment_count", AsyncMock(return_value=5)):
        with patch(
            "app.ai.intelligence.nlp_engine.NLPEngine.flush_pending_sentiment",
            AsyncMock(),
        ) as mock_flush:
            dispatched = await safety_net._check_sentiment(mock_settings)

    assert dispatched is False
    mock_flush.assert_not_called()


@pytest.mark.asyncio
async def test_check_forecast_dispatches_when_over_threshold(safety_net, mock_settings):
    with patch(
        "app.ai.fusion.forecast_batch_worker.pending_forecast_count", AsyncMock(return_value=25)
    ):
        with patch(
            "app.ai.fusion.forecast_batch_worker.flush_pending_forecasts",
            AsyncMock(return_value={"dispatched": 25, "calls_made": 5}),
        ) as mock_flush:
            dispatched = await safety_net._check_forecast(mock_settings)

    assert dispatched is True
    mock_flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_classification_dispatches_when_over_threshold(safety_net, mock_settings):
    with patch(
        "app.ai.intelligence.event_classifier.EventClassifier.pending_classification_count",
        AsyncMock(return_value=60),
    ):
        with patch(
            "app.ai.intelligence.event_classifier.EventClassifier.flush_pending_classifications",
            AsyncMock(return_value={"dispatched": 60, "calls_made": 60}),
        ) as mock_flush:
            dispatched = await safety_net._check_classification(mock_settings)

    assert dispatched is True
    mock_flush.assert_awaited_once()


# ── Per-category isolation ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_batch_isolates_category_failures(safety_net, mock_settings):
    """
    Sentiment raises; forecast and classification must still be checked and
    (if over threshold) dispatched. _run_batch() itself never raises.
    """
    with patch("app.workers.ai_processing_safety_net.get_settings", return_value=mock_settings):
        with patch.object(
            AIProcessingSafetyNet, "_check_sentiment", AsyncMock(side_effect=RuntimeError("quota exhausted"))
        ):
            with patch.object(AIProcessingSafetyNet, "_check_forecast", AsyncMock(return_value=True)) as mock_forecast:
                with patch.object(
                    AIProcessingSafetyNet, "_check_classification", AsyncMock(return_value=False)
                ) as mock_classification:
                    # Must not raise despite sentiment's exception.
                    await safety_net._run_batch(triggered=True)

    mock_forecast.assert_awaited_once()
    mock_classification.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_batch_records_error_metric_on_category_failure(safety_net, mock_settings):
    from app.core.metrics import ai_processing_safety_net_runs_total

    before = ai_processing_safety_net_runs_total.labels(status="error")._value.get()

    with patch("app.workers.ai_processing_safety_net.get_settings", return_value=mock_settings):
        with patch.object(AIProcessingSafetyNet, "_check_sentiment", AsyncMock(side_effect=RuntimeError("boom"))):
            with patch.object(AIProcessingSafetyNet, "_check_forecast", AsyncMock(return_value=False)):
                with patch.object(AIProcessingSafetyNet, "_check_classification", AsyncMock(return_value=False)):
                    await safety_net._run_batch(triggered=True)

    after = ai_processing_safety_net_runs_total.labels(status="error")._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_run_batch_records_success_metric_when_no_failures(safety_net, mock_settings):
    from app.core.metrics import ai_processing_safety_net_runs_total

    before = ai_processing_safety_net_runs_total.labels(status="success")._value.get()

    with patch("app.workers.ai_processing_safety_net.get_settings", return_value=mock_settings):
        with patch.object(AIProcessingSafetyNet, "_check_sentiment", AsyncMock(return_value=False)):
            with patch.object(AIProcessingSafetyNet, "_check_forecast", AsyncMock(return_value=False)):
                with patch.object(AIProcessingSafetyNet, "_check_classification", AsyncMock(return_value=False)):
                    await safety_net._run_batch(triggered=True)

    after = ai_processing_safety_net_runs_total.labels(status="success")._value.get()
    assert after == before + 1
