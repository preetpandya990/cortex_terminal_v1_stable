# Multi-Timeframe Ensemble

## Overview

The `MultiTimeframeEnsemble` class combines predictions from multiple timeframe models (daily, weekly, monthly) to produce more robust trading signals. It uses weighted averaging for confidence scores and majority voting with intelligent conflict resolution for direction predictions.

## Features

- **Weighted Confidence Averaging**: Combines confidence scores using configurable weights
- **Majority Voting**: Determines final direction based on agreement across timeframes
- **Conflict Resolution**: Handles disagreements using confidence-based and timeframe-based rules
- **Audit Logging**: Records all conflict resolution decisions for compliance

## Requirements

Implements requirements:
- **4.1**: Weighted ensemble combining multiple timeframe predictions
- **4.4**: Majority voting with conflict resolution
- **9.3**: Audit logging for conflict resolution decisions

## Usage

### Basic Usage

```python
from backend.app.ml.ensemble import MultiTimeframeEnsemble, TimeframePrediction

# Initialize ensemble with default weights
ensemble = MultiTimeframeEnsemble()

# Create predictions from different timeframes
predictions = {
    "daily": TimeframePrediction(
        timeframe="daily",
        direction="buy",
        confidence=0.8
    ),
    "weekly": TimeframePrediction(
        timeframe="weekly",
        direction="buy",
        confidence=0.6
    ),
    "monthly": TimeframePrediction(
        timeframe="monthly",
        direction="sell",
        confidence=0.7
    )
}

# Get ensemble prediction
result = ensemble.predict(predictions)

print(f"Direction: {result.direction}")
print(f"Confidence: {result.confidence:.3f}")
print(f"Conflict Resolved: {result.conflict_resolved}")
```

### Custom Weights

```python
# Initialize with custom weights (must sum to 1.0)
ensemble = MultiTimeframeEnsemble(
    daily_weight=0.6,    # Higher weight for recent data
    weekly_weight=0.3,
    monthly_weight=0.1
)
```

### With Audit Logging

```python
from backend.app.ml.audit import AuditLogger

# Initialize with audit logger
audit_logger = AuditLogger(db_session=db)
ensemble = MultiTimeframeEnsemble(audit_logger=audit_logger)

# Conflicts will be automatically logged
result = ensemble.predict(predictions)
```

## How It Works

### 1. Confidence Combination

Confidence scores are combined using weighted averaging:

```
combined_confidence = (daily_conf * 0.5) + (weekly_conf * 0.3) + (monthly_conf * 0.2)
```

Default weights:
- **Daily**: 0.5 (most recent, highest weight)
- **Weekly**: 0.3 (medium-term trend)
- **Monthly**: 0.2 (long-term trend)

### 2. Direction Resolution

The final direction is determined using majority voting with conflict resolution:

#### Case 1: Unanimous Agreement
All timeframes agree → Use unanimous direction (no conflict)

```python
daily: buy, weekly: buy, monthly: buy → Result: buy
```

#### Case 2: Clear Majority
More than half agree → Use majority direction (no conflict)

```python
daily: buy, weekly: buy, monthly: sell → Result: buy (2/3 majority)
```

#### Case 3: No Majority - Highest Confidence
No clear majority → Choose direction with highest confidence (conflict resolved)

```python
daily: buy (0.6), weekly: sell (0.9), monthly: hold (0.5)
→ Result: sell (highest confidence)
```

#### Case 4: Equal Confidence - Longer Timeframe
Confidences are equal → Default to longer timeframe (conflict resolved)

```python
daily: buy (0.7), weekly: sell (0.7), monthly: hold (0.7)
→ Result: hold (longest timeframe)
```

### 3. Conflict Resolution Logging

When conflicts are resolved, the system logs:
- All timeframe predictions (direction + confidence)
- Final resolved direction
- Resolution method used
- Chosen timeframe
- Weights applied

This ensures full auditability for regulatory compliance.

## API Reference

### Classes

#### `TimeframePrediction`

Represents a prediction from a single timeframe model.

**Attributes:**
- `timeframe` (str): Timeframe identifier ("daily", "weekly", "monthly")
- `direction` (str): Predicted direction ("buy", "sell", "hold")
- `confidence` (float): Confidence score (0.0 to 1.0)
- `metadata` (Optional[Dict]): Additional metadata

