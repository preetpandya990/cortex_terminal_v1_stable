"""
A1 regression tests — fail-loud returns / no silent Sharpe→accuracy downgrade.

Each test asserts the post-A1 contract and would have FAILED on pre-A1 `main`:
  * pre-A1 `CheckpointManager` only *warned* on stale/incompatible checkpoints
    and resumed anyway → here they must raise `StaleCheckpointError`.
  * pre-A1 `ModelEvaluator.evaluate(returns=None)` fabricated `0.0` financial
    metrics → here they must be `None` ("not computed").

Guards RC-1: a stale checkpoint (missing forward returns) silently degraded
ensemble optimisation from Sharpe to accuracy and shipped 0.0 financials.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from app.ml.training.checkpoint_manager import CheckpointManager, SCHEMA_VERSION
from app.ml.training.evaluator import ModelEvaluator
from app.ml.training.exceptions import StaleCheckpointError, MissingReturnsError


def _cfg(**overrides):
    base = {
        "n_features": 69,
        "sequence_length": 60,
        "include_fundamentals": True,
        "model_version": "1.0.0",
        "n_symbols": 100,           # model-affecting since WS2 (smoke-run isolation)
        "xgboost_trials": 100,      # operational (non-model-affecting) key
    }
    base.update(overrides)
    return base


def _write_checkpoint(dir_: Path, state: dict) -> None:
    (dir_ / "checkpoint.json").write_text(json.dumps(state))


class TestCheckpointSchemaGate:
    def test_fresh_checkpoint_writes_schema_version(self, tmp_path):
        CheckpointManager(tmp_path, _cfg(), fresh=True)
        saved = json.loads((tmp_path / "checkpoint.json").read_text())
        assert saved["schema_version"] == SCHEMA_VERSION

    def test_matching_checkpoint_resumes_cleanly(self, tmp_path):
        CheckpointManager(tmp_path, _cfg(), fresh=True)
        # Same schema + same model-affecting config → resume must NOT raise.
        CheckpointManager(tmp_path, _cfg(), fresh=False)

    def test_pre_v2_checkpoint_without_schema_version_aborts(self, tmp_path):
        _write_checkpoint(tmp_path, {
            "run_id": "legacy", "completed_steps": [], "config": _cfg(),
        })  # no "schema_version" — exactly the stale-checkpoint shape (RC-1)
        with pytest.raises(StaleCheckpointError, match="schema_version"):
            CheckpointManager(tmp_path, _cfg(), fresh=False)

    def test_schema_version_mismatch_aborts(self, tmp_path):
        _write_checkpoint(tmp_path, {
            "run_id": "old", "schema_version": SCHEMA_VERSION - 1,
            "completed_steps": [], "config": _cfg(),
        })
        with pytest.raises(StaleCheckpointError):
            CheckpointManager(tmp_path, _cfg(), fresh=False)

    def test_model_affecting_config_drift_aborts(self, tmp_path):
        _write_checkpoint(tmp_path, {
            "run_id": "r", "schema_version": SCHEMA_VERSION,
            "completed_steps": [], "config": _cfg(n_features=49),
        })
        with pytest.raises(StaleCheckpointError, match="n_features"):
            CheckpointManager(tmp_path, _cfg(n_features=69), fresh=False)

    def test_non_model_affecting_drift_does_not_abort(self, tmp_path):
        _write_checkpoint(tmp_path, {
            "run_id": "r", "schema_version": SCHEMA_VERSION,
            "completed_steps": [], "config": _cfg(xgboost_trials=100),
        })
        # xgboost_trials is operational, not model-affecting → warn-only, no raise.
        CheckpointManager(tmp_path, _cfg(xgboost_trials=50), fresh=False)

    def test_n_symbols_drift_aborts_since_ws2(self, tmp_path):
        """WS2 made n_symbols model-affecting: a --n-symbols smoke checkpoint
        must never resume into (or be resumed by) a full-universe run."""
        _write_checkpoint(tmp_path, {
            "run_id": "r", "schema_version": SCHEMA_VERSION,
            "completed_steps": [], "config": _cfg(n_symbols=20),
        })
        with pytest.raises(StaleCheckpointError, match="n_symbols"):
            CheckpointManager(tmp_path, _cfg(n_symbols=2551), fresh=False)


class TestEvaluatorHonestFinancials:
    @staticmethod
    def _synthetic(n=200):
        rng = np.random.default_rng(42)
        y_true = rng.integers(0, 2, n)
        y_pred = rng.integers(0, 2, n)
        p1 = rng.random(n)
        y_proba = np.column_stack([1 - p1, p1])
        return y_true, y_pred, y_proba

    def test_missing_returns_yields_none_not_zero(self):
        y_true, y_pred, y_proba = self._synthetic()
        res = ModelEvaluator().evaluate(y_true, y_pred, y_proba, returns=None)
        # Pre-A1 these were fabricated 0.0 — now they must be None.
        assert res.sharpe_ratio is None
        assert res.win_rate is None
        assert res.profit_factor is None
        assert res.n_trades is None
        # Classification metrics are still computed.
        assert isinstance(res.accuracy, float)

    def test_returns_present_yields_real_floats(self):
        y_true, y_pred, y_proba = self._synthetic()
        rng = np.random.default_rng(7)
        returns = rng.normal(0, 0.01, len(y_true))
        res = ModelEvaluator().evaluate(y_true, y_pred, y_proba, returns=returns)
        assert isinstance(res.sharpe_ratio, float)
        assert isinstance(res.n_trades, int)

    def test_print_results_none_safe(self, capsys):
        y_true, y_pred, y_proba = self._synthetic()
        res = ModelEvaluator().evaluate(y_true, y_pred, y_proba, returns=None)
        ModelEvaluator().print_results(res)  # pre-A1: TypeError on None:.4f
        assert "n/a (no returns)" in capsys.readouterr().out
