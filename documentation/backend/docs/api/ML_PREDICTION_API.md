# ML Prediction API Documentation

## Overview

The ML Prediction API provides real-time trading signal predictions using LSTM + Transformer hybrid models. All endpoints require JWT authentication and are rate-limited.

**Base URL**: `/api/v1/ml`

**Authentication**: Bearer token (JWT)

**Rate Limits**:
- `/predict`: 10 requests/minute per user
- `/predict/batch`: 5 requests/minute per user
- `/predict/ensemble`: 5 requests/minute per user

---

## Endpoints

### 1. Single Prediction

**POST** `/api/v1/ml/predict`

Generate ML prediction for a single symbol.

#### Request

**Headers**:
```
Authorization: Bearer <jwt_token>
Content-Type: application/json
```

**Body**:
```json
{
  "symbol": "BTCUSDT",
  "timeframe": "1h",
  "model_version": "1.0.0"
}
```

**Parameters**:
- `symbol` (string, required): Trading pair symbol (e.g., "BTCUSDT", "ETHUSDT")
- `timeframe` (string, required): Timeframe for prediction
  - Valid values: "1m", "5m", "15m", "1h", "4h", "1d"
- `model_version` (string, optional): Specific model version to use
  - Default: Latest production model

#### Response

**Status**: `200 OK`

```json
{
  "symbol": "BTCUSDT",
  "timeframe": "1h",
  "direction": "BUY",
  "confidence": 0.85,
  "entry_price": 45000.50,
  "stop_loss": 44500.00,
  "take_profit_1": 45500.00,
  "take_profit_2": 46000.00,
  "take_profit_3": 46500.00,
  "volatility_estimate": 2.5,
  "model_version": "1.0.0",
  "timestamp": "2024-01-15T10:30:00Z",
  "shap_explanation": {
    "top_features": [
      {
        "name": "rsi_14",
        "value": 70.5,
        "importance": 0.25
      },
      {
        "name": "macd_signal",
        "value": 0.15,
        "importance": 0.18
      },
      {
        "name": "bb_position",
        "value": 0.85,
        "importance": 0.15
      }
    ],
    "explanation_text": "Strong bullish signal driven by RSI overbought (70.5), positive MACD crossover, and price near upper Bollinger Band."
  }
}
```

**Response Fields**:
- `direction` (string): Trading direction - "BUY", "SELL", or "HOLD"
- `confidence` (float): Model confidence score [0.0 - 1.0]
- `entry_price` (float): Recommended entry price
- `stop_loss` (float): Stop loss price
- `take_profit_1/2/3` (float): Three take profit levels
- `volatility_estimate` (float): Expected volatility percentage
- `shap_explanation` (object): SHAP-based feature importance explanation

#### Error Responses

**429 Too Many Requests**:
```json
{
  "error": "rate_limit_exceeded",
  "message": "Rate limit exceeded: 10/minute per user",
  "limit": 10,
  "window": "minute",
  "retry_after": 45
}
```

**401 Unauthorized**:
```json
{
  "detail": "Not authenticated"
}
```

**503 Service Unavailable**:
```json
{
  "error": "model_unavailable",
  "message": "ML model is currently unavailable",
  "fallback": "cached_prediction"
}
```

---

### 2. Batch Prediction

**POST** `/api/v1/ml/predict/batch`

Generate predictions for multiple symbols in a single request.

#### Request

**Headers**:
```
Authorization: Bearer <jwt_token>
Content-Type: application/json
```

**Body**:
```json
{
  "symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
  "timeframe": "1h",
  "model_version": "1.0.0"
}
```

**Parameters**:
- `symbols` (array[string], required): List of trading pairs (max 10)
- `timeframe` (string, required): Timeframe for all predictions
- `model_version` (string, optional): Model version to use

#### Response

**Status**: `200 OK`

```json
{
  "predictions": [
    {
      "symbol": "BTCUSDT",
      "direction": "BUY",
      "confidence": 0.85,
      "entry_price": 45000.50,
      "stop_loss": 44500.00,
      "take_profit_1": 45500.00,
      "take_profit_2": 46000.00,
      "take_profit_3": 46500.00,
      "volatility_estimate": 2.5
    },
    {
      "symbol": "ETHUSDT",
      "direction": "SELL",
      "confidence": 0.78,
      "entry_price": 2500.00,
      "stop_loss": 2550.00,
      "take_profit_1": 2450.00,
      "take_profit_2": 2400.00,
      "take_profit_3": 2350.00,
      "volatility_estimate": 3.2
    }
  ],
  "timeframe": "1h",
  "model_version": "1.0.0",
  "timestamp": "2024-01-15T10:30:00Z",
  "total_predictions": 2,
  "successful": 2,
  "failed": 0
}
```

---

### 3. Ensemble Prediction

**POST** `/api/v1/ml/predict/ensemble`

Generate ensemble prediction combining multiple timeframes.

#### Request

**Headers**:
```
Authorization: Bearer <jwt_token>
Content-Type: application/json
```

**Body**:
```json
{
  "symbol": "BTCUSDT",
  "timeframes": ["1m", "5m", "15m", "1h", "4h", "1d"],
  "model_version": "1.0.0"
}
```

**Parameters**:
- `symbol` (string, required): Trading pair symbol
- `timeframes` (array[string], optional): Timeframes to include
  - Default: All timeframes ["1m", "5m", "15m", "1h", "4h", "1d"]
- `model_version` (string, optional): Model version to use

#### Response

**Status**: `200 OK`

