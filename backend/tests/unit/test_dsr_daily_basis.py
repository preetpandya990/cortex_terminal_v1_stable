"""
Regression tests: DSR / financial metrics on the daily-portfolio basis
(2026-07-18).

The pooled CPCV/eval streams are (symbol × day) PANELS: ~30k rows over ~150
sessions. Treating rows as sequential periods saturated the DSR at exactly
1.0 for ALL models (T=30,000 in √(T−1) → Φ(z>8) for any positive pooled SR
— observed even on an annualised-Sharpe-0.338 model) and overflowed
``total_return`` to ~1e+104 via 30k-fold compounding. The fix collapses the
panel to the equal-weight daily portfolio series before any time-series
statistic is computed.
"""

import numpy as np
import pytest

from app.ml.evaluation.backtest import aggregate_daily_portfolio
from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
from app.ml.training.evaluator import calculate_financial_metrics


def _panel(n_days=150, n_symbols=200, mean=0.0005, std=0.02, seed=11):
    """Synthetic pooled panel: returns, timestamps, per-row probabilities."""
    rng = np.random.default_rng(seed)
    days = np.repeat(np.arange("2026-01-01", "2026-12-31", dtype="datetime64[D]")[:n_days], n_symbols)
    rets = rng.normal(mean, std, size=n_days * n_symbols)
    return days, rets


# ─── aggregate_daily_portfolio ───────────────────────────────────────────────

class TestAggregateDailyPortfolio:
    def test_collapses_panel_to_one_row_per_day(self):
        ts, rets = _panel(n_days=10, n_symbols=50)
        daily = aggregate_daily_portfolio(rets, ts)
        assert len(daily) == 10

    def test_equal_weight_mean_over_active_rows(self):
        ts = np.array(["2026-01-01"] * 3 + ["2026-01-02"] * 2, dtype="datetime64[D]")
        rets = np.array([0.01, 0.03, 999.0, -0.02, -0.04])
        active = np.array([True, True, False, True, True])
        daily = aggregate_daily_portfolio(rets, ts, active)
        assert daily[0] == pytest.approx(0.02)   # inactive 999 excluded
        assert daily[1] == pytest.approx(-0.03)

    def test_day_with_no_active_rows_is_cash(self):
        ts = np.array(["2026-01-01", "2026-01-02"], dtype="datetime64[D]")
        daily = aggregate_daily_portfolio(
            np.array([0.05, 0.07]), ts, np.array([True, False])
        )
        assert daily[1] == 0.0  # idle day stays on the time axis at 0


# ─── DSR saturation regression ───────────────────────────────────────────────

def _paths_with_ts(n_paths=7, n_days=150, n_symbols=200, edge=0.0002, seed=5):
    """CPCV-like paths carrying timestamps; modest positive edge."""
    rng = np.random.default_rng(seed)
    paths = []
    all_days = np.arange("2026-01-01", "2026-12-31", dtype="datetime64[D]")[:n_days]
    for p in range(n_paths):
        days = np.repeat(all_days, n_symbols // n_paths)
        n = len(days)
        paths.append({
            "proba": rng.uniform(0.3, 0.9, n),          # ~2/3 rows active
            "forward_return": rng.normal(edge, 0.02, n),
            "timestamp": days,
        })
    return paths


class TestDsrDailyBasis:
    def test_modest_edge_does_not_saturate(self):
        """The defining regression: a modest-Sharpe panel must yield a DSR
        strictly inside (0, 1) — the legacy basis printed exactly 1.0."""
        res = compute_dsr_and_pbo(_paths_with_ts(edge=0.0008))
        assert res["dsr_basis"] == "daily_portfolio"
        assert res["n_obs_pooled"] <= 150            # days, not 30k rows
        assert 0.0 < res["deflated_sharpe"] < 0.9999

    def test_zero_edge_minus_costs_is_rejected(self):
        """No gross edge + statutory charges/slippage = a losing strategy.
        The honest DSR must be LOW — and emphatically not the saturated 1.0
        the legacy basis printed for everything with a pulse."""
        res = compute_dsr_and_pbo(_paths_with_ts(edge=0.0, seed=23))
        assert res["deflated_sharpe"] < 0.5
        assert res["pooled_oos_sharpe"] < 0.0  # cost drag makes it negative

    def test_legacy_basis_still_available_without_timestamps(self):
        paths = _paths_with_ts()
        for p in paths:
            del p["timestamp"]
        res = compute_dsr_and_pbo(paths)
        assert res["dsr_basis"] == "pooled_rows_legacy"
        assert res["n_obs_pooled"] > 10_000           # the inflated T, flagged


# ─── Financial metrics on the daily basis ────────────────────────────────────

class TestFinancialMetricsDailyBasis:
    def test_total_return_is_finite_and_sane(self):
        """Legacy compounding over 30k pooled rows printed ~1e+104."""
        ts, rets = _panel(mean=0.001)
        preds = np.ones(len(rets), dtype=np.int8)
        m = calculate_financial_metrics(preds, rets, timestamps=ts)
        assert np.isfinite(m["total_return"])
        assert -1.0 <= m["total_return"] < 10.0       # a year of daily returns

    def test_max_drawdown_is_a_valid_fraction(self):
        ts, rets = _panel(mean=-0.0005, seed=7)
        preds = np.ones(len(rets), dtype=np.int8)
        m = calculate_financial_metrics(preds, rets, timestamps=ts)
        assert 0.0 <= m["max_drawdown"] <= 1.0

    def test_trade_stats_remain_row_based(self):
        """win_rate / n_trades are genuinely per-trade — unchanged by the fix."""
        ts, rets = _panel(n_days=5, n_symbols=10)
        preds = np.ones(len(rets), dtype=np.int8)
        m = calculate_financial_metrics(preds, rets, timestamps=ts)
        assert m["n_trades"] == 50

    def test_no_timestamps_falls_back_to_legacy_without_overflow(self):
        """Legacy path stays API-compatible but must no longer overflow."""
        _, rets = _panel(mean=0.001)
        preds = np.ones(len(rets), dtype=np.int8)
        m = calculate_financial_metrics(preds, rets)
        assert np.isfinite(m["total_return"])
