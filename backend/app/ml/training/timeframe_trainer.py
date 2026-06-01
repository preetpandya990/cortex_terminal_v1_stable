"""
Cortex AI — Timeframe-Specific Model Trainer
==============================================
Training script for timeframe-specific models (daily, weekly, monthly).

This module implements Requirements 4.2, 4.3:
- Train separate models for daily, weekly, monthly timeframes
- Use same architecture but different training data (different sequence lengths)
- Store models in registry with timeframe tag
- Validate each model meets 85% accuracy threshold
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_recall_fscore_support

from app.ml.models.multi_output_model import (
    MultiOutputModel,
    MultiOutputLoss,
)
from app.ml.training.timeframe_config import (
    get_timeframe_config,
    get_training_config,
)

logger = logging.getLogger(__name__)


class TimeframeModelTrainer:
    """
    Trainer for timeframe-specific models.
    
    Trains separate models for daily, weekly, and monthly timeframes using
    the same LSTM+Transformer architecture but with different sequence lengths
    and training data.
    """
    
    def __init__(
        self,
        timeframe: str,
        input_size: int = 40,
        device: str | None = None,
        model_save_dir: str | Path = "models/timeframe_specific",
    ):
        """
        Initialize timeframe-specific trainer.

        Args:
            timeframe: Timeframe identifier ("1d", "1w", "1M")
            input_size: Number of input features (default: 40)
            device: Device to train on ("cpu", "cuda", or None for auto-detect)
            model_save_dir: Directory to save trained models
        """
        self.timeframe = timeframe
        self.input_size = input_size
        
        # Get timeframe-specific configuration
        self.config = get_timeframe_config(timeframe)
        self.training_config = get_training_config(timeframe)
        
        # Set device
        if device is None:
            self.device = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            self.device = device
            
        logger.info(f"Using device: {self.device}")
        
        # Model save directory
        self.model_save_dir = Path(model_save_dir)
        self.model_save_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize model
        self.model = self._create_model()
        
        # Initialize loss function
        self.criterion = MultiOutputLoss(
            direction_weight=2.0,
            confidence_weight=1.0,
            price_weight=1.5,
            volatility_weight=1.0,
            consistency_weight=0.5,
        )
        
        # Initialize optimizer
        self.optimizer = torch.optim.Adam(
            self.model.parameters(),
            lr=self.training_config["learning_rate"],
            weight_decay=0.0001,
        )
        
        # Learning rate scheduler
        self.scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
            self.optimizer,
            mode="min",
            factor=0.5,
            patience=5,
            min_lr=1e-6,
        )
        
        # Training history
        self.history: dict[str, list[float]] = {
            "train_loss": [],
            "val_loss": [],
            "train_accuracy": [],
            "val_accuracy": [],
        }
        
        logger.info(
            f"Initialized {self.config['name']} model trainer "
            f"(sequence_length={self.config['sequence_length']})"
        )
    
    def _create_model(self) -> MultiOutputModel:
        """Create model with timeframe-specific sequence length."""
        model = MultiOutputModel(
            input_size=self.input_size,
            sequence_length=self.config["sequence_length"],
            lstm_hidden_size=128,
            lstm_num_layers=2,
            lstm_dropout=0.2,
            transformer_d_model=128,
            transformer_nhead=8,
            transformer_num_layers=2,
            transformer_dropout=0.1,
        )
        
        model = model.to(self.device)
        
        num_params = model.get_num_parameters()
        logger.info(f"Created model with {num_params:,} trainable parameters")
        
        return model
    
    def prepare_data(
        self,
        features: np.ndarray,
        targets: dict[str, np.ndarray],
    ) -> tuple[DataLoader, DataLoader, DataLoader]:
        """
        Prepare data loaders for training, validation, and testing.
        
        Args:
            features: Feature array of shape (n_samples, sequence_length, n_features)
            targets: Dictionary of target arrays
            
        Returns:
            Tuple of (train_loader, val_loader, test_loader)
        """
        # Validate data shape
        expected_seq_len = self.config["sequence_length"]
        if features.shape[1] != expected_seq_len:
            raise ValueError(
                f"Expected sequence length {expected_seq_len}, "
                f"got {features.shape[1]}"
            )
        
        # Split data
        val_split = self.training_config["validation_split"]
        test_split = self.training_config["test_split"]
        
        # First split: train+val vs test
        train_val_size = 1.0 - test_split
        X_train_val, X_test, y_train_val, y_test = self._split_data(
            features, targets, test_size=test_split
        )
        
        # Second split: train vs val
        val_size_adjusted = val_split / train_val_size
        X_train, X_val, y_train, y_val = self._split_data(
            X_train_val, y_train_val, test_size=val_size_adjusted
        )
        
        logger.info(
            f"Data split: train={len(X_train)}, "
            f"val={len(X_val)}, test={len(X_test)}"
        )
        
        # Create data loaders
        batch_size = self.training_config["batch_size"]
        
        train_loader = self._create_dataloader(X_train, y_train, batch_size, shuffle=True)
        val_loader = self._create_dataloader(X_val, y_val, batch_size, shuffle=False)
        test_loader = self._create_dataloader(X_test, y_test, batch_size, shuffle=False)
        
        return train_loader, val_loader, test_loader
    
    def _split_data(
        self,
        features: np.ndarray,
        targets: dict[str, np.ndarray],
        test_size: float,
    ) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray], dict[str, np.ndarray]]:
        """Split features and targets into train and test sets."""
        # Use stratified split based on direction
        X_train, X_test, direction_train, direction_test = train_test_split(
            features,
            targets["direction"],
            test_size=test_size,
            stratify=targets["direction"],
            random_state=42,
        )
        
        # Split other targets using the same indices
        train_indices = np.isin(
            np.arange(len(features)),
            np.where(np.isin(features, X_train).all(axis=(1, 2)))[0]
        )
        
        y_train = {
            "direction": direction_train,
        }
        y_test = {
            "direction": direction_test,
        }
        
        for key in targets:
            if key != "direction":
                y_train[key] = targets[key][train_indices]
                y_test[key] = targets[key][~train_indices]
        
        return X_train, X_test, y_train, y_test
    
    def _create_dataloader(
        self,
        features: np.ndarray,
        targets: dict[str, np.ndarray],
        batch_size: int,
        shuffle: bool,
    ) -> DataLoader:
        """Create PyTorch DataLoader from numpy arrays."""
        # Convert to tensors
        X_tensor = torch.FloatTensor(features)
        y_tensors = {
            key: torch.FloatTensor(val) for key, val in targets.items()
        }
        
        # Create dataset
        dataset = TensorDataset(X_tensor, *y_tensors.values())
        
        # Create dataloader
        dataloader = DataLoader(
            dataset,
            batch_size=batch_size,
            shuffle=shuffle,
            num_workers=0,  # Use 0 for compatibility
            pin_memory=True if self.device == "cuda" else False,
        )
        
        return dataloader
    
    def train_epoch(self, train_loader: DataLoader) -> tuple[float, float]:
        """
        Train for one epoch.
        
        Returns:
            Tuple of (average_loss, accuracy)
        """
        self.model.train()
        
        total_loss = 0.0
        all_predictions = []
        all_targets = []
        
        for batch_idx, batch_data in enumerate(train_loader):
            # Unpack batch
            X_batch = batch_data[0].to(self.device)
            y_batch = {
                "direction": batch_data[1].to(self.device),
                "confidence": batch_data[2].to(self.device),
                "entry_price": batch_data[3].to(self.device),
                "tp1": batch_data[4].to(self.device),
                "tp2": batch_data[5].to(self.device),
                "tp3": batch_data[6].to(self.device),
                "stop_loss": batch_data[7].to(self.device),
                "volatility": batch_data[8].to(self.device),
            }
            
            # Forward pass
            predictions = self.model(X_batch)
            
            # Compute loss
            loss, _ = self.criterion(predictions, y_batch)
            
            # Backward pass
            self.optimizer.zero_grad()
            loss.backward()
            
            # Gradient clipping
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1.0)
            
            self.optimizer.step()
            
            # Track metrics
            total_loss += loss.item()
            
            # Track direction predictions for accuracy
            pred_direction = torch.argmax(predictions["direction"], dim=1)
            all_predictions.extend(pred_direction.cpu().numpy())
            all_targets.extend(y_batch["direction"].cpu().numpy())
        
        # Compute metrics
        avg_loss = total_loss / len(train_loader)
        accuracy = accuracy_score(all_targets, all_predictions)
        
        return avg_loss, accuracy
    
    def validate(self, val_loader: DataLoader) -> tuple[float, float]:
        """
        Validate model.
        
        Returns:
            Tuple of (average_loss, accuracy)
        """
        self.model.eval()
        
        total_loss = 0.0
        all_predictions = []
        all_targets = []
        
        with torch.no_grad():
            for batch_data in val_loader:
                # Unpack batch
                X_batch = batch_data[0].to(self.device)
                y_batch = {
                    "direction": batch_data[1].to(self.device),
                    "confidence": batch_data[2].to(self.device),
                    "entry_price": batch_data[3].to(self.device),
                    "tp1": batch_data[4].to(self.device),
                    "tp2": batch_data[5].to(self.device),
                    "tp3": batch_data[6].to(self.device),
                    "stop_loss": batch_data[7].to(self.device),
                    "volatility": batch_data[8].to(self.device),
                }
                
                # Forward pass
                predictions = self.model(X_batch)
                
                # Compute loss
                loss, _ = self.criterion(predictions, y_batch)
                
                total_loss += loss.item()
                
                # Track direction predictions for accuracy
                pred_direction = torch.argmax(predictions["direction"], dim=1)
                all_predictions.extend(pred_direction.cpu().numpy())
                all_targets.extend(y_batch["direction"].cpu().numpy())
        
        # Compute metrics
        avg_loss = total_loss / len(val_loader)
        accuracy = accuracy_score(all_targets, all_predictions)
        
        return avg_loss, accuracy


    def train(
        self,
        train_loader: DataLoader,
        val_loader: DataLoader,
        min_accuracy: float = 0.85,
    ) -> dict[str, Any]:
        """
        Train model with early stopping and accuracy validation.
        
        Args:
            train_loader: Training data loader
            val_loader: Validation data loader
            min_accuracy: Minimum accuracy threshold (default: 0.85 = 85%)
            
        Returns:
            Training results dictionary with metrics and model path
            
        Raises:
            ValueError: If model doesn't meet minimum accuracy threshold
        """
        epochs = self.training_config["epochs"]
        patience = self.training_config["early_stopping_patience"]
        
        best_val_loss = float("inf")
        best_val_accuracy = 0.0
        patience_counter = 0
        best_model_state = None
        
        logger.info(f"Starting training for {epochs} epochs...")
        
        for epoch in range(epochs):
            # Train
            train_loss, train_accuracy = self.train_epoch(train_loader)
            
            # Validate
            val_loss, val_accuracy = self.validate(val_loader)
            
            # Update learning rate
            self.scheduler.step(val_loss)
            
            # Track history
            self.history["train_loss"].append(train_loss)
            self.history["val_loss"].append(val_loss)
            self.history["train_accuracy"].append(train_accuracy)
            self.history["val_accuracy"].append(val_accuracy)
            
            # Log progress
            logger.info(
                f"Epoch {epoch+1}/{epochs} - "
                f"train_loss: {train_loss:.4f}, train_acc: {train_accuracy:.4f}, "
                f"val_loss: {val_loss:.4f}, val_acc: {val_accuracy:.4f}"
            )
            
            # Check for improvement
            if val_loss < best_val_loss:
                best_val_loss = val_loss
                best_val_accuracy = val_accuracy
                best_model_state = self.model.state_dict().copy()
                patience_counter = 0
                
                logger.info(f"New best model! val_loss: {val_loss:.4f}, val_acc: {val_accuracy:.4f}")
            else:
                patience_counter += 1
                
                if patience_counter >= patience:
                    logger.info(f"Early stopping triggered after {epoch+1} epochs")
                    break
        
        # Restore best model
        if best_model_state is not None:
            self.model.load_state_dict(best_model_state)
            logger.info(f"Restored best model with val_acc: {best_val_accuracy:.4f}")
        
        # Validate accuracy threshold
        if best_val_accuracy < min_accuracy:
            raise ValueError(
                f"Model accuracy {best_val_accuracy:.4f} is below "
                f"minimum threshold {min_accuracy:.4f}"
            )
        
        logger.info(
            f"Training completed! Best validation accuracy: {best_val_accuracy:.4f} "
            f"(threshold: {min_accuracy:.4f})"
        )
        
        # Save model
        model_path = self._save_model()
        
        return {
            "timeframe": self.timeframe,
            "best_val_loss": best_val_loss,
            "best_val_accuracy": best_val_accuracy,
            "model_path": str(model_path),
            "training_history": self.history,
            "meets_threshold": best_val_accuracy >= min_accuracy,
        }
    
    def _save_model(self) -> Path:
        """Save trained model to disk."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        model_filename = f"model_{self.timeframe}_{timestamp}.pt"
        model_path = self.model_save_dir / model_filename
        
        # Save model state and metadata
        torch.save({
            "model_state_dict": self.model.state_dict(),
            "timeframe": self.timeframe,
            "config": self.config,
            "training_config": self.training_config,
            "input_size": self.input_size,
            "sequence_length": self.config["sequence_length"],
            "history": self.history,
        }, model_path)
        
        logger.info(f"Model saved to: {model_path}")
        
        return model_path
    
    def evaluate(self, test_loader: DataLoader) -> dict[str, Any]:
        """
        Evaluate model on test set.
        
        Args:
            test_loader: Test data loader
            
        Returns:
            Dictionary with evaluation metrics
        """
        self.model.eval()
        
        all_predictions = []
        all_targets = []
        
        with torch.no_grad():
            for batch_data in test_loader:
                X_batch = batch_data[0].to(self.device)
                y_batch = {
                    "direction": batch_data[1].to(self.device),
                }
                
                # Forward pass
                predictions = self.model(X_batch)
                
                # Track direction predictions
                pred_direction = torch.argmax(predictions["direction"], dim=1)
                all_predictions.extend(pred_direction.cpu().numpy())
                all_targets.extend(y_batch["direction"].cpu().numpy())
        
        # Compute metrics
        accuracy = accuracy_score(all_targets, all_predictions)
        precision, recall, f1, _ = precision_recall_fscore_support(
            all_targets, all_predictions, average="weighted"
        )
        
        metrics = {
            "accuracy": accuracy,
            "precision": precision,
            "recall": recall,
            "f1_score": f1,
        }
        
        logger.info(
            f"Test metrics - accuracy: {accuracy:.4f}, "
            f"precision: {precision:.4f}, recall: {recall:.4f}, f1: {f1:.4f}"
        )
        
        return metrics


