"""
Sentiment source-weight tests
=============================
WS3 (E.C): the hardcoded 2-entry source-weight dict is replaced by a linear
map over live ai_source_credibility scores. Pins the 0-100 → 0.4-1.0 contract.
"""
from __future__ import annotations

import pytest

from app.services.sentiment_analysis_service import credibility_to_weight

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.0, 0.4),      # floor: even a fully discredited source keeps 0.4
        (50.0, 0.7),     # neutral seed maps exactly to the old default
        (80.0, 0.88),    # exchange seed
        (100.0, 1.0),    # ceiling
    ],
)
def test_linear_map(score: float, expected: float):
    assert credibility_to_weight(score) == pytest.approx(expected)


def test_unknown_source_gets_default():
    assert credibility_to_weight(None) == pytest.approx(0.7)


@pytest.mark.parametrize("out_of_range", [-10.0, 150.0])
def test_out_of_range_scores_clamped(out_of_range: float):
    weight = credibility_to_weight(out_of_range)
    assert 0.4 <= weight <= 1.0
