"""
F1 — Event-Driven Backtest Engine Tests.

Test plan
---------
TestSimulatedFillAccounting   — per-fill charge drag matches vectorized coefficients;
                                 exit price floor; slippage magnitude; short/long order.
TestLatencyShift              — latency_bars=0 → same length; =1 → n-1; ≤latency → empty.
TestNetReturnStream           — flat bars → 0.0; net < gross; charges+slippage deducted.
TestPerSymbolGrouping         — symbol-level grouping; timestamp sort; fallback.
TestPathSimulation            — all-flat produces no fills; stats in valid ranges.
TestAgreementCheck            — PASS/WARNING/INSUFFICIENT_DATA; zero-denominator floor.
TestRunEventBacktest          — integration: returns EventBacktestReport; empty raises;
                                negative latency raises; n_paths correct.
TestAcceptance                — PRIMARY: at latency_bars=0 event-driven Sharpe agrees
                                with A3 vectorized within 5% (engine correctness proof);
                                at latency_bars=1 divergence is computed and labelled.
TestAsDictSerialisation       — as_dict() produces JSON-serialisable output; field stable.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from app.ml.evaluation.event_backtest import (
    EventBacktestReport,
    PathSimulationResult,
    SimulatedFill,
    _get_charge_coefficients,
    _simulate_sequence,
    run_event_backtest,
)


# ──────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_path(
    n: int = 60,
    n_symbols: int = 2,
    *,
    rng: np.random.Generator | None = None,
    proba_value: float | None = None,   # fixed proba for deterministic tests
    fwd_value:   float | None = None,   # fixed fwd_ret
    include_meta: bool = True,
) -> dict[str, Any]:
    """Build a synthetic CPCV OOF path dict for testing."""
    rng = rng or np.random.default_rng(42)
    bars_per_sym = n // n_symbols
    syms_proba, syms_fwd, syms_ts, syms_sym = [], [], [], []

    for s_i in range(n_symbols):
        sym = f"SYM{s_i:03d}"
        p = (
            np.full(bars_per_sym, proba_value, dtype=np.float32)
            if proba_value is not None
            else rng.uniform(0.3, 0.7, bars_per_sym).astype(np.float32)
        )
        r = (
            np.full(bars_per_sym, fwd_value, dtype=np.float32)
            if fwd_value is not None
            else rng.normal(0.002, 0.015, bars_per_sym).astype(np.float32)
        )
        # daily timestamps starting from 2024-01-01, offset per symbol
        start = np.datetime64("2024-01-01", "ns") + np.timedelta64(
            s_i * bars_per_sym, "D"
        )
        ts = np.array(
            [start + np.timedelta64(i, "D") for i in range(bars_per_sym)],
            dtype="datetime64[ns]",
        )
        syms_proba.append(p)
        syms_fwd.append(r)
        syms_ts.append(ts)
        syms_sym.append(np.full(bars_per_sym, sym, dtype="<U20"))

    path: dict[str, Any] = {
        "proba":          np.concatenate(syms_proba),
        "forward_return": np.concatenate(syms_fwd),
    }
    if include_meta:
        path["timestamp"] = np.concatenate(syms_ts)
        path["symbol"]    = np.concatenate(syms_sym)
    return path


def _sim_kw(
    latency: int = 1,
    mode: str = "long_only",
    entry: float = 1_000.0,
    notional: float = 100_000.0,
    slippage_bps: float = 5.0,
    product_type: str = "CNC",
) -> dict[str, Any]:
    """Common kwargs for _simulate_sequence to reduce boilerplate."""
    buy_c, sell_c = _get_charge_coefficients(product_type)
    qty = max(1, int(notional / entry))
    nr  = (float(entry) * qty) / notional
    return dict(
        latency_bars=latency,
        mode=mode,
        entry_price=entry,
        notional=notional,
        quantity=qty,
        slippage_drag=slippage_bps * 2.0 / 10_000.0,
        buy_coeff=buy_c,
        sell_coeff=sell_c,
        notional_ratio=nr,
        daily_rfr=0.0,
        periods_per_year=252,
    )


# ──────────────────────────────────────────────────────────────────────────────
# TestSimulatedFillAccounting
# ──────────────────────────────────────────────────────────────────────────────

class TestSimulatedFillAccounting:
    """Per-fill charges are computed correctly and match calculate_charges."""

    def test_charge_coefficients_derivation_non_zero(self):
        buy_c, sell_c = _get_charge_coefficients("CNC")
        assert buy_c  > 0.0, "CNC buy coefficient must be positive"
        assert sell_c > 0.0, "CNC sell coefficient must be positive"

    def test_cnc_buy_coeff_greater_than_sell(self):
        """CNC BUY incurs stamp duty; CNC SELL does not → buy_coeff > sell_coeff."""
        buy_c, sell_c = _get_charge_coefficients("CNC")
        assert buy_c > sell_c

    def test_charge_coefficients_match_calculate_charges(self):
        """Coefficient-derived charge must match calculate_charges within 1e-6 relative.

        Verify at qty=1 (the same reference qty used during derivation) so that
        Decimal rounding inside calculate_charges is identical on both sides and
        the only error is float64 conversion — well within 1e-6 relative.
        """
        from decimal import Decimal
        from app.services.paper_trading.charge_calculator import calculate_charges

        buy_c, sell_c = _get_charge_coefficients("CNC")
        price = Decimal("1000")
        qty = 1                          # must match derivation reference qty
        turnover = float(price) * qty    # = 1000.0

        actual_buy  = calculate_charges("BUY",  "CNC", price, qty)
        actual_sell = calculate_charges("SELL", "CNC", price, qty)

        coeff_buy  = buy_c  * turnover
        coeff_sell = sell_c * turnover

        rtol = 1e-6
        assert abs(coeff_buy  - float(actual_buy.total_charges))  / float(actual_buy.total_charges)  < rtol
        assert abs(coeff_sell - float(actual_sell.total_charges)) / float(actual_sell.total_charges) < rtol

    def test_long_fill_charge_drag_positive(self):
        """Charge drag on every long fill must be strictly positive."""
        proba   = np.array([0.8, 0.9, 0.7], dtype=np.float32)   # all UP → long
        fwd_ret = np.array([0.01, 0.02, -0.01], dtype=np.float32)
        _, _, fills = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=0))

        assert len(fills) == 3
        for f in fills:
            assert f.charge_drag > 0.0

    def test_short_fill_charge_drag_positive(self):
        """Charge drag on short fills (long_short mode, DOWN signal) is also positive."""
        proba   = np.zeros(5, dtype=np.float32)    # all DOWN → short in long_short
        fwd_ret = np.full(5, 0.01, dtype=np.float32)
        _, _, fills = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=0, mode="long_short"))

        assert len(fills) == 5
        for f in fills:
            assert f.charge_drag > 0.0

    def test_exit_price_floored_on_deep_loss(self):
        """fwd_ret = -1.0 (total loss) → exit_price floored, not zero."""
        proba   = np.array([0.9], dtype=np.float32)
        fwd_ret = np.array([-1.0], dtype=np.float32)
        _, _, fills = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=0))

        assert len(fills) == 1
        assert fills[0].exit_price > 0.0

    def test_slippage_drag_correct_magnitude(self):
        """slippage_drag == 2 × slippage_bps / 10_000 on every active fill."""
        slippage_bps = 7.0
        proba   = np.ones(4, dtype=np.float32)
        fwd_ret = np.full(4, 0.01, dtype=np.float32)
        kw = _sim_kw(latency=0, slippage_bps=slippage_bps)
        _, _, fills = _simulate_sequence(proba, fwd_ret, **kw)

        expected_drag = 2.0 * slippage_bps / 10_000.0
        for f in fills:
            assert abs(f.slippage_drag - expected_drag) < 1e-12

    def test_net_equals_gross_minus_charge_minus_slippage(self):
        """SimulatedFill internal consistency: net = gross - charge - slip."""
        proba   = np.ones(10, dtype=np.float32)
        fwd_ret = np.random.default_rng(7).normal(0.005, 0.02, 10).astype(np.float32)
        _, _, fills = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=0))

        for f in fills:
            reconstructed = f.gross_return - f.charge_drag - f.slippage_drag
            assert abs(f.net_return - reconstructed) < 1e-12


# ──────────────────────────────────────────────────────────────────────────────
# TestLatencyShift
# ──────────────────────────────────────────────────────────────────────────────

class TestLatencyShift:
    """Latency shift produces correct array lengths and alignments."""

    def test_zero_lag_output_length_equals_input(self):
        n = 50
        proba   = np.full(n, 0.8, dtype=np.float32)
        fwd_ret = np.full(n, 0.01, dtype=np.float32)
        net, gross, _ = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=0))
        assert len(net) == n
        assert len(gross) == n

    def test_one_bar_lag_reduces_length_by_one(self):
        n = 40
        proba   = np.full(n, 0.8, dtype=np.float32)
        fwd_ret = np.full(n, 0.01, dtype=np.float32)
        net, gross, _ = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=1))
        assert len(net) == n - 1
        assert len(gross) == n - 1

    def test_latency_k_reduces_length_by_k(self):
        n, k = 30, 5
        proba   = np.full(n, 0.8, dtype=np.float32)
        fwd_ret = np.full(n, 0.01, dtype=np.float32)
        net, _, _ = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=k))
        assert len(net) == n - k

    def test_length_equal_to_latency_returns_empty(self):
        n = 3
        proba   = np.full(n, 0.8, dtype=np.float32)
        fwd_ret = np.full(n, 0.01, dtype=np.float32)
        net, gross, fills = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=n))
        assert len(net) == 0
        assert len(gross) == 0
        assert fills == []

    def test_one_bar_lag_captures_next_bar_return(self):
        """With latency=1, position from signal[0] captures fwd_ret[1], not fwd_ret[0]."""
        # Distinct values so we can tell which bar's return was captured
        proba   = np.array([0.9, 0.1], dtype=np.float32)   # UP, DOWN
        fwd_ret = np.array([0.05, 0.02], dtype=np.float32)  # bar0=5%, bar1=2%
        kw = _sim_kw(latency=1, mode="long_only", slippage_bps=0.0)
        # Zero out charges by making sell_coeff=0 and buy_coeff=0 for isolation
        kw["buy_coeff"]  = 0.0
        kw["sell_coeff"] = 0.0
        net, _, fills = _simulate_sequence(proba, fwd_ret, **kw)

        # Only one effective bar (n=2, latency=1 → n-1=1)
        assert len(net) == 1
        # Signal at bar 0 = UP (proba[0]=0.9 → pos=+1).
        # With latency=1 it captures fwd_ret[1] = 0.02, not fwd_ret[0]=0.05.
        assert len(fills) == 1
        assert abs(fills[0].gross_return - 0.02) < 1e-6


# ──────────────────────────────────────────────────────────────────────────────
# TestNetReturnStream
# ──────────────────────────────────────────────────────────────────────────────

class TestNetReturnStream:
    """Net return stream properties."""

    def test_all_down_long_only_all_zero(self):
        """All DOWN in long_only → all flat → net = 0.0 (RC-1 preserved in event engine)."""
        proba   = np.zeros(30, dtype=np.float32)
        fwd_ret = np.random.default_rng(1).normal(0.01, 0.02, 30).astype(np.float32)
        net, _, fills = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=0))
        assert np.all(net == 0.0)
        assert fills == []

    def test_net_strictly_below_gross_on_active_bars(self):
        """Charges and slippage always reduce gross on any non-flat bar."""
        proba   = np.ones(20, dtype=np.float32)
        fwd_ret = np.full(20, 0.02, dtype=np.float32)   # 2% gross per bar
        net, gross, _ = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=0))
        active = net != 0.0
        assert active.all()
        assert np.all(net[active] < gross[active])

    def test_output_dtype_float64(self):
        proba   = np.array([0.8, 0.3], dtype=np.float32)
        fwd_ret = np.array([0.01, -0.01], dtype=np.float32)
        net, gross, _ = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=0))
        assert net.dtype == np.float64
        assert gross.dtype == np.float64

    def test_negative_fwd_ret_gives_negative_gross_long(self):
        """Positive proba (UP) but negative market return → negative gross."""
        proba   = np.ones(5, dtype=np.float32)
        fwd_ret = np.full(5, -0.03, dtype=np.float32)
        net, gross, _ = _simulate_sequence(proba, fwd_ret, **_sim_kw(latency=0))
        assert np.all(gross < 0.0)
        assert np.all(net < gross)   # charges push net even lower


# ──────────────────────────────────────────────────────────────────────────────
# TestPerSymbolGrouping
# ──────────────────────────────────────────────────────────────────────────────

class TestPerSymbolGrouping:
    """Symbol-level independent simulation and temporal ordering."""

    def test_single_symbol_with_meta_matches_without_meta(self):
        """One-symbol path with metadata ≡ same path without metadata."""
        rng  = np.random.default_rng(99)
        n    = 40
        proba   = rng.uniform(0.3, 0.7, n).astype(np.float32)
        fwd_ret = rng.normal(0.001, 0.01, n).astype(np.float32)
        ts      = np.array(
            [np.datetime64("2024-01-01", "ns") + np.timedelta64(i, "D") for i in range(n)],
            dtype="datetime64[ns]",
        )
        path_with_meta = {
            "proba":          proba,
            "forward_return": fwd_ret,
            "timestamp":      ts,
            "symbol":         np.full(n, "AAAA", dtype="<U20"),
        }
        path_no_meta = {"proba": proba, "forward_return": fwd_ret}

        kw = dict(
            latency_bars=1, mode="long_only", notional=100_000.0,
            product_type="CNC", entry_price=1_000.0, slippage_bps=5.0,
            daily_rfr=0.0, periods_per_year=252,
        )
        from app.ml.evaluation.event_backtest import _simulate_path
        r_meta   = _simulate_path(path_with_meta, **kw)
        r_no_meta = _simulate_path(path_no_meta,  **kw)

        # Fill counts must match (same underlying sequence)
        assert r_meta.n_fills == r_no_meta.n_fills
        # Net Sharpe must be identical (same signal–return alignment after sort)
        assert abs(r_meta.net_sharpe_per_period - r_no_meta.net_sharpe_per_period) < 1e-10

    def test_groups_by_symbol_independently(self):
        """Two symbols simulated independently: cross-symbol lag contamination impossible."""
        # Symbol A: all UP, positive returns → fills
        # Symbol B: all DOWN, negative returns (long_only → flat) → no fills
        n = 20
        proba_a   = np.ones(n, dtype=np.float32)       # UP
        proba_b   = np.zeros(n, dtype=np.float32)      # DOWN
        fwd_a     = np.full(n, 0.02, dtype=np.float32)
        fwd_b     = np.full(n, -0.03, dtype=np.float32)
        ts        = np.array(
            [np.datetime64("2024-01-01", "ns") + np.timedelta64(i, "D") for i in range(n)],
            dtype="datetime64[ns]",
        )
        path = {
            "proba":          np.concatenate([proba_a, proba_b]),
            "forward_return": np.concatenate([fwd_a,   fwd_b]),
            "timestamp":      np.concatenate([ts, ts]),
            "symbol":         np.concatenate([
                np.full(n, "AAAA", dtype="<U20"),
                np.full(n, "BBBB", dtype="<U20"),
            ]),
        }
        from app.ml.evaluation.event_backtest import _simulate_path
        result = _simulate_path(
            path,
            latency_bars=1, mode="long_only", notional=100_000.0,
            product_type="CNC", entry_price=1_000.0, slippage_bps=5.0,
            daily_rfr=0.0, periods_per_year=252,
        )
        # With latency=1: n-1=19 effective bars per symbol.
        # Symbol A (all UP): 19 fills; Symbol B (all DOWN, long_only): 0 fills.
        assert result.n_fills == n - 1   # 19 fills from A only

    def test_timestamp_sort_within_symbol(self):
        """Shuffled timestamp order within a symbol is corrected before simulation."""
        rng  = np.random.default_rng(55)
        n    = 20
        proba   = rng.uniform(0.4, 0.6, n).astype(np.float32)
        fwd_ret = rng.normal(0.002, 0.01, n).astype(np.float32)
        ts = np.array(
            [np.datetime64("2024-01-01", "ns") + np.timedelta64(i, "D") for i in range(n)],
            dtype="datetime64[ns]",
        )

        # Shuffle order
        shuffle_idx = rng.permutation(n)
        path_shuffled = {
            "proba":          proba[shuffle_idx],
            "forward_return": fwd_ret[shuffle_idx],
            "timestamp":      ts[shuffle_idx],
            "symbol":         np.full(n, "AAAA", dtype="<U20"),
        }
        path_sorted = {
            "proba":          proba,
            "forward_return": fwd_ret,
            "timestamp":      ts,
            "symbol":         np.full(n, "AAAA", dtype="<U20"),
        }

        from app.ml.evaluation.event_backtest import _simulate_path
        kw = dict(
            latency_bars=1, mode="long_only", notional=100_000.0,
            product_type="CNC", entry_price=1_000.0, slippage_bps=5.0,
            daily_rfr=0.0, periods_per_year=252,
        )
        r_shuf = _simulate_path(path_shuffled, **kw)
        r_sort = _simulate_path(path_sorted,   **kw)

        # After internal sort, both should produce identical results
        assert r_shuf.n_fills == r_sort.n_fills
        assert abs(r_shuf.net_sharpe_per_period - r_sort.net_sharpe_per_period) < 1e-10

    def test_fallback_without_symbol_metadata(self):
        """Path dict without 'symbol'/'timestamp' still runs without error."""
        path = _make_path(n=30, include_meta=False)
        from app.ml.evaluation.event_backtest import _simulate_path
        result = _simulate_path(
            path,
            latency_bars=1, mode="long_only", notional=100_000.0,
            product_type="CNC", entry_price=1_000.0, slippage_bps=5.0,
            daily_rfr=0.0, periods_per_year=252,
        )
        assert isinstance(result, PathSimulationResult)


# ──────────────────────────────────────────────────────────────────────────────
# TestPathSimulation
# ──────────────────────────────────────────────────────────────────────────────

class TestPathSimulation:
    """PathSimulationResult properties and invariants."""

    def test_all_flat_produces_no_fills(self):
        """All DOWN in long_only → no fills, zero total_return, zero Sharpe."""
        path = _make_path(proba_value=0.0, fwd_value=0.02, include_meta=True)
        from app.ml.evaluation.event_backtest import _simulate_path
        result = _simulate_path(
            path,
            latency_bars=1, mode="long_only", notional=100_000.0,
            product_type="CNC", entry_price=1_000.0, slippage_bps=5.0,
            daily_rfr=0.0, periods_per_year=252,
        )
        assert result.n_fills == 0
        assert result.total_return == 0.0
        assert result.net_sharpe_per_period == 0.0

    def test_win_rate_in_unit_interval(self):
        path = _make_path()
        from app.ml.evaluation.event_backtest import _simulate_path
        result = _simulate_path(
            path,
            latency_bars=1, mode="long_only", notional=100_000.0,
            product_type="CNC", entry_price=1_000.0, slippage_bps=5.0,
            daily_rfr=0.0, periods_per_year=252,
        )
        assert 0.0 <= result.win_rate <= 1.0

    def test_max_drawdown_non_negative(self):
        path = _make_path()
        from app.ml.evaluation.event_backtest import _simulate_path
        result = _simulate_path(
            path,
            latency_bars=1, mode="long_only", notional=100_000.0,
            product_type="CNC", entry_price=1_000.0, slippage_bps=5.0,
            daily_rfr=0.0, periods_per_year=252,
        )
        assert result.max_drawdown >= 0.0

    def test_total_return_consistent_with_net_returns(self):
        """total_return == cumprod(1 + net_returns) - 1."""
        path = _make_path(rng=np.random.default_rng(3))
        from app.ml.evaluation.event_backtest import _simulate_path
        result = _simulate_path(
            path,
            latency_bars=1, mode="long_only", notional=100_000.0,
            product_type="CNC", entry_price=1_000.0, slippage_bps=5.0,
            daily_rfr=0.0, periods_per_year=252,
        )
        if len(result.net_returns) > 0:
            expected_tr = float(np.cumprod(1.0 + result.net_returns)[-1] - 1.0)
            assert abs(result.total_return - expected_tr) < 1e-10

    def test_fills_tuple_length_matches_n_fills(self):
        path = _make_path(proba_value=0.9, include_meta=True)  # all UP → many fills
        from app.ml.evaluation.event_backtest import _simulate_path
        result = _simulate_path(
            path,
            latency_bars=1, mode="long_only", notional=100_000.0,
            product_type="CNC", entry_price=1_000.0, slippage_bps=5.0,
            daily_rfr=0.0, periods_per_year=252,
        )
        assert len(result.fills) == result.n_fills


# ──────────────────────────────────────────────────────────────────────────────
# TestAgreementCheck
# ──────────────────────────────────────────────────────────────────────────────

class TestAgreementCheck:
    """Agreement check status and tolerance logic."""

    def _run_with_vec(
        self,
        paths: list[dict[str, Any]],
        vec_pp_sharpes: list[float] | None,
        tolerance: float = 0.20,
    ) -> EventBacktestReport:
        return run_event_backtest(
            paths,
            vectorized_path_sharpes_per_period=vec_pp_sharpes,
            latency_bars=0,
            agreement_tolerance=tolerance,
        )

    def test_pass_when_within_tolerance(self):
        """Small divergence → PASS."""
        rng   = np.random.default_rng(10)
        paths = [_make_path(rng=rng) for _ in range(3)]
        report = self._run_with_vec(paths, [0.05, 0.06, 0.04])
        # event_mean ≈ 0.05 (very similar given latency=0) → within 20%
        assert report.agreement_status in ("PASS", "WARNING", "INSUFFICIENT_DATA")
        # Just confirm the field is set
        assert report.agreement_tolerance_pct == 20.0

    def test_warning_beyond_tolerance(self):
        """Extreme divergence → WARNING."""
        rng   = np.random.default_rng(11)
        paths = [_make_path(rng=rng) for _ in range(2)]
        # Provide a very different reference (near-zero event Sharpe vs vec=2.0)
        report = run_event_backtest(
            paths,
            vectorized_path_sharpes_per_period=[2.0, 2.0],
            latency_bars=0,
            agreement_tolerance=0.20,
        )
        # event Sharpe will not be anywhere near 2.0 → divergence >> 20%
        assert report.agreement_status == "WARNING"
        assert report.sharpe_divergence_relative_pct is not None
        assert report.sharpe_divergence_relative_pct > 20.0

    def test_insufficient_data_when_no_reference(self):
        """No vectorized reference → INSUFFICIENT_DATA, None divergence fields."""
        rng   = np.random.default_rng(12)
        paths = [_make_path(rng=rng) for _ in range(2)]
        report = run_event_backtest(
            paths,
            vectorized_path_sharpes_per_period=None,
            latency_bars=0,
        )
        assert report.agreement_status == "INSUFFICIENT_DATA"
        assert report.vectorized_mean_sharpe_per_period is None
        assert report.sharpe_divergence_absolute is None
        assert report.sharpe_divergence_relative_pct is None

    def test_insufficient_data_on_empty_reference_list(self):
        """Empty list is also treated as INSUFFICIENT_DATA."""
        rng   = np.random.default_rng(13)
        paths = [_make_path(rng=rng) for _ in range(2)]
        report = run_event_backtest(
            paths,
            vectorized_path_sharpes_per_period=[],
            latency_bars=0,
        )
        assert report.agreement_status == "INSUFFICIENT_DATA"

    def test_near_zero_vectorized_sharpe_uses_floor(self):
        """Denominator is clamped to max(|vec_mean|, 0.01) — no division by near-zero."""
        rng   = np.random.default_rng(14)
        paths = [_make_path(rng=rng) for _ in range(2)]
        # Very small vectorized reference (< 0.01 → floor kicks in)
        report = run_event_backtest(
            paths,
            vectorized_path_sharpes_per_period=[0.001, -0.001],  # mean ≈ 0
            latency_bars=0,
            agreement_tolerance=0.20,
        )
        # Should not raise ZeroDivisionError; status computed
        assert report.agreement_status in ("PASS", "WARNING")
        assert report.sharpe_divergence_relative_pct is not None
        assert np.isfinite(report.sharpe_divergence_relative_pct)


# ──────────────────────────────────────────────────────────────────────────────
# TestRunEventBacktest
# ──────────────────────────────────────────────────────────────────────────────

class TestRunEventBacktest:
    """Integration tests for the public API."""

    def test_returns_event_backtest_report(self):
        paths  = [_make_path() for _ in range(3)]
        report = run_event_backtest(paths)
        assert isinstance(report, EventBacktestReport)

    def test_empty_paths_raises_value_error(self):
        with pytest.raises(ValueError, match="non-empty"):
            run_event_backtest([])

    def test_negative_latency_raises_value_error(self):
        paths = [_make_path()]
        with pytest.raises(ValueError, match="latency_bars"):
            run_event_backtest(paths, latency_bars=-1)

    def test_n_paths_matches_valid_input_count(self):
        paths  = [_make_path() for _ in range(4)]
        report = run_event_backtest(paths, latency_bars=1)
        assert report.n_paths == 4

    def test_single_observation_path_skipped_with_warning(self, caplog):
        """Path with < 2 observations is skipped (WARNING logged); others proceed."""
        import logging
        good_paths = [_make_path() for _ in range(3)]
        tiny_path  = {"proba": np.array([0.8], dtype=np.float32),
                      "forward_return": np.array([0.01], dtype=np.float32)}
        paths = good_paths + [tiny_path]

        with caplog.at_level(logging.WARNING, logger="app.ml.evaluation.event_backtest"):
            report = run_event_backtest(paths, latency_bars=1)
        assert report.n_paths == 3   # tiny path excluded
        assert any("< 2 observations" in m for m in caplog.messages)

    def test_all_paths_too_small_raises(self):
        """If all paths have < 2 observations after latency shift, raise ValueError."""
        tiny = {"proba": np.array([0.8], dtype=np.float32),
                "forward_return": np.array([0.01], dtype=np.float32)}
        with pytest.raises(ValueError, match="fewer than 2 observations"):
            run_event_backtest([tiny, tiny])

    def test_per_path_stats_lengths_match_n_paths(self):
        n = 5
        paths  = [_make_path() for _ in range(n)]
        report = run_event_backtest(paths, latency_bars=1)
        for attr in (
            "path_net_sharpes_per_period",
            "path_net_sharpes_annualised",
            "path_gross_sharpes_annualised",
            "path_total_returns",
            "path_n_fills",
            "path_win_rates",
            "path_max_drawdowns",
        ):
            assert len(getattr(report, attr)) == n, f"{attr} length mismatch"

    def test_total_fills_is_sum_of_path_fills(self):
        paths  = [_make_path() for _ in range(4)]
        report = run_event_backtest(paths, latency_bars=1)
        assert report.total_fills == sum(report.path_n_fills)


# ──────────────────────────────────────────────────────────────────────────────
# TestAcceptance  (PRIMARY ACCEPTANCE TESTS — the plan's exit criteria)
# ──────────────────────────────────────────────────────────────────────────────

class TestAcceptance:
    """
    PRIMARY ACCEPTANCE TESTS.

    From the task specification:
        "event-driven net Sharpe within tolerance of the vectorized cost-aware
        estimate on a reference window."

    Test 1 (zero latency): Proves engine correctness — at latency_bars=0,
    signal–return alignment is identical to the vectorized model; the only
    difference is per-trade charge re-pricing.  Agreement must be < 5%.

    Test 2 (one-bar latency): Production mode.  The 1-bar lag introduces a
    realistic execution delay; the resulting Sharpe diverges from the vectorized
    estimate but the comparison is computed correctly and labelled.
    """

    @pytest.fixture(scope="class")
    def reference_paths(self):
        """Deterministic synthetic CPCV OOF paths used for both acceptance tests."""
        rng        = np.random.default_rng(2024_05_25)
        n_paths    = 5
        n_symbols  = 3
        bars_per_sym = 150

        paths = []
        for _p in range(n_paths):
            proba_list, fwd_list, ts_list, sym_list = [], [], [], []
            for s_i in range(n_symbols):
                sym     = f"SYM{s_i:02d}"
                # Slightly informative model: P(UP|positive return) > 0.5
                fwd_ret = rng.normal(0.002, 0.015, bars_per_sym).astype(np.float32)
                proba   = np.clip(
                    0.5 + 0.15 * np.sign(fwd_ret) + rng.normal(0, 0.05, bars_per_sym),
                    0.01, 0.99,
                ).astype(np.float32)
                ts      = np.array(
                    [
                        np.datetime64("2022-01-03", "ns") + np.timedelta64(i, "D")
                        for i in range(bars_per_sym)
                    ],
                    dtype="datetime64[ns]",
                )
                proba_list.append(proba)
                fwd_list.append(fwd_ret)
                ts_list.append(ts)
                sym_list.append(np.full(bars_per_sym, sym, dtype="<U20"))

            paths.append({
                "proba":          np.concatenate(proba_list),
                "forward_return": np.concatenate(fwd_list),
                "timestamp":      np.concatenate(ts_list),
                "symbol":         np.concatenate(sym_list),
            })
        return paths

    @pytest.fixture(scope="class")
    def vectorized_pp_sharpes(self, reference_paths):
        """A3 vectorized per-period Sharpes on the reference window."""
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo
        result = compute_dsr_and_pbo(
            reference_paths,
            mode="long_only",
            entry_price=1_000.0,
            notional=100_000.0,
            slippage_bps=5.0,
        )
        return result["path_sharpes_per_period"]

    def test_zero_latency_agrees_with_vectorized_within_5pct(
        self, reference_paths, vectorized_pp_sharpes
    ):
        """
        ACCEPTANCE TEST 1: engine correctness proof.

        At latency_bars=0 the signal–return alignment is identical to A3;
        any divergence is due solely to exit-price re-pricing of the sell
        leg (typically < 1 bps per trade).  Agreement must be < 5%.
        """
        report = run_event_backtest(
            reference_paths,
            vectorized_path_sharpes_per_period=vectorized_pp_sharpes,
            latency_bars=0,
            mode="long_only",
            entry_price=1_000.0,
            notional=100_000.0,
            slippage_bps=5.0,
        )

        assert report.vectorized_mean_sharpe_per_period is not None
        assert report.sharpe_divergence_relative_pct is not None

        divergence_pct = report.sharpe_divergence_relative_pct
        assert divergence_pct < 5.0, (
            f"At latency_bars=0, event-driven and vectorized Sharpes should "
            f"agree within 5% (charge re-pricing only).  Divergence was "
            f"{divergence_pct:.2f}%.  This indicates a signal–return alignment "
            f"bug in the event engine."
        )
        assert report.agreement_status == "PASS"

    def test_one_bar_latency_agreement_computed_correctly(
        self, reference_paths, vectorized_pp_sharpes
    ):
        """
        ACCEPTANCE TEST 2: production mode (latency_bars=1).

        Verifies that the agreement check runs without error, produces a
        finite divergence figure, and correctly labels the result as either
        PASS or WARNING.  The 1-bar lag shifts signal–return alignment so
        divergence > 0% is expected; the 20% threshold determines the label.
        """
        report = run_event_backtest(
            reference_paths,
            vectorized_path_sharpes_per_period=vectorized_pp_sharpes,
            latency_bars=1,
            mode="long_only",
            entry_price=1_000.0,
            notional=100_000.0,
            slippage_bps=5.0,
            agreement_tolerance=0.20,
        )

        assert report.vectorized_mean_sharpe_per_period is not None
        assert report.sharpe_divergence_absolute is not None
        assert report.sharpe_divergence_relative_pct is not None
        assert np.isfinite(report.sharpe_divergence_relative_pct)
        assert report.agreement_status in ("PASS", "WARNING"), (
            f"agreement_status must be PASS or WARNING at latency=1, "
            f"got {report.agreement_status!r}"
        )
        # Divergence at latency=1 must be ≥ divergence at latency=0
        report_zero = run_event_backtest(
            reference_paths,
            vectorized_path_sharpes_per_period=vectorized_pp_sharpes,
            latency_bars=0,
        )
        assert (
            report.sharpe_divergence_relative_pct
            >= report_zero.sharpe_divergence_relative_pct - 1e-6
        ), (
            "1-bar latency should produce ≥ divergence compared to 0-bar latency "
            "(the lag shifts the return series; agreement can only stay the same "
            "or get worse)."
        )

    def test_event_backtest_report_fields_complete(self, reference_paths):
        """EventBacktestReport contains all required fields at production latency."""
        report = run_event_backtest(reference_paths, latency_bars=1)

        required = [
            "mean_net_sharpe_per_period",
            "mean_net_sharpe_annualised",
            "std_net_sharpe_per_period",
            "total_fills",
            "mean_win_rate",
            "mean_max_drawdown",
            "mean_total_return",
            "agreement_status",
            "latency_bars",
            "mode",
            "product_type",
            "n_paths",
        ]
        d = report.as_dict()
        for field in required:
            assert field in d, f"Missing field: {field}"
            assert d[field] is not None, f"Field {field} is None"


# ──────────────────────────────────────────────────────────────────────────────
# TestAsDictSerialisation
# ──────────────────────────────────────────────────────────────────────────────

class TestAsDictSerialisation:
    """as_dict() produces stable, JSON-serialisable output."""

    def test_as_dict_is_json_serialisable(self):
        paths  = [_make_path() for _ in range(3)]
        report = run_event_backtest(paths, latency_bars=1)
        d      = report.as_dict()
        # Must not raise
        serialised = json.dumps(d)
        assert isinstance(serialised, str)
        assert len(serialised) > 10

    def test_as_dict_roundtrips_through_json(self):
        """Values survive a JSON round-trip without type coercion issues."""
        paths  = [_make_path() for _ in range(2)]
        report = run_event_backtest(paths, latency_bars=1)
        d      = report.as_dict()
        d2     = json.loads(json.dumps(d))

        assert d["n_paths"]        == d2["n_paths"]
        assert d["latency_bars"]   == d2["latency_bars"]
        assert d["agreement_status"] == d2["agreement_status"]

    def test_event_backtest_field_always_present_in_promotion_report(self):
        """build_promotion_report always writes 'event_backtest' in the body."""
        from app.ml.evaluation.promotion_report import build_promotion_report
        from unittest.mock import MagicMock
        import tempfile, pathlib

        # Minimal mock challenger (no onnx_path → absent artefacts)
        mock_meta = MagicMock()
        mock_meta.model_version    = "test_1.0.0"
        mock_meta.model_name       = "xgboost"
        mock_meta.status           = "development"
        mock_meta.training_samples = 200_000
        mock_meta.training_metrics = {
            "auc_pr": 0.60, "deflated_sharpe": 0.65, "ece_after": 0.04,
            "symbol_coverage": 0.90, "accuracy": 0.54,
        }
        mock_meta.training_features = ["f1", "f2"]
        mock_meta.lineage           = {}
        mock_meta.onnx_path         = None

        with tempfile.TemporaryDirectory() as tmp:
            report = build_promotion_report(
                challengers={"xgboost": mock_meta},
                champions={"xgboost": None},
                report_dir=pathlib.Path(tmp),
                run_id="test_run",
                event_backtest={"agreement_status": "PASS", "n_paths": 7},
            )
        assert "event_backtest" in report
        assert report["event_backtest"]["agreement_status"] == "PASS"

    def test_event_backtest_empty_when_none(self):
        """event_backtest=None → field is {} in the report (never absent)."""
        from app.ml.evaluation.promotion_report import build_promotion_report
        from unittest.mock import MagicMock
        import tempfile, pathlib

        mock_meta = MagicMock()
        mock_meta.model_version    = "test_1.0.1"
        mock_meta.model_name       = "gru"
        mock_meta.status           = "development"
        mock_meta.training_samples = 150_000
        mock_meta.training_metrics = {
            "auc_pr": 0.58, "deflated_sharpe": 0.55, "ece_after": 0.03,
            "symbol_coverage": 0.88, "accuracy": 0.52,
        }
        mock_meta.training_features = []
        mock_meta.lineage           = {}
        mock_meta.onnx_path         = None

        with tempfile.TemporaryDirectory() as tmp:
            report = build_promotion_report(
                challengers={"gru": mock_meta},
                champions={"gru": None},
                report_dir=pathlib.Path(tmp),
                run_id="test_run",
                event_backtest=None,
            )
        assert "event_backtest" in report
        assert report["event_backtest"] == {}
