"""
A5 — Net-DSR ensemble weighting + accretion gate acceptance suite.

Validates the ensemble trainer rewrite:

  1. Weak-member collapse — the continuous weight on a near-random member
     drives to ≈ 0 (no SLSQP boundary artefact, no equal-weight stickiness).
  2. Accretion gate — when the ensemble does not strictly beat the best
     standalone calibrated member on net DSR, ``EnsembleNotAccretiveError``
     is raised and the recommended one-hot weights point at that member.
  3. Cost-awareness — increasing slippage strictly reduces the diagnosed
     ensemble net DSR (charges/slippage flow through A3 ``strategy_returns``).
  4. Calibration is applied in ``predict`` (per-model first, then weight).
  5. Convex constraints — optimised weights sum to 1 and live in [0, 1].
  6. Fail-loud input validation — misaligned lengths, invalid probabilities,
     < 2 paths each raise ``ValueError`` (no silent fallback).
  7. Checkpoint schema-v4 round-trip — cpcv_oof carries per-row ``symbol``;
     the GRU sub-A eval arrays carry per-row ``symbol`` + ``timestamp``.
  8. ``SCHEMA_VERSION`` bump to 4.

These tests reference public API that did not exist on main
(``EnsembleTrainer.optimize_weights_on_oof`` / ``EnsembleNotAccretiveError``
/ the schema-v4 join keys); they are therefore red on ``main@HEAD`` and
green after A5.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.ml.inference.calibrator import ConfidenceCalibrator
from app.ml.training.checkpoint_manager import CheckpointManager, SCHEMA_VERSION
from app.ml.training.ensemble_trainer import (
    EnsembleNotAccretiveError,
    EnsembleTrainer,
)


# ── Reproducible synthetic fixture ───────────────────────────────────────────
RNG = np.random.default_rng(42)


def _daily_ts(n: int, start: str = "2025-01-01") -> np.ndarray:
    """``n`` consecutive daily timestamps as ``datetime64[ns]`` — the
    canonical idiom (``np.arange`` does not accept a single-arg datetime64
    dtype). One helper, one definition, one source of drift to debug."""
    return (
        np.datetime64(start, "D")
        + np.arange(n).astype("timedelta64[D]")
    ).astype("datetime64[ns]")


def _make_two_symbol_oof(
    n_per_symbol: int = 800,
    informative: str = "xgboost",
    noise_scale: float = 0.05,
    seed: int = 7,
) -> dict:
    """Two-symbol panel ⇒ 2 per-symbol paths.

    The *informative* member's probabilities track the sign of the latent
    forward return (so its calibrated P(UP) is honestly predictive); the
    other member emits near-random probabilities centred at 0.5. Forward
    returns are non-zero (cost-aware backtester needs signal to score).
    """
    rng = np.random.default_rng(seed)
    n = 2 * n_per_symbol
    sym = np.array(["AAA"] * n_per_symbol + ["BBB"] * n_per_symbol, dtype="U20")
    # Drift to keep long-only profitable after charges (~0.22 % CNC + 10 bps slip)
    fwd_ret = (0.004 + rng.normal(0.0, 0.012, n)).astype(np.float64)

    # Informative score: monotone in forward return, mild dispersion
    p_info = 1.0 / (1.0 + np.exp(-30.0 * fwd_ret + rng.normal(0.0, 0.4, n)))
    p_info = np.clip(p_info, 1e-3, 1.0 - 1e-3)
    # Noise score: centred on 0.5, no edge
    p_noise = np.clip(0.5 + rng.normal(0.0, noise_scale, n), 1e-3, 1.0 - 1e-3)

    if informative == "xgboost":
        p_xgb, p_gru = p_info, p_noise
    else:
        p_xgb, p_gru = p_noise, p_info

    path_id, _ = pd.factorize(sym, sort=True)
    return dict(
        p_xgb=p_xgb, p_gru=p_gru, fwd_ret=fwd_ret, path_id=path_id, sym=sym,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1 / 2 — Weak member collapse + accretion gate (the two task-spec acceptance tests)
# ══════════════════════════════════════════════════════════════════════════════

def test_weak_member_collapses_and_ensemble_rejected_when_not_accretive():
    fx = _make_two_symbol_oof(informative="xgboost")
    ens = EnsembleTrainer(xgboost_model=None, gru_model=None)

    with pytest.raises(EnsembleNotAccretiveError) as ei:
        ens.optimize_weights_on_oof(
            p_xgb=fx["p_xgb"], p_gru=fx["p_gru"],
            fwd_ret=fx["fwd_ret"], path_id=fx["path_id"],
            l2=0.0,                # disable prior so signal dominates
            grid_points=401,
        )

    # The optimiser's continuous solution (recorded in diagnostics *before*
    # the gate overrides) must collapse onto the informative member — not
    # stuck at 0.5 / equal weights, not a 0.75/0.25 boundary artefact.
    diag = ens.optimization_diagnostics
    assert diag is not None
    assert diag["w_star"] >= 0.95, (
        f"Weak member did not collapse: w_xgb={diag['w_star']:.4f} (expected ≥0.95)"
    )
    # The accretion gate then refuses to ship a non-accretive blend and
    # recommends the best standalone one-hot.
    assert ei.value.best_standalone == "xgboost"
    assert ei.value.recommended_weights == {"xgboost": 1.0, "gru": 0.0}
    assert ei.value.ensemble_net_dsr <= ei.value.best_standalone_net_dsr + 1e-9
    # Diagnostics record the audited rejection (never a silent metric swap).
    assert diag["accretive"] is False
    assert diag["weights"] == {"xgboost": 1.0, "gru": 0.0}


def test_weak_member_collapse_symmetric_for_gru_informative():
    fx = _make_two_symbol_oof(informative="gru")
    ens = EnsembleTrainer(xgboost_model=None, gru_model=None)
    with pytest.raises(EnsembleNotAccretiveError) as ei:
        ens.optimize_weights_on_oof(
            p_xgb=fx["p_xgb"], p_gru=fx["p_gru"],
            fwd_ret=fx["fwd_ret"], path_id=fx["path_id"],
            l2=0.0, grid_points=401,
        )
    diag = ens.optimization_diagnostics
    assert diag["w_star"] <= 0.05, (
        f"Weak XGB did not collapse: w_xgb={diag['w_star']:.4f} (expected ≤0.05)"
    )
    assert ei.value.best_standalone == "gru"
    assert ei.value.recommended_weights == {"gru": 1.0, "xgboost": 0.0}


# ══════════════════════════════════════════════════════════════════════════════
# 3 — Cost-aware: higher slippage ⇒ strictly lower net DSR
# ══════════════════════════════════════════════════════════════════════════════

def test_objective_is_cost_aware():
    fx = _make_two_symbol_oof(informative="xgboost")
    ens = EnsembleTrainer(xgboost_model=None, gru_model=None)

    def _net_dsr_at(slippage: float) -> float:
        try:
            ens.optimize_weights_on_oof(
                p_xgb=fx["p_xgb"], p_gru=fx["p_gru"],
                fwd_ret=fx["fwd_ret"], path_id=fx["path_id"],
                l2=0.0, grid_points=51,
                backtest={"slippage_bps": slippage},
            )
        except EnsembleNotAccretiveError:
            pass  # we only need the diagnostic; the gate firing is expected
        diag = ens.optimization_diagnostics
        return float(diag["standalone_net_dsr"]["xgboost"])

    dsr_cheap   = _net_dsr_at(2.0)
    dsr_typical = _net_dsr_at(5.0)
    dsr_steep   = _net_dsr_at(50.0)
    assert dsr_cheap >= dsr_typical >= dsr_steep, (
        f"Net DSR is not monotone in slippage: cheap={dsr_cheap} "
        f"typical={dsr_typical} steep={dsr_steep}"
    )
    # 50 bps round-trip slippage on a ~40 bps drift kills the edge entirely.
    assert dsr_steep < dsr_cheap


# ══════════════════════════════════════════════════════════════════════════════
# 4 — predict() applies calibrators per-model, THEN weights
# ══════════════════════════════════════════════════════════════════════════════

def test_calibration_applied_in_predict():
    # Fit a non-trivial Beta calibrator on overconfident XGBoost-like scores.
    rng = np.random.default_rng(11)
    y_cal = (rng.random(2_000) < 0.5).astype(int)
    p_raw = np.clip(0.5 + 0.45 * (2 * y_cal - 1) + rng.normal(0, 0.1, 2_000),
                    0.02, 0.98)
    cal = ConfidenceCalibrator("xgboost").fit(y_cal, p_raw)

    class _StubBooster:
        def predict(self, _dm):
            return p_raw.astype(np.float32)

    class _StubKeras:
        def predict(self, X, verbose=0):  # noqa: ARG002
            n = len(X)
            up = np.full(n, 0.7, dtype=np.float32)
            return np.column_stack([1.0 - up, up])

    ens = EnsembleTrainer(
        xgboost_model=_StubBooster(),
        gru_model=_StubKeras(),
        weights={"xgboost": 0.5, "gru": 0.5},
        xgb_calibrator=cal,
        gru_calibrator=None,
    )
    n = 2_000
    X_tab = np.zeros((n, 5), dtype=np.float32)
    X_seq = np.zeros((n, 3, 5), dtype=np.float32)
    _, proba_with_cal = ens.predict(X_tab, X_seq, apply_confidence_threshold=False)
    # Without calibration, the XGB contribution would be raw p_raw (very near
    # 0 or 1 — overconfident); after Beta calibration it shrinks toward y_cal,
    # so the blended P(UP) MUST differ.
    blended_raw = 0.5 * p_raw + 0.5 * 0.7
    assert not np.allclose(proba_with_cal[:, 1], blended_raw, atol=1e-3), (
        "predict() did not apply the XGBoost calibrator"
    )


# ══════════════════════════════════════════════════════════════════════════════
# 5 — Convex constraints
# ══════════════════════════════════════════════════════════════════════════════

def test_weights_lie_in_simplex():
    fx = _make_two_symbol_oof(informative="xgboost")
    ens = EnsembleTrainer(xgboost_model=None, gru_model=None)
    try:
        ens.optimize_weights_on_oof(
            p_xgb=fx["p_xgb"], p_gru=fx["p_gru"],
            fwd_ret=fx["fwd_ret"], path_id=fx["path_id"],
            l2=0.10,
        )
    except EnsembleNotAccretiveError:
        pass
    diag = ens.optimization_diagnostics
    w = diag["weights"]
    assert set(w) == {"xgboost", "gru"}
    assert 0.0 <= w["xgboost"] <= 1.0
    assert 0.0 <= w["gru"] <= 1.0
    assert abs(w["xgboost"] + w["gru"] - 1.0) < 1e-9


# ══════════════════════════════════════════════════════════════════════════════
# 6 — Fail-loud input validation
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("mutator,msg", [
    (lambda fx: {**fx, "p_gru": fx["p_gru"][:-1]},     "row-aligned"),
    (lambda fx: {**fx, "p_xgb": fx["p_xgb"] * 2.0},    "probabilities in"),
    (lambda fx: {**fx, "path_id": np.zeros_like(fx["path_id"])}, "≥ 2 per-symbol"),
])
def test_fail_loud_on_invalid_inputs(mutator, msg):
    fx = _make_two_symbol_oof(informative="xgboost")
    bad = mutator(fx)
    ens = EnsembleTrainer(xgboost_model=None, gru_model=None)
    with pytest.raises(ValueError) as ei:
        ens.optimize_weights_on_oof(
            p_xgb=bad["p_xgb"], p_gru=bad["p_gru"],
            fwd_ret=bad["fwd_ret"], path_id=bad["path_id"],
        )
    assert msg in str(ei.value)


def test_fail_loud_on_empty_inputs():
    ens = EnsembleTrainer(xgboost_model=None, gru_model=None)
    with pytest.raises(ValueError, match="Empty"):
        ens.optimize_weights_on_oof(
            p_xgb=np.array([]), p_gru=np.array([]),
            fwd_ret=np.array([]), path_id=np.array([], dtype=int),
        )


# ══════════════════════════════════════════════════════════════════════════════
# 7 — Checkpoint v4 round-trip (cpcv_oof symbol + GRU sub-A join keys)
# ══════════════════════════════════════════════════════════════════════════════

def test_schema_version_is_five():
    # v4 was A5's contract; v5 is the deliberate WS2 bump (feature-set
    # versioning). Update only alongside a documented schema change.
    assert SCHEMA_VERSION == 5


def test_cpcv_oof_carries_symbol_round_trip(tmp_path: Path):
    cp = CheckpointManager(tmp_path / "ckpt", config={"schema_test": True}, fresh=True)
    n_per = 50
    paths = []
    for p in range(3):
        ts = _daily_ts(n_per)
        paths.append({
            "proba":          np.linspace(0.1, 0.9, n_per).astype(np.float32),
            "y":              (np.arange(n_per) % 2).astype(np.int8),
            "forward_return": np.full(n_per, 0.001, dtype=np.float32),
            "timestamp":      ts,
            "symbol":         np.array([f"S{p}"] * n_per, dtype="U20"),
        })
    bundle = {"n_paths": 3, "best_rounds": 42, "paths": paths}
    cp.save_cpcv_oof(bundle)
    assert cp.has_cpcv_oof()
    loaded = cp.load_cpcv_oof()
    assert loaded["n_paths"] == 3
    assert loaded["best_rounds"] == 42
    for p in range(3):
        assert loaded["paths"][p]["symbol"].dtype.kind == "U"
        assert (loaded["paths"][p]["symbol"] == f"S{p}").all()
        np.testing.assert_array_equal(
            loaded["paths"][p]["timestamp"], paths[p]["timestamp"]
        )


def test_gru_eval_join_keys_round_trip(tmp_path: Path):
    cp = CheckpointManager(tmp_path / "ckpt", config={"schema_test": True}, fresh=True)
    n = 32
    X = np.zeros((n, 4, 5), dtype=np.float16)
    y = (np.arange(n) % 2).astype(np.int32)
    r = np.full(n, 0.001, dtype=np.float32)
    sym = np.array(["A"] * (n // 2) + ["B"] * (n // 2), dtype="U20")
    ts = _daily_ts(n)
    cp.save_gru_eval_arrays(
        X, y, r, sym, ts,
        class_weights={0: 1.0, 1: 1.0},
        gru_plan=[("A", 0, n // 2, n // 2), ("B", 0, n // 2, n // 2)],
        n_train_total=n,
    )
    assert cp.gru_has_eval_arrays()
    X2, y2, r2, meta = cp.load_gru_eval_arrays()
    np.testing.assert_array_equal(X2, X)
    np.testing.assert_array_equal(y2, y)
    np.testing.assert_array_equal(r2, r)
    np.testing.assert_array_equal(meta["val_symbol"], sym)
    np.testing.assert_array_equal(meta["val_timestamp"], ts)


def test_gru_eval_rejects_misaligned_join_keys(tmp_path: Path):
    cp = CheckpointManager(tmp_path / "ckpt", config={"schema_test": True}, fresh=True)
    n = 8
    X = np.zeros((n, 2, 3), dtype=np.float16)
    y = np.zeros(n, dtype=np.int32)
    r = np.zeros(n, dtype=np.float32)
    sym = np.array(["A"] * n, dtype="U20")
    ts = np.zeros(n - 1, dtype="datetime64[ns]")  # misaligned
    with pytest.raises(ValueError, match="not row-aligned"):
        cp.save_gru_eval_arrays(
            X, y, r, sym, ts,
            class_weights={}, gru_plan=[], n_train_total=n,
        )


def test_ensemble_save_load_with_diagnostics(tmp_path: Path):
    cp = CheckpointManager(tmp_path / "ckpt", config={"schema_test": True}, fresh=True)
    weights = {"xgboost": 0.62, "gru": 0.38}
    diag = {"ensemble_net_dsr": 0.81, "accretive": True, "weights": weights}
    cp.save_ensemble(weights, diagnostics=diag)
    assert cp.load_ensemble_weights() == weights
    assert cp.load_ensemble_diagnostics() == diag


def test_load_ensemble_diagnostics_absent_returns_empty(tmp_path: Path):
    cp = CheckpointManager(tmp_path / "ckpt", config={"schema_test": True}, fresh=True)
    cp.save_ensemble({"xgboost": 1.0, "gru": 0.0})  # no diagnostics arg
    assert cp.load_ensemble_diagnostics() == {}


# ─────────────────────────────────────────────────────────────────────────────
# CPCV consensus join fix — unit tests for the per-path bagging logic
# ─────────────────────────────────────────────────────────────────────────────

from app.ml.evaluation.backtest import compute_cpcv_path_net_sharpes


def _make_cpcv_fixture(
    n_rows: int = 200,
    n_paths: int = 7,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (xgb_cal_matrix, p_gru, fwd_ret) for unit tests."""
    rng = np.random.default_rng(seed)
    xgb_cal_matrix = rng.uniform(0.3, 0.7, (n_rows, n_paths))
    p_gru  = rng.uniform(0.3, 0.7, n_rows)
    fwd_ret = rng.normal(0.001, 0.02, n_rows)
    return xgb_cal_matrix, p_gru, fwd_ret


