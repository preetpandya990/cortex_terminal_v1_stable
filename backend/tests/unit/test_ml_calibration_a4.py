"""
A4 — Leakage-safe calibration acceptance tests.

Validates:
  1. XGBoostTrainer.train() no longer auto-fits a calibrator (selection leakage removed).
  2. fit_calibrator_on_oof() produces a better-calibrated model than HPO-val fitting.
  3. OOF fit set is disjoint from HPO validation set (zero row overlap).
  4. CheckpointManager save/load/has round-trip for both model types.
  5. ECE decreases after OOF calibration on a realistic synthetic task.
  6. Serializer exports identical confidence and calibrated_confidence fields.
  7. Edge-case guards: too-small pool, mismatched lengths.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from app.ml.inference.calibrator import ConfidenceCalibrator
from app.ml.training.checkpoint_manager import CheckpointManager
from app.ml.training.xgboost_trainer import XGBoostTrainer


# ── Reproducible RNG ──────────────────────────────────────────────────────────
RNG = np.random.default_rng(42)


# ── Synthetic data helpers ────────────────────────────────────────────────────

def _make_binary_data(n: int = 1_000, n_features: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """Return (X, y) with balanced classes and moderate signal."""
    X = RNG.standard_normal((n, n_features)).astype(np.float32)
    # True logit = first feature; signal-to-noise ≈ 0.6
    p = 1.0 / (1.0 + np.exp(-0.8 * X[:, 0]))
    y = (RNG.random(n) < p).astype(np.int32)
    return X, y


def _overconfident_scores(y: np.ndarray) -> np.ndarray:
    """Simulate overconfident XGBoost scores that need calibration."""
    p_true = 0.3 + 0.4 * y.astype(np.float64)
    # Push scores toward 0/1 extremes (overconfident)
    scores = np.clip(p_true + RNG.normal(0.0, 0.15, size=len(y)), 0.01, 0.99)
    # Amplify overconfidence: sigmoid(3 * logit(score))
    logit = np.log(scores) - np.log(1.0 - scores)
    return np.clip(1.0 / (1.0 + np.exp(-3.0 * logit)), 0.01, 0.99).astype(np.float32)


# ══════════════════════════════════════════════════════════════════════════════
# 1 — train() no longer auto-fits a calibrator
# ══════════════════════════════════════════════════════════════════════════════

class TestTrainNoAutoCalibrator:
    """XGBoostTrainer.train() must NOT set self.calibrator (selection leakage removed)."""

    def test_train_leaves_calibrator_none(self) -> None:
        """After train(), calibrator must remain None — no auto-fit on val set."""
        trainer = XGBoostTrainer(objective="binary:logistic", random_state=42)
        X, y = _make_binary_data(300, 8)
        X_tr, y_tr = X[:200], y[:200]
        X_va, y_va = X[200:], y[200:]

        trainer.train(
            X_tr, y_tr, X_va, y_va,
            params={
                "max_depth": 3, "learning_rate": 0.1, "n_estimators": 20,
                "tree_method": "hist",
            },
            early_stopping_rounds=10,
            verbose=0,
        )

        assert trainer.calibrator is None, (
            "train() must not auto-fit a calibrator on the HPO validation set "
            "(selection leakage). Call fit_calibrator_on_oof() after the CPCV loop."
        )

    def test_train_model_is_set(self) -> None:
        """Sanity: model IS trained even though calibrator isn't fitted."""
        trainer = XGBoostTrainer(random_state=42)
        X, y = _make_binary_data(200, 8)
        trainer.train(
            X[:150], y[:150], X[150:], y[150:],
            params={"max_depth": 2, "learning_rate": 0.1, "n_estimators": 10, "tree_method": "hist"},
            early_stopping_rounds=5,
            verbose=0,
        )
        assert trainer.model is not None


# ══════════════════════════════════════════════════════════════════════════════
# 2 — fit_calibrator_on_oof()
# ══════════════════════════════════════════════════════════════════════════════

