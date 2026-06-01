"""Integration test: Ensemble prediction workflow."""
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.ensemble.multi_timeframe_ensemble import MultiTimeframeEnsemble
from app.ml.inference.prediction_engine import PredictionEngine
from app.ml.model_registry import ModelRegistry


@pytest.mark.asyncio
async def test_ensemble_prediction_integration(db_session: AsyncSession, mock_redis: AsyncMock):
    """
    Integration Test 36.3: Ensemble prediction workflow
    
    Flow: Request ensemble → Get predictions for all timeframes → Combine
    
    Validates:
    - Weighted average confidence calculation
    - Conflict resolution (majority vote)
    - All timeframes included (1m, 5m, 15m, 1h, 4h, 1d)
    - Requirements: 17.1, 17.2, 17.3
    """
    # Setup: Register models for multiple timeframes
    registry = ModelRegistry(db_session, storage_path="/tmp/test_ensemble_models")
    
    timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]
    models = {}
    
    for tf in timeframes:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as f:
            f.write(f"mock_model_{tf}".encode())
            model_path = f.name
        
        model = await registry.register_model(
            name=f"ensemble_model_{tf}",
            version="1.0.0",
            artifact_path=model_path,
            metadata={"timeframe": tf, "accuracy": 0.90}
        )
        models[tf] = model
    
    # Setup: Mock predictions for each timeframe
    mock_predictions = {
        "1m": {
            "direction": "BUY",
            "confidence": 0.75,
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit_1": 102.0,
            "take_profit_2": 104.0,
            "take_profit_3": 106.0,
        },
        "5m": {
            "direction": "BUY",
            "confidence": 0.80,
            "entry_price": 100.5,
            "stop_loss": 98.5,
            "take_profit_1": 102.5,
            "take_profit_2": 104.5,
            "take_profit_3": 106.5,
        },
        "15m": {
            "direction": "BUY",
            "confidence": 0.85,
            "entry_price": 101.0,
            "stop_loss": 99.0,
            "take_profit_1": 103.0,
            "take_profit_2": 105.0,
            "take_profit_3": 107.0,
        },
        "1h": {
            "direction": "SELL",  # Conflict
            "confidence": 0.70,
            "entry_price": 100.0,
            "stop_loss": 102.0,
            "take_profit_1": 98.0,
            "take_profit_2": 96.0,
            "take_profit_3": 94.0,
        },
        "4h": {
            "direction": "BUY",
            "confidence": 0.90,
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit_1": 102.0,
            "take_profit_2": 104.0,
            "take_profit_3": 106.0,
        },
        "1d": {
            "direction": "BUY",
            "confidence": 0.95,
            "entry_price": 100.0,
            "stop_loss": 98.0,
            "take_profit_1": 102.0,
            "take_profit_2": 104.0,
            "take_profit_3": 106.0,
        },
    }
    
    # Setup: Ensemble with timeframe weights
    timeframe_weights = {
        "1m": 0.05,
        "5m": 0.10,
        "15m": 0.15,
        "1h": 0.20,
        "4h": 0.25,
        "1d": 0.25,
    }
    
    ensemble = MultiTimeframeEnsemble(
        timeframe_weights=timeframe_weights,
        conflict_resolution="weighted_vote"
    )
    
    # Execute: Get ensemble prediction
    with patch.object(ensemble, "_get_timeframe_prediction") as mock_get_pred:
        async def mock_prediction(symbol, timeframe, features):
            return mock_predictions[timeframe]
        
        mock_get_pred.side_effect = mock_prediction
        
        features = np.random.randn(1, 50).astype(np.float32)
        ensemble_result = await ensemble.predict(
            symbol="BTCUSDT",
            features=features
        )
    
    # Assert: All timeframes included
    assert "timeframe_predictions" in ensemble_result
    assert len(ensemble_result["timeframe_predictions"]) == 6
    for tf in timeframes:
        assert tf in ensemble_result["timeframe_predictions"]
    
    # Assert: Weighted average confidence calculated
    # Expected: (0.75*0.05 + 0.80*0.10 + 0.85*0.15 + 0.70*0.20 + 0.90*0.25 + 0.95*0.25)
    expected_confidence = (
        0.75 * 0.05 +
        0.80 * 0.10 +
        0.85 * 0.15 +
        0.70 * 0.20 +
        0.90 * 0.25 +
        0.95 * 0.25
    )
    assert "ensemble_confidence" in ensemble_result
    assert abs(ensemble_result["ensemble_confidence"] - expected_confidence) < 0.01
    
    # Assert: Conflict resolution (majority vote)
    # 5 BUY vs 1 SELL → BUY wins
    assert ensemble_result["ensemble_direction"] == "BUY"
    
    # Assert: Ensemble metadata
    assert ensemble_result["metadata"]["symbol"] == "BTCUSDT"
    assert ensemble_result["metadata"]["timeframes_used"] == timeframes
    assert ensemble_result["metadata"]["conflict_detected"] is True