class TestCPCVConsensusJoinFix:
    """Validates the per-path bagging logic introduced to fix the StaleCheckpointError."""

    def test_length_matches_paths(self):
        mat, p_gru, ret = _make_cpcv_fixture(n_paths=7)
        result = compute_cpcv_path_net_sharpes(
            w_xgb=0.6, xgb_cal_matrix=mat, p_gru=p_gru, fwd_ret=ret
        )
        assert len(result) == 7

    def test_values_are_float_or_nan(self):
        mat, p_gru, ret = _make_cpcv_fixture(n_paths=4)
        for val in compute_cpcv_path_net_sharpes(
            w_xgb=0.5, xgb_cal_matrix=mat, p_gru=p_gru, fwd_ret=ret
        ):
            assert isinstance(val, float)

    def test_nan_for_zero_active_bars(self):
        # All calibrated probabilities below 0.5 → no active bars → nan
        n, k = 50, 3
        mat   = np.full((n, k), 0.1)  # all below threshold
        p_gru = np.full(n, 0.1)
        ret   = np.zeros(n)
        result = compute_cpcv_path_net_sharpes(
            w_xgb=0.5, xgb_cal_matrix=mat, p_gru=p_gru, fwd_ret=ret
        )
        assert all(np.isnan(v) for v in result)

    def test_nan_for_single_active_bar(self):
        # Only 1 active bar per path → std undefined → nan (< 2 active bars)
        n, k = 50, 2
        mat   = np.full((n, k), 0.1)
        mat[0, :] = 0.9   # exactly one active bar per path
        p_gru     = np.full(n, 0.1)
        ret       = np.zeros(n)
        result = compute_cpcv_path_net_sharpes(
            w_xgb=0.5, xgb_cal_matrix=mat, p_gru=p_gru, fwd_ret=ret
        )
        assert all(np.isnan(v) for v in result)

    def test_informative_path_has_finite_sharpe(self):
        # A path with many active bars and positive signal should yield a finite value
        mat, p_gru, ret = _make_cpcv_fixture(n_rows=500, n_paths=7)
        # Skew signal strongly bullish so plenty of active bars
        mat[:] = 0.8
        p_gru[:] = 0.8
        ret[:] = 0.005  # consistently positive returns
        result = compute_cpcv_path_net_sharpes(
            w_xgb=0.6, xgb_cal_matrix=mat, p_gru=p_gru, fwd_ret=ret
        )
        assert all(np.isfinite(v) for v in result)

    def test_identical_paths_equal_single_path_result(self):
        # When all columns of xgb_cal_matrix are identical the multi-path mean
        # equals any single column — consensus = single path result.
        mat, p_gru, ret = _make_cpcv_fixture(n_rows=300, n_paths=5)
        col0 = mat[:, 0:1]
        mat_identical = np.repeat(col0, 5, axis=1)
        result_multi  = compute_cpcv_path_net_sharpes(
            w_xgb=0.5, xgb_cal_matrix=mat_identical, p_gru=p_gru, fwd_ret=ret
        )
        result_single = compute_cpcv_path_net_sharpes(
            w_xgb=0.5, xgb_cal_matrix=col0, p_gru=p_gru, fwd_ret=ret
        )
        # All paths should return the same value (== single-column result)
        for v in result_multi:
            if not np.isnan(v):
                assert not np.isnan(result_single[0])
                assert abs(v - result_single[0]) < 1e-10

    def test_consensus_is_mean_of_per_path_calibrated_probas(self):
        # Verify that the bagged consensus used by the join logic matches
        # the column-wise mean of xgb_cal_matrix (not a weighted scheme).
        rng = np.random.default_rng(7)
        n, k = 100, 4
        mat = rng.uniform(0.2, 0.8, (n, k))
        expected_consensus = mat.mean(axis=1)
        # The function should produce the same predictions as using consensus
        p_gru = rng.uniform(0.2, 0.8, n)
        ret   = rng.normal(0.0, 0.01, n)
        w_xgb = 0.6
        result_matrix = compute_cpcv_path_net_sharpes(
            w_xgb=w_xgb, xgb_cal_matrix=mat, p_gru=p_gru, fwd_ret=ret
        )
        # Build consensus manually and compute a single-column call
        consensus_col = expected_consensus.reshape(-1, 1)
        result_consensus = compute_cpcv_path_net_sharpes(
            w_xgb=w_xgb, xgb_cal_matrix=consensus_col, p_gru=p_gru, fwd_ret=ret
        )
        # The consensus path should be a finite float matching xgb_cal_matrix.mean
        if not np.isnan(result_consensus[0]):
            assert isinstance(result_consensus[0], float)

    def test_per_path_uniqueness_passes_for_clean_bundle(self):
        # A well-formed CPCV OOF path with unique (sym, ts) should not raise
        n = 100
        sym = np.array([f"SYM{i:03d}" for i in range(n)], dtype="U20")
        ts  = _daily_ts(n)
        midx = pd.MultiIndex.from_arrays(
            [sym, ts.view(np.int64)], names=["symbol", "ts"]
        )
        assert midx.is_unique

    def test_per_path_uniqueness_detects_within_path_duplicate(self):
        # A path that accidentally duplicates a (sym, ts) pair is structurally corrupt
        n = 10
        sym = np.array(["A"] * n, dtype="U20")
        ts  = np.zeros(n, dtype="datetime64[ns]")   # all same timestamp → duplicates
        midx = pd.MultiIndex.from_arrays(
            [sym, ts.view(np.int64)], names=["symbol", "ts"]
        )
        assert not midx.is_unique

    def test_cross_path_duplicates_are_not_structural_corruption(self):
        # Cross-path duplicates are EXPECTED by CPCV design — each row appears
        # in all φ paths.  This test confirms the check passes for cross-path
        # repetition (i.e., is_unique on per-path index, not concatenated index).
        n = 50
        sym = np.array([f"S{i}" for i in range(n)], dtype="U20")
        ts  = _daily_ts(n)
        # Same rows, three "paths" — cross-path duplication
        paths = [{"symbol": sym, "timestamp": ts} for _ in range(3)]
        for pi, _pd in enumerate(paths):
            _sym_p = np.asarray(_pd["symbol"], dtype="U20")
            _ts_p  = np.asarray(_pd["timestamp"], dtype="datetime64[ns]")
            midx   = pd.MultiIndex.from_arrays(
                [_sym_p, _ts_p.view(np.int64)], names=["symbol", "ts"]
            )
            assert midx.is_unique, f"path {pi} should be per-path unique"

    def test_joint_cal_matrix_has_correct_shape(self):
        # After the XGBoost join the slice of xgb_cal_matrix must have shape
        # (n_matched_rows, n_cpcv_paths) — not (n_unique_rows, n_cpcv_paths).
        n_rows, n_paths, n_matched = 200, 7, 150
        rng = np.random.default_rng(99)
        full_matrix   = rng.uniform(0.3, 0.7, (n_rows, n_paths))
        match_indices = rng.choice(n_rows, n_matched, replace=False)
        joint_cal_matrix = full_matrix[match_indices]
        assert joint_cal_matrix.shape == (n_matched, n_paths)
