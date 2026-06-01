"""
Cortex AI — Evaluation Metrics
================================
Comprehensive metrics computation for model evaluation.

Metrics include:
- Directional accuracy (BUY/SELL/HOLD classification)
- Classification metrics (precision, recall, F1 per class)
- Regression metrics (MAE, RMSE for continuous outputs)
- Confusion matrix generation and visualization
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
try:
    import matplotlib.pyplot as plt
    import seaborn as sns
except ImportError:
    plt = None  # type: ignore
    sns = None  # type: ignore
from sklearn.metrics import (
    accuracy_score,
    confusion_matrix,
    mean_absolute_error,
    mean_squared_error,
    precision_recall_fscore_support,
)


logger = logging.getLogger(__name__)


class MetricsCalculator:
    """
    Metrics calculator for model evaluation.
    
    Computes comprehensive metrics for both classification
    (direction prediction) and regression (price/volatility) outputs.
    """

    def __init__(self):
        """Initialize metrics calculator."""
        self.class_names = ["SELL", "HOLD", "BUY"]

    def compute_directional_accuracy(
        self,
        y_true: np.ndarray,
        y_pred: np.ndarray,
    ) -> float:
        """
        Compute directional accuracy (BUY/SELL/HOLD classification).

        Args:
            y_true: True direction labels (0=SELL, 1=HOLD, 2=BUY)
            y_pred: Predicted direction labels

        Returns:
            Accuracy score [0.0, 1.0]

        Example:
            >>> calculator = MetricsCalculator()
            >>> accuracy = calculator.compute_directional_accuracy(y_true, y_pred)
            >>> print(f"Directional accuracy: {accuracy:.4f}")
        """
        accuracy = accuracy_score(y_true, y_pred)
        
        logger.info(f"Directional accuracy: {accuracy:.4f}")
        
        return float(accuracy)

    def compute_classification_metrics(
        self,
        y_true: np.ndarray,
        y_pred: np.ndarray,
    ) -> dict[str, Any]:
        """
        Compute comprehensive classification metrics.

        Args:
            y_true: True direction labels
            y_pred: Predicted direction labels

        Returns:
            Dictionary with metrics:
            {
                "accuracy": float,
                "precision": float (weighted average),
                "recall": float (weighted average),
                "f1": float (weighted average),
                "per_class_metrics": {
                    "SELL": {"precision": float, "recall": float, "f1": float, "support": int},
                    "HOLD": {...},
                    "BUY": {...},
                },
            }

        Example:
            >>> metrics = calculator.compute_classification_metrics(y_true, y_pred)
            >>> print(f"F1 Score: {metrics['f1']:.4f}")
        """
        # Overall accuracy
        accuracy = accuracy_score(y_true, y_pred)

        # Precision, recall, F1 (weighted average)
        precision, recall, f1, support = precision_recall_fscore_support(
            y_true,
            y_pred,
            average="weighted",
            zero_division=0,
        )

        # Per-class metrics
        precision_per_class, recall_per_class, f1_per_class, support_per_class = (
            precision_recall_fscore_support(
                y_true,
                y_pred,
                average=None,
                zero_division=0,
            )
        )

        per_class_metrics = {}
        for i, class_name in enumerate(self.class_names):
            per_class_metrics[class_name] = {
                "precision": float(precision_per_class[i]),
                "recall": float(recall_per_class[i]),
                "f1": float(f1_per_class[i]),
                "support": int(support_per_class[i]),
            }

        metrics = {
            "accuracy": float(accuracy),
            "precision": float(precision),
            "recall": float(recall),
            "f1": float(f1),
            "per_class_metrics": per_class_metrics,
        }

        logger.info(
            f"Classification metrics: "
            f"Accuracy={accuracy:.4f}, "
            f"Precision={precision:.4f}, "
            f"Recall={recall:.4f}, "
            f"F1={f1:.4f}"
        )

        return metrics

    def compute_regression_metrics(
        self,
        y_true: dict[str, np.ndarray],
        y_pred: dict[str, np.ndarray],
        outputs: list[str] | None = None,
    ) -> dict[str, dict[str, float]]:
        """
        Compute regression metrics for continuous outputs.

        Args:
            y_true: Dictionary of true values for each output
            y_pred: Dictionary of predicted values for each output
            outputs: List of output names to compute metrics for
                    (default: all regression outputs)

        Returns:
            Dictionary mapping output names to metrics:
            {
                "entry_price": {"mae": float, "rmse": float},
                "tp1": {"mae": float, "rmse": float},
                ...
            }

        Example:
            >>> metrics = calculator.compute_regression_metrics(y_true, y_pred)
            >>> print(f"Entry price MAE: {metrics['entry_price']['mae']:.2f}")
        """
        if outputs is None:
            outputs = ["entry_price", "tp1", "tp2", "tp3", "stop_loss", "volatility", "confidence"]

        regression_metrics = {}

        for output_name in outputs:
            if output_name not in y_true or output_name not in y_pred:
                logger.warning(f"Output '{output_name}' not found in predictions")
                continue

            true_values = y_true[output_name].flatten()
            pred_values = y_pred[output_name].flatten()

            # Compute MAE and RMSE
            mae = mean_absolute_error(true_values, pred_values)
            rmse = np.sqrt(mean_squared_error(true_values, pred_values))

            regression_metrics[output_name] = {
                "mae": float(mae),
                "rmse": float(rmse),
            }

            logger.debug(
                f"{output_name}: MAE={mae:.4f}, RMSE={rmse:.4f}"
            )

        return regression_metrics

    def generate_confusion_matrix(
        self,
        y_true: np.ndarray,
        y_pred: np.ndarray,
        save_path: str | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        """
        Generate confusion matrix for direction predictions.

        Args:
            y_true: True direction labels
            y_pred: Predicted direction labels
            save_path: Path to save confusion matrix visualization (optional)

        Returns:
            Tuple of (confusion_matrix, statistics_dict)

        Example:
            >>> cm, stats = calculator.generate_confusion_matrix(y_true, y_pred)
            >>> print(f"Confusion matrix:\n{cm}")
        """
        # Compute confusion matrix
        cm = confusion_matrix(y_true, y_pred)

        # Compute per-class accuracy
        per_class_accuracy = cm.diagonal() / cm.sum(axis=1)

        # Compute support (number of samples per class)
        support = cm.sum(axis=1)

        statistics = {
            "confusion_matrix": cm.tolist(),
            "per_class_accuracy": {
                self.class_names[i]: float(per_class_accuracy[i])
                for i in range(len(self.class_names))
            },
            "per_class_support": {
                self.class_names[i]: int(support[i])
                for i in range(len(self.class_names))
            },
        }

        logger.info(
            f"Confusion matrix generated:\n{cm}\n"
            f"Per-class accuracy: {statistics['per_class_accuracy']}"
        )

        # Visualize confusion matrix
        if save_path:
            self._visualize_confusion_matrix(cm, save_path)

        return cm, statistics

    def _visualize_confusion_matrix(
        self,
        cm: np.ndarray,
        save_path: str,
    ) -> None:
        """
        Visualize confusion matrix and save as PNG.

        Args:
            cm: Confusion matrix
            save_path: Path to save visualization
        """
        plt.figure(figsize=(8, 6))
        
        # Normalize confusion matrix for better visualization
        cm_normalized = cm.astype('float') / cm.sum(axis=1)[:, np.newaxis]
        
        sns.heatmap(
            cm_normalized,
            annot=True,
            fmt='.2f',
            cmap='Blues',
            xticklabels=self.class_names,
            yticklabels=self.class_names,
            cbar_kws={'label': 'Normalized Count'},
        )
        
        plt.title('Confusion Matrix - Direction Prediction')
        plt.ylabel('True Label')
        plt.xlabel('Predicted Label')
        plt.tight_layout()
        
        # Create directory if it doesn't exist
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        
        plt.savefig(save_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        logger.info(f"Confusion matrix visualization saved to {save_path}")

    def compute_all_metrics(
        self,
        y_true: dict[str, np.ndarray],
        y_pred: dict[str, np.ndarray],
        save_confusion_matrix: str | None = None,
    ) -> dict[str, Any]:
        """
        Compute all evaluation metrics.

        Args:
            y_true: Dictionary of true values
            y_pred: Dictionary of predicted values
            save_confusion_matrix: Path to save confusion matrix (optional)

        Returns:
            Dictionary with all metrics

        Example:
            >>> all_metrics = calculator.compute_all_metrics(y_true, y_pred)
            >>> print(f"Overall accuracy: {all_metrics['classification']['accuracy']:.4f}")
        """
        # Classification metrics
        classification_metrics = self.compute_classification_metrics(
            y_true["direction"],
            y_pred["direction"],
        )

        # Regression metrics
        regression_metrics = self.compute_regression_metrics(
            y_true,
            y_pred,
        )

        # Confusion matrix
        cm, cm_stats = self.generate_confusion_matrix(
            y_true["direction"],
            y_pred["direction"],
            save_path=save_confusion_matrix,
        )

        all_metrics = {
            "classification": classification_metrics,
            "regression": regression_metrics,
            "confusion_matrix": cm_stats,
        }

        logger.info("Computed all evaluation metrics")

        return all_metrics


# ══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def compute_metrics(
    y_true: dict[str, np.ndarray],
    y_pred: dict[str, np.ndarray],
    save_confusion_matrix: str | None = None,
) -> dict[str, Any]:
    """
    Convenience function to compute all metrics.

    Args:
        y_true: Dictionary of true values
        y_pred: Dictionary of predicted values
        save_confusion_matrix: Path to save confusion matrix (optional)

    Returns:
        Dictionary with all metrics

    Example:
        >>> metrics = compute_metrics(y_true, y_pred)
        >>> print(f"Accuracy: {metrics['classification']['accuracy']:.4f}")
    """
    calculator = MetricsCalculator()
    return calculator.compute_all_metrics(
        y_true=y_true,
        y_pred=y_pred,
        save_confusion_matrix=save_confusion_matrix,
    )


# Alias for backward compatibility
ModelEvaluator = MetricsCalculator
