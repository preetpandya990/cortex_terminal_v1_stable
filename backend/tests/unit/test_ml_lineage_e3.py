"""
E3 — Observability: MLflow lineage + structured run logs.

Acceptance criteria (from ML_IMPLEMENTATION_TASKS.md):
    * Every training run discoverable in ./mlruns with full lineage.
    * Promotion Report is the single sign-off artefact.
    * Structured per-step run log written to {checkpoint_dir}/run_log.ndjson.
    * MLflow run_id embedded in the C2 Promotion Report body (covered by bundle SHA-256).

Design invariants verified here:
    * All MLflow calls are non-fatal — a broken mlflow install never aborts training.
    * Resumed runs reconnect to the same MLflow run (stable run_id via checkpoint state).
    * The structured log is independent of MLflow (file I/O only; parseable offline).
    * mlflow_run_id in the promotion report changes the bundle SHA-256 if tampered.
    * SCHEMA_VERSION remains 4 — adding mlflow_run_id to the checkpoint state is
      backward-compatible (not a model-affecting key).
"""
from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call
import pytest

# ── helpers ────────────────────────────────────────────────────────────────────

def _make_checkpoint_manager(tmp_path: Path, fresh: bool = True):
    """Return a real CheckpointManager wired to a temp directory."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    from app.ml.training.checkpoint_manager import CheckpointManager
    config = {
        "n_features": 69, "sequence_length": 60,
        "include_fundamentals": True, "model_version": "1.0.0",
    }
    return CheckpointManager(tmp_path / "cp", config=config, fresh=fresh)


def _make_training_config():
    """Return the TrainingConfig dataclass from the orchestrator module."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
    from production_training_orchestrator import TrainingConfig
    return TrainingConfig(n_symbols=10, lookback_years=1, model_version="1.0.0", feature_set_version="1.0.0")


# ══════════════════════════════════════════════════════════════════════════════
# TestCheckpointMLflowRunId
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckpointMLflowRunId:
    """CheckpointManager correctly persists and exposes the MLflow run_id."""

    def test_mlflow_run_id_absent_on_fresh_checkpoint(self, tmp_path):
        cp = _make_checkpoint_manager(tmp_path)
        assert cp.mlflow_run_id is None

    def test_save_mlflow_run_id_persists(self, tmp_path):
        cp = _make_checkpoint_manager(tmp_path)
        cp.save_mlflow_run_id("abc-123-def")
        assert cp.mlflow_run_id == "abc-123-def"

    def test_mlflow_run_id_survives_reload(self, tmp_path):
        from app.ml.training.checkpoint_manager import CheckpointManager
        config = {
            "n_features": 69, "sequence_length": 60,
            "include_fundamentals": True, "model_version": "1.0.0",
        }
        cp1 = CheckpointManager(tmp_path / "cp", config=config, fresh=True)
        cp1.save_mlflow_run_id("run-xyz-789")

        # Reload from disk (simulates orchestrator restart)
        cp2 = CheckpointManager(tmp_path / "cp", config=config, fresh=False)
        assert cp2.mlflow_run_id == "run-xyz-789"

    def test_save_mlflow_run_id_triggers_flush(self, tmp_path):
        cp = _make_checkpoint_manager(tmp_path)
        cp.save_mlflow_run_id("flush-test")
        state = json.loads((tmp_path / "cp" / "checkpoint.json").read_text())
        assert state["mlflow_run_id"] == "flush-test"

    def test_mlflow_run_id_not_a_model_affecting_key(self, tmp_path):
        """Adding mlflow_run_id never triggers StaleCheckpointError on resume."""
        from app.ml.training.checkpoint_manager import CheckpointManager, SCHEMA_VERSION
        config = {
            "n_features": 69, "sequence_length": 60,
            "include_fundamentals": True, "model_version": "1.0.0",
        }
        cp1 = CheckpointManager(tmp_path / "cp", config=config, fresh=True)
        cp1.save_mlflow_run_id("no-compat-break")
        # Resume must succeed without raising StaleCheckpointError
        cp2 = CheckpointManager(tmp_path / "cp", config=config, fresh=False)
        assert cp2.mlflow_run_id == "no-compat-break"

    def test_schema_version_unchanged(self, tmp_path):
        # v4 was E3's contract; v5 is the deliberate WS2 bump (feature-set
        # versioning — see checkpoint_manager's SCHEMA_VERSION changelog).
        # This test still guards against ACCIDENTAL bumps: update it only
        # alongside a documented schema change.
        from app.ml.training.checkpoint_manager import SCHEMA_VERSION
        assert SCHEMA_VERSION == 5, "SCHEMA_VERSION must only change with a documented migration"


