# Prometheus Metrics Quick Reference

## Import

```python
from backend.app.ml.monitoring import (
    # Context managers
    track_prediction_latency,
    track_feature_computation,
    track_model_inference,
    track_shap_computation,
    track_active_prediction,
    # Recording functions
    record_prediction_request,
    update_model_accuracy,
    update_cache_hit_rate,
    record_feature_error,
    record_model_load_failure,
    # Decorator
    track_prediction_metrics,
)
```

## Quick Usage

### Option 1: Decorator (Easiest)
```python
@track_prediction_metrics()
async def predict(symbol: str, timeframe: str):
    return result
```

### Option 2: Context Managers (More Control)
```python
with track_prediction_latency():
    with track_feature_computation("technical_indicators"):
        features = compute_features(data)
    
    with track_model_inference("xgboost_v1", "1.0.0"):
        prediction = model.predict(features)
    
    record_prediction_request(symbol, timeframe, "success")
```

## All Metrics

| Metric | Type | Usage |
|--------|------|-------|
| `ml_prediction_latency_seconds` | Histogram | `with track_prediction_latency():` |
| `ml_prediction_requests_total` | Counter | `record_prediction_request(symbol, timeframe, status)` |
| `ml_model_accuracy_score` | Gauge | `update_model_accuracy(model, metric, score)` |
| `ml_feature_computation_duration_seconds` | Histogram | `with track_feature_computation(feature_set):` |
| `ml_feature_cache_hit_rate` | Gauge | `update_cache_hit_rate(cache_type, rate)` |
| `ml_feature_errors_total` | Counter | `record_feature_error(feature, error_type)` |
| `ml_model_inference_duration_seconds` | Histogram | `with track_model_inference(model, version):` |
| `ml_shap_computation_duration_seconds` | Histogram | `with track_shap_computation(model):` |
| `ml_model_load_failures_total` | Counter | `record_model_load_failure(model, reason)` |

## Common PromQL Queries

```promql
# Average latency
rate(ml_prediction_latency_seconds_sum[5m]) / rate(ml_prediction_latency_seconds_count[5m])

# 95th percentile latency
histogram_quantile(0.95, rate(ml_prediction_latency_seconds_bucket[5m]))

# Error rate
sum(rate(ml_prediction_requests_total{status="error"}[5m])) / sum(rate(ml_prediction_requests_total[5m])) * 100

# Request rate by symbol
sum(rate(ml_prediction_requests_total[5m])) by (symbol)
```

## FastAPI Endpoint

```python
from backend.app.ml.monitoring.fastapi_endpoint import router

app.include_router(router)
# Metrics at: http://localhost:8000/metrics
```

## Prometheus Config

```yaml
scrape_configs:
  - job_name: 'ml-prediction-system'
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/metrics'
```

## Files

- `metrics.py` - Core implementation
- `METRICS_GUIDE.md` - Complete guide
- `README.md` - Getting started
- `integration_example.py` - Code examples
- `test_metrics.py` - Tests
- `QUICK_REFERENCE.md` - This file
