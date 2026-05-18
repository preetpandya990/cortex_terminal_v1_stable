# ML Monitoring Module

Comprehensive monitoring infrastructure for the ML prediction system using Prometheus metrics.

## Overview

This module provides production-ready monitoring for:
- **Prediction Performance**: Track latency, throughput, and success rates
- **Feature Computation**: Monitor feature generation performance and cache efficiency
- **Model Serving**: Track inference time, SHAP computation, and model health
- **Error Tracking**: Capture and categorize failures for alerting

## Requirements

- Requirements 17.3: ML-specific metrics for performance monitoring
- Requirements 17.4: Prediction latency and accuracy tracking

## Files

### Core Implementation

- **`metrics.py`**: Main metrics module with all Prometheus metrics, context managers, and helper functions
- **`__init__.py`**: Module exports for easy importing
- **`fastapi_endpoint.py`**: FastAPI endpoint for exposing metrics to Prometheus

### Documentation & Examples

- **`METRICS_GUIDE.md`**: Comprehensive guide on using and querying metrics
- **`integration_example.py`**: Complete examples of integrating metrics into your code
- **`test_metrics.py`**: Unit tests demonstrating metric functionality

## Quick Start

### 1. Install Dependencies

```bash
pip install prometheus-client
```

### 2. Import Metrics

```python
from backend.app.ml.monitoring import (
    track_prediction_metrics,
    record_prediction_request,
    update_model_accuracy
)
```

### 3. Use the Decorator (Easiest)

```python
@track_prediction_metrics()
async def predict(symbol: str, timeframe: str):
    # Your prediction logic
    return result
```

### 4. Or Use Context Managers (More Control)

```python
from backend.app.ml.monitoring import (
    track_prediction_latency,
    track_feature_computation,
    track_model_inference
)

with track_prediction_latency():
    with track_feature_computation("technical_indicators"):
        features = compute_features(data)
    
    with track_model_inference("xgboost_v1", "1.0.0"):
        prediction = model.predict(features)
```

### 5. Expose Metrics Endpoint

```python
from fastapi import FastAPI
from backend.app.ml.monitoring.fastapi_endpoint import router

app = FastAPI()
app.include_router(router)

# Metrics available at: http://localhost:8000/metrics
```

### 6. Configure Prometheus

Add to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: 'ml-prediction-system'
    scrape_interval: 15s
    static_configs:
      - targets: ['localhost:8000']
    metrics_path: '/metrics'
```

## Available Metrics

### Prediction Metrics (Task 25.1)

| Metric | Type | Description | Labels |
|--------|------|-------------|--------|
| `ml_prediction_latency_seconds` | Histogram | Prediction latency | - |
| `ml_prediction_requests_total` | Counter | Total requests | symbol, timeframe, status |
| `ml_model_accuracy_score` | Gauge | Model accuracy | model_name, metric_type |

### Feature Metrics (Task 25.2)

| Metric | Type | Description | Labels |
|--------|------|-------------|--------|
| `ml_feature_computation_duration_seconds` | Histogram | Feature computation time | feature_set |
| `ml_feature_cache_hit_rate` | Gauge | Cache hit rate % | cache_type |
| `ml_feature_errors_total` | Counter | Feature errors | feature_name, error_type |

### Model Serving Metrics (Task 25.3)

| Metric | Type | Description | Labels |
|--------|------|-------------|--------|
| `ml_model_inference_duration_seconds` | Histogram | Inference time | model_name, model_version |
| `ml_shap_computation_duration_seconds` | Histogram | SHAP computation time | model_name |
| `ml_model_load_failures_total` | Counter | Model load failures | model_name, failure_reason |

## Usage Examples

### Complete Prediction Flow

```python
from backend.app.ml.monitoring import (
    track_prediction_latency,
    track_active_prediction,
    track_feature_computation,
    track_model_inference,
    track_shap_computation,
    record_prediction_request,
    record_feature_error
)

async def predict(symbol: str, timeframe: str, data: dict):
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
                
                # Run inference
                with track_model_inference("xgboost_v1", "1.0.0"):
                    prediction = model.predict(features)
                
                # Compute SHAP
                with track_shap_computation("xgboost_v1"):
                    shap_values = explainer.shap_values(features)
                
                record_prediction_request(symbol, timeframe, "success")
                return {"prediction": prediction, "shap": shap_values}
                
            except Exception as e:
                record_prediction_request(symbol, timeframe, "error")
                raise
```

### Daily Accuracy Update

```python
from backend.app.ml.monitoring import update_model_accuracy

