"""
Cortex AI — Production ML Training Orchestrator
================================================
World-class, crash-resilient training pipeline for XGBoost + GRU ensemble.

Resumability
------------
Every step persists its outputs to disk immediately upon completion via a
CheckpointManager.  On restart the orchestrator detects the existing
checkpoint, skips all completed steps, reloads their artifacts from disk,
and continues from the next pending step.

Step 6 (GRU) has three internal sub-checkpoints:
    A  eval arrays + GRU plan meta  (saved right after the train/val split)
    B  best hyperparameters         (saved after Keras Tuner search)
    C  per-epoch model weights      (updated by EpochCheckpointCallback)

A crash anywhere in step 6 recovers at the furthest sub-checkpoint reached,
avoiding the need to re-run HPO (up to hours of compute) or rebuild the
sequence array (up to 15 minutes + 5.5 GB RAM peak).

Usage
-----
    # Auto-resume any incomplete run (default):
    python production_training_orchestrator.py

    # Force a completely fresh run (ignores existing checkpoint):
    python production_training_orchestrator.py --fresh

Author: Cortex AI Team
Date: 2026-04-19
Version: 2.0.0
"""

import argparse
import asyncio
import gc
import logging
import re
import sys
import time
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Tuple, Optional, Any

import numpy as np
import pandas as pd
import json

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from app.core.config import get_settings
from app.ml.features.symbol_selector import (
    get_top_liquid_symbols,
    get_recently_listed_symbols,
    analyze_symbol_data_quality,
    MIN_BARS_FOR_TRAINING,
    MIN_BARS_SHORT_HISTORY,
    MIN_IPO_QUARANTINE_DAYS,
    MIN_RELATIVE_COMPLETENESS_PCT,
)
from app.ml.features.feature_pipeline import prepare_training_data  # noqa: F401
from app.ml.features.target_generator import create_targets_batch, get_class_weights
from app.ml.evaluation.cpcv import PanelPurgedCPCV, purged_holdout_split
from app.ml.training.xgboost_trainer import XGBoostTrainer
from app.ml.training.gru_trainer import GRUTrainer, CrossSectionalSequenceGenerator, configure_gpu
from app.ml.training.ensemble_trainer import EnsembleTrainer
from app.ml.training.evaluator import ModelEvaluator, EvaluationResults
from app.ml.training.checkpoint_manager import CheckpointManager, find_checkpoint
from app.ml.training.exceptions import DataCoverageError, MissingReturnsError, StaleCheckpointError
from app.ml.model_registry import ModelRegistry
from app.ml.inference.onnx_converter import ONNXConverter

# ── E3: MLflow lineage ─────────────────────────────────────────────────────────
# Local file-store — operator can explore via:  mlflow ui --backend-store-uri mlruns/
_MLRUNS_DIR     = Path(__file__).parent.parent / "mlruns"
_MLFLOW_EXPERIMENT = "cortex_ml_training"

# Step name → 1-based integer index for MLflow metric step axis.
_STEP_INDEX: Dict[str, int] = {s: i + 1 for i, s in enumerate([
    "step_1_symbols", "step_2_features", "step_3_targets", "step_4_splits",
    "step_5_xgboost", "step_6_gru", "step_7_ensemble", "step_8_evaluation",
    "step_9_onnx", "step_10_registry",
])}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# Configuration & result containers
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TrainingConfig:
    """Production training configuration."""
    n_symbols: int = 2557
    lookback_years: int = 10
    sequence_length: int = 60
    n_features: int = 69  # 44 technical + 5 sentiment + 20 fundamental
    include_fundamentals: bool = True

    # Walk-forward validation
    initial_train_days: int = 730  # 2 years
    validation_days: int = 90      # 3 months
    test_days: int = 30            # 1 month
    step_days: int = 30            # 1 month steps

    # Combinatorial Purged CV (A2) — supersedes the decorative walk-forward
    # fields above for the training pipeline. Those remain only for the
    # standalone WalkForwardSplitter utility still exported by app.ml.training.
    # NOTE: at resume these are read from the persisted cv_plan.json, never
    # from here, so a config edit cannot silently change folds (single source).
    cpcv_n_groups: int = 8            # N — López de Prado groups
    cpcv_n_test: int = 2              # k — test groups/combo (≥2 ⇒ path distribution)
    cpcv_horizon: int = 5             # label look-ahead (== target horizon) → purge
    cpcv_embargo: int = 5             # extra trading-days dropped after each test block
    cpcv_hpo_train_frac: float = 0.8  # one-time purged HPO-holdout train fraction

    # Hyperparameter tuning
    xgboost_trials: int = 100
    gru_trials: int = 15
    # GRU trains on top-N symbols by volume only.  XGBoost trains on all symbols.
    # Rationale: GRU learns temporal pattern structure (breakouts, trends, mean-reversion).
    # Those patterns generalise to all 2,551 symbols at inference.  Training on all
    # symbols would make each epoch ~70× longer without meaningfully improving quality.
    gru_n_symbols: int = 200

    # Training parameters
    early_stopping_patience: int = 20
    max_epochs: int = 200
    # 1024 amortises WSL2 PCIe transfer overhead; fits comfortably in 4 GB VRAM
    # for a 69-feature GRU (226 K params, batch activations ≈ 300 MiB at float16).
    batch_size: int = 1024

    # Ensemble — A5 net-DSR weighting always operates on net Deflated Sharpe
    # (the promotion bar) — see EnsembleTrainer.optimize_weights_on_oof.
    min_confidence_threshold: float = 0.4
    # ── Short-history track (recently listed symbols) ─────────────────────────
    # When enabled, a second selection pass adds symbols with 252–729 bars that
    # have cleared the IPO quarantine window.  These contribute only to the most
    # recent CPCV folds; the Panel Purged CV handles unbalanced panels naturally.
    # See symbol_selector.py for the full design rationale.
    include_short_history_symbols: bool = True
    min_bars_short_history: int = MIN_BARS_SHORT_HISTORY      # 252 ≈ 1 year
    ipo_quarantine_days: int = MIN_IPO_QUARANTINE_DAYS         # 180 calendar days

    # A7 — minimum fraction of requested symbols that must survive the
    # data-quality audit before training is allowed to proceed.  Coverage now
    # counts both established and short-history symbols against n_symbols.
    # 0.60 admits today's universe while still catching a future severe drop
    # (e.g. 40% would indicate a broken data pipeline).
    min_symbol_coverage: float = 0.60

    # Coverage gate on the GRU-OOF ∩ XGBoost-OOF join. Below this, A5 fails
    # loud (the panels disagree → the bar would be measured on a biased
    # subset). 0.90 = 10 % slack for legitimate panel-edge differences.
    a5_min_join_coverage: float = 0.90
    # Strength of the L2 prior toward equal weights in the A5 inner objective;
    # this is what defeats "no-info" boundary attractors at w = 0 or 1 when
    # one member is near-random.
    a5_l2_prior: float = 0.10

    # Bootstrap seed — used only when the registry is empty (first ever run).
    # Every subsequent fresh run auto-increments the patch component by querying
    # the registry for the highest registered semver.  Resume runs always read
    # the version locked in the existing checkpoint.json and ignore this value.
    model_version: str = "1.1.0"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TrainingResults:
    """Container for end-to-end training results."""
    config: TrainingConfig
    symbols: List[str]
    data_quality_report: Dict[str, Any]
    xgboost_results: Dict[str, Any]
    gru_results: Dict[str, Any]
    ensemble_results: Dict[str, Any]
    evaluation_results: Dict[str, EvaluationResults]
    model_paths: Dict[str, str]
    onnx_paths: Dict[str, str]
    training_duration: float
    total_samples: int
    memory_usage: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result['evaluation_results'] = {
            k: asdict(v) for k, v in self.evaluation_results.items()
        }
        return result


# ══════════════════════════════════════════════════════════════════════════════
# Keras callback — persists epoch state after every epoch
# ══════════════════════════════════════════════════════════════════════════════

class EpochCheckpointCallback:
    """
    Factory that returns a ``tf.keras.callbacks.Callback`` subclass wired to
    a ``CheckpointManager``.

    Defined as a factory (not a direct subclass) so that the import of
    ``tensorflow`` is deferred until the GRU step actually runs — keeping
    startup time fast for the non-GPU parts of the pipeline.
    """

    @staticmethod
    def build(cp: CheckpointManager, n_features: int) -> Any:
        import tensorflow as tf

        class _Callback(tf.keras.callbacks.Callback):
            def on_epoch_end(self, epoch: int, logs: Optional[Dict] = None) -> None:
                weight_path = str(cp.gru_epoch_weights_dir / f"epoch_{epoch:04d}.weights.h5")
                self.model.save_weights(weight_path)
                cp.save_gru_training_state(
                    last_epoch=epoch,
                    logs={k: float(v) for k, v in (logs or {}).items()},
                    n_features=n_features,
                )

        return _Callback()


# ══════════════════════════════════════════════════════════════════════════════
# Orchestrator
# ══════════════════════════════════════════════════════════════════════════════

