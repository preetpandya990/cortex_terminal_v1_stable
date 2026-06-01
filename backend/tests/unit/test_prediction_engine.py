"""
Unit tests for PredictionEngine with ONNX inference and caching.

Tests:
- Prediction with mock ONNX model
- Output post-processing (consistency constraints)
- Cache behavior (store, retrieve, invalidate)
- Multi-output validation
- Error handling

Requirements: 16.1, 16.3, 1.1, 1.2, 1.4, 1.5
"""
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import pytest_asyncio

from app.ml.inference.prediction_engine import PredictionEngine


@pytest_asyncio.fixture
async def mock_onnx_model():
    """Create a mock ONNX model file."""
    with tempfile.NamedTemporaryFile(suffix=".onnx", delete=False) as f:
        # Write minimal ONNX model (just a placeholder)
        f.write(b"fake_onnx_model")
        model_path = f.name
    
    yield model_path
    
    # Cleanup
    Path(model_path).unlink(missing_ok=True)


@pytest_asyncio.fixture
async def mock_cache():
    """Mock cache service."""
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    return cache


@pytest_asyncio.fixture
async def mock_onnx_session():
    """Mock ONNX Runtime session."""
    session = MagicMock()
    
    # Mock inputs/outputs
    input_mock = MagicMock()
    input_mock.name = "input"
    session.get_inputs.return_value = [input_mock]
    
    output_mocks = [MagicMock() for _ in range(8)]
    for i, mock in enumerate(output_mocks):
        mock.name = f"output_{i}"
    session.get_outputs.return_value = output_mocks
    
    # Mock inference (returns 8 outputs)
    session.run.return_value = [
        np.array([[0.1, 0.2, 0.7]]),  # direction logits (BUY)
        np.array([[0.85]]),            # confidence
        np.array([[100.0]]),           # entry_price
        np.array([[105.0]]),           # tp1
        np.array([[110.0]]),           # tp2
        np.array([[115.0]]),           # tp3
        np.array([[95.0]]),            # stop_loss
        np.array([[2.5]]),             # volatility
    ]
    
    return session


# ── Test Prediction ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch('app.ml.inference.prediction_engine.ort.InferenceSession')
async def test_predict_returns_all_outputs(mock_session_class, mock_onnx_model, mock_onnx_session, mock_cache):
    """Test that prediction returns all 7 outputs."""
    mock_session_class.return_value = mock_onnx_session
    
    engine = PredictionEngine(mock_onnx_model, cache=mock_cache)
    
    # Create feature vector
    features = np.random.randn(60, 42)
    
    # Run prediction
    prediction = await engine.predict(
        features,
        symbol="TEST",
        timeframe="1d",
        model_version="1.0.0",
        use_cache=False,
    )
    
    # Verify all outputs present
    assert "direction" in prediction
    assert "direction_label" in prediction
    assert "confidence" in prediction
    assert "entry_price" in prediction
    assert "tp1" in prediction
    assert "tp2" in prediction
    assert "tp3" in prediction
    assert "stop_loss" in prediction
    assert "volatility" in prediction


@pytest.mark.asyncio
@patch('app.ml.inference.prediction_engine.ort.InferenceSession')
async def test_predict_adds_batch_dimension(mock_session_class, mock_onnx_model, mock_onnx_session, mock_cache):
    """Test that 2D input gets batch dimension added."""
    mock_session_class.return_value = mock_onnx_session
    
    engine = PredictionEngine(mock_onnx_model, cache=mock_cache)
    
    # 2D feature vector (no batch dimension)
    features = np.random.randn(60, 42)
    
    await engine.predict(features, use_cache=False)
    
    # Verify session.run was called with 3D input
    call_args = mock_onnx_session.run.call_args
    input_data = call_args[0][1]["input"]
    assert input_data.ndim == 3
    assert input_data.shape[0] == 1  # Batch size


# ── Test Post-Processing ───────────────────────────────────────────────────────


def test_post_process_buy_enforces_tp_ordering():
    """Test that BUY predictions have TP1 < TP2 < TP3."""
    engine = PredictionEngine.__new__(PredictionEngine)
    
    raw = {
        "direction": np.array([0.1, 0.2, 0.7]),  # BUY (index 2)
        "confidence": 0.85,
        "entry_price": 100.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "stop_loss": 95.0,
        "volatility": 2.5,
    }
    
    processed = engine.post_process(raw)
    
    assert processed["direction_label"] == "BUY"
    assert processed["tp1"] < processed["tp2"]
    assert processed["tp2"] < processed["tp3"]


