"""
Test ML Integration with Signal Assembler

Tests the integration between ensemble predictor and signal fusion.
"""
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.ai.fusion.signal_assembler import SignalAssembler
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.ml.inference.feature_loader import FeatureLoader


@pytest.fixture(autouse=True)
def _disable_staleness_guard(monkeypatch):
    """These tests exercise the ML signal MAPPING in isolation (mocked predictor).

    The OHLCV staleness guard adds live-DB freshness queries to gather_ml_signals
    that a mocked DB can't serve; it is verified separately against real data in
    tests/integration/test_staleness_guard_live.py, so disable it here.
    """
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "ENABLE_STALENESS_GUARD", False)


@pytest.mark.asyncio
async def test_gather_ml_signals_with_prediction():
    """Test gather_ml_signals calls ensemble predictor correctly."""
    # Mock ensemble predictor
    mock_predictor = MagicMock(spec=EnsemblePredictor)
    mock_predictor.predict = AsyncMock(return_value={
        "direction_label": "BUY",
        "confidence": 0.85,
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "tp1": 105.0,
        "tp2": 110.0,
        "tp3": 115.0,
        "metadata": {"xgboost_version": "1.1.1", "gru_version": "1.1.1"},
    })

    # Mock feature loader
    mock_loader = MagicMock(spec=FeatureLoader)
    mock_loader.load_features = AsyncMock(return_value=(
        np.random.randn(50).astype(np.float32),  # tabular
        np.random.randn(60, 50).astype(np.float32),  # sequence
        100.0,  # current_price
        0.02,  # volatility
    ))

    # Create signal assembler
    assembler = SignalAssembler(
        ensemble_predictor=mock_predictor,
        feature_loader=mock_loader
    )

    # Call gather_ml_signals
    db_mock = AsyncMock()
    result = await assembler.gather_ml_signals(
        db=db_mock,
        symbol="NSE_EQ|INE002A01018",
        timeframe="1d",
    )

    # Verify feature loader was called with the indicator snapshot output buffer
    mock_loader.load_features.assert_called_once_with(
        symbol="NSE_EQ|INE002A01018",
        timeframe="1d",
        indicator_snapshot_out={},
    )

    # Verify predictor was called
    assert mock_predictor.predict.called

    # Verify result
    assert result["score"] == 100.0  # BUY = +100
    assert result["confidence"] == 0.85
    assert result["model"] == "xgb_1.1.1+gru_1.1.1"
    assert result["prediction"]["direction"] == "BUY"
    assert result["prediction"]["entry_price"] == 100.0
    assert result["prediction"]["stop_loss"] == 95.0
    assert result["prediction"]["targets"] == [105.0, 110.0, 115.0]


@pytest.mark.asyncio
async def test_gather_ml_signals_sell_direction():
    """Test gather_ml_signals converts SELL to negative score."""
    mock_predictor = MagicMock(spec=EnsemblePredictor)
    mock_predictor.predict = AsyncMock(return_value={
        "direction_label": "SELL",
        "confidence": 0.75,
        "entry_price": 100.0,
        "stop_loss": 105.0,
        "tp1": 95.0,
        "tp2": 90.0,
        "tp3": 85.0,
        "metadata": {"model_version": "v1.0.0"},
    })

    mock_loader = MagicMock(spec=FeatureLoader)
    mock_loader.load_features = AsyncMock(return_value=(
        np.random.randn(50).astype(np.float32),
        np.random.randn(60, 50).astype(np.float32),
        100.0,
        0.02,
    ))

    assembler = SignalAssembler(
        ensemble_predictor=mock_predictor,
        feature_loader=mock_loader
    )

    result = await assembler.gather_ml_signals(
        db=AsyncMock(),
        symbol="NSE_EQ|INE002A01018",
        timeframe="1d",
    )

    assert result["score"] == -100.0  # SELL = -100
    assert result["confidence"] == 0.75
    assert result["prediction"]["direction"] == "SELL"


@pytest.mark.asyncio
async def test_gather_ml_signals_no_predictor():
    """Test gather_ml_signals returns zeros when no predictor provided."""
    assembler = SignalAssembler(
        ensemble_predictor=None,
        feature_loader=None
    )

    result = await assembler.gather_ml_signals(
        db=AsyncMock(),
        symbol="NSE_EQ|INE002A01018",
        timeframe="1d",
    )

    assert result["score"] == 0.0
    assert result["confidence"] == 0.0
    assert result["model"] is None


@pytest.mark.asyncio
async def test_gather_ml_signals_no_loader():
    """Test gather_ml_signals returns zeros when no loader provided."""
    mock_predictor = MagicMock(spec=EnsemblePredictor)
    
    assembler = SignalAssembler(
        ensemble_predictor=mock_predictor,
        feature_loader=None
    )

    result = await assembler.gather_ml_signals(
        db=AsyncMock(),
        symbol="NSE_EQ|INE002A01018",
        timeframe="1d",
    )

    assert result["score"] == 0.0
    assert result["confidence"] == 0.0
    assert result["model"] is None
    # Predictor should not be called if loader is missing
    assert not mock_predictor.predict.called


@pytest.mark.asyncio
async def test_gather_ml_signals_handles_errors():
    """Test gather_ml_signals handles prediction errors gracefully."""
    mock_predictor = MagicMock(spec=EnsemblePredictor)
    mock_predictor.predict = AsyncMock(side_effect=Exception("Prediction error"))

    mock_loader = MagicMock(spec=FeatureLoader)
    mock_loader.load_features = AsyncMock(return_value=(
        np.random.randn(50).astype(np.float32),
        np.random.randn(60, 50).astype(np.float32),
        100.0,
        0.02,
    ))

    assembler = SignalAssembler(
        ensemble_predictor=mock_predictor,
        feature_loader=mock_loader
    )

    result = await assembler.gather_ml_signals(
        db=AsyncMock(),
        symbol="NSE_EQ|INE002A01018",
        timeframe="1d",
    )

    assert result["score"] == 0.0
    assert result["confidence"] == 0.0
    assert result["model"] is None
    assert "error" in result
    assert "Prediction error" in result["error"]


@pytest.mark.asyncio
async def test_fuse_signals_with_ml():
    """Test signal fusion includes ML predictions."""
    assembler = SignalAssembler()

    event_signals = {"score": 50.0, "confidence": 0.7, "events": []}
    ml_signals = {"score": 100.0, "confidence": 0.85, "model": "v1.0.0"}
    technical_signals = {"score": 0.0, "confidence": 0.0, "indicators": {}}

    fused = assembler.fuse_signals(event_signals, ml_signals, technical_signals)

    # Weighted average: 0.35*50 + 0.40*100 + 0.25*0 = 57.5
    assert fused["fused_score"] == 57.5
    assert fused["action"] == "BUY"  # score > 50
    # Confidence: 0.35*0.7 + 0.40*0.85 + 0.25*0 = 0.585
    assert abs(fused["confidence_score"] - 0.585) < 0.01
    assert fused["ml_predictions"] == ml_signals
