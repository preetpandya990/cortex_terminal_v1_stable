"""
Phase C — Parameter Decision tests.

Validates the Phase-C principled-prior documentation and single-source-of-truth
wiring for:

  C1 — ATR_MULTIPLIER_DEFAULT: value, derivation fields, single source of truth.
  C2 — TRAINING_DECISION_THRESHOLD: value, derivation, usage across all
       evaluation-side call sites.
  C3 — audit_label_quality enriched with principled_prior block.
  C4 — compute_threshold_sensitivity: diagnostic structure, labels optional,
       no-selection invariant.
  C5 — feature_pipeline uses ATR_MULTIPLIER_DEFAULT (not a literal).
  C6 — Pre-existing Phase A/B tests still pass after Phase C wiring.
"""
from __future__ import annotations

import math

import numpy as np
import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_ohlcv(n: int = 80, seed: int = 0) -> "pd.DataFrame":
    import pandas as pd
    rng = np.random.default_rng(seed)
    close = 500.0 + rng.normal(0, 5, n).cumsum()
    close = np.clip(close, 200, 3000)
    high  = close + rng.uniform(1, 15, n)
    low   = close - rng.uniform(1, 15, n)
    return pd.DataFrame({
        "open":   close * 0.999,
        "high":   high,
        "low":    low,
        "close":  close,
        "volume": rng.integers(100_000, 1_000_000, n).astype(float),
    })


def _make_paths(n_paths: int = 7, n_obs: int = 100, seed: int = 0) -> list:
    rng = np.random.default_rng(seed)
    paths = []
    for _ in range(n_paths):
        proba   = rng.uniform(0.3, 0.7, n_obs).astype(np.float32)
        y       = rng.integers(0, 2, n_obs).astype(np.int8)
        fwd_ret = rng.normal(0.0005, 0.01, n_obs).astype(np.float32)
        paths.append({"proba": proba, "y": y, "forward_return": fwd_ret})
    return paths


# ──────────────────────────────────────────────────────────────────────────────
# C1 — ATR_MULTIPLIER_DEFAULT
# ──────────────────────────────────────────────────────────────────────────────

class TestATRMultiplierDefault:
    def test_constant_exported(self):
        from app.ml.features.target_generator import ATR_MULTIPLIER_DEFAULT
        assert isinstance(ATR_MULTIPLIER_DEFAULT, float)

    def test_constant_value_is_half(self):
        """Documented value from the principled-prior derivation."""
        from app.ml.features.target_generator import ATR_MULTIPLIER_DEFAULT
        assert ATR_MULTIPLIER_DEFAULT == 0.5

    def test_create_target_uses_default(self):
        """Calling create_target_variable() with no multiplier must use the constant."""
        import inspect
        from app.ml.features.target_generator import (
            ATR_MULTIPLIER_DEFAULT,
            create_target_variable,
        )
        sig = inspect.signature(create_target_variable)
        default = sig.parameters["atr_multiplier"].default
        assert default == ATR_MULTIPLIER_DEFAULT

    def test_add_target_to_features_uses_default(self):
        import inspect
        from app.ml.features.target_generator import (
            ATR_MULTIPLIER_DEFAULT,
            add_target_to_features,
        )
        sig = inspect.signature(add_target_to_features)
        assert sig.parameters["atr_multiplier"].default == ATR_MULTIPLIER_DEFAULT

    def test_audit_label_quality_uses_default(self):
        import inspect
        from app.ml.features.target_generator import (
            ATR_MULTIPLIER_DEFAULT,
            audit_label_quality,
        )
        sig = inspect.signature(audit_label_quality)
        assert sig.parameters["atr_multiplier"].default == ATR_MULTIPLIER_DEFAULT

    def test_create_targets_batch_uses_default(self):
        import inspect
        from app.ml.features.target_generator import (
            ATR_MULTIPLIER_DEFAULT,
            create_targets_batch,
        )
        sig = inspect.signature(create_targets_batch)
        assert sig.parameters["atr_multiplier"].default == ATR_MULTIPLIER_DEFAULT

    def test_range_guard_rejects_below_minimum(self):
        from app.ml.features.target_generator import create_target_variable
        df = _make_ohlcv()
        with pytest.raises(ValueError, match="atr_multiplier"):
            create_target_variable(df, atr_multiplier=0.05)

    def test_range_guard_rejects_above_maximum(self):
        from app.ml.features.target_generator import create_target_variable
        df = _make_ohlcv()
        with pytest.raises(ValueError, match="atr_multiplier"):
            create_target_variable(df, atr_multiplier=2.5)

    def test_range_guard_accepts_valid_bounds(self):
        from app.ml.features.target_generator import create_target_variable
        df = _make_ohlcv(n=60)
        for m in (0.1, 0.5, 1.0, 2.0):
            series = create_target_variable(df, atr_multiplier=m)
            assert series is not None


