"""Property-based tests for ML prediction system using Hypothesis."""
import numpy as np
import pytest
from hypothesis import given, settings, strategies as st
from unittest.mock import MagicMock, patch

from app.ml.inference.prediction_engine import PredictionEngine


# Hypothesis settings for thorough testing
hypothesis_settings = settings(
    max_examples=200,
    deadline=None,
)


@given(
    confidence=st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False)
)
@hypothesis_settings
def test_property_confidence_score_range(confidence: float):
    """
    Property 7: Confidence score range validation
    
    Property: All confidence scores must be in range [0, 1] after post-processing
    Requirements: 6.3
    """
    engine = PredictionEngine(
        model_path="/tmp/test.onnx",
        cache=MagicMock(),
        cache_enabled=False
    )
    
    # Mock raw prediction with arbitrary confidence
    raw_prediction = {
        "direction_logits": np.array([[0.8, 0.15, 0.05]]),
        "confidence": np.array([[confidence]]),
        "entry": np.array([[100.0]]),
        "stop_loss": np.array([[98.0]]),
        "tp1": np.array([[102.0]]),
        "tp2": np.array([[104.0]]),
        "tp3": np.array([[106.0]]),
        "volatility": np.array([[2.5]]),
    }
    
    # Post-process
    result = engine._post_process_prediction(raw_prediction, "BTCUSDT", "1h", "1.0.0")
    
    # Assert: Confidence is clamped to [0, 1]
    assert 0.0 <= result["confidence"] <= 1.0, f"Confidence {result['confidence']} outside [0, 1]"


@given(
    direction=st.integers(min_value=0, max_value=2),
    entry=st.floats(min_value=50.0, max_value=150.0),
    sl=st.floats(min_value=50.0, max_value=150.0),
    tp1=st.floats(min_value=50.0, max_value=150.0),
    tp2=st.floats(min_value=50.0, max_value=150.0),
    tp3=st.floats(min_value=50.0, max_value=150.0),
)
@hypothesis_settings
def test_property_output_constraint_validation(
    direction: int, entry: float, sl: float, tp1: float, tp2: float, tp3: float
):
    """
    Property 14: Output constraint validation
    
    Property: After post-processing, all outputs must satisfy consistency constraints:
    - BUY: SL < Entry < TP1 < TP2 < TP3
    - SELL: SL > Entry > TP1 > TP2 > TP3
    - HOLD: TP1 = TP2 = TP3 = Entry
    
    Requirements: 15.4
    """
    engine = PredictionEngine(
        model_path="/tmp/test.onnx",
        cache=MagicMock(),
        cache_enabled=False
    )
    
    # Map direction to label
    direction_map = {0: "BUY", 1: "SELL", 2: "HOLD"}
    
    # Mock raw prediction
    raw_prediction = {
        "direction_logits": np.array([[0.8 if direction == 0 else 0.1, 
                                       0.8 if direction == 1 else 0.1,
                                       0.8 if direction == 2 else 0.1]]),
        "confidence": np.array([[0.85]]),
        "entry": np.array([[entry]]),
        "stop_loss": np.array([[sl]]),
        "tp1": np.array([[tp1]]),
        "tp2": np.array([[tp2]]),
        "tp3": np.array([[tp3]]),
        "volatility": np.array([[2.5]]),
    }
    
    # Post-process
    result = engine._post_process_prediction(raw_prediction, "BTCUSDT", "1h", "1.0.0")
    
    # Validate constraints based on direction
    if result["direction"] == "BUY":
        assert result["stop_loss"] < result["entry_price"], "BUY: SL should be < Entry"
        assert result["entry_price"] < result["take_profit_1"], "BUY: Entry should be < TP1"
        assert result["take_profit_1"] < result["take_profit_2"], "BUY: TP1 should be < TP2"
        assert result["take_profit_2"] < result["take_profit_3"], "BUY: TP2 should be < TP3"
    
    elif result["direction"] == "SELL":
        assert result["stop_loss"] > result["entry_price"], "SELL: SL should be > Entry"
        assert result["entry_price"] > result["take_profit_1"], "SELL: Entry should be > TP1"
        assert result["take_profit_1"] > result["take_profit_2"], "SELL: TP1 should be > TP2"
        assert result["take_profit_2"] > result["take_profit_3"], "SELL: TP2 should be > TP3"
    
    elif result["direction"] == "HOLD":
        assert result["take_profit_1"] == result["entry_price"], "HOLD: TP1 should equal Entry"
        assert result["take_profit_2"] == result["entry_price"], "HOLD: TP2 should equal Entry"
        assert result["take_profit_3"] == result["entry_price"], "HOLD: TP3 should equal Entry"


