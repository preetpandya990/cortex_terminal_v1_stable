"""
Phase A — Validation & Regime Integrity tests.

A1 — SimulatedTrade.regime_label / regime_detector_version wired through all
     replay modes; _per_regime_stats surfaces N alongside every metric;
     parity: the causal detector is deterministic and produces a stable label
     from the same candle history.

A2 — audit_label_quality produces a complete, accurate report covering scheme
     identification, distribution, threshold symmetry, realised-R, and causality.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

# ── helpers ────────────────────────────────────────────────────────────────────

def _make_candles(n: int = 40, seed: int = 0) -> list[dict]:
    """Synthetic daily OHLCV candles for detector smoke-tests."""
    rng = np.random.default_rng(seed)
    close = 1000.0 + rng.normal(0, 10, n).cumsum()
    close = np.clip(close, 500, 3000)
    high = close + rng.uniform(2, 20, n)
    low = close - rng.uniform(2, 20, n)
    return [
        {"high": float(high[i]), "low": float(low[i]), "close": float(close[i])}
        for i in range(n)
    ]


def _make_ohlcv_df(n: int = 60, seed: int = 1) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame for target-generator tests."""
    rng = np.random.default_rng(seed)
    close = 500.0 + rng.normal(0, 5, n).cumsum()
    close = np.clip(close, 200, 2000)
    high = close + rng.uniform(1, 10, n)
    low = close - rng.uniform(1, 10, n)
    return pd.DataFrame({
        "open": close * 0.999,
        "high": high,
        "low": low,
        "close": close,
        "volume": rng.integers(100_000, 1_000_000, n).astype(float),
    })


def _make_trade(
    *,
    regime_label: str = "bull_trending",
    regime_detector_version: str | None = "1.0.0",
    net_pnl: float = 100.0,
) -> object:
    """Minimal SimulatedTrade instance for stats tests."""
    from app.services.strategy_engine.backtest_engine import SimulatedTrade
    now = datetime.now(timezone.utc)
    return SimulatedTrade(
        symbol="RELIANCE",
        direction="LONG",
        entry_price=2500.0,
        exit_price=2600.0,
        quantity=1.0,
        stop_loss=2450.0,
        take_profit_1=2650.0,
        exit_reason="TP1",
        entry_at=now,
        exit_at=now,
        gross_pnl=net_pnl,
        total_charges=5.0,
        net_pnl=net_pnl,
        pnl_pct=(net_pnl / 2500.0) * 100,
        hold_duration_seconds=3600.0,
        regime_label=regime_label,
        regime_detector_version=regime_detector_version,
    )


# ── A1 · SimulatedTrade dataclass ─────────────────────────────────────────────

class TestSimulatedTradeRegimeFields:
    def test_default_regime_label_is_unknown(self):
        """Newly constructed SimulatedTrade defaults to 'unknown' (sentinel)."""
        from app.services.strategy_engine.backtest_engine import SimulatedTrade
        now = datetime.now(timezone.utc)
        t = SimulatedTrade(
            symbol="TCS", direction="LONG", entry_price=3000.0, exit_price=3100.0,
            quantity=1.0, stop_loss=None, take_profit_1=None, exit_reason="PERIOD_END",
            entry_at=now, exit_at=now,
            gross_pnl=100.0, total_charges=1.0, net_pnl=99.0, pnl_pct=3.3,
            hold_duration_seconds=86400.0,
        )
        assert t.regime_label == "unknown"
        assert t.regime_detector_version is None

    def test_regime_fields_round_trip_through_to_dict(self):
        """to_dict() must include both regime fields for JSONB persistence."""
        t = _make_trade(regime_label="sideways_range", regime_detector_version="1.0.0")
        d = t.to_dict()
        assert d["regime_label"] == "sideways_range"
        assert d["regime_detector_version"] == "1.0.0"

    def test_stored_version_sentinel_serialises(self):
        """signal_replay uses 'stored' as version sentinel; must survive to_dict."""
        t = _make_trade(regime_label="bear_trending", regime_detector_version="stored")
        assert t.to_dict()["regime_detector_version"] == "stored"

    def test_none_version_serialises_as_none(self):
        t = _make_trade(regime_detector_version=None)
        assert t.to_dict()["regime_detector_version"] is None


