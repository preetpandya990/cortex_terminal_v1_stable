"""
Example usage of MultiTimeframeEnsemble

This example demonstrates how to integrate the ensemble with the ML prediction system.
"""

from app.ml.ensemble import MultiTimeframeEnsemble, TimeframePrediction


def example_basic_usage():
    """Basic usage example with unanimous agreement."""
    print("=" * 60)
    print("Example 1: Unanimous Agreement")
    print("=" * 60)
    
    ensemble = MultiTimeframeEnsemble()
    
    predictions = {
        "daily": TimeframePrediction("daily", "buy", 0.8),
        "weekly": TimeframePrediction("weekly", "buy", 0.6),
        "monthly": TimeframePrediction("monthly", "buy", 0.7)
    }
    
    result = ensemble.predict(predictions)
    
    print(f"Direction: {result.direction}")
    print(f"Confidence: {result.confidence:.3f}")
    print(f"Conflict Resolved: {result.conflict_resolved}")
    print(f"Resolution Method: {result.conflict_resolution_method}")
    print()


def example_majority_vote():
    """Example with majority voting."""
    print("=" * 60)
    print("Example 2: Majority Vote")
    print("=" * 60)
    
    ensemble = MultiTimeframeEnsemble()
    
    predictions = {
        "daily": TimeframePrediction("daily", "buy", 0.8),
        "weekly": TimeframePrediction("weekly", "buy", 0.6),
        "monthly": TimeframePrediction("monthly", "sell", 0.7)
    }
    
    result = ensemble.predict(predictions)
    
    print(f"Direction: {result.direction}")
    print(f"Confidence: {result.confidence:.3f}")
    print(f"Conflict Resolved: {result.conflict_resolved}")
    print(f"Timeframe Predictions:")
    for tf, pred in result.timeframe_predictions.items():
        print(f"  {tf}: {pred.direction} ({pred.confidence:.3f})")
    print()


def example_conflict_resolution():
    """Example with conflict resolution."""
    print("=" * 60)
    print("Example 3: Conflict Resolution (Highest Confidence)")
    print("=" * 60)
    
    ensemble = MultiTimeframeEnsemble()
    
    predictions = {
        "daily": TimeframePrediction("daily", "buy", 0.6),
        "weekly": TimeframePrediction("weekly", "sell", 0.9),
        "monthly": TimeframePrediction("monthly", "hold", 0.5)
    }
    
    result = ensemble.predict(predictions)
    
    print(f"Direction: {result.direction}")
    print(f"Confidence: {result.confidence:.3f}")
    print(f"Conflict Resolved: {result.conflict_resolved}")
    print(f"Resolution Method: {result.conflict_resolution_method}")
    print(f"Timeframe Predictions:")
    for tf, pred in result.timeframe_predictions.items():
        print(f"  {tf}: {pred.direction} ({pred.confidence:.3f})")
    print()


def example_custom_weights():
    """Example with custom weights."""
    print("=" * 60)
    print("Example 4: Custom Weights (Heavy Daily)")
    print("=" * 60)
    
    # Give more weight to daily predictions
    ensemble = MultiTimeframeEnsemble(
        daily_weight=0.7,
        weekly_weight=0.2,
        monthly_weight=0.1
    )
    
    predictions = {
        "daily": TimeframePrediction("daily", "buy", 0.9),
        "weekly": TimeframePrediction("weekly", "sell", 0.6),
        "monthly": TimeframePrediction("monthly", "hold", 0.5)
    }
    
    result = ensemble.predict(predictions)
    
    print(f"Direction: {result.direction}")
    print(f"Confidence: {result.confidence:.3f}")
    print(f"Weights Used: {result.metadata['weights_used']}")
    print(f"Conflict Resolved: {result.conflict_resolved}")
    print()


def example_equal_confidence():
    """Example with equal confidence defaulting to longer timeframe."""
    print("=" * 60)
    print("Example 5: Equal Confidence (Longer Timeframe Default)")
    print("=" * 60)
    
    ensemble = MultiTimeframeEnsemble()
    
    predictions = {
        "daily": TimeframePrediction("daily", "buy", 0.7),
        "weekly": TimeframePrediction("weekly", "sell", 0.7),
        "monthly": TimeframePrediction("monthly", "hold", 0.7)
    }
    
    result = ensemble.predict(predictions)
    
    print(f"Direction: {result.direction}")
    print(f"Confidence: {result.confidence:.3f}")
    print(f"Conflict Resolved: {result.conflict_resolved}")
    print(f"Resolution Method: {result.conflict_resolution_method}")
    print("Note: When confidences are equal, defaults to longer timeframe (monthly)")
    print()


def example_partial_timeframes():
    """Example with only some timeframes available."""
    print("=" * 60)
    print("Example 6: Partial Timeframes (Daily + Weekly Only)")
    print("=" * 60)
    
    ensemble = MultiTimeframeEnsemble()
    
    # Only daily and weekly predictions available
    predictions = {
        "daily": TimeframePrediction("daily", "buy", 0.8),
        "weekly": TimeframePrediction("weekly", "buy", 0.6)
    }
    
    result = ensemble.predict(predictions)
    
    print(f"Direction: {result.direction}")
    print(f"Confidence: {result.confidence:.3f}")
    print(f"Available Timeframes: {list(result.timeframe_predictions.keys())}")
    print("Note: Confidence is normalized by available weights")
    print()


def example_trading_decision():
    """Example of using ensemble for trading decisions."""
    print("=" * 60)
    print("Example 7: Trading Decision Logic")
    print("=" * 60)
    
    ensemble = MultiTimeframeEnsemble()
    
    predictions = {
        "daily": TimeframePrediction("daily", "buy", 0.85),
        "weekly": TimeframePrediction("weekly", "buy", 0.75),
        "monthly": TimeframePrediction("monthly", "buy", 0.65)
    }
    
    result = ensemble.predict(predictions)
    
    print(f"Ensemble Prediction:")
    print(f"  Direction: {result.direction}")
    print(f"  Confidence: {result.confidence:.3f}")
    print()
    
    # Trading decision logic
    confidence_threshold = 0.7
    
    if result.direction == "buy" and result.confidence >= confidence_threshold:
        print(f"✓ EXECUTE BUY: Confidence {result.confidence:.3f} >= {confidence_threshold}")
        print(f"  Recommended position size: {result.confidence * 100:.1f}% of max")
    elif result.direction == "sell" and result.confidence >= confidence_threshold:
        print(f"✓ EXECUTE SELL: Confidence {result.confidence:.3f} >= {confidence_threshold}")
        print(f"  Recommended position size: {result.confidence * 100:.1f}% of max")
    elif result.direction == "hold":
        print(f"○ HOLD: No clear signal")
    else:
        print(f"○ SKIP: Confidence {result.confidence:.3f} < {confidence_threshold}")
    print()


if __name__ == "__main__":
    print("\n")
    print("╔" + "=" * 58 + "╗")
    print("║" + " " * 10 + "Multi-Timeframe Ensemble Examples" + " " * 14 + "║")
    print("╚" + "=" * 58 + "╝")
    print()
    
    example_basic_usage()
    example_majority_vote()
    example_conflict_resolution()
    example_custom_weights()
    example_equal_confidence()
    example_partial_timeframes()
    example_trading_decision()
    
    print("=" * 60)
    print("All examples completed successfully!")
    print("=" * 60)