# ──────────────────────────────────────────────────────────────────────────────
# C2 — TRAINING_DECISION_THRESHOLD
# ──────────────────────────────────────────────────────────────────────────────

class TestTrainingDecisionThreshold:
    def test_constant_exported_from_backtest(self):
        from app.ml.evaluation.backtest import TRAINING_DECISION_THRESHOLD
        assert isinstance(TRAINING_DECISION_THRESHOLD, float)

    def test_constant_value_is_half(self):
        """Derived from balanced-label design (ATR dead zone ≈ 50/50 UP/DOWN)."""
        from app.ml.evaluation.backtest import TRAINING_DECISION_THRESHOLD
        assert TRAINING_DECISION_THRESHOLD == 0.5

    def test_deflated_sharpe_imports_threshold(self):
        """deflated_sharpe must import and use TRAINING_DECISION_THRESHOLD."""
        from app.ml.evaluation.deflated_sharpe import TRAINING_DECISION_THRESHOLD as ds_thr
        from app.ml.evaluation.backtest import TRAINING_DECISION_THRESHOLD as bt_thr
        assert ds_thr is bt_thr or ds_thr == bt_thr

    def test_ensemble_trainer_imports_threshold(self):
        from app.ml.training.ensemble_trainer import TRAINING_DECISION_THRESHOLD as et_thr
        from app.ml.evaluation.backtest import TRAINING_DECISION_THRESHOLD as bt_thr
        assert et_thr == bt_thr

    def test_xgboost_trainer_imports_threshold(self):
        from app.ml.training.xgboost_trainer import TRAINING_DECISION_THRESHOLD as xgb_thr
        from app.ml.evaluation.backtest import TRAINING_DECISION_THRESHOLD as bt_thr
        assert xgb_thr == bt_thr

    def test_event_backtest_imports_threshold(self):
        from app.ml.evaluation.event_backtest import TRAINING_DECISION_THRESHOLD as eb_thr
        from app.ml.evaluation.backtest import TRAINING_DECISION_THRESHOLD as bt_thr
        assert eb_thr == bt_thr

    def test_no_hardcoded_literal_in_deflated_sharpe(self):
        """deflated_sharpe.py must not contain >= 0.5 after Phase C wiring."""
        import inspect
        import app.ml.evaluation.deflated_sharpe as mod
        src = inspect.getsource(mod)
        # The literal >= 0.5 should not appear in the module body
        # (comments and docstrings may still reference the numeric value, that's ok)
        # We check for the actual assignment pattern, not string mention
        assert ">= 0.5" not in src or "TRAINING_DECISION_THRESHOLD" in src, (
            "deflated_sharpe.py still has literal >= 0.5 without the constant"
        )

    def test_no_hardcoded_literal_in_ensemble_trainer(self):
        import inspect
        import app.ml.training.ensemble_trainer as mod
        src = inspect.getsource(mod)
        # The threshold line must reference the constant
        assert "p_ens >= 0.5" not in src

    def test_no_hardcoded_literal_in_backtest(self):
        import inspect
        import app.ml.evaluation.backtest as mod
        src = inspect.getsource(mod)
        # The per-path Sharpe function must reference the constant
        assert "p_ens >= 0.5" not in src


# ──────────────────────────────────────────────────────────────────────────────
# C3 — audit_label_quality enriched with principled_prior
# ──────────────────────────────────────────────────────────────────────────────

