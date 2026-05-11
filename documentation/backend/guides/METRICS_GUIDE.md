# Cortex AI Metrics Guide

**Version:** 1.0.0  
**Last Updated:** April 22, 2026  
**Prometheus Endpoint:** `/metrics`

---

## Table of Contents

1. [Overview](#overview)
2. [Metric Types](#metric-types)
3. [HTTP Request Metrics](#http-request-metrics)
4. [Trade Suggestions Metrics](#trade-suggestions-metrics)
5. [ML Prediction Metrics](#ml-prediction-metrics)
6. [Worker Metrics](#worker-metrics)
7. [Database & Redis Metrics](#database--redis-metrics)
8. [Common PromQL Queries](#common-promql-queries)
9. [Alerting Rules](#alerting-rules)
10. [Troubleshooting](#troubleshooting)

---

## Overview

Cortex AI exposes Prometheus metrics at `/metrics` endpoint for comprehensive observability. All metrics follow Prometheus naming conventions (snake_case) and include appropriate labels for filtering and aggregation.

### Accessing Metrics

```bash
# Local development
curl http://localhost:8000/metrics

# Production
curl https://api.cortex.ai/metrics
```

### Metric Naming Convention

```
<namespace>_<subsystem>_<name>_<unit>
```

Example: `http_request_duration_seconds`

---

## Metric Types

### Counter
Monotonically increasing value (never decreases). Use `rate()` or `increase()` for queries.

**Example:** `http_requests_total`

### Gauge
Value that can go up or down. Use directly in queries.

**Example:** `suggestions_active`

### Histogram
Samples observations and counts them in configurable buckets. Automatically creates `_bucket`, `_count`, and `_sum` metrics.

**Example:** `http_request_duration_seconds`

### Info
Key-value pairs for metadata.

**Example:** `cortex_app_info`

---

## HTTP Request Metrics

### `http_requests_total`

**Type:** Counter  
**Description:** Total number of HTTP requests processed  
**Labels:**
- `method`: HTTP method (GET, POST, PUT, DELETE)
- `endpoint`: Normalized endpoint path (IDs replaced with `{id}`)
- `status`: HTTP status code (200, 404, 500, etc.)

**Example:**
```promql
# Requests per second by endpoint
rate(http_requests_total[5m])

# Total requests in last hour
increase(http_requests_total[1h])

# Requests by status code
sum by (status) (rate(http_requests_total[5m]))
```

---

### `http_request_duration_seconds`

**Type:** Histogram  
**Description:** HTTP request latency in seconds  
**Labels:**
- `method`: HTTP method
- `endpoint`: Normalized endpoint path

**Buckets:** 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0

**Example:**
```promql
# P95 latency by endpoint
histogram_quantile(0.95, sum by (le, endpoint) (rate(http_request_duration_seconds_bucket[5m])))

# P99 latency
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))

# Average latency
rate(http_request_duration_seconds_sum[5m]) / rate(http_request_duration_seconds_count[5m])
```

---

### `http_requests_in_progress`

**Type:** Gauge  
**Description:** Number of HTTP requests currently being processed  
**Labels:**
- `method`: HTTP method
- `endpoint`: Normalized endpoint path

**Example:**
```promql
# Current in-progress requests
sum(http_requests_in_progress)

# In-progress by endpoint
sum by (endpoint) (http_requests_in_progress)
```

---

## Trade Suggestions Metrics

### `suggestions_generated_total`

**Type:** Counter  
**Description:** Total trade suggestions generated  
**Labels:**
- `direction`: Signal direction (BUY, SELL)
- `confidence_level`: Confidence level (HIGH, MEDIUM, LOW)
- `status`: Suggestion status (active, expired, invalidated)

**Example:**
```promql
# Suggestions per minute
rate(suggestions_generated_total[1m]) * 60

# Suggestions by direction
sum by (direction) (suggestions_generated_total)

# High confidence suggestions
sum(suggestions_generated_total{confidence_level="HIGH"})
```

---

### `consensus_score_distribution`

**Type:** Histogram  
**Description:** Distribution of consensus scores (0-100)  
**Buckets:** 0, 20, 40, 60, 70, 80, 85, 90, 95, 100

**Example:**
```promql
# Average consensus score
rate(consensus_score_distribution_sum[5m]) / rate(consensus_score_distribution_count[5m])

# P95 consensus score
histogram_quantile(0.95, sum by (le) (rate(consensus_score_distribution_bucket[5m])))

# Percentage of high-quality suggestions (score >= 80)
sum(rate(consensus_score_distribution_bucket{le="100"}[5m])) - sum(rate(consensus_score_distribution_bucket{le="80"}[5m]))
```

---

### `suggestion_expiry_total`

**Type:** Counter  
**Description:** Total trade suggestions expired  
**Labels:**
- `direction`: Signal direction (BUY, SELL)
- `confidence_level`: Confidence level (HIGH, MEDIUM, LOW)

**Example:**
```promql
# Expiry rate per hour
rate(suggestion_expiry_total[1h]) * 3600

# Expired by direction
sum by (direction) (suggestion_expiry_total)
```

---

### `correlation_latency_seconds`

**Type:** Histogram  
**Description:** Correlation engine latency by pathway and agent  
**Labels:**
- `pathway`: Correlation pathway (pathway1, pathway2)
- `agent`: Agent name (scanner, ai, ml)

**Buckets:** 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0

**Example:**
```promql
# P95 latency by agent
histogram_quantile(0.95, sum by (le, agent) (rate(correlation_latency_seconds_bucket[5m])))

# Average latency by pathway
rate(correlation_latency_seconds_sum[5m]) / rate(correlation_latency_seconds_count[5m])

# Slowest agent
topk(1, histogram_quantile(0.95, sum by (le, agent) (rate(correlation_latency_seconds_bucket[5m]))))
```

---

### `suggestions_active`

**Type:** Gauge  
**Description:** Number of currently active trade suggestions  
**Labels:**
- `direction`: Signal direction (BUY, SELL)
- `confidence_level`: Confidence level (HIGH, MEDIUM, LOW)

**Example:**
```promql
# Total active suggestions
sum(suggestions_active)

# Active by direction
sum by (direction) (suggestions_active)

# High confidence active suggestions
sum(suggestions_active{confidence_level="HIGH"})
```

---

## ML Prediction Metrics

### `ml_predictions_total`

**Type:** Counter  
**Description:** Total ML predictions made  
**Labels:**
- `model_id`: Model identifier
- `symbol`: Trading symbol
- `status`: Prediction status (success, failed)

**Example:**
```promql
# Predictions per second
rate(ml_predictions_total[5m])

# Success rate
sum(rate(ml_predictions_total{status="success"}[5m])) / sum(rate(ml_predictions_total[5m]))
```

---

### `ml_prediction_duration_seconds`

**Type:** Histogram  
**Description:** ML prediction latency  
**Labels:**
- `model_id`: Model identifier

**Buckets:** 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0

**Example:**
```promql
# P95 prediction latency
histogram_quantile(0.95, sum by (le, model_id) (rate(ml_prediction_duration_seconds_bucket[5m])))
```

---

### `ml_prediction_confidence`

**Type:** Histogram  
**Description:** ML prediction confidence scores  
**Labels:**
- `model_id`: Model identifier

**Buckets:** 0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0

**Example:**
```promql
# Average confidence
rate(ml_prediction_confidence_sum[5m]) / rate(ml_prediction_confidence_count[5m])
```

---

## Worker Metrics

### `worker_heartbeat_age_seconds`

**Type:** Gauge  
**Description:** Age of last worker heartbeat in seconds

**Example:**
```promql
# Worker health check (alert if > 60s)
worker_heartbeat_age_seconds > 60
```

---

### `worker_loop_iterations_total`

**Type:** Counter  
**Description:** Total worker loop iterations  
**Labels:**
- `loop_name`: Worker loop name

**Example:**
```promql
# Iterations per minute
rate(worker_loop_iterations_total[1m]) * 60
```

---

### `worker_loop_duration_seconds`

**Type:** Histogram  
**Description:** Worker loop execution duration  
**Labels:**
- `loop_name`: Worker loop name

**Buckets:** 0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0

**Example:**
```promql
# P95 loop duration
histogram_quantile(0.95, sum by (le, loop_name) (rate(worker_loop_duration_seconds_bucket[5m])))
```

---

## Database & Redis Metrics

### `db_query_duration_seconds`

**Type:** Histogram  
**Description:** Database query duration  
**Labels:**
- `operation`: Query operation type

**Buckets:** 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0

**Example:**
```promql
# P95 query latency
histogram_quantile(0.95, sum by (le, operation) (rate(db_query_duration_seconds_bucket[5m])))

# Slow queries (> 100ms)
histogram_quantile(0.95, sum by (le, operation) (rate(db_query_duration_seconds_bucket[5m]))) > 0.1
```

---

### `db_connections_active`

**Type:** Gauge  
**Description:** Active database connections

**Example:**
```promql
# Current connections
db_connections_active

# Alert if connections > 80% of pool size
db_connections_active > 80
```

---

### `redis_operations_total`

**Type:** Counter  
**Description:** Total Redis operations  
**Labels:**
- `operation`: Operation type (get, set, publish, etc.)
- `status`: Operation status (success, failed)

**Example:**
```promql
# Operations per second
rate(redis_operations_total[5m])

# Error rate
sum(rate(redis_operations_total{status="failed"}[5m])) / sum(rate(redis_operations_total[5m]))
```

---

### `redis_operation_duration_seconds`

**Type:** Histogram  
**Description:** Redis operation duration  
**Labels:**
- `operation`: Operation type

**Buckets:** 0.001, 0.005, 0.01, 0.025, 0.05, 0.1

**Example:**
```promql
# P95 Redis latency
histogram_quantile(0.95, sum by (le, operation) (rate(redis_operation_duration_seconds_bucket[5m])))
```

---

## Common PromQL Queries

### Request Rate

```promql
# Total requests per second
sum(rate(http_requests_total[5m]))

# Requests per second by endpoint
sum by (endpoint) (rate(http_requests_total[5m]))

# Top 5 endpoints by traffic
topk(5, sum by (endpoint) (rate(http_requests_total[5m])))
```

---

### Error Rate

```promql
# Overall error rate (%)
100 * sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))

# Error rate by endpoint
100 * sum by (endpoint) (rate(http_requests_total{status=~"5.."}[5m])) / sum by (endpoint) (rate(http_requests_total[5m]))

# 4xx vs 5xx errors
sum by (status) (rate(http_requests_total{status=~"[45].."}[5m]))
```

---

### Latency Percentiles

```promql
# P50 (median)
histogram_quantile(0.50, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))

# P95
histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))

# P99
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))

# P99.9
histogram_quantile(0.999, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))
```

---

### Business Metrics

```promql
# Suggestions generated per hour
rate(suggestions_generated_total[1h]) * 3600

# Buy vs Sell ratio
sum(suggestions_generated_total{direction="BUY"}) / sum(suggestions_generated_total{direction="SELL"})

# High confidence percentage
100 * sum(suggestions_generated_total{confidence_level="HIGH"}) / sum(suggestions_generated_total)

# Average consensus score
rate(consensus_score_distribution_sum[5m]) / rate(consensus_score_distribution_count[5m])
```

---

## Alerting Rules

### Critical Alerts

```yaml
# High error rate
- alert: HighErrorRate
  expr: 100 * sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) > 5
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "High error rate detected"
    description: "Error rate is {{ $value }}% (threshold: 5%)"

# High latency
- alert: HighLatency
  expr: histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m]))) > 0.5
  for: 5m
  labels:
    severity: critical
  annotations:
    summary: "High P95 latency detected"
    description: "P95 latency is {{ $value }}s (threshold: 0.5s)"

# Worker down
- alert: WorkerDown
  expr: worker_heartbeat_age_seconds > 120
  for: 2m
  labels:
    severity: critical
  annotations:
    summary: "Worker heartbeat missing"
    description: "Worker hasn't sent heartbeat for {{ $value }}s"
```

---

### Warning Alerts

```yaml
# Elevated error rate
- alert: ElevatedErrorRate
  expr: 100 * sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) > 1
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Elevated error rate"
    description: "Error rate is {{ $value }}% (threshold: 1%)"

# Slow database queries
- alert: SlowDatabaseQueries
  expr: histogram_quantile(0.95, sum by (le, operation) (rate(db_query_duration_seconds_bucket[5m]))) > 0.1
  for: 10m
  labels:
    severity: warning
  annotations:
    summary: "Slow database queries detected"
    description: "P95 query latency for {{ $labels.operation }} is {{ $value }}s"

# High database connections
- alert: HighDatabaseConnections
  expr: db_connections_active > 80
  for: 5m
  labels:
    severity: warning
  annotations:
    summary: "High database connection count"
    description: "Active connections: {{ $value }} (threshold: 80)"
```

---

## Troubleshooting

### Metrics Not Appearing

**Problem:** Metrics not showing in `/metrics` endpoint

**Solutions:**
1. Check if metric is defined in `app/core/metrics.py`
2. Verify metric is imported where it's used
3. Ensure metric is incremented/observed in code
4. Restart application to reload metrics

---

### Histogram Buckets Not Appropriate

**Problem:** All values fall in one bucket

**Solutions:**
1. Adjust bucket boundaries in metric definition
2. Use logarithmic buckets for wide ranges
3. Check actual value distribution in logs

**Example:**
```python
# Before (too coarse)
buckets=(0.1, 1.0, 10.0)

# After (fine-grained)
buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
```

---

### High Cardinality Labels

**Problem:** Too many unique label combinations

**Solutions:**
1. Avoid user IDs or request IDs as labels
2. Normalize paths (replace IDs with `{id}`)
3. Use fixed set of label values
4. Consider using logs for high-cardinality data

**Bad:**
```python
metric.labels(user_id="user_12345")  # Millions of users
```

**Good:**
```python
metric.labels(user_type="premium")  # Fixed set
```

---

### PromQL Query Returns No Data

**Problem:** Query returns empty result

**Solutions:**
1. Check time range (use `[5m]` for rate queries)
2. Verify label names and values
3. Use `rate()` for counters, not raw values
4. Check if metric has been incremented

**Example:**
```promql
# Wrong (counter without rate)
http_requests_total

# Correct
rate(http_requests_total[5m])
```

---

## Best Practices

1. **Use Consistent Labels** - Same label names across related metrics
2. **Avoid High Cardinality** - Limit unique label combinations
3. **Use Histograms for Latency** - Better than averages
4. **Set Appropriate Buckets** - Match expected value distribution
5. **Document Custom Metrics** - Add HELP text and examples
6. **Test Metrics** - Verify they increment correctly
7. **Monitor Metric Cardinality** - Track number of time series
8. **Use Recording Rules** - Pre-compute expensive queries

---

## Additional Resources

- [Prometheus Documentation](https://prometheus.io/docs/)
- [PromQL Cheat Sheet](https://promlabs.com/promql-cheat-sheet/)
- [Grafana Dashboard Best Practices](https://grafana.com/docs/grafana/latest/best-practices/)
- [Histogram vs Summary](https://prometheus.io/docs/practices/histograms/)

---

**Last Updated:** April 22, 2026  
**Maintained By:** Cortex AI Team  
**Questions?** Contact: devops@cortex.ai
