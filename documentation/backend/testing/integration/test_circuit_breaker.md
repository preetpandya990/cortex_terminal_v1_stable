# Circuit Breaker Integration Tests

**Test procedures for validating circuit breaker behavior.**

---

## Manual Testing

### Test 1: Normal Operation (CLOSED State)

**Objective:** Verify requests pass through when circuit is closed.

```bash
# Start application
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload

# Make successful requests
curl http://localhost:8000/api/v1/market-data/quote/ltp?symbol=NSE_EQ%7CINE002A01018

# Check metrics
curl -s http://localhost:8000/metrics | grep circuit_breaker_state
# Expected: circuit_breaker_state{service="upstox"} 0

curl -s http://localhost:8000/metrics | grep circuit_breaker_successes_total
# Expected: circuit_breaker_successes_total{service="upstox"} > 0
```

**Expected Results:**
- ✅ Requests succeed
- ✅ Circuit state = 0 (CLOSED)
- ✅ Success counter increments

---

### Test 2: Circuit Opens After Failures

**Objective:** Verify circuit opens after threshold failures.

**Setup:**
```bash
# Stop Upstox mock or use invalid token to trigger failures
export UPSTOX_ACCESS_TOKEN="invalid_token"
```

**Test:**
```bash
# Make 5+ requests (failure threshold)
for i in {1..6}; do
  curl http://localhost:8000/api/v1/market-data/quote/ltp?symbol=NSE_EQ%7CINE002A01018
  echo "Request $i"
done

# Check circuit state
curl -s http://localhost:8000/metrics | grep 'circuit_breaker_state{service="upstox"}'
# Expected: circuit_breaker_state{service="upstox"} 1

# Check failures
curl -s http://localhost:8000/metrics | grep circuit_breaker_failures_total
# Expected: circuit_breaker_failures_total{service="upstox"} >= 5
```

**Expected Results:**
- ✅ First 5 requests fail with API errors
- ✅ 6th request fails immediately (circuit open)
- ✅ Circuit state = 1 (OPEN)
- ✅ Failure counter >= 5

---

### Test 3: Circuit Rejects Requests When Open

**Objective:** Verify requests are rejected when circuit is open.

```bash
# With circuit open from Test 2
curl -v http://localhost:8000/api/v1/market-data/quote/ltp?symbol=NSE_EQ%7CINE002A01018

# Check rejections
curl -s http://localhost:8000/metrics | grep circuit_breaker_rejections_total
# Expected: circuit_breaker_rejections_total{service="upstox"} > 0
```

**Expected Results:**
- ✅ Request fails immediately (no timeout wait)
- ✅ Error: "circuit breaker is open"
- ✅ Rejection counter increments
- ✅ Response time <100ms (fail fast)

---

### Test 4: Circuit Recovers (HALF_OPEN → CLOSED)

**Objective:** Verify automatic recovery after timeout.

**Setup:**
```bash
# Restore valid token
export UPSTOX_ACCESS_TOKEN="valid_token"

# Wait for recovery timeout (60 seconds)
sleep 65
```

**Test:**
```bash
# Make request (should test recovery)
curl http://localhost:8000/api/v1/market-data/quote/ltp?symbol=NSE_EQ%7CINE002A01018

# Check circuit state
curl -s http://localhost:8000/metrics | grep 'circuit_breaker_state{service="upstox"}'
# Expected: circuit_breaker_state{service="upstox"} 0 (CLOSED)
```

**Expected Results:**
- ✅ Circuit transitions to HALF_OPEN
- ✅ First request succeeds
- ✅ Circuit transitions to CLOSED
- ✅ Subsequent requests succeed

---

## Automated Testing

### Unit Tests

