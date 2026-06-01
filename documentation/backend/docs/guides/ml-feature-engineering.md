# ML Feature Engineering Guide

## Overview

This guide covers the feature engineering process for the ML Prediction System, including 40+ technical indicators, feature versioning, and best practices for adding new features.

---

## Feature Categories

### 1. Momentum Indicators (10 features)

**RSI (Relative Strength Index)**
- `rsi_14`: 14-period RSI (timeframe-adjusted)
- `rsi_21`: 21-period RSI (timeframe-adjusted)
- Range: 0-100 (oversold <30, overbought >70)

**MACD (Moving Average Convergence Divergence)**
- `macd_line`: Fast EMA - Slow EMA
- `macd_signal`: Signal line (9-period EMA of MACD)
- `macd_histogram`: MACD - Signal

**Stochastic Oscillator**
- `stochastic_k`: %K line (14-period)
- `stochastic_d`: %D line (3-period SMA of %K)

**Other Momentum**
- `roc_10`: 10-period Rate of Change
- `roc_20`: 20-period Rate of Change
- `williams_r`: Williams %R (14-period)
- `cci_20`: Commodity Channel Index (20-period)
- `momentum_10`: 10-period momentum
- `tsi`: True Strength Index
- `ultimate_oscillator`: Ultimate Oscillator
- `awesome_oscillator`: Awesome Oscillator

### 2. Trend Indicators (8 features)

**Moving Averages**
- `sma_20`: 20-period Simple Moving Average
- `sma_50`: 50-period Simple Moving Average
- `sma_200`: 200-period Simple Moving Average
- `ema_12`: 12-period Exponential Moving Average
- `ema_26`: 26-period Exponential Moving Average

**Trend Strength**
- `adx_14`: Average Directional Index (14-period)
- `plus_di`: Positive Directional Indicator
- `minus_di`: Negative Directional Indicator

### 3. Volatility Indicators (6 features)

**Bollinger Bands**
- `bb_upper`: Upper band (SMA + 2σ)
- `bb_middle`: Middle band (20-period SMA)
- `bb_lower`: Lower band (SMA - 2σ)
- `bb_width`: Band width (upper - lower)

**ATR & Volatility**
- `atr_14`: Average True Range (14-period)
- `historical_volatility`: 20-period historical volatility

### 4. Volume Indicators (5 features)

- `volume_sma_20`: 20-period volume SMA
- `volume_ratio`: Current volume / SMA
- `obv`: On-Balance Volume
- `vwap`: Volume Weighted Average Price
- `mfi_14`: Money Flow Index (14-period)

### 5. Market Structure (8 features)

**Price Patterns**
- `higher_high`: Boolean (1/0)
- `higher_low`: Boolean (1/0)
- `lower_high`: Boolean (1/0)
- `lower_low`: Boolean (1/0)

**Support/Resistance**
- `distance_to_support`: % distance to nearest support
- `distance_to_resistance`: % distance to nearest resistance
- `pivot_point`: Daily pivot point
- `fibonacci_level`: Nearest Fibonacci retracement level

### 6. Price Action (5 features)

- `price_change_pct`: Daily % change
- `high_low_range`: (High - Low) / Close
- `close_position`: (Close - Low) / (High - Low)
- `gap_pct`: Gap from previous close
- `body_to_wick_ratio`: Candle body / total range

---

## Timeframe-Specific Periods

Indicator periods are automatically adjusted based on timeframe:

| Indicator | Daily (1d) | Weekly (1w) | Monthly (1M) |
|-----------|------------|-------------|--------------|
| RSI | 14 | 70 | 280 |
| MACD Fast | 12 | 60 | 240 |
| MACD Slow | 26 | 130 | 520 |
| Stochastic | 14 | 70 | 280 |
| ATR | 14 | 70 | 280 |
| ADX | 14 | 70 | 280 |

**Implementation**:
```python
from app.ml.training.timeframe_config import get_indicator_period

# Get RSI period for weekly timeframe
rsi_period = get_indicator_period("1w", "rsi")  # Returns 70
```

---

## Feature Computation

### Using TimeframeFeatureComputer

```python
from app.ml.features.timeframe_features import TimeframeFeatureComputer
import pandas as pd

# Initialize for specific timeframe
computer = TimeframeFeatureComputer(timeframe="1d")

# Compute all features
df = pd.DataFrame({
    "timestamp": [...],
    "open": [...],
    "high": [...],
    "low": [...],
    "close": [...],
    "volume": [...]
})

features_df = computer.compute_all_features(df)
# Returns DataFrame with 40+ feature columns
```

### Using FeaturePipeline

```python
from app.ml.training.feature_pipeline import FeaturePipeline

# Initialize pipeline
pipeline = FeaturePipeline(
    session=db_session,
    sequence_length=60,  # 60 time steps
    prediction_horizon=5  # Predict 5 days ahead
)

# Prepare training data
X, y = await pipeline.prepare_training_data(
    symbol="NSE_EQ|INE002A01018",
    timeframe="1d",
    start_date=datetime(2020, 1, 1),
    end_date=datetime(2024, 12, 31)
)

# X shape: (num_samples, 60, 40+)
# y keys: direction, confidence, entry_price, tp1, tp2, tp3, stop_loss, volatility
```

---

## Feature Versioning

### Version Schema

