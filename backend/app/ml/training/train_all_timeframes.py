"""
Cortex AI — Train All Timeframe-Specific Models
=================================================
Script to train models for all timeframes (daily, weekly, monthly) and
register them in the model registry with timeframe tags.

This script implements Requirements 4.2, 4.3:
- Train separate models for daily, weekly, monthly timeframes
- Store models in registry with timeframe tag
- Validate each model meets 85% accuracy threshold
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import numpy as np
import torch
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.ml.training.timeframe_trainer import train_timeframe_model
from app.ml.training.timeframe_config import get_all_timeframes
from app.ml.model_registry import ModelRegistry
from app.ml.features.timeframe_features import (
    TimeframeFeatureComputer,
    get_timeframe_feature_definitions,
)
from app.ml.feature_store import FeatureStore
from app.core.redis import CacheService

logger = logging.getLogger(__name__)


class TimeframeModelPipeline:
    """
    Pipeline for training and registering timeframe-specific models.
    """
    
    def __init__(
        self,
        db_session: AsyncSession,
        model_registry: ModelRegistry,
        feature_store: FeatureStore,
        min_accuracy: float = 0.85,
    ):
        """
        Initialize pipeline.
        
        Args:
            db_session: Database session
            model_registry: Model registry instance
            feature_store: Feature store instance
            min_accuracy: Minimum accuracy threshold (default: 0.85)
        """
        self.db_session = db_session
        self.model_registry = model_registry
        self.feature_store = feature_store
        self.min_accuracy = min_accuracy
        
        logger.info("Initialized timeframe model training pipeline")
    
    async def train_all_timeframes(
        self,
        training_data: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """
        Train models for all timeframes.
        
        Args:
            training_data: Dictionary mapping timeframes to their training data
                Format: {
                    "1d": {"features": np.ndarray, "targets": dict},
                    "1w": {"features": np.ndarray, "targets": dict},
                    "1M": {"features": np.ndarray, "targets": dict},
                }
        
        Returns:
            Dictionary with training results for each timeframe
        """
        results = {}
        
        timeframes = get_all_timeframes()
        
        for timeframe in timeframes:
            logger.info(f"=" * 80)
            logger.info(f"Training {timeframe} model...")
            logger.info(f"=" * 80)
            
            if timeframe not in training_data:
                logger.warning(f"No training data for {timeframe}, skipping")
                continue
            
            try:
                # Train model
                training_result = await self._train_timeframe_model(
                    timeframe=timeframe,
                    features=training_data[timeframe]["features"],
                    targets=training_data[timeframe]["targets"],
                )
                
                # Register model
                model_record = await self._register_model(
                    timeframe=timeframe,
                    training_result=training_result,
                )
                
                # Store feature definitions
                await self._store_feature_definitions(timeframe)
                
                results[timeframe] = {
                    "training_result": training_result,
                    "model_record": model_record,
                    "status": "success",
                }
                
                logger.info(
                    f"✓ {timeframe} model trained successfully! "
                    f"Accuracy: {training_result['best_val_accuracy']:.4f}"
                )
                
            except Exception as e:
                logger.error(f"✗ Failed to train {timeframe} model: {e}")
                results[timeframe] = {
                    "status": "failed",
                    "error": str(e),
                }
        
        return results
    
    async def _train_timeframe_model(
        self,
        timeframe: str,
        features: np.ndarray,
        targets: dict[str, np.ndarray],
    ) -> dict[str, Any]:
        """Train a single timeframe model."""
        logger.info(f"Training {timeframe} model with {len(features)} samples...")
        
        # Train model
        training_result = train_timeframe_model(
            timeframe=timeframe,
            features=features,
            targets=targets,
            input_size=40,
            min_accuracy=self.min_accuracy,
            device=None,  # Auto-detect
        )
        
        return training_result
    
    async def _register_model(
        self,
        timeframe: str,
        training_result: dict[str, Any],
    ) -> Any:
        """Register trained model in the model registry."""
        # Generate version based on timeframe and timestamp
        from datetime import datetime, timezone
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        version = f"{timeframe}_{timestamp}"
        
        # Prepare metrics
        metrics = {
            "accuracy": training_result["best_val_accuracy"],
            "val_loss": training_result["best_val_loss"],
            "test_accuracy": training_result["test_metrics"]["accuracy"],
            "test_precision": training_result["test_metrics"]["precision"],
            "test_recall": training_result["test_metrics"]["recall"],
            "test_f1": training_result["test_metrics"]["f1_score"],
        }
        
        # Prepare metadata
        metadata = {
            "timeframe": timeframe,
            "training_date": timestamp,
            "sequence_length": training_result.get("sequence_length"),
            "input_size": 40,
            "architecture": "lstm_transformer_hybrid",
            "training_history": training_result["training_history"],
        }
        
        # Register model
        model_record = await self.model_registry.register_model(
            version=version,
            model_type=f"lstm_transformer_{timeframe}",
            artifact_path=training_result["model_path"],
            metrics=metrics,
            metadata=metadata,
            feature_version="1.0.0",
            status="development",
        )
        
        logger.info(f"Registered model {version} in registry")
        
        return model_record
    
    async def _store_feature_definitions(self, timeframe: str) -> None:
        """Store timeframe-specific feature definitions in feature store."""
        feature_definitions = get_timeframe_feature_definitions(timeframe)
        
        # Register each feature definition
        for feature_name, feature_config in feature_definitions.items():
            # Create a dummy computation function (actual computation is in TimeframeFeatureComputer)
            def compute_fn(df):
                return 0.0  # Placeholder
            
            self.feature_store.register_feature(
                name=f"{timeframe}_{feature_name}",
                computation_fn=compute_fn,
                description=feature_config["description"],
                data_type="float",
            )
        
        logger.info(f"Stored {len(feature_definitions)} feature definitions for {timeframe}")


async def train_all_timeframe_models(
    training_data: dict[str, dict[str, Any]],
    db_url: str,
    redis_url: str,
    min_accuracy: float = 0.85,
) -> dict[str, dict[str, Any]]:
    """
    Train all timeframe-specific models.
    
    Args:
        training_data: Dictionary mapping timeframes to their training data
        db_url: Database connection URL
        redis_url: Redis connection URL
        min_accuracy: Minimum accuracy threshold (default: 0.85)
        
    Returns:
        Dictionary with training results for each timeframe
    """
    # Create database session
    engine = create_async_engine(db_url)
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    
    async with async_session() as session:
        # Initialize services
        cache_service = CacheService(redis_url)
        model_registry = ModelRegistry(
            session=session,
            model_storage_path="models/registry",
        )
        feature_store = FeatureStore(
            session=session,
            cache=cache_service,
            feature_version="1.0.0",
        )
        
        # Create pipeline
        pipeline = TimeframeModelPipeline(
            db_session=session,
            model_registry=model_registry,
            feature_store=feature_store,
            min_accuracy=min_accuracy,
        )
        
        # Train all models
        results = await pipeline.train_all_timeframes(training_data)
        
        return results


def generate_sample_training_data() -> dict[str, dict[str, Any]]:
    """
    Generate sample training data for demonstration.
    
    In production, this would load real historical market data.
    """
    np.random.seed(42)
    
    training_data = {}
    
    # Daily data
    n_samples_daily = 1000
    training_data["1d"] = {
        "features": np.random.randn(n_samples_daily, 60, 40),  # (samples, seq_len, features)
        "targets": {
            "direction": np.random.randint(0, 3, n_samples_daily),  # BUY=2, HOLD=1, SELL=0
            "confidence": np.random.rand(n_samples_daily),
            "entry_price": np.random.randn(n_samples_daily) * 100 + 1000,
            "tp1": np.random.randn(n_samples_daily) * 100 + 1050,
            "tp2": np.random.randn(n_samples_daily) * 100 + 1100,
            "tp3": np.random.randn(n_samples_daily) * 100 + 1150,
            "stop_loss": np.random.randn(n_samples_daily) * 100 + 950,
            "volatility": np.random.rand(n_samples_daily) * 0.5,
        },
    }
    
    # Weekly data
    n_samples_weekly = 500
    training_data["1w"] = {
        "features": np.random.randn(n_samples_weekly, 52, 40),  # (samples, seq_len, features)
        "targets": {
            "direction": np.random.randint(0, 3, n_samples_weekly),
            "confidence": np.random.rand(n_samples_weekly),
            "entry_price": np.random.randn(n_samples_weekly) * 100 + 1000,
            "tp1": np.random.randn(n_samples_weekly) * 100 + 1050,
            "tp2": np.random.randn(n_samples_weekly) * 100 + 1100,
            "tp3": np.random.randn(n_samples_weekly) * 100 + 1150,
            "stop_loss": np.random.randn(n_samples_weekly) * 100 + 950,
            "volatility": np.random.rand(n_samples_weekly) * 0.5,
        },
    }
    
    # Monthly data
    n_samples_monthly = 200
    training_data["1M"] = {
        "features": np.random.randn(n_samples_monthly, 24, 40),  # (samples, seq_len, features)
        "targets": {
            "direction": np.random.randint(0, 3, n_samples_monthly),
            "confidence": np.random.rand(n_samples_monthly),
            "entry_price": np.random.randn(n_samples_monthly) * 100 + 1000,
            "tp1": np.random.randn(n_samples_monthly) * 100 + 1050,
            "tp2": np.random.randn(n_samples_monthly) * 100 + 1100,
            "tp3": np.random.randn(n_samples_monthly) * 100 + 1150,
            "stop_loss": np.random.randn(n_samples_monthly) * 100 + 950,
            "volatility": np.random.rand(n_samples_monthly) * 0.5,
        },
    }
    
    return training_data


if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    
    # Generate sample data
    logger.info("Generating sample training data...")
    training_data = generate_sample_training_data()
    
    # Train models
    logger.info("Starting timeframe model training pipeline...")
    
    results = asyncio.run(
        train_all_timeframe_models(
            training_data=training_data,
            db_url="postgresql+asyncpg://user:pass@localhost/cortex",
            redis_url="redis://localhost:6379",
            min_accuracy=0.85,
        )
    )
    
    # Print results
    logger.info("\n" + "=" * 80)
    logger.info("TRAINING RESULTS")
    logger.info("=" * 80)
    
    for timeframe, result in results.items():
        if result["status"] == "success":
            logger.info(
                f"\n{timeframe}: ✓ SUCCESS\n"
                f"  Validation Accuracy: {result['training_result']['best_val_accuracy']:.4f}\n"
                f"  Test Accuracy: {result['training_result']['test_metrics']['accuracy']:.4f}\n"
                f"  Model Version: {result['model_record'].version}"
            )
        else:
            logger.error(
                f"\n{timeframe}: ✗ FAILED\n"
                f"  Error: {result['error']}"
            )
