"""
Sentiment aggregate tests
=========================
WS4: the aggregator reuses stored full-body AINLPResult scores and calls the
batched headline LLM only for events lacking one. Also pins the row-shaping
helpers and the L1 TTL behavior.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services.sentiment_analysis_service import (
    SentimentAnalysisService,
    _rows_to_scored_events,
    _stored_sentiment,
)

pytestmark = pytest.mark.unit


def _event(event_id: int, title: str = "Some headline", hours_old: float = 1.0):
    return SimpleNamespace(
        id=event_id,
        event_timestamp=datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc),
        source_name="Moneycontrol",
        raw_content=f"{title}\n\nbody",
        extra_data={"title": title},
    )


class TestStoredSentimentShaping:
    def test_valid_row_shapes_to_batch_contract(self):
        stored = _stored_sentiment(Decimal("0.75"), "positive", Decimal("0.9"), "gemini-2.5")
        assert stored == {
            "label": "positive", "score": 0.75, "confidence": 0.9, "model": "gemini-2.5",
        }

    def test_null_score_means_unscored(self):
        assert _stored_sentiment(None, "positive", Decimal("0.9"), "m") is None

    def test_invalid_label_means_unscored(self):
        assert _stored_sentiment(Decimal("0.5"), "bullish", Decimal("0.9"), "m") is None

    def test_null_confidence_defaults(self):
        stored = _stored_sentiment(Decimal("0.5"), "positive", None, "m")
        assert stored["confidence"] == 0.5

    def test_duplicate_event_rows_keep_first(self):
        e = _event(1)
        rows = [
            (e, Decimal("0.5"), "positive", Decimal("0.8"), "m"),
            (e, Decimal("-0.5"), "negative", Decimal("0.8"), "m"),
        ]
        scored = _rows_to_scored_events(rows)
        assert len(scored) == 1
        assert scored[0].stored["label"] == "positive"


class TestComputeReusesStoredScores:
    def _service(self) -> SentimentAnalysisService:
        SentimentAnalysisService._l1_cache.clear()
        return SentimentAnalysisService(db=AsyncMock(), redis=None)

    @pytest.mark.asyncio
    async def test_batch_called_only_for_unscored_events(self):
        from app.services.sentiment_analysis_service import _ScoredEvent

        service = self._service()
        scored_events = [
            _ScoredEvent(_event(1, "Stored-score article"), {
                "label": "positive", "score": 0.8, "confidence": 0.9, "model": "stored",
            }),
            _ScoredEvent(_event(2, "Unscored fallback article"), None),
            _ScoredEvent(_event(3, "Another stored one"), {
                "label": "negative", "score": -0.6, "confidence": 0.8, "model": "stored",
            }),
        ]
        service._fetch_events = AsyncMock(return_value=(scored_events, 24))
        batch_mock = AsyncMock(return_value=[
            {"label": "neutral", "score": 0.0, "confidence": 0.5, "model": "llm"},
        ])
        service._nlp.analyze_sentiment_batch = batch_mock

        with patch(
            "app.services.sentiment_analysis_service.load_credibility_scores",
            new=AsyncMock(return_value={}),
        ):
            response = await service._compute("NSE_EQ|X", "X", 24)

        # Exactly one title — the unscored event's — went to the LLM.
        batch_mock.assert_awaited_once_with(["Unscored fallback article"])
        assert response.breakdown.total == 3
        assert response.breakdown.positive == 1
        assert response.breakdown.negative == 1
        assert response.breakdown.neutral == 1

    @pytest.mark.asyncio
    async def test_all_stored_makes_zero_llm_calls(self):
        from app.services.sentiment_analysis_service import _ScoredEvent

        service = self._service()
        scored_events = [
            _ScoredEvent(_event(1), {
                "label": "positive", "score": 0.9, "confidence": 0.9, "model": "stored",
            }),
            _ScoredEvent(_event(2), {
                "label": "positive", "score": 0.7, "confidence": 0.8, "model": "stored",
            }),
        ]
        service._fetch_events = AsyncMock(return_value=(scored_events, 24))
        batch_mock = AsyncMock()
        service._nlp.analyze_sentiment_batch = batch_mock

        with patch(
            "app.services.sentiment_analysis_service.load_credibility_scores",
            new=AsyncMock(return_value={}),
        ):
            response = await service._compute("NSE_EQ|X", "X", 24)

        batch_mock.assert_not_awaited()
        assert response.sentiment_label == "bullish"


class TestL1Ttl:
    def test_entry_expires_after_ttl(self):
        SentimentAnalysisService._l1_cache.clear()
        service = SentimentAnalysisService(db=AsyncMock(), redis=None)
        service._l1_set("k", {"a": 1})
        assert service._l1_get("k") == {"a": 1}

        # Force-expire by rewriting the entry with a past deadline.
        payload, _ = SentimentAnalysisService._l1_cache["k"]
        SentimentAnalysisService._l1_cache["k"] = (payload, 0.0)
        assert service._l1_get("k") is None
        assert "k" not in SentimentAnalysisService._l1_cache  # evicted on read

    def test_missing_key(self):
        SentimentAnalysisService._l1_cache.clear()
        service = SentimentAnalysisService(db=AsyncMock(), redis=None)
        assert service._l1_get("absent") is None
