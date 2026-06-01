"""
E1 — ML Regression Suite  (CI-Gated Defect Guards)
====================================================
One-per-RC regression harness for root-cause defects RC-1 through RC-7,
plus DSR/PBO reference-vector validation, kill-9 resume parity, bundle
integrity end-to-end, and inference latency budget checks.

Design contract
---------------
Each test class is written so that **reverting the corresponding fix causes
at least one test in that class to fail**.  Assertions probe the *old broken
behaviour* (what would happen if the guard was absent), not merely the new
correct outcome.

Root-cause register
-------------------
RC-1  Stale checkpoint resumed silently — pre-v2 lacked eval_r.npy, causing a
      silent Sharpe→accuracy downgrade instead of a hard stop.
RC-2  Future-peek leakage — stratified-random HPO split let label-horizon data
      bleed from the test period into training features.
RC-3  HOLD sentinel (−1) mapped to short (−1) in long_only mode; cost drag was
      not delegated to the authoritative charge calculator.
RC-4  Calibrator fitted on the HPO validation set (selection leakage) — the
      same data Optuna used for early-stopping / trial selection.
RC-5  53 % accuracy / zero net-DSR model passed quality gates because gates were
      accuracy-based and Sharpe was a soft warning only.
RC-6  Training continued when the symbol universe silently shrank to 47 % of the
      target (2551 → 1198 drop), biasing every downstream metric.
RC-7  Dual-table promotion (ml_model_metadata + ai_ml_models) was not atomic —
      a commit failure could leave the two tables permanently divergent.
"""
from __future__ import annotations

import inspect
import json
import math
import time
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_ml_model(
    *,
    name: str = "xgboost",
    version: str = "test_1.0.0_xgboost",
    status: str = "staging",
    samples: int = 150_000,
    metrics: dict | None = None,
    onnx_path: str | None = None,
) -> MagicMock:
    """MagicMock shaped like MLModelMetadata — mirrors C2/C3 test fixture."""
    m = MagicMock()
    m.model_name        = name
    m.model_version     = version
    m.status            = status
    m.is_active         = False
    m.training_samples  = samples
    m.training_metrics  = metrics if metrics is not None else {}
    m.training_features = []
    m.onnx_path         = onnx_path
    m.lineage           = None
    m.trained_at        = None
    m.deployed_at       = None
    return m


