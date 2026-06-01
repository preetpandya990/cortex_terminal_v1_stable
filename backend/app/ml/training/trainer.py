"""
Cortex AI — Training Orchestration
====================================
Orchestrates model training with early stopping, checkpointing, and metrics tracking.

Features:
- Full training loop with validation
- Early stopping based on validation accuracy
- Model checkpointing (save best model)
- Comprehensive metrics tracking (accuracy, precision, recall, F1, MAE, RMSE)
- Experiment logging to database
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.models import MultiOutputModel, MultiOutputLoss
from app.models.ml_data import MLExperiment


logger = logging.getLogger(__name__)


class Trainer:
    """
    Training orchestrator for ML models.
    
    Manages the complete training lifecycle including:
    - Training and validation loops
    - Early stopping
    - Model checkpointing
    - Metrics tracking and logging
    """

    def __init__(
        self,
        model: MultiOutputModel,
        session: AsyncSession,
        device: str = "cpu",
        learning_rate: float = 0.001,
        checkpoint_dir: str = "models/checkpoints",
    ):
        """
        Initialize trainer.

        Args:
            model: MultiOutputModel instance
            session: Database session for experiment logging
            device: Device to train on ("cpu" or "cuda")
            learning_rate: Learning rate for optimizer
            checkpoint_dir: Directory to save model checkpoints
        """
        self.model = model.to(device)
        self.session = session
        self.device = device
        self.learning_rate = learning_rate
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

        # Loss function and optimizer
        self.criterion = MultiOutputLoss()
        self.optimizer = optim.Adam(self.model.parameters(), lr=learning_rate)

        # Training state
        self.current_epoch = 0
        self.best_val_accuracy = 0.0
        self.best_model_path = None
        self.training_history = {
            "train_loss": [],
            "val_loss": [],
            "train_accuracy": [],
            "val_accuracy": [],
        }

        logger.info(
            f"Initialized Trainer: device={device}, lr={learning_rate}, "
            f"checkpoint_dir={checkpoint_dir}"
        )

    async def train(
        self,
        train_loader: torch.utils.data.DataLoader,
        val_loader: torch.utils.data.DataLoader,
        num_epochs: int = 100,
        early_stopping_patience: int = 10,
        experiment_name: str = "ml_training",
    ) -> dict[str, Any]:
        """
        Train model with early stopping and checkpointing.

        Args:
            train_loader: DataLoader for training data
            val_loader: DataLoader for validation data
            num_epochs: Maximum number of epochs
            early_stopping_patience: Epochs to wait before early stopping
            experiment_name: Name for experiment tracking

        Returns:
            Dictionary with training results and metrics

        Example:
            >>> trainer = Trainer(model, session)
            >>> results = await trainer.train(train_loader, val_loader, num_epochs=100)
            >>> print(f"Best validation accuracy: {results['best_val_accuracy']:.4f}")
        """
        logger.info(
            f"Starting training: {num_epochs} epochs, "
            f"early_stopping_patience={early_stopping_patience}"
        )

        # Create experiment record
        experiment_id = await self._create_experiment(experiment_name)

        epochs_without_improvement = 0
        start_time = datetime.now(timezone.utc)

        for epoch in range(num_epochs):
            self.current_epoch = epoch

            # Training phase
            train_metrics = await self._train_epoch(train_loader)

            # Validation phase
            val_metrics = await self._validate_epoch(val_loader)

            # Log metrics
            self._log_epoch_metrics(epoch, train_metrics, val_metrics)

            # Update training history
            self.training_history["train_loss"].append(train_metrics["loss"])
            self.training_history["val_loss"].append(val_metrics["loss"])
            self.training_history["train_accuracy"].append(train_metrics["accuracy"])
            self.training_history["val_accuracy"].append(val_metrics["accuracy"])

            # Check for improvement
            if val_metrics["accuracy"] > self.best_val_accuracy:
                self.best_val_accuracy = val_metrics["accuracy"]
                epochs_without_improvement = 0

                # Save best model
                checkpoint_path = await self._save_checkpoint(
                    epoch=epoch,
                    metrics=val_metrics,
                )
                self.best_model_path = checkpoint_path

                logger.info(
                    f"New best model! Val accuracy: {self.best_val_accuracy:.4f}, "
                    f"saved to {checkpoint_path}"
                )
            else:
                epochs_without_improvement += 1

            # Early stopping check
            if epochs_without_improvement >= early_stopping_patience:
                logger.info(
                    f"Early stopping triggered after {epoch + 1} epochs "
                    f"({epochs_without_improvement} epochs without improvement)"
                )
                break

        end_time = datetime.now(timezone.utc)
        training_duration = (end_time - start_time).total_seconds()

        # Restore best checkpoint after training
        if self.best_model_path is not None:
            logger.info("Restoring best checkpoint after training completion")
            self.restore_best_checkpoint()

        # Final results
        results = {
            "experiment_id": experiment_id,
            "total_epochs": self.current_epoch + 1,
            "best_val_accuracy": self.best_val_accuracy,
            "best_model_path": str(self.best_model_path),
            "training_duration_seconds": training_duration,
            "training_history": self.training_history,
            "final_train_metrics": train_metrics,
            "final_val_metrics": val_metrics,
        }

        # Update experiment record
        await self._update_experiment(
            experiment_id=experiment_id,
            status="completed",
            metrics=results,
        )

        logger.info(
            f"Training completed: {results['total_epochs']} epochs, "
            f"best val accuracy: {self.best_val_accuracy:.4f}, "
            f"duration: {training_duration:.1f}s"
        )

        return results

    async def _train_epoch(
        self,
        train_loader: torch.utils.data.DataLoader,
    ) -> dict[str, float]:
        """
        Run one training epoch.

        Args:
            train_loader: DataLoader for training data

        Returns:
            Dictionary with training metrics
        """
        self.model.train()

        total_loss = 0.0
        all_predictions = []
        all_targets = []

        for batch_idx, (features, targets) in enumerate(train_loader):
            # Move to device
            features = features.to(self.device)
            targets = {k: v.to(self.device) for k, v in targets.items()}

            # Forward pass
            self.optimizer.zero_grad()
            predictions = self.model(features)

            # Compute loss
            loss, loss_dict = self.criterion(predictions, targets)

            # Backward pass
            loss.backward()
            
            # Gradient clipping to prevent exploding gradients
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1.0)
            
            self.optimizer.step()

            # Track metrics
            total_loss += loss.item()
            
            # Store predictions and targets for accuracy calculation
            pred_direction = torch.argmax(predictions["direction"], dim=1)
            all_predictions.extend(pred_direction.cpu().numpy())
            all_targets.extend(targets["direction"].cpu().numpy())

        # Calculate metrics
        avg_loss = total_loss / len(train_loader)
        accuracy = accuracy_score(all_targets, all_predictions)
        precision, recall, f1, _ = precision_recall_fscore_support(
            all_targets,
            all_predictions,
            average="weighted",
            zero_division=0,
        )

        metrics = {
            "loss": avg_loss,
            "accuracy": accuracy,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

        return metrics

    async def _validate_epoch(
        self,
        val_loader: torch.utils.data.DataLoader,
    ) -> dict[str, float]:
        """
        Run validation on validation set.

        Args:
            val_loader: DataLoader for validation data

        Returns:
            Dictionary with validation metrics
        """
        self.model.eval()

        total_loss = 0.0
        all_predictions = []
        all_targets = []
        
        # Regression targets for MAE/RMSE
        all_entry_preds = []
        all_entry_targets = []

        with torch.no_grad():
            for features, targets in val_loader:
                # Move to device
                features = features.to(self.device)
                targets = {k: v.to(self.device) for k, v in targets.items()}

                # Forward pass
                predictions = self.model(features)

                # Compute loss
                loss, loss_dict = self.criterion(predictions, targets)
                total_loss += loss.item()

                # Store predictions and targets
                pred_direction = torch.argmax(predictions["direction"], dim=1)
                all_predictions.extend(pred_direction.cpu().numpy())
                all_targets.extend(targets["direction"].cpu().numpy())
                
                # Store regression targets
                all_entry_preds.extend(predictions["entry_price"].cpu().numpy())
                all_entry_targets.extend(targets["entry_price"].cpu().numpy())

        # Calculate metrics
        avg_loss = total_loss / len(val_loader)
        accuracy = accuracy_score(all_targets, all_predictions)
        precision, recall, f1, _ = precision_recall_fscore_support(
            all_targets,
            all_predictions,
            average="weighted",
            zero_division=0,
        )

        # Regression metrics (MAE, RMSE for entry price)
        entry_preds = np.array(all_entry_preds).flatten()
        entry_targets = np.array(all_entry_targets).flatten()
        mae = np.mean(np.abs(entry_preds - entry_targets))
        rmse = np.sqrt(np.mean((entry_preds - entry_targets) ** 2))

        metrics = {
            "loss": avg_loss,
            "accuracy": accuracy,
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "mae": mae,
            "rmse": rmse,
        }

        return metrics

    def _log_epoch_metrics(
        self,
        epoch: int,
        train_metrics: dict[str, float],
        val_metrics: dict[str, float],
    ) -> None:
        """
        Log metrics for current epoch.

        Args:
            epoch: Current epoch number
            train_metrics: Training metrics
            val_metrics: Validation metrics
        """
        logger.info(
            f"Epoch {epoch + 1}/{self.current_epoch + 1} | "
            f"Train Loss: {train_metrics['loss']:.4f}, "
            f"Train Acc: {train_metrics['accuracy']:.4f} | "
            f"Val Loss: {val_metrics['loss']:.4f}, "
            f"Val Acc: {val_metrics['accuracy']:.4f}, "
            f"Val F1: {val_metrics['f1']:.4f}"
        )

    async def _save_checkpoint(
        self,
        epoch: int,
        metrics: dict[str, float],
    ) -> Path:
        """
        Save model checkpoint.

        Args:
            epoch: Current epoch number
            metrics: Validation metrics

        Returns:
            Path to saved checkpoint
        """
        checkpoint_path = self.checkpoint_dir / f"model_epoch_{epoch}_acc_{metrics['accuracy']:.4f}.pt"

        checkpoint = {
            "epoch": epoch,
            "model_state_dict": self.model.state_dict(),
            "optimizer_state_dict": self.optimizer.state_dict(),
            "metrics": metrics,
            "training_history": self.training_history,
        }

        torch.save(checkpoint, checkpoint_path)

        logger.debug(f"Saved checkpoint to {checkpoint_path}")

        return checkpoint_path

    async def _create_experiment(self, experiment_name: str) -> int:
        """
        Create experiment record in database.

        Args:
            experiment_name: Name of the experiment

        Returns:
            Experiment ID
        """
        experiment = MLExperiment(
            experiment_name=experiment_name,
            hyperparameters={
                "learning_rate": self.learning_rate,
                "model_type": "lstm_transformer_hybrid",
                "device": self.device,
            },
            metrics={},
            artifacts={},
            status="running",
            started_at=datetime.now(timezone.utc),
        )

        self.session.add(experiment)
        await self.session.commit()
        await self.session.refresh(experiment)

        logger.info(f"Created experiment: {experiment_name} (ID: {experiment.id})")

        return experiment.id

    async def _update_experiment(
        self,
        experiment_id: int,
        status: str,
        metrics: dict[str, Any],
    ) -> None:
        """
        Update experiment record with final results.

        Args:
            experiment_id: Experiment ID
            status: Final status ("completed" or "failed")
            metrics: Final metrics
        """
        # TODO: Implement actual database update
        # For now, just log
        logger.info(
            f"Experiment {experiment_id} {status}: "
            f"best_val_accuracy={metrics.get('best_val_accuracy', 0):.4f}"
        )

    def load_checkpoint(self, checkpoint_path: str) -> None:
        """
        Load model from checkpoint.

        Args:
            checkpoint_path: Path to checkpoint file
        """
        checkpoint = torch.load(checkpoint_path, map_location=self.device)

        self.model.load_state_dict(checkpoint["model_state_dict"])
        self.optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        self.current_epoch = checkpoint["epoch"]
        self.training_history = checkpoint.get("training_history", {})

        logger.info(f"Loaded checkpoint from {checkpoint_path} (epoch {self.current_epoch})")

    def restore_best_checkpoint(self) -> None:
        """
        Restore the best model checkpoint after training.
        
        This is typically called after early stopping to ensure
        the model is in its best state.
        
        Raises:
            ValueError: If no best checkpoint exists
        """
        if self.best_model_path is None:
            raise ValueError("No best checkpoint available to restore")
        
        self.load_checkpoint(str(self.best_model_path))
        
        logger.info(
            f"Restored best checkpoint: {self.best_model_path} "
            f"(val_accuracy: {self.best_val_accuracy:.4f})"
        )



# ══════════════════════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def create_trainer(
    model: MultiOutputModel,
    session: AsyncSession,
    device: str = "cpu",
    learning_rate: float = 0.001,
    checkpoint_dir: str = "models/checkpoints",
) -> Trainer:
    """
    Factory function to create Trainer instance.

    Args:
        model: MultiOutputModel instance
        session: Database session
        device: Device to train on
        learning_rate: Learning rate
        checkpoint_dir: Checkpoint directory

    Returns:
        Configured Trainer instance
    """
    return Trainer(
        model=model,
        session=session,
        device=device,
        learning_rate=learning_rate,
        checkpoint_dir=checkpoint_dir,
    )


# Alias for backward compatibility
ModelTrainer = Trainer
