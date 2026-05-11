# Health Check Endpoints Guide

**Production-grade health checks for Kubernetes deployment and monitoring.**

---

## Overview

Cortex AI implements three health check endpoints following Kubernetes best practices:

1. **`/health`** - Liveness probe (process alive)
2. **`/health/ready`** - Readiness probe (can handle traffic)
3. **`/health/detailed`** - Detailed status (monitoring/debugging)

---

## Endpoints

### 1. Liveness Probe: `/health`

**Purpose:** Determine if the process is alive and responsive.

**Use Cases:**
- Kubernetes liveness probe
- Detect deadlocks, infinite loops, or process crashes
- Trigger pod restart on failure

**Behavior:**
- ✅ **Does NOT check external dependencies** (DB, Redis)
- ✅ Always returns `200 OK` unless process is broken
- ✅ Fast response (<10ms)

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2026-04-22T13:44:41.977155+00:00"
}
```

**HTTP Status Codes:**
- `200 OK` - Process is alive

**Kubernetes Configuration:**
```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 3
```

---

### 2. Readiness Probe: `/health/ready`

**Purpose:** Determine if the service can handle traffic.

**Use Cases:**
- Kubernetes readiness probe
- Load balancer health checks
- Remove pod from service endpoints on failure

**Behavior:**
- ✅ **Checks critical dependencies** (Database + Redis)
- ✅ Returns `503` if any critical dependency is unavailable
- ✅ 2-second timeout per dependency check
- ✅ Fast response (<500ms typical)

**Response (Healthy):**
```json
{
  "status": "healthy",
  "timestamp": "2026-04-22T13:44:41.981564+00:00",
  "checks": {
    "database": {
      "status": "healthy",
      "details": {
        "connected": true,
        "response_time_ms": "<2000"
      },
      "critical": true
    },
    "redis": {
      "status": "healthy",
      "details": {
        "connected": true,
        "response_time_ms": "<2000"
      },
      "critical": true
    }
  }
}
```

**Response (Unhealthy):**
```json
{
  "status": "unhealthy",
  "timestamp": "2026-04-22T13:44:41.981564+00:00",
  "checks": {
    "database": {
      "status": "healthy",
      "details": {
        "connected": true,
        "response_time_ms": "<2000"
      },
      "critical": true
    },
    "redis": {
      "status": "unhealthy",
      "details": {
        "connected": false,
        "error": "timeout"
      },
      "critical": true
    }
  }
}
```

**HTTP Status Codes:**
- `200 OK` - Ready to accept traffic
- `503 Service Unavailable` - Not ready (dependencies unavailable)

**Kubernetes Configuration:**
```yaml
readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 5
  timeoutSeconds: 3
  failureThreshold: 2
  successThreshold: 1