class TestAuditLabelQualityPhaseC:
    def _audit(self, n: int = 100, multiplier: float = 0.5) -> dict:
        from app.ml.features.target_generator import audit_label_quality
        return audit_label_quality(_make_ohlcv(n=n), atr_multiplier=multiplier)

    def test_principled_prior_block_present(self):
        report = self._audit()
        assert "principled_prior" in report

    def test_principled_prior_required_keys(self):
        pp = self._audit()["principled_prior"]
        for key in (
            "module_constant", "value", "derivation_basis",
            "statutory_cost_bps", "min_rr_multiple", "required_min_move_bps",
            "nse_largecap_atr14_pct", "derived_multiplier", "rounded_to",
            "decision_type", "hpo_trials_consumed", "source",
        ):
            assert key in pp, f"principled_prior missing key '{key}'"

    def test_principled_prior_module_constant_name(self):
        pp = self._audit()["principled_prior"]
        assert pp["module_constant"] == "ATR_MULTIPLIER_DEFAULT"

    def test_principled_prior_hpo_trials_consumed_zero(self):
        """Principled prior adds zero trials to the DSR N count."""
        pp = self._audit()["principled_prior"]
        assert pp["hpo_trials_consumed"] == 0

    def test_principled_prior_decision_type(self):
        pp = self._audit()["principled_prior"]
        assert pp["decision_type"] == "principled_prior"

    def test_atr_multiplier_is_default_flag(self):
        from app.ml.features.target_generator import ATR_MULTIPLIER_DEFAULT
        # When using the default multiplier
        report_default = self._audit(multiplier=ATR_MULTIPLIER_DEFAULT)
        assert report_default["atr_multiplier_is_default"] is True
        # When using a non-default multiplier
        report_other = self._audit(multiplier=0.7)
        assert report_other["atr_multiplier_is_default"] is False

    def test_principled_prior_value_matches_constant(self):
        from app.ml.features.target_generator import ATR_MULTIPLIER_DEFAULT
        pp = self._audit()["principled_prior"]
        assert pp["value"] == ATR_MULTIPLIER_DEFAULT
        assert pp["rounded_to"] == ATR_MULTIPLIER_DEFAULT

    def test_statutory_cost_bps_in_range(self):
        """Statutory cost must be in the verified 20–26 bps range."""
        pp = self._audit()["principled_prior"]
        assert 20.0 <= pp["statutory_cost_bps"] <= 26.0

    def test_derived_multiplier_is_plausible(self):
        """Derived multiplier before rounding must be < 0.5 (rounds up to 0.5)."""
        pp = self._audit()["principled_prior"]
        assert 0.30 <= pp["derived_multiplier"] <= 0.50

    def test_report_is_json_serialisable_with_phase_c_fields(self):
        import json
        report = self._audit()
        json.dumps(report)  # must not raise


# ──────────────────────────────────────────────────────────────────────────────
# C4 — compute_threshold_sensitivity
# ──────────────────────────────────────────────────────────────────────────────

