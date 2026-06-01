"""
XGBoost Trainer Module

Production-grade XGBoost training with Optuna hyperparameter tuning.
Optimized for tabular stock prediction features.
"""

from __future__ import annotations

import logging
import joblib
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import optuna
import xgboost as xgb
from optuna.pruners import MedianPruner
from optuna.samplers import TPESampler
from sklearn.metrics import average_precision_score

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
        n_jobs: int = 4
    ) -> Dict:
        """
        Tune hyperparameters with Optuna
        
        Args:
            X_train: Training features
            y_train: Training labels
            X_val: Validation features
            y_val: Validation labels
            n_trials: Number of Optuna trials
            timeout: Timeout in seconds (None = no limit)
            n_jobs: Parallel jobs
            
        Returns:
            Best parameters dict
        """
        logger.info(f"Starting hyperparameter tuning: {n_trials} trials, {n_jobs} jobs")
        
        # Convert labels to int (binary: 0, 1)
        y_train_idx = y_train.astype(int)
        y_val_idx = y_val.astype(int)
        
        # Create DMatrix
        dtrain = xgb.DMatrix(X_train, label=y_train_idx)
        dval = xgb.DMatrix(X_val, label=y_val_idx)
        
        def objective(trial):
            """Optuna objective: maximise AUC-PR (Average Precision Score).

            AUC-PR is the correct optimisation target for imbalanced binary
            classification.  Accuracy is meaningless here — a model that always
            predicts the majority class would score high on accuracy but zero on
            Average Precision.
            """
            params = {
                'objective': 'binary:logistic',
                # aucpr for internal early-stopping; logloss for monotone loss tracking
                'eval_metric': ['logloss', 'aucpr'],
                # CPU-only: GPU budget is reserved for TF/GRU to avoid CUDA version
                # conflicts between tensorflow[and-cuda] (CUDA 12.9) and XGBoost.
                # hist on CPU handles 4M+ samples efficiently.
                'tree_method': 'hist',
                'random_state': self.random_state,

                # Tunable parameters
                'max_depth': trial.suggest_int('max_depth', 4, 8),
                'min_child_weight': trial.suggest_int('min_child_weight', 1, 7),
                'gamma': trial.suggest_float('gamma', 0.0, 0.3),
                'subsample': trial.suggest_float('subsample', 0.7, 0.9),
                'colsample_bytree': trial.suggest_float('colsample_bytree', 0.7, 0.9),
                'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.1, log=True),
                'lambda': trial.suggest_float('lambda', 0.5, 2.0),
                'alpha': trial.suggest_float('alpha', 0.0, 0.5),
            }

            model = xgb.train(
                params,
                dtrain,
                num_boost_round=300,
                evals=[(dval, 'val')],
                early_stopping_rounds=30,
                verbose_eval=False,
            )

            # sklearn's average_precision_score is the canonical AUC-PR
            # implementation — uses left-rectangle rule (area under PR curve).
            preds_proba = model.predict(dval)   # P(UP) ∈ [0, 1]
            return average_precision_score(y_val_idx, preds_proba)
        
        # Create study
        study = optuna.create_study(
            direction='maximize',
            sampler=TPESampler(seed=self.random_state),
            pruner=MedianPruner(n_startup_trials=10, n_warmup_steps=5)
        )
        
        # Optimize
        study.optimize(
            objective,
            n_trials=n_trials,
            timeout=timeout,
            n_jobs=n_jobs,
            show_progress_bar=True
        )
        
        self.best_params = study.best_params

        logger.info(f"Tuning complete. Best AUC-PR: {study.best_value:.4f}")
        logger.info(f"Best params: {self.best_params}")
        
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
