"""
Explanation prompt-building + guardrail tests (WS7)
===================================================
Covers the deduped system prompts, the explicit scanner-unavailable line, the
price-action summary, the demand full-article context caps, the ungrounded-
number guardrail, and subclass survival through the guardrail pass.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.ai.intelligence.explanation_worker import (
    DemandExplanationOutput,
    ExplanationOutput,
    MLAssessmentOutput,
    NewsForecastOutput,
    _apply_guardrails,
    _build_context_prompt,
    _build_explanation_prompt,
    _CONTEXT_SYSTEM_PROMPT,
    _DEMAND_EXPLANATION_SYSTEM_PROMPT,
    _EXPLANATION_SYSTEM_PROMPT,
    _format_demand_context,
    _number_variants,
    _strip_ungrounded_numbers,
    _summarize_price_action,
)

pytestmark = pytest.mark.unit


def _suggestion(scanner_signal: dict | None = None) -> MagicMock:
    s = MagicMock()
    s.symbol = "RELIANCE"
    s.signal_direction = "BUY"
    s.consensus_score = Decimal("82.0")
    s.confidence_level = "HIGH"
    s.entry_price = None
    s.stop_loss = None
    s.risk_reward_ratio = None
    s.time_horizon = "intraday"
    s.regime_type = "high_volatility"
    s.trigger_pathway = "scanner_anomaly"
    s.ml_signal = {"available": False}
    s.ai_signal = {}
    s.scanner_signal = scanner_signal or {}
    return s


def _candle(close: float, high: float | None = None, low: float | None = None) -> dict:
    return {
        "ts": datetime(2026, 7, 6, tzinfo=timezone.utc),
        "open": close, "high": high or close * 1.01,
        "low": low or close * 0.99, "close": close, "volume": 1000,
    }


class TestPromptComposition:
    def test_both_prompts_carry_the_five_headers(self):
        for prompt in (_EXPLANATION_SYSTEM_PROMPT, _CONTEXT_SYSTEM_PROMPT):
            for header in (
                "### What the models saw", "### Technical picture",
                "### News context", "### What this suggests", "### Key risks",
            ):
                assert header in prompt

    def test_both_prompts_carry_the_shared_rules(self):
        for prompt in (_EXPLANATION_SYSTEM_PROMPT, _CONTEXT_SYSTEM_PROMPT):
            assert "Mandatory rules" in prompt
            assert "PROHIBITED language" in prompt
            assert "DISCLAIMER" in prompt

    def test_demand_prompt_extends_explanation_prompt(self):
        assert _DEMAND_EXPLANATION_SYSTEM_PROMPT.startswith(_EXPLANATION_SYSTEM_PROMPT)
        assert "news_forecast" in _DEMAND_EXPLANATION_SYSTEM_PROMPT


class TestScannerSection:
    def test_empty_scanner_states_unavailability_explicitly(self):
        prompt = _build_explanation_prompt(_suggestion(scanner_signal={}), context="")
        assert "## Technical Scanner Readings" in prompt
        assert "Technical scanner data unavailable" in prompt

    def test_populated_scanner_renders_readings(self):
        prompt = _build_explanation_prompt(
            _suggestion(scanner_signal={"rsi": 71.3, "volume_ratio": 2.8}),
            context="",
        )
        assert "RSI-14: 71.3" in prompt
        assert "Technical scanner data unavailable" not in prompt


class TestPriceActionSection:
    def test_summary_math(self):
        candles = [_candle(c) for c in (100, 102, 101, 104, 106, 110)]
        lines = _summarize_price_action(candles)
        text = "\n".join(lines)
        assert "6 daily bars" in text
        assert "+10.0% over the window" in text          # 100 → 110
        assert "Realized daily volatility" in text
        # Last 5 bars: 102 → 110 = +7.8%; ups: 101→104→106→110 = 3 of 4
        assert "(3/4 up days)" in text

    def test_insufficient_history_is_honest(self):
        assert _summarize_price_action([_candle(100)]) == [
            "Insufficient OHLCV history for a price-action summary."
        ]

    def test_price_action_rendered_into_prompt(self):
        prompt = _build_explanation_prompt(
            _suggestion(), context="", price_action=["Window: 60 daily bars"]
        )
        assert "## Recent Price Action (daily bars)" in prompt
        assert "Window: 60 daily bars" in prompt

    def test_absent_price_action_renders_nothing(self):
        prompt = _build_explanation_prompt(_suggestion(), context="")
        assert "Recent Price Action" not in prompt


class TestDemandContextCaps:
    def test_articles_and_chars_capped(self):
        from app.ai.rag.retriever import RetrievedChunk

        chunks = [
            RetrievedChunk(
                content=f"article {i} " + "x" * 5000,
                source_name=f"Feed{i}",
                as_of_timestamp=datetime(2026, 7, 6, tzinfo=timezone.utc),
                source_url="https://example.com",
                rrf_score=1.0 - i * 0.1,
            )
            for i in range(5)
        ]
        block = _format_demand_context(chunks, max_articles=3, max_chars=100)
        assert "Feed0" in block and "Feed2" in block
        assert "Feed3" not in block  # article cap
        # Each body truncated to 100 chars — the 5000-x tail never appears.
        assert "x" * 101 not in block


class TestUngroundedNumberGuardrail:
    PROMPT = "Calibrated Confidence:  62%\nRSI-14: 71.3\nPrice Change %: 3.5"

    def test_grounded_numbers_survive(self):
        text = "Confidence sits at 62% and RSI 71.3 supports it."
        cleaned, removed = _strip_ungrounded_numbers(text, self.PROMPT)
        assert removed == 0
        assert cleaned == text

    def test_hallucinated_percentage_stripped(self):
        text = "Confidence sits at 62%. The model is 97% certain of a breakout."
        cleaned, removed = _strip_ungrounded_numbers(text, self.PROMPT)
        assert removed == 1
        assert "97%" not in cleaned
        assert "62%" in cleaned

    def test_hallucinated_rsi_stripped(self):
        text = "RSI 71.3 is elevated. RSI 28 signals oversold conditions."
        cleaned, removed = _strip_ungrounded_numbers(text, self.PROMPT)
        assert removed == 1
        assert "RSI 28" not in cleaned

    def test_headers_never_stripped(self):
        text = "### Technical picture\nThe model is 97% certain."
        cleaned, removed = _strip_ungrounded_numbers(text, self.PROMPT)
        assert removed == 1
        assert "### Technical picture" in cleaned

    def test_integer_float_variants_match(self):
        # Prompt says "62%", output says "62.0%" — same figure, grounded.
        cleaned, removed = _strip_ungrounded_numbers("Confidence is 62.0%.", self.PROMPT)
        assert removed == 0
        assert "62" in _number_variants("62.0")


class TestGuardrailsPreserveSubclass:
    def test_demand_output_keeps_news_forecast_and_assessment(self):
        raw = DemandExplanationOutput(
            summary="s",
            full_explanation="### What the models saw\nfine.",
            sources_used=[],
            news_forecast=NewsForecastOutput(
                direction="BUY", sentiment_label="bullish",
                confidence=0.8, rationale="strong results",
            ),
            ml_assessment=MLAssessmentOutput(
                likely_missed_pattern="none",
                confidence_should_have_been_lower=False,
                price_action_agreement="agrees",
            ),
        )
        final, _events = _apply_guardrails(raw, has_context=False)
        assert isinstance(final, DemandExplanationOutput)
        assert final.news_forecast.direction == "BUY"
        assert final.ml_assessment.price_action_agreement == "agrees"
        assert "informational purposes only" in final.full_explanation

    def test_assessment_enum_coercion_never_raises(self):
        """Out-of-vocabulary enum values coerce to safe defaults — a flaky
        enum must never fail the whole generation."""
        assessment = MLAssessmentOutput(
            likely_missed_pattern="something_novel",
            confidence_should_have_been_lower=True,
            price_action_agreement="strongly_disagrees",
        )
        assert assessment.likely_missed_pattern == "other"
        assert assessment.price_action_agreement == "partial"

    def test_legacy_output_unchanged_shape(self):
        raw = ExplanationOutput(
            summary="s", full_explanation="### What the models saw\nfine.",
        )
        final, _events = _apply_guardrails(raw, has_context=False)
        assert isinstance(final, ExplanationOutput)


class TestContextPromptSnapshotAge:
    def test_as_of_rendered_when_present(self):
        prompt = _build_context_prompt(
            "NSE_EQ|X", "X",
            {"available": True, "direction": "BUY", "confidence": 0.7,
             "prediction_generated_at": "2026-07-06T10:00:00+00:00"},
            context="",
        )
        assert "(Snapshot as of 2026-07-06T10:00:00+00:00)" in prompt

    def test_absent_timestamp_renders_nothing(self):
        prompt = _build_context_prompt(
            "NSE_EQ|X", "X",
            {"available": True, "direction": "BUY", "confidence": 0.7},
            context="",
        )
        assert "Snapshot as of" not in prompt
