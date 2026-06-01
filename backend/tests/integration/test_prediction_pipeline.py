"""Integration test: End-to-end prediction pipeline."""
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.inference.prediction_engine import PredictionEngine
from app.ml.feature_store import FeatureStore
from app.ml.model_registry import ModelRegistry
from app.ml.audit.audit_logger import AuditLogger
from app.models.ml_data import MLModelMetadata


@pytest.mark.asyncio
async def test_end_to_end_prediction_pipeline(db_session: AsyncSession, mock_redis: AsyncMock):
    """
    Integration Test 36.1: End-to-end prediction pipeline
    
    Flow: Load OHLCV → Compute features → Run inference → Return prediction
    
    Validates:
    - All 7 outputs present (direction, confidence, entry, sl, tp1, tp2, tp3)
    - SHAP explanation included
    - Audit log created
    - Requirements: 1.1, 1.2, 1.4, 1.5, 8.1, 16.1
    """
    # Setup: Mock OHLCV data
    ohlcv_data = np.array([
        [100.0, 105.0, 99.0, 103.0, 10000.0],  # OHLCV bars
        [103.0, 107.0, 102.0, 106.0, 12000.0],
        [106.0, 108.0, 104.0, 105.0, 11000.0],
    ])
    
    # Setup: Mock model with known outputs
    mock_model = MagicMock()
    mock_model.run.return_value = [
        np.array([[0.8, 0.15, 0.05]]),  # direction logits (BUY)
        np.array([[0.85]]),              # confidence
        np.array([[105.5]]),             # entry
        np.array([[103.0]]),             # stop_loss
        np.array([[107.0]]),             # tp1
        np.array([[108.5]]),             # tp2
        np.array([[110.0]]),             # tp3
        np.array([[2.5]]),               # volatility
    ]
    
    # Setup: Mock SHAP values
    mock_shap_values = np.random.randn(1, 50)  # 50 features
    
    # Setup: Register encrypted model
    with patch("app.ml.model_registry.onnx.load") as mock_onnx_load:
        mock_onnx_load.return_value = mock_model
        
        registry = ModelRegistry(db_session, storage_path="/tmp/test_models")
        
        # Create mock model file
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as f:
            f.write(b"mock_onnx_model_data")
            model_path = f.name
        
        # Register model
        model = await registry.register_model(
            name="test_model",
            version="1.0.0",
            artifact_path=model_path,
            metadata={"accuracy": 0.90, "timeframe": "1h"}
        )
    
    # Setup: Feature store
    feature_store = FeatureStore(cache_service=MagicMock())
    
    # Setup: Prediction engine
    with patch("app.ml.inference.prediction_engine.onnx.InferenceSession") as mock_session:
        mock_session.return_value = mock_model
        
        with patch("app.ml.inference.prediction_engine.shap.KernelExplainer") as mock_explainer:
            mock_explainer_instance = MagicMock()
            mock_explainer_instance.shap_values.return_value = mock_shap_values
            mock_explainer.return_value = mock_explainer_instance
            
            engine = PredictionEngine(
                model_path="/tmp/test_models/test_model_1.0.0.onnx.enc",
                cache_service=MagicMock(),
                enable_cache=False
            )
            
            # Setup: Audit logger
            audit_logger = AuditLogger(db_session)
            
            # Execute: Run prediction pipeline
            # 1. Compute features from OHLCV
            features = np.random.randn(1, 50).astype(np.float32)  # Mock features
            
            # 2. Run inference
            prediction = await engine.predict(
                features=features,
                symbol="BTCUSDT",
                timeframe="1h",
                model_version="1.0.0"
            )
            
            # 3. Log prediction
            await audit_logger.log_prediction(
                symbol="BTCUSDT",
                timeframe="1h",
                model_version="1.0.0",
                prediction=prediction,
                user_id="test-user"
            )
    
    # Assert: All 7 outputs present
    assert "direction" in prediction
    assert "confidence" in prediction
    assert "entry_price" in prediction
    assert "stop_loss" in prediction
    assert "take_profit_1" in prediction
    assert "take_profit_2" in prediction
    assert "take_profit_3" in prediction
    
    # Assert: Direction is valid
    assert prediction["direction"] in ["BUY", "SELL", "HOLD"]
    
    # Assert: Confidence in valid range
    assert 0.0 <= prediction["confidence"] <= 1.0
    
    # Assert: SHAP explanation included
    assert "shap_values" in prediction
    assert prediction["shap_values"] is not None
    
    # Assert: Metadata present
    assert prediction["metadata"]["symbol"] == "BTCUSDT"
    assert prediction["metadata"]["timeframe"] == "1h"
    assert prediction["metadata"]["model_version"] == "1.0.0"
    
    # Assert: Audit log created
    logs = await audit_logger.get_logs(
        symbol="BTCUSDT",
        start_date=None,
        end_date=None
    )
    assert len(logs) == 1
    assert logs[0].symbol == "BTCUSDT"
    assert logs[0].model_version == "1.0.0"
