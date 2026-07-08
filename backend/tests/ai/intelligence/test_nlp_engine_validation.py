"""
NLP engine validation tests
===========================
WS4: label↔score sign consistency is enforced at Pydantic parse time (before
any cache write), and batched results carry reasoning that is spot-checked
against the article to catch index misattribution.
"""
from __future__ import annotations

import pytest

from app.ai.intelligence.nlp_engine import (
    SentimentOutput,
    _expected_label_for_score,
    _label_contradicts_score,
    _reasoning_matches_content,
    _SentimentBatchItem,
)

pytestmark = pytest.mark.unit


class TestSignContract:
    @pytest.mark.parametrize(
        ("label", "score", "contradicts"),
        [
            ("positive", 0.8, False),
            ("positive", 0.05, False),   # small positive is fine (prompt: (0, 1])
            ("positive", 0.0, True),     # positive requires score > 0
            ("positive", -0.4, True),    # sign flip
            ("negative", -0.6, False),
            ("negative", -0.05, False),
            ("negative", 0.3, True),     # sign flip
            ("neutral", 0.0, False),
            ("neutral", 0.09, False),    # within ±0.1 tolerance
            ("neutral", 0.4, True),      # neutral with strong score
        ],
    )
    def test_contradiction_matrix(self, label, score, contradicts):
        assert _label_contradicts_score(label, score) is contradicts

    @pytest.mark.parametrize(
        ("score", "expected"),
        [(0.5, "positive"), (0.11, "positive"), (0.1, "neutral"),
         (-0.1, "neutral"), (-0.11, "negative"), (-0.9, "negative")],
    )
    def test_expected_label(self, score, expected):
        assert _expected_label_for_score(score) == expected


class TestSingleModelValidator:
    def test_sign_flip_coerced_and_confidence_capped(self):
        out = SentimentOutput(
            label="negative", score=0.4, confidence=0.9, reasoning="strong beat"
        )
        assert out.label == "positive"
        assert out.confidence == 0.5

    def test_consistent_response_untouched(self):
        out = SentimentOutput(
            label="positive", score=0.4, confidence=0.9, reasoning="strong beat"
        )
        assert out.label == "positive"
        assert out.confidence == 0.9

    def test_neutral_with_strong_score_coerced_to_score_sign(self):
        out = SentimentOutput(
            label="neutral", score=-0.7, confidence=0.8, reasoning="big miss"
        )
        assert out.label == "negative"
        assert out.confidence == 0.5

    def test_low_confidence_not_raised_by_cap(self):
        out = SentimentOutput(
            label="negative", score=0.4, confidence=0.2, reasoning="x"
        )
        assert out.confidence == 0.2  # min(), never a floor


class TestBatchModelValidator:
    def test_sign_flip_coerced(self):
        item = _SentimentBatchItem(
            index=0, label="positive", score=-0.5, confidence=0.95
        )
        assert item.label == "negative"
        assert item.confidence == 0.5

    def test_consistent_item_untouched(self):
        item = _SentimentBatchItem(
            index=0, label="neutral", score=0.05, confidence=0.7, reasoning="factual"
        )
        assert item.label == "neutral"
        assert item.confidence == 0.7

    def test_reasoning_defaults_empty(self):
        item = _SentimentBatchItem(index=0, label="neutral", score=0.0, confidence=0.5)
        assert item.reasoning == ""


class TestMisattributionCheck:
    def test_overlapping_token_passes(self):
        assert _reasoning_matches_content(
            "Infosys wins large European banking contract",
            "The article cites a major banking contract win.",
        ) is True

    def test_zero_overlap_fails(self):
        assert _reasoning_matches_content(
            "Infosys wins large European banking contract",
            "Steel output declined amid weak demand.",
        ) is False

    def test_short_article_is_unjudgeable(self):
        # No token ≥ 4 chars in the article — no verdict possible.
        assert _reasoning_matches_content("TCS up 2%", "anything") is None

    def test_case_insensitive(self):
        assert _reasoning_matches_content(
            "RELIANCE announces buyback", "the reliance buyback plan"
        ) is True

    def test_empty_reasoning_fails_when_judgeable(self):
        assert _reasoning_matches_content(
            "Infosys wins large banking contract", ""
        ) is False
