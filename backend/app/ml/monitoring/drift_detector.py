"""
Cortex AI — ML Drift Detection
================================
Monitors feature and prediction drift to detect model degradation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
# scipy not available, using custom KS test
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ml_data import MLDriftMetric, MLPrediction, MLModelMetadata


logger = logging.getLogger(__name__)


def kolmogorov_smirnov_test(data1: np.ndarray, data2: np.ndarray):
    """Compute KS test statistic and p-value without scipy."""
    n1, n2 = len(data1), len(data2)
    data1_sorted, data2_sorted = np.sort(data1), np.sort(data2)
    all_values = np.sort(np.concatenate([data1_sorted, data2_sorted]))
    cdf1 = np.searchsorted(data1_sorted, all_values, side="right") / n1
    cdf2 = np.searchsorted(data2_sorted, all_values, side="right") / n2
    ks_statistic = np.max(np.abs(cdf1 - cdf2))
    en = np.sqrt(n1 * n2 / (n1 + n2))
    p_value = min(1.0, max(0.0, 2 * np.exp(-2 * (en * ks_statistic) ** 2)))
    return float(ks_statistic), float(p_value)


class DriftDetector:
    """
    Detects feature and prediction drift for ML models.
    
    Monitors distribution changes between training data and production data
    to identify when model retraining may be needed.
    """
    
    def __init__(
        self,
        model_id: str,
        db_session: AsyncSession,
        drift_threshold: float = 2.0,  # Standard deviations
        lookback_days: int = 7,
    ):
        """
        Initialize drift detector.
        
        Args:
            model_id: Unique identifier for the model
            db_session: Database session for queries
            drift_threshold: Number of standard deviations for drift alert
            lookback_days: Days of recent data to analyze
        """
        self.model_id = model_id
        self.db_session = db_session
        self.drift_threshold = drift_threshold
        self.lookback_days = lookback_days
        
    async def compute_feature_drift(
        self,
        current_features: Dict[str, np.ndarray],
        training_stats: Dict[str, Dict[str, float]],
    ) -> Dict[str, float]:
        """
        Compute drift scores for each feature.
        
        Compares current feature distributions against training statistics
        using Kolmogorov-Smirnov test.
        
        Args:
            current_features: Dict of feature_name -> array of current values
            training_stats: Dict of feature_name -> {mean, std, min, max}
            
        Returns:
            Dict of feature_name -> drift_score (KS statistic)
        """
        drift_scores = {}
        
        for feature_name, current_values in current_features.items():
            if feature_name not in training_stats:
                logger.warning(f"Feature {feature_name} not in training stats")
                continue
                
            train_stats = training_stats[feature_name]
            
            # Generate reference distribution from training stats
            # Assume normal distribution based on mean/std
            reference_dist = np.random.normal(
                loc=train_stats["mean"],
                scale=train_stats["std"],
                size=len(current_values)
            )
            
            # Kolmogorov-Smirnov test
            ks_statistic, p_value = kolmogorov_smirnov_test(current_values, reference_dist)
            
            drift_scores[feature_name] = float(ks_statistic)
            
            logger.debug(
                f"Feature {feature_name}: KS={ks_statistic:.4f}, p={p_value:.4f}"
            )
            
        return drift_scores
    
    async def compute_prediction_drift(
        self,
        current_predictions: np.ndarray,
        training_stats: Dict[str, float],
    ) -> Tuple[float, float, float, float]:
        """
        Compute prediction drift metrics.
        
        Analyzes prediction distribution changes over time.
        
        Args:
            current_predictions: Array of recent predictions
            training_stats: Dict with {mean, std, min, max} from training
            
        Returns:
            Tuple of (drift_score, z_score, ks_statistic, p_value)
        """
        current_mean = float(np.mean(current_predictions))
        current_std = float(np.std(current_predictions))
        
        train_mean = training_stats["mean"]
        train_std = training_stats["std"]
        
        # Z-score for mean shift
        z_score = abs(current_mean - train_mean) / train_std if train_std > 0 else 0.0
        
        # Generate reference distribution
        reference_dist = np.random.normal(
            loc=train_mean,
            scale=train_std,
            size=len(current_predictions)
        )
        
        # Kolmogorov-Smirnov test
        ks_statistic, p_value = kolmogorov_smirnov_test(current_predictions, reference_dist)
        
        # Overall drift score (combination of z-score and KS statistic)
        drift_score = max(z_score, ks_statistic * 10)  # Scale KS to similar range
        
        logger.info(
            f"Prediction drift: mean={current_mean:.4f} (train={train_mean:.4f}), "
            f"z_score={z_score:.4f}, KS={ks_statistic:.4f}"
        )
        
        return drift_score, z_score, ks_statistic, p_value
    
    async def detect_drift(self) -> Optional[MLDriftMetric]:
        """
        Run drift detection and create drift metric record.
        
        Fetches recent predictions, compares against training baseline,
        and triggers alerts if drift exceeds threshold.
        
        Returns:
            MLDriftMetric record if drift detected, None otherwise
        """
        logger.info(f"Running drift detection for model {self.model_id}")
        
        # Get model metadata with training statistics
        model_metadata = await self._get_model_metadata()
        if not model_metadata:
            logger.error(f"Model metadata not found for {self.model_id}")
            return None
            
        # Get recent predictions
        cutoff_date = datetime.utcnow() - timedelta(days=self.lookback_days)
        recent_predictions = await self._get_recent_predictions(cutoff_date)
        
        if len(recent_predictions) < 10:
            logger.warning(
                f"Insufficient predictions ({len(recent_predictions)}) for drift detection"
            )
            return None
        
        # Extract features and predictions
        features_by_name = self._extract_features(recent_predictions)
        prediction_values = np.array([p.prediction for p in recent_predictions])
        
        # Compute feature drift
        feature_drift_scores = await self.compute_feature_drift(
            features_by_name,
            model_metadata.training_feature_stats or {}
        )
        
        # Compute prediction drift
        pred_drift_score, z_score, ks_stat, p_value = await self.compute_prediction_drift(
            prediction_values,
            model_metadata.training_prediction_stats or {}
        )
        
        # Identify drifted features (above threshold)
        drifted_features = [
            name for name, score in feature_drift_scores.items()
            if score > (self.drift_threshold / 10)  # KS threshold is smaller
        ]
        
        # Determine if drift detected
        drift_detected = (
            z_score > self.drift_threshold or
            len(drifted_features) > 0
        )
        
        # Determine drift type
        drift_type = None
        if drift_detected:
            if z_score > self.drift_threshold and len(drifted_features) > 0:
                drift_type = "both"
            elif z_score > self.drift_threshold:
                drift_type = "prediction"
            else:
                drift_type = "feature"
        
        # Generate alerts
        alerts = []
        if z_score > self.drift_threshold:
            alerts.append(
                f"Prediction drift detected: z-score={z_score:.2f} "
                f"(threshold={self.drift_threshold})"
            )
        if drifted_features:
            alerts.append(
                f"Feature drift detected in {len(drifted_features)} features: "
                f"{', '.join(drifted_features[:5])}"
            )
        
        # Create drift metric record
        drift_metric = MLDriftMetric(
            model_id=self.model_id,
            timestamp=datetime.utcnow(),
            drift_detected=drift_detected,
            drift_type=drift_type,
            feature_drift_scores=feature_drift_scores,
            drifted_features=drifted_features,
            prediction_drift_score=pred_drift_score,
            prediction_z_score=z_score,
            prediction_ks_statistic=ks_stat,
            prediction_p_value=p_value,
            current_mean=float(np.mean(prediction_values)),
            current_std=float(np.std(prediction_values)),
            historical_mean=model_metadata.training_prediction_stats.get("mean"),
            historical_std=model_metadata.training_prediction_stats.get("std"),
            alerts=alerts,
            alert_sent=False,
            sample_count=len(recent_predictions),
        )
        
        # Save to database
        self.db_session.add(drift_metric)
        await self.db_session.commit()
        await self.db_session.refresh(drift_metric)
        
        logger.info(
            f"Drift detection complete: drift_detected={drift_detected}, "
            f"type={drift_type}, alerts={len(alerts)}"
        )
        
        return drift_metric
    
    async def _get_model_metadata(self) -> Optional[MLModelMetadata]:
        """Fetch model metadata from database."""
        result = await self.db_session.execute(
            select(MLModelMetadata)
            .where(MLModelMetadata.model_id == self.model_id)
            .where(MLModelMetadata.is_active == True)
        )
        return result.scalar_one_or_none()
    
    async def _get_recent_predictions(
        self,
        cutoff_date: datetime
    ) -> List[MLPrediction]:
        """Fetch recent predictions from database."""
        result = await self.db_session.execute(
            select(MLPrediction)
            .where(MLPrediction.model_id == self.model_id)
            .where(MLPrediction.timestamp >= cutoff_date)
            .order_by(MLPrediction.timestamp.desc())
            .limit(10000)  # Reasonable limit
        )
        return list(result.scalars().all())
    
    def _extract_features(
        self,
        predictions: List[MLPrediction]
    ) -> Dict[str, np.ndarray]:
        """Extract features from prediction records into arrays."""
        features_by_name = {}
        
        for pred in predictions:
            if not pred.features:
                continue
                
            for feature_name, value in pred.features.items():
                if feature_name not in features_by_name:
                    features_by_name[feature_name] = []
                features_by_name[feature_name].append(float(value))
        
        # Convert lists to numpy arrays
        return {
            name: np.array(values)
            for name, values in features_by_name.items()
        }
