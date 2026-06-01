# Ensemble Prediction API Quick Reference

## Endpoint

```
POST /api/v1/ml/predict/ensemble
```

## Authentication

Requires JWT token in Authorization header:
```
Authorization: Bearer <your_jwt_token>
```

## Rate Limiting

- 10 requests per minute per user

## Request

### Parameters

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| symbol | string | Yes | Stock symbol (e.g., "AAPL", "GOOGL") |
| timeframes | array[string] | Yes | List of timeframes: "daily", "weekly", "monthly" |
| user_id | string | No | User ID for audit logging |

### Example Request

```json
{
  "symbol": "AAPL",
  "timeframes": ["daily", "weekly", "monthly"],
  "user_id": "user123"
}
```

### Validation Rules

- `timeframes` must contain at least 1 timeframe
- `timeframes` must contain at most 3 timeframes
- Valid timeframes: "daily", "weekly", "monthly"
- Invalid timeframes will return 400 Bad Request

## Response

### Success Response (200 OK)

```json
{
  "symbol": "AAPL",
  "direction": "buy",
  "confidence": 0.85,
  "timeframe_predictions": {
    "daily": {
      "timeframe": "daily",
      "direction": "buy",
      "confidence": 0.90,
      "entry_price": 150.0,
      "tp1": 155.0,
      "tp2": 160.0,
      "tp3": 165.0,
      "stop_loss": 145.0,
      "volatility": 0.02,
      "metadata": {}
    },
    "weekly": {
      "timeframe": "weekly",
      "direction": "buy",
      "confidence": 0.80,
      "entry_price": 150.0,
      "tp1": 158.0,
      "tp2": 165.0,
      "tp3": 172.0,
      "stop_loss": 142.0,
      "volatility": 0.03,
      "metadata": {}
    },
    "monthly": {
      "timeframe": "monthly",
      "direction": "hold",
      "confidence": 0.75,
      "entry_price": 150.0,
      "tp1": 160.0,
      "tp2": 170.0,
      "tp3": 180.0,
      "stop_loss": 135.0,
      "volatility": 0.05,
      "metadata": {}
    }
  },
  "confidence_breakdown": {
    "daily": 0.45,
    "weekly": 0.24,
    "monthly": 0.15
  },
  "conflict_resolved": true,
  "conflict_resolution_method": "highest_confidence",
  "metadata": {
    "timestamp": "2024-01-01T00:00:00Z",
    "timeframes_used": ["daily", "weekly", "monthly"],
    "weights": {
      "daily": 0.5,
      "weekly": 0.3,
      "monthly": 0.2
    }
  },
  "warnings": null
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| symbol | string | Stock symbol |
| direction | string | Ensemble prediction direction: "buy", "sell", or "hold" |
| confidence | float | Ensemble confidence score (0.0 to 1.0) |
| timeframe_predictions | object | Individual predictions per timeframe |
| confidence_breakdown | object | Confidence contribution per timeframe |
| conflict_resolved | boolean | Whether conflict resolution was applied |
| conflict_resolution_method | string | Method used for conflict resolution (if any) |
| metadata | object | Additional metadata including timestamp and weights |
| warnings | array[string] | Warning messages (if any) |

### Timeframe Prediction Fields

| Field | Type | Description |
|-------|------|-------------|
| timeframe | string | Timeframe name |
| direction | string | Prediction direction: "buy", "sell", or "hold" |
| confidence | float | Confidence score (0.0 to 1.0) |
| entry_price | float | Recommended entry price |
| tp1 | float | Take profit level 1 |
| tp2 | float | Take profit level 2 |
| tp3 | float | Take profit level 3 |
| stop_loss | float | Stop loss level |
| volatility | float | Volatility estimate |
| metadata | object | Additional metadata |

## Error Responses

### 400 Bad Request - Invalid Timeframes

```json
{
  "detail": "Invalid timeframes: {'invalid_timeframe'}. Valid options: {'daily', 'weekly', 'monthly'}"
}
```

### 422 Unprocessable Entity - Validation Error

```json
{
  "detail": [
    {
      "loc": ["body", "timeframes"],
      "msg": "ensure this value has at least 1 items",
      "type": "value_error.list.min_items"
    }
  ]
}
```

### 503 Service Unavailable - Model Unavailable

```json
{
  "detail": "ML model unavailable: No active production model available"
}
```

### 500 Internal Server Error - Prediction Failed

```json
{
  "detail": "Ensemble prediction failed: <error_message>"
}
```

## Caching

- Ensemble predictions are cached in Redis for 5 minutes
- Cache key format: `ml:ensemble:{symbol}:{timeframes}`
- Cache is automatically checked before generating new predictions
- Cache is invalidated when any timeframe model is updated

## Conflict Resolution

The ensemble uses the following conflict resolution strategy:

1. **Unanimous Agreement**: If all timeframes agree, use that direction
2. **Majority Vote**: If more than half agree, use majority direction
3. **Highest Confidence**: If no majority, choose direction with highest confidence
4. **Longer Timeframe**: If confidences are equal, prefer longer timeframe

When conflict resolution is applied:
- `conflict_resolved` will be `true`
- `conflict_resolution_method` will indicate the method used

## Confidence Breakdown

The confidence breakdown shows the weighted contribution of each timeframe to the final confidence score:

- **Daily**: 50% weight (0.5)
- **Weekly**: 30% weight (0.3)
- **Monthly**: 20% weight (0.2)

Example calculation:
```
daily_contribution = daily_confidence * 0.5 = 0.90 * 0.5 = 0.45
weekly_contribution = weekly_confidence * 0.3 = 0.80 * 0.3 = 0.24
monthly_contribution = monthly_confidence * 0.2 = 0.75 * 0.2 = 0.15
total_confidence = 0.45 + 0.24 + 0.15 = 0.84
```

## Graceful Degradation

If one or more timeframe predictions fail:
- The endpoint continues with available predictions
- Warnings are included in the response
- Partial ensemble prediction is returned

Example with warnings:
```json
{
  "symbol": "AAPL",
  "direction": "buy",
  "confidence": 0.85,
  "timeframe_predictions": {
    "daily": { ... }
  },
  "warnings": [
    "weekly: Prediction unavailable: Model not found",
    "monthly: Error getting monthly prediction: Connection timeout"
  ]
}
```

## Usage Examples

### Python (httpx)

```python
import httpx