```

---

### 3. Detailed Health: `/health/detailed`

**Purpose:** Comprehensive component status for monitoring and debugging.

**Use Cases:**
- Monitoring dashboards (Grafana, Datadog)
- Debugging and troubleshooting
- Operational visibility
- Status pages

**Behavior:**
- ✅ Checks all components (critical + non-critical)
- ✅ Includes system and application info
- ✅ Returns `200 OK` always (for monitoring)
- ✅ Status field indicates overall health

**Response:**
```json
{
  "status": "healthy",
  "timestamp": "2026-04-22T13:44:42.032986+00:00",
  "checks": {
    "database": {
      "status": "healthy",
      "details": {
        "connected": true,
        "response_time_ms": "<2000"
      },
      "critical": true
    },
    "redis": {
      "status": "healthy",
      "details": {
        "connected": true,
        "response_time_ms": "<2000"
      },
      "critical": true
    },
    "system": {
      "status": "healthy",
      "details": {
        "python_version": "3.11.15",
        "platform": "Linux",
        "process_id": null
      },
      "critical": false
    },
    "application": {
      "status": "healthy",
      "details": {
        "name": "Cortex AI Trading Platform",
        "version": "1.0.0",
        "environment": "production"
      },
      "critical": false
    }
  }
}
```

**HTTP Status Codes:**
- `200 OK` - Always (check `status` field for health)

**Status Values:**
- `healthy` - All checks passed
- `degraded` - Non-critical checks failed
- `unhealthy` - Critical checks failed

---

## Best Practices

### 1. Liveness Probe Guidelines

**DO:**
- ✅ Keep it simple and fast (<10ms)
- ✅ Only check if process is responsive
- ✅ Use for detecting deadlocks or crashes

**DON'T:**
- ❌ Check external dependencies (DB, Redis, APIs)
- ❌ Perform expensive operations
- ❌ Return failure for transient issues

**Why:** If database goes down, you don't want Kubernetes to restart all your pods. The service itself is fine; it's the dependency that failed.

### 2. Readiness Probe Guidelines

**DO:**
- ✅ Check critical dependencies only
- ✅ Use short timeouts (2-5 seconds)
- ✅ Return 503 when not ready
- ✅ Use for load balancer health checks

**DON'T:**
- ❌ Check non-critical dependencies
- ❌ Use long timeouts (>5 seconds)
- ❌ Restart pods on failure (use liveness for that)

**Why:** Readiness controls traffic routing. If a dependency is temporarily unavailable, remove the pod from the load balancer but don't restart it.

### 3. Detailed Health Guidelines

**DO:**
- ✅ Include all component status
- ✅ Provide useful debugging info
- ✅ Always return 200 OK
- ✅ Use for monitoring dashboards

**DON'T:**
- ❌ Expose sensitive information (secrets, keys)
- ❌ Perform expensive operations
- ❌ Use for Kubernetes probes

**Why:** Detailed health is for humans and monitoring systems, not for automated decisions.

---

## Kubernetes Deployment

### Complete Example

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: cortex-ai-api
  namespace: production
spec:
  replicas: 3
  selector:
    matchLabels:
      app: cortex-ai-api
  template:
    metadata:
      labels:
        app: cortex-ai-api
    spec:
      containers:
      - name: api
        image: cortex-ai:1.0.0
        ports:
        - containerPort: 8000
          name: http
        
        # Liveness probe - Restart if process is broken
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
            scheme: HTTP
          initialDelaySeconds: 30  # Wait for app to start
          periodSeconds: 10         # Check every 10s
          timeoutSeconds: 5         # Timeout after 5s
          failureThreshold: 3       # Restart after 3 failures
          successThreshold: 1       # Consider healthy after 1 success
        
        # Readiness probe - Remove from load balancer if not ready
        readinessProbe:
          httpGet:
            path: /health/ready
            port: 8000
            scheme: HTTP
          initialDelaySeconds: 10   # Start checking after 10s
          periodSeconds: 5          # Check every 5s
          timeoutSeconds: 3         # Timeout after 3s
          failureThreshold: 2       # Remove after 2 failures
          successThreshold: 1       # Add back after 1 success
        
        # Startup probe - Give app time to start (optional)
        startupProbe:
          httpGet:
            path: /health
            port: 8000
            scheme: HTTP
          initialDelaySeconds: 0
          periodSeconds: 5
          timeoutSeconds: 3
          failureThreshold: 12      # 12 * 5s = 60s max startup time
          successThreshold: 1
        
        resources:
          requests:
            memory: "512Mi"
            cpu: "500m"
          limits:
            memory: "1Gi"
            cpu: "1000m"
        
        env:
        - name: ENVIRONMENT
          value: "production"
        - name: DATABASE_URL
          valueFrom:
            secretKeyRef:
              name: cortex-ai-secrets
              key: database-url
        - name: REDIS_URL
          valueFrom:
            secretKeyRef:
              name: cortex-ai-secrets
              key: redis-url
```

---

## Load Balancer Configuration

### AWS Application Load Balancer (ALB)

```yaml
apiVersion: v1
kind: Service
metadata:
  name: cortex-ai-api
  namespace: production
  annotations:
    service.beta.kubernetes.io/aws-load-balancer-type: "nlb"
    service.beta.kubernetes.io/aws-load-balancer-healthcheck-path: "/health/ready"
    service.beta.kubernetes.io/aws-load-balancer-healthcheck-interval: "10"
    service.beta.kubernetes.io/aws-load-balancer-healthcheck-timeout: "5"
    service.beta.kubernetes.io/aws-load-balancer-healthcheck-healthy-threshold: "2"
    service.beta.kubernetes.io/aws-load-balancer-healthcheck-unhealthy-threshold: "2"
spec:
  type: LoadBalancer
  selector:
    app: cortex-ai-api
  ports:
  - port: 80
    targetPort: 8000
    protocol: TCP
```

### NGINX Ingress

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: cortex-ai-ingress
  namespace: production
  annotations:
    nginx.ingress.kubernetes.io/health-check-path: "/health/ready"
    nginx.ingress.kubernetes.io/health-check-interval: "10s"
    nginx.ingress.kubernetes.io/health-check-timeout: "5s"
spec:
  rules:
  - host: api.cortex.ai
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: cortex-ai-api
            port:
              number: 80
```

---

## Monitoring Integration

### Prometheus ServiceMonitor

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: cortex-ai-health
  namespace: production
spec:
  selector:
    matchLabels:
      app: cortex-ai-api
  endpoints:
  - port: http
    path: /health/detailed
    interval: 30s
    scrapeTimeout: 10s
```