class ProductionTrainingOrchestrator:
    """
    Crash-resilient ML training orchestrator with billion-dollar application standards.

    Every step checkpoints its outputs on success.  On restart the orchestrator
    reads the checkpoint, skips completed steps, and continues from the first
    pending step — no work is duplicated.

    Step 6 (GRU) adds three internal sub-checkpoints (eval arrays, HPO params,
    per-epoch weights) so a crash anywhere inside that step is recoverable
    without re-running HPO or rebuilding the full 5.5 GB sequence array.
    """

    def __init__(
        self,
        db_session: AsyncSession,
        config: Optional[TrainingConfig] = None,
        output_dir: Optional[Path] = None,
        *,
        fresh: bool = False,
        feedback_weights_path: Optional[str] = None,
    ) -> None:
        self.db = db_session
        self.config = config or TrainingConfig()
        self.output_dir = output_dir or Path("models/production")

        # Create output directories
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.models_dir = self.output_dir / "models"
        self.logs_dir   = self.output_dir / "logs"
        self.onnx_dir   = self.output_dir / "onnx"
        for d in [self.models_dir, self.logs_dir, self.onnx_dir]:
            d.mkdir(exist_ok=True)

        # Checkpoint manager — single source of truth for what's been done.
        checkpoint_dir = self.output_dir / "checkpoints"
        self.cp = CheckpointManager(
            checkpoint_dir=checkpoint_dir,
            config=self.config.to_dict(),
            fresh=fresh,
        )

        # Components
        self.model_evaluator = ModelEvaluator()
        self.onnx_converter  = ONNXConverter(
            input_size=self.config.n_features,
            sequence_length=self.config.sequence_length,
        )

        # Training state (populated step by step)
        self.symbols: List[str] = []
        self.features_data: Dict[str, Any] = {}
        self.targets_data: Dict[str, Any] = {}
        self.class_weights: Optional[Dict[int, float]] = None
        self._total_samples_count: int = 0
        # A7 — fraction of config.n_symbols that survived the data-quality audit.
        # Populated by _assert_symbol_coverage (fresh) or step-1 resume path.
        # Injected into registry metrics so the A6 QualityGate coverage gate
        # (gate 4) becomes a true hard gate instead of a provisional skip.
        self._symbol_coverage: Optional[float] = None
        # Per-tier symbol counts — set by _select_symbols_and_assess_quality;
        # injected into MLflow step-10 metrics for observability.
        self._n_symbols_established:   int = 0
        self._n_symbols_short_history: int = 0

        # Models
        self.xgboost_trainer: Optional[XGBoostTrainer] = None
        self.gru_trainer: Optional[GRUTrainer] = None
        self.ensemble_trainer: Optional[EnsembleTrainer] = None

        # GRU evaluation subset (kept after step 6 for steps 7-8)
        self.gru_eval_X:         Optional[np.ndarray] = None
        self.gru_eval_y:         Optional[np.ndarray] = None
        self.gru_eval_returns:   Optional[np.ndarray] = None  # h-bar forward returns
        self.gru_eval_symbol:    Optional[np.ndarray] = None  # A5 join key
        self.gru_eval_timestamp: Optional[np.ndarray] = None  # A5 join key

        # CPCV OOF backtest paths from step-5 XGBoost (used by A3/A4/A5)
        self.cpcv_oof: Optional[Dict[str, Any]] = None

        self.results: Optional[TrainingResults] = None

        # Phase 2 — Feedback bundle path + pre-loaded lookup dict.
        # Set via --feedback-weights CLI flag or the training console API.
        # The lookup is loaded once during step-3 (target generation) and reused
        # by steps 5 (XGBoost) and 6 (GRU) without re-reading the parquet.
        self._feedback_weights_path: Optional[str] = feedback_weights_path
        self._feedback_weight_lookup: Optional[Dict] = None   # populated at step-3
        self._feedback_bundle_stats: Optional[Any]   = None   # FeedbackBundleStats

        # E3 — MLflow lineage (set by _setup_mlflow at run() start; None until then)
        self._e3_mlflow_run_id: Optional[str] = None
        # E3 — Promotion report path cached so _finalize_mlflow can log it as artifact
        self._e3_promotion_report_path: Optional[str] = None

        self._setup_logging()
        logger.info("Production Training Orchestrator initialised  run_id=%s", self.cp.run_id)
        logger.info("Checkpoint file : %s", self.cp._file.resolve())
        logger.info("Checkpoint dir  : %s", self.cp._dir.resolve())
        logger.info("Configuration: %s", json.dumps(self.config.to_dict(), indent=2))

    # ── E3: MLflow lineage + structured run log ────────────────────────────────

    def _setup_mlflow(self) -> None:
        """Start (or resume) the MLflow run for this training session.

        Uses a local file-store so no server is required.  The run_id is
        persisted in the checkpoint state so a resumed run reconnects to the
        same MLflow experiment run instead of spawning a duplicate.

        Entirely non-fatal — a broken mlflow install never aborts training.
        """
        try:
            import mlflow
            _MLRUNS_DIR.mkdir(parents=True, exist_ok=True)
            mlflow.set_tracking_uri(f"file://{_MLRUNS_DIR}")
            mlflow.set_experiment(_MLFLOW_EXPERIMENT)

            stored_mlflow_id = self.cp.mlflow_run_id
            active = mlflow.start_run(
                run_id=stored_mlflow_id,          # None on fresh runs → new run
                run_name=self.cp.run_id,
                tags={
                    "checkpoint.run_id":    self.cp.run_id,
                    "model.version":        self.config.model_version,
                    "checkpoint.dir":       str(self.cp._dir),
                    "tracking.uri":         f"file://{_MLRUNS_DIR}",
                    "pipeline.status":      "running",
                },
            )
            self._e3_mlflow_run_id = active.info.run_id

            if stored_mlflow_id is None:
                # Fresh run — persist so resumes reconnect.
                self.cp.save_mlflow_run_id(self._e3_mlflow_run_id)
                # Log all TrainingConfig fields as params (once per run).
                params = {k: str(v) for k, v in self.config.to_dict().items()}
                mlflow.log_params(params)
                logger.info(
                    "E3 MLflow run started  run_id=%s  experiment=%s",
                    self._e3_mlflow_run_id, _MLFLOW_EXPERIMENT,
                )
            else:
                logger.info(
                    "E3 MLflow run resumed  run_id=%s",
                    self._e3_mlflow_run_id,
                )

            # Write run_start event to the structured log.
            self._write_run_log_entry({
                "event":          "run_start",
                "run_id":         self.cp.run_id,
                "mlflow_run_id":  self._e3_mlflow_run_id,
                "model_version":  self.config.model_version,
                "resumed":        stored_mlflow_id is not None,
            })
        except Exception as exc:
            logger.warning("E3 MLflow setup failed (non-fatal): %s", exc)

    def _log_mlflow_step_done(self, step_name: str, duration_s: float, metrics: Optional[Dict[str, Any]] = None) -> None:
        """Log step completion duration + optional metrics to MLflow and the structured log."""
        step_num = _STEP_INDEX.get(step_name, 0)
        # Structured log (always — independent of MLflow availability).
        self._write_run_log_entry({
            "event":       "step_complete",
            "step":        step_name,
            "step_num":    step_num,
            "duration_s":  round(duration_s, 3),
            "metrics":     {k: v for k, v in (metrics or {}).items() if v is not None},
        })
        try:
            import mlflow
            if self._e3_mlflow_run_id is None:
                return
            mlflow.log_metric("pipeline.step_duration_s", duration_s, step=step_num)
            if metrics:
                safe = {}
                for k, v in metrics.items():
                    if isinstance(v, (int, float)) and v == v:   # exclude NaN
                        safe[k] = float(v)
                    elif isinstance(v, bool):
                        safe[k] = 1.0 if v else 0.0
                if safe:
                    mlflow.log_metrics(safe, step=step_num)
        except Exception as exc:
            logger.debug("E3 MLflow step log failed (non-fatal): %s", exc)

    def _log_mlflow_eval_metrics(self, evaluation_results: Dict[str, Any]) -> None:
        """Log step-8 evaluation metrics to MLflow after comprehensive evaluation."""
        try:
            import mlflow
            if self._e3_mlflow_run_id is None:
                return
            metrics: Dict[str, float] = {}
            for model_name, res in evaluation_results.items():
                d = asdict(res) if hasattr(res, "__dataclass_fields__") else (res if isinstance(res, dict) else {})
                prefix = model_name
                for key in ("accuracy", "sharpe_ratio", "auc_pr", "deflated_sharpe", "pbo"):
                    val = d.get(key)
                    if val is not None and isinstance(val, (int, float)) and val == val:
                        metrics[f"{prefix}.{key}"] = float(val)
            if metrics:
                mlflow.log_metrics(metrics, step=8)
                logger.debug("E3 MLflow eval metrics logged: %s", list(metrics))
        except Exception as exc:
            logger.debug("E3 MLflow eval log failed (non-fatal): %s", exc)

    def _log_mlflow_registration_metrics(
        self,
        xgb_metrics: Dict[str, Any],
        gru_metrics: Dict[str, Any],
    ) -> None:
        """Log step-10 registered model metrics to MLflow."""
        try:
            import mlflow
            if self._e3_mlflow_run_id is None:
                return
            metrics: Dict[str, float] = {}
            for prefix, m in (("xgb", xgb_metrics), ("gru", gru_metrics)):
                for key in ("auc_pr", "deflated_sharpe", "pbo", "ece_after",
                             "symbol_coverage", "ensemble_weight",
                             "ensemble_net_dsr", "accuracy"):
                    val = m.get(key)
                    if val is not None and isinstance(val, (int, float)) and val == val:
                        metrics[f"{prefix}.{key}"] = float(val)
                accretive = m.get("ensemble_accretive")
                if accretive is not None:
                    metrics[f"{prefix}.ensemble_accretive"] = 1.0 if accretive else 0.0
            metrics["data.n_symbols"]              = float(len(self.symbols))
            metrics["data.n_symbols_established"]  = float(self._n_symbols_established)
            metrics["data.n_symbols_short_history"]= float(self._n_symbols_short_history)
            metrics["data.total_samples"]          = float(self._calculate_total_samples())
            if self._symbol_coverage is not None:
                metrics["data.symbol_coverage"] = float(self._symbol_coverage)
            if metrics:
                mlflow.log_metrics(metrics, step=10)
        except Exception as exc:
            logger.debug("E3 MLflow registration metrics log failed (non-fatal): %s", exc)

    def _log_mlflow_trial_sharpes(
        self,
        trial_sharpes: Optional[List[float]],
    ) -> None:
        """Log per-trial HPO Sharpe scalars and per-period return series to MLflow.

        Uploads two artifact groups under ``hpo_trial_data/``:

        ``hpo_trial_sharpes.npy``
            1-D float64 array; one value per retained HPO trial, index-aligned
            with the per-trial return-series files below.  Enables σ_SR and
            N_eff in the DSR sensitivity band (Phase-B §3.5).

        ``trial_return_series/trial_NNNN.npy``
            Per-period net-of-cost return series for each retained trial.
            Index i corresponds to index i in the Sharpes array.  Required for
            CSCV matrix construction and effective-N clustering post-hoc
            (Bailey-Borwein-López de Prado 2017).

        Non-fatal — exceptions are caught and logged at DEBUG so training is
        never interrupted.  Data is available from the first run after
        ``fwd_ret_val`` is wired into ``tune_hyperparameters``.
        """
        import tempfile
        try:
            import mlflow
            if self._e3_mlflow_run_id is None:
                return
            if not trial_sharpes:
                logger.debug(
                    "_log_mlflow_trial_sharpes: no trial Sharpes — "
                    "fwd_ret_val was absent during HPO; skipping artifact upload."
                )
                return

            ts_arr = np.asarray(trial_sharpes, dtype=np.float64)
            return_series = self.xgboost_trainer.trial_return_series  # List[np.ndarray] | None
            n_series = len(return_series) if return_series is not None else 0

            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)

                # ── 1. Scalar Sharpes array ─────────────────────────────────
                sharpes_file = tmp_path / "hpo_trial_sharpes.npy"
                np.save(str(sharpes_file), ts_arr)
                mlflow.log_artifact(str(sharpes_file), artifact_path="hpo_trial_data")

                # ── 2. Per-trial return series (one .npy per trial) ─────────
                if n_series > 0:
                    series_dir = tmp_path / "trial_return_series"
                    series_dir.mkdir()
                    for i, ret_arr in enumerate(return_series):
                        np.save(str(series_dir / f"trial_{i:04d}.npy"), ret_arr)
                    mlflow.log_artifacts(
                        str(series_dir),
                        artifact_path="hpo_trial_data/trial_return_series",
                    )

            logger.info(
                "E3 MLflow: logged %d HPO trial Sharpes + %d return-series "
                "artifacts → hpo_trial_data/",
                len(ts_arr), n_series,
            )
        except Exception as exc:
            logger.debug(
                "E3 MLflow trial-sharpe artifact upload failed (non-fatal): %s", exc
            )

    def _finalize_mlflow(self, status: str, promotion_report_path: Optional[str] = None) -> None:
        """Log final artifacts, update status tag, and end the MLflow run."""
        # Structured log (always).
        self._write_run_log_entry({
            "event":                  f"run_{status.lower()}",
            "mlflow_run_id":          self._e3_mlflow_run_id,
            "promotion_report_path":  promotion_report_path,
        })
        try:
            import mlflow
            if self._e3_mlflow_run_id is None:
                return
            mlflow.set_tag("pipeline.status", status.lower())

            # Log the structured run log as an artifact.
            run_log_path = self.cp._dir / "run_log.ndjson"
            if run_log_path.exists():
                mlflow.log_artifact(str(run_log_path), artifact_path="run_lineage")

            # Log the promotion report as an artifact if available.
            if promotion_report_path:
                p = Path(promotion_report_path)
                if p.exists():
                    mlflow.log_artifact(str(p), artifact_path="promotion_report")

            # Log the training config for reproducibility.
            mlflow.log_dict(self.config.to_dict(), "run_lineage/training_config.json")

            mlflow.end_run(status="FINISHED" if status == "FINISHED" else "FAILED")
            logger.info(
                "E3 MLflow run ended  status=%s  run_id=%s  mlruns=%s",
                status, self._e3_mlflow_run_id, _MLRUNS_DIR,
            )
        except Exception as exc:
            logger.warning("E3 MLflow finalize failed (non-fatal): %s", exc)

    def _write_run_log_entry(self, entry: Dict[str, Any]) -> None:
        """Append a JSON event record to the per-run structured log (NDJSON).

        The structured log at ``{checkpoint_dir}/run_log.ndjson`` is a
        newline-delimited JSON file (one object per line) that operators can
        tail/parse independently of the MLflow UI for automated alerting.
        """
        try:
            entry_with_ts = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
            run_log_path  = self.cp._dir / "run_log.ndjson"
            with open(run_log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry_with_ts, default=str) + "\n")
        except Exception as exc:
            logger.debug("E3 structured log write failed (non-fatal): %s", exc)

    # ── Logging ────────────────────────────────────────────────────────────────

    def _setup_logging(self) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file  = self.logs_dir / f"training_{timestamp}.log"
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
        ))
        logging.getLogger().addHandler(fh)
        logger.info("Logging to: %s", log_file)

    # ══════════════════════════════════════════════════════════════════════════
    # Main pipeline entry point
    # ══════════════════════════════════════════════════════════════════════════

    async def run(self) -> TrainingResults:
        """
        Execute (or resume) the complete production training pipeline.

        Each step is guarded by a checkpoint check.  If a step is already
        marked done its artifacts are loaded from disk; otherwise the step
        runs normally and its outputs are saved before marking it done.
        """
        start_time = datetime.now()
        cp = self.cp  # alias for brevity

        # E3: Start or resume the MLflow run for this session.
        self._setup_mlflow()

        logger.info("=" * 100)
        logger.info("CORTEX AI — PRODUCTION ML TRAINING PIPELINE  run_id=%s", cp.run_id)
        logger.info("Next pending step: %s", cp.next_pending() or "ALL DONE")
        logger.info("=" * 100)

        try:
            # ── Step 1 ────────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 1: SYMBOL SELECTION AND DATA QUALITY ASSESSMENT")
            logger.info("=" * 100)

            if cp.is_done("step_1_symbols"):
                self.symbols = cp.load_symbols()
                # Re-derive coverage so step-10 can inject it into registry
                # metrics regardless of whether this is a fresh or resumed run.
                # The coverage gate already fired on the original fresh run, so
                # this re-derivation is for observability and metric surfacing only.
                self._symbol_coverage = len(self.symbols) / max(self.config.n_symbols, 1)
                # Per-tier counts are not separately persisted in the checkpoint.
                # On resume, approximate: assume all are established (conservative).
                # Accurate counts are only available on a fresh run.
                self._n_symbols_established   = len(self.symbols)
                self._n_symbols_short_history = 0
                logger.info(
                    "→ Resuming: step_1 skipped  "
                    "(%d symbols from checkpoint  established=%d  short_history=%d  coverage=%.1f%%)",
                    len(self.symbols),
                    self._n_symbols_established,
                    self._n_symbols_short_history,
                    self._symbol_coverage * 100,
                )
            else:
                t0 = time.monotonic()
                await self._select_symbols_and_assess_quality()
                cp.save_symbols(self.symbols)
                _dur1 = time.monotonic() - t0
                cp.mark_done("step_1_symbols", _dur1)
                self._log_mlflow_step_done("step_1_symbols", _dur1, {
                    "n_symbols":               len(self.symbols),
                    "n_symbols_established":   self._n_symbols_established,
                    "n_symbols_short_history": self._n_symbols_short_history,
                })

            # ── Step 2 ────────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 2: FEATURE COMPUTATION AND VALIDATION")
            logger.info("=" * 100)

            if cp.is_done("step_2_features"):
                # Only load full DataFrames if step 3 still needs them.
                # Step 4 only needs max_timestamps (in manifest __meta__).
                if not cp.is_done("step_3_targets"):
                    self.raw_features_data = cp.load_features()
                    self._rebuild_features_meta_from_raw()
                else:
                    meta = cp.load_features_meta()
                    self._total_samples_count = meta["total_rows"]
                    self._max_timestamps_cached = meta["max_timestamps"]
                logger.info("→ Resuming: step_2 skipped  (checkpoint)")
                # A7 — training_samples invariant: zero rows means the persisted
                # feature checkpoint is corrupt or empty.  A model trained on zero
                # samples would emit fabricated metrics and ship silently broken.
                if self._calculate_total_samples() <= 0:
                    raise DataCoverageError(
                        "Resume produced training_samples=0: the step_2 feature "
                        "checkpoint is corrupt or empty.  Run with --fresh to rebuild "
                        "the feature data from the database."
                    )
            else:
                t0 = time.monotonic()
                await self._compute_and_validate_features()
                logger.info("Saving step_2 checkpoint (%d DataFrames)...", len(self.raw_features_data))
                cp.save_features(self.raw_features_data)
                _dur2 = time.monotonic() - t0
                cp.mark_done("step_2_features", _dur2)
                self._log_mlflow_step_done("step_2_features", _dur2, {"total_samples": self._calculate_total_samples()})

            # ── Step 3 ────────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 3: TARGET GENERATION AND CLASS ANALYSIS")
            logger.info("=" * 100)

            if cp.is_done("step_3_targets"):
                # Only load targets_data if a downstream step still needs it.
                targets_still_needed = (
                    not cp.is_done("step_5_xgboost") or not cp.is_done("step_6_gru")
                )
                if targets_still_needed:
                    self.targets_data, self.class_weights = cp.load_targets()
                logger.info("→ Resuming: step_3 skipped  (checkpoint)")
            else:
                t0 = time.monotonic()
                await self._generate_and_analyze_targets()
                logger.info("Saving step_3 checkpoint (%d symbols)...", len(self.targets_data))
                cp.save_targets(self.targets_data, self.class_weights)
                _dur3 = time.monotonic() - t0
                cp.mark_done("step_3_targets", _dur3)
                self._log_mlflow_step_done("step_3_targets", _dur3)

            # ── Step 4 ────────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 4: COMBINATORIAL PURGED CV PLAN")
            logger.info("=" * 100)

            if cp.is_done("step_4_splits"):
                cv_plan = cp.load_cv_plan()
                await self._free_raw_features()
                logger.info(
                    "→ Resuming: step_4 skipped  (CPCV plan: %s splits / %s paths)",
                    cv_plan.get("n_splits"), cv_plan.get("n_paths"),
                )
            else:
                t0 = time.monotonic()
                cv_plan = await self._build_cv_plan()
                await self._free_raw_features()
                logger.info(
                    "Saving step_4 checkpoint (CPCV plan: %s splits / %s paths)...",
                    cv_plan.get("n_splits"), cv_plan.get("n_paths"),
                )
                cp.save_cv_plan(cv_plan)
                _dur4 = time.monotonic() - t0
                cp.mark_done("step_4_splits", _dur4)
                self._log_mlflow_step_done("step_4_splits", _dur4, {"n_splits": cv_plan.get("n_splits"), "n_paths": cv_plan.get("n_paths")})

            # ── Step 5 ────────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 5: XGBOOST TRAINING WITH HYPERPARAMETER OPTIMIZATION")
            logger.info("=" * 100)

            if cp.is_done("step_5_xgboost"):
                import xgboost as xgb
                booster, best_params, meta = cp.load_xgboost()
                self.xgboost_trainer = XGBoostTrainer(
                    objective='binary:logistic', num_class=2, random_state=42
                )
                self.xgboost_trainer.model = booster
                xgboost_results = {
                    'model':              booster,
                    'best_params':        best_params,
                    'feature_importance': meta.get('feature_importance', {}),
                    'training_samples':   meta.get('training_samples', 0),
                    'validation_samples': meta.get('validation_samples', 0),
                }
                # Reload CPCV OOF paths (needed by A3/A4/A5); absent on runs
                # that pre-date A2 — treat as a soft warning (not an error)
                # so the pipeline can still proceed to registry registration.
                if cp.has_cpcv_oof():
                    self.cpcv_oof = cp.load_cpcv_oof()
                    xgboost_results['cpcv_oof'] = self.cpcv_oof
                    logger.info(
                        "→ Resuming: CPCV OOF loaded  n_paths=%d  best_rounds=%s",
                        self.cpcv_oof["n_paths"], self.cpcv_oof["best_rounds"],
                    )
                else:
                    self.cpcv_oof = None
                    logger.warning(
                        "→ Resuming: CPCV OOF artifacts absent (pre-A2 checkpoint). "
                        "A3/A4/A5 tasks require a fresh run to regenerate them."
                    )
                # Reload OOF-fitted calibrator (A4); absent on pre-A4 runs.
                if cp.has_calibrator("xgboost"):
                    self.xgboost_trainer.calibrator = cp.load_calibrator("xgboost")
                    logger.info(
                        "→ Resuming: XGBoost OOF calibrator loaded  ECE_after=%.4f",
                        self.xgboost_trainer.calibrator.ece_after or 0.0,
                    )
                else:
                    logger.warning(
                        "→ Resuming: XGBoost calibrator absent (pre-A4 checkpoint). "
                        "Inference will fall back to raw probabilities."
                    )
                logger.info("→ Resuming: step_5 skipped  (XGBoost model from checkpoint)")
            else:
                # Ensure targets_data is in memory (may need lazy-load if step 3 was resumed)
                if not hasattr(self, 'targets_data') or not self.targets_data:
                    self.targets_data, self.class_weights = cp.load_targets()
                t0 = time.monotonic()
                xgboost_results = await self._train_xgboost_with_optimization(cv_plan)
                logger.info("Saving step_5 checkpoint (XGBoost model + CPCV OOF)...")
                cp.save_xgboost(
                    xgboost_results['model'],
                    xgboost_results['best_params'],
                    meta={
                        'feature_importance': xgboost_results.get('feature_importance', {}),
                        'training_samples':   xgboost_results.get('training_samples', 0),
                        'validation_samples': xgboost_results.get('validation_samples', 0),
                    },
                )
                # Persist the φ CPCV OOF backtest paths so A3/A4/A5 can
                # consume them on resume without re-running the 28-combo loop.
                if 'cpcv_oof' in xgboost_results and xgboost_results['cpcv_oof']:
                    cp.save_cpcv_oof(xgboost_results['cpcv_oof'])
                    self.cpcv_oof = xgboost_results['cpcv_oof']
                else:
                    self.cpcv_oof = None
                    logger.warning("No CPCV OOF in xgboost_results — skipping OOF persistence.")
                # Persist the OOF-fitted calibrator (A4) — crash-resilient copy
                # so a resume after step-5 completes does not lose the calibrator
                # and force a full CPCV re-run.
                if self.xgboost_trainer.calibrator is not None:
                    cp.save_calibrator("xgboost", self.xgboost_trainer.calibrator)
                else:
                    logger.warning(
                        "XGBoost calibrator is None after step-5 — OOF pool may have "
                        "been empty. Inference will fall back to raw probabilities."
                    )
                _dur5 = time.monotonic() - t0
                cp.mark_done("step_5_xgboost", _dur5)
                self._log_mlflow_step_done("step_5_xgboost", _dur5, {
                    "xgb_training_samples":   xgboost_results.get("training_samples"),
                    "xgb_best_rounds":        xgboost_results.get("best_rounds"),
                })
                # Phase-B §3.5: persist per-trial Sharpes + return series so
                # CSCV matrix construction and effective-N clustering can be
                # done post-hoc without re-running the HPO study.
                self._log_mlflow_trial_sharpes(xgboost_results.get("trial_sharpes"))

            # ── Step 6 ────────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 6: GRU TRAINING WITH HYPERPARAMETER OPTIMIZATION")
            logger.info("=" * 100)

            if cp.is_done("step_6_gru"):
                gru_model, best_params, eval_X, eval_y, eval_r = cp.load_gru()
                self.gru_trainer = GRUTrainer(
                    input_shape=(self.config.sequence_length, self.config.n_features),
                    num_classes=2,
                    random_state=42,
                )
                self.gru_trainer.model = gru_model
                self.gru_eval_X       = eval_X
                self.gru_eval_y       = eval_y
                self.gru_eval_returns = eval_r
                # A5 join keys + GRU calibrator survive a step-6 resume via
                # sub-A / the calibrator checkpoint (load_gru itself omits
                # them). Absent ⇒ pre-A5 checkpoint; the ensemble step will
                # fail loud rather than weight on uncalibrated/unjoinable data.
                _, _, _, _gru_meta = cp.load_gru_eval_arrays()
                self.gru_eval_symbol    = _gru_meta["val_symbol"]
                self.gru_eval_timestamp = _gru_meta["val_timestamp"]
                if cp.has_calibrator("gru"):
                    self.gru_trainer.calibrator = cp.load_calibrator("gru")
                    logger.info(
                        "→ Resuming: GRU calibrator loaded  ECE_after=%.4f",
                        self.gru_trainer.calibrator.ece_after or 0.0,
                    )
                else:
                    logger.warning(
                        "→ Resuming: GRU calibrator absent (pre-A5 checkpoint). "
                        "Step 7 (A5 net-DSR weighting) will fail loud."
                    )
                gru_results = {
                    'model':              gru_model,
                    'best_params':        best_params,
                    'history':            None,
                    'training_samples':   0,
                    'validation_samples': len(eval_y),
                }
                # Release targets_data if it is still resident
                if hasattr(self, 'targets_data') and self.targets_data:
                    del self.targets_data
                    gc.collect()
                logger.info("→ Resuming: step_6 skipped  (GRU model from checkpoint)")
            else:
                # Ensure targets_data is available (may have been freed in step 5 path)
                if not hasattr(self, 'targets_data') or not self.targets_data:
                    self.targets_data, self.class_weights = cp.load_targets()
                t0 = time.monotonic()
                gru_results = await self._train_gru_with_optimization(cv_plan)
                logger.info("Saving step_6 checkpoint (GRU SavedModel)...")
                cp.save_gru_model(
                    gru_results['model'],
                    gru_results['best_params'],
                    meta={'training_samples': gru_results.get('training_samples', 0)},
                )
                # Persist the GRU calibrator (symmetry with XGBoost, A4) so a
                # resume after step-6 can still run the A5 calibrated net-DSR
                # weighting without retraining the GRU.
                if self.gru_trainer.calibrator is not None:
                    cp.save_calibrator("gru", self.gru_trainer.calibrator)
                else:
                    logger.warning(
                        "GRU calibrator is None after step-6 — A5 weighting "
                        "will fail loud (refusing to weight on raw scores)."
                    )
                _dur6 = time.monotonic() - t0
                cp.mark_done("step_6_gru", _dur6)
                self._log_mlflow_step_done("step_6_gru", _dur6, {
                    "gru_training_samples":   gru_results.get("training_samples"),
                    "gru_validation_samples": gru_results.get("validation_samples"),
                })

            # ── Step 7 ────────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 7: ENSEMBLE CREATION AND WEIGHT OPTIMIZATION")
            logger.info("=" * 100)

            if cp.is_done("step_7_ensemble"):
                weights = cp.load_ensemble_weights()
                diagnostics = cp.load_ensemble_diagnostics()
                self.ensemble_trainer = EnsembleTrainer(
                    xgboost_model=self.xgboost_trainer.model,
                    gru_model=self.gru_trainer.model,
                    weights=weights,
                    xgb_calibrator=self.xgboost_trainer.calibrator,
                    gru_calibrator=self.gru_trainer.calibrator,
                )
                self.ensemble_trainer.optimization_diagnostics = diagnostics or None
                ensemble_results = {
                    'ensemble':           self.ensemble_trainer,
                    'optimized_weights':  weights,
                    'diagnostics':        diagnostics,
                    'validation_samples': len(self.gru_eval_y),
                }
                logger.info(
                    "→ Resuming: step_7 skipped  (weights + A5 diagnostics from checkpoint)"
                )
            else:
                t0 = time.monotonic()
                ensemble_results = await self._create_and_optimize_ensemble(cv_plan)
                logger.info("Saving step_7 checkpoint (weights + A5 diagnostics)...")
                cp.save_ensemble(
                    ensemble_results['optimized_weights'],
                    diagnostics=ensemble_results.get('diagnostics'),
                )
                _dur7 = time.monotonic() - t0
                cp.mark_done("step_7_ensemble", _dur7)
                self._log_mlflow_step_done("step_7_ensemble", _dur7, {
                    "ensemble_accretive": ensemble_results.get("diagnostics", {}).get("accretive"),
                })

            # ── Step 8 ────────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 8: COMPREHENSIVE MODEL EVALUATION")
            logger.info("=" * 100)

            if cp.is_done("step_8_evaluation"):
                # Evaluation results are purely informational; no model state depends on them.
                evaluation_results_raw = cp.load_evaluation()
                # Reconstruct EvaluationResults objects for the TrainingResults container.
                evaluation_results = self._deserialize_evaluation(evaluation_results_raw)
                logger.info("→ Resuming: step_8 skipped  (evaluation from checkpoint)")
            else:
                t0 = time.monotonic()
                evaluation_results = await self._evaluate_all_models(cv_plan)
                logger.info("Saving step_8 checkpoint (evaluation results)...")
                cp.save_evaluation({
                    k: asdict(v) for k, v in evaluation_results.items()
                })
                _dur8 = time.monotonic() - t0
                cp.mark_done("step_8_evaluation", _dur8)
                self._log_mlflow_step_done("step_8_evaluation", _dur8)
                self._log_mlflow_eval_metrics(evaluation_results)

            # ── Step 9 ────────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 9: ONNX EXPORT WITH OPTIMIZATION")
            logger.info("=" * 100)

            if cp.is_done("step_9_onnx"):
                # ONNX files are already on disk — just reconstruct the paths dict.
                onnx_paths = self._discover_onnx_paths()
                logger.info("→ Resuming: step_9 skipped  (ONNX files on disk)")
            else:
                t0 = time.monotonic()
                onnx_paths = await self._export_models_to_onnx()
                _dur9 = time.monotonic() - t0
                cp.mark_done("step_9_onnx", _dur9)
                self._log_mlflow_step_done("step_9_onnx", _dur9, {"onnx_model_count": len(onnx_paths)})

            # ── Step 10 ───────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 10: MODEL REGISTRY REGISTRATION")
            logger.info("=" * 100)

            if cp.is_done("step_10_registry"):
                model_paths = self._discover_model_paths()
                logger.info("→ Resuming: step_10 skipped  (models already registered)")
            else:
                t0 = time.monotonic()
                model_paths = await self._register_models_in_registry(evaluation_results, onnx_paths)
                _dur10 = time.monotonic() - t0
                cp.mark_done("step_10_registry", _dur10)
                self._log_mlflow_step_done("step_10_registry", _dur10)

            # ── F1 — Event-Driven Backtest Validation ─────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("F1: EVENT-DRIVEN BACKTEST VALIDATION")
            logger.info("=" * 100)
            _f1_event_backtest: Optional[Dict[str, Any]] = None
            try:
                _f1_event_backtest = self._run_event_backtest_f1()
                if _f1_event_backtest:
                    logger.info(
                        "F1 event backtest: agreement=%s  divergence=%.1f%%  "
                        "mean_ann_SR=%.4f  total_fills=%d  latency_bars=%d",
                        _f1_event_backtest.get("agreement_status"),
                        _f1_event_backtest.get("sharpe_divergence_relative_pct") or 0.0,
                        _f1_event_backtest.get("mean_net_sharpe_annualised") or 0.0,
                        _f1_event_backtest.get("total_fills") or 0,
                        _f1_event_backtest.get("latency_bars") or 1,
                    )
                    self._write_run_log_entry({
                        "event":            "f1_event_backtest_complete",
                        "agreement_status": _f1_event_backtest.get("agreement_status"),
                        "divergence_pct":   _f1_event_backtest.get("sharpe_divergence_relative_pct"),
                        "mean_ann_sharpe":  _f1_event_backtest.get("mean_net_sharpe_annualised"),
                        "total_fills":      _f1_event_backtest.get("total_fills"),
                    })
            except Exception as _f1_exc:
                logger.error(
                    "F1 event backtest FAILED (non-fatal — promotion report "
                    "will have empty event_backtest section): %s", _f1_exc,
                )
                logger.error("F1 traceback:\n%s", traceback.format_exc())

            # ── C2 — Champion/Challenger Promotion Report ─────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("C2: GENERATING PROMOTION REPORT")
            logger.info("=" * 100)
            try:
                promotion_report = await self._generate_promotion_report(
                    onnx_paths, event_backtest=_f1_event_backtest
                )
                if promotion_report:
                    self._e3_promotion_report_path = promotion_report.get("report_path")
                    logger.info(
                        "C2 Promotion Report: status=%s  path=%s",
                        promotion_report.get("status"),
                        self._e3_promotion_report_path,
                    )
                    self._write_run_log_entry({
                        "event":       "c2_report_generated",
                        "status":      promotion_report.get("status"),
                        "report_path": self._e3_promotion_report_path,
                        "mlflow_run_id": self._e3_mlflow_run_id,
                    })
            except Exception as _c2_exc:
                # Non-fatal: models are registered; a broken report should not
                # abort the training run, but it MUST be loudly logged so the
                # operator knows they cannot promote without a valid report.
                logger.error(
                    "C2 promotion report generation FAILED (non-fatal — "
                    "models are registered but cannot be promoted without a "
                    "valid report): %s", _c2_exc,
                )
                logger.error("C2 traceback:\n%s", traceback.format_exc())

            # ── Finalise ──────────────────────────────────────────────────────
            end_time = datetime.now()
            training_duration = (end_time - start_time).total_seconds()

            self.results = TrainingResults(
                config=self.config,
                symbols=self.symbols,
                data_quality_report=self._generate_data_quality_report(),
                xgboost_results=xgboost_results,
                gru_results=gru_results,
                ensemble_results=ensemble_results,
                evaluation_results=evaluation_results,
                model_paths=model_paths,
                onnx_paths=onnx_paths,
                training_duration=training_duration,
                total_samples=self._calculate_total_samples(),
                memory_usage=self._get_memory_usage(),
            )

            await self._save_training_results()

            # E3: log final run metrics and end the MLflow run.
            self._finalize_mlflow("FINISHED", self._e3_promotion_report_path)

            logger.info("=" * 100)
            logger.info("TRAINING PIPELINE COMPLETED SUCCESSFULLY  run_id=%s", cp.run_id)
            logger.info("Duration: %.2fs", training_duration)
            logger.info("Total samples: %d", self.results.total_samples)
            logger.info("=" * 100)

            return self.results

        except Exception as e:
            logger.error("Training pipeline failed: %s", e)
            logger.error("Traceback:\n%s", traceback.format_exc())
            # E3: end run with FAILED status (non-fatal — never masks the real exception).
            self._finalize_mlflow("FAILED", None)
            await self._save_error_state(e)
            raise

    # ══════════════════════════════════════════════════════════════════════════
    # Step implementations
    # ══════════════════════════════════════════════════════════════════════════

    async def _select_symbols_and_assess_quality(self) -> None:
        """
        Two-pass symbol selection with independent quality audits per track.

        Pass 1 — Established track (≥ MIN_BARS_FOR_TRAINING = 730 bars):
            Selects up to n_symbols liquid symbols with sufficient history to
            fill at least one full initial training fold in the CPCV.

        Pass 2 — Short-history track (252–729 bars, ≥ ipo_quarantine_days):
            Selects recently listed symbols that have cleared the post-IPO
            stabilisation window.  Enabled by config.include_short_history_symbols.
            These symbols contribute only to the most recent CPCV folds; the
            Panel Purged CV handles unbalanced panels naturally.

        Both passes apply the same relative-completeness gate (≥ 95%) and the
        same data-integrity checks (zero prices, invalid OHLC, zero-volume bars).
        The short-history pass uses min_bars_override=config.min_bars_short_history
        (252) so the insufficient_history gate reflects the correct floor.

        Established symbols always precede short-history symbols in self.symbols
        so that if a hard cap is needed they are prioritised.
        """
        logger.info(
            "Selecting symbols  (n_symbols=%d  "
            "established_min_bars=%d  completeness=%.0f%%  "
            "short_history=%s  short_history_min_bars=%d  ipo_quarantine=%d days)",
            self.config.n_symbols,
            MIN_BARS_FOR_TRAINING,
            MIN_RELATIVE_COMPLETENESS_PCT,
            self.config.include_short_history_symbols,
            self.config.min_bars_short_history,
            self.config.ipo_quarantine_days,
        )

        lookback_days = self.config.lookback_years * 365

        # ── Pass 1: Established track ─────────────────────────────────────────
        logger.info("─ Pass 1/2: Established track (≥ %d bars) ─", MIN_BARS_FOR_TRAINING)

        established_candidates = await get_top_liquid_symbols(
            db=self.db,
            n=self.config.n_symbols,
            timeframe='1D',
            lookback_days=lookback_days,
            min_data_points=MIN_BARS_FOR_TRAINING,
        )

        if not established_candidates:
            raise ValueError("No established symbols found. Check database data availability.")

        logger.info(
            "✓ Established candidates: %d  top_10=%s",
            len(established_candidates), established_candidates[:10],
        )

        established_qualified = await self._audit_symbol_quality(
            candidates=established_candidates,
            track_label="established",
            lookback_days=lookback_days,
            min_bars_override=None,          # uses MIN_BARS_FOR_TRAINING (730)
        )

        # ── Pass 2: Short-history track ───────────────────────────────────────
        short_history_qualified: list[str] = []

        if self.config.include_short_history_symbols:
            logger.info(
                "─ Pass 2/2: Short-history track (%d–%d bars  ipo_quarantine=%d days) ─",
                self.config.min_bars_short_history,
                MIN_BARS_FOR_TRAINING - 1,
                self.config.ipo_quarantine_days,
            )

            # Exclude symbols already qualified in Pass 1 to prevent duplication.
            established_set: set[str] = set(established_qualified)

            short_history_candidates = await get_recently_listed_symbols(
                db=self.db,
                n=self.config.n_symbols,
                timeframe='1D',
                lookback_days=lookback_days,
                min_bars=self.config.min_bars_short_history,
                max_bars=MIN_BARS_FOR_TRAINING - 1,
                ipo_quarantine_days=self.config.ipo_quarantine_days,
            )

            # Filter out any accidental overlap (should not occur given the bar
            # range [min_bars, MIN_BARS_FOR_TRAINING-1] is disjoint from Pass 1,
            # but guard defensively).
            short_history_candidates = [
                s for s in short_history_candidates if s not in established_set
            ]

            if short_history_candidates:
                short_history_qualified = await self._audit_symbol_quality(
                    candidates=short_history_candidates,
                    track_label="short-history",
                    lookback_days=lookback_days,
                    min_bars_override=self.config.min_bars_short_history,
                )
            else:
                logger.info("  Short-history track: no candidates found.")
        else:
            logger.info("─ Pass 2/2: Short-history track DISABLED (include_short_history_symbols=False)")

        # ── Merge: established first, then short-history ──────────────────────
        self.symbols = established_qualified + short_history_qualified

        logger.info(
            "✓ Symbol selection complete  "
            "established=%d  short_history=%d  total=%d",
            len(established_qualified),
            len(short_history_qualified),
            len(self.symbols),
        )

        # Store per-tier counts for MLflow metrics (accessed in step-10 logger).
        self._n_symbols_established   = len(established_qualified)
        self._n_symbols_short_history = len(short_history_qualified)

        self._assert_symbol_coverage(usable=len(self.symbols), requested=self.config.n_symbols)

    async def _audit_symbol_quality(
        self,
        candidates: list[str],
        track_label: str,
        lookback_days: int,
        min_bars_override: int | None,
    ) -> list[str]:
        """
        Run the per-symbol quality audit for one selection track.

        Returns the list of symbols that passed all gates, in their original
        volume-descending order.  Logs disqualifications and audit errors.
        """
        logger.info(
            "  Auditing quality for %d %s-track candidates  (min_bars=%s)",
            len(candidates), track_label,
            min_bars_override if min_bars_override is not None else MIN_BARS_FOR_TRAINING,
        )

        quality_reports: list[dict] = []
        audit_errors:    list[str]  = []
        working_candidates = list(candidates)  # copy — we mutate on error

        for symbol in list(working_candidates):
            try:
                report = await analyze_symbol_data_quality(
                    symbol=symbol,
                    timeframe='1D',
                    lookback_days=lookback_days,
                    db=self.db,
                    min_bars_override=min_bars_override,
                )
                quality_reports.append(report)
            except Exception as exc:
                logger.warning("  Audit error [%s] %s: %s", track_label, symbol, exc)
                audit_errors.append(symbol)
                working_candidates.remove(symbol)

        if audit_errors:
            logger.warning(
                "  Removed %d %s-track symbol(s) due to audit errors  (first 20): %s",
                len(audit_errors), track_label, audit_errors[:20],
            )

        if not working_candidates:
            logger.warning("  %s track: all candidates removed during audit.", track_label.capitalize())
            return []

        qualified_pairs  = [
            (s, r) for s, r in zip(working_candidates, quality_reports)
            if r.get("is_qualified")
        ]
        disqualified = [
            (s, r) for s, r in zip(working_candidates, quality_reports)
            if not r.get("is_qualified")
        ]

        if disqualified:
            logger.warning(
                "  [%s] Disqualified %d symbol(s)  (first 20 shown):",
                track_label, len(disqualified),
            )
            for sym, rep in disqualified[:20]:
                logger.warning(
                    "    ✗ %s — %s  (bars=%d  completeness=%.1f%%)",
                    sym,
                    rep.get("disqualification_reason", "unknown"),
                    rep.get("data_points", 0),
                    rep.get("completeness_pct", 0.0),
                )

        if qualified_pairs:
            completeness_vals = [r["completeness_pct"] for _, r in qualified_pairs]
            logger.info(
                "  ✓ [%s] Quality gate  qualified=%d  disqualified=%d  "
                "avg_completeness=%.1f%%  min_completeness=%.1f%%",
                track_label,
                len(qualified_pairs),
                len(disqualified),
                float(np.mean(completeness_vals)),
                float(np.min(completeness_vals)),
            )

        return [s for s, _ in qualified_pairs]

    async def _compute_and_validate_features(self) -> None:
        end_date   = datetime.now()
        start_date = end_date - timedelta(days=self.config.lookback_years * 365)

        logger.info(
            "Computing %d features for %d symbols  [%s → %s]",
            self.config.n_features, len(self.symbols),
            start_date.date(), end_date.date(),
        )

        from app.ml.features.feature_pipeline import compute_features_batch

        self.raw_features_data = await compute_features_batch(
            symbols=self.symbols,
            start_date=start_date,
            end_date=end_date,
            timeframe='1D',
            db=self.db,
            include_sentiment=True,
            include_fundamentals=self.config.include_fundamentals,
        )

        self._rebuild_features_meta_from_raw()
        self._validate_features_data()

        total_rows = sum(d['n_samples'] for d in self.features_data.values())
        logger.info(
            "✓ Feature computation complete  symbols=%d  total_rows=%d",
            len(self.features_data), total_rows,
        )
        logger.info("  (Sequences built on-demand in steps 5-6 to avoid 56 GB materialisation)")

    def _rebuild_features_meta_from_raw(self) -> None:
        """Populate self.features_data from self.raw_features_data (no heavy allocation)."""
        self.features_data = {
            symbol: {
                'symbol':     symbol,
                'n_samples':  len(df),
                'n_features': self.config.n_features,
                'timestamps': pd.DatetimeIndex(
                    df['timestamp'].values if 'timestamp' in df.columns else df.index
                ),
            }
            for symbol, df in self.raw_features_data.items()
        }
        self._total_samples_count = sum(d['n_samples'] for d in self.features_data.values())
        self._max_timestamps_cached = max(d['n_samples'] for d in self.features_data.values())

    def _validate_features_data(self) -> None:
        if not self.raw_features_data:
            raise ValueError("No features data computed")
        for symbol, df in self.raw_features_data.items():
            if df.empty:
                raise ValueError(f"Empty DataFrame for {symbol}")
            if df.isnull().values.any():
                raise ValueError(f"NaN values remain in raw features for {symbol}")
        logger.info("✓ Features validation passed (%d symbols)", len(self.raw_features_data))

    async def _generate_and_analyze_targets(self) -> None:
        logger.info("Generating binary targets (UP/DOWN) with symmetric ATR dead zone...")

        self.targets_data = create_targets_batch(
            features_dict=self.raw_features_data,
            atr_multiplier=0.5,
            horizon=5,
            use_atr_normalization=True,
        )

        for symbol, df in self.targets_data.items():
            if 'target' not in df.columns:
                raise ValueError(f"No target column found for {symbol}")
            if 'forward_return' not in df.columns:
                raise ValueError(f"No forward_return column found for {symbol}")
            self.targets_data[symbol] = {
                'target':         df['target'].astype(int).values,
                'forward_return':  df['forward_return'].astype(np.float32).values,
                'features_df':    df.drop(columns=['forward_return']),
            }

        self._validate_targets_data()

        all_targets = np.concatenate([t['target'] for t in self.targets_data.values()])
        self.class_weights = get_class_weights(all_targets)

        unique, counts = np.unique(all_targets, return_counts=True)
        class_dist = dict(zip(unique, counts))
        total = len(all_targets)
        logger.info("✓ Target generation complete  total=%d", total)
        for cls, name in [(0, 'DOWN'), (1, 'UP')]:
            n = class_dist.get(cls, 0)
            logger.info("    %s (%d): %8d  (%.1f%%)", name, cls, n, 100 * n / total)
        logger.info("  class_weights: %s", {k: round(v, 4) for k, v in self.class_weights.items()})

        # Phase 2 — pre-load the feedback bundle so steps 5 & 6 can skip the
        # parquet open/parse cost (amortised once across all CPCV combos).
        if self._feedback_weights_path:
            try:
                from app.ml.training.feedback_loader import load_bundle_lookup
                lookup, stats = load_bundle_lookup(self._feedback_weights_path)
                self._feedback_weight_lookup = lookup
                self._feedback_bundle_stats  = stats
                logger.info(
                    "Feedback bundle loaded  rows=%d  window=[%s, %s]  sha256=%s…",
                    len(lookup),
                    stats.window_start if stats else "?",
                    stats.window_end   if stats else "?",
                    (stats.sha256[:12] if stats and stats.sha256 else "?"),
                )
                self._write_run_log_entry({
                    "event":              "feedback_bundle_loaded",
                    "bundle_path":        self._feedback_weights_path,
                    "bundle_row_count":   len(lookup),
                    "bundle_sha256":      stats.sha256 if stats else None,
                    "bundle_window":      [stats.window_start, stats.window_end] if stats else None,
                })
            except Exception as exc:
                logger.error(
                    "Failed to load feedback bundle '%s': %s  — training continues "
                    "WITHOUT feedback weights (all sample weights will be 1.0).",
                    self._feedback_weights_path, exc,
                )
                self._feedback_weight_lookup = None

    def _validate_targets_data(self) -> None:
        if not self.targets_data:
            raise ValueError("No targets data generated")
        for symbol, data in self.targets_data.items():
            if 'target' not in data:
                raise ValueError(f"Missing 'target' key for {symbol}")
            targets = data['target']
            if not isinstance(targets, np.ndarray):
                raise ValueError(f"Targets must be numpy array for {symbol}")
            if not set(np.unique(targets)).issubset({0, 1}):
                raise ValueError(f"Invalid target values for {symbol}: {np.unique(targets)}")
            if np.any(np.isnan(targets)):
                raise ValueError(f"NaN in targets for {symbol}")
        logger.info("✓ Targets validation passed")

    async def _build_cv_plan(self) -> Dict[str, Any]:
        """Build the Combinatorial-Purged-CV plan from the global unique
        timestamp axis (the union of every symbol's post-dead-zone timestamps).

        The C(N,k) combinatorial splits are deliberately NOT materialised here:
        they are regenerated deterministically at step-5 from the rebuilt
        XGBoost panel and guarded by the axis fingerprint (resume
        reproducibility — A1 fail-loud philosophy). Step-4 persists only the
        CPCV parameters, group boundaries, n_splits / n_paths, the axis
        fingerprint, and the one-time purged HPO-holdout descriptor.

        All step-5+ code reads CPCV parameters from this persisted plan — never
        from live config — so a config edit cannot silently change folds on
        resume (single source of truth).
        """
        if not getattr(self, 'targets_data', None):
            # Resume robustness — mirror the step-5 lazy-load contract.
            self.targets_data, self.class_weights = self.cp.load_targets()

        # .values on a datetime64[us, UTC] Series yields datetime64[us] —
        # np.concatenate then produces a datetime64[us] array whose PanelPurgedCPCV
        # axis_fingerprint differs from step-5's explicit datetime64[ns] cast.
        # Cast to ns here so both steps hash the same integer representation.
        ts_parts = [
            self.targets_data[s]['features_df']['timestamp'].values.astype("datetime64[ns]")
            for s in self.symbols
            if s in self.targets_data
        ]
        if not ts_parts:
            raise ValueError(
                "Cannot build CV plan — no symbol timestamps present in targets_data."
            )
        all_ts = np.concatenate(ts_parts)

        cpcv = PanelPurgedCPCV(
            all_ts,
            horizon=self.config.cpcv_horizon,
            n_groups=self.config.cpcv_n_groups,
            n_test=self.config.cpcv_n_test,
            embargo=self.config.cpcv_embargo,
        )
        plan = cpcv.plan_summary()
        plan["hpo_holdout"] = {
            "train_frac": self.config.cpcv_hpo_train_frac,
            "horizon":    self.config.cpcv_horizon,
            "embargo":    self.config.cpcv_embargo,
        }
        logger.info(
            "✓ CPCV plan built  N=%d k=%d → %d splits / %d paths  "
            "(%d unique timestamps, axis fp %s…)",
            cpcv.n_groups, cpcv.n_test, plan["n_splits"], plan["n_paths"],
            plan["n_unique_timestamps"], plan["axis_fingerprint"][:12],
        )
        return plan

    async def _free_raw_features(self) -> None:
        """Release raw_features_data — no longer needed after step 4."""
        if hasattr(self, 'raw_features_data') and self.raw_features_data:
            del self.raw_features_data
            gc.collect()
            logger.info("✓ raw_features_data released (~1.3 GB freed)")

    async def _train_xgboost_with_optimization(self, cv_plan: Dict[str, Any]) -> Dict[str, Any]:
        from app.ml.features.feature_pipeline import normalize_features, get_all_feature_names

        feature_names = get_all_feature_names(
            include_sentiment=True,
            include_fundamentals=self.config.include_fundamentals,
        )

        # ── Panel build — features, labels, AND the per-row timestamp +
        # forward-return arrays CPCV / the OOF backtest paths need (the old
        # path tracked neither, which is why "walk-forward" was decorative).
        X_list, y_list, ts_list, r_list, sym_list = [], [], [], [], []
        for symbol in self.symbols:
            if symbol not in self.targets_data:
                continue
            entry  = self.targets_data[symbol]
            df_raw = entry['features_df']
            y      = entry['target']
            r      = entry['forward_return']
            ts     = df_raw['timestamp'].values
            norm_df = normalize_features(
                df_raw, method='rolling', window=60, feature_cols=feature_names
            )
            available = [c for c in feature_names if c in norm_df.columns]
            X_tab = norm_df[available].values.astype(np.float32)
            n = min(len(X_tab), len(y), len(r), len(ts))
            if n == 0:
                continue
            X_list.append(X_tab[:n])
            y_list.append(y[:n])
            r_list.append(np.asarray(r[:n], dtype=np.float32))
            ts_list.append(np.asarray(ts[:n], dtype="datetime64[ns]"))
            # Per-row symbol — half of the (symbol, timestamp) key the A5
            # ensemble net-DSR weighting joins the GRU purged-val OOF on.
            sym_list.append(np.full(n, symbol, dtype="U20"))

        X_all   = np.vstack(X_list)
        y_all   = np.concatenate(y_list)
        r_all   = np.concatenate(r_list)
        ts_all  = np.concatenate(ts_list)
        sym_all = np.concatenate(sym_list)
        del X_list, y_list, r_list, ts_list, sym_list

        logger.info(
            "XGBoost panel prepared  samples=%d  features=%d  class_dist=%s",
            len(X_all), X_all.shape[1],
            dict(zip(*np.unique(y_all, return_counts=True))),
        )

        # Phase 2 — per-sample feedback weights (indexed parallel to X_all).
        # Built once here, sliced per CPCV combo and reused for the final refit.
        # Defaults to None when no feedback bundle is loaded.
        _xgb_sw: Optional[np.ndarray] = None
        if self._feedback_weight_lookup:
            try:
                import pandas as _pd
                _xgb_sw = np.ones(len(X_all), dtype=np.float32)
                for _i in range(len(X_all)):
                    _sym = str(sym_all[_i])
                    _d   = _pd.Timestamp(ts_all[_i]).tz_localize("UTC").date()
                    _xgb_sw[_i] = float(self._feedback_weight_lookup.get((_sym, _d), 1.0))
                _matched = int(np.sum(_xgb_sw != 1.0))
                logger.info(
                    "XGBoost feedback weights: %d / %d rows matched (%.1f%%)",
                    _matched, len(X_all), 100.0 * _matched / max(len(X_all), 1),
                )
            except Exception as _exc:
                logger.warning("XGBoost feedback weight build failed (continuing without): %s", _exc)
                _xgb_sw = None

        self.xgboost_trainer = XGBoostTrainer(
            objective='binary:logistic', num_class=2, random_state=42
        )

        # ── Reconstruct the CPCV plan on THIS panel; the axis fingerprint must
        # match step-4 exactly or the panel was rebuilt non-deterministically
        # (resume would silently use different folds — fail loud, A1 rule).
        cpcv = PanelPurgedCPCV(
            ts_all,
            horizon=cv_plan["horizon"],
            n_groups=cv_plan["n_groups"],
            n_test=cv_plan["n_test"],
            embargo=cv_plan["embargo"],
        )
        if cpcv.axis_fingerprint != cv_plan["axis_fingerprint"]:
            raise StaleCheckpointError(
                "XGBoost panel timestamp axis does not match the step-4 CV "
                f"plan (panel fp {cpcv.axis_fingerprint[:12]}… vs plan "
                f"{cv_plan['axis_fingerprint'][:12]}…). The panel was rebuilt "
                "non-deterministically; resuming would use different CPCV "
                "folds. Start a clean run with --fresh."
            )
        n_paths = cpcv.total_paths

        # ── HPO ONCE on a purged + embargoed holdout (replaces the old
        # stratified-random train_test_split — that was look-ahead leakage on
        # time-series data, the core of RC-2).
        hpo = cv_plan["hpo_holdout"]
        hpo_tr, hpo_va = purged_holdout_split(
            ts_all,
            horizon=hpo["horizon"],
            embargo=hpo["embargo"],
            train_frac=hpo["train_frac"],
        )
        logger.info(
            "Purged HPO holdout  train=%d  val=%d  | %d Optuna trials (AUC-PR)…",
            len(hpo_tr), len(hpo_va), self.config.xgboost_trials,
        )
        best_params = await asyncio.to_thread(
            self.xgboost_trainer.tune_hyperparameters,
            X_all[hpo_tr], y_all[hpo_tr], X_all[hpo_va], y_all[hpo_va],
            n_trials=self.config.xgboost_trials,
            timeout=7200,
            n_jobs=4,
            # Phase-B §3.5: supply holdout forward returns so each Optuna trial
            # logs its net-of-cost Sharpe and full return series.  Enables
            # correct V (variance) and effective N in the DSR sensitivity band,
            # and populates trial_return_series for the MLflow artifact below.
            fwd_ret_val=r_all[hpo_va].astype(np.float64),
        )
        logger.info("✓ XGBoost HPO complete  best_params=%s", best_params)

        # Reference early-stopped fit on the purged holdout → the tuned round
        # count reused (fixed) by every CPCV refit + the final model. (This
        # also fits an interim leakage-safe calibrator on the holdout; A4
        # replaces it with the CPCV-OOF calibrator.)
        await asyncio.to_thread(
            self.xgboost_trainer.train,
            X_all[hpo_tr], y_all[hpo_tr], X_all[hpo_va], y_all[hpo_va],
            params=best_params,
            early_stopping_rounds=self.config.early_stopping_patience,
        )
        best_rounds = max(1, (self.xgboost_trainer.model.best_iteration or 0) + 1)
        logger.info("✓ Tuned boosting rounds (fixed for CPCV + final): %d", best_rounds)

        # ── 28-combo Combinatorial Purged CV → φ out-of-sample backtest paths.
        # Each combo: fixed-round refit on purged+embargoed train, predict the
        # held-out test groups, route every test row to its path. The result
        # is φ coherent OOS prediction series (the honest, leakage-free perf
        # distribution that feeds Deflated-Sharpe / PBO in A3).
        oof_proba = [list() for _ in range(n_paths)]
        oof_idx   = [list() for _ in range(n_paths)]
        n_splits  = cpcv.n_splits(cpcv.n_groups, cpcv.n_test)
        for i, sp in enumerate(cpcv.split(), 1):
            if sp.train_idx.size == 0 or sp.test_idx.size == 0:
                raise ValueError(
                    f"CPCV combo {sp.combo_id} produced an empty train/test "
                    f"set (train={sp.train_idx.size}, test={sp.test_idx.size}). "
                    "Refusing to bias the path distribution by skipping it — "
                    "the data window is too short for these CPCV parameters."
                )
            booster = await asyncio.to_thread(
                self.xgboost_trainer.fit_fixed,
                X_all[sp.train_idx], y_all[sp.train_idx],
                params=best_params,
                num_boost_round=best_rounds,
                class_weights=None,  # XGBoost path is class-weight-free (ATR dead-zone ⇒ ~balanced)
                sample_weight=_xgb_sw[sp.train_idx] if _xgb_sw is not None else None,
            )
            proba_up = XGBoostTrainer.proba_up(booster, X_all[sp.test_idx])
            for p in np.unique(sp.test_path_of_row):
                mask = sp.test_path_of_row == p
                oof_idx[p].append(sp.test_idx[mask])
                oof_proba[p].append(proba_up[mask])
            del booster
            if i % 7 == 0 or i == n_splits:
                gc.collect()
                logger.info("  CPCV progress: %d/%d combos refit", i, n_splits)

        # Assemble the φ paths (each path covers every group exactly once → a
        # full OOS series), time-ordered.
        cpcv_oof = {"n_paths": n_paths, "best_rounds": best_rounds, "paths": []}
        for p in range(n_paths):
            idx = np.concatenate(oof_idx[p])
            order = np.argsort(ts_all[idx], kind="stable")
            idx = idx[order]
            cpcv_oof["paths"].append({
                "proba":          np.concatenate(oof_proba[p])[order].astype(np.float32),
                "y":              y_all[idx].astype(np.int8),
                "forward_return": r_all[idx].astype(np.float32),
                "timestamp":      ts_all[idx],
                "symbol":         sym_all[idx].astype("U20"),
            })
        del oof_proba, oof_idx
        gc.collect()
        logger.info("✓ CPCV OOF assembled  paths=%d  (%d combos refit)", n_paths, n_splits)

        # ── A4: Leakage-free calibrator fitted on CPCV OOF pool ───────────────
        # Each panel row appears in exactly ONE OOF path (CPCVSplit.test_path_of_row
        # routes every test row to a single path), so concatenating all φ paths
        # gives the full un-repeated OOF set — zero selection leakage, zero
        # look-ahead.  Fitting on the HPO holdout val set (the old approach)
        # would contaminate the calibrator because that set also guided
        # Optuna trial selection and early-stopping (RC-2 companion bug).
        all_oof_proba = np.concatenate([p["proba"] for p in cpcv_oof["paths"]])
        all_oof_y     = np.concatenate([p["y"]     for p in cpcv_oof["paths"]])
        xgb_calibrator = await asyncio.to_thread(
            self.xgboost_trainer.fit_calibrator_on_oof,
            all_oof_proba, all_oof_y,
        )
        del all_oof_proba, all_oof_y
        logger.info(
            "✓ XGBoost OOF calibrator fitted  ECE %.4f → %.4f  (n=%d)",
            xgb_calibrator.ece_before, xgb_calibrator.ece_after,
            xgb_calibrator.n_calibration_samples,
        )

        # ── Final production model: all-data refit at the fixed tuned round
        # count (owner decision — a live trading model must see the most recent
        # regime; the CPCV OOF above remains the honest performance estimate).
        prod = await asyncio.to_thread(
            self.xgboost_trainer.fit_fixed,
            X_all, y_all,
            params=best_params,
            num_boost_round=best_rounds,
            class_weights=None,
            sample_weight=_xgb_sw,  # None when no feedback bundle
        )
        self.xgboost_trainer.model = prod
        feature_importance = prod.get_score(importance_type='weight')
        logger.info(
            "✓ XGBoost final model: all-data refit (%d samples, %d rounds)  "
            "importance_entries=%d", len(X_all), best_rounds, len(feature_importance),
        )

        return {
            'model':              prod,
            'best_params':        best_params,
            'feature_importance': feature_importance,
            'training_samples':   len(X_all),       # final model = all data
            'validation_samples': len(hpo_va),      # purged HPO holdout
            'best_rounds':        best_rounds,
            'cpcv_oof':           cpcv_oof,          # → persisted in A2.6 for A3/A4/A5
            # Phase-B §3.5: scalar Sharpes per completed HPO trial.  None when
            # fwd_ret_val was not supplied (should not happen — always passed above).
            # Used by _log_mlflow_trial_sharpes and by compute_dsr_and_pbo.
            'trial_sharpes': (
                self.xgboost_trainer.trial_sharpes.tolist()
                if self.xgboost_trainer.trial_sharpes is not None else None
            ),
        }

    async def _train_gru_with_optimization(self, cv_plan: Dict[str, Any]) -> Dict[str, Any]:
        """
        Train GRU with full sub-checkpoint support.

        Sub-A  — eval arrays + gru_plan saved right after split
        Sub-B  — best_params saved after Keras Tuner search
        Sub-C  — epoch weights + training_state saved after every epoch

        On resume each sub-checkpoint is detected and the corresponding
        work is skipped.  Training sequences (X_train) are always rebuilt
        from targets_data because they are too large to persist (5.5 GB);
        the rebuild takes ~15 min from disk-loaded parquets.
        """
        import tensorflow as tf

        # Hard VRAM cap must be set before the first GPU kernel.  configure_gpu()
        # is idempotent — this is a no-op if called earlier in the process, and
        # the authoritative call-site if no earlier caller exists.
        configure_gpu()

        from app.ml.features.feature_pipeline import (
            normalize_features, create_sequences, get_all_feature_names,
        )

        feature_names = get_all_feature_names(
            include_sentiment=True,
            include_fundamentals=self.config.include_fundamentals,
        )
        seq_len       = self.config.sequence_length
        n_feat        = len(feature_names)
        VAL_CAP       = 30_000
        # Leakage-safe split params come from the persisted CV plan (single
        # source of truth; A2). The GRU uses a deterministic PER-SYMBOL purged
        # split — full combinatorial CPCV is deferred to Workstream B (TFT),
        # exactly per the locked A2 decision (no over-engineering a model that
        # is being replaced; the leakage fix itself is NOT deferred).
        train_frac    = cv_plan["hpo_holdout"]["train_frac"]
        purge_gap     = cv_plan["horizon"] + cv_plan["embargo"]

        cp = self.cp

        # ── Sub-A: eval arrays ────────────────────────────────────────────────
        if cp.gru_has_eval_arrays():
            # Skip sequence building — load eval arrays from checkpoint.
            X_val, y_val, r_val, eval_meta = cp.load_gru_eval_arrays()
            n_train_total = eval_meta["n_train_total"]
            sym_val = eval_meta["val_symbol"]
            ts_val  = eval_meta["val_timestamp"]
            # Reconstruct the per-symbol purged plan (symbol, y_start, n_tr,
            # n_va) so X_train is rebuilt deterministically and identically.
            gru_plan: List[Tuple[str, int, int, int]] = [
                (e[0], e[1], e[2], e[3]) for e in eval_meta["gru_plan"]
            ]
            if self.class_weights is None:
                self.class_weights = {
                    int(k): float(v) for k, v in eval_meta["class_weights"].items()
                }
            logger.info(
                "→ sub-A resumed: eval arrays loaded  X=%s  n_train_total=%d",
                X_val.shape, n_train_total,
            )
        else:
            # ── Pass 1: per-symbol PURGED split plan ──────────────────────────
            # Each symbol's sequences are already chronological. The first
            # `train_frac` → train; the last block → val; a `purge_gap`
            # (= horizon + embargo) of sequences BETWEEN them is DROPPED so no
            # train label window [t, t+h] (+embargo) can overlap that symbol's
            # val period. Deterministic & leakage-safe (kills RC-2 for the
            # sequence path); the old positional-across-symbols cut +
            # np.random.choice subsample are gone.
            gru_candidates = self.symbols[: self.config.gru_n_symbols * 2]
            gru_plan: List[Tuple[str, int, int, int]] = []
            n_train_total = 0
            n_val_total   = 0
            for symbol in gru_candidates:
                if symbol not in self.targets_data:
                    continue
                df_raw = self.targets_data[symbol]['features_df']
                y_sym  = self.targets_data[symbol]['target']
                n_rows = len(df_raw)
                if n_rows < seq_len + 1:
                    continue
                n_seq   = n_rows - seq_len + 1
                y_start = seq_len - 1
                n       = min(n_seq, len(y_sym) - y_start)
                if n <= 0:
                    continue
                n_tr = int(n * train_frac)
                n_va = max(0, n - n_tr - purge_gap)
                if n_tr <= 0 or n_va <= 0:
                    continue  # too few sequences for a purged train+val
                gru_plan.append((symbol, y_start, n_tr, n_va))
                n_train_total += n_tr
                n_val_total   += n_va
                if len(gru_plan) >= self.config.gru_n_symbols:
                    break

            if not gru_plan or n_val_total == 0:
                raise ValueError(
                    "No symbols produced a valid purged train+val GRU split — "
                    "check targets_data / sequence_length / purge gap."
                )

            logger.info(
                "GRU purged plan: %d symbols  train=%d  val=%d  "
                "(seq_len=%d  features=%d  gap=%d)",
                len(gru_plan), n_train_total, n_val_total, seq_len, n_feat, purge_gap,
            )

            # ── Build ONLY the held-out val arrays (small; persisted via
            # sub-A). X_train is rebuilt separately below — the full combined
            # array is never materialised, so peak memory is strictly lower
            # than the old build-X_all-then-split design.
            X_val = np.empty((n_val_total, seq_len, n_feat), dtype=np.float16)
            y_val = np.empty(n_val_total, dtype=np.int32)
            r_val = np.empty(n_val_total, dtype=np.float32)
            # (symbol, timestamp) per val row — the leakage-free key the A5
            # ensemble net-DSR weighting joins against the XGBoost CPCV OOF.
            sym_val = np.empty(n_val_total, dtype="U20")
            ts_val  = np.empty(n_val_total, dtype="datetime64[ns]")
            cursor = 0
            for symbol, y_start, n_tr, n_va in gru_plan:
                df_raw = self.targets_data[symbol]['features_df']
                y_sym  = self.targets_data[symbol]['target']
                r_sym  = self.targets_data[symbol]['forward_return']
                norm_df = normalize_features(
                    df_raw, method='rolling', window=seq_len, feature_cols=feature_names
                )
                # seq_ts[i] is the timestamp of the LAST bar of sequence i —
                # identical to the XGBoost panel's per-row timestamp for the
                # same (symbol, bar), so the A5 join is exact.
                X_seq, seq_ts, _ = create_sequences(norm_df, seq_len, feature_names)
                m       = len(X_seq)
                take_tr = min(n_tr, m)
                v0      = take_tr + purge_gap                  # skip the purge gap
                take_va = max(0, min(n_va, m - v0, n_val_total - cursor))
                if take_va <= 0:
                    continue
                X_val[cursor:cursor + take_va] = X_seq[v0:v0 + take_va]
                y_val[cursor:cursor + take_va] = y_sym[y_start + v0:y_start + v0 + take_va]
                r_val[cursor:cursor + take_va] = r_sym[y_start + v0:y_start + v0 + take_va]
                sym_val[cursor:cursor + take_va] = symbol
                ts_val[cursor:cursor + take_va] = np.asarray(
                    seq_ts[v0:v0 + take_va], dtype="datetime64[ns]"
                )
                cursor += take_va

            X_val = X_val[:cursor]
            y_val = y_val[:cursor]
            r_val = r_val[:cursor]
            sym_val = sym_val[:cursor]
            ts_val  = ts_val[:cursor]

            # Deterministic memory cap (replaces np.random.choice): the val
            # pool is already purged, so a FIXED-seed uniform sample is
            # unbiased AND identical across resumes. The SAME mask is applied
            # to the join keys so they stay row-aligned with X/y/r.
            if len(X_val) > VAL_CAP:
                keep = np.sort(
                    np.random.default_rng(42).choice(len(X_val), VAL_CAP, replace=False)
                )
                X_val = X_val[keep].copy()
                y_val = y_val[keep].copy()
                r_val = r_val[keep].copy()
                sym_val = sym_val[keep].copy()
                ts_val  = ts_val[keep].copy()
            logger.info("✓ GRU val arrays built: %d sequences (cap=%d)", len(X_val), VAL_CAP)

            cp.save_gru_eval_arrays(
                X_val, y_val, r_val, sym_val, ts_val,
                class_weights=self.class_weights or {},
                gru_plan=gru_plan,
                n_train_total=n_train_total,
            )
            gc.collect()

        # ── Free targets_data now (not needed for training path ahead) ────────
        if hasattr(self, 'targets_data') and self.targets_data:
            del self.targets_data
            gc.collect()
            logger.info("✓ targets_data released after GRU sequence build")

        # Store eval arrays for steps 7-8
        self.gru_eval_X         = X_val
        self.gru_eval_y         = y_val
        self.gru_eval_returns   = r_val
        self.gru_eval_symbol    = sym_val
        self.gru_eval_timestamp = ts_val

        # ── Build per-symbol training segments ────────────────────────────────
        # tf.data.Dataset.from_tensor_slices() always copies its numpy input into
        # a TF EagerTensor (confirmed in TF docs; TF ≥ 2.17 also attempts GPU
        # placement — GitHub TF issue #71744).  For a 1.99 GB float16 array this
        # creates a ~4 GB system-RAM peak (original array + TF copy) before the
        # del/gc can reclaim the numpy side — enough to trigger the Linux OOM
        # killer on WSL2 at 6–8 GB available.
        #
        # Fix: accumulate per-symbol (float16 X, int32 y) pairs — same total
        # data, no TF tensor duplicate.  A factory function streams them via
        # from_generator in 4 K-sample chunks so peak RAM stays ≈ 2 GB throughout
        # both HPO and final training.  The factory is re-callable, so the
        # existing OOM batch-halving recovery in _run_gru_fit just calls
        # make_dataset(new_batch) instead of unbatch()+batch() on a stale object.
        n_train = n_train_total
        logger.info(
            "Building GRU training segments  n_train=%d  (%.2f GB float16)...",
            n_train, n_train * seq_len * n_feat * 2 / 1e9,
        )

        if not hasattr(self, 'targets_data') or not self.targets_data:
            logger.info("  Loading targets_data from checkpoint for training data rebuild...")
            self.targets_data, _ = self.cp.load_targets()

        # Per symbol: FIRST n_tr chronological sequences = train.
        # Purge gap + val tail are excluded — identical split to the persisted plan.
        gru_train_segs: list[tuple[np.ndarray, np.ndarray]] = []
        # Phase 2: parallel per-symbol weight arrays; None when no bundle loaded.
        gru_weight_segs: Optional[list[np.ndarray]] = (
            [] if self._feedback_weight_lookup else None
        )
        actual_train   = 0
        nan_sym_count  = 0

        from app.ml.training.feedback_loader import build_sequence_weights_for_symbol

        for symbol, y_start, n_tr, n_va in gru_plan:
            if symbol not in self.targets_data:
                continue
            df_raw  = self.targets_data[symbol]['features_df']
            y_sym   = self.targets_data[symbol]['target']
            norm_df = normalize_features(
                df_raw, method='rolling', window=seq_len, feature_cols=feature_names
            )
            # Keep seq_ts (last-bar timestamp per sequence) for feedback weight lookup.
            X_seq, seq_ts, _ = create_sequences(norm_df, seq_len, feature_names)
            # NaN-drops in normalize_features can yield fewer rows than planned.
            take = min(n_tr, len(X_seq))
            if take <= 0:
                continue
            X_sym   = X_seq[:take].astype(np.float16)
            y_sym_s = y_sym[y_start:y_start + take].astype(np.int32)
            # Per-symbol NaN/inf guard — catches upstream feature computation
            # regressions before they silently corrupt the training distribution.
            if np.any(np.isnan(X_sym)) or np.any(np.isinf(X_sym)):
                nan_sym_count += 1
                logger.warning(
                    "Symbol %s: NaN/inf in %d training sequences — skipping",
                    symbol, take,
                )
                continue
            gru_train_segs.append((X_sym, y_sym_s))
            actual_train += take

            # Phase 2 — per-sequence combined weight (class × feedback).
            # When feedback is absent the weight array is not built (gru_weight_segs is None).
            if gru_weight_segs is not None:
                try:
                    w_sym = build_sequence_weights_for_symbol(
                        bundle_path=None,               # lookup already loaded
                        weight_lookup=self._feedback_weight_lookup,
                        symbol=symbol,
                        seq_timestamps=seq_ts[:take],
                        n_take=take,
                        class_weights=self.class_weights,
                        y_sym=y_sym,
                        y_start=y_start,
                    )
                    gru_weight_segs.append(w_sym)
                except Exception as _exc:
                    logger.warning(
                        "GRU feedback weight build for symbol %s failed (using 1.0): %s",
                        symbol, _exc,
                    )
                    gru_weight_segs.append(np.ones(take, dtype=np.float32))

        if nan_sym_count:
            logger.warning(
                "%d symbol(s) skipped due to NaN/inf in training sequences",
                nan_sym_count,
            )
        if not gru_train_segs:
            raise ValueError(
                "No valid GRU training segments after NaN/inf filtering — "
                "check normalisation pipeline"
            )

        del self.targets_data
        gc.collect()
        try:
            import ctypes
            ctypes.CDLL('libc.so.6').malloc_trim(0)
            logger.info("✓ malloc_trim: freed heap pages returned to OS")
        except Exception as trim_err:
            logger.warning("malloc_trim unavailable (%s) — RSS may stay elevated", trim_err)

        seg_gb          = sum(s[0].nbytes for s in gru_train_segs) / 1e9
        n_train_samples = actual_train
        logger.info(
            "✓ GRU training segments: symbols=%d  samples=%d (planned=%d)  %.2f GB float16",
            len(gru_train_segs), actual_train, n_train, seg_gb,
        )
        logger.info(
            "GRU data summary:  train=%d  (%.2f GB)  val=%d  class_dist(val)=%s",
            actual_train, seg_gb, len(X_val),
            dict(zip(*np.unique(y_val, return_counts=True))),
        )

        # ── tf.data streaming pipeline ─────────────────────────────────────────
        # Yields 4 K-sample float16 chunks → unbatch → shuffle → batch → prefetch.
        # No TF tensor holds the full dataset in memory at any point:
        #   • segments (Python numpy, ~2 GB): kept alive as closure until method returns
        #   • shuffle buffer (5 K float32 samples): ≈ 82 MB
        #   • prefetch (2 × batch_size=1024 float32 batches): ≈ 34 MB
        # GPU-bound training on RTX 3050 at ~200–500 ms/batch means the ~15 ms/
        # batch generator overhead is fully absorbed by prefetch(2) — no GPU stall.
        # Prefetch is capped at 2 (not AUTOTUNE) — AUTOTUNE grew unboundedly in
        # earlier runs and exhausted RAM.
        _GEN_CHUNK    = 4_096
        _segs         = gru_train_segs   # closure — kept alive until this method returns
        _weight_segs  = gru_weight_segs  # None when no feedback bundle

        def _gru_chunk_gen() -> Any:
            """Yields (float32 chunk, int32 labels[, float32 weights]) tuples.

            When ``_weight_segs`` is not None, yields 3-element tuples so Keras
            reads per-sample weights from the dataset directly (class_weight must
            be None in that case — Keras forbids both simultaneously).
            """
            if _weight_segs is not None:
                for (X_sym, y_sym_s), w_sym in zip(_segs, _weight_segs):
                    n = len(X_sym)
                    for start in range(0, n, _GEN_CHUNK):
                        end = min(start + _GEN_CHUNK, n)
                        yield (
                            X_sym[start:end].astype(np.float32),
                            y_sym_s[start:end],
                            w_sym[start:end],
                        )
            else:
                for X_sym, y_sym_s in _segs:
                    n = len(X_sym)
                    for start in range(0, n, _GEN_CHUNK):
                        end = min(start + _GEN_CHUNK, n)
                        yield X_sym[start:end].astype(np.float32), y_sym_s[start:end]

        def _make_gru_dataset(batch_sz: int) -> "tf.data.Dataset":
            """Return a fresh, re-iterable CPU-pinned tf.data pipeline at batch_sz.

            Called once for HPO and once per retry attempt in _run_gru_fit so that
            batch-halving OOM recovery gets a clean dataset (proper shuffle, no
            stale iterator state) rather than an unbatch/rebatch chain.

            When feedback weights are loaded the dataset yields (X, y, w) 3-tuples;
            Keras extracts the sample_weight column automatically.
            """
            with tf.device('/CPU:0'):
                if _weight_segs is not None:
                    sig = (
                        tf.TensorSpec(shape=(None, seq_len, n_feat), dtype=tf.float32),
                        tf.TensorSpec(shape=(None,),                 dtype=tf.int32),
                        tf.TensorSpec(shape=(None,),                 dtype=tf.float32),
                    )
                else:
                    sig = (
                        tf.TensorSpec(shape=(None, seq_len, n_feat), dtype=tf.float32),
                        tf.TensorSpec(shape=(None,),                 dtype=tf.int32),
                    )
                return (
                    tf.data.Dataset
                    .from_generator(_gru_chunk_gen, output_signature=sig)
                    .unbatch()
                    .shuffle(buffer_size=5_000, reshuffle_each_iteration=True)
                    .batch(batch_sz)
                    .prefetch(2)
                )

        train_ds = _make_gru_dataset(self.config.batch_size)

        # ── Initialise GRU trainer ─────────────────────────────────────────────
        self.gru_trainer = GRUTrainer(
            input_shape=(self.config.sequence_length, self.config.n_features),
            num_classes=2,
            random_state=42,
        )

        # ── Sub-B: HPO ────────────────────────────────────────────────────────
        if cp.gru_has_best_params():
            best_params = cp.load_gru_best_params()
            logger.info("→ sub-B resumed: HPO skipped  best_params=%s", best_params)
        else:
            # Resume partial Keras Tuner run if trials exist on disk, else start fresh.
            overwrite_tuner = not cp.gru_tuner_has_trials()
            logger.info(
                "Keras Tuner HPO: %d trials  overwrite=%s  dir=%s",
                self.config.gru_trials, overwrite_tuner, cp.gru_tuner_dir,
            )
            best_params = await asyncio.to_thread(
                self.gru_trainer.tune_hyperparameters,
                max_trials=self.config.gru_trials,
                train_generator=train_ds,
                val_data=(X_val, y_val),
                tuner_directory=str(cp.gru_tuner_dir),
                overwrite=overwrite_tuner,
            )
            cp.save_gru_best_params(best_params)
            logger.info("✓ GRU HPO complete  best_params=%s", best_params)

        # ── Sub-C: final training with epoch checkpointing ───────────────────
        initial_epoch = 0

        if cp.gru_has_training_state():
            state = cp.load_gru_training_state()
            ckpt_n_features = state.get("n_features")

            if ckpt_n_features is not None and ckpt_n_features != n_feat:
                # Architecture mismatch: the checkpoint was written with a
                # different feature count.  Continuing would raise a Keras
                # variable-shape error.  Discard the stale sub-C checkpoint and
                # retrain from epoch 0 with the current architecture.
                logger.warning(
                    "GRU epoch checkpoint has n_features=%d but current model "
                    "expects n_features=%d — checkpoint is stale (features were "
                    "added/removed between runs).  Discarding epoch weights and "
                    "restarting from epoch 0.",
                    ckpt_n_features, n_feat,
                )
                cp.invalidate_gru_epoch_checkpoint()
            else:
                last_epoch = state["last_epoch"]
                initial_epoch = last_epoch + 1
                weight_path = str(cp.gru_epoch_weights_dir / f"epoch_{last_epoch:04d}.weights.h5")
                tmp_model = self.gru_trainer.build_model(best_params)
                tmp_model.load_weights(weight_path)
                self.gru_trainer.model = tmp_model
                logger.info(
                    "→ sub-C resumed: training from epoch %d  (weights: %s)",
                    initial_epoch, weight_path,
                )

        epoch_cb = EpochCheckpointCallback.build(cp, n_features=n_feat)

        logger.info(
            "Training final GRU model  batch=%d  max_epochs=%d  initial_epoch=%d",
            self.config.batch_size, self.config.max_epochs, initial_epoch,
        )

        await asyncio.to_thread(
            self._run_gru_fit,
            _make_gru_dataset,
            X_val,
            y_val,
            best_params,
            initial_epoch,
            epoch_cb,
            _weight_segs is not None,   # use_sample_weights
        )

        gru_model = self.gru_trainer.model
        history   = self.gru_trainer.history

        logger.info("✓ GRU training complete  params=%d  epochs=%d",
                    gru_model.count_params(), len(history.history['loss']))

        return {
            'model':              gru_model,
            'best_params':        best_params,
            'history':            history,
            'training_samples':   n_train_samples,
            'validation_samples': len(X_val),
        }

    def _run_gru_fit(
        self,
        make_dataset: Callable[[int], Any],
        X_val: np.ndarray,
        y_val: np.ndarray,
        best_params: Dict[str, Any],
        initial_epoch: int,
        epoch_cb: Any,
        use_sample_weights: bool = False,
    ) -> None:
        """
        Run model.fit() with OOM recovery — extracted so it can run via asyncio.to_thread.

        ``make_dataset`` is a factory callable ``(batch_size: int) -> tf.data.Dataset``
        that returns a fresh, CPU-pinned, re-iterable pipeline backed by the
        per-symbol float16 segment list built in ``_train_gru_with_optimization``.
        Calling the factory on each OOM retry produces a clean dataset at the
        reduced batch size without needing to unbatch/rebatch a stale iterator.

        OOM recovery strategy
        ---------------------
        If TF raises ``ResourceExhaustedError`` (GPU VRAM exhausted), this method:

        1. Clears the Keras session to release all GPU-resident model weights and
           optimizer states.
        2. Calls ``make_dataset(new_batch)`` to obtain a fresh pipeline at half
           the current batch size — full shuffle is preserved and iterator state
           is reset cleanly for the retry.
        3. Resets the GRU trainer's model to ``None`` so ``train()`` rebuilds it
           with fresh weights at the smaller batch.
        4. Repeats up to ``_MAX_OOM_RETRIES`` times, halving batch size each time.

        The minimum batch floor is 32 to avoid degenerate single-sample training.
        If all retries are exhausted, a ``RuntimeError`` is raised with actionable
        guidance (reduce ``gru_n_symbols`` or ``n_features`` in
        ``TrainingConfig``, or free physical VRAM before re-running).
        """
        import tensorflow as tf
        import gc

        # XLA JIT is intentionally NOT enabled.
        # cuDNN's GRU/LSTM kernels (CudnnRNNV3) have no XLA_GPU_JIT OpKernel
        # (TF issue #100872, open as of TF 2.21) — set_jit(True) causes
        # FAILED_PRECONDITION on the first XLA-compiled step.  cuDNN's native
        # path already delivers near-optimal Ampere throughput without JIT.

        _MAX_OOM_RETRIES: int = 2
        batch_size = self.config.batch_size
        current_ds = make_dataset(batch_size)
        cur_epoch  = initial_epoch

        for attempt in range(_MAX_OOM_RETRIES + 1):
            try:
                # When the dataset already carries per-sample weights (3-tuples),
                # class_weight must be None — Keras rejects both simultaneously.
                _cw = None if use_sample_weights else self.class_weights
                self.gru_trainer.train(
                    params=best_params,
                    epochs=self.config.max_epochs,
                    class_weight=_cw,
                    train_generator=current_ds,
                    val_data=(X_val, y_val),
                    initial_epoch=cur_epoch,
                    extra_callbacks=[epoch_cb],
                )
                return  # success — exit retry loop

            except tf.errors.ResourceExhaustedError as oom:
                if attempt >= _MAX_OOM_RETRIES:
                    logger.error(
                        "GRU training exhausted GPU memory after %d attempt(s) "
                        "(last batch_size=%d).  "
                        "Remediation: reduce gru_n_symbols / n_features "
                        "in TrainingConfig, or free physical VRAM before re-running.",
                        attempt + 1,
                        batch_size,
                    )
                    raise RuntimeError(
                        f"GRU OOM — all {_MAX_OOM_RETRIES + 1} attempts failed. "
                        f"Last batch_size: {batch_size}.  See logs for remediation."
                    ) from oom

                new_batch = max(batch_size // 2, 32)
                logger.warning(
                    "GRU training OOM at batch_size=%d (attempt %d/%d) — "
                    "clearing Keras session and rebuilding dataset at batch_size=%d.",
                    batch_size,
                    attempt + 1,
                    _MAX_OOM_RETRIES + 1,
                    new_batch,
                )

                # ── Recovery step 1: free GPU memory ─────────────────────────
                # clear_session() destroys all Keras models, layers, and
                # optimizer states, returning their VRAM to TF's allocator.
                tf.keras.backend.clear_session()
                gc.collect()

                # ── Recovery step 2: fresh dataset at reduced batch size ───────
                # make_dataset() reconstructs the full from_generator pipeline at
                # new_batch — proper shuffle is preserved and there is no stale
                # iterator state from the OOM'd run.
                current_ds = make_dataset(new_batch)
                batch_size = new_batch

                # ── Recovery step 3: reset model state ───────────────────────
                # train() rebuilds the model graph from scratch.  We restart
                # from epoch 0 — no valid checkpoint exists for the OOM'd run.
                self.gru_trainer.model = None
                cur_epoch = 0

                logger.warning(
                    "OOM recovery: retrying GRU training at batch_size=%d "
                    "(attempt %d/%d)...",
                    new_batch,
                    attempt + 2,
                    _MAX_OOM_RETRIES + 1,
                )

    async def _create_and_optimize_ensemble(
        self, cv_plan: Dict[str, Any]
    ) -> Dict[str, Any]:
        """A5: accretion-gated net-DSR ensemble weighting on the leakage-free
        joint set (GRU per-symbol purged-val rows ∩ XGBoost CPCV OOF rows,
        keyed on ``(symbol, timestamp)``). The promotion bar (A6) and the
        weighting bar are the *same* number: net Deflated Sharpe via the A3
        authority. The accretion gate refuses to ship a non-accretive
        ensemble — instead it one-hot-weights the best standalone calibrated
        member, an audited and explicit outcome (no silent fallback).
        """
        import pandas as pd
        from app.ml.training.ensemble_trainer import EnsembleNotAccretiveError

        # ── Fail-loud preconditions (no silent fallback; A1 rule) ─────────────
        if self.gru_eval_returns is None:
            raise MissingReturnsError(
                "Forward returns unavailable at A5 weighting. Refusing to "
                "weight on a non-financial objective (RC-1). --fresh."
            )
        if self.gru_eval_symbol is None or self.gru_eval_timestamp is None:
            raise StaleCheckpointError(
                "GRU per-row (symbol, timestamp) join keys are missing — this "
                "is a pre-A5 (schema < 4) checkpoint. The A5 leakage-free join "
                "cannot be reconstructed. Start a clean run with --fresh."
            )
        if self.cpcv_oof is None or not self.cpcv_oof.get("paths"):
            raise StaleCheckpointError(
                "XGBoost CPCV OOF bundle is missing — A5 requires it for the "
                "leakage-free joint set. --fresh."
            )
        xgb_cal = self.xgboost_trainer.calibrator
        gru_cal = self.gru_trainer.calibrator
        if xgb_cal is None or gru_cal is None:
            raise RuntimeError(
                "A5 refuses to weight on raw (uncalibrated) probabilities. "
                f"Calibrators present: xgboost={xgb_cal is not None}  "
                f"gru={gru_cal is not None}. Re-run with --fresh so A4 fits "
                "both."
            )

        # ── XGBoost OOF: assert per-path uniqueness, then calibrate-then-average
        #
        # CPCV with n_test=2 puts every panel row in ALL φ paths by construction
        # (each group is tested in C(n_groups-1, n_test-1) combos).  Concatenating
        # all paths and checking cross-path uniqueness is therefore always False —
        # the former check was based on a false invariant.  The correct structural
        # guarantee is: within each individual path, (symbol, timestamp) is unique.
        # Averaging calibrated probabilities across paths = prediction bagging
        # (calibrate-then-average, not average-then-calibrate).
        xgb_paths    = self.cpcv_oof["paths"]
        n_cpcv_paths = len(xgb_paths)

        for pi, _pd in enumerate(xgb_paths):
            _sym_p = np.asarray(_pd["symbol"], dtype="U20")
            _ts_p  = np.asarray(_pd["timestamp"], dtype="datetime64[ns]")
            _midx  = pd.MultiIndex.from_arrays(
                [_sym_p, _ts_p.view(np.int64)], names=["symbol", "ts"]
            )
            if not _midx.is_unique:
                raise StaleCheckpointError(
                    f"CPCV OOF path {pi} has within-path duplicate "
                    "(symbol, timestamp) rows — the path itself is corrupt; --fresh."
                )

        # Path 0 defines the canonical (sym, ts) universe; all φ paths are
        # guaranteed to cover the same rows (CPCV property).
        xgb_sym_canonical = np.asarray(xgb_paths[0]["symbol"],    dtype="U20")
        xgb_ts_canonical  = np.asarray(xgb_paths[0]["timestamp"], dtype="datetime64[ns]")
        n_unique_rows      = len(xgb_sym_canonical)

        # Build calibrated probability matrix: shape (n_rows, n_cpcv_paths).
        # Each column is one path calibrated independently (correct order:
        # calibrate first, then average — averaging raw logits and calibrating
        # the mean would underestimate uncertainty).
        xgb_cal_matrix = np.empty((n_unique_rows, n_cpcv_paths), dtype=np.float64)
        for pi, _pd in enumerate(xgb_paths):
            _raw = np.asarray(_pd["proba"], dtype=np.float64)
            xgb_cal_matrix[:, pi] = xgb_cal.calibrate(
                np.column_stack([1.0 - _raw, _raw])
            )[:, 1]
        # Bagged consensus: mean of φ independently calibrated probabilities.
        xgb_consensus_p = xgb_cal_matrix.mean(axis=1)

        # ── GRU side: predict on the persisted purged-val rows, calibrate ─────
        gru_raw = self.gru_trainer.model.predict(
            self.gru_eval_X, verbose=0, batch_size=512
        )
        gru_cal_p = gru_cal.calibrate(gru_raw)[:, 1].astype(np.float64)

        # ── Join on (symbol, timestamp) — canonical XGBoost path-0 index is
        # guaranteed unique; O(n) pandas hash join. ────────────────────────────
        xgb_unique_idx = pd.MultiIndex.from_arrays(
            [xgb_sym_canonical, xgb_ts_canonical.view(np.int64)],
            names=["symbol", "ts"],
        )
        gru_idx = pd.MultiIndex.from_arrays(
            [self.gru_eval_symbol.astype("U20"),
             self.gru_eval_timestamp.view(np.int64)],
            names=["symbol", "ts"],
        )
        match        = xgb_unique_idx.get_indexer(gru_idx)
        matched_mask = match >= 0
        coverage     = float(matched_mask.mean()) if len(match) else 0.0
        if coverage < self.config.a5_min_join_coverage:
            raise RuntimeError(
                f"A5 join coverage {coverage:.4f} < threshold "
                f"{self.config.a5_min_join_coverage:.2f}. "
                f"{(~matched_mask).sum()} of {len(match)} GRU val rows have "
                "no XGBoost OOF counterpart — the panels are inconsistent. "
                "Refusing to weight on a biased subset."
            )
        if not matched_mask.any():
            raise RuntimeError("A5 join is empty — cannot weight.")

        xgb_match_idx    = match[matched_mask]
        joint_sym        = xgb_sym_canonical[xgb_match_idx]
        joint_r          = self.gru_eval_returns[matched_mask].astype(np.float64)
        joint_p_xgb      = xgb_consensus_p[xgb_match_idx]
        joint_p_gru      = gru_cal_p[matched_mask]
        joint_cal_matrix = xgb_cal_matrix[xgb_match_idx]     # (n_joint, n_cpcv_paths)
        # Deterministic per-symbol path id — one path per symbol (purged
        # window). pd.factorize returns codes in first-appearance order.
        path_id, path_labels = pd.factorize(joint_sym, sort=True)

        logger.info(
            "A5 joint set: %d rows across %d symbols (coverage %.4f, "
            "%d CPCV paths → bagged consensus) — weighting on net Deflated Sharpe.",
            len(joint_p_xgb), len(path_labels), coverage, n_cpcv_paths,
        )

        # ── Instantiate calibration-aware EnsembleTrainer and optimise ────────
        self.ensemble_trainer = EnsembleTrainer(
            xgboost_model=self.xgboost_trainer.model,
            gru_model=self.gru_trainer.model,
            weights={"xgboost": 0.5, "gru": 0.5},
            xgb_calibrator=xgb_cal,
            gru_calibrator=gru_cal,
        )

        try:
            diagnostics = await asyncio.to_thread(
                self.ensemble_trainer.optimize_weights_on_oof,
                p_xgb=joint_p_xgb,
                p_gru=joint_p_gru,
                fwd_ret=joint_r,
                path_id=path_id,
                l2=self.config.a5_l2_prior,
            )
            optimized_weights = diagnostics["weights"]
            accretive = True
        except EnsembleNotAccretiveError as err:
            # Designed outcome: ship best standalone, record the decision.
            optimized_weights = err.recommended_weights
            self.ensemble_trainer.weights = optimized_weights
            diagnostics = self.ensemble_trainer.optimization_diagnostics or {}
            accretive = False
            logger.warning(
                "A5: shipping standalone '%s' (one-hot) — ensemble not "
                "accretive on net DSR. xgboost_solo=%.6f gru_solo=%.6f "
                "ensemble=%.6f",
                err.best_standalone,
                diagnostics.get("standalone_net_dsr", {}).get("xgboost", float("nan")),
                diagnostics.get("standalone_net_dsr", {}).get("gru", float("nan")),
                err.ensemble_net_dsr,
            )

        # ── Per-CPCV-path robustness diagnostic (informational, not a gate) ──
        # Apply the optimized weight to each individual CPCV combo-model path to
        # confirm the weight is stable across the full CPCV path distribution.
        # This is López de Prado's intended use of CPCV paths: robustness testing
        # after parameter selection.  The A6 DSR gate on the consensus is unchanged.
        from app.ml.evaluation.backtest import compute_cpcv_path_net_sharpes
        cpcv_sharpes = compute_cpcv_path_net_sharpes(
            w_xgb          = float(optimized_weights.get("xgboost", 0.5)),
            xgb_cal_matrix = joint_cal_matrix,
            p_gru          = joint_p_gru,
            fwd_ret        = joint_r,
        )
        positive_cpcv = sum(1 for s in cpcv_sharpes if not np.isnan(s) and s > 0)
        logger.info(
            "A5 CPCV robustness: %d/%d paths positive net Sharpe  distribution=%s",
            positive_cpcv, n_cpcv_paths,
            [f"{s:.4f}" if not np.isnan(s) else "nan" for s in cpcv_sharpes],
        )
        diagnostics["cpcv_path_net_sharpes"] = cpcv_sharpes
        diagnostics["cpcv_n_paths"]          = n_cpcv_paths
        diagnostics["cpcv_positive_paths"]   = positive_cpcv
        diagnostics["coverage"]              = coverage
        diagnostics["path_labels"]           = [str(s) for s in path_labels]
        diagnostics["accretive"]             = accretive

        logger.info(
            "✓ A5 complete  weights=%s  accretive=%s  ensemble_net_dsr=%.6f",
            optimized_weights, accretive,
            float(diagnostics.get("ensemble_net_dsr", float("nan"))),
        )
        return {
            "ensemble":           self.ensemble_trainer,
            "optimized_weights":  optimized_weights,
            "diagnostics":        diagnostics,
            "validation_samples": int(matched_mask.sum()),
        }

    async def _evaluate_all_models(self, cv_plan: Dict[str, Any]) -> Dict[str, EvaluationResults]:
        import xgboost as xgb

        X_test_seq   = self.gru_eval_X
        X_test_tab   = self.gru_eval_X[:, -1, :]
        y_test       = self.gru_eval_y
        test_returns = self.gru_eval_returns
        if test_returns is None:
            raise MissingReturnsError(
                "Forward returns unavailable at model evaluation. Refusing to "
                "emit 0.0 financial metrics for an unvalidated model (RC-1). "
                "Re-run with --fresh so forward returns are regenerated."
            )

        logger.info("Evaluating on %d test samples...", len(y_test))
        evaluation_results = {}

        # XGBoost
        dtest = xgb.DMatrix(X_test_tab)
        xgb_raw = self.xgboost_trainer.model.predict(dtest)
        if xgb_raw.ndim == 1:
            xgb_proba = np.column_stack([1 - xgb_raw, xgb_raw])
        else:
            xgb_proba = xgb_raw
        xgb_pred = np.argmax(xgb_proba, axis=1)
        xgb_res  = self.model_evaluator.evaluate(y_true=y_test, y_pred=xgb_pred, y_proba=xgb_proba, returns=test_returns)
        evaluation_results['xgboost'] = xgb_res
        logger.info("  XGBoost  acc=%.4f  F1(up)=%.4f  F1(down)=%.4f  Sharpe=%.4f", xgb_res.accuracy, xgb_res.f1_score.get('up', 0), xgb_res.f1_score.get('down', 0), xgb_res.sharpe_ratio)

        # GRU
        gru_proba = self.gru_trainer.model.predict(X_test_seq, verbose=0)
        gru_pred  = np.argmax(gru_proba, axis=1)
        gru_res   = self.model_evaluator.evaluate(y_true=y_test, y_pred=gru_pred, y_proba=gru_proba, returns=test_returns)
        evaluation_results['gru'] = gru_res
        logger.info("  GRU      acc=%.4f  F1(up)=%.4f  F1(down)=%.4f  Sharpe=%.4f", gru_res.accuracy, gru_res.f1_score.get('up', 0), gru_res.f1_score.get('down', 0), gru_res.sharpe_ratio)

        # Ensemble
        ens_pred, ens_proba = self.ensemble_trainer.predict(
            X_test_tab, X_test_seq, apply_confidence_threshold=False
        )
        ens_res = self.model_evaluator.evaluate(y_true=y_test, y_pred=ens_pred, y_proba=ens_proba, returns=test_returns)
        evaluation_results['ensemble'] = ens_res
        logger.info(
            "  Ensemble acc=%.4f  F1(up)=%.4f  F1(down)=%.4f  Sharpe=%.4f",
            ens_res.accuracy, ens_res.f1_score.get('up', 0), ens_res.f1_score.get('down', 0), ens_res.sharpe_ratio,
        )

        logger.info("✓ Model evaluation complete")
        return evaluation_results

    def _deserialize_evaluation(
        self, raw: Dict[str, Any]
    ) -> Dict[str, EvaluationResults]:
        """Reconstruct EvaluationResults dataclass instances from checkpoint JSON."""
        results = {}
        for model_name, data in raw.items():
            try:
                results[model_name] = EvaluationResults(**data)
            except TypeError:
                # EvaluationResults shape changed — return a placeholder
                logger.warning("Could not deserialize evaluation for %s; using placeholder.", model_name)
                results[model_name] = EvaluationResults(
                    accuracy=data.get('accuracy', 0.0),
                    f1_score=data.get('f1_score', {'macro': 0.0}),
                    sharpe_ratio=data.get('sharpe_ratio', 0.0),
                )
        return results

    async def _export_models_to_onnx(self) -> Dict[str, str]:
        onnx_paths: Dict[str, str] = {}

        # Save XGBoost training artifact now so Treelite can load it.
        # (Step 10 also saves it, but Step 9 runs first and needs the file on disk.)
        xgb_json_early = self.models_dir / "xgboost_model.json"
        self.xgboost_trainer.model.save_model(str(xgb_json_early))
        logger.info("  ✓ XGBoost JSON pre-saved for Treelite: %s", xgb_json_early)

        # XGBoost → Treelite (5-10x faster than ONNX)
        logger.info("Compiling XGBoost to Treelite...")
        try:
            import treelite
            import tl2cgen
            
            # Create treelite directory
            treelite_dir = self.output_dir / "treelite"
            treelite_dir.mkdir(exist_ok=True)
            
            # Load XGBoost model into Treelite
            xgb_json_path = self.models_dir / "xgboost_model.json"
            model = treelite.frontend.load_xgboost_model(str(xgb_json_path))
            
            # Compile to native library
            lib_path = treelite_dir / "xgboost_model.so"
            tl2cgen.export_lib(
                model,
                toolchain="gcc",
                libpath=str(lib_path),
                params={"parallel_comp": 4, "quantize": 1},
                verbose=False,
            )
            
            onnx_paths['xgboost'] = str(lib_path)
            logger.info("  ✓ XGBoost → %s (Treelite)", lib_path)

            # Copy OOF-fitted calibrator alongside the Treelite artifact so
            # RegistryModelLoader._try_load_calibrator finds it at load time.
            # RegistryModelLoader looks for: Path(onnx_path).parent / "calibrator_xgb.pkl"
            # = treelite_dir / "calibrator_xgb.pkl"
            if self.xgboost_trainer.calibrator is not None:
                cal_serving_path = treelite_dir / "calibrator_xgb.pkl"
                self.xgboost_trainer.calibrator.save(cal_serving_path)
                logger.info("  ✓ XGBoost calibrator → %s", cal_serving_path)
            else:
                logger.warning(
                    "  ⚠ XGBoost calibrator not available — inference will use "
                    "raw probabilities (re-run with --fresh to fix)."
                )
        except Exception as e:
            logger.error("XGBoost Treelite compilation failed: %s", e)

        # GRU → ONNX
        logger.info("Exporting GRU to ONNX...")
        try:
            import tf2onnx
            gru_path = self.onnx_dir / "gru_model.onnx"
            onnx_model, _ = tf2onnx.convert.from_keras(self.gru_trainer.model, opset=11)
            with open(gru_path, 'wb') as fh:
                fh.write(onnx_model.SerializeToString())
            
            # Optimize GRU ONNX
            opt_path = self.onnx_dir / "gru_optimized.onnx"
            opt_str = await asyncio.to_thread(
                self.onnx_converter.optimize_onnx, str(gru_path), str(opt_path)
            )
            onnx_paths['gru'] = opt_str
            logger.info("  ✓ GRU → %s (optimized)", opt_path)

            # Save GRU calibrator alongside the ONNX artifact so that
            # RegistryModelLoader._try_load_calibrator and _register_models_in_registry
            # both find it at: Path(gru_inference).parent / "calibrator_gru.pkl"
            if self.gru_trainer.calibrator is not None:
                gru_cal_path = self.onnx_dir / "calibrator_gru.pkl"
                self.gru_trainer.calibrator.save(gru_cal_path)
                logger.info("  ✓ GRU calibrator → %s", gru_cal_path)
            else:
                logger.warning(
                    "  ⚠ GRU calibrator not available — inference will use "
                    "raw probabilities (re-run with --fresh to fix)."
                )
        except Exception as e:
            logger.error("GRU ONNX export failed: %s", e)

        logger.info("✓ Model export complete")
        return onnx_paths

    def _discover_onnx_paths(self) -> Dict[str, str]:
        """Reconstruct inference artifact paths from files already on disk.

        Called when Step 9 is skipped due to a checkpoint resume.  Returns the
        same structure as ``_export_models_to_onnx()`` so Step 10 always
        receives a consistent dict regardless of the resume path.
        """
        paths: Dict[str, str] = {}

        # XGBoost → Treelite .so
        treelite_dir = self.output_dir / "treelite"
        xgb_so = treelite_dir / "xgboost_model.so"
        if xgb_so.exists():
            paths["xgboost"] = str(xgb_so)
        else:
            logger.warning("_discover_onnx_paths: Treelite .so not found at %s", xgb_so)

        # GRU → optimized ONNX (prefer *_optimized.onnx, fall back to plain)
        gru_opt = self.onnx_dir / "gru_optimized.onnx"
        gru_plain = self.onnx_dir / "gru_model.onnx"
        if gru_opt.exists():
            paths["gru"] = str(gru_opt)
        elif gru_plain.exists():
            paths["gru"] = str(gru_plain)
        else:
            logger.warning("_discover_onnx_paths: GRU ONNX not found in %s", self.onnx_dir)

        return paths

    async def _register_models_in_registry(
        self,
        evaluation_results: Dict[str, Any],
        onnx_paths: Dict[str, str],
    ) -> Dict[str, str]:
        """Register trained models in the registry.

        Stores the native training artifacts (model_path) and the
        inference-ready artifacts (onnx_path) separately so that
        RegistryModelLoader can load the correct format (.so / .onnx)
        without extension-check failures.
        """
        import os
        from cryptography.fernet import Fernet

        encryption_key = os.getenv("ML_MODEL_ENCRYPTION_KEY")
        if not encryption_key:
            encryption_key = Fernet.generate_key().decode()
            logger.warning("ML_MODEL_ENCRYPTION_KEY not set — using generated key (not for production)")

        registry = ModelRegistry(
            session=self.db,
            model_storage_path=self.models_dir,
            encryption_key=encryption_key,
        )

        model_paths: Dict[str, str] = {}

        # Save native training artifacts to disk
        xgb_path = self.models_dir / "xgboost_model.json"
        self.xgboost_trainer.model.save_model(str(xgb_path))
        model_paths["xgboost"] = str(xgb_path)
        logger.info("  ✓ XGBoost training artifact → %s", xgb_path)

        gru_path = self.models_dir / "gru_model.keras"
        self.gru_trainer.model.save(str(gru_path))
        model_paths["gru"] = str(gru_path)
        logger.info("  ✓ GRU training artifact → %s", gru_path)

        xgb_inference = onnx_paths.get("xgboost")
        gru_inference  = onnx_paths.get("gru")

        # E2 — derive calibrator paths from the inference artifact directories.
        # Convention mirrors _calibrator_path_for() in evaluation.promotion_report
        # and RegistryModelLoader._try_load_calibrator() so all three callers
        # agree on the exact file location.
        xgb_calibrator = (
            Path(xgb_inference).parent / "calibrator_xgb.pkl"
            if xgb_inference else None
        )
        gru_calibrator = (
            Path(gru_inference).parent / "calibrator_gru.pkl"
            if gru_inference else None
        )

        # Defensive materialisation: if Step 9 was checkpointed (skipped on
        # resume) the calibrator file may not exist on disk yet even though the
        # trainer still holds it in memory.  Write it now so the registry
        # copy-into-storage succeeds whether this is a first run or a resume.
        if (
            gru_calibrator is not None
            and not gru_calibrator.exists()
            and getattr(self.gru_trainer, "calibrator", None) is not None
        ):
            self.gru_trainer.calibrator.save(gru_calibrator)
            logger.info(
                "  ✓ GRU calibrator materialised (resume path) → %s", gru_calibrator
            )

        if not xgb_inference:
            logger.warning(
                "No XGBoost inference artifact in onnx_paths — "
                "RegistryModelLoader will reject this record at startup. "
                "Ensure Step 9 (Treelite compilation) succeeded."
            )
        if not gru_inference:
            logger.warning(
                "No GRU inference artifact in onnx_paths — "
                "RegistryModelLoader will reject this record at startup. "
                "Ensure Step 9 (ONNX export) succeeded."
            )

        try:
            xgb_weight = self.ensemble_trainer.weights.get("xgboost", 0.75)
            gru_weight  = self.ensemble_trainer.weights.get("gru", 0.25)

            # A5 diagnostics → per-model registry metrics so the A6 promotion
            # gate hard-checks the *honest* net Deflated Sharpe (cost-aware,
            # calibrated, leakage-free) rather than the informational
            # step-8 in-sample Sharpe. The diagnostics block is the single
            # source of truth produced by EnsembleTrainer.optimize_weights_on_oof.
            ens_diag = getattr(self.ensemble_trainer, "optimization_diagnostics", None) or {}
            standalone_dsr = ens_diag.get("standalone_net_dsr", {}) or {}
            standalone_pbo = ens_diag.get("standalone_pbo", {}) or {}

            xgb_metrics = asdict(evaluation_results["xgboost"])
            xgb_metrics["ensemble_weight"]    = xgb_weight
            xgb_metrics["deflated_sharpe"]    = standalone_dsr.get("xgboost")
            xgb_metrics["pbo"]                = standalone_pbo.get("xgboost")
            xgb_metrics["ensemble_net_dsr"]   = ens_diag.get("ensemble_net_dsr")
            xgb_metrics["ensemble_accretive"] = ens_diag.get("accretive")
            # A6 calibration gate — ECE lives on the calibrator object, not in
            # EvaluationResults; surface it here so QualityGate can enforce the
            # ece_after ≤ 0.05 hard gate without a DB join to the calibrator.
            xgb_cal = getattr(self.xgboost_trainer, "calibrator", None)
            xgb_metrics["ece_after"]        = xgb_cal.ece_after if xgb_cal is not None else None
            # A7 — surface symbol coverage so QualityGate gate-4 is a hard gate.
            xgb_metrics["symbol_coverage"]  = self._symbol_coverage

            # The feature manifest is the ordered list of feature names used
            # during training.  Storing it in training_features makes every
            # model self-describing: FeatureLoader reads the manifest from the
            # active production record and computes exactly those features,
            # so n_features changing between runs never requires code changes.
            from app.ml.features.feature_pipeline import get_all_feature_names
            include_sentiment     = getattr(self.config, "include_sentiment", True)
            include_fundamentals  = getattr(self.config, "include_fundamentals", True)
            feature_names = get_all_feature_names(
                include_sentiment=include_sentiment,
                include_fundamentals=include_fundamentals,
            )
            logger.info(
                "  Feature manifest: %d features (sentiment=%s, fundamentals=%s)",
                len(feature_names), include_sentiment, include_fundamentals,
            )

            _fb_meta = None
            if self._feedback_bundle_stats is not None:
                _fb_meta = {
                    "bundle_path":  self._feedback_bundle_stats.bundle_path,
                    "sha256":       self._feedback_bundle_stats.sha256,
                    "row_count":    self._feedback_bundle_stats.row_count,
                    "window_start": self._feedback_bundle_stats.window_start,
                    "window_end":   self._feedback_bundle_stats.window_end,
                    "created_at":   self._feedback_bundle_stats.created_at,
                }
                logger.info(
                    "Lineage: feedback_bundle  sha256=%s…  rows=%d  window=[%s, %s]",
                    _fb_meta["sha256"][:12], _fb_meta["row_count"],
                    _fb_meta["window_start"], _fb_meta["window_end"],
                )

            xgb_meta = await registry.register_model(
                version                 = f"{self.config.model_version}_xgboost",
                model_type              = "xgboost",
                artifact_path           = model_paths["xgboost"],
                inference_artifact_path = xgb_inference,
                calibrator_path         = xgb_calibrator,
                metrics                 = xgb_metrics,
                metadata                = {
                    "features":         feature_names,   # → training_features column
                    "n_symbols":        len(self.symbols),
                    "n_features":       len(feature_names),
                    "training_samples": self._calculate_total_samples(),
                },
                feature_version      = "1.0.0",
                status               = "development",
                overwrite            = True,
                feedback_bundle_meta = _fb_meta,
            )
            logger.info("  ✓ XGBoost registered  id=%s  inference=%s  n_features=%d",
                        xgb_meta.id, Path(xgb_inference).suffix if xgb_inference else "none",
                        len(feature_names))

            gru_metrics = asdict(evaluation_results["gru"])
            gru_metrics["ensemble_weight"]    = gru_weight
            gru_metrics["deflated_sharpe"]    = standalone_dsr.get("gru")
            gru_metrics["pbo"]                = standalone_pbo.get("gru")
            gru_metrics["ensemble_net_dsr"]   = ens_diag.get("ensemble_net_dsr")
            gru_metrics["ensemble_accretive"] = ens_diag.get("accretive")
            gru_cal = getattr(self.gru_trainer, "calibrator", None)
            gru_metrics["ece_after"]        = gru_cal.ece_after if gru_cal is not None else None
            # A7 — same coverage key as xgboost so the gate fires for both models.
            gru_metrics["symbol_coverage"]  = self._symbol_coverage

            gru_meta = await registry.register_model(
                version                 = f"{self.config.model_version}_gru",
                model_type              = "gru",
                artifact_path           = model_paths["gru"],
                inference_artifact_path = gru_inference,
                calibrator_path         = gru_calibrator,
                metrics                 = gru_metrics,
                metadata                = {
                    "features":         feature_names,   # → training_features column
                    "n_symbols":        len(self.symbols),
                    "n_features":       len(feature_names),
                    "training_samples": self._calculate_total_samples(),
                },
                feature_version      = "1.0.0",
                status               = "development",
                overwrite            = True,
                feedback_bundle_meta = _fb_meta,
            )
            logger.info("  ✓ GRU registered  id=%s  inference=%s  n_features=%d",
                        gru_meta.id, Path(gru_inference).suffix if gru_inference else "none",
                        len(feature_names))

            # E3: log the final registered metrics to MLflow.
            self._log_mlflow_registration_metrics(xgb_metrics, gru_metrics)

        except Exception as e:
            logger.error("Model registry registration failed: %s", e)
            raise

        # Sync training_date back to the governance registry (ai_ml_models).
        # MLModelMetadata.trained_at is the authoritative timestamp set by the
        # DB at INSERT time; without this sync the governance UI shows the
        # original registration date (when the model was first created in
        # ai_ml_models) instead of the actual retrain date.
        try:
            from datetime import timezone
            from sqlalchemy import update as _sa_update
            from app.ai.fusion.models import AIMLModel as _GovernanceModel

            for model_type, meta in (("xgboost", xgb_meta), ("gru", gru_meta)):
                trained_at = meta.trained_at or datetime.now(timezone.utc)
                await self.db.execute(
                    _sa_update(_GovernanceModel)
                    .where(_GovernanceModel.model_type == model_type)
                    .values(
                        training_date=trained_at,
                        updated_at=datetime.now(timezone.utc),
                    )
                )
            await self.db.commit()
            logger.info("✓ Governance training_date synced for xgboost + gru")
        except Exception as e:
            logger.warning("Governance training_date sync failed (non-fatal): %s", e)

        logger.info("✓ Model registry registration complete")
        return model_paths

    def _discover_model_paths(self) -> Dict[str, str]:
        paths: Dict[str, str] = {}
        for suffix in ("*.json", "*.h5", "*.pkl"):
            for p in self.models_dir.glob(suffix):
                paths[p.stem] = str(p)
        return paths

    # ── Utility methods ────────────────────────────────────────────────────────

    def _assert_symbol_coverage(self, *, usable: int, requested: int) -> None:
        """Enforce the A7 symbol-coverage hard gate.

        Computes coverage = usable / requested, stores it on self._symbol_coverage
        for downstream metric injection (A6 QualityGate gate 4 + registry metadata),
        then raises DataCoverageError if coverage < min_symbol_coverage.

        Called at the end of _select_symbols_and_assess_quality (fresh path); the
        resume path re-derives and stores coverage without re-checking the gate
        (it already fired on the original run).
        """
        coverage = usable / max(requested, 1)
        self._symbol_coverage = coverage
        logger.info(
            "A7 coverage gate: %d / %d = %.2f%%  (threshold=%.2f%%)",
            usable, requested, coverage * 100, self.config.min_symbol_coverage * 100,
        )
        if coverage < self.config.min_symbol_coverage:
            raise DataCoverageError(
                f"Symbol coverage {coverage:.1%} is below the minimum threshold "
                f"{self.config.min_symbol_coverage:.1%} "
                f"({usable} usable of {requested} requested). "
                f"Training on a severely degraded symbol universe produces unreliable "
                f"models with biased evaluation metrics. "
                f"Investigate data pipeline health; if coverage reduction is intentional, "
                f"lower TrainingConfig.min_symbol_coverage with caution."
            )

    def _generate_data_quality_report(self) -> Dict[str, Any]:
        return {
            "status":              "complete",
            "n_symbols":           len(self.symbols),
            "n_symbols_requested": self.config.n_symbols,
            "symbol_coverage":     self._symbol_coverage,
            "coverage_threshold":  self.config.min_symbol_coverage,
        }

    def _calculate_total_samples(self) -> int:
        if self._total_samples_count:
            return self._total_samples_count
        if self.features_data:
            return sum(len(data['timestamps']) for data in self.features_data.values())
        return 0

    def _get_memory_usage(self) -> Dict[str, float]:
        import psutil
        mi = psutil.Process().memory_info()
        return {
            'rss_mb':  mi.rss / 1024 / 1024,
            'vms_mb':  mi.vms / 1024 / 1024,
            'percent': psutil.Process().memory_percent(),
        }

    async def _generate_promotion_report(
        self,
        onnx_paths: Dict[str, str],
        *,
        event_backtest: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate the C2 Champion/Challenger Promotion Report after step 10.

        Queries the DB for the just-registered challenger records and the
        current production champions, then delegates to
        ``build_promotion_report``.  The resulting JSON is written to
        ``backend/incident_reports/`` and its path is returned.

        Non-fatal: caller catches and logs any exception so a broken report
        never aborts the training run (models are already registered by step 10).
        """
        from sqlalchemy import select as _sa_select, and_ as _sa_and
        from app.ml.evaluation.promotion_report import build_promotion_report
        from app.models.ml_data import MLModelMetadata
        from app.ai.fusion.models import AIMLModel as _GovernanceModel, AIDriftReport as _DriftReport

        model_names: list[str] = ["xgboost", "gru"]

        # ── Load just-registered challenger records from the DB ────────────────
        challengers: Dict[str, Any] = {}
        for model_name in model_names:
            version = f"{self.config.model_version}_{model_name}"
            meta = (await self.db.execute(
                _sa_select(MLModelMetadata).where(
                    MLModelMetadata.model_version == version
                )
            )).scalar_one_or_none()
            if meta is not None:
                challengers[model_name] = meta

        if not challengers:
            logger.warning(
                "C2: no challenger records found in the DB for version=%s — "
                "skipping promotion report",
                self.config.model_version,
            )
            return None

        # ── Fetch current production champions (may be None for first deploy) ──
        champions: Dict[str, Any] = {}
        for model_name in challengers:
            champ = (await self.db.execute(
                _sa_select(MLModelMetadata).where(
                    _sa_and(
                        MLModelMetadata.model_name == model_name,
                        MLModelMetadata.status    == "production",
                        MLModelMetadata.is_active == True,  # noqa: E712
                    )
                ).order_by(MLModelMetadata.deployed_at.desc()).limit(1)
            )).scalar_one_or_none()
            champions[model_name] = champ

        # ── D1: Build drift advisory from governance metadata + latest drift reports ──
        # For each champion, read the advisory flag from ai_ml_models.governance_metadata
        # and the most recent AIDriftReport to surface live metrics in the C2 report.
        # This is a read-only snapshot — no state mutation happens here.
        drift_advisory: Dict[str, Any] = {}
        for model_name, champ in champions.items():
            if champ is None:
                # First deployment — no champion, no drift history.
                drift_advisory[model_name] = None
                continue

            # Resolve the governance row for this champion model type.
            # AIMLModel.model_type maps to MLModelMetadata.model_name (e.g. "xgboost").
            gov_row = (await self.db.execute(
                _sa_select(_GovernanceModel).where(
                    _sa_and(
                        _GovernanceModel.model_type      == model_name,
                        _GovernanceModel.deployment_state == "live",
                    )
                ).order_by(_GovernanceModel.updated_at.desc()).limit(1)
            )).scalar_one_or_none()

            if gov_row is None:
                drift_advisory[model_name] = {"challenger_recommended": False, "last_check": None}
                continue

            gov_meta = gov_row.governance_metadata or {}

            # Latest drift report for this governance model — provides the full
            # live-metrics snapshot computed by DriftDetector.check_drift().
            latest_drift = (await self.db.execute(
                _sa_select(_DriftReport).where(
                    _DriftReport.model_id == gov_row.id
                ).order_by(_DriftReport.report_timestamp.desc()).limit(1)
            )).scalar_one_or_none()

            drift_advisory[model_name] = {
                "challenger_recommended": gov_meta.get("challenger_recommended", False),
                "drift_recommendation":  gov_meta.get("drift_recommendation"),
                "last_check":            (
                    latest_drift.report_timestamp.isoformat()
                    if latest_drift else None
                ),
                "last_drift_score": (
                    float(latest_drift.drift_score)
                    if latest_drift and latest_drift.drift_score is not None
                    else None
                ),
                "live_metrics": (
                    latest_drift.distribution_metrics
                    if latest_drift else {}
                ),
            }

        # Promotion reports live alongside C3 incident reports.
        report_dir = Path(__file__).parent.parent / "incident_reports"

        return build_promotion_report(
            challengers=challengers,
            champions=champions,
            report_dir=report_dir,
            run_id=self.cp.run_id,
            drift_advisory=drift_advisory,
            mlflow_run_id=self._e3_mlflow_run_id,
            event_backtest=event_backtest,
        )

    def _run_event_backtest_f1(self) -> Optional[Dict[str, Any]]:
        """Run the F1 event-driven backtest on the XGBoost CPCV OOF paths.

        Loads the CPCV OOF from the checkpoint, re-derives the vectorized
        per-period Sharpe reference via ``compute_dsr_and_pbo`` (fast — pure
        NumPy), runs the event-driven engine with the production defaults
        (1-bar latency, long_only, CNC, 100K notional, 5 bps slippage), and
        returns ``EventBacktestReport.as_dict()`` for inclusion in the C2
        Promotion Report.

        Returns ``None`` when no CPCV OOF is available (e.g. first run with
        a pre-A5 checkpoint that has no OOF bundle).

        Non-fatal: caller wraps in try/except.
        """
        from app.ml.evaluation.event_backtest import run_event_backtest
        from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo

        if self.cpcv_oof is None or not self.cpcv_oof.get("paths"):
            logger.warning(
                "F1: CPCV OOF not available (pre-A5 checkpoint or step-5 "
                "not yet complete) — skipping event-driven backtest."
            )
            return None

        paths = self.cpcv_oof["paths"]
        if not paths:
            return None

        # Re-derive vectorized per-period Sharpes so we have a fresh reference
        # for the agreement check.  compute_dsr_and_pbo is fast (NumPy only).
        # Supply trial_sharpes when available (Phase-B §3.5): collapses the DSR
        # sensitivity band from (N=1, N_upper) to (N=1, N_eff, N_upper) using
        # the actual HPO trial distribution.  Degrades gracefully to the
        # path-proxy if trial_sharpes is None (checkpoint resume or pre-§3.5).
        try:
            vec_result = compute_dsr_and_pbo(
                paths,
                mode="long_only",
                entry_price=1_000.0,
                trial_sharpes=self.xgboost_trainer.trial_sharpes,
            )
            vec_pp_sharpes: List[float] = vec_result["path_sharpes_per_period"]
        except Exception as _vec_exc:
            logger.warning(
                "F1: vectorized DSR reference failed (%s) — "
                "agreement check will report INSUFFICIENT_DATA.", _vec_exc,
            )
            vec_pp_sharpes = []

        report = run_event_backtest(
            paths,
            vectorized_path_sharpes_per_period=vec_pp_sharpes or None,
            latency_bars=1,
            mode="long_only",
            notional=100_000.0,
            product_type="CNC",
            entry_price=1_000.0,
            slippage_bps=5.0,
            periods_per_year=252,
            risk_free_rate_annual=0.05,
            agreement_tolerance=0.20,
        )
        return report.as_dict()

    async def _save_training_results(self) -> None:
        if self.results:
            out = self.output_dir / f"training_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(out, 'w') as fh:
                json.dump(self.results.to_dict(), fh, indent=2, default=str)
            logger.info("Training results saved → %s", out)

    async def _save_error_state(self, error: Exception) -> None:
        out = self.output_dir / f"error_state_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        state = {
            'error':        str(error),
            'traceback':    traceback.format_exc(),
            'config':       self.config.to_dict(),
            'symbols':      self.symbols,
            'memory_usage': self._get_memory_usage(),
            'timestamp':    datetime.now().isoformat(),
            'checkpoint':   str(self.cp._file),
            'next_pending': self.cp.next_pending(),
        }
        with open(out, 'w') as fh:
            json.dump(state, fh, indent=2, default=str)
        logger.error("Error state saved → %s", out)


# ══════════════════════════════════════════════════════════════════════════════
# Version resolution
# ══════════════════════════════════════════════════════════════════════════════

# Matches the semver prefix of any stored model_version string.
# Registry stores values like "1.1.0_xgboost" — we parse the leading semver
# and discard the model-type suffix.
_SEMVER_PREFIX_RE: re.Pattern[str] = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


async def _resolve_model_version(
    db_session: AsyncSession,
    output_dir: Path,
    fresh: bool,
) -> str:
    """Return the model version that this training run should use.

    Resume path (``fresh=False`` with an existing checkpoint):
        Read the version locked in ``checkpoint.json``.  This guarantees the
        value matches what ``CheckpointManager._enforce_compat`` expects — a
        mismatch would abort the run as a model-affecting config change.

    Fresh path (``fresh=True`` or no checkpoint on disk):
        Query ``ml_model_metadata`` for every registered ``model_version``,
        parse the semver prefix, find the maximum, and bump the patch component.
        Falls back gracefully to ``TrainingConfig.model_version`` (the bootstrap
        seed) when the registry is empty (first ever run) or if the DB query
        fails for any reason.

    Args:
        db_session:  Live async SQLAlchemy session (read-only use here).
        output_dir:  The orchestrator's output directory; ``checkpoints/`` is
                     expected directly inside it.
        fresh:       Mirrors the ``--fresh`` CLI flag / auto-detection result.

    Returns:
        A semver string, e.g. ``"1.1.2"``.
    """
    if not fresh:
        cp_file = output_dir / "checkpoints" / "checkpoint.json"
        if cp_file.exists():
            try:
                with open(cp_file) as fh:
                    state = json.load(fh)
                version: str | None = state.get("config", {}).get("model_version")
                if version:
                    logger.info(
                        "Version resolution — resume: locked version=%s (from %s)",
                        version, cp_file,
                    )
                    return version
            except (json.JSONDecodeError, KeyError, OSError) as exc:
                logger.warning(
                    "Version resolution — could not read checkpoint (non-fatal, "
                    "will query registry instead): %s", exc,
                )

    # Fresh run (or checkpoint unreadable) — derive the next version from the DB.
    seed = TrainingConfig().model_version
    try:
        result = await db_session.execute(
            text("SELECT model_version FROM ml_model_metadata")
        )
        rows: list[str] = [r for r in result.scalars().all() if r]

        highest: tuple[int, int, int] = (0, 0, 0)
        for raw in rows:
            m = _SEMVER_PREFIX_RE.match(raw)
            if m:
                candidate = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if candidate > highest:
                    highest = candidate

        if highest == (0, 0, 0):
            logger.info(
                "Version resolution — registry empty: using bootstrap seed %s", seed,
            )
            return seed

        major, minor, patch = highest
        next_version = f"{major}.{minor}.{patch + 1}"
        logger.info(
            "Version resolution — fresh run: %d.%d.%d → %s",
            major, minor, patch, next_version,
        )
        return next_version

    except Exception as exc:
        logger.warning(
            "Version resolution — DB query failed (%s), falling back to seed %s",
            exc, seed,
        )
        return seed


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    parser = argparse.ArgumentParser(description="Cortex AI Production ML Training Orchestrator")
    parser.add_argument(
        "--fresh",
        action="store_true",
        default=False,
        help=(
            "Ignore any existing checkpoint and start a completely fresh run.  "
            "Existing checkpoint artifacts are NOT deleted."
        ),
    )
    parser.add_argument(
        "--feedback-weights",
        metavar="PATH",
        default=None,
        help=(
            "Path to a feedback_weights_*.parquet bundle produced by "
            "scripts/build_feedback_weights.py.  When provided, the per-sample "
            "B1+B2 weights are merged into XGBoost (CPCV + final refit) and "
            "GRU (tf.data pipeline) training.  Omit for unweighted training."
        ),
    )
    args = parser.parse_args()

    output_dir = Path("models/production")

    # Auto-detect whether a resumable checkpoint exists
    has_checkpoint = find_checkpoint(output_dir)
    if has_checkpoint and not args.fresh:
        logger.info("Incomplete checkpoint detected — resuming from last completed step.")
        logger.info("Run with --fresh to start a new run instead.")
    elif args.fresh:
        logger.info("--fresh flag set — starting a new training run.")
    else:
        logger.info("No existing checkpoint — starting a new training run.")

    fresh = args.fresh or not has_checkpoint

    # Database setup
    settings = get_settings()
    engine   = create_async_engine(
        str(settings.DATABASE_URL),
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        model_version = await _resolve_model_version(session, output_dir, fresh)

    config = TrainingConfig(
        n_symbols=2551,
        lookback_years=10,
        xgboost_trials=100,
        gru_trials=5,
        gru_n_symbols=200,
        model_version=model_version,
    )

    async with async_session() as session:
        orchestrator = ProductionTrainingOrchestrator(
            db_session=session,
            config=config,
            output_dir=output_dir,
            fresh=fresh,
            feedback_weights_path=args.feedback_weights,
        )

        try:
            results = await orchestrator.run()

            print("\n" + "=" * 100)
            print("EXECUTIVE SUMMARY")
            print("=" * 100)
            print(f"Training Duration : {results.training_duration:.2f}s")
            print(f"Total Samples     : {results.total_samples:,}")
            print(f"Symbols Trained   : {len(results.symbols)}")
            print(f"Model Version     : {results.config.model_version}")
            print("=" * 100)
            return results

        except Exception as e:
            logger.error("Production training failed: %s", e)
            raise
        finally:
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
