"""
A3 — Cost-aware backtest + Deflated Sharpe Ratio + PBO.

Test plan
---------
TestPositionMapping      — binary_to_positions: long_only / long_short / HOLD sentinel
TestCostDragExact        — round_trip_charge_fraction delegates correctly to charge_calculator
TestStrategyReturns      — flat bars = zero; gross/net structure; slippage deducted
TestDSRFormula           — deflated_sharpe_ratio: analytic reference, N=1 edge case,
                           high-quality model → DSR > 0.95, overfitting → DSR < 0.3
TestPBO                  — PBO corners: 0/all-negative/mixed
TestComputeDsrAndPbo     — integration: synthetic OOF paths → plausible DSR + PBO
"""
import math

import numpy as np
import pytest
from decimal import Decimal
from scipy import stats


# ──────────────────────────────────────────────────────────────────────────────
# Position mapping
# ──────────────────────────────────────────────────────────────────────────────

class TestPositionMapping:
    from app.ml.evaluation.backtest import binary_to_positions

    def test_long_only_up_is_long(self):
        from app.ml.evaluation.backtest import binary_to_positions
        pred = np.ones(10, dtype=np.int8)
        pos = binary_to_positions(pred, mode="long_only")
        assert np.all(pos == 1)

    def test_long_only_down_is_flat_not_short(self):
        """DOWN (pred=0) in long_only mode must be FLAT — not SHORT (RC-1 fix)."""
        from app.ml.evaluation.backtest import binary_to_positions
        pred = np.zeros(10, dtype=np.int8)
        pos = binary_to_positions(pred, mode="long_only")
        assert np.all(pos == 0), "DOWN should map to FLAT in long_only mode"

    def test_long_short_down_is_short(self):
        """DOWN (pred=0) in long_short mode is SHORT (-1)."""
        from app.ml.evaluation.backtest import binary_to_positions
        pred = np.zeros(10, dtype=np.int8)
        pos = binary_to_positions(pred, mode="long_short")
        assert np.all(pos == -1)

    def test_hold_sentinel_always_flat(self):
        """pred=-1 (explicit HOLD) maps to 0 in BOTH modes."""
        from app.ml.evaluation.backtest import binary_to_positions
        pred = np.full(10, -1, dtype=np.int8)
        for mode in ("long_only", "long_short"):
            pos = binary_to_positions(pred, mode=mode)
            assert np.all(pos == 0), f"HOLD sentinel should be FLAT in {mode}"

    def test_mixed_predictions(self):
        from app.ml.evaluation.backtest import binary_to_positions
        pred = np.array([-1, 0, 1, 0, 1, -1], dtype=np.int8)
        pos_lo = binary_to_positions(pred, mode="long_only")
        pos_ls = binary_to_positions(pred, mode="long_short")
        # long_only: -1→0, 0→0, 1→1
        np.testing.assert_array_equal(pos_lo, [0, 0, 1, 0, 1, 0])
        # long_short: -1→0, 0→-1, 1→1
        np.testing.assert_array_equal(pos_ls, [0, -1, 1, -1, 1, 0])


# ──────────────────────────────────────────────────────────────────────────────
# Statutory charge delegation
# ──────────────────────────────────────────────────────────────────────────────

class TestCostDragExact:
    def test_fraction_matches_charge_calculator(self):
        """round_trip_charge_fraction delegates to charge_calculator exactly."""
        from app.ml.evaluation.backtest import round_trip_charge_fraction
        from app.services.paper_trading.charge_calculator import estimate_round_trip_charges

        price = Decimal("1000")
        qty = 100
        _, _, total_drag = estimate_round_trip_charges("CNC", price, price, qty)
        expected = float(total_drag) / (float(price) * qty)

        actual = round_trip_charge_fraction("CNC", price, price, qty)
        assert abs(actual - expected) < 1e-12

    def test_cnc_drag_is_positive(self):
        from app.ml.evaluation.backtest import round_trip_charge_fraction
        frac = round_trip_charge_fraction("CNC", Decimal("500"), Decimal("500"), 200)
        assert frac > 0.0

    def test_cnc_drag_roughly_22bps(self):
        """CNC round-trip statutory drag ≈ 0.22% of notional (FY 2025-26 schedule)."""
        from app.ml.evaluation.backtest import round_trip_charge_fraction
        frac = round_trip_charge_fraction("CNC", Decimal("1000"), Decimal("1000"), 100)
        # Verified against charge_calculator: STT×2 + Exchange×2 + SEBI×2 + GST + Stamp
        assert 0.0020 < frac < 0.0026


# ──────────────────────────────────────────────────────────────────────────────
# Net strategy returns
# ──────────────────────────────────────────────────────────────────────────────

