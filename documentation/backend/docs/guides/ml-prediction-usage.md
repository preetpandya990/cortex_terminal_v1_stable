# ML Prediction Usage Guide

## Overview

This guide covers how to use the ML Prediction API for real-time trading signals, including single predictions, batch predictions, ensemble predictions, and SHAP explanations.

---

## Quick Start

### 1. Authentication

All API requests require JWT authentication:

```bash
# Get access token
curl -X POST "http://localhost:8000/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{
    "username": "your_username",
    "password": "your_password"
  }'

# Response
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer"
}
```

### 2. Single Prediction

```bash
curl -X POST "http://localhost:8000/api/v1/ml/predict" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "NSE_EQ|INE002A01018",
    "timeframe": "1d",
    "model_version": "1.0.0"
  }'
```

**Response**:
```json
{
  "prediction_id": "pred_abc123",
  "symbol": "NSE_EQ|INE002A01018",
  "timeframe": "1d",
  "timestamp": "2026-04-09T15:30:00Z",
  "direction": "BUY",
  "confidence": 0.87,
  "entry_price": 2450.50,
  "targets": {
    "tp1": 2475.00,
    "tp2": 2500.00,
    "tp3": 2525.00
  },
  "stop_loss": 2425.00,
  "risk_reward_ratio": 3.0,
  "volatility": 0.025,
  "model_version": "1.0.0",
  "latency_ms": 45
}
```

---

## Python Client

### Installation

```bash
pip install requests
```

### Basic Usage

```python
import requests
from typing import Dict, Any

class MLPredictionClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
    
    def predict(
        self,
        symbol: str,
        timeframe: str,
        model_version: str = "latest"
    ) -> Dict[str, Any]:
        """Get single prediction."""
        response = requests.post(
            f"{self.base_url}/api/v1/ml/predict",
            headers=self.headers,
            json={
                "symbol": symbol,
                "timeframe": timeframe,
                "model_version": model_version
            }
        )
        response.raise_for_status()
        return response.json()
    
    def batch_predict(
        self,
        requests_list: list[Dict[str, str]]
    ) -> Dict[str, Any]:
        """Get batch predictions."""
        response = requests.post(
            f"{self.base_url}/api/v1/ml/predict/batch",
            headers=self.headers,
            json={"requests": requests_list}
        )
        response.raise_for_status()
        return response.json()
    
    def ensemble_predict(
        self,
        symbol: str,
        timeframe: str,
        model_versions: list[str]
    ) -> Dict[str, Any]:
        """Get ensemble prediction."""
        response = requests.post(
            f"{self.base_url}/api/v1/ml/predict/ensemble",
            headers=self.headers,
            json={
                "symbol": symbol,
                "timeframe": timeframe,
                "model_versions": model_versions
            }
        )
        response.raise_for_status()
        return response.json()

# Usage
client = MLPredictionClient(
    base_url="http://localhost:8000",
    token="YOUR_TOKEN"
)

# Single prediction
result = client.predict(
    symbol="NSE_EQ|INE002A01018",
    timeframe="1d"
)
print(f"Direction: {result['direction']}")
print(f"Confidence: {result['confidence']:.2%}")
print(f"Entry: {result['entry_price']}")
```

---

## Prediction Types

### 1. Single Prediction

**Use Case**: Real-time trading signals for a single asset

**Endpoint**: `POST /api/v1/ml/predict`

**Request**:
```json
{
  "symbol": "NSE_EQ|INE002A01018",
  "timeframe": "1d",
  "model_version": "1.0.0"
}
```

**Rate Limit**: 10 requests/minute

**Example**:
```python
result = client.predict(
    symbol="NSE_EQ|INE002A01018",
    timeframe="1d"
)

if result["direction"] == "BUY" and result["confidence"] > 0.85:
    print(f"Strong BUY signal at {result['entry_price']}")
    print(f"Targets: {result['targets']}")
    print(f"Stop Loss: {result['stop_loss']}")
```

### 2. Batch Prediction

**Use Case**: Get predictions for multiple assets at once

**Endpoint**: `POST /api/v1/ml/predict/batch`

**Request**:
```json
{
  "requests": [
    {
      "symbol": "NSE_EQ|INE002A01018",
      "timeframe": "1d",
      "model_version": "1.0.0"
    },
    {
      "symbol": "NSE_EQ|INE009A01021",
      "timeframe": "1d",
      "model_version": "1.0.0"
    }
  ]
}
```

**Rate Limit**: 5 requests/minute

**Example**:
```python
watchlist = [
    "NSE_EQ|INE002A01018",  # Reliance
    "NSE_EQ|INE009A01021",  # Infosys
    "NSE_EQ|INE040A01034"   # HDFC Bank
]

requests_list = [
    {"symbol": symbol, "timeframe": "1d"}
    for symbol in watchlist
]

results = client.batch_predict(requests_list)

# Filter strong signals
strong_signals = [
    pred for pred in results["predictions"]
    if pred["confidence"] > 0.85 and pred["direction"] == "BUY"
]

print(f"Found {len(strong_signals)} strong BUY signals")
```

### 3. Ensemble Prediction