def test_post_process_sell_enforces_tp_ordering():
    """Test that SELL predictions have TP1 > TP2 > TP3."""
    engine = PredictionEngine.__new__(PredictionEngine)
    
    raw = {
        "direction": np.array([0.7, 0.2, 0.1]),  # SELL (index 0)
        "confidence": 0.85,
        "entry_price": 100.0,
        "tp1": 95.0,
        "tp2": 90.0,
        "tp3": 85.0,
        "stop_loss": 105.0,
        "volatility": 2.5,
    }
    
    processed = engine.post_process(raw)
    
    assert processed["direction_label"] == "SELL"
    assert processed["tp1"] > processed["tp2"]
    assert processed["tp2"] > processed["tp3"]


def test_post_process_buy_enforces_sl_below_entry():
    """Test that BUY predictions have SL < Entry."""
    engine = PredictionEngine.__new__(PredictionEngine)
    
    raw = {
        "direction": np.array([0.1, 0.2, 0.7]),  # BUY
        "confidence": 0.85,
        "entry_price": 100.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "stop_loss": 95.0,
        "volatility": 2.5,
    }
    
    processed = engine.post_process(raw)
    
    assert processed["stop_loss"] < processed["entry_price"]


def test_post_process_sell_enforces_sl_above_entry():
    """Test that SELL predictions have SL > Entry."""
    engine = PredictionEngine.__new__(PredictionEngine)
    
    raw = {
        "direction": np.array([0.7, 0.2, 0.1]),  # SELL
        "confidence": 0.85,
        "entry_price": 100.0,
        "tp1": 95.0,
        "tp2": 90.0,
        "tp3": 85.0,
        "stop_loss": 105.0,
        "volatility": 2.5,
    }
    
    processed = engine.post_process(raw)
    
    assert processed["stop_loss"] > processed["entry_price"]


def test_post_process_low_confidence_returns_hold():
    """Test that confidence < 0.5 returns HOLD."""
    engine = PredictionEngine.__new__(PredictionEngine)
    
    raw = {
        "direction": np.array([0.1, 0.2, 0.7]),  # BUY logits
        "confidence": 0.3,  # Low confidence
        "entry_price": 100.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "stop_loss": 95.0,
        "volatility": 2.5,
    }
    
    processed = engine.post_process(raw)
    
    assert processed["direction_label"] == "HOLD"
    assert processed["confidence"] < 0.5


def test_post_process_clamps_confidence():
    """Test that confidence is clamped to [0, 1]."""
    engine = PredictionEngine.__new__(PredictionEngine)
    
    # Test upper bound
    raw = {
        "direction": np.array([0.1, 0.2, 0.7]),
        "confidence": 1.5,  # Over 1.0
        "entry_price": 100.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "stop_loss": 95.0,
        "volatility": 2.5,
    }
    
    processed = engine.post_process(raw)
    assert processed["confidence"] <= 1.0
    
    # Test lower bound
    raw["confidence"] = -0.5  # Below 0.0
    processed = engine.post_process(raw)
    assert processed["confidence"] >= 0.0


def test_post_process_ensures_positive_volatility():
    """Test that volatility is always positive."""
    engine = PredictionEngine.__new__(PredictionEngine)
    
    raw = {
        "direction": np.array([0.1, 0.2, 0.7]),
        "confidence": 0.85,
        "entry_price": 100.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "stop_loss": 95.0,
        "volatility": -2.5,  # Negative
    }
    
    processed = engine.post_process(raw)
    assert processed["volatility"] >= 0.0


def test_post_process_hold_sets_tp_equal_to_entry():
    """Test that HOLD predictions have TP levels equal to entry."""
    engine = PredictionEngine.__new__(PredictionEngine)
    
    raw = {
        "direction": np.array([0.2, 0.7, 0.1]),  # HOLD (index 1)
        "confidence": 0.85,
        "entry_price": 100.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "stop_loss": 95.0,
        "volatility": 2.5,
    }
    
    processed = engine.post_process(raw)
    
    assert processed["direction_label"] == "HOLD"
    assert processed["tp1"] == processed["entry_price"]
    assert processed["tp2"] == processed["entry_price"]
    assert processed["tp3"] == processed["entry_price"]


