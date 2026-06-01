"""
ML Training Package

Production-grade training pipeline for stock prediction models.
Implements XGBoost, GRU, and ensemble training with walk-forward validation.
"""

from .walk_forward import (
    WalkForwardSplitter,
    create_walk_forward_splits,
    validate_split
)
from .xgboost_trainer import (
    XGBoostTrainer,
    train_xgboost_model,
    tune_xgboost_hyperparameters
)
from .gru_trainer import (
    GRUTrainer,
    train_gru_model,
    tune_gru_hyperparameters
)
from .ensemble_trainer import (
    EnsembleTrainer,
    EnsembleNotAccretiveError,
    create_ensemble,
)
from .evaluator import (
    ModelEvaluator,
    evaluate_predictions,
    calculate_financial_metrics,
    calculate_classification_metrics
)

__all__ = [
    # Walk-forward validation
    'WalkForwardSplitter',
    'create_walk_forward_splits',
    'validate_split',
    # XGBoost training
    'XGBoostTrainer',
    'train_xgboost_model',
    'tune_xgboost_hyperparameters',
    # GRU training
    'GRUTrainer',
    'train_gru_model',
    'tune_gru_hyperparameters',
    # Ensemble
    'EnsembleTrainer',
    'EnsembleNotAccretiveError',
    'create_ensemble',
    # Evaluation
    'ModelEvaluator',
    'evaluate_predictions',
    'calculate_financial_metrics',
    'calculate_classification_metrics'
]