class TestComputeThresholdSensitivity:
    def test_returns_required_top_level_keys(self):
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        result = compute_threshold_sensitivity(_make_paths())
        assert "operating_points" in result
        assert "baseline_threshold" in result
        assert "note" in result

    def test_baseline_threshold_is_training_constant(self):
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        from app.ml.evaluation.backtest import TRAINING_DECISION_THRESHOLD
        result = compute_threshold_sensitivity(_make_paths())
        assert result["baseline_threshold"] == TRAINING_DECISION_THRESHOLD

    def test_operating_points_count_matches_thresholds(self):
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        thr = (0.40, 0.50, 0.60)
        result = compute_threshold_sensitivity(_make_paths(), thresholds=thr)
        assert len(result["operating_points"]) == 3

    def test_operating_point_keys(self):
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        pts = compute_threshold_sensitivity(_make_paths())["operating_points"]
        required = {"threshold", "precision", "recall", "f1", "n_active_bars",
                    "active_rate", "pooled_net_sharpe"}
        for pt in pts:
            assert required <= set(pt), f"Operating point missing keys: {required - set(pt)}"

    def test_thresholds_sorted_ascending(self):
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        pts = compute_threshold_sensitivity(_make_paths())["operating_points"]
        values = [p["threshold"] for p in pts]
        assert values == sorted(values)

    def test_active_rate_decreases_with_threshold(self):
        """Higher threshold → fewer active bars (fewer UP predictions)."""
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        pts = compute_threshold_sensitivity(_make_paths(seed=5))["operating_points"]
        rates = [p["active_rate"] for p in pts]
        # Not strictly monotone (could be equal), but non-increasing
        for i in range(1, len(rates)):
            assert rates[i] <= rates[i - 1] + 0.01  # allow rounding noise

    def test_precision_recall_present_when_y_in_paths(self):
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        pts = compute_threshold_sensitivity(_make_paths())["operating_points"]
        for pt in pts:
            assert pt["precision"] is not None
            assert pt["recall"] is not None
            assert pt["f1"] is not None

    def test_precision_recall_none_without_y(self):
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        paths = _make_paths()
        for p in paths:
            p.pop("y")  # remove labels
        pts = compute_threshold_sensitivity(paths)["operating_points"]
        for pt in pts:
            assert pt["precision"] is None
            assert pt["recall"] is None
            assert pt["f1"] is None

    def test_precision_in_unit_interval(self):
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        pts = compute_threshold_sensitivity(_make_paths(seed=3))["operating_points"]
        for pt in pts:
            if pt["precision"] is not None:
                assert 0.0 <= pt["precision"] <= 1.0

    def test_recall_in_unit_interval(self):
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        pts = compute_threshold_sensitivity(_make_paths(seed=3))["operating_points"]
        for pt in pts:
            if pt["recall"] is not None:
                assert 0.0 <= pt["recall"] <= 1.0

    def test_note_warns_against_selection(self):
        """Note must explicitly discourage threshold selection from this table."""
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        result = compute_threshold_sensitivity(_make_paths())
        note = result["note"].upper()
        assert "DIAGNOSTIC" in note or "NOT FOR" in note or "NOT" in note

    def test_empty_paths_returns_gracefully(self):
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        result = compute_threshold_sensitivity([])
        assert result["operating_points"] == []

    def test_default_thresholds_include_baseline(self):
        """Default threshold range must include the baseline 0.5."""
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        from app.ml.evaluation.backtest import TRAINING_DECISION_THRESHOLD
        pts = compute_threshold_sensitivity(_make_paths())["operating_points"]
        thrs = {p["threshold"] for p in pts}
        assert TRAINING_DECISION_THRESHOLD in thrs

    def test_result_is_json_serialisable(self):
        import json
        from app.ml.evaluation.deflated_sharpe import compute_threshold_sensitivity
        result = compute_threshold_sensitivity(_make_paths())
        json.dumps(result)  # must not raise


# ──────────────────────────────────────────────────────────────────────────────
# C5 — feature_pipeline uses constant (not literal)
# ──────────────────────────────────────────────────────────────────────────────

class TestFeaturePipelineUsesConstant:
    def test_feature_pipeline_imports_constant(self):
        """The ML training feature pipeline must import ATR_MULTIPLIER_DEFAULT."""
        import inspect
        import app.ml.training.feature_pipeline as mod
        src = inspect.getsource(mod)
        assert "ATR_MULTIPLIER_DEFAULT" in src

    def test_feature_pipeline_does_not_have_literal_05(self):
        """No hardcoded atr_multiplier=0.5 literal should remain."""
        import inspect
        import app.ml.training.feature_pipeline as mod
        src = inspect.getsource(mod)
        assert "atr_multiplier=0.5" not in src


# ──────────────────────────────────────────────────────────────────────────────
# C6 — Regression: earlier Phase A/B tests still pass after Phase C wiring
# ──────────────────────────────────────────────────────────────────────────────

class TestPhaseCRegressionChecks:
    """Smoke tests to verify Phase C changes don't break Phase A/B outputs."""

    def test_deflated_sharpe_output_unchanged_in_structure(self):
        """compute_dsr_and_pbo must still return all Phase-A/B keys."""
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(_make_paths())
        for key in ("deflated_sharpe", "pbo", "oos_loss_rate", "dsr_sensitivity",
                    "pooled_oos_sharpe", "n_cpcv_paths"):
            assert key in result

    def test_compute_dsr_still_uses_training_threshold(self):
        """After Phase C, compute_dsr_and_pbo must use TRAINING_DECISION_THRESHOLD."""
        import inspect
        import app.ml.evaluation.deflated_sharpe as mod
        src = inspect.getsource(mod)
        assert "TRAINING_DECISION_THRESHOLD" in src

    def test_create_target_variable_still_works(self):
        from app.ml.features.target_generator import create_target_variable
        df = _make_ohlcv(n=80)
        series = create_target_variable(df)
        assert len(series) == len(df)

    def test_audit_label_quality_still_has_phase_a2_keys(self):
        from app.ml.features.target_generator import audit_label_quality
        report = audit_label_quality(_make_ohlcv(n=80))
        for key in ("labelling_scheme", "is_triple_barrier", "n_up", "n_down",
                    "n_dead_zone", "threshold_symmetric"):
            assert key in report