@pytest.mark.asyncio
async def test_ensemble_unanimous_prediction(db_session: AsyncSession):
    """
    Integration Test 36.3b: Ensemble with unanimous predictions
    
    Validates:
    - No conflict when all timeframes agree
    - Higher confidence when unanimous
    - Requirements: 17.2
    """
    # Setup: All timeframes predict BUY
    mock_predictions = {
        "1m": {"direction": "BUY", "confidence": 0.80},
        "5m": {"direction": "BUY", "confidence": 0.85},
        "15m": {"direction": "BUY", "confidence": 0.90},
        "1h": {"direction": "BUY", "confidence": 0.88},
        "4h": {"direction": "BUY", "confidence": 0.92},
        "1d": {"direction": "BUY", "confidence": 0.95},
    }
    
    timeframe_weights = {
        "1m": 0.05, "5m": 0.10, "15m": 0.15,
        "1h": 0.20, "4h": 0.25, "1d": 0.25,
    }
    
    ensemble = MultiTimeframeEnsemble(
        timeframe_weights=timeframe_weights,
        conflict_resolution="weighted_vote"
    )
    
    with patch.object(ensemble, "_get_timeframe_prediction") as mock_get_pred:
        async def mock_prediction(symbol, timeframe, features):
            return mock_predictions[timeframe]
        
        mock_get_pred.side_effect = mock_prediction
        
        features = np.random.randn(1, 50).astype(np.float32)
        ensemble_result = await ensemble.predict(
            symbol="BTCUSDT",
            features=features
        )
    
    # Assert: No conflict detected
    assert ensemble_result["metadata"]["conflict_detected"] is False
    
    # Assert: Unanimous direction
    assert ensemble_result["ensemble_direction"] == "BUY"
    
    # Assert: High confidence (weighted average)
    expected_confidence = (
        0.80 * 0.05 + 0.85 * 0.10 + 0.90 * 0.15 +
        0.88 * 0.20 + 0.92 * 0.25 + 0.95 * 0.25
    )
    assert abs(ensemble_result["ensemble_confidence"] - expected_confidence) < 0.01
    assert ensemble_result["ensemble_confidence"] > 0.85  # High confidence


@pytest.mark.asyncio
async def test_ensemble_hold_on_low_confidence(db_session: AsyncSession):
    """
    Integration Test 36.3c: Ensemble returns HOLD on low confidence
    
    Validates:
    - HOLD when ensemble confidence < threshold
    - Requirements: 17.3
    """
    # Setup: All timeframes have low confidence
    mock_predictions = {
        "1m": {"direction": "BUY", "confidence": 0.45},
        "5m": {"direction": "SELL", "confidence": 0.40},
        "15m": {"direction": "BUY", "confidence": 0.48},
        "1h": {"direction": "SELL", "confidence": 0.42},
        "4h": {"direction": "BUY", "confidence": 0.46},
        "1d": {"direction": "SELL", "confidence": 0.44},
    }
    
    timeframe_weights = {
        "1m": 0.05, "5m": 0.10, "15m": 0.15,
        "1h": 0.20, "4h": 0.25, "1d": 0.25,
    }
    
    ensemble = MultiTimeframeEnsemble(
        timeframe_weights=timeframe_weights,
        confidence_threshold=0.50  # Require 50% confidence
    )
    
    with patch.object(ensemble, "_get_timeframe_prediction") as mock_get_pred:
        async def mock_prediction(symbol, timeframe, features):
            return mock_predictions[timeframe]
        
        mock_get_pred.side_effect = mock_prediction
        
        features = np.random.randn(1, 50).astype(np.float32)
        ensemble_result = await ensemble.predict(
            symbol="BTCUSDT",
            features=features
        )
    
    # Assert: HOLD due to low confidence
    assert ensemble_result["ensemble_direction"] == "HOLD"
    
    # Assert: Low ensemble confidence
    assert ensemble_result["ensemble_confidence"] < 0.50
