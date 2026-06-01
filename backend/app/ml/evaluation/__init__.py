"""
Cortex AI — ML Evaluation Package
===================================
Evaluation metrics, quality gates, explainability validation,
leakage-safe Combinatorial Purged Cross-Validation, and cost-aware
backtesting with Deflated Sharpe Ratio and Probability of Backtest
Overfitting.
"""
from app.ml.evaluation.metrics import MetricsCalculator, compute_metrics
from app.ml.evaluation.shap_validator import SHAPValidator, create_shap_validator
from app.ml.evaluation.cpcv import PanelPurgedCPCV, CPCVSplit, purged_holdout_split
from app.ml.evaluation.backtest import (
    binary_to_positions,
    round_trip_charge_fraction,
    strategy_returns,
    per_period_sharpe,
    path_sharpe,
    compute_cpcv_path_net_sharpes,
)
from app.ml.evaluation.deflated_sharpe import (
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    compute_dsr_and_pbo,
)
from app.ml.evaluation.promotion_report import (
    PromotionStatus,
    BundleChecksumError,
    BlockedChallengerError,
    build_promotion_report,
    load_and_verify_report,
)
from app.ml.evaluation.event_backtest import (
    SimulatedFill,
    PathSimulationResult,
    EventBacktestReport,
    run_event_backtest,
)

__all__ = [
    # Metrics (production QualityGate lives in app.ml.model_registry — A6)
    "MetricsCalculator",
    "compute_metrics",
    # Explainability
    "SHAPValidator",
    "create_shap_validator",
    # Combinatorial Purged CV (A2)
    "PanelPurgedCPCV",
    "CPCVSplit",
    "purged_holdout_split",
    # Cost-aware backtest (A3)
    "binary_to_positions",
    "round_trip_charge_fraction",
    "strategy_returns",
    "per_period_sharpe",
    "path_sharpe",
    "compute_cpcv_path_net_sharpes",
    # DSR & PBO (A3)
    "deflated_sharpe_ratio",
    "probability_of_backtest_overfitting",
    "compute_dsr_and_pbo",
    # Champion/Challenger Promotion Report (C2)
    "PromotionStatus",
    "BundleChecksumError",
    "BlockedChallengerError",
    "build_promotion_report",
    "load_and_verify_report",
    # Event-Driven Backtest (F1)
    "SimulatedFill",
    "PathSimulationResult",
    "EventBacktestReport",
    "run_event_backtest",
]
