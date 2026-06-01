# ML Health Check Endpoints

## Overview

The ML health check endpoints provide comprehensive monitoring of the ML prediction system components. These endpoints are designed for use by monitoring systems, load balancers, and Kubernetes probes.

## Endpoints

### 1. Comprehensive Health Check

**Endpoint:** `GET /api/v1/health/ml`

**Purpose:** Comprehensive health check of all ML system components

**Checks:**
- Model availability (latest production model loaded)
- Database connectivity (query ml_models table)
- Redis connectivity (test feature cache)

**Response Codes:**
- `200 OK`: All systems healthy
- `503 Service Unavailable`: One or more critical systems unavailable

**Response Format:**
```json
{
  "status": "healthy" | "degraded",
  "timestamp": "2024-01-01T00:00:00Z",
  "checks": {
    "database": {
      "status": "healthy" | "unhealthy",
      "details": {
        "connected": true,
        "active_model_available": true,
        "active_model_id": "model_v1.0.0",
        "active_model_version": "1.0.0"
      }
    },
    "redis": {
      "status": "healthy" | "unhealthy",
      "details": {
        "connected": true,
        "total_keys": 1234,
        "hit_rate": 0.85,
        "memory_used": "10.5M"
      }
    },
    "model": {
      "status": "healthy" | "unhealthy",
      "details": {
        "model_loaded": true,
        "model_id": "model_v1.0.0",
        "model_version": "1.0.0",
        "model_name": "Stock Prediction Model",
        "deployed_at": "2024-01-01T00:00:00Z",
        "model_file_exists": true,
        "onnx_path": "/path/to/model.onnx"
      }
    }
  }
}
```

**Requirements:** 17.5, 19.4

---

### 2. Readiness Check

**Endpoint:** `GET /api/v1/health/ml/ready`

**Purpose:** Kubernetes/load balancer readiness probe

**Checks:**
- Model availability and readiness

**Response Codes:**
- `200 OK`: System ready to accept requests
- `503 Service Unavailable`: System not ready (model unavailable)

**Response Format:**
```json
{
  "ready": true,
  "model": {
    "model_loaded": true,
    "model_id": "model_v1.0.0",
    "model_version": "1.0.0",
    "model_name": "Stock Prediction Model",
    "deployed_at": "2024-01-01T00:00:00Z",
    "model_file_exists": true,
    "onnx_path": "/path/to/model.onnx"
  }
}
```

**Requirements:** 17.5, 19.4

---

### 3. Liveness Check

**Endpoint:** `GET /api/v1/health/ml/live`

**Purpose:** Kubernetes liveness probe

**Checks:**
- Service is running and responsive

**Response Codes:**
- `200 OK`: Service is alive

**Response Format:**
```json
{
  "alive": true,
  "timestamp": "2024-01-01T00:00:00Z"
}
```

**Requirements:** 17.5, 19.4

---

## Graceful Degradation

The ML prediction endpoints implement graceful degradation when components are unavailable:

### 1. Model Unavailable (HTTP 503)

**Behavior:** Return HTTP 503 if model unavailable

**Requirements:** 19.1, 26.2

**Example:**
```json
{
  "detail": "ML model unavailable: No active production model available"
}
```

### 2. Database Unavailable

**Behavior:** Return cached predictions if database unavailable

**Requirements:** 19.2, 26.2

**Example Response:**
```json
{
  "direction": 2,
  "direction_label": "BUY",
  "confidence": 0.85,
  "entry_price": 100.0,
  "tp1": 105.0,
  "tp2": 110.0,
  "tp3": 115.0,
  "stop_loss": 95.0,
  "volatility": 0.02,
  "warnings": ["Using cached prediction - database unavailable"]
}
```

### 3. SHAP Service Unavailable

**Behavior:** Return predictions without SHAP if SHAP service unavailable

**Requirements:** 19.2, 26.2

