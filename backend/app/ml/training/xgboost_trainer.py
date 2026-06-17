"""
XGBoost Trainer Module

Production-grade XGBoost training with Optuna hyperparameter tuning.
Optimized for tabular stock prediction features.
"""

from __future__ import annotations

import logging
import joblib
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import optuna
import xgboost as xgb
from optuna.pruners import MedianPruner
from optuna.samplers import TPESampler
from sklearn.metrics import average_precision_score

from app.ml.evaluation.backtest import (
    TRAINING_DECISION_THRESHOLD,
    per_period_sharpe,
    strategy_returns,
)
from app.ml.inference.calibrator import ConfidenceCalibrator
from .evaluator import calculate_classification_metrics, calculate_financial_metrics

logger = logging.getLogger(__name__)


class XGBoostTrainer:
    """
    XGBoost trainer with hyperparameter tuning and production features.
    Supports binary classification with class weights.
    """
    
    def __init__(
        self,
        objective: str = 'binary:logistic',
        num_class: int = 2,
        random_state: int = 42
    ):
        """
        Initialize XGBoost trainer
        
        Args:
            objective: XGBoost objective function (binary:logistic for binary classification)
            num_class: Number of classes (2 for binary)
            random_state: Random seed
        """
        self.objective = objective
        self.num_class = num_class
        self.random_state = random_state
        self.model: xgb.Booster | None = None
        self.best_params: Dict | None = None
        self.feature_importance: Dict | None = None
        self.calibrator: ConfidenceCalibrator | None = None
        # Per-trial net Sharpe array, populated by tune_hyperparameters when
        # fwd_ret_val is provided.  Pass to compute_dsr_and_pbo(trial_sharpes=...)
        # to enable correct V and effective N in the DSR sensitivity band.
        self.trial_sharpes: Optional[np.ndarray] = None
        # Per-trial net-return series (one array per completed trial, aligned to
        # the HPO holdout rows).  Logged to MLflow as an artifact so the CSCV
        # matrix and effective-N clustering can be computed post-hoc without
        # re-running the HPO study (Phase-B §3.5).
        self.trial_return_series: Optional[List[np.ndarray]] = None
    
    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: np.ndarray,
        y_val: np.ndarray,
        params: Optional[Dict] = None,
        class_weights: Optional[Dict[int, float]] = None,
        early_stopping_rounds: int = 30,
        verbose: int = 10,
    ) -> xgb.Booster:
        """
        Train XGBoost model with class weights support.

        The calibrator is NOT fitted here.  Call ``fit_calibrator_on_oof()``
        separately after the CPCV OOF loop to fit a leakage-free calibrator
        on pooled out-of-fold predictions.  Fitting the calibrator on the
        validation set passed here (HPO holdout) would introduce selection
        leakage: this set was also used for early-stopping / Optuna guidance,
        biasing the score distribution the calibrator sees.

        Args:
            X_train:               Training features (n, n_features).
            y_train:               Training labels (binary: 0, 1).
            X_val:                 Validation features (m, n_features) — HPO holdout only.
            y_val:                 Validation labels (binary: 0, 1).
            params:                XGBoost parameters (None = defaults).
            class_weights:         {0: w0, 1: w1} sample weight dict.
            early_stopping_rounds: Early stopping patience.
            verbose:               XGBoost verbosity level.

        Returns:
            Trained XGBoost booster.
        """
        y_train_idx = y_train.astype(int)
        y_val_idx   = y_val.astype(int)

        if params is None:
            params = self._get_default_params()

        p = dict(params)  # never mutate caller's dict
        # n_estimators is a scikit-learn-style alias; xgb.train uses
        # num_boost_round as a top-level arg and warns on any unknown key in
        # the params dict — pop it before the call (same pattern as fit_fixed).
        num_boost_round = int(p.pop("n_estimators", 300))
        p.update({
            "objective":    self.objective,
            "eval_metric":  ["logloss", "aucpr"],
            "tree_method":  "hist",     # CPU — GPU budget reserved for TF/GRU
            "random_state": self.random_state,
        })

        if class_weights:
            sample_weights = np.array([class_weights[int(y)] for y in y_train_idx])
            dtrain = xgb.DMatrix(X_train, label=y_train_idx, weight=sample_weights)
            logger.info(
                "Applied class weights: %s | weight range [%.3f, %.3f]",
                class_weights, sample_weights.min(), sample_weights.max(),
            )
        else:
            dtrain = xgb.DMatrix(X_train, label=y_train_idx)

        dval = xgb.DMatrix(X_val, label=y_val_idx)

        logger.info("Training XGBoost model  num_boost_round=%d…", num_boost_round)
        self.model = xgb.train(
            p,
            dtrain,
            num_boost_round=num_boost_round,
            evals=[(dtrain, "train"), (dval, "val")],
            early_stopping_rounds=early_stopping_rounds,
            verbose_eval=verbose,
        )

        self.feature_importance = self.model.get_score(importance_type="gain")
        logger.info("XGBoost training complete. Best iteration: %d", self.model.best_iteration)

        return self.model

    def fit_fixed(
        self,
        X: np.ndarray,
        y: np.ndarray,
        *,
        params: Dict,
        num_boost_round: int,
        class_weights: Optional[Dict[int, float]] = None,
        sample_weight: Optional[np.ndarray] = None,
    ) -> xgb.Booster:
        """Fixed-round fit for CPCV combinatorial refits and the final
        all-data production refit.

        No validation set, no early stopping, no calibrator: the boosting
        round count is FIXED to ``num_boost_round`` (the ``best_iteration``
        determined once on the purged HPO holdout). This is the correct
        primitive for CPCV — a per-combo early-stopping validation set would
        itself be in-sample to that combo and reintroduce exactly the leakage
        CPCV exists to remove. Returns a *fresh* booster and does NOT mutate
        ``self.model`` / ``self.calibrator``, so the production model and the
        throwaway per-combo models stay fully isolated.

        Weight combination (when both are provided):
            combined[i] = class_weights[y[i]] × sample_weight[i]

        This means CPCV combos and the final production refit both see the
        feedback-adjusted distribution. ``sample_weight`` rows with no feedback
        match default to 1.0 (caller responsibility).
        """
        if num_boost_round < 1:
            raise ValueError(f"num_boost_round must be ≥ 1, got {num_boost_round}")

        y_idx = y.astype(int)
        p = dict(params)
        p.update({
            "objective":    self.objective,
            "eval_metric":  ["logloss", "aucpr"],
            "tree_method":  "hist",
            "random_state": self.random_state,
        })
        # round count is carried by num_boost_round — drop any stale alias
        p.pop("n_estimators", None)

        # ── Combined per-sample weight (class × feedback) ──────────────────────
        w: Optional[np.ndarray] = None
        if class_weights and sample_weight is not None:
            cw = np.array([class_weights[int(v)] for v in y_idx], dtype=np.float32)
            w  = cw * sample_weight.astype(np.float32)
        elif class_weights:
            w = np.array([class_weights[int(v)] for v in y_idx], dtype=np.float32)
        elif sample_weight is not None:
            w = sample_weight.astype(np.float32)

        dtrain = xgb.DMatrix(X, label=y_idx, weight=w) if w is not None else xgb.DMatrix(X, label=y_idx)

        return xgb.train(
            p, dtrain, num_boost_round=int(num_boost_round), verbose_eval=False
        )

    @staticmethod
    def proba_up(booster: xgb.Booster, X: np.ndarray) -> np.ndarray:
        """P(UP) for a ``binary:logistic`` booster (the class-1 probability).

        Keeps XGBoost/DMatrix specifics encapsulated so the orchestrator's
        CPCV OOF loop stays model-agnostic.
        """
        return booster.predict(xgb.DMatrix(X))

    def predict(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Predict with trained model (binary classification).
        
        Args:
            X: Features (n, features)
            
        Returns:
            (predictions, probabilities)
            predictions: Binary labels (0, 1)
            probabilities: (n, 2) for binary classification
        """
        if self.model is None:
            raise ValueError("Model not trained. Call train() first.")
        
        dtest = xgb.DMatrix(X)
        
        if self.objective == 'binary:logistic':
            # Binary classification: returns (n,) probabilities for class 1
            proba_class1 = self.model.predict(dtest)
            # Convert to (n, 2) format
            proba = np.column_stack([1 - proba_class1, proba_class1])
            predictions = (proba_class1 > 0.5).astype(int)
        else:
            # Multi-class: returns (n, num_classes)
            proba = self.model.predict(dtest)
            predictions = proba.argmax(axis=1)
        
        return predictions, proba
    
    def tune_hyperparameters(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: np.ndarray,
        y_val: np.ndarray,
        n_trials: int = 50,
        timeout: Optional[int] = 7200,
        n_jobs: int = 4,
        fwd_ret_val: Optional[np.ndarray] = None,
    ) -> Dict:
        """
        Tune hyperparameters with Optuna (AUC-PR objective).

        Args:
            X_train:     Training features.
            y_train:     Training labels (binary 0/1).
            X_val:       Validation features (HPO holdout).
            y_val:       Validation labels.
            n_trials:    Number of Optuna trials.
            timeout:     Timeout in seconds (None = no limit).
            n_jobs:      Parallel jobs.
            fwd_ret_val: Optional forward returns for the HPO holdout rows
                         (same length as y_val, dtype float32/float64).
                         When provided, each trial logs its net-of-cost
                         per-period Sharpe as a user attribute, which is
                         collected into ``self.trial_sharpes`` after the
                         study.  Pass ``self.trial_sharpes`` to
                         ``compute_dsr_and_pbo(trial_sharpes=...)`` to enable
                         correct V and effective N in the DSR sensitivity band.
                         This is the convergent instrumentation fix from the
                         Validation Remediation Plan Phase-B §3.5.

        Returns:
            Best parameters dict (unchanged from previous behaviour).
            Side-effect: sets ``self.trial_sharpes`` (np.ndarray | None).
        """
        logger.info(
            "Starting hyperparameter tuning: %d trials, %d jobs%s",
            n_trials, n_jobs,
            " (with per-trial Sharpe logging)" if fwd_ret_val is not None else "",
        )

        # Convert labels to int (binary: 0, 1)
        y_train_idx = y_train.astype(int)
        y_val_idx = y_val.astype(int)

        # Precompute DMatrix objects once; shared safely across threads because
        # xgb.DMatrix is read-only after construction.
        dtrain = xgb.DMatrix(X_train, label=y_train_idx)
        dval = xgb.DMatrix(X_val, label=y_val_idx)

        # Resolve forward returns once so the closure doesn't hold a reference
        # to the caller's array (avoids accidental mutation).
        _fwd_ret: Optional[np.ndarray] = (
            np.asarray(fwd_ret_val, dtype=np.float64)
            if fwd_ret_val is not None else None
        )
        _daily_rfr: float = (1.0 + 0.05) ** (1.0 / 252) - 1.0

        # Thread-safe buffer for per-trial return series.  Keyed by trial.number
        # (unique per trial, even with n_jobs > 1) to avoid stitching artefacts
        # from concurrent writes.  Only populated when fwd_ret_val is provided.
        _returns_lock: threading.Lock = threading.Lock()
        _trial_returns_buf: Dict[int, List[float]] = {}

        def objective(trial: optuna.Trial) -> float:
            """Maximise AUC-PR (Average Precision Score).

            AUC-PR is the correct optimisation target for imbalanced binary
            classification.  When ``fwd_ret_val`` was supplied, each trial
            also computes its net-of-cost per-period Sharpe on the holdout
            and stores it as a trial user attribute for later DSR use.
            """
            params = {
                "objective":       "binary:logistic",
                # aucpr for internal early-stopping; logloss for loss tracking
                "eval_metric":     ["logloss", "aucpr"],
                # CPU-only: GPU budget reserved for TF/GRU to avoid CUDA
                # version conflicts between tensorflow and XGBoost.
                "tree_method":     "hist",
                "random_state":    self.random_state,

                # ── Tunable parameters ────────────────────────────────────────
                "max_depth":         trial.suggest_int("max_depth", 4, 8),
                "min_child_weight":  trial.suggest_int("min_child_weight", 1, 7),
                "gamma":             trial.suggest_float("gamma", 0.0, 0.3),
                "subsample":         trial.suggest_float("subsample", 0.7, 0.9),
                "colsample_bytree":  trial.suggest_float("colsample_bytree", 0.7, 0.9),
                "learning_rate":     trial.suggest_float("learning_rate", 0.01, 0.1, log=True),
                "lambda":            trial.suggest_float("lambda", 0.5, 2.0),
                "alpha":             trial.suggest_float("alpha", 0.0, 0.5),
            }

            model = xgb.train(
                params,
                dtrain,
                num_boost_round=300,
                evals=[(dval, "val")],
                early_stopping_rounds=30,
                verbose_eval=False,
            )

            # sklearn's average_precision_score uses the left-rectangle rule
            # (area under the PR curve) — canonical AUC-PR implementation.
            preds_proba = model.predict(dval)   # P(UP) ∈ [0, 1]
            auc_pr = average_precision_score(y_val_idx, preds_proba)

            # ── Per-trial Sharpe instrumentation (Phase-B §3.5) ───────────────
            # Compute net-of-cost strategy returns on the HPO holdout for this
            # trial's model.  Stored as a trial user attribute so it survives
            # parallel execution and can be collected after study.optimize().
            if _fwd_ret is not None:
                pred = (preds_proba >= TRAINING_DECISION_THRESHOLD).astype(np.int8)
                net_rets = strategy_returns(
                    pred,
                    _fwd_ret,
                    mode="long_only",
                    notional=100_000.0,
                    product_type="CNC",
                    entry_price=1_000.0,
                    slippage_bps=5.0,
                ).astype(np.float64)
                if len(net_rets) >= 2:
                    trial_sharpe_val = per_period_sharpe(net_rets, daily_rfr=_daily_rfr)
                    trial.set_user_attr("net_sharpe_oos", float(trial_sharpe_val))
                    # Thread-safe capture of the full return series; keyed by
                    # trial.number which is unique and monotone across workers.
                    with _returns_lock:
                        _trial_returns_buf[trial.number] = net_rets.tolist()

            return auc_pr

        study = optuna.create_study(
            direction="maximize",
            sampler=TPESampler(seed=self.random_state),
            pruner=MedianPruner(n_startup_trials=10, n_warmup_steps=5),
        )

        study.optimize(
            objective,
            n_trials=n_trials,
            timeout=timeout,
            n_jobs=n_jobs,
            show_progress_bar=True,
        )

        self.best_params = study.best_params

        # ── Collect per-trial Sharpes + return series for DSR N/V estimation ──
        # Only populated when fwd_ret_val was provided; None otherwise so
        # callers can distinguish "not instrumented" from "zero trials".
        self.trial_sharpes = None
        self.trial_return_series = None
        if _fwd_ret is not None:
            raw = [
                t.user_attrs["net_sharpe_oos"]
                for t in study.trials
                if t.state == optuna.trial.TrialState.COMPLETE
                and "net_sharpe_oos" in t.user_attrs
            ]
            if raw:
                self.trial_sharpes = np.array(raw, dtype=np.float64)
                ts = self.trial_sharpes
                logger.info(
                    "HPO trial Sharpes: n=%d  mean=%.4f  std=%.4f  "
                    "range=[%.4f, %.4f]",
                    len(ts), float(ts.mean()),
                    float(ts.std(ddof=1)) if len(ts) > 1 else 0.0,
                    float(ts.min()), float(ts.max()),
                )
            # Build the aligned return series list in ascending trial-number
            # order so index i in trial_return_series corresponds to index i
            # in trial_sharpes.  Trials that failed before computing net_rets
            # are absent from _trial_returns_buf and are silently skipped.
            ordered_keys = sorted(
                k for k in _trial_returns_buf
                if k in {t.number for t in study.trials
                         if t.state == optuna.trial.TrialState.COMPLETE
                         and "net_sharpe_oos" in t.user_attrs}
            )
            if ordered_keys:
                self.trial_return_series = [
                    np.asarray(_trial_returns_buf[k], dtype=np.float64)
                    for k in ordered_keys
                ]

        logger.info("Tuning complete. Best AUC-PR: %.4f", study.best_value)
        logger.info("Best params: %s", self.best_params)

        return self.best_params
    
    def save(self, path: str) -> None:
        """
        Save model, metadata, and calibrator to disk.

        Writes artefacts alongside ``path``:
          - ``<stem>.json``          — XGBoost model (native format)
          - ``<stem>.metadata.pkl``  — hyperparams + feature importance
          - ``calibrator_xgb.pkl``   — Beta calibrator in parent directory
                                       (only written when ``self.calibrator`` is set;
                                       call ``fit_calibrator_on_oof()`` first)
        """
        if self.model is None:
            raise ValueError("No model to save — call train() first.")

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        model_path = path.with_suffix(".json")
        self.model.save_model(str(model_path))

        metadata = {
            "best_params":        self.best_params,
            "feature_importance": self.feature_importance,
            "best_iteration":     self.model.best_iteration,
        }
        joblib.dump(metadata, path.with_suffix(".metadata.pkl"))

        if self.calibrator is not None:
            cal_path = path.parent / "calibrator_xgb.pkl"
            self.calibrator.save(cal_path)

        logger.info("XGBoost model saved to %s", model_path)

    def load(self, path: str) -> None:
        """
        Load model, metadata, and calibrator from disk.

        The calibrator is optional — if ``calibrator_xgb.pkl`` is absent in the
        parent directory, ``self.calibrator`` is set to ``None`` (passthrough).
        """
        path = Path(path)

        model_path = path.with_suffix(".json")
        self.model = xgb.Booster()
        self.model.load_model(str(model_path))

        metadata_path = path.with_suffix(".metadata.pkl")
        if metadata_path.exists():
            metadata = joblib.load(metadata_path)
            self.best_params        = metadata.get("best_params")
            self.feature_importance = metadata.get("feature_importance")

        cal_path = path.parent / "calibrator_xgb.pkl"
        if cal_path.exists():
            self.calibrator = ConfidenceCalibrator.load(cal_path)
        else:
            self.calibrator = None

        logger.info("XGBoost model loaded from %s", model_path)
    
    def fit_calibrator_on_oof(
        self,
        oof_proba: np.ndarray,
        oof_y: np.ndarray,
    ) -> ConfidenceCalibrator:
        """Fit a leakage-free Beta calibrator on pooled CPCV out-of-fold predictions.

        This is the ONLY correct way to fit the XGBoost calibrator.  Fitting on
        the HPO validation set (the old ``_fit_calibrator`` approach) introduces
        selection leakage because that set also guided early-stopping and Optuna
        trial selection, biasing the score distribution the calibrator sees.

        CPCV OOF rows are produced by models that never saw them during training
        or HPO — zero selection leakage, zero look-ahead.  Each panel row
        appears in exactly one OOF path, so concatenating all φ paths gives the
        full un-repeated panel.

        Parameters
        ----------
        oof_proba : (n,) float array — pooled P(UP) from all CPCV OOF paths.
        oof_y     : (n,) int array  — corresponding binary labels {0, 1}.

        Sets ``self.calibrator`` and returns the fitted calibrator.
        """
        if len(oof_proba) != len(oof_y):
            raise ValueError(
                f"oof_proba length {len(oof_proba)} != oof_y length {len(oof_y)}"
            )
        if len(oof_proba) < 50:
            raise ValueError(
                f"OOF pool too small for reliable calibration ({len(oof_proba)} samples). "
                "Need at least 50 samples."
            )

        logger.info(
            "Fitting XGBoost Beta calibrator on CPCV OOF pool (%d samples, leakage-free)…",
            len(oof_proba),
        )
        calibrator = ConfidenceCalibrator("xgboost")
        calibrator.fit(oof_y.astype(int), oof_proba.astype(np.float64))

        if calibrator.ece_after is not None and calibrator.ece_after >= 0.05:
            logger.warning(
                "XGBoost calibration ECE %.4f ≥ 0.05 target — "
                "model scores are not well-calibrated on OOF data.",
                calibrator.ece_after,
            )
        logger.info(
            "XGBoost calibration complete: ECE %.4f → %.4f  (n=%d)",
            calibrator.ece_before, calibrator.ece_after, len(oof_proba),
        )
        self.calibrator = calibrator
        return calibrator

    def _get_default_params(self) -> Dict:
        """Get default XGBoost parameters."""
        return {
            'max_depth': 6,
            'min_child_weight': 3,
            'gamma': 0.1,
            'subsample': 0.8,
            'colsample_bytree': 0.8,
            'colsample_bylevel': 0.8,
            'learning_rate': 0.05,
            'n_estimators': 300,
            'lambda': 1.0,
            'alpha': 0.1,
            'tree_method': 'hist',     # CPU — GPU budget reserved for TF/GRU
            'n_jobs': -1,
        }


def train_xgboost_model(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    params: Optional[Dict] = None,
    tune: bool = False,
    n_trials: int = 50
) -> Tuple[xgb.Booster, Dict]:
    """
    Convenience function to train XGBoost model
    
    Args:
        X_train: Training features
        y_train: Training labels
        X_val: Validation features
        y_val: Validation labels
        params: XGBoost parameters (None = defaults)
        tune: Whether to tune hyperparameters
        n_trials: Number of tuning trials
        
    Returns:
        (model, best_params)
    """
    trainer = XGBoostTrainer()
    
    if tune:
        best_params = trainer.tune_hyperparameters(
            X_train, y_train, X_val, y_val, n_trials=n_trials
        )
        params = {**trainer._get_default_params(), **best_params}
    
    model = trainer.train(X_train, y_train, X_val, y_val, params)
    
    return model, trainer.best_params or params


def tune_xgboost_hyperparameters(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    n_trials: int = 50,
    timeout: Optional[int] = 7200
) -> Dict:
    """
    Convenience function for hyperparameter tuning
    
    Args:
        X_train: Training features
        y_train: Training labels
        X_val: Validation features
        y_val: Validation labels
        n_trials: Number of trials
        timeout: Timeout in seconds
        
    Returns:
        Best parameters dict
    """
    trainer = XGBoostTrainer()
    return trainer.tune_hyperparameters(
        X_train, y_train, X_val, y_val, n_trials, timeout
    )