async def get_ensemble_prediction(symbol: str, timeframes: list[str], token: str):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:8000/api/v1/ml/predict/ensemble",
            json={
                "symbol": symbol,
                "timeframes": timeframes,
                "user_id": "user123"
            },
            headers={"Authorization": f"Bearer {token}"}
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Error: {response.status_code} - {response.text}")

# Usage
prediction = await get_ensemble_prediction("AAPL", ["daily", "weekly"], "your_token")
print(f"Direction: {prediction['direction']}")
print(f"Confidence: {prediction['confidence']}")
```

### JavaScript (fetch)

```javascript
async function getEnsemblePrediction(symbol, timeframes, token) {
  const response = await fetch('http://localhost:8000/api/v1/ml/predict/ensemble', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${token}`
    },
    body: JSON.stringify({
      symbol: symbol,
      timeframes: timeframes,
      user_id: 'user123'
    })
  });
  
  if (!response.ok) {
    throw new Error(`Error: ${response.status} - ${await response.text()}`);
  }
  
  return await response.json();
}

// Usage
const prediction = await getEnsemblePrediction('AAPL', ['daily', 'weekly'], 'your_token');
console.log(`Direction: ${prediction.direction}`);
console.log(`Confidence: ${prediction.confidence}`);
```

### cURL

```bash
curl -X POST "http://localhost:8000/api/v1/ml/predict/ensemble" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your_token" \
  -d '{
    "symbol": "AAPL",
    "timeframes": ["daily", "weekly", "monthly"],
    "user_id": "user123"
  }'
```

## Best Practices

1. **Use All Timeframes**: For best results, include all three timeframes (daily, weekly, monthly)
2. **Check Warnings**: Always check the `warnings` field for any issues with individual timeframe predictions
3. **Monitor Confidence**: Lower confidence scores may indicate uncertainty or conflicting signals
4. **Respect Rate Limits**: Stay within the 10 requests/minute limit to avoid rate limiting errors
5. **Handle Errors**: Implement proper error handling for 503 (model unavailable) and 500 (prediction failed) responses
6. **Cache Awareness**: Be aware that predictions are cached for 5 minutes - fresh data may not be immediately available

## Monitoring

Key metrics to monitor:
- Cache hit rate
- Response times
- Error rates (400, 503, 500)
- Conflict resolution frequency
- Timeframe prediction failure rates

## Support

For issues or questions:
- Check the implementation summary: `backend/docs/TASK_29_IMPLEMENTATION_SUMMARY.md`
- Review the test cases: `backend/tests/test_ensemble_prediction_api.py`
- Check the ensemble class documentation: `backend/app/ml/ensemble/README.md`
