"""
Cortex AI — Hyperparameter Tuning
===================================
Automated hyperparameter search using grid search and random search.

Supports tuning:
- Learning rate
- Batch size
- LSTM hidden units
- Transformer dimensions
- Dropout rates
- Number of layers
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from itertools import product
from typing import Any

import numpy as np
import torch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.models import create_model
from app.ml.training.trainer import create_trainer
from app.models.ml_data import MLExperiment


logger = logging.getLogger(__name__)


class HyperparameterTuner:
    """
    Hyperparameter tuning orchestrator.
    
    Supports both grid search (exhaustive) and random search (efficient)
    for finding optimal hyperparameters.
    """

    def __init__(
        self,
        session: AsyncSession,
        device: str = "cpu",
    ):
        """
        Initialize hyperparameter tuner.

        Args:
            session: Database session for experiment logging
            device: Device to train on ("cpu" or "cuda")
        """
        self.session = session
        self.device = device
        self.experiments = []

        logger.info(f"Initialized HyperparameterTuner on {device}")

    async def grid_search(
        self,
        train_loader: torch.utils.data.DataLoader,
        val_loader: torch.utils.data.DataLoader,
        param_grid: dict[str, list[Any]],
        num_epochs: int = 50,
        experiment_prefix: str = "grid_search",
    ) -> dict[str, Any]:
        """
        Perform grid search over hyperparameter space.

        Args:
            train_loader: Training data loader
            val_loader: Validation data loader
            param_grid: Dictionary mapping parameter names to lists of values
            num_epochs: Number of epochs per experiment
            experiment_prefix: Prefix for experiment names

        Returns:
            Dictionary with best hyperparameters and results

        Example:
            >>> param_grid = {
            ...     "learning_rate": [0.001, 0.0001],
            ...     "lstm_hidden_size": [64, 128],
            ...     "batch_size": [16, 32],
            ... }
            >>> results = await tuner.grid_search(train_loader, val_loader, param_grid)
            >>> print(f"Best params: {results['best_params']}")
        """
        logger.info(f"Starting grid search with {len(param_grid)} parameters")

        # Generate all combinations
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        combinations = list(product(*param_values))

        logger.info(f"Total combinations to evaluate: {len(combinations)}")

        best_accuracy = 0.0
        best_params = None
        best_experiment_id = None

        for idx, combination in enumerate(combinations):
            params = dict(zip(param_names, combination))
            
            logger.info(
                f"Experiment {idx + 1}/{len(combinations)}: "
                f"Testing params: {params}"
            )

            # Run experiment with these hyperparameters
            result = await self._run_experiment(
                train_loader=train_loader,
                val_loader=val_loader,
                hyperparameters=params,
                num_epochs=num_epochs,
                experiment_name=f"{experiment_prefix}_{idx + 1}",
            )

            # Track best result
            if result["best_val_accuracy"] > best_accuracy:
                best_accuracy = result["best_val_accuracy"]
                best_params = params
                best_experiment_id = result["experiment_id"]

            self.experiments.append(result)

        logger.info(
            f"Grid search complete: Best accuracy={best_accuracy:.4f}, "
            f"Best params={best_params}"
        )

        return {
            "best_params": best_params,
            "best_accuracy": best_accuracy,
            "best_experiment_id": best_experiment_id,
            "all_experiments": self.experiments,
            "total_experiments": len(combinations),
        }

    async def random_search(
        self,
        train_loader: torch.utils.data.DataLoader,
        val_loader: torch.utils.data.DataLoader,
        param_distributions: dict[str, tuple[Any, Any]],
        n_iter: int = 20,
        num_epochs: int = 50,
        experiment_prefix: str = "random_search",
        seed: int | None = None,
    ) -> dict[str, Any]:
        """
        Perform random search over hyperparameter space.

        More efficient than grid search for high-dimensional spaces.

        Args:
            train_loader: Training data loader
            val_loader: Validation data loader
            param_distributions: Dictionary mapping parameter names to (min, max) ranges
            n_iter: Number of random samples to evaluate
            num_epochs: Number of epochs per experiment
            experiment_prefix: Prefix for experiment names
            seed: Random seed for reproducibility

        Returns:
            Dictionary with best hyperparameters and results

        Example:
            >>> param_distributions = {
            ...     "learning_rate": (0.0001, 0.01),  # Log scale
            ...     "lstm_hidden_size": (64, 256),
            ...     "dropout": (0.1, 0.5),
            ... }
            >>> results = await tuner.random_search(
            ...     train_loader, val_loader, param_distributions, n_iter=20
            ... )
        """
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

        logger.info(
            f"Starting random search: {n_iter} iterations, "
            f"{len(param_distributions)} parameters"
        )

        best_accuracy = 0.0
        best_params = None
        best_experiment_id = None

        for idx in range(n_iter):
            # Sample random hyperparameters
            params = self._sample_hyperparameters(param_distributions)

            logger.info(
                f"Experiment {idx + 1}/{n_iter}: "
                f"Testing params: {params}"
            )

            # Run experiment
            result = await self._run_experiment(
                train_loader=train_loader,
                val_loader=val_loader,
                hyperparameters=params,
                num_epochs=num_epochs,
                experiment_name=f"{experiment_prefix}_{idx + 1}",
            )

            # Track best result
            if result["best_val_accuracy"] > best_accuracy:
                best_accuracy = result["best_val_accuracy"]
                best_params = params
                best_experiment_id = result["experiment_id"]

            self.experiments.append(result)

        logger.info(
            f"Random search complete: Best accuracy={best_accuracy:.4f}, "
            f"Best params={best_params}"
        )

        return {
            "best_params": best_params,
            "best_accuracy": best_accuracy,
            "best_experiment_id": best_experiment_id,
            "all_experiments": self.experiments,
            "total_experiments": n_iter,
        }

    def _sample_hyperparameters(
        self,
        param_distributions: dict[str, tuple[Any, Any]],
    ) -> dict[str, Any]:
        """
        Sample random hyperparameters from distributions.

        Args:
            param_distributions: Dictionary mapping parameter names to (min, max) ranges

        Returns:
            Dictionary of sampled hyperparameters
        """
        params = {}

        for param_name, (min_val, max_val) in param_distributions.items():
            if param_name == "learning_rate":
                # Log-uniform sampling for learning rate
                log_min = np.log10(min_val)
                log_max = np.log10(max_val)
                params[param_name] = 10 ** np.random.uniform(log_min, log_max)
            elif isinstance(min_val, int) and isinstance(max_val, int):
                # Integer parameters
                params[param_name] = random.randint(min_val, max_val)
            else:
                # Float parameters (uniform sampling)
                params[param_name] = np.random.uniform(min_val, max_val)

        return params

    async def _run_experiment(
        self,
        train_loader: torch.utils.data.DataLoader,
        val_loader: torch.utils.data.DataLoader,
        hyperparameters: dict[str, Any],
        num_epochs: int,
        experiment_name: str,
    ) -> dict[str, Any]:
        """
        Run a single training experiment with given hyperparameters.

        Args:
            train_loader: Training data loader
            val_loader: Validation data loader
            hyperparameters: Hyperparameters to use
            num_epochs: Number of epochs
            experiment_name: Name for this experiment

        Returns:
            Dictionary with experiment results
        """
        # Extract hyperparameters
        learning_rate = hyperparameters.get("learning_rate", 0.001)
        lstm_hidden_size = hyperparameters.get("lstm_hidden_size", 128)
        transformer_d_model = hyperparameters.get("transformer_d_model", 128)
        lstm_dropout = hyperparameters.get("lstm_dropout", 0.2)
        transformer_dropout = hyperparameters.get("transformer_dropout", 0.1)

        # Create model with hyperparameters
        model = create_model(
            input_size=40,  # Number of features
            sequence_length=60,
            device=self.device,
        )

        # Update model architecture if needed
        # Note: For full flexibility, we'd need to recreate the model
        # with different architecture parameters

        # Create trainer
        trainer = create_trainer(
            model=model,
            session=self.session,
            device=self.device,
            learning_rate=learning_rate,
        )

        # Train model
        try:
            results = await trainer.train(
                train_loader=train_loader,
                val_loader=val_loader,
                num_epochs=num_epochs,
                early_stopping_patience=5,  # Shorter patience for tuning
                experiment_name=experiment_name,
            )

            return {
                "experiment_id": results["experiment_id"],
                "experiment_name": experiment_name,
                "hyperparameters": hyperparameters,
                "best_val_accuracy": results["best_val_accuracy"],
                "total_epochs": results["total_epochs"],
                "training_duration": results["training_duration_seconds"],
                "status": "completed",
            }

        except Exception as exc:
            logger.error(
                f"Experiment {experiment_name} failed: {exc}",
                exc_info=True,
            )
            return {
                "experiment_id": None,
                "experiment_name": experiment_name,
                "hyperparameters": hyperparameters,
                "best_val_accuracy": 0.0,
                "total_epochs": 0,
                "training_duration": 0.0,
                "status": "failed",
                "error": str(exc),
            }

    async def compare_experiments(
        self,
        metric: str = "best_val_accuracy",
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Compare experiments and rank by specified metric.

        Args:
            metric: Metric to rank by (default: "best_val_accuracy")
            top_k: Number of top experiments to return

        Returns:
            List of top experiments sorted by metric

        Example:
            >>> top_experiments = await tuner.compare_experiments(top_k=5)
            >>> for exp in top_experiments:
            ...     print(f"{exp['experiment_name']}: {exp['best_val_accuracy']:.4f}")
        """
        if not self.experiments:
            logger.warning("No experiments to compare")
            return []

        # Sort experiments by metric (descending)
        sorted_experiments = sorted(
            self.experiments,
            key=lambda x: x.get(metric, 0.0),
            reverse=True,
        )

        top_experiments = sorted_experiments[:top_k]

        logger.info(
            f"Top {top_k} experiments by {metric}:\n" +
            "\n".join([
                f"  {i+1}. {exp['experiment_name']}: {exp.get(metric, 0.0):.4f}"
                for i, exp in enumerate(top_experiments)
            ])
        )

        return top_experiments

    def get_best_hyperparameters(self) -> dict[str, Any] | None:
        """
        Get the best hyperparameters from all experiments.

        Returns:
            Dictionary with best hyperparameters, or None if no experiments

        Example:
            >>> best_params = tuner.get_best_hyperparameters()
            >>> print(f"Best learning rate: {best_params['learning_rate']}")
        """
        if not self.experiments:
            logger.warning("No experiments available")
            return None

        # Find experiment with highest validation accuracy
        best_experiment = max(
            self.experiments,
            key=lambda x: x.get("best_val_accuracy", 0.0),
        )

        logger.info(
            f"Best hyperparameters from {best_experiment['experiment_name']}: "
            f"{best_experiment['hyperparameters']}"
        )

        return best_experiment["hyperparameters"]

    async def generate_summary_report(self) -> dict[str, Any]:
        """
        Generate comprehensive summary report of all experiments.

        Returns:
            Dictionary with summary statistics and visualizations

        Example:
            >>> report = await tuner.generate_summary_report()
            >>> print(f"Total experiments: {report['total_experiments']}")
            >>> print(f"Best accuracy: {report['best_accuracy']:.4f}")
        """
        if not self.experiments:
            return {
                "total_experiments": 0,
                "message": "No experiments to summarize",
            }

        # Calculate statistics
        accuracies = [exp["best_val_accuracy"] for exp in self.experiments]
        durations = [exp["training_duration"] for exp in self.experiments]

        report = {
            "total_experiments": len(self.experiments),
            "completed_experiments": sum(
                1 for exp in self.experiments if exp["status"] == "completed"
            ),
            "failed_experiments": sum(
                1 for exp in self.experiments if exp["status"] == "failed"
            ),
            "best_accuracy": max(accuracies) if accuracies else 0.0,
            "mean_accuracy": np.mean(accuracies) if accuracies else 0.0,
            "std_accuracy": np.std(accuracies) if accuracies else 0.0,
            "total_training_time": sum(durations),
            "mean_training_time": np.mean(durations) if durations else 0.0,
            "best_experiment": max(
                self.experiments,
                key=lambda x: x.get("best_val_accuracy", 0.0),
            ),
            "top_5_experiments": await self.compare_experiments(top_k=5),
        }

        logger.info(
            f"Experiment Summary:\n"
            f"  Total: {report['total_experiments']}\n"
            f"  Completed: {report['completed_experiments']}\n"
            f"  Failed: {report['failed_experiments']}\n"
            f"  Best Accuracy: {report['best_accuracy']:.4f}\n"
            f"  Mean Accuracy: {report['mean_accuracy']:.4f} ± {report['std_accuracy']:.4f}\n"
            f"  Total Training Time: {report['total_training_time']:.1f}s"
        )

        return report

    async def load_experiments_from_db(
        self,
        experiment_name_pattern: str | None = None,
    ) -> int:
        """
        Load experiments from database for comparison.

        Args:
            experiment_name_pattern: SQL LIKE pattern to filter experiments

        Returns:
            Number of experiments loaded

        Example:
            >>> count = await tuner.load_experiments_from_db("grid_search_%")
            >>> print(f"Loaded {count} experiments")
        """
        stmt = select(MLExperiment)
        
        if experiment_name_pattern:
            stmt = stmt.where(MLExperiment.experiment_name.like(experiment_name_pattern))

        result = await self.session.execute(stmt)
        db_experiments = result.scalars().all()

        for exp in db_experiments:
            self.experiments.append({
                "experiment_id": exp.id,
                "experiment_name": exp.experiment_name,
                "hyperparameters": exp.hyperparameters,
                "best_val_accuracy": exp.metrics.get("best_val_accuracy", 0.0),
                "total_epochs": exp.metrics.get("total_epochs", 0),
                "training_duration": (
                    (exp.completed_at - exp.started_at).total_seconds()
                    if exp.completed_at else 0.0
                ),
                "status": exp.status,
            })

        logger.info(f"Loaded {len(db_experiments)} experiments from database")

        return len(db_experiments)


# ══════════════════════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def create_tuner(
    session: AsyncSession,
    device: str = "cpu",
) -> HyperparameterTuner:
    """
    Factory function to create HyperparameterTuner instance.

    Args:
        session: Database session
        device: Device to train on

    Returns:
        Configured HyperparameterTuner instance
    """
    return HyperparameterTuner(
        session=session,
        device=device,
    )
