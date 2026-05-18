# Prometheus Metrics Guide

This guide explains how to use and query the ML prediction system metrics.

## Overview

The metrics module provides comprehensive monitoring for:
- **Prediction Performance**: Latency, request counts, and accuracy
- **Feature Computation**: Duration, cache efficiency, and errors
- **Model Serving**: Inference time, SHAP computation, and load failures

## Metric Types

### Histograms
Track distributions of values (e.g., latency). Automatically creates `_bucket`, `_sum`, and `_count` metrics.

### Counters
Monotonically increasing values (e.g., total requests). Only goes up.

### Gauges
Values that can go up or down (e.g., accuracy, cache hit rate).

### Info
Static metadata about the system (e.g., model version).

## Available Metrics

### Prediction Metrics (Task 25.1)

#### `ml_prediction_latency_seconds`
**Type**: Histogram  
**Description**: Time taken to generate a prediction  
**Buckets**: 0.05, 0.1, 0.15, 0.2, 0.25 seconds  
**Requirements**: 17.3, 17.4

**Usage**:
```python
from backend.app.ml.monitoring.metrics import track_prediction_latency

with track_prediction_latency():
    result = make_prediction(data)
```

**PromQL Queries**:
```promql
# Average prediction latency over 5 minutes
rate(ml_prediction_latency_seconds_sum[5m]) / rate(ml_prediction_latency_seconds_count[5m])

# 95th percentile latency
histogram_quantile(0.95, rate(ml_prediction_latency_seconds_bucket[5m]))

# Percentage of requests under 100ms SLA
sum(rate(ml_prediction_latency_seconds_bucket{le="0.1"}[5m])) / sum(rate(ml_prediction_latency_seconds_count[5m])) * 100
```

#### `ml_prediction_requests_total`
**Type**: Counter  
**Labels**: `symbol`, `timeframe`, `status`  
**Description**: Total number of prediction requests  
**Requirements**: 17.3, 17.4

**Usage**:
```python
from backend.app.ml.monitoring.metrics import record_prediction_request

record_prediction_request("AAPL", "1h", "success")
record_prediction_request("GOOGL", "1d", "error")
```

**PromQL Queries**:
```promql
# Request rate by symbol
sum(rate(ml_prediction_requests_total[5m])) by (symbol)

# Error rate
sum(rate(ml_prediction_requests_total{status="error"}[5m])) / sum(rate(ml_prediction_requests_total[5m])) * 100

# Top 10 most requested symbols
topk(10, sum(rate(ml_prediction_requests_total[1h])) by (symbol))

# Success rate by timeframe
sum(rate(ml_prediction_requests_total{status="success"}[5m])) by (timeframe)
```

#### `ml_model_accuracy_score`
**Type**: Gauge  
**Labels**: `model_name`, `metric_type`  
**Description**: Current model accuracy score  
**Requirements**: 17.3, 17.4

**Usage**:
```python
from backend.app.ml.monitoring.metrics import update_model_accuracy

# Update daily after model evaluation
update_model_accuracy("xgboost_v1", "accuracy", 0.85)
update_model_accuracy("xgboost_v1", "f1", 0.82)
update_model_accuracy("xgboost_v1", "precision", 0.88)
```

**PromQL Queries**:
```promql
# Current accuracy for all models
ml_model_accuracy_score{metric_type="accuracy"}

# Models with accuracy below threshold
ml_model_accuracy_score{metric_type="accuracy"} < 0.80

# Accuracy trend over time
ml_model_accuracy_score{model_name="xgboost_v1", metric_type="accuracy"}[7d]
```

### Feature Computation Metrics (Task 25.2)

#### `ml_feature_computation_duration_seconds`
**Type**: Histogram  
**Labels**: `feature_set`  
**Description**: Time taken to compute features  
**Buckets**: 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0 seconds  
**Requirements**: 17.3

**Usage**:
```python
from backend.app.ml.monitoring.metrics import track_feature_computation

with track_feature_computation("technical_indicators"):
    features = compute_technical_indicators(data)
```

**PromQL Queries**:
```promql
# Average feature computation time
rate(ml_feature_computation_duration_seconds_sum[5m]) / rate(ml_feature_computation_duration_seconds_count[5m])

# 99th percentile by feature set
histogram_quantile(0.99, rate(ml_feature_computation_duration_seconds_bucket[5m])) by (feature_set)

# Slowest feature sets
topk(5, rate(ml_feature_computation_duration_seconds_sum[5m]) / rate(ml_feature_computation_duration_seconds_count[5m])) by (feature_set)
```

#### `ml_feature_cache_hit_rate`
**Type**: Gauge  
**Labels**: `cache_type`  
**Description**: Percentage of feature requests served from cache  
**Requirements**: 17.3

**Usage**:
```python
from backend.app.ml.monitoring.metrics import update_cache_hit_rate

# Update periodically (e.g., every minute)
total_requests = cache.get_total_requests()
cache_hits = cache.get_cache_hits()
hit_rate = (cache_hits / total_requests) * 100 if total_requests > 0 else 0
update_cache_hit_rate("redis", hit_rate)
```