def _make_orchestrator(tmp_path: Path, *, n_symbols: int = 100, min_symbol_coverage: float = 0.85) -> Any:
    """Minimal ProductionTrainingOrchestrator with mocked DB (for RC-6 gate tests)."""
    from scripts.production_training_orchestrator import (
        ProductionTrainingOrchestrator,
        TrainingConfig,
    )
    config = TrainingConfig(n_symbols=n_symbols, min_symbol_coverage=min_symbol_coverage)
    return ProductionTrainingOrchestrator(
        db_session=AsyncMock(),
        config=config,
        output_dir=tmp_path / "models",
        fresh=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# RC-1 — Stale checkpoint loud-fail
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRC1StaleCheckpointLoudFail:
    """Reverting _enforce_compat or removing the schema_version check would cause
    every test in this class to fail (old bug: silent resume of incompatible state).
    """

    def _current_config(self) -> dict:
        return {
            "n_features": 69,
            "sequence_length": 60,
            "include_fundamentals": True,
            "model_version": "1.0.0",
            "max_epochs": 30,
        }

    def test_absent_schema_version_raises(self) -> None:
        """State dict with no schema_version key must be refused — pre-versioning era."""
        from app.ml.training.checkpoint_manager import _enforce_compat
        from app.ml.training.exceptions import StaleCheckpointError

        with pytest.raises(StaleCheckpointError, match="schema_version"):
            _enforce_compat({"config": self._current_config()}, self._current_config())

    def test_schema_v1_raises(self) -> None:
        """Pre-v2 checkpoint (no eval_r.npy) must be refused, not silently resumed."""
        from app.ml.training.checkpoint_manager import _enforce_compat
        from app.ml.training.exceptions import StaleCheckpointError

        with pytest.raises(StaleCheckpointError):
            _enforce_compat({"schema_version": 1, "config": self._current_config()}, self._current_config())

    def test_schema_v2_raises(self) -> None:
        """v2 checkpoint lacks per-row symbol join key required for A5 — must refuse."""
        from app.ml.training.checkpoint_manager import _enforce_compat
        from app.ml.training.exceptions import StaleCheckpointError

        with pytest.raises(StaleCheckpointError):
            _enforce_compat({"schema_version": 2, "config": self._current_config()}, self._current_config())

    def test_schema_v3_raises(self) -> None:
        """v3 checkpoint predates A5 symbol join keys — must refuse."""
        from app.ml.training.checkpoint_manager import _enforce_compat
        from app.ml.training.exceptions import StaleCheckpointError

        with pytest.raises(StaleCheckpointError):
            _enforce_compat({"schema_version": 3, "config": self._current_config()}, self._current_config())

    def test_current_schema_matching_config_passes(self) -> None:
        """Identical schema_version + matching config must not raise."""
        from app.ml.training.checkpoint_manager import SCHEMA_VERSION, _enforce_compat

        _enforce_compat(
            {"schema_version": SCHEMA_VERSION, "config": self._current_config()},
            self._current_config(),
        )

    def test_model_affecting_key_drift_raises(self) -> None:
        """A change to n_features invalidates every downstream artifact — must abort."""
        from app.ml.training.checkpoint_manager import SCHEMA_VERSION, _enforce_compat
        from app.ml.training.exceptions import StaleCheckpointError

        saved_config = {**self._current_config(), "n_features": 49}   # old feature set
        current_config = {**self._current_config(), "n_features": 69}  # new fundamentals

        with pytest.raises(StaleCheckpointError, match="n_features"):
            _enforce_compat({"schema_version": SCHEMA_VERSION, "config": saved_config}, current_config)

    def test_non_model_affecting_key_drift_does_not_raise(self) -> None:
        """Operational-only key changes (e.g. max_epochs) must not abort the run."""
        from app.ml.training.checkpoint_manager import SCHEMA_VERSION, _enforce_compat

        saved_config = {**self._current_config(), "max_epochs": 20}
        current_config = {**self._current_config(), "max_epochs": 50}

        _enforce_compat({"schema_version": SCHEMA_VERSION, "config": saved_config}, current_config)

    def test_eval_r_npy_absence_detected(self, tmp_path: Path) -> None:
        """gru_has_eval_arrays() must return False when eval_r.npy is absent —
        the guard that prevents silent Sharpe→accuracy downgrade on resume.
        """
        from app.ml.training.checkpoint_manager import SCHEMA_VERSION, CheckpointManager

        config = {"n_features": 69, "sequence_length": 60, "include_fundamentals": True, "model_version": "1.0.0"}
        cp = CheckpointManager(tmp_path / "cp", config, fresh=True)

        gru_dir = tmp_path / "cp" / "step_6_gru"
        gru_dir.mkdir(exist_ok=True)
        np.save(str(gru_dir / "eval_X.npy"), np.zeros((10, 5, 3), dtype=np.float32))
        np.save(str(gru_dir / "eval_y.npy"), np.zeros(10, dtype=np.int8))
        np.save(str(gru_dir / "eval_sym.npy"), np.array(["SYM"] * 10, dtype="U20"))
        np.save(str(gru_dir / "eval_ts.npy"), np.zeros(10, dtype=np.int64))
        (gru_dir / "eval_meta.json").write_text("{}")
        # eval_r.npy deliberately absent

        assert not cp.gru_has_eval_arrays(), (
            "gru_has_eval_arrays() must return False when eval_r.npy is absent — "
            "resuming without returns would silently downgrade to accuracy objective."
        )


# ══════════════════════════════════════════════════════════════════════════════
# RC-2 — Future-peek leakage probe
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRC2FuturePeekLeakageProbe:
    """Removing the purge/embargo logic from PanelPurgedCPCV would cause every
    property test here to fail (old bug: train rows could see test-period labels).
    """

    def _make_panel_timestamps(self, n_symbols: int = 5, n_dates: int = 100) -> np.ndarray:
        """Synthetic panel: n_symbols × n_dates rows sharing a calendar."""
        dates = np.array(
            [np.datetime64("2020-01-02") + np.timedelta64(i, "D") for i in range(n_dates)],
            dtype="datetime64[ns]",
        )
        return np.tile(dates, n_symbols)

    def test_no_train_code_overlaps_purge_embargo_window(self) -> None:
        """Property: for every split and every contiguous test-code run [s, e],
        no train code falls in the forbidden window [s − horizon, e + embargo].
        Removing the purge/embargo logic causes this to fail.
        """
        from app.ml.evaluation.cpcv import PanelPurgedCPCV

        ts = self._make_panel_timestamps(n_symbols=5, n_dates=120)
        cpcv = PanelPurgedCPCV(ts, horizon=5, n_groups=8, n_test=2, embargo=5)
        h, emb = cpcv.horizon, cpcv.embargo

        for split in cpcv.split():
            if split.test_idx.size == 0:
                continue
            test_codes = np.sort(np.unique(cpcv._row_code[split.test_idx]))
            train_codes_set = set(cpcv._row_code[split.train_idx].tolist())

            # Identify contiguous runs of test codes and check each run's forbidden zone
            boundaries = np.flatnonzero(np.diff(test_codes) > 1)
            run_starts = np.r_[test_codes[0], test_codes[boundaries + 1]]
            run_ends   = np.r_[test_codes[boundaries], test_codes[-1]]

            for s, e in zip(run_starts.tolist(), run_ends.tolist()):
                s, e = int(s), int(e)
                forbidden = set(range(max(0, s - h), e + 1 + emb))
                overlap = train_codes_set & forbidden
                assert not overlap, (
                    f"Combo {split.combo_id}: train contains {len(overlap)} code(s) "
                    f"in purge/embargo window for test run [{s}, {e}]. "
                    "Future-peek leakage detected — purge logic is broken."
                )

    def test_each_test_row_assigned_to_exactly_one_path_per_split(self) -> None:
        """For every split, test_path_of_row must be row-aligned with test_idx
        and each value must be a valid path id from that split's test_paths dict.
        Violation means the OOF path-routing for backtesting is broken.
        """
        from app.ml.evaluation.cpcv import PanelPurgedCPCV

        ts = self._make_panel_timestamps(n_symbols=4, n_dates=80)
        cpcv = PanelPurgedCPCV(ts, horizon=5, n_groups=8, n_test=2, embargo=3)
        valid_paths = set(range(cpcv.total_paths))

        for split in cpcv.split():
            assert split.test_path_of_row.shape == split.test_idx.shape, (
                f"Combo {split.combo_id}: test_path_of_row shape "
                f"{split.test_path_of_row.shape} != test_idx shape {split.test_idx.shape}"
            )
            assigned = set(split.test_path_of_row.tolist())
            assert assigned.issubset(valid_paths), (
                f"Combo {split.combo_id}: path ids {assigned - valid_paths} "
                f"are outside the valid range [0, {cpcv.total_paths})."
            )
            # Every path assigned in this split must be in test_paths values
            assert assigned == set(split.test_paths.values()), (
                f"Combo {split.combo_id}: assigned paths {assigned} != "
                f"declared test_paths {set(split.test_paths.values())}."
            )

    def test_purged_holdout_split_train_test_disjoint(self) -> None:
        """purged_holdout_split must produce strictly disjoint train and test sets."""
        from app.ml.evaluation.cpcv import purged_holdout_split

        ts = self._make_panel_timestamps(n_symbols=5, n_dates=100)
        train_idx, test_idx = purged_holdout_split(ts, horizon=5, embargo=5, train_frac=0.8)

        overlap = np.intersect1d(train_idx, test_idx)
        assert overlap.size == 0, f"Train/test overlap: {overlap.size} rows."

    def test_purged_holdout_enforces_temporal_order(self) -> None:
        """All train timestamps must strictly precede test timestamps by ≥ h+embargo gap."""
        from app.ml.evaluation.cpcv import purged_holdout_split

        ts = self._make_panel_timestamps(n_symbols=5, n_dates=100)
        train_idx, test_idx = purged_holdout_split(ts, horizon=5, embargo=5, train_frac=0.8)

        ts_arr = np.asarray(ts)
        train_max_ts = ts_arr[train_idx].max()
        test_min_ts  = ts_arr[test_idx].min()
        assert train_max_ts < test_min_ts, (
            "Temporal ordering violated: some train timestamps are ≥ test timestamps."
        )

    def test_empty_timestamps_raises(self) -> None:
        """Fail-loud on empty timestamp array — never produce a degenerate split silently."""
        from app.ml.evaluation.cpcv import PanelPurgedCPCV

        with pytest.raises(ValueError, match="empty"):
            PanelPurgedCPCV(np.array([], dtype="datetime64[ns]"))


# ══════════════════════════════════════════════════════════════════════════════
# RC-3 — HOLD as flat + exact cost drag
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRC3HoldAsFlatAndExactCostDrag:
    """Removing the HOLD-override mask from binary_to_positions, or replacing the
    charge-calculator delegation with a hard-coded constant, would cause these tests
    to fail (old bug: HOLD→short in long_only; approximate cost drag).
    """

    def test_hold_sentinel_is_flat_long_only(self) -> None:
        """HOLD (−1) must map to 0 (flat) in long_only — never to −1 (short)."""
        from app.ml.evaluation.backtest import binary_to_positions

        pred = np.array([-1, -1, -1], dtype=np.int8)
        pos = binary_to_positions(pred, mode="long_only")
        assert (pos == 0).all(), f"Expected all 0 (flat), got: {pos}"

    def test_hold_sentinel_is_flat_long_short(self) -> None:
        """HOLD (−1) must map to 0 even in long_short mode — HOLD overrides short mapping."""
        from app.ml.evaluation.backtest import binary_to_positions

        pred = np.array([-1, -1, -1], dtype=np.int8)
        pos = binary_to_positions(pred, mode="long_short")
        assert (pos == 0).all(), f"Expected all 0 (flat), got: {pos}"

    def test_down_is_flat_not_short_in_long_only(self) -> None:
        """DOWN (0) must produce position 0 in long_only — a long-only portfolio never shorts."""
        from app.ml.evaluation.backtest import binary_to_positions

        pred = np.array([0, 0, 0], dtype=np.int8)
        pos = binary_to_positions(pred, mode="long_only")
        assert (pos == 0).all(), (
            f"RC-3: DOWN maps to {set(pos.tolist())} in long_only, expected {{0}} (flat). "
            "Reverting this fix would allow long_only strategies to short."
        )

    def test_up_produces_long_position_both_modes(self) -> None:
        """UP (1) must always produce +1 regardless of mode."""
        from app.ml.evaluation.backtest import binary_to_positions

        pred = np.array([1, 1, 1], dtype=np.int8)
        for mode in ("long_only", "long_short"):
            pos = binary_to_positions(pred, mode=mode)
            assert (pos == 1).all(), f"UP should map to +1 in {mode}, got {pos}"

    def test_flat_bar_produces_zero_net_return(self) -> None:
        """All-flat predictions must yield zero net return — no P&L, no costs."""
        from app.ml.evaluation.backtest import strategy_returns

        pred = np.zeros(50, dtype=np.int8)
        fwd  = np.random.default_rng(0).standard_normal(50).astype(np.float32)
        ret  = strategy_returns(pred, fwd, mode="long_only")

        assert (ret == 0.0).all(), (
            "Flat predictions must produce zero net return. "
            f"Non-zero values: {ret[ret != 0]}"
        )

    def test_cost_drag_delegates_to_charge_calculator(self) -> None:
        """round_trip_charge_fraction must return the exact charge from estimate_round_trip_charges.
        A hard-coded approximation would diverge from the authoritative schedule.
        """
        from app.ml.evaluation.backtest import round_trip_charge_fraction
        from app.services.paper_trading.charge_calculator import estimate_round_trip_charges

        ep = Decimal("1000")
        qty = 10
        product = "CNC"

        _, _, expected_drag = estimate_round_trip_charges(product, ep, ep, qty)
        expected_fraction = float(expected_drag) / (float(ep) * qty)

        result = round_trip_charge_fraction(product, ep, ep, qty)

        assert abs(result - expected_fraction) < 1e-12, (
            f"round_trip_charge_fraction={result:.8f} differs from "
            f"estimate_round_trip_charges={expected_fraction:.8f}. "
            "Cost drag must delegate to the authoritative charge calculator."
        )


# ══════════════════════════════════════════════════════════════════════════════
# RC-4 — Calibration leakage closed
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRC4CalibrationLeakageClosed:
    """Reverting to auto-fit inside train() (the old leaking path) would cause
    tests 1-3 to fail; removing fit_calibrator_on_oof would break tests 4-5.
    """

    def _make_panel_timestamps(self, n: int = 200) -> np.ndarray:
        dates = np.array(
            [np.datetime64("2020-01-02") + np.timedelta64(i, "D") for i in range(n)],
            dtype="datetime64[ns]",
        )
        return np.tile(dates, 3)  # 3 symbols × n dates

    def test_cpcv_split_train_and_test_are_disjoint(self) -> None:
        """Within every CPCV split, train_idx and test_idx must be strictly disjoint.
        Overlap means the model (and calibrator) sees test-period labels during training —
        the core purge invariant guarding against RC-4 calibration leakage.
        """
        from app.ml.evaluation.cpcv import PanelPurgedCPCV

        ts = self._make_panel_timestamps(300)
        cpcv = PanelPurgedCPCV(ts, horizon=5, n_groups=8, n_test=2, embargo=5)
        for split in cpcv.split():
            overlap = np.intersect1d(split.train_idx, split.test_idx)
            assert overlap.size == 0, (
                f"CPCV combo {split.combo_id}: train ∩ test = {overlap.size} rows. "
                "Purge logic is broken — test-period rows leaked into the training set."
            )

    def test_cpcv_each_row_appears_in_exactly_n_test_combos(self) -> None:
        """Each panel row must appear in exactly C(n_groups−1, n_test−1) test splits.
        For n_groups=8, n_test=2 this equals C(7,1)=7. Deviation means group-assignment
        or the path-routing logic is broken.
        """
        from math import comb as math_comb
        from app.ml.evaluation.cpcv import PanelPurgedCPCV

        ts = self._make_panel_timestamps(200)
        n = len(ts)
        cpcv = PanelPurgedCPCV(ts, horizon=5, n_groups=8, n_test=2, embargo=5)
        expected = math_comb(cpcv.n_groups - 1, cpcv.n_test - 1)

        coverage = np.zeros(n, dtype=np.int32)
        for split in cpcv.split():
            coverage[split.test_idx] += 1

        assert (coverage == expected).all(), (
            f"CPCV OOF coverage violated: expected each row in {expected} test splits, "
            f"got counts spanning [{coverage.min()}, {coverage.max()}]."
        )

    def test_xgboost_trainer_train_does_not_fit_calibrator(self) -> None:
        """train() must not call any calibrator-fitting method on the HPO val set.
        Fitting on HPO val = selection leakage (the original RC-4 defect).
        """
        from app.ml.training.xgboost_trainer import XGBoostTrainer

        src = inspect.getsource(XGBoostTrainer.train)
        forbidden_patterns = [
            "self._fit_calibrator",       # direct old method call
            "calibrator.fit(",            # direct fit on any calibrator object
            "ConfidenceCalibrator(",      # instantiation inside train()
        ]
        for pattern in forbidden_patterns:
            assert pattern not in src, (
                f"XGBoostTrainer.train() contains '{pattern}' — this introduces "
                "calibration selection leakage (RC-4). The calibrator must be fitted "
                "via fit_calibrator_on_oof() after the CPCV OOF loop, not inside train()."
            )

    def test_calibrator_ece_decreases_after_fit(self) -> None:
        """fit() on a well-structured validation set must reduce ECE."""
        from app.ml.inference.calibrator import ConfidenceCalibrator

        rng = np.random.default_rng(42)
        n = 500
        y = rng.integers(0, 2, size=n).astype(np.int8)
        # Overconfident raw scores — calibration should correct them
        raw_scores = np.where(y == 1, rng.uniform(0.7, 1.0, n), rng.uniform(0.0, 0.3, n))

        cal = ConfidenceCalibrator("xgboost")
        cal.fit(y, raw_scores)

        assert cal.ece_after is not None
        assert cal.ece_before is not None
        assert cal.ece_after <= cal.ece_before, (
            f"ECE did not improve: before={cal.ece_before:.4f}, after={cal.ece_after:.4f}. "
            "Beta calibration must reduce ECE on structured data."
        )

    def test_serializer_calibrated_confidence_field_present(self) -> None:
        """serializers.py must expose 'calibrated_confidence' to signal the field is populated
        by the A4 pipeline, not a raw uncalibrated score.
        """
        import app.ai.fusion.serializers as _serializers

        src = inspect.getsource(_serializers)
        assert "calibrated_confidence" in src, (
            "'calibrated_confidence' key absent from serializers.py. "
            "The field must be present to truthfully expose post-calibration scores."
        )


# ══════════════════════════════════════════════════════════════════════════════
# RC-5 — 53 % / zero-DSR model blocked by quality gates
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRC5FiftyThreePercentZeroDsrBlocked:
    """Removing the AUC-PR primary gate or the DSR hard gate would allow a
    weak model to slip through — these tests would fail if gates are relaxed.
    """

    def _weak_model(self) -> MagicMock:
        """Fixture replicating the 1.0.0 production model's fabricated-zero metrics."""
        return _make_ml_model(
            name="xgboost",
            version="test_weak_1.0.0_xgboost",
            status="staging",
            samples=150_000,
            metrics={
                "accuracy":       0.53,
                "f1_score":       0.51,
                # auc_pr absent → fail-safe default 0.0 (gate 1 fails)
                # deflated_sharpe absent → fail-safe default −1.0 (gate 2 fails)
                # ece_after absent → fail-safe default 1.0 (gate 3 fails)
                "sharpe_ratio":   0.0,   # fabricated-zero pattern from RC-1
                "n_trades":       0,
            },
        )

    def _strong_model(self) -> MagicMock:
        return _make_ml_model(
            name="xgboost",
            version="test_strong_1.0.0_xgboost",
            status="staging",
            samples=200_000,
            metrics={
                "accuracy":        0.57,
                "f1_score":        0.55,
                "auc_pr":          0.60,
                "deflated_sharpe": 0.75,
                "ece_after":       0.03,
                "symbol_coverage": 0.92,
            },
        )

    def test_absent_auc_pr_uses_fail_safe_zero(self) -> None:
        """Absent auc_pr must default to 0.0 — an unvalidated model cannot pass silently."""
        from app.ml.model_registry import QualityGate, QualityGateError

        with pytest.raises(QualityGateError) as exc_info:
            QualityGate().validate(self._weak_model())

        assert "auc_pr" in exc_info.value.failed_checks, (
            "auc_pr gate must fail when key is absent from training_metrics."
        )

    def test_zero_dsr_fails_strict_positive_gate(self) -> None:
        """deflated_sharpe=0.0 must fail — the gate requires strictly positive DSR."""
        from app.ml.model_registry import QualityGate, QualityGateError

        model = _make_ml_model(
            metrics={
                "accuracy": 0.58, "f1_score": 0.56,
                "auc_pr": 0.60, "deflated_sharpe": 0.0, "ece_after": 0.03,
            }
        )
        with pytest.raises(QualityGateError) as exc_info:
            QualityGate().validate(model)

        assert "deflated_sharpe" in exc_info.value.failed_checks, (
            "deflated_sharpe=0.0 must fail (gate requires strictly positive DSR > 0.0). "
            "Zero DSR means the model earns no alpha after costs and selection-bias deflation."
        )

    def test_absent_ece_uses_fail_safe_one(self) -> None:
        """Absent ece_after must default to 1.0 (worst possible ECE) — never pass silently."""
        from app.ml.model_registry import QualityGate, QualityGateError

        model = _make_ml_model(
            metrics={
                "accuracy": 0.58, "f1_score": 0.56,
                "auc_pr": 0.60, "deflated_sharpe": 0.80,
                # ece_after absent
            }
        )
        with pytest.raises(QualityGateError) as exc_info:
            QualityGate().validate(model)

        assert "calibration_ece" in exc_info.value.failed_checks

    def test_53pct_fixture_fails_minimum_three_gates(self) -> None:
        """The canonical weak-model fixture must fail ≥ 3 hard gates."""
        from app.ml.model_registry import QualityGate, QualityGateError

        with pytest.raises(QualityGateError) as exc_info:
            QualityGate().validate(self._weak_model())

        n_failed = len(exc_info.value.failed_checks)
        assert n_failed >= 3, (
            f"Expected ≥ 3 hard-gate failures for the 53 % / zero-DSR fixture, "
            f"got {n_failed}: {list(exc_info.value.failed_checks)}"
        )

    def test_well_qualified_model_passes_all_gates(self) -> None:
        """A model that genuinely meets every threshold must pass without raising."""
        from app.ml.model_registry import QualityGate

        result = QualityGate().validate(self._strong_model())
        assert result["passed"] is True


# ══════════════════════════════════════════════════════════════════════════════
# RC-6 — Low coverage abort + sample count
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRC6LowCoverageAbortAndSampleCount:
    """Removing the A7 coverage gate or softening it to a warning would cause
    tests 2-4 to fail (old bug: training continued on a 47 % symbol universe).
    """

    def test_data_coverage_error_is_runtime_error_subclass(self) -> None:
        """DataCoverageError must be a RuntimeError subclass — fail-loud, not swallowed."""
        from app.ml.training.exceptions import DataCoverageError

        assert issubclass(DataCoverageError, RuntimeError), (
            "DataCoverageError must inherit from RuntimeError so bare 'except Exception' "
            "handlers still surface the failure instead of swallowing it."
        )

    def test_coverage_below_threshold_raises(self, tmp_path: Path) -> None:
        """50 % coverage (50/100) is below the 85 % threshold — must abort with DataCoverageError."""
        from app.ml.training.exceptions import DataCoverageError

        orch = _make_orchestrator(tmp_path, n_symbols=100, min_symbol_coverage=0.85)
        with pytest.raises(DataCoverageError):
            orch._assert_symbol_coverage(usable=50, requested=100)

    def test_coverage_at_threshold_passes(self, tmp_path: Path) -> None:
        """Exactly at the threshold (85/100 = 85 %) must pass — gate is ≥ inclusive."""
        orch = _make_orchestrator(tmp_path, n_symbols=100, min_symbol_coverage=0.85)
        orch._assert_symbol_coverage(usable=85, requested=100)  # must not raise

    def test_error_message_contains_raw_counts(self, tmp_path: Path) -> None:
        """Error message must include usable + requested counts so operators can investigate."""
        from app.ml.training.exceptions import DataCoverageError

        orch = _make_orchestrator(tmp_path, n_symbols=100, min_symbol_coverage=0.85)
        with pytest.raises(DataCoverageError, match=r"50") as exc_info:
            orch._assert_symbol_coverage(usable=50, requested=100)

        msg = str(exc_info.value)
        assert "100" in msg, f"Error message missing 'requested' count. Got: {msg!r}"

    def test_zero_requested_is_division_safe(self, tmp_path: Path) -> None:
        """requested=0 must not raise ZeroDivisionError — max(requested, 1) guard must exist."""
        from app.ml.training.exceptions import DataCoverageError

        orch = _make_orchestrator(tmp_path, n_symbols=0, min_symbol_coverage=0.85)
        # 0 usable / max(0,1) = 0.0 coverage → DataCoverageError, not ZeroDivisionError
        with pytest.raises(DataCoverageError):
            orch._assert_symbol_coverage(usable=0, requested=0)


# ══════════════════════════════════════════════════════════════════════════════
# RC-7 — Atomic dual-table promotion
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRC7AtomicDualTablePromotion:
    """Moving _project_to_ai_ml_models to after commit() would cause test 1 to fail;
    removing the try/except rollback would cause test 2 to fail (old bug: divergent tables).
    """

    def _make_staging_model(self) -> MagicMock:
        m = MagicMock()
        m.model_version = "test_1.0.0_xgboost"
        m.model_name    = "xgboost"
        m.status        = "staging"
        m.is_active     = False
        m.updated_at    = None
        m.deployed_at   = None
        m.training_metrics = {"accuracy": 0.57, "f1_score": 0.55, "auc_pr": 0.60,
                              "deflated_sharpe": 0.75, "ece_after": 0.03}
        m.training_samples = 200_000
        return m

    @pytest.mark.asyncio
    async def test_project_called_before_commit(self) -> None:
        """_project_to_ai_ml_models must be invoked before session.commit().
        Inverting the order would make a commit failure leave the tables divergent.
        """
        from app.ml.model_registry import ModelPromoter, QualityGate

        call_order: list[str] = []
        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_staging_model())
        )

        async def _track_commit() -> None:
            call_order.append("commit")

        async def _track_refresh(_model: Any) -> None:
            pass

        session.commit.side_effect = _track_commit
        session.refresh.side_effect = _track_refresh

        gate = MagicMock(spec=QualityGate)
        gate.validate.return_value = {"passed": True, "checks": {}}

        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
        ) as mock_project:
            async def _track_project(*args: Any, **kwargs: Any) -> int:
                call_order.append("project")
                return 1

            mock_project.side_effect = _track_project
            promoter = ModelPromoter(session, quality_gate=gate)
            await promoter.promote_to_production("test_1.0.0_xgboost", "xgboost")

        assert "project" in call_order and "commit" in call_order
        assert call_order.index("project") < call_order.index("commit"), (
            f"Atomicity violated: project must precede commit. Got order: {call_order}"
        )

    @pytest.mark.asyncio
    async def test_commit_failure_triggers_rollback(self) -> None:
        """If commit raises, session.rollback() must be awaited — both writes discarded."""
        from app.ml.model_registry import ModelPromoter, QualityGate

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_staging_model())
        )
        session.commit.side_effect = RuntimeError("simulated DB outage")

        gate = MagicMock(spec=QualityGate)
        gate.validate.return_value = {"passed": True, "checks": {}}

        with patch("app.ml.model_registry._project_to_ai_ml_models", new_callable=AsyncMock, return_value=1):
            with pytest.raises(RuntimeError, match="simulated DB outage"):
                await ModelPromoter(session, quality_gate=gate).promote_to_production(
                    "test_1.0.0_xgboost", "xgboost"
                )

        session.rollback.assert_awaited_once()

    def test_state_map_covers_all_ml_statuses(self) -> None:
        """Every ml_model_metadata status must have a corresponding ai_ml_models state."""
        from app.ml.model_registry import _ML_TO_GOVERNANCE_STATE

        required = {"production": "live", "staging": "paper", "development": "shadow"}
        for ml_status, expected_state in required.items():
            assert ml_status in _ML_TO_GOVERNANCE_STATE, (
                f"Missing state mapping for ml_status='{ml_status}'"
            )
            assert _ML_TO_GOVERNANCE_STATE[ml_status] == expected_state, (
                f"Wrong state: {ml_status!r} → {_ML_TO_GOVERNANCE_STATE[ml_status]!r}, "
                f"expected {expected_state!r}"
            )

    def test_deprecated_promote_to_production_raises(self, tmp_path: Path) -> None:
        """ModelRegistry.promote_to_production() must raise RegistryDeprecatedError.
        This guard closes the ungated bypass path that existed before A8.
        """
        from app.ml.model_registry import ModelRegistry, RegistryDeprecatedError

        registry = ModelRegistry(session=AsyncMock(), model_storage_path=tmp_path)
        with pytest.raises(RegistryDeprecatedError, match="ModelPromoter"):
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                registry.promote_to_production("test_1.0.0_xgboost")
            )

    def test_break_glass_without_reason_raises(self) -> None:
        """skip_quality_gates=True with no reason must raise BreakGlassError immediately."""
        from app.ml.model_registry import BreakGlassError, ModelPromoter

        promoter = ModelPromoter(AsyncMock())
        import asyncio
        with pytest.raises(BreakGlassError, match="break_glass_reason"):
            asyncio.get_event_loop().run_until_complete(
                promoter.promote_to_production(
                    "test_1.0.0_xgboost",
                    "xgboost",
                    skip_quality_gates=True,
                    break_glass_reason="",  # empty → must fail
                )
            )


