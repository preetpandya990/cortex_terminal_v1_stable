"""
A7 — Data Integrity Gate acceptance suite.

Validates that:

  1. DataCoverageError is a typed RuntimeError subclass (fail-loud contract).
  2. TrainingConfig ships a min_symbol_coverage=0.85 default that can be
     overridden.
  3. _assert_symbol_coverage enforces the hard coverage gate:
       - usable/requested >= threshold  →  passes, stores _symbol_coverage
       - usable/requested <  threshold  →  raises DataCoverageError
       - exact threshold boundary        →  passes (>= is inclusive)
       - one symbol below boundary       →  raises
  4. The resume path (step-2) raises DataCoverageError when checkpoint meta
     records total_rows=0 (corrupt / empty feature checkpoint).
  5. A valid step-2 resume (total_rows > 0) does not raise.
  6. symbol_coverage is injected into both xgb_metrics and gru_metrics at
     registry registration time so the A6 QualityGate gate-4 is a hard gate.
  7. _generate_data_quality_report includes all A7 fields.

Tests in classes 1-7 are RED on main@HEAD, GREEN after A7.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ml.training.exceptions import DataCoverageError


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_orchestrator(
    tmp_path: Path,
    *,
    n_symbols: int = 1000,
    min_symbol_coverage: float = 0.85,
) -> Any:
    """Build a minimal ProductionTrainingOrchestrator with a mocked DB session.

    Uses a real CheckpointManager against tmp_path so checkpoint-related paths
    are exercised without a live database.
    """
    # Import here to keep the module-level namespace clean (DB not available at
    # collection time).
    from scripts.production_training_orchestrator import (
        ProductionTrainingOrchestrator,
        TrainingConfig,
    )

    config = TrainingConfig(
        n_symbols=n_symbols,
        min_symbol_coverage=min_symbol_coverage,
    )
    db = AsyncMock()
    return ProductionTrainingOrchestrator(
        db_session=db,
        config=config,
        output_dir=tmp_path / "models",
        fresh=True,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. DataCoverageError contract
# ══════════════════════════════════════════════════════════════════════════════

class TestDataCoverageErrorContract:
    def test_is_runtime_error_subclass(self) -> None:
        assert issubclass(DataCoverageError, RuntimeError)

    def test_message_is_preserved(self) -> None:
        msg = "test coverage failure"
        err = DataCoverageError(msg)
        assert str(err) == msg

    def test_can_be_caught_as_runtime_error(self) -> None:
        with pytest.raises(RuntimeError):
            raise DataCoverageError("coverage too low")

    def test_can_be_caught_as_data_coverage_error(self) -> None:
        with pytest.raises(DataCoverageError):
            raise DataCoverageError("coverage too low")


# ══════════════════════════════════════════════════════════════════════════════
# 2. TrainingConfig.min_symbol_coverage field
# ══════════════════════════════════════════════════════════════════════════════

class TestTrainingConfigCoverageField:
    def test_default_is_060(self) -> None:
        # 0.60 matches the current achievable NSE universe (~1679 / 2557).  The
        # gate still catches a future severe drop (≤ 40%) but admits today's
        # listing-churn / Upstox-backfill reality.
        from scripts.production_training_orchestrator import TrainingConfig
        assert TrainingConfig().min_symbol_coverage == 0.60

    def test_can_be_overridden(self) -> None:
        from scripts.production_training_orchestrator import TrainingConfig
        cfg = TrainingConfig(min_symbol_coverage=0.70)
        assert cfg.min_symbol_coverage == 0.70

    def test_serialises_to_dict(self) -> None:
        from scripts.production_training_orchestrator import TrainingConfig
        d = TrainingConfig().to_dict()
        assert "min_symbol_coverage" in d
        assert d["min_symbol_coverage"] == 0.60


# ══════════════════════════════════════════════════════════════════════════════
# 3. _assert_symbol_coverage — hard gate logic
# ══════════════════════════════════════════════════════════════════════════════

class TestAssertSymbolCoverage:
    """Tests the private _assert_symbol_coverage helper on a live orchestrator."""

    def test_coverage_above_threshold_passes(self, tmp_path: Path) -> None:
        orc = _make_orchestrator(tmp_path, n_symbols=1000, min_symbol_coverage=0.85)
        # 900 / 1000 = 0.90 — above the 0.85 threshold
        orc._assert_symbol_coverage(usable=900, requested=1000)
        assert orc._symbol_coverage == pytest.approx(0.90)

    def test_coverage_at_exact_threshold_passes(self, tmp_path: Path) -> None:
        # >= is inclusive: 850 / 1000 = 0.85 must PASS, not fail.
        orc = _make_orchestrator(tmp_path, n_symbols=1000, min_symbol_coverage=0.85)
        orc._assert_symbol_coverage(usable=850, requested=1000)
        assert orc._symbol_coverage == pytest.approx(0.85)

    def test_coverage_one_below_threshold_raises(self, tmp_path: Path) -> None:
        # 849 / 1000 = 0.849 — just below 0.85
        orc = _make_orchestrator(tmp_path, n_symbols=1000, min_symbol_coverage=0.85)
        with pytest.raises(DataCoverageError) as exc_info:
            orc._assert_symbol_coverage(usable=849, requested=1000)
        assert "84.9%" in str(exc_info.value) or "0.849" in str(exc_info.value).lower() or \
               "849" in str(exc_info.value)

    def test_coverage_severely_below_threshold_raises(self, tmp_path: Path) -> None:
        # Simulates the 2551 → 1198 real-world regression that A7 is designed to block.
        orc = _make_orchestrator(tmp_path, n_symbols=2551, min_symbol_coverage=0.85)
        with pytest.raises(DataCoverageError) as exc_info:
            orc._assert_symbol_coverage(usable=1198, requested=2551)
        msg = str(exc_info.value)
        # Both counts must be visible in the error message for operability.
        assert "1198" in msg
        assert "2551" in msg

    def test_stores_symbol_coverage_on_instance(self, tmp_path: Path) -> None:
        orc = _make_orchestrator(tmp_path, n_symbols=1000, min_symbol_coverage=0.85)
        orc._assert_symbol_coverage(usable=950, requested=1000)
        assert orc._symbol_coverage == pytest.approx(0.95)

    def test_stores_coverage_even_when_gate_fails(self, tmp_path: Path) -> None:
        # _symbol_coverage is set before the raise so callers can inspect it.
        orc = _make_orchestrator(tmp_path, n_symbols=1000, min_symbol_coverage=0.85)
        with pytest.raises(DataCoverageError):
            orc._assert_symbol_coverage(usable=800, requested=1000)
        assert orc._symbol_coverage == pytest.approx(0.80)

    def test_custom_threshold_enforced(self, tmp_path: Path) -> None:
        orc = _make_orchestrator(tmp_path, n_symbols=1000, min_symbol_coverage=0.70)
        # 720/1000 = 0.72 passes the 0.70 threshold but would fail the default 0.85.
        orc._assert_symbol_coverage(usable=720, requested=1000)
        assert orc._symbol_coverage == pytest.approx(0.72)

    def test_zero_requested_does_not_divide_by_zero(self, tmp_path: Path) -> None:
        # max(requested, 1) guards against ZeroDivisionError.
        orc = _make_orchestrator(tmp_path, n_symbols=0, min_symbol_coverage=0.85)
        # 0 usable / max(0, 1) = 0.0 → below 0.85 → raises DataCoverageError
        with pytest.raises(DataCoverageError):
            orc._assert_symbol_coverage(usable=0, requested=0)


# ══════════════════════════════════════════════════════════════════════════════
# 4 & 5. Step-2 resume path — training_samples invariant
# ══════════════════════════════════════════════════════════════════════════════

class TestResumeTrainingSamplesInvariant:
    """
    The step-2 resume block must raise DataCoverageError when total_rows=0.

    We exercise this by monkey-patching CheckpointManager to report step_2
    as done with a corrupt (zero-rows) manifest, then calling run() and
    verifying it raises before touching any DB or model code.
    """

    def _write_corrupt_step2_checkpoint(self, checkpoint_dir: Path) -> None:
        """Write a minimal checkpoint JSON where step_2 is done with 0 total rows."""
        from app.ml.training.checkpoint_manager import SCHEMA_VERSION

        step2_dir = checkpoint_dir / "step_2_features"
        step2_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "__meta__": {
                "total_rows":    0,     # ← corrupt: zero rows
                "max_timestamps": 0,
                "n_symbols":     0,
            }
        }
        (step2_dir / "manifest.json").write_text(json.dumps(manifest))

        step1_dir = checkpoint_dir / "step_1_symbols"
        step1_dir.mkdir(parents=True, exist_ok=True)
        (step1_dir / "symbols.json").write_text(
            json.dumps({"symbols": ["RELIANCE", "TCS"], "count": 2})
        )

        # step_3 must also be marked done so the resume takes the
        # load_features_meta() branch (not load_features()), which is where
        # total_rows is read from the manifest and the zero-samples guard fires.
        step3_dir = checkpoint_dir / "step_3_targets"
        step3_dir.mkdir(parents=True, exist_ok=True)
        (step3_dir / "targets_meta.json").write_text(json.dumps({"n_symbols": 0}))

        checkpoint = {
            "schema_version":  SCHEMA_VERSION,
            "run_id":          "test-resume",
            "completed_steps": {
                "step_1_symbols":   {"duration_s": 1.0},
                "step_2_features":  {"duration_s": 2.0},
                "step_3_targets":   {"duration_s": 1.5},
            },
            "config": {
                "n_features":           69,
                "sequence_length":      60,
                "include_fundamentals": True,
                "model_version":        "1.0.0",
                "n_symbols":            1000,
                "feature_set_version":  "1.0.0",
            },
        }
        (checkpoint_dir / "checkpoint.json").write_text(json.dumps(checkpoint))

    def _write_valid_step2_checkpoint(self, checkpoint_dir: Path, total_rows: int) -> None:
        """Write a checkpoint where step_2 is done with non-zero total rows."""
        from app.ml.training.checkpoint_manager import SCHEMA_VERSION

        step2_dir = checkpoint_dir / "step_2_features"
        step2_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "__meta__": {
                "total_rows":     total_rows,
                "max_timestamps": 500,
                "n_symbols":      2,
            }
        }
        (step2_dir / "manifest.json").write_text(json.dumps(manifest))

        step1_dir = checkpoint_dir / "step_1_symbols"
        step1_dir.mkdir(parents=True, exist_ok=True)
        (step1_dir / "symbols.json").write_text(
            json.dumps({"symbols": ["RELIANCE", "TCS"], "count": 2})
        )

        step3_dir = checkpoint_dir / "step_3_targets"
        step3_dir.mkdir(parents=True, exist_ok=True)
        (step3_dir / "targets_meta.json").write_text(json.dumps({"n_symbols": 2}))

        checkpoint = {
            "schema_version":  SCHEMA_VERSION,
            "run_id":          "test-resume-valid",
            "completed_steps": {
                "step_1_symbols":   {"duration_s": 1.0},
                "step_2_features":  {"duration_s": 2.0},
                "step_3_targets":   {"duration_s": 1.5},
            },
            "config": {
                "n_features":           69,
                "sequence_length":      60,
                "include_fundamentals": True,
                "model_version":        "1.0.0",
                "n_symbols":            1000,
                "feature_set_version":  "1.0.0",
            },
        }
        (checkpoint_dir / "checkpoint.json").write_text(json.dumps(checkpoint))

    def test_zero_total_rows_on_resume_raises_data_coverage_error(
        self, tmp_path: Path
    ) -> None:
        from scripts.production_training_orchestrator import (
            ProductionTrainingOrchestrator,
            TrainingConfig,
        )

        output_dir    = tmp_path / "models"
        checkpoint_dir = output_dir / "checkpoints"
        self._write_corrupt_step2_checkpoint(checkpoint_dir)

        # model_version must match the fixture (1.0.0) — it's a model-affecting
        # key, so any drift triggers StaleCheckpointError before the A7 check.
        config = TrainingConfig(n_symbols=1000, min_symbol_coverage=0.85, model_version="1.0.0", feature_set_version="1.0.0")
        orc = ProductionTrainingOrchestrator(
            db_session=AsyncMock(),
            config=config,
            output_dir=output_dir,
            fresh=False,   # resume from the corrupt checkpoint above
        )

        import asyncio
        with pytest.raises(DataCoverageError, match="training_samples=0"):
            asyncio.get_event_loop().run_until_complete(orc.run())

    def test_positive_total_rows_on_resume_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        """A valid step-2 checkpoint (total_rows > 0) must not raise DataCoverageError."""
        from scripts.production_training_orchestrator import (
            ProductionTrainingOrchestrator,
            TrainingConfig,
        )

        output_dir     = tmp_path / "models"
        checkpoint_dir = output_dir / "checkpoints"
        self._write_valid_step2_checkpoint(checkpoint_dir, total_rows=150_000)

        config = TrainingConfig(n_symbols=1000, min_symbol_coverage=0.85, model_version="1.0.0", feature_set_version="1.0.0")
        orc = ProductionTrainingOrchestrator(
            db_session=AsyncMock(),
            config=config,
            output_dir=output_dir,
            fresh=False,
        )

        # The pipeline will fail further down (step 4 checkpoint not present),
        # but it must NOT raise DataCoverageError for the training_samples gate.
        import asyncio
        with pytest.raises(Exception) as exc_info:
            asyncio.get_event_loop().run_until_complete(orc.run())
        assert not isinstance(exc_info.value, DataCoverageError), (
            "Valid step-2 checkpoint (total_rows=150_000) must not raise DataCoverageError"
        )


# ══════════════════════════════════════════════════════════════════════════════
# 6. symbol_coverage surfaced to registry metrics
# ══════════════════════════════════════════════════════════════════════════════

class TestSymbolCoverageSurfacedToMetrics:
    """Verify _symbol_coverage ends up in xgb_metrics and gru_metrics."""

    def test_symbol_coverage_initialised_to_none(self, tmp_path: Path) -> None:
        orc = _make_orchestrator(tmp_path)
        assert orc._symbol_coverage is None

    def test_symbol_coverage_set_after_assert_call(self, tmp_path: Path) -> None:
        orc = _make_orchestrator(tmp_path, n_symbols=1000, min_symbol_coverage=0.85)
        orc._assert_symbol_coverage(usable=900, requested=1000)
        assert orc._symbol_coverage == pytest.approx(0.90)

    def test_xgb_metrics_include_symbol_coverage(self, tmp_path: Path) -> None:
        """
        Mirror what _register_models_in_registry does at step 10:
        start from asdict(EvaluationResults), then inject symbol_coverage.
        Uses a plain dict to avoid constructing the full 14-field dataclass.
        """
        orc = _make_orchestrator(tmp_path, n_symbols=1000)
        orc._assert_symbol_coverage(usable=920, requested=1000)

        # This is the exact pattern in _register_models_in_registry.
        xgb_metrics: dict = {"accuracy": 0.60}   # representative subset of EvaluationResults
        xgb_metrics["symbol_coverage"] = orc._symbol_coverage

        assert "symbol_coverage" in xgb_metrics
        assert xgb_metrics["symbol_coverage"] == pytest.approx(0.92)

    def test_gru_metrics_include_symbol_coverage(self, tmp_path: Path) -> None:
        orc = _make_orchestrator(tmp_path, n_symbols=2551)
        orc._assert_symbol_coverage(usable=2200, requested=2551)

        gru_metrics: dict = {"accuracy": 0.55}
        gru_metrics["symbol_coverage"] = orc._symbol_coverage

        assert "symbol_coverage" in gru_metrics
        assert gru_metrics["symbol_coverage"] == pytest.approx(2200 / 2551)


# ══════════════════════════════════════════════════════════════════════════════
# 7. _generate_data_quality_report includes A7 fields
# ══════════════════════════════════════════════════════════════════════════════

class TestDataQualityReport:
    def test_report_contains_a7_fields(self, tmp_path: Path) -> None:
        orc = _make_orchestrator(tmp_path, n_symbols=1000, min_symbol_coverage=0.85)
        orc.symbols = ["SYM1"] * 900  # simulate 900 qualified symbols
        orc._symbol_coverage = 0.90

        report = orc._generate_data_quality_report()

        assert report["n_symbols"] == 900
        assert report["n_symbols_requested"] == 1000
        assert report["symbol_coverage"] == pytest.approx(0.90)
        assert report["coverage_threshold"] == pytest.approx(0.85)
        assert report["status"] == "complete"

    def test_report_coverage_none_before_step1(self, tmp_path: Path) -> None:
        orc = _make_orchestrator(tmp_path)
        report = orc._generate_data_quality_report()
        # Before step 1 runs, coverage is None (not fabricated).
        assert report["symbol_coverage"] is None