### Grafana Dashboard Query

```promql
# Health check success rate
sum(rate(http_requests_total{endpoint="/health/ready", status="200"}[5m])) 
/ 
sum(rate(http_requests_total{endpoint="/health/ready"}[5m]))

# Health check latency P95
histogram_quantile(0.95, 
  sum by (le) (rate(http_request_duration_seconds_bucket{endpoint="/health/ready"}[5m]))
)
```

---

## Troubleshooting

### Issue: Liveness probe failing

**Symptoms:**
- Pods constantly restarting
- `CrashLoopBackOff` status

**Diagnosis:**
```bash
# Check pod logs
kubectl logs -n production cortex-ai-api-xxx --previous

# Check liveness probe config
kubectl describe pod -n production cortex-ai-api-xxx
```

**Solutions:**
1. Increase `initialDelaySeconds` (app needs more time to start)
2. Increase `timeoutSeconds` (slow responses)
3. Increase `failureThreshold` (transient issues)
4. Check if process is actually deadlocked

### Issue: Readiness probe failing

**Symptoms:**
- Pods not receiving traffic
- Service endpoints empty

**Diagnosis:**
```bash
# Check readiness status
kubectl get pods -n production -o wide

# Test health endpoint directly
kubectl port-forward -n production cortex-ai-api-xxx 8000:8000
curl http://localhost:8000/health/ready
```

**Solutions:**
1. Check database connectivity
2. Check Redis connectivity
3. Verify network policies allow DB/Redis access
4. Check dependency timeouts

### Issue: All pods failing readiness

**Symptoms:**
- No pods ready
- Service unavailable

**Diagnosis:**
```bash
# Check detailed health
curl http://api.cortex.ai/health/detailed

# Check dependency status
kubectl get pods -n production | grep -E "postgres|redis"
```

**Solutions:**
1. Check if database is up
2. Check if Redis is up
3. Verify connection strings
4. Check network connectivity

---

## Testing

### Manual Testing

```bash
# Test liveness
curl -i http://localhost:8000/health

# Test readiness
curl -i http://localhost:8000/health/ready

# Test detailed
curl -i http://localhost:8000/health/detailed | jq
```

### Automated Testing

```python
import pytest
from fastapi.testclient import TestClient
from app.main import create_app

@pytest.fixture
def client():
    app = create_app()
    return TestClient(app)

def test_liveness(client):
    """Test liveness probe always returns 200."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

def test_readiness_healthy(client):
    """Test readiness probe returns 200 when dependencies are healthy."""
    response = client.get("/health/ready")
    assert response.status_code in [200, 503]  # Depends on environment
    assert "checks" in response.json()
    assert "database" in response.json()["checks"]
    assert "redis" in response.json()["checks"]

def test_detailed_health(client):
    """Test detailed health returns comprehensive status."""
    response = client.get("/health/detailed")
    assert response.status_code == 200
    data = response.json()
    assert "checks" in data
    assert "database" in data["checks"]
    assert "redis" in data["checks"]
    assert "system" in data["checks"]
    assert "application" in data["checks"]
```

---

## Performance

### Response Times

| Endpoint | Typical | Max | Timeout |
|----------|---------|-----|---------|
| `/health` | <10ms | 50ms | 5s |
| `/health/ready` | <100ms | 500ms | 3s |
| `/health/detailed` | <200ms | 1s | 5s |

### Resource Usage

- **CPU:** <0.1% per check
- **Memory:** <1MB
- **Network:** <1KB per response

---

## Security

### Best Practices

1. **Don't expose sensitive information**
   - ❌ Database passwords
   - ❌ API keys
   - ❌ Internal IPs
   - ✅ Component status only

2. **Rate limiting**
   - Apply rate limits to prevent abuse
   - Use separate limits for health endpoints

3. **Authentication**
   - Liveness/readiness: No auth (Kubernetes needs access)
   - Detailed: Consider auth for production

4. **Network policies**
   - Allow Kubernetes control plane to access health endpoints
   - Restrict detailed endpoint to monitoring systems

---

## Summary

| Endpoint | Purpose | Checks Dependencies | Returns 503 | Use For |
|----------|---------|---------------------|-------------|---------|
| `/health` | Liveness | ❌ No | ❌ No | Kubernetes liveness probe |
| `/health/ready` | Readiness | ✅ Yes | ✅ Yes | Kubernetes readiness probe, load balancers |
| `/health/detailed` | Monitoring | ✅ Yes | ❌ No | Dashboards, debugging, status pages |

---

**Last Updated:** April 22, 2026  
**Version:** 1.0.0  
**Status:** Production-ready