**Example Response:**
```json
{
  "direction": 2,
  "direction_label": "BUY",
  "confidence": 0.85,
  "entry_price": 100.0,
  "tp1": 105.0,
  "tp2": 110.0,
  "tp3": 115.0,
  "stop_loss": 95.0,
  "volatility": 0.02,
  "shap_explanation": null,
  "warnings": ["SHAP explanation unavailable - service degraded"]
}
```

---

## Monitoring Integration

### Prometheus Metrics

The health check endpoints can be integrated with Prometheus for monitoring:

```yaml
scrape_configs:
  - job_name: 'cortex-ml-health'
    metrics_path: '/api/v1/health/ml'
    scrape_interval: 30s
    static_configs:
      - targets: ['localhost:8000']
```

### Kubernetes Probes

Example Kubernetes deployment configuration:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cortex-ml-api
spec:
  template:
    spec:
      containers:
      - name: api
        image: cortex-ml-api:latest
        livenessProbe:
          httpGet:
            path: /api/v1/health/ml/live
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
          timeoutSeconds: 5
          failureThreshold: 3
        readinessProbe:
          httpGet:
            path: /api/v1/health/ml/ready
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 5
          timeoutSeconds: 3
          failureThreshold: 3
```

### Load Balancer Health Checks

Example AWS ALB target group health check configuration:

```json
{
  "HealthCheckEnabled": true,
  "HealthCheckPath": "/api/v1/health/ml/ready",
  "HealthCheckIntervalSeconds": 30,
  "HealthCheckTimeoutSeconds": 5,
  "HealthyThresholdCount": 2,
  "UnhealthyThresholdCount": 3,
  "Matcher": {
    "HttpCode": "200"
  }
}
```

---

## Testing

### Manual Testing

```bash
# Comprehensive health check
curl http://localhost:8000/api/v1/health/ml

# Readiness check
curl http://localhost:8000/api/v1/health/ml/ready

# Liveness check
curl http://localhost:8000/api/v1/health/ml/live
```

### Automated Testing

```python
import requests

def test_ml_health():
    response = requests.get("http://localhost:8000/api/v1/health/ml")
    assert response.status_code in [200, 503]
    data = response.json()
    assert "status" in data
    assert "checks" in data
    assert "database" in data["checks"]
    assert "redis" in data["checks"]
    assert "model" in data["checks"]

def test_ml_readiness():
    response = requests.get("http://localhost:8000/api/v1/health/ml/ready")
    assert response.status_code in [200, 503]
    data = response.json()
    assert "ready" in data

def test_ml_liveness():
    response = requests.get("http://localhost:8000/api/v1/health/ml/live")
    assert response.status_code == 200
    data = response.json()
    assert data["alive"] == True
```

---

## Troubleshooting

### Model Unavailable

**Symptom:** Health check returns 503 with "Model unavailable"

**Possible Causes:**
1. No active model in database
2. Model file not found at specified path
3. Model file corrupted

**Resolution:**
1. Check `ml_model_metadata` table for active models
2. Verify model file exists at `onnx_path`
3. Redeploy model if necessary

### Database Unavailable

**Symptom:** Health check returns degraded status with database unhealthy

**Possible Causes:**
1. Database connection pool exhausted
2. Database server down
3. Network connectivity issues

**Resolution:**
1. Check database connection settings
2. Verify database server is running
3. Check network connectivity
4. Review connection pool settings

### Redis Unavailable

**Symptom:** Health check returns degraded status with Redis unhealthy

**Possible Causes:**
1. Redis server down
2. Redis connection pool exhausted
3. Network connectivity issues

**Resolution:**
1. Check Redis connection settings
2. Verify Redis server is running
3. Check network connectivity
4. Review connection pool settings

---

## Requirements Mapping

- **17.5**: Model availability check, database connectivity check, Redis connectivity check
- **19.1**: Return HTTP 503 if model unavailable
- **19.2**: Return cached predictions if database unavailable, return predictions without SHAP if SHAP service unavailable
- **19.4**: Health check endpoints for monitoring
- **26.2**: Graceful degradation logic implementation