**Use Case**: Combine multiple models for higher confidence

**Endpoint**: `POST /api/v1/ml/predict/ensemble`

**Request**:
```json
{
  "symbol": "NSE_EQ|INE002A01018",
  "timeframe": "1d",
  "model_versions": ["1.0.0", "1.1.0", "1.2.0"],
  "strategy": "voting"
}
```

**Rate Limit**: 5 requests/minute

**Example**:
```python
result = client.ensemble_predict(
    symbol="NSE_EQ|INE002A01018",
    timeframe="1d",
    model_versions=["1.0.0", "1.1.0", "1.2.0"]
)

print(f"Ensemble Direction: {result['direction']}")
print(f"Ensemble Confidence: {result['confidence']:.2%}")
print(f"Individual Predictions: {result['individual_predictions']}")
```

---

## SHAP Explanations

### Understanding Feature Importance

SHAP (SHapley Additive exPlanations) values show which features contributed to the prediction.

**Request with Explanations**:
```python
result = client.predict(
    symbol="NSE_EQ|INE002A01018",
    timeframe="1d"
)

# SHAP values included in response
shap_values = result.get("shap_values", {})

# Top contributing features
top_features = sorted(
    shap_values.items(),
    key=lambda x: abs(x[1]),
    reverse=True
)[:10]

print("Top 10 contributing features:")
for feature, value in top_features:
    direction = "↑" if value > 0 else "↓"
    print(f"{feature}: {value:.4f} {direction}")
```

**Example Output**:
```
Top 10 contributing features:
rsi_14: 0.1234 ↑
macd_histogram: 0.0987 ↑
sma_50: -0.0765 ↓
volume_ratio: 0.0654 ↑
bb_width: 0.0543 ↑
adx_14: 0.0432 ↑
stochastic_k: -0.0321 ↓
atr_14: 0.0298 ↑
cci_20: 0.0276 ↑
roc_10: 0.0254 ↑
```

### Visualizing SHAP Values

```python
import matplotlib.pyplot as plt

def plot_shap_values(shap_values: dict, top_n: int = 15):
    """Plot top N SHAP values."""
    # Sort by absolute value
    sorted_features = sorted(
        shap_values.items(),
        key=lambda x: abs(x[1]),
        reverse=True
    )[:top_n]
    
    features = [f[0] for f in sorted_features]
    values = [f[1] for f in sorted_features]
    
    # Create bar plot
    colors = ['red' if v < 0 else 'green' for v in values]
    plt.barh(features, values, color=colors)
    plt.xlabel('SHAP Value')
    plt.title('Feature Importance (SHAP)')
    plt.tight_layout()
    plt.show()

# Usage
result = client.predict("NSE_EQ|INE002A01018", "1d")
plot_shap_values(result["shap_values"])
```

---

## Interpreting Predictions

### Direction

| Value | Meaning | Action |
|-------|---------|--------|
| `SELL` | Bearish signal | Consider short/exit long |
| `HOLD` | Neutral signal | No action |
| `BUY` | Bullish signal | Consider long entry |

### Confidence

| Range | Interpretation |
|-------|----------------|
| 0.90 - 1.00 | Very high confidence |
| 0.80 - 0.89 | High confidence |
| 0.70 - 0.79 | Moderate confidence |
| 0.60 - 0.69 | Low confidence |
| < 0.60 | Very low confidence |

**Recommendation**: Only act on predictions with confidence > 0.80

### Risk-Reward Ratio

```python
rr_ratio = result["risk_reward_ratio"]

if rr_ratio >= 3.0:
    print("Excellent risk-reward (3:1 or better)")
elif rr_ratio >= 2.0:
    print("Good risk-reward (2:1 or better)")
elif rr_ratio >= 1.5:
    print("Acceptable risk-reward")
else:
    print("Poor risk-reward - skip trade")
```

### Volatility

```python
volatility = result["volatility"]

if volatility > 0.03:
    print("High volatility - use wider stops")
elif volatility > 0.02:
    print("Moderate volatility")
else:
    print("Low volatility - tighter stops possible")
```

---

## Trading Strategies

### Strategy 1: High Confidence Filter

```python
def get_high_confidence_signals(watchlist: list[str]) -> list[dict]:
    """Get only high-confidence signals."""
    signals = []
    
    for symbol in watchlist:
        result = client.predict(symbol, "1d")
        
        if (result["confidence"] > 0.85 and 
            result["risk_reward_ratio"] >= 2.0):
            signals.append(result)
    
    return signals

# Usage
signals = get_high_confidence_signals(watchlist)
print(f"Found {len(signals)} high-confidence signals")
```

### Strategy 2: Multi-Timeframe Confirmation

```python
def get_multi_timeframe_signal(symbol: str) -> dict:
    """Get signal with multi-timeframe confirmation."""
    timeframes = ["1d", "1w"]
    predictions = []
    
    for tf in timeframes:
        result = client.predict(symbol, tf)
        predictions.append(result)
    
    # Check if all timeframes agree
    directions = [p["direction"] for p in predictions]
    if len(set(directions)) == 1:  # All agree
        return {
            "symbol": symbol,
            "direction": directions[0],
            "confidence": sum(p["confidence"] for p in predictions) / len(predictions),
            "confirmed": True
        }
    else:
        return {"symbol": symbol, "confirmed": False}

# Usage
signal = get_multi_timeframe_signal("NSE_EQ|INE002A01018")
if signal["confirmed"]:
    print(f"Multi-timeframe confirmation: {signal['direction']}")
```

