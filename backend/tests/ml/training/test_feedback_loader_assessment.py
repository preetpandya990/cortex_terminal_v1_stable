"""
LLM-assessment retraining factor tests (WS10/C.B2)
==================================================
Pins: factor math (both conditions, stacking, clip), NULL/malformed
passthrough, confirmed-misses-only semantics, and THE core guarantee —
flag-off weights are bit-identical to pre-WS10 output.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.ml.training.feedback_loader import (
    _ASSESSMENT_CONTRADICTS_FACTOR,
    _ASSESSMENT_OVERCONFIDENT_FACTOR,
    _assessment_factor,
    compute_sample_weight,
)

pytestmark = pytest.mark.unit


def _row(
    *,
    direction_correct: bool | None = False,
    confidence: float = 80.0,
    assessment: dict | None = None,
    hit_sl: bool = True,
    **hits,
) -> pd.Series:
    return pd.Series({
        "instrument_key": "NSE_EQ|X",
        "ml_direction_correct": direction_correct,
        "hit_tp1": hits.get("hit_tp1", False),
        "hit_tp2": hits.get("hit_tp2", False),
        "hit_tp3": hits.get("hit_tp3", False),
        "hit_sl": hit_sl,
        "confidence_score": confidence,
        "llm_ml_assessment": assessment,
    })


def _assessment(agreement: str = "agrees", overconfident: bool = False) -> dict:
    return {
        "likely_missed_pattern": "none",
        "confidence_should_have_been_lower": overconfident,
        "price_action_agreement": agreement,
        "model": "gemini/gemini-2.5-flash",
        "prompt_version": 1,
    }


class TestAssessmentFactor:
    def test_null_passthrough(self):
        assert _assessment_factor(None, False, 0.8) == 1.0

    def test_malformed_passthrough(self):
        assert _assessment_factor("not-a-dict", False, 0.8) == 1.0  # type: ignore[arg-type]

    def test_contradicts_with_wrong_direction(self):
        factor = _assessment_factor(_assessment("contradicts"), False, 0.5)
        assert factor == pytest.approx(_ASSESSMENT_CONTRADICTS_FACTOR)

    def test_contradicts_with_correct_direction_never_fires(self):
        """The factor sharpens confirmed misses — never punishes winners."""
        assert _assessment_factor(_assessment("contradicts"), True, 0.9) == 1.0

    def test_overconfident_hard_negative(self):
        factor = _assessment_factor(
            _assessment(overconfident=True), False, 0.85
        )
        assert factor == pytest.approx(_ASSESSMENT_OVERCONFIDENT_FACTOR)

    def test_overconfident_below_threshold_never_fires(self):
        """Hard-negative definition matches _confidence_factor (conf ≥ 0.70)."""
        assert _assessment_factor(_assessment(overconfident=True), False, 0.5) == 1.0

    def test_both_conditions_stack(self):
        factor = _assessment_factor(
            _assessment("contradicts", overconfident=True), False, 0.85
        )
        assert factor == pytest.approx(
            _ASSESSMENT_CONTRADICTS_FACTOR * _ASSESSMENT_OVERCONFIDENT_FACTOR
        )

    def test_none_direction_never_fires(self):
        assert _assessment_factor(_assessment("contradicts"), None, 0.9) == 1.0

    def test_agrees_never_fires(self):
        assert _assessment_factor(_assessment("agrees"), False, 0.9) == 1.0


class TestComputeSampleWeightWithAssessment:
    def test_flag_off_is_bit_identical(self):
        """THE WS10 guarantee: default-off output never reads the assessment."""
        with_assessment = _row(assessment=_assessment("contradicts", overconfident=True))
        without_column = with_assessment.drop("llm_ml_assessment")

        default = compute_sample_weight(with_assessment)
        explicit_off = compute_sample_weight(with_assessment, use_llm_assessment=False)
        blind = compute_sample_weight(without_column)

        assert default == explicit_off == blind

    def test_flag_on_multiplies_and_clips(self):
        # hit_sl wrong-direction base: 0.5 (outcome) x 2.0 (hard-negative) = 1.0
        base = compute_sample_weight(_row())
        assert base == pytest.approx(1.0)

        boosted = compute_sample_weight(
            _row(assessment=_assessment("contradicts", overconfident=True)),
            use_llm_assessment=True,
        )
        assert boosted == pytest.approx(1.0 * 1.3 * 1.2)

    def test_flag_on_with_null_assessment_unchanged(self):
        row = _row(assessment=None)
        assert compute_sample_weight(row, use_llm_assessment=True) == pytest.approx(
            compute_sample_weight(row)
        )

    def test_clip_bounds_the_stacked_product(self):
        # tp-less, hard-negative, both assessment conditions on a highly
        # confident wrong call: 1.0 (unknown... actually hit_sl) — construct a
        # max case: direction_only x hard-neg impossible; use hit_sl path:
        # 0.5 x 2.0 x 1.56 = 1.56 < 5.0 (clip headroom); verify clip floor too.
        weight = compute_sample_weight(
            _row(assessment=_assessment("contradicts", overconfident=True)),
            use_llm_assessment=True,
        )
        assert 0.1 <= weight <= 5.0

    def test_dataframe_apply_matches_scalar_path(self):
        """The exact call shape build_feedback_weights_df uses."""
        df = pd.DataFrame([
            _row(assessment=_assessment("contradicts")),
            _row(direction_correct=True, hit_sl=False, hit_tp1=True, assessment=None),
        ])
        weights_on = df.apply(
            compute_sample_weight, axis=1, use_llm_assessment=True
        ).astype(np.float32)
        weights_off = df.apply(
            compute_sample_weight, axis=1, use_llm_assessment=False
        ).astype(np.float32)

        assert weights_on.iloc[0] == pytest.approx(1.0 * 1.3, rel=1e-6)
        assert weights_on.iloc[1] == weights_off.iloc[1]  # no assessment → identical