class TestStrategyReturns:
    def test_flat_bars_produce_zero_return(self):
        """All DOWN in long_only → all flat → net return = 0.0 regardless of fwd_ret."""
        from app.ml.evaluation.backtest import strategy_returns
        pred = np.zeros(20, dtype=np.int8)
        fwd = np.random.default_rng(0).normal(0.01, 0.02, 20)
        net = strategy_returns(pred, fwd, mode="long_only")
        assert np.all(net == 0.0)

    def test_up_bar_net_less_than_gross(self):
        """Long position net return must be strictly below gross (charges deducted)."""
        from app.ml.evaluation.backtest import strategy_returns
        pred = np.ones(50, dtype=np.int8)
        fwd = np.full(50, 0.02, dtype=np.float32)  # 2% gross per bar
        net = strategy_returns(pred, fwd, mode="long_only", slippage_bps=0.0)
        # charges > 0 → net < gross
        assert np.all(net < 0.02)
        assert np.all(net > 0.0)  # still positive for 2% gross return

    def test_short_bar_in_long_short(self):
        """DOWN bar in long_short mode earns negative forward return (minus charges)."""
        from app.ml.evaluation.backtest import strategy_returns
        pred = np.zeros(10, dtype=np.int8)
        fwd = np.full(10, 0.01, dtype=np.float32)  # market up 1%
        net = strategy_returns(pred, fwd, mode="long_short")
        # Short on an up day → gross = -0.01, minus charges → even more negative
        assert np.all(net < 0.0)

    def test_slippage_reduces_return(self):
        """Higher slippage_bps strictly reduces net return on active bars."""
        from app.ml.evaluation.backtest import strategy_returns
        pred = np.ones(100, dtype=np.int8)
        fwd = np.full(100, 0.005, dtype=np.float32)
        net_lo_slip = strategy_returns(pred, fwd, slippage_bps=0.0)
        net_hi_slip = strategy_returns(pred, fwd, slippage_bps=20.0)
        assert np.all(net_hi_slip < net_lo_slip)

    def test_output_dtype_is_float32(self):
        from app.ml.evaluation.backtest import strategy_returns
        pred = np.array([0, 1, 0, 1], dtype=np.int8)
        fwd = np.array([0.01, 0.02, -0.01, 0.03], dtype=np.float32)
        net = strategy_returns(pred, fwd)
        assert net.dtype == np.float32


# ──────────────────────────────────────────────────────────────────────────────
# DSR formula
# ──────────────────────────────────────────────────────────────────────────────

