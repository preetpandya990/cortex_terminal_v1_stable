"""
Pure-logic unit tests for the Gemini news forecaster.

These cover deterministic, side-effect-free logic with literal inputs only —
the score mapping, prompt rendering, and circuit-breaker state machine.  Anything
that touches data or services (the gather_news_forecast orchestration, real
Gemini calls) is verified by the real-data integration suite, not here.
"""
from __future__ import annotations

from time import monotonic

import pytest

from app.ai.fusion.news_forecaster import (
    CircuitBreaker,
    build_forecast_prompt,
    score_from_direction,
)


# ── Score mapping ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("direction,conf,expected", [
    ("BUY", 0.8, 80.0),
    ("SELL", 0.5, -50.0),
    ("HOLD", 0.9, 0.0),
    ("buy", 1.0, 100.0),     # case-insensitive
    ("BUY", 2.0, 100.0),     # confidence clamped to 1.0
    ("BUY", -1.0, 0.0),      # clamped to 0.0
    ("SELL", 1.0, -100.0),
])
def test_score_from_direction(direction, conf, expected):
    assert score_from_direction(direction, conf) == expected


def test_score_from_direction_handles_bad_confidence():
    # Non-numeric confidence degrades to 0.0 rather than raising.
    assert score_from_direction("BUY", None) == 0.0  # type: ignore[arg-type]


# ── Prompt rendering ─────────────────────────────────────────────────────────

def test_build_prompt_renders_indicators_events_and_regime():
    prompt = build_forecast_prompt(
        "RELIANCE",
        {"rsi_14": 71.8, "ema_20": 1450.0, "macd_histogram": 4.3, "unknown_key": 1.0},
        [{"article_title": "Record profit", "impact": 8.0, "source_name": "Mint"}],
        regime="bullish",
    )
    assert "RELIANCE" in prompt
    assert "bullish" in prompt
    assert "RSI-14" in prompt and "71.8" in prompt
    assert "EMA-20" in prompt
    assert "MACD histogram" in prompt
    assert "Record profit" in prompt and "Mint" in prompt
    # Unmapped feature keys are not surfaced.
    assert "unknown_key" not in prompt


def test_build_prompt_states_news_absence_explicitly():
    prompt = build_forecast_prompt("TCS", {"rsi_14": 50.0}, [])
    assert "none in the lookback" in prompt.lower()


def test_build_prompt_handles_missing_indicators():
    prompt = build_forecast_prompt("INFY", {}, [{"type": "earnings"}])
    assert "(none available)" in prompt


# ── Circuit breaker ──────────────────────────────────────────────────────────

def test_circuit_breaker_opens_at_threshold_and_recovers():
    cb = CircuitBreaker(threshold=2, cooldown=100.0)
    assert cb.allow() is True
    cb.record_failure()
    assert cb.allow() is True          # one below threshold
    cb.record_failure()
    assert cb.is_open is True          # threshold reached → open
    assert cb.allow() is False
    cb.record_success()                # success closes it
    assert cb.is_open is False
    assert cb.allow() is True


def test_circuit_breaker_half_opens_after_cooldown():
    cb = CircuitBreaker(threshold=1, cooldown=0.0)  # cooldown 0 → immediately half-open
    cb.record_failure()
    # With zero cooldown the open window has already elapsed → next call allowed.
    assert cb.allow() is True


def test_circuit_breaker_success_resets_failure_count():
    cb = CircuitBreaker(threshold=3, cooldown=100.0)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open is False          # count was reset; only 2 since success
