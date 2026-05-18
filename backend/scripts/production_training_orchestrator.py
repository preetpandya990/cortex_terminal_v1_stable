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
import sys
import time
import traceback
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

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
    analyze_symbol_data_quality,
    MIN_BARS_FOR_TRAINING,
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
from app.ml.training.exceptions import MissingReturnsError, StaleCheckpointError
from app.ml.model_registry import ModelRegistry
from app.ml.inference.onnx_converter import ONNXConverter

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
    n_symbols: int = 2551
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
    # 1024 amortises WSL2 PCIe transfer overhead without exceeding 1592 MB VRAM.
    # 2048 OOM'd due to XLA scratch buffers on top of batch activations.
    batch_size: int = 1024

    # Ensemble
    ensemble_optimization_metric: str = 'sharpe_ratio'
    min_confidence_threshold: float = 0.4

    # Model versioning
    model_version: str = "1.0.0"

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
    def build(cp: CheckpointManager) -> Any:
        import tensorflow as tf

        class _Callback(tf.keras.callbacks.Callback):
            def on_epoch_end(self, epoch: int, logs: Optional[Dict] = None) -> None:
                # Save weights to a numbered checkpoint file.
                weight_path = str(cp.gru_epoch_weights_dir / f"epoch_{epoch:04d}.weights.h5")
                self.model.save_weights(weight_path)
                # Persist state atomically — a crash here only loses this epoch's entry.
                cp.save_gru_training_state(
                    last_epoch=epoch,
                    logs={k: float(v) for k, v in (logs or {}).items()},
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

        # Models
        self.xgboost_trainer: Optional[XGBoostTrainer] = None
        self.gru_trainer: Optional[GRUTrainer] = None
        self.ensemble_trainer: Optional[EnsembleTrainer] = None

        # GRU evaluation subset (kept after step 6 for steps 7-8)
        self.gru_eval_X:       Optional[np.ndarray] = None
        self.gru_eval_y:       Optional[np.ndarray] = None
        self.gru_eval_returns: Optional[np.ndarray] = None  # h-bar forward returns

        self.results: Optional[TrainingResults] = None

        self._setup_logging()
        logger.info("Production Training Orchestrator initialised  run_id=%s", self.cp.run_id)
        logger.info("Checkpoint file : %s", self.cp._file.resolve())
        logger.info("Checkpoint dir  : %s", self.cp._dir.resolve())
        logger.info("Configuration: %s", json.dumps(self.config.to_dict(), indent=2))

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
                logger.info("→ Resuming: step_1 skipped  (%d symbols from checkpoint)", len(self.symbols))
            else:
                t0 = time.monotonic()
                await self._select_symbols_and_assess_quality()
                cp.save_symbols(self.symbols)
                cp.mark_done("step_1_symbols", time.monotonic() - t0)

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
            else:
                t0 = time.monotonic()
                await self._compute_and_validate_features()
                logger.info("Saving step_2 checkpoint (%d DataFrames)...", len(self.raw_features_data))
                cp.save_features(self.raw_features_data)
                cp.mark_done("step_2_features", time.monotonic() - t0)

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
                cp.mark_done("step_3_targets", time.monotonic() - t0)

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
                cp.mark_done("step_4_splits", time.monotonic() - t0)

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
                logger.info("→ Resuming: step_5 skipped  (XGBoost model from checkpoint)")
            else:
                # Ensure targets_data is in memory (may need lazy-load if step 3 was resumed)
                if not hasattr(self, 'targets_data') or not self.targets_data:
                    self.targets_data, self.class_weights = cp.load_targets()
                t0 = time.monotonic()
                xgboost_results = await self._train_xgboost_with_optimization(cv_plan)
                logger.info("Saving step_5 checkpoint (XGBoost model)...")
                cp.save_xgboost(
                    xgboost_results['model'],
                    xgboost_results['best_params'],
                    meta={
                        'feature_importance': xgboost_results.get('feature_importance', {}),
                        'training_samples':   xgboost_results.get('training_samples', 0),
                        'validation_samples': xgboost_results.get('validation_samples', 0),
                    },
                )
                cp.mark_done("step_5_xgboost", time.monotonic() - t0)

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
                cp.mark_done("step_6_gru", time.monotonic() - t0)

            # ── Step 7 ────────────────────────────────────────────────────────
            logger.info("\n" + "=" * 100)
            logger.info("STEP 7: ENSEMBLE CREATION AND WEIGHT OPTIMIZATION")
            logger.info("=" * 100)

            if cp.is_done("step_7_ensemble"):
                weights = cp.load_ensemble_weights()
                self.ensemble_trainer = EnsembleTrainer(
                    xgboost_model=self.xgboost_trainer.model,
                    gru_model=self.gru_trainer.model,
                    weights=weights,
                )
                ensemble_results = {
                    'ensemble':           self.ensemble_trainer,
                    'optimized_weights':  weights,
                    'validation_samples': len(self.gru_eval_y),
                }
                logger.info("→ Resuming: step_7 skipped  (ensemble weights from checkpoint)")
            else:
                t0 = time.monotonic()
                ensemble_results = await self._create_and_optimize_ensemble(cv_plan)
                logger.info("Saving step_7 checkpoint (ensemble weights)...")
                cp.save_ensemble(ensemble_results['optimized_weights'])
                cp.mark_done("step_7_ensemble", time.monotonic() - t0)

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
                cp.mark_done("step_8_evaluation", time.monotonic() - t0)

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
                cp.mark_done("step_9_onnx", time.monotonic() - t0)

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
                cp.mark_done("step_10_registry", time.monotonic() - t0)

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

            logger.info("=" * 100)
            logger.info("TRAINING PIPELINE COMPLETED SUCCESSFULLY  run_id=%s", cp.run_id)
            logger.info("Duration: %.2fs", training_duration)
            logger.info("Total samples: %d", self.results.total_samples)
            logger.info("=" * 100)

            return self.results

        except Exception as e:
            logger.error("Training pipeline failed: %s", e)
            logger.error("Traceback:\n%s", traceback.format_exc())
            await self._save_error_state(e)
            raise

    # ══════════════════════════════════════════════════════════════════════════
    # Step implementations
    # ══════════════════════════════════════════════════════════════════════════

    async def _select_symbols_and_assess_quality(self) -> None:
        logger.info(
            "Selecting top %d liquid symbols  "
            "(min_bars=%d  min_relative_completeness=%.0f%%)",
            self.config.n_symbols,
            MIN_BARS_FOR_TRAINING,
            MIN_RELATIVE_COMPLETENESS_PCT,
        )

        # DB-level pre-filter: only symbols with ≥ MIN_BARS_FOR_TRAINING candles
        # in the lookback window are returned.
        self.symbols = await get_top_liquid_symbols(
            db=self.db,
            n=self.config.n_symbols,
            timeframe='1D',
            lookback_days=self.config.lookback_years * 365,
            min_data_points=MIN_BARS_FOR_TRAINING,
        )

        if not self.symbols:
            raise ValueError("No symbols selected. Check database data availability.")

        logger.info("✓ Pre-selected %d symbols  top_10=%s", len(self.symbols), self.symbols[:10])

        # Per-symbol quality audit: relative completeness + data-integrity gates
        logger.info("Auditing data quality for %d symbols...", len(self.symbols))
        quality_reports: list[dict] = []
        analysis_failed: list[str] = []

        for symbol in list(self.symbols):  # iterate a copy so we can mutate self.symbols
            try:
                report = await analyze_symbol_data_quality(
                    symbol=symbol,
                    timeframe='1D',
                    lookback_days=self.config.lookback_years * 365,
                    db=self.db,
                )
                quality_reports.append(report)
            except Exception as exc:
                logger.warning("  Quality audit failed for %s: %s", symbol, exc)
                analysis_failed.append(symbol)
                self.symbols.remove(symbol)

        if analysis_failed:
            logger.warning(
                "  Removed %d symbol(s) due to audit errors: %s",
                len(analysis_failed), analysis_failed[:20],
            )

        if len(self.symbols) < 10:
            raise ValueError(f"Insufficient symbols after quality audit: {len(self.symbols)}")

        # Apply quality gates using the is_qualified flag (combines all sub-gates)
        if quality_reports:
            qualified_pairs  = [(s, r) for s, r in zip(self.symbols, quality_reports) if r["is_qualified"]]
            disqualified     = [(s, r) for s, r in zip(self.symbols, quality_reports) if not r["is_qualified"]]

            if disqualified:
                logger.warning(
                    "  Disqualified %d symbol(s)  (first 20 shown):",
                    len(disqualified),
                )
                for sym, rep in disqualified[:20]:
                    logger.warning(
                        "    ✗ %s — %s  (bars=%d  completeness=%.1f%%)",
                        sym,
                        rep.get("disqualification_reason", "unknown"),
                        rep.get("actual_bars", 0),
                        rep.get("completeness_pct", 0.0),
                    )

            completeness_vals = [r["completeness_pct"] for _, r in qualified_pairs]
            logger.info(
                "✓ Quality gate passed  qualified=%d  disqualified=%d  "
                "avg_completeness=%.1f%%  min_completeness=%.1f%%  "
                "method=%s",
                len(qualified_pairs),
                len(disqualified),
                float(np.mean(completeness_vals)) if completeness_vals else 0.0,
                float(np.min(completeness_vals))  if completeness_vals else 0.0,
                quality_reports[0].get("completeness_method", "relative_to_own_listing_period"),
            )

            self.symbols = [s for s, _ in qualified_pairs][: self.config.n_symbols]

        logger.info("✓ Final symbol count: %d", len(self.symbols))

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

        ts_parts = [
            self.targets_data[s]['features_df']['timestamp'].values
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
        X_list, y_list, ts_list, r_list = [], [], [], []
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

        X_all  = np.vstack(X_list)
        y_all  = np.concatenate(y_list)
        r_all  = np.concatenate(r_list)
        ts_all = np.concatenate(ts_list)
        del X_list, y_list, r_list, ts_list

        logger.info(
            "XGBoost panel prepared  samples=%d  features=%d  class_dist=%s",
            len(X_all), X_all.shape[1],
            dict(zip(*np.unique(y_all, return_counts=True))),
        )

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
            })
        del oof_proba, oof_idx
        gc.collect()
        logger.info("✓ CPCV OOF assembled  paths=%d  (%d combos refit)", n_paths, n_splits)

        # ── Final production model: all-data refit at the fixed tuned round
        # count (owner decision — a live trading model must see the most recent
        # regime; the CPCV OOF above remains the honest performance estimate).
        prod = await asyncio.to_thread(
            self.xgboost_trainer.fit_fixed,
            X_all, y_all,
            params=best_params,
            num_boost_round=best_rounds,
            class_weights=None,
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
            cursor = 0
            for symbol, y_start, n_tr, n_va in gru_plan:
                df_raw = self.targets_data[symbol]['features_df']
                y_sym  = self.targets_data[symbol]['target']
                r_sym  = self.targets_data[symbol]['forward_return']
                norm_df = normalize_features(
                    df_raw, method='rolling', window=seq_len, feature_cols=feature_names
                )
                X_seq, _, _ = create_sequences(norm_df, seq_len, feature_names)
                m       = len(X_seq)
                take_tr = min(n_tr, m)
                v0      = take_tr + purge_gap                  # skip the purge gap
                take_va = max(0, min(n_va, m - v0, n_val_total - cursor))
                if take_va <= 0:
                    continue
                X_val[cursor:cursor + take_va] = X_seq[v0:v0 + take_va]
                y_val[cursor:cursor + take_va] = y_sym[y_start + v0:y_start + v0 + take_va]
                r_val[cursor:cursor + take_va] = r_sym[y_start + v0:y_start + v0 + take_va]
                cursor += take_va

            X_val = X_val[:cursor]
            y_val = y_val[:cursor]
            r_val = r_val[:cursor]

            # Deterministic memory cap (replaces np.random.choice): the val
            # pool is already purged, so a FIXED-seed uniform sample is
            # unbiased AND identical across resumes.
            if len(X_val) > VAL_CAP:
                keep = np.sort(
                    np.random.default_rng(42).choice(len(X_val), VAL_CAP, replace=False)
                )
                X_val = X_val[keep].copy()
                y_val = y_val[keep].copy()
                r_val = r_val[keep].copy()
            logger.info("✓ GRU val arrays built: %d sequences (cap=%d)", len(X_val), VAL_CAP)

            cp.save_gru_eval_arrays(
                X_val, y_val, r_val,
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
        self.gru_eval_X       = X_val
        self.gru_eval_y       = y_val
        self.gru_eval_returns = r_val

        # ── Build X_train (always rebuilt — too large to persist) ─────────────
        # Load targets_data if not already freed (sub-A path freed it above)
        # On the sub-A resume path we need to reload targets_data to rebuild X_train.
        n_train = n_train_total
        logger.info(
            "Building X_train from targets_data  n_train=%d  (%.2f GB float16)...",
            n_train, n_train * seq_len * n_feat * 2 / 1e9,
        )

        # Reload targets_data for the training sequence rebuild
        if not hasattr(self, 'targets_data') or not self.targets_data:
            logger.info("  Loading targets_data from checkpoint for X_train rebuild...")
            self.targets_data, _ = self.cp.load_targets()

        # Per symbol: the FIRST n_tr (chronological) sequences = train. The
        # purge gap + val tail are excluded, so no train label window overlaps
        # that symbol's val period — identical to the persisted purged plan.
        # float16 halves RAM; cast to float32 in the tf.data pipeline below.
        X_train = np.empty((n_train, seq_len, n_feat), dtype=np.float16)
        y_train = np.empty(n_train, dtype=np.int32)
        cursor  = 0
        for symbol, y_start, n_tr, n_va in gru_plan:
            if symbol not in self.targets_data:
                continue
            df_raw = self.targets_data[symbol]['features_df']
            y_sym  = self.targets_data[symbol]['target']
            norm_df = normalize_features(
                df_raw, method='rolling', window=seq_len, feature_cols=feature_names
            )
            X_seq, _, _ = create_sequences(norm_df, seq_len, feature_names)
            # Clamp to actual sequences produced — NaN-drops in
            # normalize_features can yield fewer rows than planned in pass 1.
            take = min(n_tr, len(X_seq), n_train - cursor)
            if take <= 0:
                continue
            X_train[cursor:cursor + take] = X_seq[:take]
            y_train[cursor:cursor + take] = y_sym[y_start:y_start + take]
            cursor += take

        # Trim to actual filled rows (gru_plan counts may exceed actual after NaN drops)
        X_train = X_train[:cursor]
        y_train = y_train[:cursor]

        # Free targets_data again (done with it)
        del self.targets_data
        gc.collect()
        logger.info("✓ X_train built  shape=%s  (planned=%d  actual=%d)", X_train.shape, n_train, cursor)

        logger.info(
            "GRU data summary:  train=%d  (%.2f GB)  val=%d  class_dist(val)=%s",
            len(X_train), X_train.nbytes / 1e9, len(X_val),
            dict(zip(*np.unique(y_val, return_counts=True))),
        )

        # ── Convert X_train → tf.data before spawning thread ──────────────────
        if np.any(np.isnan(X_train)) or np.any(np.isinf(X_train)):
            raise ValueError("X_train contains NaN/inf values")

        y_train_int = y_train.astype(np.int32)
        n_train_samples = len(X_train)

        # Pin source tensors to CPU — TF transfers one batch at a time to GPU.
        # Cast float16→float32 per batch (cheap op; fixed parallelism=2 prevents
        # unbounded float32 materialization that exhausted RAM with AUTOTUNE).
        # Prefetch capped at 2 for same reason — AUTOTUNE grew the buffer each epoch.
        with tf.device('/CPU:0'):
            train_ds = (
                tf.data.Dataset
                .from_tensor_slices((X_train, y_train_int))
                .shuffle(buffer_size=5_000, reshuffle_each_iteration=True)
                .batch(self.config.batch_size)
                .map(
                    lambda x, y: (tf.cast(x, tf.float32), y),
                    num_parallel_calls=2,
                )
                .prefetch(2)
            )

        # Free numpy arrays — TF dataset retains its own reference on CPU.
        del X_train, y_train, y_train_int
        gc.collect()
        try:
            import ctypes
            ctypes.CDLL('libc.so.6').malloc_trim(0)
            logger.info("✓ malloc_trim: freed heap pages returned to OS")
        except Exception as trim_err:
            logger.warning("malloc_trim unavailable (%s) — RSS may stay elevated", trim_err)

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
            last_epoch = state["last_epoch"]
            initial_epoch = last_epoch + 1
            weight_path = str(cp.gru_epoch_weights_dir / f"epoch_{last_epoch:04d}.weights.h5")
            # Build model so we can load weights into it
            tmp_model = self.gru_trainer.build_model(best_params)
            tmp_model.load_weights(weight_path)
            self.gru_trainer.model = tmp_model
            logger.info(
                "→ sub-C resumed: training from epoch %d  (weights: %s)",
                initial_epoch, weight_path,
            )

        epoch_cb = EpochCheckpointCallback.build(cp)

        logger.info(
            "Training final GRU model  batch=%d  max_epochs=%d  initial_epoch=%d",
            self.config.batch_size, self.config.max_epochs, initial_epoch,
        )

        await asyncio.to_thread(
            self._run_gru_fit,
            train_ds,
            X_val,
            y_val,
            best_params,
            initial_epoch,
            epoch_cb,
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
        train_ds: Any,
        X_val: np.ndarray,
        y_val: np.ndarray,
        best_params: Dict[str, Any],
        initial_epoch: int,
        epoch_cb: Any,
    ) -> None:
        """
        Run model.fit() with OOM recovery — extracted so it can run via asyncio.to_thread.

        OOM recovery strategy
        ---------------------
        If TF raises ``ResourceExhaustedError`` (GPU VRAM exhausted), this method:

        1. Clears the Keras session to release all GPU-resident model weights and
           optimizer states.
        2. Rebatches the existing ``tf.data`` pipeline to half the current batch
           size — no raw arrays needed since ``train_ds`` is backed by pinned CPU
           tensors and is re-iterable.
        3. Resets the GRU trainer's model to ``None`` so ``train()`` rebuilds it
           with fresh weights at the smaller batch.
        4. Repeats up to ``_MAX_OOM_RETRIES`` times, halving batch size each time.

        The minimum batch floor is 32 to avoid degenerate single-sample training.
        If all retries are exhausted, a ``RuntimeError`` is raised with actionable
        guidance (reduce ``gru_n_symbols`` or ``n_features``, or increase
        ``_GPU_VRAM_LIMIT_MB`` in ``gru_trainer.py``).
        """
        import tensorflow as tf
        import gc

        # XLA kernel fusion: ~20 % throughput improvement on Ampere GPUs.
        # Set here (not in configure_gpu) so it applies only when model.fit()
        # is active, not during HPO tuner search or evaluation passes.
        tf.config.optimizer.set_jit(True)

        _MAX_OOM_RETRIES: int = 2
        current_ds   = train_ds
        batch_size   = self.config.batch_size   # tracked for logging only
        cur_epoch    = initial_epoch

        for attempt in range(_MAX_OOM_RETRIES + 1):
            try:
                self.gru_trainer.train(
                    params=best_params,
                    epochs=self.config.max_epochs,
                    class_weight=self.class_weights,
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
                        "Remediation: reduce gru_n_symbols / n_features, "
                        "or increase _GPU_VRAM_LIMIT_MB in gru_trainer.py.",
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
                    "clearing Keras session and rebatching to %d.",
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

                # ── Recovery step 2: rebatch existing dataset ─────────────────
                # train_ds is backed by pinned CPU tensors (from_tensor_slices)
                # and is re-iterable.  unbatch() peels the current batch dim;
                # batch(new_batch) regroups at the smaller size.  The shuffle
                # and cast-to-float32 steps are preserved in the parent pipeline.
                current_ds = current_ds.unbatch().batch(new_batch).prefetch(2)
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

    async def _create_and_optimize_ensemble(self, cv_plan: Dict[str, Any]) -> Dict[str, Any]:
        logger.info("Optimizing ensemble weights on %d validation samples...", len(self.gru_eval_y))

        xgboost_model = self.xgboost_trainer.model
        gru_model     = self.gru_trainer.model

        self.ensemble_trainer = EnsembleTrainer(
            xgboost_model=xgboost_model,
            gru_model=gru_model,
            weights={'xgboost': 0.6, 'gru': 0.4},
        )

        X_val_seq = self.gru_eval_X
        X_val_tab = self.gru_eval_X[:, -1, :]
        y_val     = self.gru_eval_y

        eff_metric = self.config.ensemble_optimization_metric
        if self.gru_eval_returns is None:
            raise MissingReturnsError(
                "Forward returns (gru_eval_returns) are unavailable at ensemble "
                "weight optimisation. Refusing to fall back to an accuracy "
                "objective — silently changing the model-selection metric is "
                "exactly the RC-1 failure that shipped an unvalidated model. "
                "Re-run with --fresh so forward returns are regenerated."
            )

        optimized_weights = await asyncio.to_thread(
            self.ensemble_trainer.optimize_weights,
            X_val_tab, X_val_seq, y_val,
            returns=self.gru_eval_returns,
            metric=eff_metric,
        )
        self.ensemble_trainer.weights = optimized_weights

        logger.info("✓ Ensemble optimised  weights=%s", optimized_weights)
        return {
            'ensemble':           self.ensemble_trainer,
            'optimized_weights':  optimized_weights,
            'validation_samples': len(y_val),
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

            xgb_metrics = asdict(evaluation_results["xgboost"])
            xgb_metrics["ensemble_weight"] = xgb_weight

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

            xgb_meta = await registry.register_model(
                version                 = f"{self.config.model_version}_xgboost",
                model_type              = "xgboost",
                artifact_path           = model_paths["xgboost"],
                inference_artifact_path = xgb_inference,
                metrics                 = xgb_metrics,
                metadata                = {
                    "features":         feature_names,   # → training_features column
                    "n_symbols":        len(self.symbols),
                    "n_features":       len(feature_names),
                    "training_samples": self._calculate_total_samples(),
                },
                feature_version = "1.0.0",
                status          = "development",
                overwrite       = True,
            )
            logger.info("  ✓ XGBoost registered  id=%s  inference=%s  n_features=%d",
                        xgb_meta.id, Path(xgb_inference).suffix if xgb_inference else "none",
                        len(feature_names))

            gru_metrics = asdict(evaluation_results["gru"])
            gru_metrics["ensemble_weight"] = gru_weight

            gru_meta = await registry.register_model(
                version                 = f"{self.config.model_version}_gru",
                model_type              = "gru",
                artifact_path           = model_paths["gru"],
                inference_artifact_path = gru_inference,
                metrics                 = gru_metrics,
                metadata                = {
                    "features":         feature_names,   # → training_features column
                    "n_symbols":        len(self.symbols),
                    "n_features":       len(feature_names),
                    "training_samples": self._calculate_total_samples(),
                },
                feature_version = "1.0.0",
                status          = "development",
                overwrite       = True,
            )
            logger.info("  ✓ GRU registered  id=%s  inference=%s  n_features=%d",
                        gru_meta.id, Path(gru_inference).suffix if gru_inference else "none",
                        len(feature_names))

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

    def _generate_data_quality_report(self) -> Dict[str, Any]:
        return {"status": "complete", "n_symbols": len(self.symbols)}

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

    config = TrainingConfig(
        n_symbols=2551,
        lookback_years=10,
        xgboost_trials=100,
        gru_trials=5,
        gru_n_symbols=200,
        model_version="1.0.0",
    )

    async with async_session() as session:
        orchestrator = ProductionTrainingOrchestrator(
            db_session=session,
            config=config,
            output_dir=output_dir,
            fresh=fresh,
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
