"""
Cortex AI — Production ML Training Checkpoint Manager
======================================================
Crash-resilient, fully-resumable training pipeline.

Every orchestrator step persists its outputs to disk immediately upon
completion.  On restart the orchestrator skips completed steps, reloads
their artifacts from disk, and continues from the next pending step.

Step 6 (GRU) has three internal sub-checkpoints so a crash at any
point inside that step is recoverable without re-running HPO or
re-building the 5.5 GB sequence array:

    Sub-A  eval_X / eval_y + GRU plan metadata
           Saved right after the train/val split.
           On resume: skip sequence building, load eval arrays.

    Sub-B  best_params.json
           Saved after Keras Tuner search() completes.
           On resume: skip HPO, load best params.

    Sub-C  epoch_weights/  +  training_state.json
           Updated after every epoch via EpochCheckpointCallback.
           On resume: load weights from last completed epoch,
           call model.fit(initial_epoch=last+1, ...).

Checkpoint directory layout
───────────────────────────
<output_dir>/checkpoints/
├── checkpoint.json              # Master state (atomic POSIX write)
├── step_1_symbols/
│   └── symbols.json
├── step_2_features/
│   ├── manifest.json            # symbol → parquet path + meta
│   └── <safe_name>.parquet      # one per symbol, zstd-compressed
├── step_3_targets/
│   ├── manifest.json
│   ├── class_weights.json
│   ├── <safe_name>_features.parquet
│   └── <safe_name>_target.npy
├── step_4_splits/
│   └── splits.json
├── step_5_xgboost/
│   ├── model.json               # XGBoost Booster native format
│   ├── params.json
│   └── meta.json                # training / validation sample counts
├── step_6_gru/
│   ├── eval_X.npy               # sub-A: validation sequences
│   ├── eval_y.npy
│   ├── eval_meta.json           # sub-A: gru_plan + split indices + class_weights
│   ├── best_params.json         # sub-B: Keras Tuner best hyperparameters
│   ├── training_state.json      # sub-C: last completed epoch
│   ├── epoch_weights/           # sub-C: Keras weight files per epoch
│   └── model/                   # final Keras SavedModel
├── step_7_ensemble/
│   └── weights.json
└── step_8_evaluation/
    └── results.json

Author: Cortex AI Team
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from app.ml.training.exceptions import StaleCheckpointError

logger = logging.getLogger(__name__)

# Canonical step order — must match orchestrator exactly.
PIPELINE_STEPS: List[str] = [
    "step_1_symbols",
    "step_2_features",
    "step_3_targets",
    "step_4_splits",
    "step_5_xgboost",
    "step_6_gru",
    "step_7_ensemble",
    "step_8_evaluation",
    "step_9_onnx",
    "step_10_registry",
]

# Bump whenever a change invalidates existing on-disk checkpoint artifacts so a
# stale checkpoint can never be silently resumed.
#   v2 — mandatory forward-return persistence (eval_r.npy); a pre-v2 checkpoint
#        lacks it and would trigger a silent Sharpe→accuracy degradation (RC-1).
#   v3 — step-4 stores a Combinatorial-Purged-CV plan (cv_plan.json) instead of
#        the old decorative date-range walk-forward splits (splits.json). The
#        leaky stratified-random / TRAIN_FRAC split paths are removed, so a v2
#        checkpoint's step-4..step-8 artifacts are methodologically invalid.
#   v4 — A5: the CPCV OOF bundle carries a per-row ``symbol`` and the GRU
#        sub-A eval arrays carry per-row ``symbol`` + ``timestamp``. These are
#        the join keys for the leakage-free ensemble net-DSR weighting; a v3
#        checkpoint lacks them, so its step-5/6 artifacts cannot drive A5.
#   v5 — WS2 (ML_FIX_IMPLEMENTATION_PLAN.md): the feature contract became
#        version-selected (66-feature PIT rank-normalized v2 vs legacy 69).
#        A v4 checkpoint's feature/sequence/target artifacts carry no
#        feature-set provenance, so resuming one under v5 code could silently
#        train on the wrong contract. StaleCheckpointError is the correct,
#        loud outcome for every pre-existing run dir.
SCHEMA_VERSION = 5

# Config keys whose value determines the shape/meaning of every downstream
# artifact (features, sequences, targets, models). Changing any of them makes a
# resumed run silently mix incompatible data, so drift here is a HARD failure
# (not the warn-only treatment applied to operational keys).
_MODEL_AFFECTING_KEYS = frozenset({
    "n_features",
    "sequence_length",
    "include_fundamentals",
    "model_version",
    # WS2 — the feature-set contract (66 PIT rank-normalized vs legacy 69)
    # changes every artifact from step 2 onward.
    "feature_set_version",
    # Smoke runs (--n-symbols) shrink the universe; a smoke checkpoint must
    # never resume into (or be resumed by) a full run.
    "n_symbols",
})


class CheckpointManager:
    """
    Crash-resilient checkpoint manager for the ML training pipeline.

    Atomicity
    ---------
    All JSON state writes go through ``_atomic_json()``:  data is written
    to a ``.tmp`` sibling, then ``os.replace()`` renames it in place.
    ``os.replace`` is atomic on POSIX and on Windows ≥ Vista, so the master
    ``checkpoint.json`` is never left in a partially-written state.

    NumPy ``.npy`` files are written once (``np.save``).  If the process is
    killed mid-write the file will be missing or truncated; the header
    checksum will raise ``ValueError`` on load, which surfaces as a clear
    error message prompting the user to delete only that step's directory
    and re-run.

    Thread-safety
    -------------
    **Not thread-safe** — designed for use from a single async orchestrator
    where all checkpoint operations are sequential.
    """

    def __init__(
        self,
        checkpoint_dir: Path,
        config: Dict[str, Any],
        *,
        fresh: bool = False,
    ) -> None:
        """
        Initialise or resume a checkpoint session.

        Args:
            checkpoint_dir: Root directory for all checkpoint artifacts.
            config:         Current training config dict (saved for audit).
            fresh:          When True, ignore any existing checkpoint and
                            start a new run.  Existing artifact files are
                            NOT deleted — they are overwritten step by step
                            as new data is produced.
        """
        self._dir = Path(checkpoint_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "checkpoint.json"
        self._state: Dict[str, Any] = {}

        if not fresh and self._file.exists():
            self._load_state()
            # Hard gate FIRST: refuse to resume an incompatible checkpoint
            # (schema mismatch or model-affecting config drift). Only after
            # this passes do we surface non-fatal operational drift as a warning.
            _enforce_compat(self._state, config)
            _warn_config_drift(self._state.get("config", {}), config)
            logger.info(
                "Resuming training run  run_id=%s  completed=%s",
                self._state["run_id"],
                self._state["completed_steps"],
            )
        else:
            self._init_fresh(config)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def _init_fresh(self, config: Dict[str, Any]) -> None:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._state = {
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "config": config,
            "completed_steps": [],
            "step_durations_s": {},
            "started_at": datetime.now().isoformat(),
            "last_updated_at": datetime.now().isoformat(),
        }
        self._flush()
        logger.info("New training run  run_id=%s", run_id)

    def _load_state(self) -> None:
        with open(self._file) as fh:
            self._state = json.load(fh)

    def _flush(self) -> None:
        """Atomic state write — never leaves checkpoint.json partially written."""
        self._state["last_updated_at"] = datetime.now().isoformat()
        _atomic_json(self._file, self._state)

    # ── Query ──────────────────────────────────────────────────────────────────

    @property
    def run_id(self) -> str:
        return self._state["run_id"]

    @property
    def mlflow_run_id(self) -> Optional[str]:
        """E3 — MLflow run_id persisted so resumed runs reconnect to the same experiment run."""
        return self._state.get("mlflow_run_id")

    def save_mlflow_run_id(self, run_id: str) -> None:
        """Persist the MLflow run_id atomically.  Called once per fresh run after start_run()."""
        self._state["mlflow_run_id"] = run_id
        self._flush()

    def is_done(self, step: str) -> bool:
        """Return True if *step* completed successfully in this (or a previous) run."""
        return step in self._state["completed_steps"]

    def next_pending(self) -> Optional[str]:
        """Return the first step not yet completed, or None if all done."""
        for s in PIPELINE_STEPS:
            if s not in self._state["completed_steps"]:
                return s
        return None

    # ── Step completion ────────────────────────────────────────────────────────

    def mark_done(self, step: str, duration_s: float) -> None:
        """Record *step* as successfully completed and persist the state."""
        if step not in self._state["completed_steps"]:
            self._state["completed_steps"].append(step)
        self._state["step_durations_s"][step] = round(duration_s, 2)
        self._flush()
        logger.info(
            "✓ Checkpoint state updated: %s complete  (%.1fs)  file=%s",
            step, duration_s, self._file,
        )

    # ── Directory helper ───────────────────────────────────────────────────────

    def _step_dir(self, step: str) -> Path:
        d = self._dir / step
        d.mkdir(exist_ok=True)
        return d

    # ── Symbol → filesystem-safe name ─────────────────────────────────────────

    @staticmethod
    def _safe(symbol: str) -> str:
        """``NSE_EQ|INE123`` → ``NSE_EQ_INE123``  (pipe/slash replaced)."""
        return symbol.replace("|", "_").replace("/", "_").replace("\\", "_")

    # ══════════════════════════════════════════════════════════════════════════
    # Step 1 — Symbol list
    # ══════════════════════════════════════════════════════════════════════════

    def save_symbols(self, symbols: List[str]) -> None:
        path = self._step_dir("step_1_symbols") / "symbols.json"
        _atomic_json(path, {"symbols": symbols, "count": len(symbols)})
        logger.info("Checkpoint step_1 saved: %d symbols → %s", len(symbols), path)

    def load_symbols(self) -> List[str]:
        with open(self._dir / "step_1_symbols" / "symbols.json") as fh:
            return json.load(fh)["symbols"]

    # ══════════════════════════════════════════════════════════════════════════
    # Step 2 — Raw feature DataFrames
    # ══════════════════════════════════════════════════════════════════════════

    def save_features(self, raw_features_data: Dict[str, pd.DataFrame]) -> None:
        """
        Persist every symbol's feature DataFrame as zstd-compressed Parquet.

        The manifest also stores two scalar values used to reconstruct
        walk-forward splits without re-loading any Parquet file:
            max_timestamps  — max row-count across all symbols
            total_rows      — sum of all row-counts  (for _calculate_total_samples)
        """
        d = self._step_dir("step_2_features")
        manifest: Dict[str, Any] = {}
        total_rows = 0
        max_timestamps = 0
        for symbol, df in raw_features_data.items():
            path = d / f"{self._safe(symbol)}.parquet"
            df.to_parquet(path, index=True, compression="zstd")
            manifest[symbol] = str(path)
            total_rows += len(df)
            max_timestamps = max(max_timestamps, len(df))

        # Store scalars under a reserved key so they survive alongside the
        # per-symbol paths without a separate file.
        manifest["__meta__"] = {
            "total_rows": total_rows,
            "max_timestamps": max_timestamps,
            "n_symbols": len(raw_features_data),
        }
        _atomic_json(d / "manifest.json", manifest)
        logger.info(
            "Checkpoint step_2 saved: %d symbols, %d total rows, %d parquet files → %s",
            len(raw_features_data), total_rows, len(raw_features_data), d,
        )

    def load_features(self) -> Dict[str, pd.DataFrame]:
        """Load all symbol DataFrames.  Only call when step 3 is pending."""
        d = self._dir / "step_2_features"
        with open(d / "manifest.json") as fh:
            manifest = json.load(fh)
        data = {
            sym: pd.read_parquet(path)
            for sym, path in manifest.items()
            if sym != "__meta__"
        }
        logger.info("Checkpoint step_2: loaded %d feature DataFrames.", len(data))
        return data

    def load_features_meta(self) -> Dict[str, Any]:
        """Load only the scalar metadata — no Parquet I/O."""
        with open(self._dir / "step_2_features" / "manifest.json") as fh:
            manifest = json.load(fh)
        return manifest["__meta__"]

    # ══════════════════════════════════════════════════════════════════════════
    # Step 3 — Target arrays + aligned feature DataFrames + class weights
    # ══════════════════════════════════════════════════════════════════════════

    def save_targets(
        self,
        targets_data: Dict[str, Dict],
        class_weights: Dict[int, float],
    ) -> None:
        d = self._step_dir("step_3_targets")
        manifest: Dict[str, Dict[str, str]] = {}
        for symbol, entry in targets_data.items():
            safe = self._safe(symbol)
            feat_path = d / f"{safe}_features.parquet"
            tgt_path  = d / f"{safe}_target.npy"
            entry["features_df"].to_parquet(feat_path, index=True, compression="zstd")
            np.save(str(tgt_path), entry["target"])
            manifest[symbol] = {"features": str(feat_path), "target": str(tgt_path)}

        _atomic_json(d / "manifest.json", manifest)
        _atomic_json(d / "class_weights.json", {str(k): v for k, v in class_weights.items()})
        logger.info(
            "Checkpoint step_3 saved: %d symbols (features + targets) → %s",
            len(manifest), d,
        )

    def load_targets(self) -> Tuple[Dict[str, Dict], Dict[int, float]]:
        d = self._dir / "step_3_targets"
        with open(d / "manifest.json") as fh:
            manifest = json.load(fh)
        with open(d / "class_weights.json") as fh:
            cw_raw = json.load(fh)
        class_weights = {int(k): float(v) for k, v in cw_raw.items()}
        targets_data = {
            sym: {
                "features_df": pd.read_parquet(paths["features"]),
                "target":      np.load(paths["target"]),
            }
            for sym, paths in manifest.items()
        }
        logger.info("Checkpoint step_3: loaded %d symbols.", len(targets_data))
        return targets_data, class_weights

    # ══════════════════════════════════════════════════════════════════════════
    # Step 4 — Combinatorial Purged CV plan  (v3 — replaced date-range splits)
    # ══════════════════════════════════════════════════════════════════════════

    def save_cv_plan(self, plan: Dict[str, Any]) -> None:
        """Persist the CPCV plan: parameters (n_groups/n_test/horizon/embargo),
        n_splits/n_paths, group boundaries, the unique-timestamp axis
        fingerprint, and the one-time purged HPO-holdout descriptor.

        Deliberately small JSON — the 28 combinatorial splits are regenerated
        deterministically at step-5 from the rebuilt panel, with the axis
        fingerprint guarding resume reproducibility (a changed axis fails loud
        rather than silently producing different folds — A1 philosophy).
        """
        path = self._step_dir("step_4_splits") / "cv_plan.json"
        _atomic_json(path, plan)
        logger.info(
            "Checkpoint step_4 saved: CPCV plan  n_splits=%s  n_paths=%s → %s",
            plan.get("n_splits"), plan.get("n_paths"), path,
        )

    def load_cv_plan(self) -> Dict[str, Any]:
        with open(self._dir / "step_4_splits" / "cv_plan.json") as fh:
            plan = json.load(fh)
        logger.info(
            "Checkpoint step_4: CPCV plan loaded  n_splits=%s  n_paths=%s",
            plan.get("n_splits"), plan.get("n_paths"),
        )
        return plan

    # ══════════════════════════════════════════════════════════════════════════
    # Step 5 — XGBoost model
    # ══════════════════════════════════════════════════════════════════════════

    def save_xgboost(
        self,
        booster: Any,
        best_params: Dict[str, Any],
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        d = self._step_dir("step_5_xgboost")
        booster.save_model(str(d / "model.json"))
        _atomic_json(d / "params.json", best_params)
        if meta:
            _atomic_json(d / "meta.json", meta)
        logger.info("Checkpoint step_5 saved: XGBoost model → %s", d / "model.json")

    def load_xgboost(self) -> Tuple[Any, Dict[str, Any], Dict[str, Any]]:
        import xgboost as xgb
        d = self._dir / "step_5_xgboost"
        booster = xgb.Booster()
        booster.load_model(str(d / "model.json"))
        with open(d / "params.json") as fh:
            params = json.load(fh)
        meta: Dict[str, Any] = {}
        meta_path = d / "meta.json"
        if meta_path.exists():
            with open(meta_path) as fh:
                meta = json.load(fh)
        logger.info("Checkpoint step_5: XGBoost model loaded.")
        return booster, params, meta

    # ══════════════════════════════════════════════════════════════════════════
    # Step 5b — CPCV OOF paths  (co-located in step_5_xgboost/)
    # ══════════════════════════════════════════════════════════════════════════
    # The φ out-of-sample backtest paths produced by the 28-combo XGBoost
    # CPCV loop are persisted here so A3/A4/A5 can consume them on resume
    # without re-running the multi-hour OOF loop.  Each path stores four
    # aligned numpy arrays: calibrated probabilities, binary labels, h-bar
    # forward returns, and per-row timestamps (as int64 / datetime64 ns).

    @property
    def _cpcv_oof_dir(self) -> Path:
        d = self._step_dir("step_5_xgboost") / "cpcv_oof"
        d.mkdir(exist_ok=True)
        return d

    def has_cpcv_oof(self) -> bool:
        """True if CPCV OOF artifacts exist on disk (meta.json + at least one path)."""
        d = self._dir / "step_5_xgboost" / "cpcv_oof"
        return (d / "meta.json").exists()

    def save_cpcv_oof(self, cpcv_oof: Dict[str, Any]) -> None:
        """Persist the φ CPCV OOF backtest paths produced in step-5.

        ``cpcv_oof`` must have the structure built by the orchestrator::

            {
                "n_paths":     int,
                "best_rounds": int,
                "paths": [
                    {
                        "proba":          np.ndarray  # float32, shape (n_test,)
                        "y":              np.ndarray  # int8,    shape (n_test,)
                        "forward_return": np.ndarray  # float32, shape (n_test,)
                        "timestamp":      np.ndarray  # datetime64[ns], shape (n_test,)
                        "symbol":         np.ndarray  # <U20, shape (n_test,)
                    },
                    ...  # one entry per path (φ paths total)
                ]
            }

        Timestamps are stored as int64 (the raw nanosecond view) and
        recast to ``datetime64[ns]`` on load.
        """
        d = self._cpcv_oof_dir
        n_paths = int(cpcv_oof["n_paths"])
        _atomic_json(d / "meta.json", {
            "n_paths":     n_paths,
            "best_rounds": int(cpcv_oof["best_rounds"]),
        })
        for p, path_data in enumerate(cpcv_oof["paths"]):
            tag = f"path_{p:03d}"
            np.save(str(d / f"{tag}_proba.npy"),   path_data["proba"].astype(np.float32))
            np.save(str(d / f"{tag}_y.npy"),        path_data["y"].astype(np.int8))
            np.save(str(d / f"{tag}_fwd_ret.npy"),  path_data["forward_return"].astype(np.float32))
            # datetime64 → int64 for numpy-native round-trip
            ts = np.asarray(path_data["timestamp"], dtype="datetime64[ns]")
            np.save(str(d / f"{tag}_ts.npy"), ts.view(np.int64))
            # (symbol, timestamp) is the leakage-free join key A5 uses to align
            # this OOF row with the GRU per-symbol purged-val OOF row.
            np.save(
                str(d / f"{tag}_symbol.npy"),
                np.asarray(path_data["symbol"], dtype="U20"),
            )
        logger.info(
            "Checkpoint step_5b saved: CPCV OOF  n_paths=%d  "
            "path_sizes=%s  best_rounds=%d  → %s",
            n_paths,
            [len(cpcv_oof["paths"][p]["proba"]) for p in range(n_paths)],
            cpcv_oof["best_rounds"],
            d,
        )

    def load_cpcv_oof(self) -> Dict[str, Any]:
        """Reload the CPCV OOF paths persisted by :meth:`save_cpcv_oof`.

        Returns the same dict structure as ``cpcv_oof`` produced by the
        orchestrator, with timestamps as ``datetime64[ns]`` arrays.
        Raises ``FileNotFoundError`` if the artifacts are absent
        (call :meth:`has_cpcv_oof` first).
        """
        d = self._dir / "step_5_xgboost" / "cpcv_oof"
        with open(d / "meta.json") as fh:
            meta = json.load(fh)
        n_paths = int(meta["n_paths"])
        paths = []
        for p in range(n_paths):
            tag = f"path_{p:03d}"
            proba   = np.load(str(d / f"{tag}_proba.npy"))
            y       = np.load(str(d / f"{tag}_y.npy"))
            fwd_ret = np.load(str(d / f"{tag}_fwd_ret.npy"))
            ts_i64  = np.load(str(d / f"{tag}_ts.npy"))
            ts      = ts_i64.view("datetime64[ns]")
            symbol  = np.load(str(d / f"{tag}_symbol.npy"))
            paths.append({
                "proba":          proba,
                "y":              y,
                "forward_return": fwd_ret,
                "timestamp":      ts,
                "symbol":         symbol,
            })
        logger.info(
            "Checkpoint step_5b: CPCV OOF loaded  n_paths=%d  best_rounds=%s",
            n_paths, meta["best_rounds"],
        )
        return {
            "n_paths":     n_paths,
            "best_rounds": int(meta["best_rounds"]),
            "paths":       paths,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # Step 5c — Probability calibrators  (co-located alongside model artifacts)
    # ══════════════════════════════════════════════════════════════════════════
    # Fitted on CPCV OOF predictions (XGBoost) or per-symbol purged val
    # predictions (GRU) — NEVER on the HPO validation set (selection leakage).
    # Persisted here for crash-resilience so a run that crashes after the OOF
    # loop does not lose the calibrator and force a full CPCV re-run on resume.
    # The serving pipeline (RegistryModelLoader) reads these from the model
    # artifact directories, NOT from here — this step writes an intermediate
    # copy that the export step (step-9) then copies to the Treelite / ONNX dirs.

    def has_calibrator(self, model_type: str) -> bool:
        """Return True if a calibrator checkpoint exists for *model_type*."""
        return self._calibrator_path(model_type).exists()

    def save_calibrator(self, model_type: str, calibrator: Any) -> None:
        """Persist a fitted ConfidenceCalibrator checkpoint for *model_type*.

        *model_type* must be ``"xgboost"`` or ``"gru"``.  The file is written
        alongside the corresponding model step directory so a crash anywhere in
        training does not lose the calibrator once the OOF / purged-val loop
        completes.

        The step-9 export path reads ``self.calibrator`` from the trainer object
        in memory; this checkpoint is used only for crash-resilience (resume
        path re-attaches the calibrator from disk).
        """
        path = self._calibrator_path(model_type)
        calibrator.save(path)
        logger.info(
            "Checkpoint: calibrator saved  model_type=%s  ECE_after=%.4f  path=%s",
            model_type, calibrator.ece_after or 0.0, path.name,
        )

    def load_calibrator(self, model_type: str) -> Any:
        """Load a previously saved calibrator checkpoint.

        Raises ``FileNotFoundError`` if absent (call :meth:`has_calibrator`
        first).
        """
        from app.ml.inference.calibrator import ConfidenceCalibrator
        path = self._calibrator_path(model_type)
        cal = ConfidenceCalibrator.load(path)
        logger.info(
            "Checkpoint: calibrator loaded  model_type=%s  ECE_after=%.4f",
            model_type, cal.ece_after or 0.0,
        )
        return cal

    def _calibrator_path(self, model_type: str) -> Path:
        if model_type == "xgboost":
            return self._step_dir("step_5_xgboost") / "calibrator_xgb.pkl"
        if model_type == "gru":
            return self._step_dir("step_6_gru") / "calibrator_gru.pkl"
        raise ValueError(
            f"Unknown model_type {model_type!r}. Must be 'xgboost' or 'gru'."
        )

    # ══════════════════════════════════════════════════════════════════════════
    # Step 6 — GRU (four sub-checkpoint levels)
    # ══════════════════════════════════════════════════════════════════════════

    @property
    def _gru_dir(self) -> Path:
        return self._step_dir("step_6_gru")

    # ── Sub-A: eval arrays + GRU plan metadata ────────────────────────────────

    def gru_has_eval_arrays(self) -> bool:
        d = self._dir / "step_6_gru"
        return (
            (d / "eval_X.npy").exists()
            and (d / "eval_y.npy").exists()
            and (d / "eval_r.npy").exists()
            and (d / "eval_sym.npy").exists()
            and (d / "eval_ts.npy").exists()
            and (d / "eval_meta.json").exists()
        )

    def save_gru_eval_arrays(
        self,
        X_val: np.ndarray,
        y_val: np.ndarray,
        r_val: np.ndarray,
        sym_val: np.ndarray,
        ts_val: np.ndarray,
        class_weights: Dict[int, float],
        gru_plan: List[Tuple[str, int, int, int]],
        n_train_total: int,
    ) -> None:
        """
        Sub-checkpoint A: persist the held-out eval arrays + the per-symbol
        purged GRU plan.

        ``sym_val`` / ``ts_val`` are the per-eval-row ``symbol`` and
        ``timestamp`` (row-aligned with ``X_val`` / ``y_val`` / ``r_val``,
        including the deterministic VAL_CAP subsample). They are the
        leakage-free join key A5 uses to align each GRU purged-val OOF row
        with its XGBoost CPCV OOF row — persisted here so the join survives a
        sub-A resume without rebuilding sequences.

        ``gru_plan`` is a list of ``(symbol, y_start_idx, n_train, n_val)`` —
        a deterministic *per-symbol chronological* split with an h+embargo
        purge gap between every symbol's train and val sequences (leakage-safe;
        A2). It lets the resume path rebuild only X_train (never the full
        ~5.5 GB combined array) and reproduce the exact eval set with **no
        randomness**.

        ``r_val`` holds the h-bar forward returns aligned with ``y_val`` so
        steps 7-8 can compute real financial metrics. ``n_train_total``
        pre-sizes the X_train rebuild.
        """
        d = self._gru_dir
        if not (len(X_val) == len(y_val) == len(r_val) == len(sym_val) == len(ts_val)):
            raise ValueError(
                "GRU eval arrays are not row-aligned: "
                f"X={len(X_val)} y={len(y_val)} r={len(r_val)} "
                f"sym={len(sym_val)} ts={len(ts_val)}. Refusing to persist a "
                "mis-aligned join key (would silently corrupt the A5 join)."
            )
        np.save(str(d / "eval_X.npy"), X_val)
        np.save(str(d / "eval_y.npy"), y_val)
        np.save(str(d / "eval_r.npy"), r_val)
        np.save(str(d / "eval_sym.npy"), np.asarray(sym_val, dtype="U20"))
        ts_i64 = np.asarray(ts_val, dtype="datetime64[ns]").view(np.int64)
        np.save(str(d / "eval_ts.npy"), ts_i64)
        meta: Dict[str, Any] = {
            "n_train_total":  int(n_train_total),
            "n_val":          len(y_val),
            "class_weights":  {str(k): float(v) for k, v in class_weights.items()},
            "gru_plan":       [[s, int(ys), int(ntr), int(nva)]
                               for s, ys, ntr, nva in gru_plan],
        }
        _atomic_json(d / "eval_meta.json", meta)
        logger.info(
            "Checkpoint step_6 [A]: eval arrays saved  X=%s  r=%s  "
            "n_train_total=%d  (per-symbol purged split, deterministic).",
            X_val.shape, r_val.shape, n_train_total,
        )

    def load_gru_eval_arrays(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, Dict[str, Any]]:
        """Returns (X_val, y_val, r_val, meta_dict)."""
        d = self._dir / "step_6_gru"
        X = np.load(str(d / "eval_X.npy"))
        y = np.load(str(d / "eval_y.npy"))
        r = np.load(str(d / "eval_r.npy"))
        with open(d / "eval_meta.json") as fh:
            meta = json.load(fh)
        # A5 leakage-free join keys (row-aligned with X/y/r). datetime64 is
        # restored from its int64 nanosecond view.
        meta["val_symbol"] = np.load(str(d / "eval_sym.npy"))
        meta["val_timestamp"] = np.load(str(d / "eval_ts.npy")).view("datetime64[ns]")
        logger.info("Checkpoint step_6 [A]: eval arrays loaded  X=%s  r=%s.", X.shape, r.shape)
        return X, y, r, meta

    # ── Sub-B: HPO best hyperparameters ───────────────────────────────────────

    def gru_has_best_params(self) -> bool:
        return (self._dir / "step_6_gru" / "best_params.json").exists()

    def save_gru_best_params(self, best_params: Dict[str, Any]) -> None:
        _atomic_json(self._gru_dir / "best_params.json", best_params)
        logger.info("Checkpoint step_6 [B]: best_params saved.")

    def load_gru_best_params(self) -> Dict[str, Any]:
        with open(self._dir / "step_6_gru" / "best_params.json") as fh:
            return json.load(fh)

    # ── Sub-C: per-epoch training state ───────────────────────────────────────

    @property
    def gru_epoch_weights_dir(self) -> Path:
        d = self._gru_dir / "epoch_weights"
        d.mkdir(exist_ok=True)
        return d

    @property
    def gru_training_state_path(self) -> Path:
        return self._gru_dir / "training_state.json"

    def gru_has_training_state(self) -> bool:
        return self.gru_training_state_path.exists()

    def save_gru_training_state(
        self, last_epoch: int, logs: Dict[str, float], n_features: int
    ) -> None:
        """Called by EpochCheckpointCallback after every epoch (atomic write)."""
        _atomic_json(
            self.gru_training_state_path,
            {
                "last_epoch": last_epoch,
                "n_features": n_features,
                "logs": {k: float(v) for k, v in logs.items()},
            },
        )

    def load_gru_training_state(self) -> Dict[str, Any]:
        with open(self.gru_training_state_path) as fh:
            return json.load(fh)

    def invalidate_gru_epoch_checkpoint(self) -> None:
        """Delete the epoch training state and all per-epoch weight files.

        Called when a resumed run detects an architecture mismatch (e.g.
        n_features changed between runs).  The sub-B best_params and sub-A
        eval arrays are NOT touched — only sub-C state is cleared.
        """
        import shutil

        if self.gru_training_state_path.exists():
            self.gru_training_state_path.unlink()
        weights_dir = self._gru_dir / "epoch_weights"
        if weights_dir.exists():
            shutil.rmtree(weights_dir)
        logger.info(
            "Invalidated stale GRU epoch checkpoint (architecture mismatch): "
            "training_state.json and epoch_weights/ removed."
        )

    # ── Final GRU model ───────────────────────────────────────────────────────

    def gru_has_model(self) -> bool:
        return (self._dir / "step_6_gru" / "model.keras").exists()

    def save_gru_model(
        self,
        model: Any,
        best_params: Dict[str, Any],
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        d = self._gru_dir
        model.save(str(d / "model.keras"))
        # best_params may already be there from sub-B; overwrite for consistency
        _atomic_json(d / "best_params.json", best_params)
        if meta:
            _atomic_json(d / "meta.json", meta)
        logger.info("Checkpoint step_6: GRU model.keras written.")

    def load_gru(self) -> Tuple[Any, Dict[str, Any], np.ndarray, np.ndarray, np.ndarray]:
        """Returns (model, best_params, eval_X, eval_y, eval_r).

        In ``SCHEMA_VERSION`` >= 2 the forward-returns array (``eval_r.npy``)
        is mandatory. Any checkpoint that reaches this loader has already
        passed :func:`_enforce_compat`, so a missing ``eval_r.npy`` means the
        step_6 artifacts are corrupt/incomplete — a hard
        :class:`StaleCheckpointError`, never a silent ``None`` (the latter is
        precisely what caused the RC-1 Sharpe→accuracy degradation).
        """
        import keras  # lazy import — use standalone Keras 3 (not tf.keras shim)
        from app.ml.training.gru_trainer import _BinaryAUCPR  # ensure @register_keras_serializable runs
        d = self._dir / "step_6_gru"
        model = keras.saving.load_model(
            str(d / "model.keras"),
            custom_objects={"_BinaryAUCPR": _BinaryAUCPR},
        )
        with open(d / "best_params.json") as fh:
            params = json.load(fh)
        X = np.load(str(d / "eval_X.npy"))
        y = np.load(str(d / "eval_y.npy"))
        r_path = d / "eval_r.npy"
        if not r_path.exists():
            raise StaleCheckpointError(
                f"step_6 checkpoint is missing the mandatory forward-returns "
                f"array ({r_path}). The checkpoint is corrupt or was written by "
                f"an incompatible (pre-v2) run. Start a clean run with --fresh."
            )
        r = np.load(str(r_path))
        logger.info("Checkpoint step_6: GRU model + eval arrays (with returns) loaded.")
        return model, params, X, y, r

    # ── Keras Tuner directory for HPO ─────────────────────────────────────────

    @property
    def gru_tuner_dir(self) -> Path:
        """Put the Keras Tuner project inside the checkpoint tree so it's co-located."""
        d = self._gru_dir / "keras_tuner"
        d.mkdir(exist_ok=True)
        return d

    def gru_tuner_has_trials(self) -> bool:
        """True if at least one Keras Tuner trial result exists on disk."""
        td = self._dir / "step_6_gru" / "keras_tuner" / "gru_stock_prediction"
        return td.exists() and any(td.iterdir())

    # ══════════════════════════════════════════════════════════════════════════
    # Step 7 — Ensemble weights
    # ══════════════════════════════════════════════════════════════════════════

    def save_ensemble(
        self,
        weights: Dict[str, float],
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist the accretion-gated ensemble weights and the A5 net-DSR
        diagnostics (objective trace, standalone vs ensemble net-DSR, accretion
        decision). Diagnostics are surfaced into training metrics so the A6
        promotion gate and the promotion report can read the honest,
        cost-aware, calibrated net-DSR the weights were selected on.
        """
        d = self._step_dir("step_7_ensemble")
        _atomic_json(d / "weights.json", weights)
        if diagnostics is not None:
            _atomic_json(d / "diagnostics.json", diagnostics)

    def load_ensemble_weights(self) -> Dict[str, float]:
        with open(self._dir / "step_7_ensemble" / "weights.json") as fh:
            return json.load(fh)

    def load_ensemble_diagnostics(self) -> Dict[str, Any]:
        """Return the persisted A5 diagnostics, or ``{}`` if a pre-A5 run
        wrote only weights.json (resume stays functional, never silent-wrong:
        the absence is explicit, not a fabricated metric)."""
        p = self._dir / "step_7_ensemble" / "diagnostics.json"
        if not p.exists():
            return {}
        with open(p) as fh:
            return json.load(fh)

    # ══════════════════════════════════════════════════════════════════════════
    # Step 8 — Evaluation results
    # ══════════════════════════════════════════════════════════════════════════

    def save_evaluation(self, results: Dict[str, Any]) -> None:
        _atomic_json(
            self._step_dir("step_8_evaluation") / "results.json",
            results,
        )

    def load_evaluation(self) -> Dict[str, Any]:
        with open(self._dir / "step_8_evaluation" / "results.json") as fh:
            return json.load(fh)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _atomic_json(path: Path, data: Any) -> None:
    """Write *data* as JSON to *path* atomically (temp-file + os.replace)."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, default=str)
    os.replace(tmp, path)  # atomic on POSIX; atomic on Windows ≥ Vista


def _warn_config_drift(saved: Dict[str, Any], current: Dict[str, Any]) -> None:
    """Log a warning for any config keys that changed between runs."""
    all_keys = set(saved) | set(current)
    diffs = [
        f"  {k}: {saved.get(k)!r} → {current.get(k)!r}"
        for k in sorted(all_keys)
        if saved.get(k) != current.get(k)
    ]
    if diffs:
        logger.warning(
            "Training config differs from the checkpoint saved on disk:\n%s\n"
            "Resuming with the CURRENT config values shown above.\n"
            "To start a completely fresh run, pass --fresh to the orchestrator.",
            "\n".join(diffs),
        )


def _enforce_compat(saved_state: Dict[str, Any], current_config: Dict[str, Any]) -> None:
    """Hard-fail (StaleCheckpointError) when an on-disk checkpoint is unsafe to resume.

    Two unrecoverable conditions:

    1. **Schema mismatch** — the checkpoint predates schema versioning, or its
       ``schema_version`` differs from the current ``SCHEMA_VERSION``. Pre-v2
       checkpoints lack the persisted forward-returns array (``eval_r.npy``);
       resuming one is exactly what previously caused the silent
       Sharpe→accuracy degradation (RC-1).
    2. **Model-affecting config drift** — a key in ``_MODEL_AFFECTING_KEYS``
       differs from the saved run. Every downstream artifact
       (features/sequences/targets/models) was produced under the saved value;
       resuming with a new value silently mixes incompatible data.

    Non-model-affecting drift is intentionally NOT fatal here — it is surfaced
    separately by :func:`_warn_config_drift` for operator judgement.
    """
    saved_schema = saved_state.get("schema_version")
    if saved_schema != SCHEMA_VERSION:
        raise StaleCheckpointError(
            f"Checkpoint schema_version={saved_schema!r} is incompatible with the "
            f"current SCHEMA_VERSION={SCHEMA_VERSION}. This checkpoint predates a "
            f"breaking change (e.g. mandatory forward-return persistence) and "
            f"cannot be safely resumed. Start a clean run with --fresh."
        )

    saved_config = saved_state.get("config", {}) or {}
    drifted = sorted(
        k for k in _MODEL_AFFECTING_KEYS
        if saved_config.get(k) != current_config.get(k)
    )
    if drifted:
        details = "; ".join(
            f"{k}: {saved_config.get(k)!r} → {current_config.get(k)!r}"
            for k in drifted
        )
        raise StaleCheckpointError(
            f"Model-affecting config changed since the checkpoint was written "
            f"[{details}]. Resuming would silently mix incompatible artifacts "
            f"(features/sequences/targets/models). Start a clean run with --fresh."
        )


def find_checkpoint(output_dir: Path) -> bool:
    """
    Return True if an *incomplete* checkpoint exists under
    ``output_dir/checkpoints/``.

    Callers use this to decide whether to set ``fresh=False`` (resume)
    or ``fresh=True`` (start from scratch).
    """
    cp_file = output_dir / "checkpoints" / "checkpoint.json"
    if not cp_file.exists():
        return False
    try:
        with open(cp_file) as fh:
            state = json.load(fh)
        completed = set(state.get("completed_steps", []))
        return not completed.issuperset(set(PIPELINE_STEPS))
    except (json.JSONDecodeError, KeyError):
        return False  # corrupt file — treat as no checkpoint