Features are versioned to ensure training-serving consistency:

```python
from app.ml.feature_versioning import FeatureVersion

version = FeatureVersion(
    version="1.0.0",
    features=[
        "rsi_14", "rsi_21", "macd_line", "macd_signal",
        "sma_20", "sma_50", "bb_upper", "bb_lower",
        # ... all 40+ features
    ],
    timeframe_configs={
        "1d": {"rsi": 14, "macd_fast": 12, "macd_slow": 26},
        "1w": {"rsi": 70, "macd_fast": 60, "macd_slow": 130},
        "1M": {"rsi": 280, "macd_fast": 240, "macd_slow": 520}
    },
    created_at=datetime.now()
)
```

### Registering Feature Versions

```python
from app.ml.feature_store import FeatureStore

store = FeatureStore(session=db_session)

# Register new version
await store.register_feature_version(
    version="1.1.0",
    features=updated_feature_list,
    timeframe_configs=updated_configs,
    description="Added TSI and Ultimate Oscillator"
)

# Get specific version
version = await store.get_feature_version("1.0.0")

# List all versions
versions = await store.list_feature_versions()
```

---

## Adding New Features

### Step 1: Define Feature Computation

Add to `app/ml/features/timeframe_features.py`:

```python
def _compute_new_indicator(
    self, df: pd.DataFrame, features_df: pd.DataFrame
) -> pd.DataFrame:
    """Compute new custom indicator."""
    close = df["close"]
    
    # Get timeframe-specific period
    period = get_indicator_period(self.timeframe, "new_indicator")
    
    # Compute indicator
    features_df["new_indicator"] = self._calculate_indicator(close, period)
    
    return features_df
```

### Step 2: Add to compute_all_features

```python
def compute_all_features(self, df: pd.DataFrame) -> pd.DataFrame:
    # ... existing code ...
    
    # Add new indicator
    features_df = self._compute_new_indicator(df, features_df)
    
    return features_df
```

### Step 3: Add Timeframe Config

In `app/ml/training/timeframe_config.py`:

```python
TIMEFRAME_CONFIGS = {
    "1d": {
        # ... existing ...
        "new_indicator": 20,
    },
    "1w": {
        # ... existing ...
        "new_indicator": 100,
    },
    "1M": {
        # ... existing ...
        "new_indicator": 400,
    }
}
```

### Step 4: Update Feature Version

```python
# Register new version with updated feature list
await store.register_feature_version(
    version="1.2.0",
    features=[...existing_features, "new_indicator"],
    timeframe_configs=updated_configs,
    description="Added new_indicator"
)
```

### Step 5: Retrain Models

```python
# Train with new features
from app.ml.training.training_pipeline import TrainingPipeline

pipeline = TrainingPipeline(session=db_session)
model_id = await pipeline.train_model(
    symbol="NSE_EQ|INE002A01018",
    timeframe="1d",
    feature_version="1.2.0"  # Use new version
)
```

---

## Best Practices

### 1. Feature Scaling
All features are automatically normalized to [0, 1] range during training.

### 2. Missing Values
- Forward-fill for price-based features
- Zero-fill for volume-based features
- Drop rows with >10% missing values

### 3. Feature Selection
Use correlation analysis to remove redundant features:

```python
# Compute correlation matrix
corr_matrix = features_df.corr()

# Remove features with correlation > 0.95
high_corr = (corr_matrix.abs() > 0.95) & (corr_matrix != 1.0)
to_drop = [col for col in high_corr.columns if high_corr[col].any()]
```

### 4. Lookback Periods
- Short-term: 10-20 periods
- Medium-term: 50-100 periods
- Long-term: 200+ periods

### 5. Testing New Features
Always backtest new features before production:

```python
# Split data
train_end = int(len(df) * 0.8)
train_df = df[:train_end]
test_df = df[train_end:]

# Train with new features
model = train_model(train_df, features=new_feature_list)

# Evaluate on test set
metrics = evaluate_model(model, test_df)
print(f"Accuracy: {metrics['accuracy']:.2%}")
```

---

## Feature Importance

After training, analyze feature importance:

```python
from app.ml.training.training_pipeline import TrainingPipeline

pipeline = TrainingPipeline(session=db_session)

# Get feature importance
importance = await pipeline.get_feature_importance(model_id)

# Top 10 features
top_features = sorted(
    importance.items(),
    key=lambda x: x[1],
    reverse=True
)[:10]

for feature, score in top_features:
    print(f"{feature}: {score:.4f}")
```

---

## Troubleshooting

### Issue: Features contain NaN values
**Solution**: Increase minimum data requirement or adjust lookback periods.

### Issue: Features not updating in production
**Solution**: Verify feature version matches between training and serving.

### Issue: High correlation between features
**Solution**: Use feature selection to remove redundant features.

### Issue: Poor model performance
**Solution**: Check feature distributions, add domain-specific features, or adjust timeframe periods.

---

## References

- **Feature Definitions**: `app/ml/features/timeframe_features.py`
- **Feature Pipeline**: `app/ml/training/feature_pipeline.py`
- **Timeframe Config**: `app/ml/training/timeframe_config.py`
- **Feature Store**: `app/ml/feature_store.py`
- **Feature Versioning**: `app/ml/feature_versioning.py`

---

**Last Updated**: 2026-04-09  
**Version**: 1.0.0