#### `EnsemblePrediction`

Combined prediction from multiple timeframes.

**Attributes:**
- `direction` (str): Final predicted direction
- `confidence` (float): Combined confidence score
- `timeframe_predictions` (Dict): Original predictions by timeframe
- `conflict_resolved` (bool): Whether conflict resolution was needed
- `conflict_resolution_method` (Optional[str]): Method used for resolution
- `metadata` (Optional[Dict]): Additional metadata including timestamp and weights

#### `MultiTimeframeEnsemble`

Main ensemble class for combining predictions.

**Methods:**

##### `__init__(daily_weight=0.5, weekly_weight=0.3, monthly_weight=0.2, audit_logger=None)`

Initialize the ensemble.

**Parameters:**
- `daily_weight` (float): Weight for daily predictions (default: 0.5)
- `weekly_weight` (float): Weight for weekly predictions (default: 0.3)
- `monthly_weight` (float): Weight for monthly predictions (default: 0.2)
- `audit_logger` (Optional): Audit logger instance for conflict logging

**Raises:**
- `ValueError`: If weights don't sum to 1.0

##### `predict(predictions: Dict[str, TimeframePrediction]) -> EnsemblePrediction`

Generate ensemble prediction from multiple timeframes.

**Parameters:**
- `predictions` (Dict): Dictionary mapping timeframe to prediction

**Returns:**
- `EnsemblePrediction`: Combined prediction with metadata

**Raises:**
- `ValueError`: If no predictions provided or invalid timeframes

##### `combine_predictions(predictions: Dict[str, TimeframePrediction]) -> float`

Combine confidence scores using weighted average.

**Parameters:**
- `predictions` (Dict): Dictionary of timeframe predictions

**Returns:**
- `float`: Weighted average confidence score

## Testing

The ensemble includes comprehensive unit tests covering:

- Default and custom weight initialization
- Weight validation
- Weighted confidence averaging
- Unanimous agreement scenarios
- Majority voting scenarios
- Conflict resolution (highest confidence)
- Conflict resolution (longer timeframe default)
- Edge cases (empty predictions, single timeframe, all hold, zero/max confidence)
- Metadata inclusion
- Audit logging integration

Run tests:

```bash
pytest backend/app/ml/ensemble/test_multi_timeframe_ensemble.py -v
```

## Integration Example

```python
from backend.app.ml.ensemble import MultiTimeframeEnsemble, TimeframePrediction
from backend.app.ml.inference import create_prediction_engine
from backend.app.ml.audit import AuditLogger

# Initialize components
audit_logger = AuditLogger(db_session=db)
ensemble = MultiTimeframeEnsemble(audit_logger=audit_logger)

# Get predictions from different timeframe models
daily_model = create_prediction_engine("daily_model_v1")
weekly_model = create_prediction_engine("weekly_model_v1")
monthly_model = create_prediction_engine("monthly_model_v1")

# Generate predictions
daily_pred = daily_model.predict(features)
weekly_pred = weekly_model.predict(features)
monthly_pred = monthly_model.predict(features)

# Combine using ensemble
predictions = {
    "daily": TimeframePrediction("daily", daily_pred.direction, daily_pred.confidence),
    "weekly": TimeframePrediction("weekly", weekly_pred.direction, weekly_pred.confidence),
    "monthly": TimeframePrediction("monthly", monthly_pred.direction, monthly_pred.confidence)
}

final_prediction = ensemble.predict(predictions)

# Use final prediction for trading decision
if final_prediction.direction == "buy" and final_prediction.confidence > 0.7:
    execute_trade(symbol, "buy", confidence=final_prediction.confidence)
```

## Performance Considerations

- **Lightweight**: Ensemble logic is pure Python with minimal overhead
- **No I/O in Critical Path**: Audit logging is designed to be async-compatible
- **Stateless**: Each prediction is independent, allowing for easy parallelization
- **Memory Efficient**: Uses dataclasses for minimal memory footprint

## Future Enhancements

Potential improvements:
- Dynamic weight adjustment based on recent model performance
- Support for additional timeframes (hourly, quarterly)
- Confidence calibration based on historical accuracy
- Advanced conflict resolution strategies (e.g., Bayesian combination)
- Real-time weight optimization using reinforcement learning