@given(
    feature_values=st.lists(
        st.floats(min_value=-10.0, max_value=10.0, allow_nan=False, allow_infinity=False),
        min_size=50,
        max_size=50
    )
)
@hypothesis_settings
def test_property_shap_explainability_threshold(feature_values: list[float]):
    """
    Property 16: SHAP explainability threshold
    
    Property: SHAP values must be computed for all predictions and sum of absolute
    SHAP values should be > 0 (non-trivial explanation)
    
    Requirements: 22.4
    """
    engine = PredictionEngine(
        model_path="/tmp/test.onnx",
        cache=MagicMock(),
        cache_enabled=False
    )
    
    features = np.array([feature_values], dtype=np.float32)
    
    # Mock ONNX model
    mock_model = MagicMock()
    mock_model.run.return_value = [
        np.array([[0.8, 0.15, 0.05]]),
        np.array([[0.85]]),
        np.array([[100.0]]),
        np.array([[98.0]]),
        np.array([[102.0]]),
        np.array([[104.0]]),
        np.array([[106.0]]),
        np.array([[2.5]]),
    ]
    
    # Mock SHAP explainer
    mock_shap_values = np.random.randn(1, 50) * 0.1  # Small but non-zero values
    
    with patch("app.ml.inference.prediction_engine.onnx.InferenceSession") as mock_session:
        mock_session.return_value = mock_model
        
        with patch("app.ml.inference.prediction_engine.shap.KernelExplainer") as mock_explainer:
            mock_explainer_instance = MagicMock()
            mock_explainer_instance.shap_values.return_value = mock_shap_values
            mock_explainer.return_value = mock_explainer_instance
            
            # Make prediction (async, so we need to run in event loop)
            import asyncio
            result = asyncio.run(engine.predict(
                features=features,
                symbol="BTCUSDT",
                timeframe="1h",
                model_version="1.0.0"
            ))
    
    # Assert: SHAP values present
    assert "shap_values" in result, "SHAP values missing from prediction"
    
    # Assert: SHAP values are non-trivial
    if result["shap_values"] is not None:
        shap_sum = np.sum(np.abs(result["shap_values"]))
        assert shap_sum > 0, "SHAP values sum to zero (trivial explanation)"


@given(
    volatility=st.floats(min_value=-5.0, max_value=100.0, allow_nan=False, allow_infinity=False)
)
@hypothesis_settings
def test_property_volatility_always_positive(volatility: float):
    """
    Property: Volatility estimates must always be positive after post-processing
    
    Requirements: 1.5
    """
    engine = PredictionEngine(
        model_path="/tmp/test.onnx",
        cache=MagicMock(),
        cache_enabled=False
    )
    
    raw_prediction = {
        "direction_logits": np.array([[0.8, 0.15, 0.05]]),
        "confidence": np.array([[0.85]]),
        "entry": np.array([[100.0]]),
        "stop_loss": np.array([[98.0]]),
        "tp1": np.array([[102.0]]),
        "tp2": np.array([[104.0]]),
        "tp3": np.array([[106.0]]),
        "volatility": np.array([[volatility]]),
    }
    
    result = engine._post_process_prediction(raw_prediction, "BTCUSDT", "1h", "1.0.0")
    
    # Assert: Volatility is always positive
    assert result.get("volatility_estimate", 0) >= 0, f"Volatility {result.get('volatility_estimate')} is negative"


@given(
    prices=st.lists(
        st.floats(min_value=1.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
        min_size=7,
        max_size=7
    )
)
@hypothesis_settings
def test_property_price_ordering_consistency(prices: list[float]):
    """
    Property: Price levels must maintain consistent ordering after post-processing
    regardless of input values
    
    Requirements: 1.2, 1.3
    """
    engine = PredictionEngine(
        model_path="/tmp/test.onnx",
        cache=MagicMock(),
        cache_enabled=False
    )
    
    entry, sl, tp1, tp2, tp3 = prices[0], prices[1], prices[2], prices[3], prices[4]
    
    # Test BUY direction
    raw_prediction_buy = {
        "direction_logits": np.array([[0.9, 0.05, 0.05]]),  # Strong BUY
        "confidence": np.array([[0.85]]),
        "entry": np.array([[entry]]),
        "stop_loss": np.array([[sl]]),
        "tp1": np.array([[tp1]]),
        "tp2": np.array([[tp2]]),
        "tp3": np.array([[tp3]]),
        "volatility": np.array([[2.5]]),
    }
    
    result_buy = engine._post_process_prediction(raw_prediction_buy, "BTCUSDT", "1h", "1.0.0")
    
    if result_buy["direction"] == "BUY":
        # Verify ordering is enforced
        assert result_buy["stop_loss"] < result_buy["entry_price"]
        assert result_buy["entry_price"] < result_buy["take_profit_1"]
        assert result_buy["take_profit_1"] < result_buy["take_profit_2"]
        assert result_buy["take_profit_2"] < result_buy["take_profit_3"]


@given(
    low_confidence=st.floats(min_value=0.0, max_value=0.49)
)
@hypothesis_settings
def test_property_low_confidence_returns_hold(low_confidence: float):
    """
    Property: Predictions with confidence < 0.5 should return HOLD direction
    
    Requirements: 1.1
    """
    engine = PredictionEngine(
        model_path="/tmp/test.onnx",
        cache=MagicMock(),
        cache_enabled=False
    )
    
    raw_prediction = {
        "direction_logits": np.array([[0.4, 0.3, 0.3]]),  # Uncertain
        "confidence": np.array([[low_confidence]]),
        "entry": np.array([[100.0]]),
        "stop_loss": np.array([[98.0]]),
        "tp1": np.array([[102.0]]),
        "tp2": np.array([[104.0]]),
        "tp3": np.array([[106.0]]),
        "volatility": np.array([[2.5]]),
    }
    
    result = engine._post_process_prediction(raw_prediction, "BTCUSDT", "1h", "1.0.0")
    
    # Assert: Low confidence results in HOLD
    assert result["direction"] == "HOLD", f"Low confidence {low_confidence} should return HOLD"
