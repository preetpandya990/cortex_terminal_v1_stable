# Health Check Integration Tests

**Test procedures for validating health check endpoints.**

---

## Overview

This document provides comprehensive test procedures for the three health check endpoints:
- `/health` (liveness)
- `/health/ready` (readiness)
- `/health/detailed` (monitoring)

---

## Manual Testing

### Prerequisites

```bash
# Start the application
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Test 1: Liveness Probe

**Endpoint:** `GET /health`

**Expected Behavior:**
- Always returns `200 OK`
- Response time <10ms
- No dependency checks

**Test Command:**
```bash
curl -i http://localhost:8000/health
```

**Expected Response:**
```http
HTTP/1.1 200 OK
content-type: application/json

{
  "status": "healthy",
  "timestamp": "2026-04-22T13:44:41.977155+00:00"
}
```

**Validation:**
- ✅ Status code is 200
- ✅ Response contains `status` field
- ✅ Response contains `timestamp` field
- ✅ Response time <10ms

---

### Test 2: Readiness Probe (Healthy)

**Endpoint:** `GET /health/ready`

**Expected Behavior:**
- Returns `200 OK` when all dependencies are healthy
- Checks database and Redis
- Response time <500ms

**Test Command:**
```bash
curl -i http://localhost:8000/health/ready
```

**Expected Response:**
```http
HTTP/1.1 200 OK
content-type: application/json

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

**Validation:**
- ✅ Status code is 200
- ✅ `status` field is "healthy"
- ✅ `checks.database.status` is "healthy"
- ✅ `checks.redis.status` is "healthy"
- ✅ Response time <500ms

---

### Test 3: Readiness Probe (Unhealthy - Database Down)

**Endpoint:** `GET /health/ready`

**Setup:**
```bash
# Stop database
docker stop cortex-postgres
```

**Expected Behavior:**
- Returns `503 Service Unavailable`
- Database check fails
- Redis check may succeed

**Test Command:**
```bash
curl -i http://localhost:8000/health/ready
```

**Expected Response:**
```http
HTTP/1.1 503 Service Unavailable
content-type: application/json

{
  "status": "unhealthy",
  "timestamp": "2026-04-22T13:44:41.981564+00:00",
  "checks": {
    "database": {
      "status": "unhealthy",
      "details": {
        "connected": false,
        "error": "timeout"
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

**Validation:**
- ✅ Status code is 503
- ✅ `status` field is "unhealthy"
- ✅ `checks.database.status` is "unhealthy"
- ✅ Response time <3s (includes timeout)

**Cleanup:**
```bash
# Restart database
docker start cortex-postgres
```

---

### Test 4: Detailed Health

**Endpoint:** `GET /health/detailed`

**Expected Behavior:**
- Always returns `200 OK`
- Includes all component checks
- Includes system and application info

**Test Command:**
```bash
curl -i http://localhost:8000/health/detailed | jq
```

**Expected Response:**
```http
HTTP/1.1 200 OK
content-type: application/json

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
        "environment": "development"
      },
      "critical": false
    }
  }
}
```

**Validation:**
- ✅ Status code is 200 (always)
- ✅ Contains `database` check
- ✅ Contains `redis` check
- ✅ Contains `system` check
- ✅ Contains `application` check
- ✅ Response time <1s

---

## Automated Testing

### Unit Tests

```python
"""
Unit tests for health check functions.
"""
import pytest
from app.core.health_checks import (
    liveness_check,
    readiness_check,
    detailed_check,
    check_database,
    check_redis,
)

@pytest.mark.asyncio
async def test_liveness_check():
    """Test liveness check always returns healthy."""
    result = await liveness_check()
    
    assert result["status"] == "healthy"
    assert "timestamp" in result


@pytest.mark.asyncio
async def test_readiness_check_structure():
    """Test readiness check returns proper structure."""
    is_ready, status = await readiness_check()
    
    assert isinstance(is_ready, bool)
    assert "status" in status
    assert "timestamp" in status
    assert "checks" in status
    assert "database" in status["checks"]
    assert "redis" in status["checks"]


@pytest.mark.asyncio
async def test_detailed_check_structure():
    """Test detailed check returns comprehensive status."""
    result = await detailed_check()
    
    assert "status" in result
    assert "timestamp" in result
    assert "checks" in result
    assert "database" in result["checks"]
    assert "redis" in result["checks"]
    assert "system" in result["checks"]
    assert "application" in result["checks"]
```

### Integration Tests

```python
"""
Integration tests for health check endpoints.
"""
import pytest
from fastapi.testclient import TestClient
from app.main import create_app

@pytest.fixture
def client():
    """Create test client."""
    app = create_app()
    return TestClient(app)


def test_liveness_endpoint(client):
    """Test /health endpoint."""
    response = client.get("/health")
    
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "timestamp" in data


def test_readiness_endpoint(client):
    """Test /health/ready endpoint."""
    response = client.get("/health/ready")
    
    # Status code depends on environment (200 or 503)
    assert response.status_code in [200, 503]
    
    data = response.json()
    assert "status" in data
    assert "timestamp" in data
    assert "checks" in data
    assert "database" in data["checks"]
    assert "redis" in data["checks"]


def test_detailed_endpoint(client):
    """Test /health/detailed endpoint."""
    response = client.get("/health/detailed")
    
    # Always returns 200
    assert response.status_code == 200
    
    data = response.json()
    assert "status" in data
    assert "timestamp" in data
    assert "checks" in data
    
    # Verify all checks present
    assert "database" in data["checks"]
    assert "redis" in data["checks"]
    assert "system" in data["checks"]
    assert "application" in data["checks"]
```

---

## Performance Testing

### Load Test with Apache Bench

```bash
# Test liveness endpoint
ab -n 1000 -c 10 http://localhost:8000/health

# Test readiness endpoint
ab -n 1000 -c 10 http://localhost:8000/health/ready

# Test detailed endpoint
ab -n 1000 -c 10 http://localhost:8000/health/detailed
```

**Expected Results:**
- Liveness: >1000 req/s, <10ms avg
- Readiness: >100 req/s, <100ms avg
- Detailed: >50 req/s, <200ms avg

---

## Kubernetes Testing

### Test in Kubernetes Cluster

```bash
# Deploy application
kubectl apply -f k8s/deployment.yaml

# Check pod status
kubectl get pods -n production -w

# Test endpoints directly
kubectl port-forward -n production cortex-ai-api-xxx 8000:8000
curl http://localhost:8000/health
curl http://localhost:8000/health/ready
curl http://localhost:8000/health/detailed
```

---

## Production Checklist

### Pre-Deployment

- [ ] All unit tests passing
- [ ] All integration tests passing
- [ ] Performance tests meet requirements
- [ ] Kubernetes manifests configured correctly
- [ ] Load balancer health checks configured
- [ ] Monitoring alerts configured

### Post-Deployment

- [ ] Verify liveness probe working
- [ ] Verify readiness probe working
- [ ] Verify pods are ready
- [ ] Verify load balancer health checks passing
- [ ] Verify monitoring dashboards showing health status
- [ ] Test failure scenarios (DB down, Redis down)
- [ ] Verify automatic recovery

---

**Last Updated:** April 22, 2026  
**Version:** 1.0.0  
**Status:** Production-ready
