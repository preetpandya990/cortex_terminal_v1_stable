"""Pure-logic unit tests for the forecaster evaluation metrics (literal inputs)."""
from __future__ import annotations

import math

import pytest

from app.ai.evaluation.forecast_metrics import (
    evaluate_directions,
    p_up_from_forecast,
    realized_up,
)


def test_realized_up():
    assert realized_up(0.03) == 1
    assert realized_up(-0.01) == 0
    assert realized_up(0.0) == 0


@pytest.mark.parametrize("direction,conf,expected", [
    ("BUY", 0.8, 0.8),
    ("SELL", 0.7, pytest.approx(0.3)),
    ("buy", 1.0, 1.0),
    ("HOLD", 0.9, None),
    ("BUY", 2.0, 1.0),     # clamped
])
def test_p_up_from_forecast(direction, conf, expected):
    assert p_up_from_forecast(direction, conf) == expected


def test_all_hold_yields_no_directional_metrics():
    m = evaluate_directions([None, None, None], [1, 0, 1])
    assert m.n_total == 3 and m.n_directional == 0
    assert m.coverage == 0.0
    assert m.accuracy is None and m.brier is None and m.ece is None  # honest None


def test_perfect_predictions():
    # Confident and always correct.
    p_ups = [0.9, 0.1, 0.8, 0.2]      # up, down, up, down
    realized = [1, 0, 1, 0]
    m = evaluate_directions(p_ups, realized)
    assert m.n_directional == 4 and m.coverage == 1.0
    assert m.accuracy == 1.0
    assert m.precision_up == 1.0 and m.recall_up == 1.0
    # Brier is small but not 0 (confidence < 1).
    assert 0.0 < m.brier < 0.05


def test_accuracy_and_coverage_with_holds():
    # 2 of 4 are HOLD (None); of the 2 directional, 1 correct.
    p_ups = [0.7, None, 0.6, None]    # up(correct), -, up(wrong), -
    realized = [1, 0, 0, 1]
    m = evaluate_directions(p_ups, realized)
    assert m.n_total == 4 and m.n_directional == 2
    assert m.coverage == 0.5
    assert m.accuracy == 0.5


def test_brier_matches_manual():
    p_ups = [0.8, 0.3]
    realized = [1, 0]
    m = evaluate_directions(p_ups, realized)
    manual = ((0.8 - 1) ** 2 + (0.3 - 0) ** 2) / 2
    assert math.isclose(m.brier, manual, rel_tol=1e-9)


def test_ece_present_and_bounded():
    p_ups = [0.9, 0.85, 0.6, 0.55, 0.95]
    realized = [1, 1, 0, 1, 1]
    m = evaluate_directions(p_ups, realized)
    assert m.ece is not None and 0.0 <= m.ece <= 1.0
    assert sum(b.count for b in m.reliability) == m.n_directional