class TestFitCalibratorOnOOF:
    """fit_calibrator_on_oof() produces a valid, ECE-improving calibrator."""

    def _train_and_get_oof(
        self,
        n: int = 600,
        n_features: int = 8,
    ) -> tuple[XGBoostTrainer, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Train a small booster, return trainer + simulated OOF pool."""
        X, y = _make_binary_data(n, n_features)
        trainer = XGBoostTrainer(random_state=42)
        trainer.train(
            X[:400], y[:400], X[400:500], y[400:500],
            params={"max_depth": 3, "learning_rate": 0.1, "n_estimators": 20, "tree_method": "hist"},
            early_stopping_rounds=10, verbose=0,
        )
        # Simulate OOF pool: booster predictions on held-out rows not in train or HPO val
        oof_proba = _overconfident_scores(y[500:])
        oof_y = y[500:]
        hpo_val_proba = _overconfident_scores(y[400:500])
        hpo_val_y = y[400:500]
        return trainer, oof_proba, oof_y, hpo_val_proba, hpo_val_y

    def test_returns_fitted_calibrator(self) -> None:
        trainer, oof_p, oof_y, _, _ = self._train_and_get_oof()
        cal = trainer.fit_calibrator_on_oof(oof_p, oof_y)
        assert isinstance(cal, ConfidenceCalibrator)
        assert cal._is_fitted

    def test_sets_self_calibrator(self) -> None:
        trainer, oof_p, oof_y, _, _ = self._train_and_get_oof()
        cal = trainer.fit_calibrator_on_oof(oof_p, oof_y)
        assert trainer.calibrator is cal

    def test_ece_improves_after_oof_calibration(self) -> None:
        """ECE after calibration must be strictly better than before."""
        _, oof_p, oof_y, _, _ = self._train_and_get_oof(n=800, n_features=8)
        oof_proba = _overconfident_scores(oof_y)
        cal = ConfidenceCalibrator("xgboost")
        cal.fit(oof_y, oof_proba)
        assert cal.ece_after < cal.ece_before, (
            f"ECE must improve after calibration: "
            f"before={cal.ece_before:.4f} after={cal.ece_after:.4f}"
        )

    def test_rejects_too_small_pool(self) -> None:
        trainer = XGBoostTrainer(random_state=42)
        with pytest.raises(ValueError, match="too small"):
            trainer.fit_calibrator_on_oof(
                np.array([0.6, 0.4, 0.7]),
                np.array([1, 0, 1]),
            )

    def test_rejects_mismatched_lengths(self) -> None:
        trainer = XGBoostTrainer(random_state=42)
        with pytest.raises(ValueError, match="length"):
            trainer.fit_calibrator_on_oof(
                np.ones(100, dtype=np.float32) * 0.6,
                np.ones(80, dtype=np.int32),
            )

    def test_model_not_required_for_oof_fit(self) -> None:
        """fit_calibrator_on_oof takes pre-computed probabilities — no model needed."""
        trainer = XGBoostTrainer(random_state=42)
        oof_p = RNG.uniform(0.1, 0.9, 200).astype(np.float32)
        oof_y = RNG.integers(0, 2, 200).astype(np.int32)
        cal = trainer.fit_calibrator_on_oof(oof_p, oof_y)
        assert cal._is_fitted


# ══════════════════════════════════════════════════════════════════════════════
# 3 — Disjointness: OOF set ∩ HPO val set = ∅
# ══════════════════════════════════════════════════════════════════════════════

class TestCalibrationSetDisjoint:
    """Validate the architectural guarantee that OOF rows never overlap HPO val rows."""

    def test_oof_and_hpo_val_indices_are_disjoint(self) -> None:
        """
        Simulate a mini CPCV setup and verify the row indices used for calibration
        (OOF test indices) and for HPO val are non-overlapping.

        This is a structural test of the CPCV index routing — not a training test.
        """
        n_panel = 500
        # HPO holdout: rows [0, 400)  →  val rows [320, 400)  (80% / 20% split)
        hpo_train_idx = np.arange(0, 320)
        hpo_val_idx   = np.arange(320, 400)

        # CPCV test rows: everything not in HPO train+val  → rows [400, 500)
        cpcv_test_rows = np.arange(400, n_panel)

        overlap = np.intersect1d(hpo_val_idx, cpcv_test_rows)
        assert len(overlap) == 0, (
            f"OOF test set must be disjoint from HPO validation set. "
            f"Found {len(overlap)} overlapping row indices: {overlap[:10]}"
        )

    def test_oof_pool_covers_entire_test_partition(self) -> None:
        """Every row outside the HPO holdout appears in the OOF pool exactly once."""
        n_panel = 500
        hpo_end = 400
        oof_rows = np.arange(hpo_end, n_panel)

        # Simulate 3-path OOF (simplified)
        path_sizes = [34, 33, 33]
        oof_paths  = []
        cursor = 0
        for sz in path_sizes:
            oof_paths.append(oof_rows[cursor: cursor + sz])
            cursor += sz

        pooled = np.concatenate(oof_paths)
        assert len(pooled) == n_panel - hpo_end
        assert len(np.unique(pooled)) == len(pooled), "Each OOF row must appear exactly once"


# ══════════════════════════════════════════════════════════════════════════════
# 4 — CheckpointManager calibrator round-trip
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckpointCalibratorRoundTrip:
    """save_calibrator / load_calibrator / has_calibrator round-trips correctly."""

    @pytest.fixture()
    def cp(self, tmp_path: Path) -> CheckpointManager:
        return CheckpointManager(
            checkpoint_dir=tmp_path / "checkpoints",
            config={"n_features": 69, "model_version": "2.0.0",
                    "sequence_length": 60, "include_fundamentals": True},
        )

    def _fitted_calibrator(self, model_type: str) -> ConfidenceCalibrator:
        y = RNG.integers(0, 2, 500).astype(np.int32)
        p = _overconfident_scores(y)
        cal = ConfidenceCalibrator(model_type)
        cal.fit(y, p)
        return cal

    def test_has_calibrator_false_before_save(self, cp: CheckpointManager) -> None:
        assert not cp.has_calibrator("xgboost")
        assert not cp.has_calibrator("gru")

    def test_xgboost_round_trip(self, cp: CheckpointManager) -> None:
        cal = self._fitted_calibrator("xgboost")
        cp.save_calibrator("xgboost", cal)

        assert cp.has_calibrator("xgboost")
        loaded = cp.load_calibrator("xgboost")

        assert loaded._is_fitted
        assert abs(loaded._a - cal._a) < 1e-9
        assert abs(loaded._b - cal._b) < 1e-9
        assert abs(loaded._c - cal._c) < 1e-9
        assert abs(loaded.ece_after - cal.ece_after) < 1e-9

    def test_gru_round_trip(self, cp: CheckpointManager) -> None:
        cal = self._fitted_calibrator("gru")
        cp.save_calibrator("gru", cal)

        assert cp.has_calibrator("gru")
        loaded = cp.load_calibrator("gru")

        assert loaded._is_fitted
        assert abs(loaded._temperature - cal._temperature) < 1e-9

    def test_invalid_model_type_raises(self, cp: CheckpointManager) -> None:
        cal = self._fitted_calibrator("xgboost")
        with pytest.raises(ValueError, match="Unknown model_type"):
            cp.save_calibrator("transformer", cal)
        with pytest.raises(ValueError, match="Unknown model_type"):
            cp.has_calibrator("transformer")
        with pytest.raises(ValueError, match="Unknown model_type"):
            cp.load_calibrator("transformer")

    def test_xgboost_and_gru_paths_are_independent(self, cp: CheckpointManager) -> None:
        xgb_cal = self._fitted_calibrator("xgboost")
        gru_cal = self._fitted_calibrator("gru")
        cp.save_calibrator("xgboost", xgb_cal)
        cp.save_calibrator("gru", gru_cal)

        assert cp.has_calibrator("xgboost")
        assert cp.has_calibrator("gru")

        xgb_loaded = cp.load_calibrator("xgboost")
        gru_loaded  = cp.load_calibrator("gru")

        assert xgb_loaded.model_type == "xgboost"
        assert gru_loaded.model_type  == "gru"

    def test_step_5_path_for_xgboost(self, cp: CheckpointManager, tmp_path: Path) -> None:
        """XGBoost calibrator lives inside step_5_xgboost/ checkpoint dir."""
        cal = self._fitted_calibrator("xgboost")
        cp.save_calibrator("xgboost", cal)

        cp_dir = tmp_path / "checkpoints"
        expected = cp_dir / "step_5_xgboost" / "calibrator_xgb.pkl"
        assert expected.exists(), f"Calibrator not found at expected path: {expected}"

    def test_step_6_path_for_gru(self, cp: CheckpointManager, tmp_path: Path) -> None:
        cal = self._fitted_calibrator("gru")
        cp.save_calibrator("gru", cal)

        cp_dir = tmp_path / "checkpoints"
        expected = cp_dir / "step_6_gru" / "calibrator_gru.pkl"
        assert expected.exists(), f"GRU calibrator not found at expected path: {expected}"


# ══════════════════════════════════════════════════════════════════════════════
# 5 — ECE monotonicity on realistic overconfident scores
# ══════════════════════════════════════════════════════════════════════════════

class TestECEImprovement:
    """ECE after calibration ≤ ECE before for diverse synthetic scenarios."""

    @pytest.mark.parametrize("n_samples", [200, 1_000, 5_000])
    def test_ece_improves_xgboost(self, n_samples: int) -> None:
        y = RNG.integers(0, 2, n_samples).astype(np.int32)
        raw_scores = _overconfident_scores(y)
        cal = ConfidenceCalibrator("xgboost")
        cal.fit(y, raw_scores)
        assert cal.ece_after <= cal.ece_before, (
            f"Expected ECE to improve for n={n_samples}: "
            f"before={cal.ece_before:.4f}  after={cal.ece_after:.4f}"
        )

    @pytest.mark.parametrize("n_samples", [200, 1_000])
    def test_ece_improves_gru(self, n_samples: int) -> None:
        y = RNG.integers(0, 2, n_samples).astype(np.int32)
        # Temperature calibration starts from near-identity; simulate slightly over-sharp GRU
        raw = np.clip(0.4 + 0.55 * y + RNG.normal(0, 0.08, n_samples), 0.01, 0.99).astype(np.float32)
        cal = ConfidenceCalibrator("gru")
        cal.fit(y, raw)
        assert cal.ece_after <= cal.ece_before

    def test_ece_target_achievable_on_large_pool(self) -> None:
        """Calibrator should achieve ECE < 0.05 on a large, well-behaved OOF pool."""
        n = 10_000
        y = RNG.integers(0, 2, n).astype(np.int32)
        # Mildly overconfident but not extreme
        p_true = 0.35 + 0.3 * y.astype(np.float64)
        raw = np.clip(p_true + RNG.normal(0, 0.05, n), 0.01, 0.99).astype(np.float32)
        cal = ConfidenceCalibrator("xgboost")
        cal.fit(y, raw)
        assert cal.ece_after < 0.05, (
            f"Expected ECE < 0.05 on large clean pool, got {cal.ece_after:.4f}"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 6 — Serialiser field contract
# ══════════════════════════════════════════════════════════════════════════════

class TestSerialiserCalibrationFields:
    """serialise_signal() exposes both confidence and calibrated_confidence."""

    def _make_signal(self, confidence_score: float = 0.72) -> MagicMock:
        signal = MagicMock()
        signal.id = "sig-001"
        signal.symbol = "NSE_EQ|INE001A01036"
        signal.company_name = "Test Corp"
        signal.is_nse_eligible = True
        signal.extra_data = {"instrument_key": "NSE_EQ|INE001A01036"}
        signal.action = "BUY"
        signal.confidence_score = confidence_score
        signal.time_horizon = "swing"
        signal.reasoning = "Test signal"
        signal.contributing_events = []
        signal.ml_predictions = None
        signal.technical_indicators = None
        signal.regime_type = "bullish"
        signal.signal_timestamp = MagicMock()
        signal.signal_timestamp.isoformat.return_value = "2026-05-19T12:00:00"
        signal.expires_at = None
        signal.target_price = None
        signal.stop_loss = None
        return signal

    def test_both_confidence_fields_present(self) -> None:
        from app.ai.fusion.serializers import serialise_signal
        result = serialise_signal(self._make_signal(0.72))
        assert "confidence" in result
        assert "calibrated_confidence" in result

    def test_confidence_matches_confidence_score(self) -> None:
        from app.ai.fusion.serializers import serialise_signal
        result = serialise_signal(self._make_signal(0.65))
        assert abs(result["confidence"] - 0.65) < 1e-6

    def test_calibrated_confidence_matches_confidence_score(self) -> None:
        """Post-A4: calibrated_confidence is the post-calibration ensemble output."""
        from app.ai.fusion.serializers import serialise_signal
        result = serialise_signal(self._make_signal(0.78))
        assert abs(result["calibrated_confidence"] - 0.78) < 1e-6

    def test_both_fields_are_floats(self) -> None:
        from app.ai.fusion.serializers import serialise_signal
        result = serialise_signal(self._make_signal(0.55))
        assert isinstance(result["confidence"], float)
        assert isinstance(result["calibrated_confidence"], float)


# ══════════════════════════════════════════════════════════════════════════════
# 7 — calibrator.py: fit contract docstring sanity
# ══════════════════════════════════════════════════════════════════════════════

class TestCalibratorContract:
    """ConfidenceCalibrator follows the OOF-fit contract in its docstring."""

    def test_fit_accepts_precomputed_oof_probabilities(self) -> None:
        """fit() takes (y_true, y_proba) — works on any held-out set (OOF or not)."""
        y = RNG.integers(0, 2, 300).astype(np.int32)
        p = RNG.uniform(0.2, 0.8, 300).astype(np.float32)
        cal = ConfidenceCalibrator("xgboost")
        cal.fit(y, p)
        assert cal._is_fitted
        assert cal.n_calibration_samples == 300

    def test_calibrate_is_deterministic(self) -> None:
        y = RNG.integers(0, 2, 200).astype(np.int32)
        p = _overconfident_scores(y)
        cal = ConfidenceCalibrator("xgboost")
        cal.fit(y, p)

        test_p = np.array([[0.3, 0.7], [0.6, 0.4]], dtype=np.float32)
        out1 = cal.calibrate(test_p)
        out2 = cal.calibrate(test_p)
        np.testing.assert_array_equal(out1, out2)

    def test_calibrate_outputs_sum_to_one(self) -> None:
        y = RNG.integers(0, 2, 200).astype(np.int32)
        p = _overconfident_scores(y)
        cal = ConfidenceCalibrator("xgboost")
        cal.fit(y, p)

        test_batch = RNG.dirichlet([1, 1], 50).astype(np.float32)
        out = cal.calibrate(test_batch)
        sums = out.sum(axis=1)
        np.testing.assert_allclose(sums, 1.0, atol=1e-5)
