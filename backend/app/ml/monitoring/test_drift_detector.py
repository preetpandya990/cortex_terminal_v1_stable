"""
Unit tests for drift detector.

Tests drift detection functionality for features and predictions.
"""

import pytest
import numpy as np
from unittest.mock import Mock, AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from app.ml.monitoring.drift_detector import DriftDetector
from app.models.ml_data import MLDriftMetric, MLPrediction, MLModelMetadata


class TestDriftDetector:
    """Test suite for DriftDetector class."""

    @pytest.fixture
    def mock_db(self):
        """Create mock database session."""
        db = AsyncMock()
        db.add = Mock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.execute = AsyncMock()
        return db

    @pytest.fixture
    def drift_detector(self, mock_db):
        """Create DriftDetector instance."""
        return DriftDetector(
            model_id="test_model",
            db_session=mock_db,
            drift_threshold=2.0,
            lookback_days=7,
        )

    @pytest.mark.asyncio
    async def test_compute_feature_drift_no_drift(self, drift_detector):
        """Test feature drift detection when distributions are similar."""
        np.random.seed(42)
        
        # Create similar distributions
        current_features = {
            "feature_1": np.random.normal(0, 1, 100),
            "feature_2": np.random.normal(0, 1, 100),
        }
        
        training_stats = {
            "feature_1": {"mean": 0.0, "std": 1.0, "min": -3.0, "max": 3.0},
            "feature_2": {"mean": 0.0, "std": 1.0, "min": -3.0, "max": 3.0},
        }

        drift_scores = await drift_detector.compute_feature_drift(
            current_features,
            training_stats
        )

        assert len(drift_scores) == 2
        assert "feature_1" in drift_scores
        assert "feature_2" in drift_scores
        # Similar distributions should have low KS statistic
        assert drift_scores["feature_1"] < 0.3
        assert drift_scores["feature_2"] < 0.3

    @pytest.mark.asyncio
    async def test_compute_feature_drift_with_drift(self, drift_detector):
        """Test feature drift detection when distributions differ significantly."""
        np.random.seed(42)
        
        # Create different distributions
        current_features = {
            "feature_1": np.random.normal(5, 2, 100),  # Mean=5, std=2
            "feature_2": np.random.normal(10, 3, 100),  # Mean=10, std=3
        }
        
        training_stats = {
            "feature_1": {"mean": 0.0, "std": 1.0, "min": -3.0, "max": 3.0},
            "feature_2": {"mean": 0.0, "std": 1.0, "min": -3.0, "max": 3.0},
        }

        drift_scores = await drift_detector.compute_feature_drift(
            current_features,
            training_stats
        )

        assert len(drift_scores) == 2
        # Different distributions should have high KS statistic
        assert drift_scores["feature_1"] > 0.3
        assert drift_scores["feature_2"] > 0.3

    @pytest.mark.asyncio
    async def test_compute_prediction_drift_no_drift(self, drift_detector):
        """Test prediction drift when distributions are similar."""
        np.random.seed(42)
        
        current_predictions = np.random.normal(0, 1, 100)
        training_stats = {"mean": 0.0, "std": 1.0, "min": -3.0, "max": 3.0}

        drift_score, z_score, ks_stat, p_value = await drift_detector.compute_prediction_drift(
            current_predictions,
            training_stats
        )

        assert z_score < 2.0  # Below threshold
        assert ks_stat < 0.3  # Low KS statistic
        assert drift_score < 2.0  # Below threshold

    @pytest.mark.asyncio
    async def test_compute_prediction_drift_with_drift(self, drift_detector):
        """Test prediction drift when distributions differ significantly."""
        np.random.seed(42)
        
        current_predictions = np.random.normal(10, 1, 100)  # Mean=10
        training_stats = {"mean": 0.0, "std": 1.0, "min": -3.0, "max": 3.0}

        drift_score, z_score, ks_stat, p_value = await drift_detector.compute_prediction_drift(
            current_predictions,
            training_stats
        )

        assert z_score > 2.0  # Above threshold
        assert drift_score > 2.0  # Above threshold

    @pytest.mark.asyncio
    async def test_detect_drift_insufficient_data(self, drift_detector, mock_db):
        """Test detect_drift with insufficient predictions."""
        # Mock model metadata
        model_metadata = MLModelMetadata(
            model_id="test_model",
            model_name="Test Model",
            model_version="1.0",
            training_feature_stats={"feature_1": {"mean": 0, "std": 1, "min": -3, "max": 3}},
            training_prediction_stats={"mean": 0, "std": 1, "min": -3, "max": 3},
        )
        
        # Mock database queries
        mock_result = AsyncMock()
        mock_result.scalar_one_or_none.return_value = model_metadata
        mock_result.scalars.return_value.all.return_value = []  # No predictions
        mock_db.execute.return_value = mock_result

        drift_metric = await drift_detector.detect_drift()

        assert drift_metric is None

    @pytest.mark.asyncio
    async def test_detect_drift_no_drift(self, drift_detector, mock_db):
        """Test detect_drift when no drift is present."""
        np.random.seed(42)
        
        # Mock model metadata
        model_metadata = MLModelMetadata(
            model_id="test_model",
            model_name="Test Model",
            model_version="1.0",
            training_feature_stats={
                "feature_1": {"mean": 0, "std": 1, "min": -3, "max": 3},
                "feature_2": {"mean": 0, "std": 1, "min": -3, "max": 3},
            },
            training_prediction_stats={"mean": 0, "std": 1, "min": -3, "max": 3},
        )
        
        # Create predictions with similar distribution
        predictions = []
        for i in range(50):
            pred = MLPrediction(
                model_id="test_model",
                timestamp=datetime.utcnow() - timedelta(days=i % 7),
                features={
                    "feature_1": float(np.random.normal(0, 1)),
                    "feature_2": float(np.random.normal(0, 1)),
                },
                prediction=float(np.random.normal(0, 1)),
            )
            predictions.append(pred)
        
        # Mock database queries
        mock_result_metadata = AsyncMock()
        mock_result_metadata.scalar_one_or_none.return_value = model_metadata
        
        mock_result_predictions = AsyncMock()
        mock_result_predictions.scalars.return_value.all.return_value = predictions
        
        mock_db.execute.side_effect = [mock_result_metadata, mock_result_predictions]

        drift_metric = await drift_detector.detect_drift()

        assert drift_metric is not None
        assert drift_metric.drift_detected is False
        assert len(drift_metric.alerts) == 0

    @pytest.mark.asyncio
    async def test_detect_drift_with_drift(self, drift_detector, mock_db):
        """Test detect_drift when drift is present."""
        np.random.seed(42)
        
        # Mock model metadata
        model_metadata = MLModelMetadata(
            model_id="test_model",
            model_name="Test Model",
            model_version="1.0",
            training_feature_stats={
                "feature_1": {"mean": 0, "std": 1, "min": -3, "max": 3},
                "feature_2": {"mean": 0, "std": 1, "min": -3, "max": 3},
            },
            training_prediction_stats={"mean": 0, "std": 1, "min": -3, "max": 3},
        )
        
        # Create predictions with drifted distribution
        predictions = []
        for i in range(50):
            pred = MLPrediction(
                model_id="test_model",
                timestamp=datetime.utcnow() - timedelta(days=i % 7),
                features={
                    "feature_1": float(np.random.normal(5, 2)),  # Drifted
                    "feature_2": float(np.random.normal(0, 1)),
                },
                prediction=float(np.random.normal(10, 1)),  # Drifted
            )
            predictions.append(pred)
        
        # Mock database queries
        mock_result_metadata = AsyncMock()
        mock_result_metadata.scalar_one_or_none.return_value = model_metadata
        
        mock_result_predictions = AsyncMock()
        mock_result_predictions.scalars.return_value.all.return_value = predictions
        
        mock_db.execute.side_effect = [mock_result_metadata, mock_result_predictions]

        drift_metric = await drift_detector.detect_drift()

        assert drift_metric is not None
        assert drift_metric.drift_detected is True
        assert drift_metric.drift_type in ["feature", "prediction", "both"]
        assert len(drift_metric.alerts) > 0
        assert drift_metric.prediction_z_score > 2.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