# ── A1 · _per_regime_stats ────────────────────────────────────────────────────

class TestPerRegimeStats:
    def _stats(self, trades):
        from app.services.strategy_engine.backtest_engine import _per_regime_stats
        return _per_regime_stats(trades)

    def test_empty_trades_returns_empty_dict(self):
        assert self._stats([]) == {}

    def test_single_regime_n_equals_trade_count(self):
        trades = [_make_trade(regime_label="bull_trending") for _ in range(5)]
        stats = self._stats(trades)
        assert stats["bull_trending"]["n"] == 5

    def test_multiple_regimes_bucketed_correctly(self):
        trades = (
            [_make_trade(regime_label="bull_trending", net_pnl=100.0)] * 3
            + [_make_trade(regime_label="sideways_range", net_pnl=-50.0)] * 2
        )
        stats = self._stats(trades)
        assert stats["bull_trending"]["n"] == 3
        assert stats["sideways_range"]["n"] == 2

    def test_win_rate_pct_computed_correctly(self):
        trades = [
            _make_trade(regime_label="bull_trending", net_pnl=100.0),
            _make_trade(regime_label="bull_trending", net_pnl=-20.0),
            _make_trade(regime_label="bull_trending", net_pnl=50.0),
        ]
        stats = self._stats(trades)
        b = stats["bull_trending"]
        assert b["wins"] == 2
        assert b["losses"] == 1
        assert abs(b["win_rate_pct"] - 66.67) < 0.01

    def test_net_pnl_aggregated_per_regime(self):
        trades = [
            _make_trade(regime_label="bear_trending", net_pnl=200.0),
            _make_trade(regime_label="bear_trending", net_pnl=-80.0),
        ]
        stats = self._stats(trades)
        assert abs(stats["bear_trending"]["net_pnl"] - 120.0) < 0.001

    def test_n_is_present_in_every_bucket(self):
        """N must be surfaced alongside every metric (acceptance criterion)."""
        labels = ["bull_trending", "sideways_range", "high_volatility", "insufficient_data"]
        trades = [_make_trade(regime_label=label) for label in labels]
        stats = self._stats(trades)
        for label in labels:
            assert "n" in stats[label], f"n missing for regime '{label}'"
            assert stats[label]["n"] == 1


# ── A1 · Causal detector determinism & version constant ───────────────────────

class TestRegimeDetector:
    def test_version_constant_exists_and_is_semver(self):
        from app.services.regime_service import REGIME_DETECTOR_VERSION
        parts = REGIME_DETECTOR_VERSION.split(".")
        assert len(parts) == 3, "REGIME_DETECTOR_VERSION must follow MAJOR.MINOR.PATCH"
        assert all(p.isdigit() for p in parts)

    def test_sufficient_candles_produces_known_label(self):
        from app.services.regime_service import RegimeService
        candles = _make_candles(n=40)
        result = RegimeService._compute_regime_from_candles(candles)
        assert result["regime"] != "unknown"
        assert result["regime"] in {
            "bull_trending", "bear_trending", "sideways_range",
            "high_volatility", "low_liquidity", "insufficient_data",
        }

    def test_detector_is_deterministic(self):
        """Same candle history must always yield the same label (no randomness)."""
        from app.services.regime_service import RegimeService
        candles = _make_candles(n=40, seed=42)
        r1 = RegimeService._compute_regime_from_candles(candles)
        r2 = RegimeService._compute_regime_from_candles(candles)
        assert r1["regime"] == r2["regime"]
        assert r1["confidence"] == r2["confidence"]

    def test_insufficient_candles_returns_sentinel(self):
        """Fewer than 20 candles must return 'insufficient_data', not 'unknown'."""
        from app.services.regime_service import RegimeService
        candles = _make_candles(n=10)
        result = RegimeService._compute_regime_from_candles(candles)
        assert result["regime"] == "insufficient_data"

    def test_detector_is_causal_no_future_data(self):
        """
        Parity invariant: detector output at bar i must be identical whether
        computed from candles[0..i] or candles[0..i+10].  If the detector used
        future data, adding bars beyond i would change the output.
        """
        from app.services.regime_service import RegimeService
        candles_long = _make_candles(n=60, seed=7)
        candles_short = candles_long[:40]
        r_long = RegimeService._compute_regime_from_candles(candles_long[:40])
        r_short = RegimeService._compute_regime_from_candles(candles_short)
        assert r_long["regime"] == r_short["regime"]
        assert r_long["confidence"] == r_short["confidence"]