async def daily_model_evaluation():
    """Run daily to update model accuracy metrics."""
    metrics = evaluate_model("xgboost_v1")
    
    update_model_accuracy("xgboost_v1", "accuracy", metrics["accuracy"])
    update_model_accuracy("xgboost_v1", "f1", metrics["f1_score"])
    update_model_accuracy("xgboost_v1", "precision", metrics["precision"])
    update_model_accuracy("xgboost_v1", "recall", metrics["recall"])
```

### Cache Monitoring

```python
from backend.app.ml.monitoring import update_cache_hit_rate

async def update_cache_metrics():
    """Run periodically to update cache metrics."""
    stats = get_cache_statistics()
    
    for cache_type, data in stats.items():
        total = data["hits"] + data["misses"]
        hit_rate = (data["hits"] / total * 100) if total > 0 else 0
        update_cache_hit_rate(cache_type, hit_rate)
```

## PromQL Query Examples

### SLA Monitoring

```promql
# 95th percentile latency
histogram_quantile(0.95, rate(ml_prediction_latency_seconds_bucket[5m]))

# Percentage under 100ms SLA
sum(rate(ml_prediction_latency_seconds_bucket{le="0.1"}[5m])) 
/ sum(rate(ml_prediction_latency_seconds_count[5m])) * 100
```

### Error Tracking

```promql
# Error rate
sum(rate(ml_prediction_requests_total{status="error"}[5m])) 
/ sum(rate(ml_prediction_requests_total[5m])) * 100

# Top error-prone features
topk(10, sum(rate(ml_feature_errors_total[1h])) by (feature_name))
```

### Performance Analysis

```promql
# Average inference time by model
rate(ml_model_inference_duration_seconds_sum[5m]) 
/ rate(ml_model_inference_duration_seconds_count[5m]) 
by (model_name)

# SHAP overhead
(rate(ml_shap_computation_duration_seconds_sum[5m]) 
/ rate(ml_prediction_latency_seconds_sum[5m])) * 100
```

## Alerting Rules

### Critical Alerts

```yaml
groups:
  - name: ml_critical
    rules:
      - alert: HighErrorRate
        expr: |
          sum(rate(ml_prediction_requests_total{status="error"}[5m])) 
          / sum(rate(ml_prediction_requests_total[5m])) > 0.05
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Prediction error rate above 5%"
      
      - alert: LatencySLAViolation
        expr: |
          histogram_quantile(0.95, rate(ml_prediction_latency_seconds_bucket[5m])) > 0.2
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "95th percentile latency exceeds 200ms"
      
      - alert: ModelAccuracyDrop
        expr: ml_model_accuracy_score{metric_type="accuracy"} < 0.75
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "Model accuracy below 75%"
```

## Testing

Run the test suite:

```bash
pytest backend/app/ml/monitoring/test_metrics.py -v
```

## Integration Checklist

- [ ] Install `prometheus-client` dependency
- [ ] Import metrics into your prediction code
- [ ] Add metrics tracking to prediction endpoints
- [ ] Add metrics tracking to feature computation
- [ ] Add metrics tracking to model inference
- [ ] Expose `/metrics` endpoint in FastAPI
- [ ] Configure Prometheus to scrape metrics
- [ ] Set up Grafana dashboards
- [ ] Configure alerting rules
- [ ] Schedule daily accuracy updates
- [ ] Schedule periodic cache metrics updates

## Best Practices

1. **Use Context Managers**: They automatically handle timing and cleanup
2. **Label Cardinality**: Keep label values bounded (don't use user IDs, timestamps)
3. **Error Recording**: Always record errors in metrics, even if logging them
4. **Histogram Buckets**: Choose buckets based on your SLAs
5. **Update Frequency**: 
   - Counters/Histograms: On every event
   - Gauges: Periodically (e.g., every minute)
   - Info: On deployment/config change

## Troubleshooting

### Metrics Not Appearing

1. Check that `/metrics` endpoint is accessible
2. Verify Prometheus is scraping the endpoint
3. Check Prometheus logs for scrape errors
4. Ensure metrics are being recorded in your code

### High Cardinality Warning

If you see warnings about high cardinality:
1. Review your label values
2. Remove unbounded labels (user IDs, timestamps, etc.)
3. Aggregate similar values into categories

### Memory Usage

If metrics consume too much memory:
1. Reduce histogram bucket count
2. Limit label cardinality
3. Consider using summary instead of histogram for some metrics

## Further Reading

- [Prometheus Best Practices](https://prometheus.io/docs/practices/naming/)
- [Histogram vs Summary](https://prometheus.io/docs/practices/histograms/)
- [PromQL Basics](https://prometheus.io/docs/prometheus/latest/querying/basics/)
- See `METRICS_GUIDE.md` for detailed usage and query examples
- See `integration_example.py` for complete code examples