### Strategy 3: Ensemble with Threshold

```python
def get_ensemble_signal(symbol: str, min_confidence: float = 0.85) -> dict:
    """Get ensemble signal with confidence threshold."""
    result = client.ensemble_predict(
        symbol=symbol,
        timeframe="1d",
        model_versions=["1.0.0", "1.1.0", "1.2.0"]
    )
    
    if result["confidence"] >= min_confidence:
        return {
            "symbol": symbol,
            "direction": result["direction"],
            "confidence": result["confidence"],
            "entry_price": result["entry_price"],
            "targets": result["targets"],
            "stop_loss": result["stop_loss"],
            "action": "TRADE"
        }
    else:
        return {"symbol": symbol, "action": "SKIP"}

# Usage
signal = get_ensemble_signal("NSE_EQ|INE002A01018")
if signal["action"] == "TRADE":
    print(f"Trade signal: {signal['direction']} at {signal['entry_price']}")
```

---

## Error Handling

### Common Errors

**401 Unauthorized**:
```python
try:
    result = client.predict(symbol, timeframe)
except requests.HTTPError as e:
    if e.response.status_code == 401:
        print("Token expired - refresh authentication")
        # Refresh token logic
```

**429 Rate Limit Exceeded**:
```python
import time

def predict_with_retry(symbol: str, timeframe: str, max_retries: int = 3):
    """Predict with automatic retry on rate limit."""
    for attempt in range(max_retries):
        try:
            return client.predict(symbol, timeframe)
        except requests.HTTPError as e:
            if e.response.status_code == 429:
                wait_time = 2 ** attempt  # Exponential backoff
                print(f"Rate limited - waiting {wait_time}s")
                time.sleep(wait_time)
            else:
                raise
    raise Exception("Max retries exceeded")
```

**404 Model Not Found**:
```python
try:
    result = client.predict(symbol, timeframe, model_version="2.0.0")
except requests.HTTPError as e:
    if e.response.status_code == 404:
        print("Model version not found - using latest")
        result = client.predict(symbol, timeframe)
```

---

## Best Practices

### 1. Cache Predictions

```python
from functools import lru_cache
from datetime import datetime, timedelta

class CachedMLClient:
    def __init__(self, client: MLPredictionClient):
        self.client = client
        self.cache = {}
        self.cache_ttl = timedelta(minutes=5)
    
    def predict(self, symbol: str, timeframe: str):
        cache_key = f"{symbol}:{timeframe}"
        
        # Check cache
        if cache_key in self.cache:
            cached_result, cached_time = self.cache[cache_key]
            if datetime.now() - cached_time < self.cache_ttl:
                return cached_result
        
        # Fetch new prediction
        result = self.client.predict(symbol, timeframe)
        self.cache[cache_key] = (result, datetime.now())
        return result
```

### 2. Batch Processing

```python
def process_watchlist_efficiently(watchlist: list[str]):
    """Process watchlist using batch API."""
    # Use batch API instead of individual requests
    requests_list = [
        {"symbol": symbol, "timeframe": "1d"}
        for symbol in watchlist
    ]
    
    results = client.batch_predict(requests_list)
    return results["predictions"]
```

### 3. Logging

```python
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def predict_with_logging(symbol: str, timeframe: str):
    """Predict with comprehensive logging."""
    logger.info(f"Requesting prediction: {symbol} [{timeframe}]")
    
    try:
        result = client.predict(symbol, timeframe)
        logger.info(
            f"Prediction received: {result['direction']} "
            f"(confidence: {result['confidence']:.2%})"
        )
        return result
    except Exception as e:
        logger.error(f"Prediction failed: {e}")
        raise
```

### 4. Monitoring

```python
from dataclasses import dataclass
from typing import List

@dataclass
class PredictionMetrics:
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    avg_latency_ms: float = 0.0
    
    def record_success(self, latency_ms: float):
        self.total_requests += 1
        self.successful_requests += 1
        self.avg_latency_ms = (
            (self.avg_latency_ms * (self.total_requests - 1) + latency_ms)
            / self.total_requests
        )
    
    def record_failure(self):
        self.total_requests += 1
        self.failed_requests += 1

metrics = PredictionMetrics()

# Track metrics
start = time.time()
try:
    result = client.predict(symbol, timeframe)
    latency = (time.time() - start) * 1000
    metrics.record_success(latency)
except:
    metrics.record_failure()
```

---

## References

- **API Documentation**: `backend/docs/api/ML_PREDICTION_API.md`
- **Architecture**: `backend/docs/architecture/ML_SYSTEM_ARCHITECTURE.md`
- **Deployment**: `backend/docs/runbooks/ML_MODEL_DEPLOYMENT.md`

---

**Last Updated**: 2026-04-09  
**Version**: 1.0.0