# ══════════════════════════════════════════════════════════════════════════════
# TestSetupMLflow
# ══════════════════════════════════════════════════════════════════════════════

class TestSetupMLflow:
    """_setup_mlflow creates the MLflow run, logs params, persists run_id."""

    def _make_mock_orchestrator(self, tmp_path):
        """Return a lightweight mock orchestrator with real CheckpointManager."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
        # Import only the constants + standalone methods we need
        from production_training_orchestrator import (
            _MLRUNS_DIR, _MLFLOW_EXPERIMENT, _STEP_INDEX,
        )
        config = _make_training_config()
        cp = _make_checkpoint_manager(tmp_path)

        # Build a minimal orchestrator-like object without hitting the DB
        orch = MagicMock()
        orch.cp = cp
        orch.config = config
        orch._e3_mlflow_run_id = None
        orch._e3_promotion_report_path = None

        # Bind real methods to the mock
        import production_training_orchestrator as _mod
        orch._setup_mlflow = _mod.ProductionTrainingOrchestrator._setup_mlflow.__get__(orch)
        orch._write_run_log_entry = _mod.ProductionTrainingOrchestrator._write_run_log_entry.__get__(orch)
        return orch

    def test_mlflow_run_id_set_on_fresh_run(self, tmp_path):
        orch = self._make_mock_orchestrator(tmp_path)
        with patch("mlflow.set_tracking_uri"), \
             patch("mlflow.set_experiment"), \
             patch("mlflow.start_run") as mock_start, \
             patch("mlflow.log_params"):
            mock_run = MagicMock()
            mock_run.info.run_id = "fresh-run-id"
            mock_start.return_value.__enter__ = lambda s: mock_run
            mock_start.return_value = mock_run
            orch._setup_mlflow()
        assert orch._e3_mlflow_run_id == "fresh-run-id"

    def test_mlflow_run_id_persisted_to_checkpoint(self, tmp_path):
        orch = self._make_mock_orchestrator(tmp_path)
        with patch("mlflow.set_tracking_uri"), \
             patch("mlflow.set_experiment"), \
             patch("mlflow.start_run") as mock_start, \
             patch("mlflow.log_params"):
            mock_run = MagicMock()
            mock_run.info.run_id = "persist-test-id"
            mock_start.return_value = mock_run
            orch._setup_mlflow()
        assert orch.cp.mlflow_run_id == "persist-test-id"

    def test_params_logged_on_fresh_run(self, tmp_path):
        orch = self._make_mock_orchestrator(tmp_path)
        with patch("mlflow.set_tracking_uri"), \
             patch("mlflow.set_experiment"), \
             patch("mlflow.start_run") as mock_start, \
             patch("mlflow.log_params") as mock_params:
            mock_run = MagicMock()
            mock_run.info.run_id = "params-test"
            mock_start.return_value = mock_run
            orch._setup_mlflow()
        assert mock_params.called
        logged = mock_params.call_args[0][0]
        assert "model_version" in logged
        assert "n_symbols" in logged

    def test_params_all_strings(self, tmp_path):
        orch = self._make_mock_orchestrator(tmp_path)
        with patch("mlflow.set_tracking_uri"), \
             patch("mlflow.set_experiment"), \
             patch("mlflow.start_run") as mock_start, \
             patch("mlflow.log_params") as mock_params:
            mock_run = MagicMock()
            mock_run.info.run_id = "str-params"
            mock_start.return_value = mock_run
            orch._setup_mlflow()
        logged = mock_params.call_args[0][0]
        for k, v in logged.items():
            assert isinstance(v, str), f"param {k!r} value {v!r} is not a string"

    def test_resume_skips_param_logging(self, tmp_path):
        cp = _make_checkpoint_manager(tmp_path)
        cp.save_mlflow_run_id("existing-run-id")
        import production_training_orchestrator as _mod
        orch = MagicMock()
        orch.cp = cp
        orch.config = _make_training_config()
        orch._e3_mlflow_run_id = None
        orch._setup_mlflow = _mod.ProductionTrainingOrchestrator._setup_mlflow.__get__(orch)
        orch._write_run_log_entry = _mod.ProductionTrainingOrchestrator._write_run_log_entry.__get__(orch)
        with patch("mlflow.set_tracking_uri"), \
             patch("mlflow.set_experiment"), \
             patch("mlflow.start_run") as mock_start, \
             patch("mlflow.log_params") as mock_params:
            mock_run = MagicMock()
            mock_run.info.run_id = "existing-run-id"
            mock_start.return_value = mock_run
            orch._setup_mlflow()
        # On resume, log_params must NOT be called
        assert not mock_params.called

    def test_setup_mlflow_non_fatal_on_import_error(self, tmp_path):
        """A broken mlflow install must never abort _setup_mlflow."""
        orch = self._make_mock_orchestrator(tmp_path)
        with patch("builtins.__import__", side_effect=ImportError("no mlflow")):
            # Should not raise
            try:
                orch._setup_mlflow()
            except ImportError:
                pytest.fail("_setup_mlflow must catch ImportError and log a warning")

    def test_tracking_uri_uses_absolute_mlruns_path(self, tmp_path):
        orch = self._make_mock_orchestrator(tmp_path)
        with patch("mlflow.set_tracking_uri") as mock_uri, \
             patch("mlflow.set_experiment"), \
             patch("mlflow.start_run") as mock_start, \
             patch("mlflow.log_params"):
            mock_run = MagicMock()
            mock_run.info.run_id = "uri-test"
            mock_start.return_value = mock_run
            orch._setup_mlflow()
        uri_arg = mock_uri.call_args[0][0]
        assert uri_arg.startswith("file://"), f"URI must be file:// scheme, got {uri_arg!r}"
        assert "mlruns" in uri_arg


# ══════════════════════════════════════════════════════════════════════════════
# TestStructuredRunLog
# ══════════════════════════════════════════════════════════════════════════════

class TestStructuredRunLog:
    """_write_run_log_entry appends parseable NDJSON to {checkpoint_dir}/run_log.ndjson."""

    def _make_orch_for_log(self, tmp_path):
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
        import production_training_orchestrator as _mod
        orch = MagicMock()
        orch.cp = _make_checkpoint_manager(tmp_path)
        orch._write_run_log_entry = _mod.ProductionTrainingOrchestrator._write_run_log_entry.__get__(orch)
        return orch

    def test_run_log_file_created(self, tmp_path):
        orch = self._make_orch_for_log(tmp_path)
        orch._write_run_log_entry({"event": "test"})
        log_path = orch.cp._dir / "run_log.ndjson"
        assert log_path.exists()

    def test_run_log_entries_are_valid_json(self, tmp_path):
        orch = self._make_orch_for_log(tmp_path)
        orch._write_run_log_entry({"event": "step_complete", "step": "step_1_symbols", "duration_s": 3.5})
        orch._write_run_log_entry({"event": "run_complete"})
        log_path = orch.cp._dir / "run_log.ndjson"
        entries = [json.loads(line) for line in log_path.read_text().strip().splitlines()]
        assert len(entries) == 2

    def test_run_log_entries_have_timestamp(self, tmp_path):
        orch = self._make_orch_for_log(tmp_path)
        orch._write_run_log_entry({"event": "test_ts"})
        log_path = orch.cp._dir / "run_log.ndjson"
        entry = json.loads(log_path.read_text().strip())
        assert "ts" in entry
        # Must be an ISO-8601 string
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(entry["ts"])
        assert dt.tzinfo is not None

    def test_run_log_entry_contains_original_fields(self, tmp_path):
        orch = self._make_orch_for_log(tmp_path)
        orch._write_run_log_entry({"event": "step_complete", "step": "step_5_xgboost", "duration_s": 1800.0, "metrics": {"xgb_best_rounds": 500}})
        log_path = orch.cp._dir / "run_log.ndjson"
        entry = json.loads(log_path.read_text().strip())
        assert entry["event"] == "step_complete"
        assert entry["step"] == "step_5_xgboost"
        assert entry["duration_s"] == 1800.0

    def test_run_log_is_appendable(self, tmp_path):
        orch = self._make_orch_for_log(tmp_path)
        for i in range(5):
            orch._write_run_log_entry({"event": "step", "i": i})
        log_path = orch.cp._dir / "run_log.ndjson"
        entries = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        assert len(entries) == 5
        assert [e["i"] for e in entries] == list(range(5))

    def test_run_log_non_fatal_on_write_error(self, tmp_path):
        """A write failure must not propagate — training must never abort from log I/O."""
        orch = self._make_orch_for_log(tmp_path)
        with patch("builtins.open", side_effect=OSError("disk full")):
            try:
                orch._write_run_log_entry({"event": "test"})
            except OSError:
                pytest.fail("_write_run_log_entry must catch OSError")


# ══════════════════════════════════════════════════════════════════════════════
# TestLogMLflowStepDone
# ══════════════════════════════════════════════════════════════════════════════

class TestLogMLflowStepDone:
    """_log_mlflow_step_done logs the step duration metric and writes a structured log entry."""

    def _make_orch(self, tmp_path):
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
        import production_training_orchestrator as _mod
        orch = MagicMock()
        orch.cp = _make_checkpoint_manager(tmp_path)
        orch._e3_mlflow_run_id = "mock-run-id"
        orch._log_mlflow_step_done = _mod.ProductionTrainingOrchestrator._log_mlflow_step_done.__get__(orch)
        orch._write_run_log_entry = _mod.ProductionTrainingOrchestrator._write_run_log_entry.__get__(orch)
        return orch

    def test_duration_metric_logged_to_mlflow(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with patch("mlflow.log_metric") as mock_metric, \
             patch("mlflow.log_metrics"):
            orch._log_mlflow_step_done("step_1_symbols", 4.5)
        mock_metric.assert_called_once_with("pipeline.step_duration_s", 4.5, step=1)

    def test_step_index_correct(self, tmp_path):
        orch = self._make_orch(tmp_path)
        from production_training_orchestrator import _STEP_INDEX
        with patch("mlflow.log_metric") as mock_metric, \
             patch("mlflow.log_metrics"):
            orch._log_mlflow_step_done("step_10_registry", 30.0)
        _, kwargs = mock_metric.call_args
        assert kwargs.get("step") == 10

    def test_extra_metrics_logged(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with patch("mlflow.log_metric"), \
             patch("mlflow.log_metrics") as mock_metrics:
            orch._log_mlflow_step_done("step_1_symbols", 4.5, {"n_symbols": 2200.0})
        mock_metrics.assert_called_once()
        logged = mock_metrics.call_args[0][0]
        assert "n_symbols" in logged

    def test_structured_log_entry_written(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with patch("mlflow.log_metric"), patch("mlflow.log_metrics"):
            orch._log_mlflow_step_done("step_5_xgboost", 600.0, {"xgb_best_rounds": 400.0})
        log_path = orch.cp._dir / "run_log.ndjson"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["event"] == "step_complete"
        assert entry["step"] == "step_5_xgboost"

    def test_nan_metrics_excluded(self, tmp_path):
        orch = self._make_orch(tmp_path)
        import math
        with patch("mlflow.log_metric"), \
             patch("mlflow.log_metrics") as mock_metrics:
            orch._log_mlflow_step_done("step_5_xgboost", 10.0, {"good": 1.0, "bad": float("nan")})
        if mock_metrics.called:
            logged = mock_metrics.call_args[0][0]
            assert "bad" not in logged

    def test_mlflow_failure_non_fatal(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with patch("mlflow.log_metric", side_effect=RuntimeError("mlflow broken")):
            try:
                orch._log_mlflow_step_done("step_1_symbols", 1.0)
            except RuntimeError:
                pytest.fail("_log_mlflow_step_done must catch mlflow errors")


# ══════════════════════════════════════════════════════════════════════════════
# TestFinalizeMLflow
# ══════════════════════════════════════════════════════════════════════════════

class TestFinalizeMLflow:
    """_finalize_mlflow logs artifacts, updates status tag, ends the run."""

    def _make_orch(self, tmp_path):
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
        import production_training_orchestrator as _mod
        orch = MagicMock()
        orch.cp = _make_checkpoint_manager(tmp_path)
        orch.config = _make_training_config()
        orch._e3_mlflow_run_id = "finalize-run-id"
        orch._finalize_mlflow = _mod.ProductionTrainingOrchestrator._finalize_mlflow.__get__(orch)
        orch._write_run_log_entry = _mod.ProductionTrainingOrchestrator._write_run_log_entry.__get__(orch)
        return orch

    def test_end_run_called_with_finished(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with patch("mlflow.set_tag"), \
             patch("mlflow.log_artifact"), \
             patch("mlflow.log_dict"), \
             patch("mlflow.end_run") as mock_end:
            orch._finalize_mlflow("FINISHED")
        mock_end.assert_called_once_with(status="FINISHED")

    def test_end_run_called_with_failed(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with patch("mlflow.set_tag"), \
             patch("mlflow.log_artifact"), \
             patch("mlflow.log_dict"), \
             patch("mlflow.end_run") as mock_end:
            orch._finalize_mlflow("FAILED")
        mock_end.assert_called_once_with(status="FAILED")

    def test_status_tag_updated(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with patch("mlflow.set_tag") as mock_tag, \
             patch("mlflow.log_artifact"), \
             patch("mlflow.log_dict"), \
             patch("mlflow.end_run"):
            orch._finalize_mlflow("FINISHED")
        mock_tag.assert_called_with("pipeline.status", "finished")

    def test_training_config_logged_as_artifact(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with patch("mlflow.set_tag"), \
             patch("mlflow.log_artifact"), \
             patch("mlflow.log_dict") as mock_dict, \
             patch("mlflow.end_run"):
            orch._finalize_mlflow("FINISHED")
        assert mock_dict.called
        logged_obj = mock_dict.call_args[0][0]
        assert "model_version" in logged_obj

    def test_promotion_report_logged_when_present(self, tmp_path):
        orch = self._make_orch(tmp_path)
        report_path = tmp_path / "report.json"
        report_path.write_text('{"status": "ok"}')
        with patch("mlflow.set_tag"), \
             patch("mlflow.log_artifact") as mock_artifact, \
             patch("mlflow.log_dict"), \
             patch("mlflow.end_run"):
            orch._finalize_mlflow("FINISHED", promotion_report_path=str(report_path))
        paths_logged = [call_args[0][0] for call_args in mock_artifact.call_args_list]
        assert str(report_path) in paths_logged

    def test_structured_log_artifact_logged(self, tmp_path):
        orch = self._make_orch(tmp_path)
        # Create the structured log file so it exists
        run_log = orch.cp._dir / "run_log.ndjson"
        run_log.write_text('{"ts":"2026-01-01T00:00:00+00:00","event":"test"}\n')
        with patch("mlflow.set_tag"), \
             patch("mlflow.log_artifact") as mock_artifact, \
             patch("mlflow.log_dict"), \
             patch("mlflow.end_run"):
            orch._finalize_mlflow("FINISHED")
        paths_logged = [call_args[0][0] for call_args in mock_artifact.call_args_list]
        assert str(run_log) in paths_logged

    def test_finalize_non_fatal_on_mlflow_error(self, tmp_path):
        orch = self._make_orch(tmp_path)
        with patch("mlflow.set_tag", side_effect=RuntimeError("tag error")):
            try:
                orch._finalize_mlflow("FINISHED")
            except RuntimeError:
                pytest.fail("_finalize_mlflow must catch all mlflow errors")

    def test_structured_log_written_regardless_of_mlflow(self, tmp_path):
        """The run_failed entry must be written even when MLflow is broken."""
        orch = self._make_orch(tmp_path)
        with patch("mlflow.set_tag", side_effect=RuntimeError("broken")):
            orch._finalize_mlflow("FAILED")
        log_path = orch.cp._dir / "run_log.ndjson"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["event"] == "run_failed"


# ══════════════════════════════════════════════════════════════════════════════
# TestPromotionReportMLflowRunId
# ══════════════════════════════════════════════════════════════════════════════

class TestPromotionReportMLflowRunId:
    """build_promotion_report includes mlflow_run_id in the signed body."""

    def _build_report(self, tmp_path: Path, mlflow_run_id=None) -> dict:
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent))
        from app.ml.evaluation.promotion_report import build_promotion_report
        from unittest.mock import MagicMock

        challenger = MagicMock()
        challenger.model_version = "1.1.0_xgboost"
        challenger.model_name    = "xgboost"
        challenger.status        = "development"
        challenger.training_samples = 100_000
        challenger.training_metrics = {
            "auc_pr": 0.6, "deflated_sharpe": 0.5,
            "ece_after": 0.03, "accuracy": 0.62,
        }
        challenger.training_features = ["f1", "f2"]
        challenger.onnx_path = None
        challenger.lineage   = {}

        return build_promotion_report(
            challengers={"xgboost": challenger},
            champions={"xgboost": None},
            report_dir=tmp_path,
            run_id="cp-run-id",
            mlflow_run_id=mlflow_run_id,
        )

    def test_mlflow_run_id_present_in_report(self, tmp_path):
        report = self._build_report(tmp_path, mlflow_run_id="mlf-abc-123")
        assert report.get("mlflow_run_id") == "mlf-abc-123"

    def test_mlflow_run_id_empty_string_when_none(self, tmp_path):
        report = self._build_report(tmp_path, mlflow_run_id=None)
        assert report.get("mlflow_run_id") == ""

    def test_mlflow_run_id_covered_by_bundle_sha(self, tmp_path):
        """Changing mlflow_run_id changes the bundle_sha256 — it is in the signed body."""
        from app.ml.evaluation.promotion_report import load_and_verify_report
        report_with_id = self._build_report(tmp_path / "r1", mlflow_run_id="original")
        sha_with_id = report_with_id["bundle_sha256"]

        report_without_id = self._build_report(tmp_path / "r2", mlflow_run_id=None)
        sha_without_id = report_without_id["bundle_sha256"]

        assert sha_with_id != sha_without_id, (
            "mlflow_run_id is NOT in the signed body — tamper would be undetected"
        )

    def test_report_verifies_with_mlflow_run_id(self, tmp_path):
        from app.ml.evaluation.promotion_report import load_and_verify_report
        report = self._build_report(tmp_path, mlflow_run_id="verify-me")
        path = report["report_path"]
        # Verification must succeed (no BundleChecksumError)
        verified = load_and_verify_report(
            path,
            challenger_version="1.1.0_xgboost",
            enforce_status=False,
        )
        assert verified["mlflow_run_id"] == "verify-me"

    def test_tampered_mlflow_run_id_breaks_bundle(self, tmp_path):
        from app.ml.evaluation.promotion_report import (
            load_and_verify_report, BundleChecksumError,
        )
        report = self._build_report(tmp_path, mlflow_run_id="original")
        path = Path(report["report_path"])
        # Tamper with the mlflow_run_id in the written file
        content = json.loads(path.read_text())
        content["mlflow_run_id"] = "tampered"
        path.write_text(json.dumps(content))
        with pytest.raises(BundleChecksumError):
            load_and_verify_report(path, challenger_version="1.1.0_xgboost", enforce_status=False)


# ══════════════════════════════════════════════════════════════════════════════
# TestMLflowExperimentConstants
# ══════════════════════════════════════════════════════════════════════════════

class TestMLflowExperimentConstants:
    """Module-level E3 constants are correctly defined."""

    def test_mlruns_dir_is_absolute(self):
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))
        from production_training_orchestrator import _MLRUNS_DIR
        assert _MLRUNS_DIR.is_absolute()

    def test_mlruns_dir_inside_backend(self):
        from production_training_orchestrator import _MLRUNS_DIR
        # mlruns must live inside the backend directory (operator-predictable)
        assert "backend" in str(_MLRUNS_DIR) or "mlruns" in _MLRUNS_DIR.name

    def test_mlflow_experiment_name_non_empty(self):
        from production_training_orchestrator import _MLFLOW_EXPERIMENT
        assert _MLFLOW_EXPERIMENT and isinstance(_MLFLOW_EXPERIMENT, str)

    def test_step_index_covers_all_10_steps(self):
        from production_training_orchestrator import _STEP_INDEX
        from app.ml.training.checkpoint_manager import PIPELINE_STEPS
        for step in PIPELINE_STEPS:
            assert step in _STEP_INDEX, f"{step!r} missing from _STEP_INDEX"

    def test_step_index_values_one_based(self):
        from production_training_orchestrator import _STEP_INDEX
        values = sorted(_STEP_INDEX.values())
        assert values == list(range(1, 11)), "Step indices must be 1-based consecutive integers"