**PromQL Queries**:
```promql
# Current cache hit rate
ml_feature_cache_hit_rate

# Cache hit rate below threshold
ml_feature_cache_hit_rate < 80

# Cache efficiency trend
ml_feature_cache_hit_rate{cache_type="redis"}[1h]
```

#### `ml_feature_errors_total`
**Type**: Counter  
**Labels**: `feature_name`, `error_type`  
**Description**: Total number of feature computation errors  
**Requirements**: 17.3

**Usage**:
```python
from backend.app.ml.monitoring.metrics import record_feature_error

try:
    rsi = compute_rsi(data)
except Exception as e:
    record_feature_error("rsi_14", "computation_error")
    raise
```

**PromQL Queries**:
```promql
# Error rate by feature
sum(rate(ml_feature_errors_total[5m])) by (feature_name)

# Most problematic features
topk(10, sum(rate(ml_feature_errors_total[1h])) by (feature_name))

# Error types distribution
sum(rate(ml_feature_errors_total[5m])) by (error_type)
```

### Model Serving Metrics (Task 25.3)

#### `ml_model_inference_duration_seconds`
**Type**: Histogram  
**Labels**: `model_name`, `model_version`  
**Description**: Time taken for model inference  
**Buckets**: 0.01, 0.025, 0.05, 0.1, 0.25, 0.5 seconds  
**Requirements**: 17.3

**Usage**:
```python
from backend.app.ml.monitoring.metrics import track_model_inference

with track_model_inference("xgboost_v1", "1.0.0"):
    prediction = model.predict(features)
```

**PromQL Queries**:
```promql
# Average inference time by model
rate(ml_model_inference_duration_seconds_sum[5m]) / rate(ml_model_inference_duration_seconds_count[5m]) by (model_name)

# 95th percentile inference time
histogram_quantile(0.95, rate(ml_model_inference_duration_seconds_bucket[5m])) by (model_name)

# Compare model versions
rate(ml_model_inference_duration_seconds_sum[5m]) / rate(ml_model_inference_duration_seconds_count[5m]) by (model_name, model_version)
```

#### `ml_shap_computation_duration_seconds`
**Type**: Histogram  
**Labels**: `model_name`  
**Description**: Time taken to compute SHAP values  
**Buckets**: 0.1, 0.25, 0.5, 1.0, 2.0, 5.0 seconds  
**Requirements**: 17.3

**Usage**:
```python
from backend.app.ml.monitoring.metrics import track_shap_computation

with track_shap_computation("xgboost_v1"):
    shap_values = explainer.shap_values(features)
```

**PromQL Queries**:
```promql
# Average SHAP computation time
rate(ml_shap_computation_duration_seconds_sum[5m]) / rate(ml_shap_computation_duration_seconds_count[5m])

# SHAP computation overhead (as % of total prediction time)
(rate(ml_shap_computation_duration_seconds_sum[5m]) / rate(ml_prediction_latency_seconds_sum[5m])) * 100

# 99th percentile SHAP time
histogram_quantile(0.99, rate(ml_shap_computation_duration_seconds_bucket[5m]))
```

#### `ml_model_load_failures_total`
**Type**: Counter  
**Labels**: `model_name`, `failure_reason`  
**Description**: Total number of model loading failures  
**Requirements**: 17.3

**Usage**:
```python
from backend.app.ml.monitoring.metrics import record_model_load_failure

try:
    model = load_model("xgboost_v1")
except FileNotFoundError:
    record_model_load_failure("xgboost_v1", "file_not_found")
    raise
except Exception as e:
    record_model_load_failure("xgboost_v1", "corrupted")
    raise
```

**PromQL Queries**:
```promql
# Model load failure rate
sum(rate(ml_model_load_failures_total[5m])) by (model_name)

# Failure reasons distribution
sum(rate(ml_model_load_failures_total[5m])) by (failure_reason)

# Models with recent failures
sum(increase(ml_model_load_failures_total[1h])) by (model_name) > 0
```

## Integration Examples

### Complete Prediction Flow

```python
from backend.app.ml.monitoring.metrics import (
    track_prediction_latency,
    track_active_prediction,
    track_feature_computation,
    track_model_inference,
    track_shap_computation,
    record_prediction_request,
    record_feature_error,
    record_model_load_failure
)

async def predict(symbol: str, timeframe: str, data: dict):
    """Make a prediction with full metric tracking."""
    
    with track_prediction_latency():
        with track_active_prediction():
            try:
                # Compute features
                with track_feature_computation("technical_indicators"):
                    try:
                        features = compute_features(data)
                    except Exception as e:
                        record_feature_error("technical_indicators", "computation_error")
                        raise
                
                # Load model
                try:
                    model = load_model("xgboost_v1")
                except Exception as e:
                    record_model_load_failure("xgboost_v1", "file_not_found")
                    raise
                
                # Run inference
                with track_model_inference("xgboost_v1", "1.0.0"):
                    prediction = model.predict(features)
                
                # Compute SHAP values
                with track_shap_computation("xgboost_v1"):
                    shap_values = explainer.shap_values(features)
                
                # Record success
                record_prediction_request(symbol, timeframe, "success")
                
                return {
                    "prediction": prediction,
                    "shap_values": shap_values
                }
                
            except Exception as e:
                record_prediction_request(symbol, timeframe, "error")
                raise
```

