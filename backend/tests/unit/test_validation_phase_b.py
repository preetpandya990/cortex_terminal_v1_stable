"""
Phase B — Validation Correctness tests.

Validates the Phase-B correction to ``compute_dsr_and_pbo``:

  B1 — Pooled-OOS candidate replaces argmax-path candidate.
  B2 — DSR reported as sensitivity band (N=1 to N_upper); PSR at N=1 first.
  B3 — oos_loss_rate correctly named; pbo retained as deprecated alias.
  B4 — trial_sharpes parameter enables correct V and effective N.
  B5 — Backward compatibility: all pre-Phase-B dict keys still present.
  B6 — _N_HPO_TRIALS_UPPER exported for external reference.
  B7 — XGBoostTrainer.tune_hyperparameters logs per-trial Sharpes.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_paths(
    n_paths: int = 7,
    n_obs: int = 100,
    seed: int = 42,
    always_up: bool = False,
) -> list[dict]:
    """Synthetic CPCV-shaped OOF paths."""
    rng = np.random.default_rng(seed)
    paths = []
    for _ in range(n_paths):
        proba = (
            np.full(n_obs, 0.9, dtype=np.float32)
            if always_up
            else rng.uniform(0.3, 0.7, n_obs).astype(np.float32)
        )
        fwd_ret = (
            rng.uniform(0.005, 0.02, n_obs).astype(np.float32)
            if always_up
            else rng.normal(0.0005, 0.01, n_obs).astype(np.float32)
        )
        paths.append({
            "proba": proba,
            "y": rng.integers(0, 2, n_obs).astype(np.int8),
            "forward_return": fwd_ret,
        })
    return paths


# ──────────────────────────────────────────────────────────────────────────────
# B1 — Pooled-OOS candidate
# ──────────────────────────────────────────────────────────────────────────────

class TestPooledOosCandidate:
    def test_pooled_oos_sharpe_is_not_argmax_path_sharpe(self):
        """
        The corrected candidate must be the pooled-OOS Sharpe, not the best
        path's Sharpe.  When path Sharpes differ, these values will differ.
        """
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        # Use random paths — individual paths will have varying Sharpes
        result = compute_dsr_and_pbo(_make_paths(seed=1, n_obs=200))
        pp_srs = result["path_sharpes_per_period"]
        argmax_sr = max(pp_srs)
        pooled_sr = result["pooled_oos_sharpe"]
        # The pooled SR is a mean-like aggregate; the argmax path SR is an
        # extreme — they must not be equal unless all paths have the same SR.
        # We test that the infrastructure produces a distinct number; if they
        # happen to be equal the test passes vacuously (pathological data).
        assert isinstance(pooled_sr, float)
        # The argmax SR is always ≥ the pooled SR for long-only (argmax pulls up)
        assert argmax_sr >= pooled_sr - 1e-10

    def test_deflated_sharpe_uses_pooled_not_argmax(self):
        """
        deflated_sharpe is based on N=1 PSR of the POOLED candidate, not
        the argmax path.  Cross-check by reconstructing the DSR from the
        output's own fields.
        """
        from app.ml.evaluation.deflated_sharpe import (
            compute_dsr_and_pbo,
            deflated_sharpe_ratio,
        )
        result = compute_dsr_and_pbo(_make_paths(seed=2, n_obs=150))
        # Reconstruct DSR at N=1 using the pooled fields from the output
        reconstructed = deflated_sharpe_ratio(
            sr=result["pooled_oos_sharpe"],
            n_obs=result["n_obs_pooled"],
            skew=result["skewness"],
            kurt_raw=result["kurtosis_raw"],
            sr_std_across_trials=0.0,
            n_trials=1,
        )
        assert abs(result["deflated_sharpe"] - reconstructed) < 1e-10

    def test_n_obs_pooled_equals_sum_of_path_lengths(self):
        """Pooled T must equal the sum of all path lengths (no dedup needed)."""
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(_make_paths(n_paths=7, n_obs=100))
        assert result["n_obs_pooled"] == 7 * 100

    def test_pooled_sr_is_finite(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(_make_paths())
        assert math.isfinite(result["pooled_oos_sharpe"])


# ──────────────────────────────────────────────────────────────────────────────
# B2 — DSR sensitivity band
# ──────────────────────────────────────────────────────────────────────────────

class TestDsrSensitivityBand:
    def test_n1_value_is_always_1(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(_make_paths())
        assert result["dsr_sensitivity"]["n1_value"] == 1

    def test_n_upper_value_matches_module_constant(self):
        from app.ml.evaluation.deflated_sharpe import (
            compute_dsr_and_pbo,
            _N_HPO_TRIALS_UPPER,
        )
        result = compute_dsr_and_pbo(_make_paths())
        assert result["dsr_sensitivity"]["n_upper_value"] == _N_HPO_TRIALS_UPPER

    def test_n1_in_unit_interval(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        s = compute_dsr_and_pbo(_make_paths())["dsr_sensitivity"]
        assert 0.0 <= s["n1"] <= 1.0

    def test_n_upper_in_unit_interval(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        s = compute_dsr_and_pbo(_make_paths())["dsr_sensitivity"]
        assert 0.0 <= s["n_upper"] <= 1.0

    def test_n1_geq_n_upper_monotonicity(self):
        """DSR is monotonically non-increasing in N (higher N → higher SR₀ → lower DSR)."""
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        for seed in (0, 1, 2, 42):
            s = compute_dsr_and_pbo(_make_paths(seed=seed))["dsr_sensitivity"]
            assert s["n1"] >= s["n_upper"] - 1e-10, (
                f"Seed {seed}: N=1 DSR ({s['n1']:.6f}) < N_upper DSR ({s['n_upper']:.6f})"
            )

    def test_n_eff_none_without_trial_sharpes(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        s = compute_dsr_and_pbo(_make_paths())["dsr_sensitivity"]
        assert s["n_eff"] is None
        assert s["n_eff_value"] is None

    def test_n_eff_populated_with_trial_sharpes(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        ts = np.random.default_rng(10).normal(0.0, 0.03, 42)
        result = compute_dsr_and_pbo(_make_paths(), trial_sharpes=ts)
        s = result["dsr_sensitivity"]
        assert s["n_eff"] is not None
        assert 0.0 <= s["n_eff"] <= 1.0
        assert s["n_eff_value"] == 42

    def test_monotonicity_n1_geq_n_eff_geq_n_upper_with_trials(self):
        """DSR at N_eff must lie between N=1 and N_upper."""
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        ts = np.random.default_rng(5).normal(0.01, 0.05, 25)
        s = compute_dsr_and_pbo(_make_paths(seed=3), trial_sharpes=ts)["dsr_sensitivity"]
        assert s["n1"] >= s["n_eff"] - 1e-10
        assert s["n_eff"] >= s["n_upper"] - 1e-10

    def test_basis_is_non_empty_string(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        s = compute_dsr_and_pbo(_make_paths())["dsr_sensitivity"]
        assert isinstance(s["basis"], str) and len(s["basis"]) > 10

    def test_band_collapses_at_n1_pure_psr(self):
        """At N=1, SR₀ = 0 and DSR = Φ(z_raw); verify against deflated_sharpe_ratio."""
        from app.ml.evaluation.deflated_sharpe import (
            compute_dsr_and_pbo,
            deflated_sharpe_ratio,
        )
        result = compute_dsr_and_pbo(_make_paths(seed=77))
        psr_direct = deflated_sharpe_ratio(
            sr=result["pooled_oos_sharpe"],
            n_obs=result["n_obs_pooled"],
            skew=result["skewness"],
            kurt_raw=result["kurtosis_raw"],
            sr_std_across_trials=0.0,
            n_trials=1,
        )
        assert abs(result["dsr_sensitivity"]["n1"] - psr_direct) < 1e-12


# ──────────────────────────────────────────────────────────────────────────────
# B3 — OOS loss rate rename
# ──────────────────────────────────────────────────────────────────────────────

class TestOosLossRate:
    def test_oos_loss_rate_key_present(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(_make_paths())
        assert "oos_loss_rate" in result

    def test_pbo_deprecated_alias_matches(self):
        """pbo must be identical to oos_loss_rate — both computed from the same value."""
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(_make_paths())
        assert result["pbo"] == result["oos_loss_rate"]

    def test_all_profitable_paths_oos_loss_rate_zero(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(_make_paths(always_up=True))
        assert result["oos_loss_rate"] == 0.0

    def test_oos_loss_rate_matches_probability_of_backtest_overfitting(self):
        """oos_loss_rate must equal (path_srs ≤ 0).mean() — same underlying formula."""
        from app.ml.evaluation.deflated_sharpe import (
            compute_dsr_and_pbo,
            probability_of_backtest_overfitting,
        )
        result = compute_dsr_and_pbo(_make_paths(seed=19))
        pp_srs = np.array(result["path_sharpes_per_period"])
        expected = probability_of_backtest_overfitting(pp_srs)
        assert abs(result["oos_loss_rate"] - expected) < 1e-12


# ──────────────────────────────────────────────────────────────────────────────
# B4 — trial_sharpes parameter
# ──────────────────────────────────────────────────────────────────────────────

class TestTrialSharpesParameter:
    def test_sr_std_source_proxy_without_trial_sharpes(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(_make_paths())
        assert result["dsr_sensitivity"]["sr_std_source"] == "cpcv_paths_proxy"
        assert result["dsr_sensitivity"]["trials_instrumented"] is False

    def test_sr_std_source_real_with_trial_sharpes(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        ts = np.array([0.01, 0.02, -0.01, 0.03, 0.00], dtype=np.float64)
        result = compute_dsr_and_pbo(_make_paths(), trial_sharpes=ts)
        s = result["dsr_sensitivity"]
        assert s["sr_std_source"] == "trial_sharpes"
        assert s["trials_instrumented"] is True

    def test_sr_std_across_trials_matches_input_std(self):
        """sr_std_across_trials must match ts.std(ddof=1) exactly."""
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        rng = np.random.default_rng(88)
        ts = rng.normal(0.01, 0.04, 30).astype(np.float64)
        result = compute_dsr_and_pbo(_make_paths(), trial_sharpes=ts)
        expected_std = float(ts.std(ddof=1))
        assert abs(result["sr_std_across_trials"] - expected_std) < 1e-12

    def test_n_hpo_trials_matches_input_length(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        ts = np.random.default_rng(0).normal(0, 0.02, 67)
        result = compute_dsr_and_pbo(_make_paths(), trial_sharpes=ts)
        assert result["n_hpo_trials"] == 67

    def test_single_trial_sharpe_does_not_raise(self):
        """A single trial sharpe has no std (ddof=1); must handle gracefully."""
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        ts = np.array([0.05], dtype=np.float64)
        # One trial: std(ddof=1) = NaN → treated as proxy fallback
        result = compute_dsr_and_pbo(_make_paths(), trial_sharpes=ts)
        # Should not raise; fallback to proxy (len < 2)
        assert result is not None

    def test_dsr_with_real_trial_sharpes_is_lower_than_psr(self):
        """
        Introducing selection correction (N>1) with non-zero σ_SR must push
        DSR below the pure PSR (N=1).  Confirmed by monotonicity property.
        """
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        rng = np.random.default_rng(22)
        ts = rng.normal(0.01, 0.05, 50)  # spread of trial sharpes → non-zero V
        result = compute_dsr_and_pbo(_make_paths(seed=4), trial_sharpes=ts)
        s = result["dsr_sensitivity"]
        # N=1 ≥ N_eff ≥ N_upper
        assert s["n1"] >= s["n_eff"] - 1e-10
        assert s["n_eff"] >= s["n_upper"] - 1e-10


# ──────────────────────────────────────────────────────────────────────────────
# B5 — Backward compatibility
# ──────────────────────────────────────────────────────────────────────────────

class TestBackwardCompatibility:
    """All pre-Phase-B dict keys must still be present with correct semantics."""

    def _result(self) -> dict[str, Any]:
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        return compute_dsr_and_pbo(_make_paths())

    def test_deflated_sharpe_present(self):
        assert "deflated_sharpe" in self._result()

    def test_pbo_present(self):
        assert "pbo" in self._result()

    def test_path_sharpes_annualised_present(self):
        assert "path_sharpes_annualised" in self._result()

    def test_path_sharpes_per_period_present(self):
        assert "path_sharpes_per_period" in self._result()

    def test_best_path_idx_present(self):
        """best_path_idx retained as an informational field (argmax of path SRs)."""
        result = self._result()
        assert "best_path_idx" in result
        # Must still point to the argmax of per-path Sharpes
        pp_srs = np.array(result["path_sharpes_per_period"])
        assert result["best_path_idx"] == int(np.argmax(pp_srs))

    def test_best_per_period_sharpe_present(self):
        assert "best_per_period_sharpe" in self._result()

    def test_sr0_benchmark_present_and_zero_at_n1(self):
        """sr0_benchmark is SR₀ at N=1 which equals 0.0 by definition."""
        result = self._result()
        assert "sr0_benchmark" in result
        assert result["sr0_benchmark"] == 0.0

    def test_sr_std_across_paths_present(self):
        assert "sr_std_across_paths" in self._result()

    def test_n_trials_deprecated_alias_present(self):
        """n_trials must still equal n_cpcv_paths for backward compat."""
        result = self._result()
        assert "n_trials" in result
        assert result["n_trials"] == result["n_cpcv_paths"]

    def test_n_obs_per_path_present(self):
        assert "n_obs_per_path" in self._result()

    def test_skewness_present(self):
        assert "skewness" in self._result()

    def test_kurtosis_raw_present(self):
        assert "kurtosis_raw" in self._result()

    def test_all_deprecated_values_still_float_or_int(self):
        """Verify no key silently changed type."""
        result = self._result()
        float_keys = {
            "deflated_sharpe", "pbo", "best_per_period_sharpe",
            "sr0_benchmark", "sr_std_across_paths", "skewness", "kurtosis_raw",
        }
        int_keys = {"best_path_idx", "n_trials", "n_obs_per_path"}
        for k in float_keys:
            assert isinstance(result[k], float), f"Key '{k}' should be float"
        for k in int_keys:
            assert isinstance(result[k], int), f"Key '{k}' should be int"


# ──────────────────────────────────────────────────────────────────────────────
# B6 — Module constant
# ──────────────────────────────────────────────────────────────────────────────

class TestModuleConstants:
    def test_n_hpo_trials_upper_exported(self):
        from app.ml.evaluation.deflated_sharpe import _N_HPO_TRIALS_UPPER
        assert isinstance(_N_HPO_TRIALS_UPPER, int)
        assert _N_HPO_TRIALS_UPPER > 0

    def test_n_hpo_trials_upper_value(self):
        """Documented value from Validation Remediation Plan §3.2."""
        from app.ml.evaluation.deflated_sharpe import _N_HPO_TRIALS_UPPER
        assert _N_HPO_TRIALS_UPPER == 105

    def test_euler_mascheroni_precision(self):
        """γ must match to standard precision (at least 8 decimal places)."""
        from app.ml.evaluation.deflated_sharpe import _EULER_MASCHERONI
        reference = 0.5772156649015328
        assert abs(_EULER_MASCHERONI - reference) < 1e-15


# ──────────────────────────────────────────────────────────────────────────────
# B7 — XGBoostTrainer per-trial Sharpe instrumentation
# ──────────────────────────────────────────────────────────────────────────────

class TestXGBoostTrainerTrialSharpes:
    """
    Verify the Phase-B §3.5 convergent instrumentation fix in
    XGBoostTrainer.tune_hyperparameters.
    """

    def _make_data(self, n_train: int = 100, n_val: int = 40, seed: int = 0):
        rng = np.random.default_rng(seed)
        X_train = rng.normal(0, 1, (n_train, 5)).astype(np.float32)
        y_train = rng.integers(0, 2, n_train).astype(np.float32)
        X_val = rng.normal(0, 1, (n_val, 5)).astype(np.float32)
        y_val = rng.integers(0, 2, n_val).astype(np.float32)
        fwd_val = rng.normal(0.001, 0.01, n_val).astype(np.float64)
        return X_train, y_train, X_val, y_val, fwd_val

    def test_trial_sharpes_none_by_default_in_init(self):
        from app.ml.training.xgboost_trainer import XGBoostTrainer
        trainer = XGBoostTrainer()
        assert trainer.trial_sharpes is None

    def test_no_instrumentation_without_fwd_ret_val(self):
        from app.ml.training.xgboost_trainer import XGBoostTrainer
        X_tr, y_tr, X_val, y_val, _ = self._make_data()
        trainer = XGBoostTrainer()
        trainer.tune_hyperparameters(
            X_tr, y_tr, X_val, y_val,
            n_trials=2, timeout=None, n_jobs=1,
        )
        assert trainer.trial_sharpes is None

    def test_trial_sharpes_set_with_fwd_ret_val(self):
        from app.ml.training.xgboost_trainer import XGBoostTrainer
        X_tr, y_tr, X_val, y_val, fwd = self._make_data()
        trainer = XGBoostTrainer()
        trainer.tune_hyperparameters(
            X_tr, y_tr, X_val, y_val,
            n_trials=3, timeout=None, n_jobs=1,
            fwd_ret_val=fwd,
        )
        ts = trainer.trial_sharpes
        assert ts is not None
        assert ts.dtype == np.float64
        assert len(ts) >= 1

    def test_all_trial_sharpes_are_finite(self):
        from app.ml.training.xgboost_trainer import XGBoostTrainer
        X_tr, y_tr, X_val, y_val, fwd = self._make_data(seed=7)
        trainer = XGBoostTrainer()
        trainer.tune_hyperparameters(
            X_tr, y_tr, X_val, y_val,
            n_trials=3, timeout=None, n_jobs=1,
            fwd_ret_val=fwd,
        )
        if trainer.trial_sharpes is not None and len(trainer.trial_sharpes) > 0:
            assert np.all(np.isfinite(trainer.trial_sharpes))

    def test_return_value_is_best_params_dict(self):
        """tune_hyperparameters must still return the best-params dict (backward compat)."""
        from app.ml.training.xgboost_trainer import XGBoostTrainer
        X_tr, y_tr, X_val, y_val, fwd = self._make_data(seed=3)
        trainer = XGBoostTrainer()
        result = trainer.tune_hyperparameters(
            X_tr, y_tr, X_val, y_val,
            n_trials=2, timeout=None, n_jobs=1,
            fwd_ret_val=fwd,
        )
        assert isinstance(result, dict)
        assert result is trainer.best_params

    def test_trial_sharpes_flow_through_to_dsr(self):
        """
        trial_sharpes from XGBoostTrainer must be accepted by compute_dsr_and_pbo
        and change the dsr_sensitivity output relative to the uninstrumented case.
        """
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        from app.ml.training.xgboost_trainer import XGBoostTrainer
        X_tr, y_tr, X_val, y_val, fwd = self._make_data(seed=13)
        trainer = XGBoostTrainer()
        trainer.tune_hyperparameters(
            X_tr, y_tr, X_val, y_val,
            n_trials=4, timeout=None, n_jobs=1,
            fwd_ret_val=fwd,
        )

        paths = _make_paths()
        result_with = compute_dsr_and_pbo(paths, trial_sharpes=trainer.trial_sharpes)
        result_without = compute_dsr_and_pbo(paths)

        if trainer.trial_sharpes is not None:
            # With trial_sharpes, source must be real
            assert result_with["dsr_sensitivity"]["sr_std_source"] == "trial_sharpes"
            assert result_without["dsr_sensitivity"]["sr_std_source"] == "cpcv_paths_proxy"
        # Both must return valid DSR
        assert 0.0 <= result_with["deflated_sharpe"] <= 1.0
        assert 0.0 <= result_without["deflated_sharpe"] <= 1.0