```json
{
  "symbol": "BTCUSDT",
  "ensemble_direction": "BUY",
  "ensemble_confidence": 0.87,
  "entry_price": 45000.50,
  "stop_loss": 44500.00,
  "take_profit_1": 45500.00,
  "take_profit_2": 46000.00,
  "take_profit_3": 46500.00,
  "timeframe_predictions": {
    "1m": {
      "direction": "BUY",
      "confidence": 0.75,
      "weight": 0.05
    },
    "5m": {
      "direction": "BUY",
      "confidence": 0.80,
      "weight": 0.10
    },
    "15m": {
      "direction": "BUY",
      "confidence": 0.85,
      "weight": 0.15
    },
    "1h": {
      "direction": "SELL",
      "confidence": 0.70,
      "weight": 0.20
    },
    "4h": {
      "direction": "BUY",
      "confidence": 0.90,
      "weight": 0.25
    },
    "1d": {
      "direction": "BUY",
      "confidence": 0.95,
      "weight": 0.25
    }
  },
  "conflict_detected": true,
  "conflict_resolution": "weighted_vote",
  "model_version": "1.0.0",
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Response Fields**:
- `ensemble_direction` (string): Final ensemble direction (weighted vote)
- `ensemble_confidence` (float): Weighted average confidence
- `timeframe_predictions` (object): Individual predictions per timeframe
- `conflict_detected` (boolean): Whether timeframes disagreed
- `conflict_resolution` (string): Method used to resolve conflicts

---

## Authentication

All endpoints require JWT authentication via Bearer token.

### Obtaining a Token

**POST** `/api/v1/auth/login`

```json
{
  "username": "your_username",
  "password": "your_password"
}
```

**Response**:
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 3600
}
```

### Using the Token

Include the token in the `Authorization` header:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

---

## Rate Limiting

Rate limits are enforced using **hybrid rate limiting** (both IP address and user ID).

### Limits

| Endpoint | User Limit | IP Limit |
|----------|------------|----------|
| `/predict` | 10/minute | 20/minute |
| `/predict/batch` | 5/minute | 10/minute |
| `/predict/ensemble` | 5/minute | 10/minute |

### Rate Limit Headers

Responses include rate limit information:

```
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 7
X-RateLimit-Reset: 1642248600
```

### Handling Rate Limits

When rate limited, the API returns `429 Too Many Requests` with a `Retry-After` header:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 45

{
  "error": "rate_limit_exceeded",
  "message": "Rate limit exceeded: 10/minute per user",
  "retry_after": 45
}
```

**Best Practice**: Implement exponential backoff when receiving 429 responses.

---

## Error Codes

| Status Code | Error Type | Description |
|-------------|------------|-------------|
| 400 | `invalid_request` | Invalid request parameters |
| 401 | `unauthorized` | Missing or invalid authentication |
| 403 | `forbidden` | Insufficient permissions |
| 404 | `not_found` | Resource not found |
| 429 | `rate_limit_exceeded` | Rate limit exceeded |
| 500 | `internal_error` | Internal server error |
| 503 | `service_unavailable` | Service temporarily unavailable |

---

## Examples

### cURL Examples

**Single Prediction**:
```bash
curl -X POST https://api.cortex.ai/api/v1/ml/predict \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "BTCUSDT",
    "timeframe": "1h"
  }'
```

**Batch Prediction**:
```bash
curl -X POST https://api.cortex.ai/api/v1/ml/predict/batch \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
    "timeframe": "1h"
  }'
```

**Ensemble Prediction**:
```bash
curl -X POST https://api.cortex.ai/api/v1/ml/predict/ensemble \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "BTCUSDT",
    "timeframes": ["1h", "4h", "1d"]
  }'
```

### Python Client Examples

```python
import requests

# Configuration
API_BASE = "https://api.cortex.ai/api/v1"
TOKEN = "your_jwt_token"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

# Single Prediction
response = requests.post(
    f"{API_BASE}/ml/predict",
    headers=headers,
    json={
        "symbol": "BTCUSDT",
        "timeframe": "1h"
    }
)

prediction = response.json()
print(f"Direction: {prediction['direction']}")
print(f"Confidence: {prediction['confidence']}")
print(f"Entry: {prediction['entry_price']}")

# Batch Prediction
response = requests.post(
    f"{API_BASE}/ml/predict/batch",
    headers=headers,
    json={
        "symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
        "timeframe": "1h"
    }
)

batch_predictions = response.json()
for pred in batch_predictions['predictions']:
    print(f"{pred['symbol']}: {pred['direction']} ({pred['confidence']:.2f})")

# Ensemble Prediction
response = requests.post(
    f"{API_BASE}/ml/predict/ensemble",
    headers=headers,
    json={
        "symbol": "BTCUSDT",
        "timeframes": ["1h", "4h", "1d"]
    }
)

ensemble = response.json()
print(f"Ensemble Direction: {ensemble['ensemble_direction']}")
print(f"Ensemble Confidence: {ensemble['ensemble_confidence']}")
print(f"Conflict Detected: {ensemble['conflict_detected']}")
```

---

## Best Practices

1. **Cache Predictions**: Predictions are cached for 5 minutes. Avoid redundant requests.

2. **Handle Rate Limits**: Implement exponential backoff when receiving 429 responses.

3. **Use Batch Endpoints**: For multiple symbols, use `/predict/batch` instead of multiple single requests.

4. **Monitor Confidence**: Only act on predictions with confidence > 0.7.

5. **Interpret SHAP**: Use SHAP explanations to understand model reasoning.

6. **Handle Errors Gracefully**: Implement fallback logic for 503 errors.

7. **Validate Responses**: Always check `direction` and `confidence` before trading.

---

## Support

For API support, contact: api-support@cortex.ai

For technical documentation, visit: https://docs.cortex.ai