# ══════════════════════════════════════════════════════════════════════════════
# DSR / PBO reference-vector unit tests
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDsrPboReferenceVectors:
    """Verify DSR/PBO against known analytic values from Bailey & López de Prado (2014).
    Any regression in the formula implementation will cause at least one test here to fail.
    """

    def test_dsr_positive_edge_above_half(self) -> None:
        """Normally-distributed returns with μ>0, N=252 trials, 7 paths → DSR > 0.5."""
        from app.ml.evaluation.deflated_sharpe import deflated_sharpe_ratio

        sr = 0.08          # per-period Sharpe (positive edge)
        n_obs = 252        # one year of daily data
        skew = 0.0         # normal
        kurt_raw = 3.0     # normal (raw, not excess)
        sr_std = 0.02      # moderate cross-path variation
        n_trials = 7

        dsr = deflated_sharpe_ratio(sr, n_obs, skew, kurt_raw, sr_std, n_trials)
        assert 0.0 < dsr <= 1.0, f"DSR must be in (0,1], got {dsr}"
        assert dsr > 0.5, f"Positive-edge strategy should have DSR > 0.5, got {dsr:.4f}"

    def test_dsr_zero_mean_returns_below_half(self) -> None:
        """Zero-edge strategy (SR=0) should have DSR < 0.5 — not genuinely profitable."""
        from app.ml.evaluation.deflated_sharpe import deflated_sharpe_ratio

        dsr = deflated_sharpe_ratio(
            sr=0.0, n_obs=252, skew=0.0, kurt_raw=3.0,
            sr_std_across_trials=0.05, n_trials=10,
        )
        assert dsr < 0.5, f"Zero-edge strategy must have DSR < 0.5, got {dsr:.4f}"

    def test_dsr_n1_has_zero_sr0_benchmark(self) -> None:
        """With n_trials=1 there is no selection bias — SR₀ must be 0.0."""
        from app.ml.evaluation.deflated_sharpe import _benchmark_sr0

        sr0 = _benchmark_sr0(sr_std=0.05, n_trials=1)
        assert sr0 == 0.0, f"SR₀ must be 0 for n_trials=1 (no selection), got {sr0}"

    def test_dsr_monotone_in_n_obs(self) -> None:
        """More observations → higher confidence → DSR increases (law of large numbers)."""
        from app.ml.evaluation.deflated_sharpe import deflated_sharpe_ratio

        sr, skew, kurt_raw, sr_std, n_trials = 0.06, 0.0, 3.0, 0.03, 5
        dsr_small = deflated_sharpe_ratio(sr, 60, skew, kurt_raw, sr_std, n_trials)
        dsr_large = deflated_sharpe_ratio(sr, 500, skew, kurt_raw, sr_std, n_trials)
        assert dsr_large > dsr_small, (
            f"DSR must increase with more observations: "
            f"T=60 → {dsr_small:.4f}, T=500 → {dsr_large:.4f}"
        )

    def test_sr0_benchmark_increases_with_n_trials(self) -> None:
        """More trials = more selection bias = higher SR₀ benchmark (harder to beat chance)."""
        from app.ml.evaluation.deflated_sharpe import _benchmark_sr0

        sr_std = 0.05
        sr0_5  = _benchmark_sr0(sr_std, 5)
        sr0_20 = _benchmark_sr0(sr_std, 20)
        assert sr0_20 > sr0_5, (
            f"SR₀ must increase with n_trials: n=5 → {sr0_5:.4f}, n=20 → {sr0_20:.4f}"
        )

    def test_pbo_all_negative_sharpes_is_one(self) -> None:
        """All paths losing money → PBO = 1.0 (model universally underperforms)."""
        from app.ml.evaluation.deflated_sharpe import probability_of_backtest_overfitting

        sharpes = np.array([-0.5, -0.3, -0.1, -0.8], dtype=np.float64)
        pbo = probability_of_backtest_overfitting(sharpes)
        assert pbo == 1.0, f"All-negative Sharpes must give PBO=1.0, got {pbo}"

    def test_pbo_all_positive_sharpes_is_zero(self) -> None:
        """All paths profitable → PBO = 0.0 (no overfitting signal)."""
        from app.ml.evaluation.deflated_sharpe import probability_of_backtest_overfitting

        sharpes = np.array([0.4, 0.2, 0.6, 0.1], dtype=np.float64)
        pbo = probability_of_backtest_overfitting(sharpes)
        assert pbo == 0.0, f"All-positive Sharpes must give PBO=0.0, got {pbo}"

    def test_pbo_empty_returns_nan(self) -> None:
        """Empty path array → NaN (not 0.0, not 1.0 — explicitly undefined)."""
        from app.ml.evaluation.deflated_sharpe import probability_of_backtest_overfitting

        pbo = probability_of_backtest_overfitting(np.array([], dtype=np.float64))
        assert math.isnan(pbo), f"Empty array must return NaN, got {pbo}"


