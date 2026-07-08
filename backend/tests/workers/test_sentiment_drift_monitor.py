"""
Sentiment drift monitor tests
=============================
WS4 (E.B): pure-function coverage for the window statistics and the total
variation distance the daily drift check publishes.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.workers.sentiment_drift_monitor import (
    WindowStats,
    build_window_stats,
    total_variation_distance,
)

pytestmark = pytest.mark.unit


def _row(label: str, n: int, mean_score: float):
    return SimpleNamespace(label=label, n=n, mean_score=mean_score)


class TestBuildWindowStats:
    def test_fractions_and_weighted_mean(self):
        stats = build_window_stats([
            _row("positive", 60, 0.5),
            _row("negative", 20, -0.4),
            _row("neutral", 20, 0.0),
        ])
        assert stats.total == 100
        assert stats.fractions == {"positive": 0.6, "negative": 0.2, "neutral": 0.2}
        # (60*0.5 + 20*-0.4 + 20*0.0) / 100 = 0.22
        assert stats.mean_score == pytest.approx(0.22)

    def test_missing_labels_get_zero_fraction(self):
        stats = build_window_stats([_row("positive", 10, 0.3)])
        assert stats.fractions["negative"] == 0.0
        assert stats.fractions["neutral"] == 0.0
        assert stats.mean_score == pytest.approx(0.3)

    def test_empty_window(self):
        stats = build_window_stats([])
        assert stats == WindowStats(
            total=0,
            fractions={"positive": 0.0, "negative": 0.0, "neutral": 0.0},
            mean_score=0.0,
        )

    def test_unknown_labels_ignored(self):
        stats = build_window_stats([_row("bullish", 50, 0.9), _row("positive", 10, 0.2)])
        assert stats.total == 10
        assert stats.fractions["positive"] == 1.0


class TestTotalVariationDistance:
    def test_identical_distributions(self):
        d = {"positive": 0.5, "negative": 0.3, "neutral": 0.2}
        assert total_variation_distance(d, d) == 0.0

    def test_disjoint_distributions(self):
        a = {"positive": 1.0, "negative": 0.0, "neutral": 0.0}
        b = {"positive": 0.0, "negative": 1.0, "neutral": 0.0}
        assert total_variation_distance(a, b) == 1.0

    def test_partial_shift(self):
        a = {"positive": 0.6, "negative": 0.2, "neutral": 0.2}
        b = {"positive": 0.4, "negative": 0.4, "neutral": 0.2}
        # 0.5 * (0.2 + 0.2 + 0.0) = 0.2
        assert total_variation_distance(a, b) == pytest.approx(0.2)

    def test_symmetric(self):
        a = {"positive": 0.7, "negative": 0.1, "neutral": 0.2}
        b = {"positive": 0.3, "negative": 0.5, "neutral": 0.2}
        assert total_variation_distance(a, b) == total_variation_distance(b, a)