```python
"""
Unit tests for circuit breaker functionality.
"""
import pytest
import asyncio
from datetime import timedelta
from aiobreaker import CircuitBreaker, CircuitBreakerError
from app.core.circuit_breaker import create_circuit_breaker

@pytest.mark.asyncio
async def test_circuit_breaker_creation():
    """Test circuit breaker is created with correct configuration."""
    breaker = create_circuit_breaker(
        service_name="test_service",
        failure_threshold=3,
        recovery_timeout=30
    )
    
    assert breaker.fail_max == 3
    assert breaker.timeout_duration == timedelta(seconds=30)
    assert breaker.name == "test_service"


@pytest.mark.asyncio
async def test_circuit_opens_after_failures():
    """Test circuit opens after threshold failures."""
    breaker = create_circuit_breaker(
        service_name="test_service",
        failure_threshold=3,
        recovery_timeout=1
    )
    
    # Simulate 3 failures
    for _ in range(3):
        try:
            async with breaker:
                raise Exception("Simulated failure")
        except Exception:
            pass
    
    # Circuit should be open
    assert breaker.current_state == "open"


@pytest.mark.asyncio
async def test_circuit_rejects_when_open():
    """Test circuit rejects requests when open."""
    breaker = create_circuit_breaker(
        service_name="test_service",
        failure_threshold=2,
        recovery_timeout=1
    )
    
    # Open the circuit
    for _ in range(2):
        try:
            async with breaker:
                raise Exception("Failure")
        except Exception:
            pass
    
    # Next request should be rejected
    with pytest.raises(CircuitBreakerError):
        async with breaker:
            pass


@pytest.mark.asyncio
async def test_circuit_recovers():
    """Test circuit recovers after timeout."""
    breaker = create_circuit_breaker(
        service_name="test_service",
        failure_threshold=2,
        recovery_timeout=1
    )
    
    # Open the circuit
    for _ in range(2):
        try:
            async with breaker:
                raise Exception("Failure")
        except Exception:
            pass
    
    assert breaker.current_state == "open"
    
    # Wait for recovery timeout
    await asyncio.sleep(1.5)
    
    # Next request should test recovery (half-open)
    async with breaker:
        pass  # Success
    
    # Circuit should be closed
    assert breaker.current_state == "closed"
```

---

## Integration Tests

```python
"""
Integration tests for circuit breaker with Upstox client.
"""
import pytest
from unittest.mock import patch, AsyncMock
from app.services.upstox_client import UpstoxClient
from app.exceptions import UpstoxConnectionError

@pytest.mark.asyncio
async def test_upstox_client_with_circuit_breaker():
    """Test Upstox client uses circuit breaker."""
    client = UpstoxClient()
    await client.start()
    client.set_access_token("test_token")
    
    # Mock httpx client to simulate failures
    with patch.object(client._client, 'request', side_effect=Exception("Connection error")):
        # Make 5 requests to open circuit
        for _ in range(5):
            with pytest.raises(UpstoxConnectionError):
                await client.get("market-quote/ltp")
        
        # 6th request should be rejected by circuit breaker
        with pytest.raises(UpstoxConnectionError) as exc_info:
            await client.get("market-quote/ltp")
        
        assert "circuit breaker is open" in str(exc_info.value)
    
    await client.stop()
```

---

## Performance Testing

### Load Test

```bash
# Install hey (HTTP load testing tool)
go install github.com/rakyll/hey@latest

# Test with circuit breaker
hey -n 1000 -c 10 -m GET \
  "http://localhost:8000/api/v1/market-data/quote/ltp?symbol=NSE_EQ%7CINE002A01018"
```

**Expected Results:**
- Requests/sec: >100
- Circuit breaker overhead: <1ms
- No memory leaks

---

## Production Checklist

### Pre-Deployment

- [ ] Circuit breaker integrated with all external API calls
- [ ] Metrics exported to Prometheus
- [ ] Grafana dashboard configured
- [ ] Alerting rules configured
- [ ] Thresholds tuned for production load
- [ ] Documentation reviewed

### Post-Deployment

- [ ] Verify circuit breaker metrics in Grafana
- [ ] Test failure scenarios in staging
- [ ] Monitor circuit breaker state
- [ ] Verify automatic recovery
- [ ] Review alert thresholds

---

## Troubleshooting Tests

### Test: Verify Metrics Export

```bash
curl -s http://localhost:8000/metrics | grep circuit_breaker

# Expected output:
# circuit_breaker_state{service="upstox"} 0
# circuit_breaker_failures_total{service="upstox",exception_type="..."} N
# circuit_breaker_successes_total{service="upstox"} N
# circuit_breaker_rejections_total{service="upstox"} N
```

### Test: Verify State Transitions

```bash
# Monitor state changes in real-time
watch -n 1 'curl -s http://localhost:8000/metrics | grep circuit_breaker_state'
```

---

**Last Updated:** April 22, 2026  
**Version:** 1.0.0  
**Status:** Production-ready