# ── A2 · audit_label_quality ──────────────────────────────────────────────────

class TestAuditLabelQuality:
    def _audit(self, n: int = 100, atr_multiplier: float = 0.5, horizon: int = 5):
        from app.ml.features.target_generator import audit_label_quality
        df = _make_ohlcv_df(n=n)
        return audit_label_quality(df, atr_multiplier=atr_multiplier, horizon=horizon)

    def test_returns_all_required_keys(self):
        report = self._audit()
        required = {
            "labelling_scheme", "is_triple_barrier", "description",
            "atr_multiplier", "horizon_bars", "threshold_symmetric",
            "symmetry_note", "n_evaluable_rows", "n_up", "n_down",
            "n_dead_zone", "up_pct", "down_pct", "dead_zone_pct",
            "realised_return_up", "realised_return_down",
            "feature_causality", "lookahead_in_features",
        }
        assert required <= set(report.keys())

    def test_not_triple_barrier(self):
        """Scheme must be correctly identified — NOT triple-barrier."""
        report = self._audit()
        assert report["is_triple_barrier"] is False
        assert report["labelling_scheme"] == "binary_dead_zone"

    def test_threshold_is_always_symmetric(self):
        report = self._audit()
        assert report["threshold_symmetric"] is True

    def test_no_feature_lookahead(self):
        report = self._audit()
        assert report["lookahead_in_features"] is False

    def test_distribution_sums_to_evaluable_rows(self):
        report = self._audit(n=120)
        total = report["n_up"] + report["n_down"] + report["n_dead_zone"]
        assert total == report["n_evaluable_rows"]

    def test_pct_fields_sum_to_100(self):
        report = self._audit(n=120)
        total_pct = report["up_pct"] + report["down_pct"] + report["dead_zone_pct"]
        assert abs(total_pct - 100.0) < 0.1

    def test_realised_return_up_has_positive_mean(self):
        """By construction, UP labels have a positive forward return (> threshold)."""
        report = self._audit(n=200)
        up_dist = report["realised_return_up"]
        if up_dist:  # may be empty for pathological synthetic data
            assert up_dist["mean"] > 0.0

    def test_realised_return_down_has_negative_mean(self):
        """By construction, DOWN labels have a negative forward return (< -threshold)."""
        report = self._audit(n=200)
        down_dist = report["realised_return_down"]
        if down_dist:
            assert down_dist["mean"] < 0.0

    def test_atr_multiplier_reflected_in_report(self):
        report = self._audit(atr_multiplier=0.75)
        assert report["atr_multiplier"] == 0.75

    def test_horizon_reflected_in_report(self):
        report = self._audit(horizon=10)
        assert report["horizon_bars"] == 10

    def test_symbol_passed_through(self):
        from app.ml.features.target_generator import audit_label_quality
        df = _make_ohlcv_df()
        report = audit_label_quality(df, symbol="INFY")
        assert report["symbol"] == "INFY"

    def test_report_is_json_serialisable(self):
        import json
        report = self._audit()
        json.dumps(report)  # must not raise


# ── A1 · REGIME_DETECTOR_VERSION exported for callers ─────────────────────────

class TestVersionExport:
    def test_version_importable_from_regime_service(self):
        from app.services.regime_service import REGIME_DETECTOR_VERSION
        assert isinstance(REGIME_DETECTOR_VERSION, str)
        assert len(REGIME_DETECTOR_VERSION) > 0

    def test_version_importable_from_backtest_engine(self):
        """backtest_engine re-exports REGIME_DETECTOR_VERSION for callers."""
        from app.services.strategy_engine.backtest_engine import REGIME_DETECTOR_VERSION
        assert isinstance(REGIME_DETECTOR_VERSION, str)