# ── Test Caching ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch('app.ml.inference.prediction_engine.ort.InferenceSession')
async def test_predict_uses_cache_on_hit(mock_session_class, mock_onnx_model, mock_onnx_session, mock_cache):
    """Test that cached predictions are returned."""
    mock_session_class.return_value = mock_onnx_session
    
    # Mock cache hit
    cached_prediction = {
        "direction": 2,
        "direction_label": "BUY",
        "confidence": 0.85,
        "entry_price": 100.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "stop_loss": 95.0,
        "volatility": 2.5,
    }
    mock_cache.get.return_value = cached_prediction
    
    engine = PredictionEngine(mock_onnx_model, cache=mock_cache)
    features = np.random.randn(60, 42)
    
    prediction = await engine.predict(
        features,
        symbol="TEST",
        timeframe="1d",
        model_version="1.0.0",
        use_cache=True,
    )
    
    # Verify cache was checked
    mock_cache.get.assert_called_once()
    
    # Verify inference was NOT run (cache hit)
    assert mock_onnx_session.run.call_count == 0
    
    # Verify cached prediction was returned
    assert prediction == cached_prediction


@pytest.mark.asyncio
@patch('app.ml.inference.prediction_engine.ort.InferenceSession')
async def test_predict_stores_in_cache(mock_session_class, mock_onnx_model, mock_onnx_session, mock_cache):
    """Test that predictions are stored in cache."""
    mock_session_class.return_value = mock_onnx_session
    mock_cache.get.return_value = None  # Cache miss
    
    engine = PredictionEngine(mock_onnx_model, cache=mock_cache)
    features = np.random.randn(60, 42)
    
    await engine.predict(
        features,
        symbol="TEST",
        timeframe="1d",
        model_version="1.0.0",
        use_cache=True,
    )
    
    # Verify prediction was cached
    mock_cache.set.assert_called_once()


@pytest.mark.asyncio
@patch('app.ml.inference.prediction_engine.ort.InferenceSession')
async def test_predict_skips_cache_when_disabled(mock_session_class, mock_onnx_model, mock_onnx_session, mock_cache):
    """Test that cache is skipped when use_cache=False."""
    mock_session_class.return_value = mock_onnx_session
    
    engine = PredictionEngine(mock_onnx_model, cache=mock_cache)
    features = np.random.randn(60, 42)
    
    await engine.predict(
        features,
        symbol="TEST",
        timeframe="1d",
        model_version="1.0.0",
        use_cache=False,
    )
    
    # Verify cache was NOT checked
    mock_cache.get.assert_not_called()
    
    # Verify inference WAS run
    assert mock_onnx_session.run.call_count == 1


# ── Test Metadata ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@patch('app.ml.inference.prediction_engine.ort.InferenceSession')
async def test_predict_includes_metadata(mock_session_class, mock_onnx_model, mock_onnx_session, mock_cache):
    """Test that prediction includes metadata."""
    mock_session_class.return_value = mock_onnx_session
    
    engine = PredictionEngine(mock_onnx_model, cache=mock_cache)
    features = np.random.randn(60, 42)
    
    prediction = await engine.predict(
        features,
        symbol="TEST",
        timeframe="1d",
        model_version="1.0.0",
        use_cache=False,
    )
    
    assert "metadata" in prediction
    assert prediction["metadata"]["symbol"] == "TEST"
    assert prediction["metadata"]["timeframe"] == "1d"
    assert prediction["metadata"]["model_version"] == "1.0.0"
    assert "predicted_at" in prediction["metadata"]


# ── Test Initialization ────────────────────────────────────────────────────────


@patch('app.ml.inference.prediction_engine.ort.InferenceSession')
def test_engine_initialization(mock_session_class, mock_onnx_model):
    """Test that engine initializes correctly."""
    mock_session = MagicMock()
    mock_session_class.return_value = mock_session
    
    engine = PredictionEngine(mock_onnx_model, num_threads=4)
    
    assert engine.model_path == mock_onnx_model
    assert engine.session == mock_session
    
    # Verify session options were configured
    call_args = mock_session_class.call_args
    assert call_args is not None