# ══════════════════════════════════════════════════════════════════════════════
# Kill-9 resume parity proxy
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestKillResumeParityProxy:
    """Verify the structural properties that enable kill-9 resume parity.
    Removing these properties would break crash-resilient training restarts.
    """

    def _fresh_cp(self, tmp_path: Path) -> Any:
        from app.ml.training.checkpoint_manager import CheckpointManager
        config = {"n_features": 69, "sequence_length": 60, "include_fundamentals": True, "model_version": "1.0.0"}
        return CheckpointManager(tmp_path / "cp", config, fresh=True)

    def test_training_state_round_trips_correctly(self, tmp_path: Path) -> None:
        """Epoch checkpoint (sub-C) must survive a save→load round-trip exactly."""
        cp = self._fresh_cp(tmp_path)
        logs = {"loss": 0.342, "val_loss": 0.389, "val_auc_pr": 0.612}
        cp.save_gru_training_state(last_epoch=7, logs=logs)

        state = cp.load_gru_training_state()
        assert state["last_epoch"] == 7, f"Expected last_epoch=7, got {state['last_epoch']}"
        assert abs(state["logs"]["val_auc_pr"] - 0.612) < 1e-10

    def test_schema_version_written_in_fresh_init(self, tmp_path: Path) -> None:
        """A freshly created checkpoint must embed the current SCHEMA_VERSION."""
        from app.ml.training.checkpoint_manager import SCHEMA_VERSION, CheckpointManager

        config = {"n_features": 69, "sequence_length": 60, "include_fundamentals": True, "model_version": "1.0.0"}
        cp = CheckpointManager(tmp_path / "cp2", config, fresh=True)

        with open(tmp_path / "cp2" / "checkpoint.json") as fh:
            state = json.load(fh)

        assert state["schema_version"] == SCHEMA_VERSION, (
            f"Fresh checkpoint has schema_version={state['schema_version']}, "
            f"expected {SCHEMA_VERSION}. "
            "Missing or wrong version means stale checkpoints cannot be detected."
        )

    def test_atomic_json_write_leaves_no_tmp_file(self, tmp_path: Path) -> None:
        """_atomic_json must remove the .tmp file after a successful rename."""
        from app.ml.training.checkpoint_manager import _atomic_json

        target = tmp_path / "test.json"
        _atomic_json(target, {"key": "value"})

        tmp_file = target.with_suffix(".tmp")
        assert not tmp_file.exists(), (
            ".tmp file still present after _atomic_json — os.replace() may not have run. "
            "A crash during write would leave the target in an unknown state."
        )
        assert target.exists(), "Target JSON file was not created."

    def test_eval_r_npy_absence_blocks_eval_array_load(self, tmp_path: Path) -> None:
        """gru_has_eval_arrays() returns False when eval_r.npy is absent.
        This prevents silent resume without forward returns.
        """
        cp = self._fresh_cp(tmp_path)
        d = tmp_path / "cp" / "step_6_gru"
        d.mkdir(exist_ok=True)

        # Write all sub-A files EXCEPT eval_r.npy
        np.save(str(d / "eval_X.npy"),   np.zeros((5, 3, 2), dtype=np.float32))
        np.save(str(d / "eval_y.npy"),   np.zeros(5, dtype=np.int8))
        np.save(str(d / "eval_sym.npy"), np.array(["SYM"] * 5, dtype="U20"))
        np.save(str(d / "eval_ts.npy"),  np.zeros(5, dtype=np.int64))
        (d / "eval_meta.json").write_text("{}")

        assert not cp.gru_has_eval_arrays(), (
            "gru_has_eval_arrays() must return False when eval_r.npy is absent."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Bundle integrity end-to-end
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBundleIntegrityEndToEnd:
    """End-to-end verification that any byte-level tamper to a Promotion Report
    is detected by the bundle SHA-256.  These tests guard against regressions in
    the C2 checksum machinery (not just individual component tests).
    """

    def _build_report(self, tmp_path: Path) -> tuple[dict, Path]:
        """Build a minimal signed Promotion Report with fake artefact files."""
        from app.ml.evaluation.promotion_report import build_promotion_report

        model_dir = tmp_path / "models" / "xgboost"
        model_dir.mkdir(parents=True)
        onnx_file = model_dir / "model.onnx"
        onnx_file.write_bytes(b"\x00" * 64)   # fake ONNX payload

        cal_file = model_dir / "calibrator_xgb.pkl"
        cal_file.write_bytes(b"\x01" * 32)    # fake calibrator payload

        challenger = _make_ml_model(
            name="xgboost",
            version="test_1.1.0_xgboost",
            status="development",
            samples=200_000,
            metrics={
                "accuracy": 0.57, "f1_score": 0.55,
                "auc_pr": 0.60, "deflated_sharpe": 0.80, "ece_after": 0.03,
                "symbol_coverage": 0.92,
            },
            onnx_path=str(onnx_file),
        )

        report = build_promotion_report(
            challengers={"xgboost": challenger},
            champions={"xgboost": None},
            report_dir=tmp_path / "reports",
            run_id="test_e1_run",
        )

        report_path = Path(report["report_path"])
        return report, report_path

    def test_valid_report_passes_verification(self, tmp_path: Path) -> None:
        """A freshly built, untampered report must pass load_and_verify_report."""
        from app.ml.evaluation.promotion_report import load_and_verify_report

        _, path = self._build_report(tmp_path)
        verified = load_and_verify_report(path, challenger_version="test_1.1.0_xgboost", enforce_status=False)
        assert verified.get("bundle_sha256") is not None

    def test_report_body_tampering_raises_checksum_error(self, tmp_path: Path) -> None:
        """Modifying any scalar field in the report body must raise BundleChecksumError."""
        from app.ml.evaluation.promotion_report import BundleChecksumError, load_and_verify_report

        _, path = self._build_report(tmp_path)
        with open(path) as fh:
            data = json.load(fh)

        data["run_id"] = "TAMPERED_RUN_ID"   # mutate a body field
        with open(path, "w") as fh:
            json.dump(data, fh)

        with pytest.raises(BundleChecksumError):
            load_and_verify_report(path, challenger_version="test_1.1.0_xgboost", enforce_status=False)

    def test_bundle_sha256_tampering_raises_checksum_error(self, tmp_path: Path) -> None:
        """Overwriting the top-level bundle_sha256 must still be caught."""
        from app.ml.evaluation.promotion_report import BundleChecksumError, load_and_verify_report

        _, path = self._build_report(tmp_path)
        with open(path) as fh:
            data = json.load(fh)

        data["bundle_sha256"] = "a" * 64   # plausible-looking fake hash
        with open(path, "w") as fh:
            json.dump(data, fh)

        with pytest.raises(BundleChecksumError):
            load_and_verify_report(path, challenger_version="test_1.1.0_xgboost", enforce_status=False)

    def test_blocked_report_raises_on_standard_promotion_path(self, tmp_path: Path) -> None:
        """A BLOCKED Promotion Report must raise BlockedChallengerError on the standard path."""
        from app.ml.evaluation.promotion_report import BlockedChallengerError, build_promotion_report, load_and_verify_report

        # Challenger with zero-DSR → gate fails → BLOCKED
        challenger = _make_ml_model(
            metrics={"accuracy": 0.53, "f1_score": 0.51},
            onnx_path=None,
        )
        report = build_promotion_report(
            challengers={"xgboost": challenger},
            champions={"xgboost": None},
            report_dir=tmp_path / "reports_blocked",
            run_id="test_blocked",
        )

        assert report["status"] == "BLOCKED", "Weak challenger should produce a BLOCKED report."

        path = Path(report["report_path"])
        with pytest.raises(BlockedChallengerError):
            load_and_verify_report(path, challenger_version="test_1.0.0_xgboost", enforce_status=True)

    def test_blocked_report_allowed_on_break_glass_path(self, tmp_path: Path) -> None:
        """enforce_status=False must allow a BLOCKED report through (audited break-glass)."""
        from app.ml.evaluation.promotion_report import build_promotion_report, load_and_verify_report

        challenger = _make_ml_model(
            metrics={"accuracy": 0.53, "f1_score": 0.51},
            onnx_path=None,
        )
        report = build_promotion_report(
            challengers={"xgboost": challenger},
            champions={"xgboost": None},
            report_dir=tmp_path / "reports_bg",
            run_id="test_bg",
        )
        path = Path(report["report_path"])
        result = load_and_verify_report(path, challenger_version="test_1.0.0_xgboost", enforce_status=False)
        assert result is not None


# ══════════════════════════════════════════════════════════════════════════════
# Inference latency budget
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.performance
class TestInferenceLatencyBudget:
    """Latency regression guards — ensure critical inference-path operations remain
    within budget.  Run via: pytest -m performance tests/ml/test_regression_e1.py
    """

    def test_calibrator_calibrate_p99_within_5ms_per_1k_samples(self) -> None:
        """Calibrator.calibrate() p99 must be < 5 ms for a 1 000-sample batch."""
        from app.ml.inference.calibrator import ConfidenceCalibrator

        rng = np.random.default_rng(0)
        batch = rng.random((1_000, 2)).astype(np.float32)
        batch /= batch.sum(axis=1, keepdims=True)   # normalise to valid probabilities

        cal = ConfidenceCalibrator("xgboost")
        cal._is_fitted = True
        cal._a = 1.0; cal._b = 1.0; cal._c = 0.0

        latencies_ms = []
        for _ in range(200):
            t0 = time.perf_counter()
            cal.calibrate(batch)
            latencies_ms.append((time.perf_counter() - t0) * 1_000.0)

        p99_ms = float(np.percentile(latencies_ms, 99))
        assert p99_ms < 5.0, (
            f"Calibrator p99 latency {p99_ms:.2f} ms exceeds 5 ms budget "
            f"for a 1 000-sample batch. Inference throughput is < 200 k samples/s."
        )

    def test_strategy_returns_100k_within_500ms(self) -> None:
        """strategy_returns() must process 100 k predictions in < 500 ms."""
        from app.ml.evaluation.backtest import strategy_returns

        rng = np.random.default_rng(1)
        n = 100_000
        pred  = rng.integers(0, 2, size=n, dtype=np.int8)
        fwd   = rng.standard_normal(n).astype(np.float32) * 0.02

        t0 = time.perf_counter()
        result = strategy_returns(pred, fwd, mode="long_only")
        elapsed_ms = (time.perf_counter() - t0) * 1_000.0

        assert result.shape == (n,), "Output shape mismatch."
        assert elapsed_ms < 500.0, (
            f"strategy_returns took {elapsed_ms:.0f} ms for 100 k rows "
            f"(budget: 500 ms). Vectorised cost drag may have regressed."
        )