def train_timeframe_model(
    timeframe: str,
    features: np.ndarray,
    targets: dict[str, np.ndarray],
    input_size: int = 40,
    min_accuracy: float = 0.85,
    device: str | None = None,
) -> dict[str, Any]:
    """
    Train a timeframe-specific model.

    Args:
        timeframe: Timeframe identifier ("1d", "1w", "1M")
        features: Feature array of shape (n_samples, sequence_length, n_features)
        targets: Dictionary of target arrays
        input_size: Number of input features (default: 37)
        min_accuracy: Minimum accuracy threshold (default: 0.85)
        device: Device to train on (None for auto-detect)
        
    Returns:
        Training results dictionary
        
    Raises:
        ValueError: If model doesn't meet minimum accuracy threshold
    """
    # Initialize trainer
    trainer = TimeframeModelTrainer(
        timeframe=timeframe,
        input_size=input_size,
        device=device,
    )
    
    # Prepare data
    train_loader, val_loader, test_loader = trainer.prepare_data(features, targets)
    
    # Train model
    training_results = trainer.train(
        train_loader=train_loader,
        val_loader=val_loader,
        min_accuracy=min_accuracy,
    )
    
    # Evaluate on test set
    test_metrics = trainer.evaluate(test_loader)
    training_results["test_metrics"] = test_metrics
    
    return training_results