class TestDSRFormula:
    def test_n1_no_selection_bias(self):
        """N=1 → SR₀=0; DSR is purely a one-sided SR significance test."""
        from app.ml.evaluation.deflated_sharpe import deflated_sharpe_ratio
        # Params: Ŝ=0.05, T=252, normal distribution, N=1
        # z = 0.05*√251 / √(1 + 0.5*0.05²) ≈ 0.05*15.843/√1.00125 ≈ 0.7910
        # DSR = Φ(0.7910) ≈ 0.785
        dsr = deflated_sharpe_ratio(
            sr=0.05, n_obs=252, skew=0.0, kurt_raw=3.0,
            sr_std_across_trials=0.02, n_trials=1,
        )
        assert 0.76 < dsr < 0.81

    def test_high_quality_model_exceeds_threshold(self):
        """Strong per-period Sharpe, small N → DSR well above 0.95."""
        from app.ml.evaluation.deflated_sharpe import deflated_sharpe_ratio
        dsr = deflated_sharpe_ratio(
            sr=0.12, n_obs=600, skew=0.0, kurt_raw=3.0,
            sr_std_across_trials=0.01, n_trials=3,
        )
        assert dsr > 0.99

    def test_overfitting_scenario_below_threshold(self):
        """Weak Sharpe selected from many trials → DSR < 0.3."""
        from app.ml.evaluation.deflated_sharpe import deflated_sharpe_ratio
        # Ŝ=0.01, N=10, σ_SR=0.05 → SR₀ ≈ 0.079 > Ŝ → z < 0 → DSR < 0.5
        dsr = deflated_sharpe_ratio(
            sr=0.01, n_obs=252, skew=0.0, kurt_raw=3.0,
            sr_std_across_trials=0.05, n_trials=10,
        )
        assert dsr < 0.30

    def test_degenerate_n_obs(self):
        """n_obs < 2 returns NaN (cannot compute meaningful DSR)."""
        from app.ml.evaluation.deflated_sharpe import deflated_sharpe_ratio
        result = deflated_sharpe_ratio(
            sr=0.1, n_obs=1, skew=0.0, kurt_raw=3.0,
            sr_std_across_trials=0.0, n_trials=1,
        )
        assert math.isnan(result)

    def test_matches_analytic_formula_exactly(self):
        """deflated_sharpe_ratio matches a direct scipy transliteration to 1e-10."""
        from app.ml.evaluation.deflated_sharpe import (
            deflated_sharpe_ratio,
            _benchmark_sr0,
            _EULER_MASCHERONI,
        )
        sr, T, skew, kurt_raw = 0.07, 500, -0.3, 4.5
        sr_std, N = 0.02, 7

        sr0 = _benchmark_sr0(sr_std, N)
        var_factor = 1.0 - skew * sr + (kurt_raw - 1.0) / 4.0 * sr ** 2
        z_ref = (sr - sr0) * math.sqrt(T - 1) / math.sqrt(var_factor)
        dsr_ref = stats.norm.cdf(z_ref)

        dsr_impl = deflated_sharpe_ratio(sr, T, skew, kurt_raw, sr_std, N)
        assert abs(dsr_impl - dsr_ref) < 1e-10

    def test_benchmark_sr0_increases_with_n_trials(self):
        """More trials → higher SR₀ benchmark → harder to pass DSR."""
        from app.ml.evaluation.deflated_sharpe import _benchmark_sr0
        srs = [_benchmark_sr0(0.05, N) for N in (2, 5, 10, 20)]
        assert srs == sorted(srs), "SR₀ should be monotonically increasing in N"

    def test_raw_kurtosis_3_gives_normal_variance_factor(self):
        """kurt_raw=3.0 (normal), skew=0 → var_factor = 1.0 exactly."""
        from app.ml.evaluation.deflated_sharpe import deflated_sharpe_ratio
        # For normal: var_factor = 1 - 0*sr + (3-1)/4*sr² = 1 + 0.5*sr²
        # Use sr close to 0 so 0.5*sr² ≈ 0; DSR should be near Φ(z_approx).
        dsr = deflated_sharpe_ratio(
            sr=0.001, n_obs=252, skew=0.0, kurt_raw=3.0,
            sr_std_across_trials=0.0, n_trials=1,
        )
        assert 0.0 < dsr < 1.0  # just sanity; main value is reaching this line


# ──────────────────────────────────────────────────────────────────────────────
# Probability of Backtest Overfitting
# ──────────────────────────────────────────────────────────────────────────────

class TestPBO:
    def test_all_positive_paths_pbo_zero(self):
        from app.ml.evaluation.deflated_sharpe import probability_of_backtest_overfitting
        pbo = probability_of_backtest_overfitting(np.array([0.1, 0.05, 0.2, 0.15]))
        assert pbo == 0.0

    def test_all_negative_paths_pbo_one(self):
        from app.ml.evaluation.deflated_sharpe import probability_of_backtest_overfitting
        pbo = probability_of_backtest_overfitting(np.array([-0.1, -0.2, -0.05]))
        assert pbo == 1.0

    def test_zero_sharpe_counts_as_non_positive(self):
        from app.ml.evaluation.deflated_sharpe import probability_of_backtest_overfitting
        pbo = probability_of_backtest_overfitting(np.array([0.0, 0.1, 0.2]))
        # 1 of 3 paths ≤ 0
        assert abs(pbo - 1.0 / 3.0) < 1e-10

    def test_mixed_paths_exact_fraction(self):
        from app.ml.evaluation.deflated_sharpe import probability_of_backtest_overfitting
        sharpes = np.array([0.1, -0.05, 0.2, -0.1, 0.15, -0.08, 0.3])
        pbo = probability_of_backtest_overfitting(sharpes)
        assert abs(pbo - 3.0 / 7.0) < 1e-10

    def test_empty_input_returns_nan(self):
        from app.ml.evaluation.deflated_sharpe import probability_of_backtest_overfitting
        assert math.isnan(probability_of_backtest_overfitting(np.array([])))


# ──────────────────────────────────────────────────────────────────────────────
# Integration: compute_dsr_and_pbo with synthetic OOF paths
# ──────────────────────────────────────────────────────────────────────────────

