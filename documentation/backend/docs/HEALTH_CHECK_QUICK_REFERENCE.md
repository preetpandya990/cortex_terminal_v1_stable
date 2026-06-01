# ML Health Check Quick Reference

## Endpoints

| Endpoint | Purpose | Success Code | Failure Code |
|----------|---------|--------------|--------------|
| `GET /api/v1/health/ml` | Comprehensive health check | 200 | 503 |
| `GET /api/v1/health/ml/ready` | Readiness probe | 200 | 503 |
| `GET /api/v1/health/ml/live` | Liveness probe | 200 | - |

## Health Checks

### Database
- ✅ Connection test
- ✅ Active model query
- ✅ Model metadata availability

### Redis
- ✅ Connection test
- ✅ Cache statistics
- ✅ Feature cache availability

### Model
- ✅ Active model in database
- ✅ Model file exists
- ✅ Model metadata complete

## Graceful Degradation

| Component | Behavior | HTTP Code | Warning |
|-----------|----------|-----------|---------|
| Model unavailable | Reject requests | 503 | "ML model unavailable" |
| Database unavailable | Use cached predictions | 200 | "Using cached prediction - database unavailable" |
| SHAP unavailable | Return predictions without SHAP | 200 | "SHAP explanation unavailable" |

## Quick Test Commands

```bash
# Comprehensive health check
curl -i http://localhost:8000/api/v1/health/ml

# Readiness check
curl -i http://localhost:8000/api/v1/health/ml/ready

# Liveness check
curl -i http://localhost:8000/api/v1/health/ml/live

# Test prediction with graceful degradation
curl -X POST http://localhost:8000/api/v1/ml/predict \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"symbol": "NSE_EQ|INE002A01018", "timeframe": "1d", "user_id": 1}'
```

## Kubernetes Configuration

```yaml
livenessProbe:
  httpGet:
    path: /api/v1/health/ml/live
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /api/v1/health/ml/ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 5
```

## Response Examples

### Healthy System
```json
{
  "status": "healthy",
  "timestamp": "2024-01-01T00:00:00Z",
  "checks": {
    "database": {"status": "healthy", "details": {...}},
    "redis": {"status": "healthy", "details": {...}},
    "model": {"status": "healthy", "details": {...}}
  }
}
```

### Degraded System
```json
{
  "status": "degraded",
  "timestamp": "2024-01-01T00:00:00Z",
  "checks": {
    "database": {"status": "unhealthy", "details": {"error": "..."}},
    "redis": {"status": "healthy", "details": {...}},
    "model": {"status": "healthy", "details": {...}}
  }
}
```

## Requirements Mapping

- **17.5**: Model availability, database connectivity, Redis connectivity checks
- **19.1**: HTTP 503 if model unavailable
- **19.2**: Cached predictions if database unavailable, predictions without SHAP if SHAP unavailable
- **19.4**: Health check endpoints
- **26.2**: Graceful degradation implementation
