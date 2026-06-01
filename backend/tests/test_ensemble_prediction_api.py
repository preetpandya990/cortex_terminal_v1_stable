"""
Unit tests for ensemble prediction API endpoint.

Tests cover:
- POST /api/v1/ml/predict/ensemble endpoint
- Request validation
- Ensemble prediction logic
- Redis caching
- Cache invalidation
- Error handling
- Graceful degradation

Requirements: 4.1, 6.1, 6.4
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ml_data import MLModelMetadata
from app.schemas.ml_predictions import (
    EnsemblePredictionRequest,
    EnsemblePredictionResponse,
)


@pytest.mark.asyncio
class TestEnsemblePredictionAPI:
    """Test suite for ensemble prediction API endpoint."""

    async def test_ensemble_prediction_success(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession,
        mock_cache: MagicMock,
    ):
        """
        Test successful ensemble prediction with all timeframes.
        
        Requirements: 4.1, 6.1
        """
        # Setup: Create active model in database
        model = MLModelMetadata(
            model_id="test-model-v1",
            model_name="test_model",
            model_version="1.0.0",
            is_active=True,
            onnx_path="models/test_model.onnx",
        )
        db_session.add(model)
        await db_session.commit()

        # Mock model file existence
        with patch("os.path.exists", return_value=True):
            # Mock prediction engine and ensemble
            with patch("app.api.v1.ml_predictions.get_prediction_engine") as mock_engine:
                with patch("app.api.v1.ml_predictions.MultiTimeframeEnsemble") as mock_ensemble_class:
                    # Setup mock prediction engine
                    mock_pred_engine = MagicMock()
                    mock_pred_engine.predict = AsyncMock(return_value={
                        "direction": 2,
                        "direction_label": "BUY",
                        "confidence": 0.85,
                        "entry_price": 150.0,
                        "tp1": 155.0,
                        "tp2": 160.0,
                        "tp3": 165.0,
                        "stop_loss": 145.0,
                        "volatility": 0.02,
                    })
                    mock_engine.return_value = mock_pred_engine

                    # Setup mock ensemble
                    from app.ml.ensemble import TimeframePrediction, EnsemblePrediction
                    
                    mock_ensemble = MagicMock()
                    mock_ensemble.timeframe_weights = {
                        "daily": 0.5,
                        "weekly": 0.3,
                        "monthly": 0.2,
                    }
                    mock_ensemble.predict.return_value = EnsemblePrediction(
                        direction="buy",
                        confidence=0.82,
                        timeframe_predictions={
                            "daily": TimeframePrediction(
                                timeframe="daily",
                                direction="buy",
                                confidence=0.85,
                            ),
                            "weekly": TimeframePrediction(
                                timeframe="weekly",
                                direction="buy",
                                confidence=0.80,
                            ),
                            "monthly": TimeframePrediction(
                                timeframe="monthly",
                                direction="hold",
                                confidence=0.75,
                            ),
                        },
                        conflict_resolved=True,
                        conflict_resolution_method="highest_confidence",
                    )
                    mock_ensemble_class.return_value = mock_ensemble

                    # Make request
                    response = await async_client.post(
                        "/api/v1/ml/predict/ensemble",
                        json={
                            "symbol": "AAPL",
                            "timeframes": ["daily", "weekly", "monthly"],
                            "user_id": "test-user",
                        },
                    )

        # Assertions
        assert response.status_code == 200
        data = response.json()
        
        assert data["symbol"] == "AAPL"
        assert data["direction"] == "buy"
        assert data["confidence"] == 0.82
        assert "daily" in data["timeframe_predictions"]
        assert "weekly" in data["timeframe_predictions"]
        assert "monthly" in data["timeframe_predictions"]
        assert data["conflict_resolved"] is True
        assert data["conflict_resolution_method"] == "highest_confidence"
        assert "confidence_breakdown" in data

    async def test_ensemble_prediction_cache_hit(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession,
        mock_cache: MagicMock,
    ):
        """
        Test ensemble prediction returns cached result.
        
        Requirements: 6.4
        """
        # Setup: Create active model
        model = MLModelMetadata(
            model_id="test-model-v1",
            model_name="test_model",
            model_version="1.0.0",
            is_active=True,
            onnx_path="models/test_model.onnx",
        )
        db_session.add(model)
        await db_session.commit()

        # Mock cache hit
        cached_response = {
            "symbol": "AAPL",
            "direction": "buy",
            "confidence": 0.85,
            "timeframe_predictions": {
                "daily": {
                    "timeframe": "daily",
                    "direction": "buy",
                    "confidence": 0.90,
                    "entry_price": 150.0,
                    "tp1": 155.0,
                    "tp2": 160.0,
                    "tp3": 165.0,
                    "stop_loss": 145.0,
                    "volatility": 0.02,
                    "metadata": None,
                }
            },
            "confidence_breakdown": {"daily": 0.45},
            "conflict_resolved": False,
            "conflict_resolution_method": None,
            "metadata": {"timestamp": "2024-01-01T00:00:00Z"},
            "warnings": None,
        }
        mock_cache.get = AsyncMock(return_value=cached_response)

        with patch("os.path.exists", return_value=True):
            response = await async_client.post(
                "/api/v1/ml/predict/ensemble",
                json={
                    "symbol": "AAPL",
                    "timeframes": ["daily"],
                    "user_id": "test-user",
                },
            )

        # Assertions
        assert response.status_code == 200
        data = response.json()
        assert data["symbol"] == "AAPL"
        assert data["direction"] == "buy"
        
        # Verify cache was checked
        mock_cache.get.assert_called_once()

    async def test_ensemble_prediction_invalid_timeframe(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession,
    ):
        """
        Test ensemble prediction rejects invalid timeframes.
        
        Requirements: 6.1
        """
        # Setup: Create active model
        model = MLModelMetadata(
            model_id="test-model-v1",
            model_name="test_model",
            model_version="1.0.0",
            is_active=True,
            onnx_path="models/test_model.onnx",
        )
        db_session.add(model)
        await db_session.commit()

        with patch("os.path.exists", return_value=True):
            response = await async_client.post(
                "/api/v1/ml/predict/ensemble",
                json={
                    "symbol": "AAPL",
                    "timeframes": ["daily", "invalid_timeframe"],
                    "user_id": "test-user",
                },
            )

        # Assertions
        assert response.status_code == 400
        assert "Invalid timeframes" in response.json()["detail"]

    async def test_ensemble_prediction_model_unavailable(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession,
    ):
        """
        Test ensemble prediction returns 503 when model unavailable.
        
        Requirements: 19.1, 26.2
        """
        # No active model in database
        response = await async_client.post(
            "/api/v1/ml/predict/ensemble",
            json={
                "symbol": "AAPL",
                "timeframes": ["daily"],
                "user_id": "test-user",
            },
        )

        # Assertions
        assert response.status_code == 503
        assert "ML model unavailable" in response.json()["detail"]

    async def test_ensemble_prediction_partial_timeframes(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession,
        mock_cache: MagicMock,
    ):
        """
        Test ensemble prediction with partial timeframe failures.
        
        Requirements: 4.1, 19.2
        """
        # Setup: Create active model
        model = MLModelMetadata(
            model_id="test-model-v1",
            model_name="test_model",
            model_version="1.0.0",
            is_active=True,
            onnx_path="models/test_model.onnx",
        )
        db_session.add(model)
        await db_session.commit()

        with patch("os.path.exists", return_value=True):
            with patch("app.api.v1.ml_predictions.get_prediction_engine") as mock_engine:
                with patch("app.api.v1.ml_predictions.MultiTimeframeEnsemble") as mock_ensemble_class:
                    # Setup mock that fails for weekly but succeeds for daily
                    mock_pred_engine = MagicMock()
                    
                    call_count = 0
                    async def mock_predict(*args, **kwargs):
                        nonlocal call_count
                        call_count += 1
                        if call_count == 2:  # Second call (weekly) fails
                            raise Exception("Weekly prediction failed")
                        return {
                            "direction": 2,
                            "direction_label": "BUY",
                            "confidence": 0.85,
                            "entry_price": 150.0,
                            "tp1": 155.0,
                            "tp2": 160.0,
                            "tp3": 165.0,
                            "stop_loss": 145.0,
                            "volatility": 0.02,
                        }
                    
                    mock_pred_engine.predict = mock_predict
                    mock_engine.return_value = mock_pred_engine

                    # Setup mock ensemble
                    from app.ml.ensemble import TimeframePrediction, EnsemblePrediction
                    
                    mock_ensemble = MagicMock()
                    mock_ensemble.timeframe_weights = {"daily": 0.5, "weekly": 0.3}
                    mock_ensemble.predict.return_value = EnsemblePrediction(
                        direction="buy",
                        confidence=0.85,
                        timeframe_predictions={
                            "daily": TimeframePrediction(
                                timeframe="daily",
                                direction="buy",
                                confidence=0.85,
                            ),
                        },
                        conflict_resolved=False,
                    )
                    mock_ensemble_class.return_value = mock_ensemble

                    response = await async_client.post(
                        "/api/v1/ml/predict/ensemble",
                        json={
                            "symbol": "AAPL",
                            "timeframes": ["daily", "weekly"],
                            "user_id": "test-user",
                        },
                    )

        # Assertions
        assert response.status_code == 200
        data = response.json()
        assert data["symbol"] == "AAPL"
        assert "warnings" in data
        assert any("weekly" in w.lower() for w in data["warnings"])

    async def test_ensemble_prediction_caching(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession,
        mock_cache: MagicMock,
    ):
        """
        Test ensemble prediction caches results with 5-minute TTL.
        
        Requirements: 6.4
        """
        # Setup: Create active model
        model = MLModelMetadata(
            model_id="test-model-v1",
            model_name="test_model",
            model_version="1.0.0",
            is_active=True,
            onnx_path="models/test_model.onnx",
        )
        db_session.add(model)
        await db_session.commit()

        # Mock cache miss
        mock_cache.get = AsyncMock(return_value=None)
        mock_cache.set = AsyncMock()

        with patch("os.path.exists", return_value=True):
            with patch("app.api.v1.ml_predictions.get_prediction_engine") as mock_engine:
                with patch("app.api.v1.ml_predictions.MultiTimeframeEnsemble") as mock_ensemble_class:
                    # Setup mocks
                    mock_pred_engine = MagicMock()
                    mock_pred_engine.predict = AsyncMock(return_value={
                        "direction": 2,
                        "direction_label": "BUY",
                        "confidence": 0.85,
                        "entry_price": 150.0,
                        "tp1": 155.0,
                        "tp2": 160.0,
                        "tp3": 165.0,
                        "stop_loss": 145.0,
                        "volatility": 0.02,
                    })
                    mock_engine.return_value = mock_pred_engine

                    from app.ml.ensemble import TimeframePrediction, EnsemblePrediction
                    
                    mock_ensemble = MagicMock()
                    mock_ensemble.timeframe_weights = {"daily": 0.5}
                    mock_ensemble.predict.return_value = EnsemblePrediction(
                        direction="buy",
                        confidence=0.85,
                        timeframe_predictions={
                            "daily": TimeframePrediction(
                                timeframe="daily",
                                direction="buy",
                                confidence=0.85,
                            ),
                        },
                        conflict_resolved=False,
                    )
                    mock_ensemble_class.return_value = mock_ensemble

                    response = await async_client.post(
                        "/api/v1/ml/predict/ensemble",
                        json={
                            "symbol": "AAPL",
                            "timeframes": ["daily"],
                            "user_id": "test-user",
                        },
                    )

        # Assertions
        assert response.status_code == 200
        
        # Verify cache was set with 5-minute TTL
        mock_cache.set.assert_called_once()
        call_args = mock_cache.set.call_args
        assert call_args[1]["ttl"] == 300  # 5 minutes

    async def test_cache_invalidation(
        self,
        mock_cache: MagicMock,
    ):
        """
        Test cache invalidation for ensemble predictions.
        
        Requirements: 6.4
        """
        from app.api.v1.ml_predictions import invalidate_ensemble_cache

        # Mock cache delete_pattern
        mock_cache.delete_pattern = AsyncMock(return_value=3)

        # Call invalidation
        deleted = await invalidate_ensemble_cache("AAPL", mock_cache)

        # Assertions
        assert deleted == 3
        mock_cache.delete_pattern.assert_called_once_with("ml:ensemble:AAPL:*")

    async def test_ensemble_prediction_empty_timeframes(
        self,
        async_client: AsyncClient,
    ):
        """
        Test ensemble prediction rejects empty timeframes list.
        
        Requirements: 6.1
        """
        response = await async_client.post(
            "/api/v1/ml/predict/ensemble",
            json={
                "symbol": "AAPL",
                "timeframes": [],
                "user_id": "test-user",
            },
        )

        # Assertions
        assert response.status_code == 422  # Validation error

    async def test_ensemble_prediction_confidence_breakdown(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession,
        mock_cache: MagicMock,
    ):
        """
        Test ensemble prediction includes confidence breakdown per timeframe.
        
        Requirements: 6.1
        """
        # Setup: Create active model
        model = MLModelMetadata(
            model_id="test-model-v1",
            model_name="test_model",
            model_version="1.0.0",
            is_active=True,
            onnx_path="models/test_model.onnx",
        )
        db_session.add(model)
        await db_session.commit()

        with patch("os.path.exists", return_value=True):
            with patch("app.api.v1.ml_predictions.get_prediction_engine") as mock_engine:
                with patch("app.api.v1.ml_predictions.MultiTimeframeEnsemble") as mock_ensemble_class:
                    # Setup mocks
                    mock_pred_engine = MagicMock()
                    mock_pred_engine.predict = AsyncMock(return_value={
                        "direction": 2,
                        "direction_label": "BUY",
                        "confidence": 0.80,
                        "entry_price": 150.0,
                        "tp1": 155.0,
                        "tp2": 160.0,
                        "tp3": 165.0,
                        "stop_loss": 145.0,
                        "volatility": 0.02,
                    })
                    mock_engine.return_value = mock_pred_engine

                    from app.ml.ensemble import TimeframePrediction, EnsemblePrediction
                    
                    mock_ensemble = MagicMock()
                    mock_ensemble.timeframe_weights = {
                        "daily": 0.5,
                        "weekly": 0.3,
                        "monthly": 0.2,
                    }
                    mock_ensemble.predict.return_value = EnsemblePrediction(
                        direction="buy",
                        confidence=0.80,
                        timeframe_predictions={
                            "daily": TimeframePrediction(
                                timeframe="daily",
                                direction="buy",
                                confidence=0.85,
                            ),
                            "weekly": TimeframePrediction(
                                timeframe="weekly",
                                direction="buy",
                                confidence=0.75,
                            ),
                            "monthly": TimeframePrediction(
                                timeframe="monthly",
                                direction="hold",
                                confidence=0.70,
                            ),
                        },
                        conflict_resolved=True,
                        conflict_resolution_method="highest_confidence",
                    )
                    mock_ensemble_class.return_value = mock_ensemble

                    response = await async_client.post(
                        "/api/v1/ml/predict/ensemble",
                        json={
                            "symbol": "AAPL",
                            "timeframes": ["daily", "weekly", "monthly"],
                            "user_id": "test-user",
                        },
                    )

        # Assertions
        assert response.status_code == 200
        data = response.json()
        
        # Verify confidence breakdown
        assert "confidence_breakdown" in data
        breakdown = data["confidence_breakdown"]
        assert "daily" in breakdown
        assert "weekly" in breakdown
        assert "monthly" in breakdown
        
        # Verify breakdown values are weighted contributions
        assert isinstance(breakdown["daily"], float)
        assert isinstance(breakdown["weekly"], float)
        assert isinstance(breakdown["monthly"], float)
