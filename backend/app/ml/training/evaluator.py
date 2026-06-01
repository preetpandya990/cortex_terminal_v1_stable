"""
Model Evaluator Module

Comprehensive evaluation metrics for stock prediction models.
Includes classification metrics and financial performance metrics.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from sklearn.metrics import (
    accuracy_score, precision_score, recall_score, f1_score,
    confusion_matrix, classification_report, log_loss, brier_score_loss,
    average_precision_score,
)
from dataclasses import dataclass, field
import logging

from app.ml.evaluation.backtest import strategy_returns as _cost_aware_returns

logger = logging.getLogger(__name__)


@dataclass
class EvaluationResults:
    """Container for evaluation results"""
    # Classification metrics
    accuracy: float
    precision: Dict[str, float]
    recall: Dict[str, float]
    f1_score: Dict[str, float]
    confusion_matrix: np.ndarray
    log_loss: float
    
    # Financial metrics — None when aligned forward returns were not provided.
    # This is an honest "not computed"; the pipeline must NEVER fabricate 0.0
    # here, as a zero makes an unvalidated model look validated (RC-1).
    sharpe_ratio: Optional[float]
    sortino_ratio: Optional[float]
    max_drawdown: Optional[float]
    win_rate: Optional[float]
    profit_factor: Optional[float]
    total_return: Optional[float]

    # Additional stats
    n_samples: int
    n_trades: Optional[int]
    avg_return_per_trade: Optional[float]

    # A3 — cost-aware promotion gates (populated when CPCV OOF is available)
    auc_pr: Optional[float] = None
    deflated_sharpe: Optional[float] = None
    pbo: Optional[float] = None
    path_sharpes: Optional[List[float]] = None


class ModelEvaluator:
    """
    Comprehensive model evaluation for stock prediction
    
    Evaluates both classification performance and financial viability.
    """
    
    def __init__(self, risk_free_rate: float = 0.05):
        """
        Initialize evaluator
        
        Args:
            risk_free_rate: Annual risk-free rate for Sharpe/Sortino (default 5%)
        """
        self.risk_free_rate = risk_free_rate
        self.daily_risk_free_rate = (1 + risk_free_rate) ** (1/252) - 1
    
    def evaluate(
        self,
        y_true: np.ndarray,
        y_pred: np.ndarray,
        y_proba: Optional[np.ndarray] = None,
        returns: Optional[np.ndarray] = None
    ) -> EvaluationResults:
        """
        Comprehensive evaluation
        
        Args:
            y_true: True labels [-1, 0, 1]
            y_pred: Predicted labels [-1, 0, 1]
            y_proba: Predicted probabilities (n, 3) [optional]
            returns: Actual returns for financial metrics [optional]
            
        Returns:
            EvaluationResults object
        """
        # Classification metrics
        class_metrics = calculate_classification_metrics(y_true, y_pred, y_proba)
        
        # Financial metrics — computed ONLY when aligned forward returns are
        # provided. When absent, every financial field is recorded as None
        # ("not computed"), NEVER 0.0. A fabricated zero silently makes an
        # unvalidated model look validated — the exact failure mode (RC-1)
        # that previously let a model ship with no financial validation.
        if returns is not None:
            fin_metrics = calculate_financial_metrics(
                y_pred, returns, self.daily_risk_free_rate
            )
        else:
            fin_metrics = {
                'sharpe_ratio': None,
                'sortino_ratio': None,
                'max_drawdown': None,
                'win_rate': None,
                'profit_factor': None,
                'total_return': None,
                'n_trades': None,
                'avg_return_per_trade': None,
            }
        
        results = EvaluationResults(
            accuracy=class_metrics['accuracy'],
            precision=class_metrics['precision'],
            recall=class_metrics['recall'],
            f1_score=class_metrics['f1_score'],
            confusion_matrix=class_metrics['confusion_matrix'],
            log_loss=class_metrics['log_loss'],
            sharpe_ratio=fin_metrics['sharpe_ratio'],
            sortino_ratio=fin_metrics['sortino_ratio'],
            max_drawdown=fin_metrics['max_drawdown'],
            win_rate=fin_metrics['win_rate'],
            profit_factor=fin_metrics['profit_factor'],
            total_return=fin_metrics['total_return'],
            n_samples=len(y_true),
            n_trades=fin_metrics['n_trades'],
            avg_return_per_trade=fin_metrics['avg_return_per_trade'],
            auc_pr=class_metrics['auc_pr'],
        )
        
        return results
    
    def print_results(self, results: EvaluationResults) -> None:
        """Print formatted evaluation results"""
        print(f"\n{'='*80}")
        print("MODEL EVALUATION RESULTS")
        print(f"{'='*80}\n")
        
        print("CLASSIFICATION METRICS:")
        print(f"  Accuracy:        {results.accuracy:.4f}")
        print(f"  Precision (BUY): {results.precision.get('buy', 0):.4f}")
        print(f"  Precision (SELL):{results.precision.get('sell', 0):.4f}")
        print(f"  Recall (BUY):    {results.recall.get('buy', 0):.4f}")
        print(f"  Recall (SELL):   {results.recall.get('sell', 0):.4f}")
        print(f"  F1 Score (BUY):  {results.f1_score.get('buy', 0):.4f}")
        print(f"  F1 Score (SELL): {results.f1_score.get('sell', 0):.4f}")
        print(f"  Log Loss:        {results.log_loss:.4f}")
        
        def _fin(value, fmt: str) -> str:
            # None = financial metric not computed (no returns) — show it
            # plainly instead of crashing on a numeric format spec.
            return "n/a (no returns)" if value is None else format(value, fmt)

        print("\nFINANCIAL METRICS:")
        print(f"  Sharpe Ratio:    {_fin(results.sharpe_ratio, '.4f')}")
        print(f"  Sortino Ratio:   {_fin(results.sortino_ratio, '.4f')}")
        print(f"  Max Drawdown:    {_fin(results.max_drawdown, '.2%')}")
        print(f"  Win Rate:        {_fin(results.win_rate, '.2%')}")
        print(f"  Profit Factor:   {_fin(results.profit_factor, '.4f')}")
        print(f"  Total Return:    {_fin(results.total_return, '.2%')}")

        print("\nTRADING STATS:")
        print(f"  Total Samples:   {results.n_samples}")
        print(f"  Total Trades:    {_fin(results.n_trades, 'd')}")
        print(f"  Avg Return/Trade:{_fin(results.avg_return_per_trade, '.4%')}")
        
        # Binary task (labels [0, 1] = DOWN, UP) — matches
        # calculate_classification_metrics; the old 3×3 SELL/HOLD/BUY layout
        # IndexError'd on the real 2×2 matrix.
        cm = results.confusion_matrix
        print("\nCONFUSION MATRIX (binary):")
        print("              Predicted")
        print("              DOWN     UP")
        print(f"Actual DOWN   {cm[0, 0]:5d}  {cm[0, 1]:5d}")
        print(f"       UP     {cm[1, 0]:5d}  {cm[1, 1]:5d}")
        
        print(f"\n{'='*80}\n")


def calculate_classification_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    y_proba: Optional[np.ndarray] = None
) -> Dict:
    """
    Calculate classification metrics for binary classification.
    
    Args:
        y_true: True binary labels (0, 1)
        y_pred: Predicted binary labels (0, 1)
        y_proba: Predicted probabilities (n, 2) [optional]
        
    Returns:
        Dict with classification metrics
    """
    # Ensure binary format
    y_true_bin = y_true.astype(int)
    y_pred_bin = y_pred.astype(int)
    
    # Accuracy
    acc = accuracy_score(y_true_bin, y_pred_bin)
    
    # Per-class metrics
    precision = precision_score(y_true_bin, y_pred_bin, average=None, zero_division=0)
    recall = recall_score(y_true_bin, y_pred_bin, average=None, zero_division=0)
    f1 = f1_score(y_true_bin, y_pred_bin, average=None, zero_division=0)
    
    # Confusion matrix
    cm = confusion_matrix(y_true_bin, y_pred_bin, labels=[0, 1])
    
    # Log loss and AUC-PR (if probabilities provided)
    if y_proba is not None and y_proba.shape[1] == 2:
        ll = log_loss(y_true_bin, y_proba, labels=[0, 1])
        auc_pr = average_precision_score(y_true_bin, y_proba[:, 1])
    else:
        ll = np.nan
        auc_pr = None

    return {
        'accuracy': acc,
        'precision': {
            'down': precision[0],
            'up': precision[1]
        },
        'recall': {
            'down': recall[0],
            'up': recall[1]
        },
        'f1_score': {
            'down': f1[0],
            'up': f1[1]
        },
        'confusion_matrix': cm,
        'log_loss': ll,
        'auc_pr': auc_pr,
    }


def calculate_financial_metrics(
    predictions: np.ndarray,
    returns: np.ndarray,
    risk_free_rate: float = 0.0,
    *,
    mode: str = "long_only",
    product_type: str = "CNC",
    entry_price: float = 1_000.0,
    notional: float = 100_000.0,
    slippage_bps: float = 5.0,
) -> Dict:
    """
    Calculate financial performance metrics using cost-aware backtesting.

    Replaces the prior implementation that treated DOWN (pred=0) as a short
    position (the HOLD-as-short bug, RC-1).  Now uses ``binary_to_positions``
    via the cost-aware backtester, which correctly maps:
      - long_only : DOWN → flat (0 exposure); UP → long (+1)
      - long_short: DOWN → short (−1);        UP → long (+1)

    Statutory charges and slippage are deducted on every active bar.

    Parameters
    ----------
    predictions    : Binary predictions (0=DOWN, 1=UP) or ternary {−1,0,1}.
    returns        : Aligned h-bar forward returns.
    risk_free_rate : Daily risk-free rate for Sharpe / Sortino.
    mode           : "long_only" or "long_short".
    product_type   : "CNC" or "MIS".
    entry_price    : Representative entry price (₹) for charge calculation.
    notional       : Position size in ₹ for charge calculation.
    slippage_bps   : One-way half-spread in basis points.
    """
    net_rets = _cost_aware_returns(
        predictions,
        returns,
        mode=mode,
        product_type=product_type,
        entry_price=entry_price,
        notional=notional,
        slippage_bps=slippage_bps,
    ).astype(np.float64)

    if len(net_rets) == 0:
        return {
            'sharpe_ratio': 0.0, 'sortino_ratio': 0.0, 'max_drawdown': 0.0,
            'win_rate': 0.0, 'profit_factor': 0.0, 'total_return': 0.0,
            'n_trades': 0, 'avg_return_per_trade': 0.0,
        }

    # Cumulative return
    cumulative = (1.0 + net_rets).cumprod()
    total_return = float(cumulative[-1] - 1.0)

    # Annualised Sharpe (ddof=1 for unbiased std)
    excess = net_rets - risk_free_rate
    std_exc = float(excess.std(ddof=1))
    sharpe = float(np.sqrt(252) * excess.mean() / std_exc) if std_exc > 0.0 else 0.0

    # Annualised Sortino (downside deviation)
    downside = excess[excess < 0.0]
    std_down = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
    sortino = float(np.sqrt(252) * excess.mean() / std_down) if std_down > 0.0 else 0.0

    # Max drawdown
    cummax = np.maximum.accumulate(cumulative)
    drawdown = (cumulative - cummax) / cummax
    max_dd = float(abs(drawdown.min()))

    # Active-bar trade stats (flat bars excluded from win-rate / profit factor)
    active = net_rets[net_rets != 0.0]
    if len(active) > 0:
        win_rate = float((active > 0.0).sum() / len(active))
        avg_return = float(active.mean())
        gains = float(active[active > 0.0].sum())
        losses = float(abs(active[active < 0.0].sum()))
        profit_factor = gains / losses if losses > 0.0 else 0.0
        n_trades = int(len(active))
    else:
        win_rate = 0.0
        avg_return = 0.0
        profit_factor = 0.0
        n_trades = 0

    return {
        'sharpe_ratio': sharpe,
        'sortino_ratio': sortino,
        'max_drawdown': max_dd,
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        'total_return': total_return,
        'n_trades': n_trades,
        'avg_return_per_trade': avg_return,
    }


def evaluate_predictions(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    y_proba: Optional[np.ndarray] = None,
    returns: Optional[np.ndarray] = None,
    risk_free_rate: float = 0.05
) -> EvaluationResults:
    """
    Convenience function for full evaluation
    
    Args:
        y_true: True labels
        y_pred: Predicted labels
        y_proba: Predicted probabilities [optional]
        returns: Actual returns [optional]
        risk_free_rate: Annual risk-free rate
        
    Returns:
        EvaluationResults object
    """
    evaluator = ModelEvaluator(risk_free_rate=risk_free_rate)
    return evaluator.evaluate(y_true, y_pred, y_proba, returns)