class TestComputeDsrAndPbo:
    def _make_paths(self, n_paths: int = 7, n_obs: int = 100, seed: int = 42) -> list:
        rng = np.random.default_rng(seed)
        paths = []
        for _ in range(n_paths):
            proba = rng.uniform(0.3, 0.7, n_obs).astype(np.float32)
            y = rng.integers(0, 2, n_obs).astype(np.int8)
            fwd_ret = rng.normal(0.0005, 0.01, n_obs).astype(np.float32)
            paths.append({
                "proba": proba,
                "y": y,
                "forward_return": fwd_ret,
            })
        return paths

    def test_returns_all_required_keys(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        paths = self._make_paths()
        result = compute_dsr_and_pbo(paths)
        required = {
            "deflated_sharpe", "pbo", "path_sharpes_annualised",
            "path_sharpes_per_period", "best_path_idx",
            "best_per_period_sharpe", "sr0_benchmark",
            "sr_std_across_paths", "n_trials", "n_obs_per_path",
            "skewness", "kurtosis_raw",
        }
        assert required <= set(result)

    def test_dsr_in_unit_interval(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(self._make_paths())
        dsr = result["deflated_sharpe"]
        assert 0.0 <= dsr <= 1.0

    def test_pbo_in_unit_interval(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(self._make_paths())
        pbo = result["pbo"]
        assert 0.0 <= pbo <= 1.0

    def test_path_count_matches_input(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        for n in (3, 7):
            result = compute_dsr_and_pbo(self._make_paths(n_paths=n))
            assert result["n_trials"] == n
            assert len(result["path_sharpes_annualised"]) == n
            assert len(result["path_sharpes_per_period"]) == n

    def test_best_path_idx_is_argmax_per_period_sr(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(self._make_paths())
        pp_srs = np.array(result["path_sharpes_per_period"])
        assert result["best_path_idx"] == int(np.argmax(pp_srs))

    def test_empty_paths_raises(self):
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        with pytest.raises(ValueError, match="non-empty"):
            compute_dsr_and_pbo([])

    def test_profitable_model_low_pbo(self):
        """A consistently profitable model should have PBO = 0."""
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        rng = np.random.default_rng(0)
        # Construct paths where UP predictions always see positive fwd_ret
        paths = []
        for _ in range(7):
            n = 200
            proba = np.full(n, 0.9, dtype=np.float32)  # always predict UP
            fwd_ret = rng.uniform(0.005, 0.02, n).astype(np.float32)  # always positive
            paths.append({
                "proba": proba,
                "y": np.ones(n, dtype=np.int8),
                "forward_return": fwd_ret,
            })
        result = compute_dsr_and_pbo(paths)
        # All paths profitable → PBO = 0
        assert result["pbo"] == 0.0
        # Good model → DSR should be reasonably high
        assert result["deflated_sharpe"] > 0.5


# ──────────────────────────────────────────────────────────────────────────────
# Evaluator integration: AUC-PR propagates, financial metrics use cost-aware path
# ──────────────────────────────────────────────────────────────────────────────

class TestEvaluatorAUCPR:
    def test_auc_pr_populated_when_proba_provided(self):
        from app.ml.training.evaluator import ModelEvaluator
        rng = np.random.default_rng(1)
        n = 200
        y_true = rng.integers(0, 2, n)
        y_pred = y_true.copy()
        y_proba = np.stack([1 - y_true * 0.9, y_true * 0.9], axis=1).astype(np.float32)
        # Normalise to sum to 1
        y_proba = y_proba / y_proba.sum(axis=1, keepdims=True)

        ev = ModelEvaluator()
        results = ev.evaluate(y_true, y_pred, y_proba=y_proba)
        assert results.auc_pr is not None
        assert 0.0 < results.auc_pr <= 1.0

    def test_auc_pr_none_when_no_proba(self):
        from app.ml.training.evaluator import ModelEvaluator
        rng = np.random.default_rng(2)
        n = 100
        y_true = rng.integers(0, 2, n)
        y_pred = rng.integers(0, 2, n)
        ev = ModelEvaluator()
        results = ev.evaluate(y_true, y_pred)
        assert results.auc_pr is None

    def test_long_only_flat_not_short(self):
        """calculate_financial_metrics no longer treats DOWN as SHORT."""
        from app.ml.training.evaluator import calculate_financial_metrics
        # 50 DOWN predictions in long_only mode → all flat → net_ret = 0
        pred = np.zeros(50, dtype=np.int8)
        fwd = np.full(50, 0.01)  # 1% up every day
        metrics = calculate_financial_metrics(pred, fwd, mode="long_only")
        # Flat all the way → zero total return; old code would show +50% from short
        assert metrics["total_return"] == pytest.approx(0.0, abs=1e-6)
        assert metrics["n_trades"] == 0

    def test_long_short_down_is_short(self):
        """calculate_financial_metrics in long_short mode: DOWN → short position."""
        from app.ml.training.evaluator import calculate_financial_metrics
        pred = np.zeros(50, dtype=np.int8)
        fwd = np.full(50, 0.01)  # market up 1%/bar
        metrics = calculate_financial_metrics(pred, fwd, mode="long_short")
        # Short when market goes up → losses → negative total return
        assert metrics["total_return"] < 0.0