### Using the Decorator

```python
from backend.app.ml.monitoring.metrics import track_prediction_metrics

@track_prediction_metrics()
async def predict_endpoint(symbol: str, timeframe: str):
    """Endpoint with automatic metric tracking."""
    # Decorator automatically tracks latency, active predictions, and request counts
    return await make_prediction(symbol, timeframe)
```

## Alerting Rules

### Critical Alerts

```yaml
groups:
  - name: ml_prediction_critical
    interval: 30s
    rules:
      # High error rate
      - alert: HighPredictionErrorRate
        expr: |
          sum(rate(ml_prediction_requests_total{status="error"}[5m])) 
          / sum(rate(ml_prediction_requests_total[5m])) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "High prediction error rate (>5%)"
          
      # SLA violation
      - alert: PredictionLatencySLAViolation
        expr: |
          histogram_quantile(0.95, rate(ml_prediction_latency_seconds_bucket[5m])) > 0.2
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "95th percentile latency exceeds 200ms SLA"
          
      # Model accuracy drop
      - alert: ModelAccuracyDrop
        expr: ml_model_accuracy_score{metric_type="accuracy"} < 0.75
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "Model accuracy dropped below 75%"
```

### Warning Alerts

```yaml
  - name: ml_prediction_warning
    interval: 1m
    rules:
      # Low cache hit rate
      - alert: LowCacheHitRate
        expr: ml_feature_cache_hit_rate < 70
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "Feature cache hit rate below 70%"
          
      # High feature error rate
      - alert: HighFeatureErrorRate
        expr: sum(rate(ml_feature_errors_total[5m])) > 1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "High feature computation error rate"
          
      # Model load failures
      - alert: ModelLoadFailures
        expr: sum(increase(ml_model_load_failures_total[5m])) > 0
        labels:
          severity: warning
        annotations:
          summary: "Model loading failures detected"
```

## Grafana Dashboard Queries

### Prediction Performance Panel

```promql
# Request rate
sum(rate(ml_prediction_requests_total[5m]))

# Success rate
sum(rate(ml_prediction_requests_total{status="success"}[5m])) 
/ sum(rate(ml_prediction_requests_total[5m])) * 100

# Average latency
rate(ml_prediction_latency_seconds_sum[5m]) 
/ rate(ml_prediction_latency_seconds_count[5m])

# P50, P95, P99 latency
histogram_quantile(0.50, rate(ml_prediction_latency_seconds_bucket[5m]))
histogram_quantile(0.95, rate(ml_prediction_latency_seconds_bucket[5m]))
histogram_quantile(0.99, rate(ml_prediction_latency_seconds_bucket[5m]))
```

### Feature Computation Panel

```promql
# Feature computation time by set
rate(ml_feature_computation_duration_seconds_sum[5m]) 
/ rate(ml_feature_computation_duration_seconds_count[5m]) 
by (feature_set)

# Cache hit rate
ml_feature_cache_hit_rate

# Feature error rate
sum(rate(ml_feature_errors_total[5m])) by (feature_name)
```

### Model Serving Panel

```promql
# Inference time by model
rate(ml_model_inference_duration_seconds_sum[5m]) 
/ rate(ml_model_inference_duration_seconds_count[5m]) 
by (model_name)

# SHAP computation time
rate(ml_shap_computation_duration_seconds_sum[5m]) 
/ rate(ml_shap_computation_duration_seconds_count[5m])

# Model accuracy
ml_model_accuracy_score{metric_type="accuracy"}
```

## Best Practices

1. **Label Cardinality**: Keep label values bounded. Don't use unbounded values like user IDs or timestamps as labels.

2. **Metric Naming**: Follow Prometheus conventions:
   - Use `_total` suffix for counters
   - Use `_seconds` for durations
   - Use base units (seconds, bytes, not milliseconds or megabytes)

3. **Histogram Buckets**: Choose buckets based on your SLAs and expected distribution.

4. **Update Frequency**:
   - Counters/Histograms: Update on every event
   - Gauges: Update periodically (e.g., every minute for cache hit rate)
   - Info: Update on deployment or configuration change

5. **Error Handling**: Always record errors in metrics, even if you're also logging them.

6. **Context Managers**: Use context managers for automatic timing to avoid forgetting to record metrics.

## Exposing Metrics Endpoint

Add to your FastAPI application:

```python
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi import Response

@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )
```

## Prometheus Configuration

Add to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'ml-prediction-system'
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/metrics'
```
