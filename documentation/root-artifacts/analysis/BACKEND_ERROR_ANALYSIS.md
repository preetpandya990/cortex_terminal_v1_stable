# Backend Error Analysis & Resolution Plan

**Date**: 2026-04-23  
**Severity**: CRITICAL - Production Blocker  
**Status**: Analysis Complete, Ready for Implementation

---

## Error Summary

### 1. Circuit Breaker Async Context Manager Error (CRITICAL)
**Error**: `TypeError: 'CircuitBreaker' object does not support the asynchronous context manager protocol`

### 2. WebSocket Protocol Spam (HIGH - Performance Impact)
**Issue**: Excessive DEBUG logging of WebSocket ping/pong messages flooding logs

### 3. Data Ingestion Worker Failures (HIGH)
**Impact**: Cannot fetch OHLCV data from Upstox API

---

## Detailed Analysis

### Error 1: Circuit Breaker Async Context Manager

**Location**: `backend/app/services/upstox_client.py:209`

**Problem**: Using `async with upstox_breaker:` but `aiobreaker.CircuitBreaker` doesn't support async context manager protocol

**Evidence from Code**:
```python
# Line 209 - INCORRECT USAGE
async with upstox_breaker:  # ❌ CircuitBreaker is NOT an async context manager
    response = await self._client.request(...)
```

**Root Cause**:
- `aiobreaker.CircuitBreaker` is a **decorator**, not a context manager
- It should be used as `@upstox_breaker` decorator or `.call()` method
- Using `async with` requires `__aenter__` and `__aexit__` methods which CircuitBreaker doesn't have

**Impact**:
- Data ingestion worker completely broken
- All Upstox API calls failing
- No OHLCV data being fetched
- Worker logs flooded with errors

**Affected Services**:
- Data ingestion worker (2553 instruments failing)
- Any service using Upstox API
- Market scanner (depends on OHLCV data)
- ML predictions (depends on features from OHLCV)

---

### Error 2: WebSocket Protocol Spam

**Location**: Backend API logs (uvicorn.error)

**Problem**: DEBUG level logging enabled for WebSocket ping/pong messages

**Evidence from Logs**:
```json
{"level": "DEBUG", "name": "uvicorn.error", "message": "% sending keepalive ping"}
{"level": "DEBUG", "name": "uvicorn.error", "message": "> PING fa c7 ad 08 [binary, 4 bytes]"}
{"level": "DEBUG", "name": "uvicorn.error", "message": "< PONG fa c7 ad 08 [binary, 4 bytes]"}
{"level": "DEBUG", "name": "uvicorn.error", "message": "% received keepalive pong"}
```

**Frequency**: Every ~1 second per WebSocket connection

**Impact**:
- Log files growing rapidly (GB/hour with multiple connections)
- Performance degradation (I/O overhead)
- Difficult to find actual errors in logs
- Increased storage costs
- Log rotation issues

**Root Cause**:
- Uvicorn log level set to DEBUG
- WebSocket keepalive messages logged at DEBUG level
- Frontend infinite loop creating multiple connections
- Each connection generates 4 log entries per second

**Calculation**:
- 10 WebSocket connections × 4 logs/sec × 3600 sec/hour = 144,000 log entries/hour
- At ~200 bytes/entry = 28.8 MB/hour just for ping/pong
- With JSON formatting = ~50-100 MB/hour

---

### Error 3: Data Ingestion Worker Failures

**Location**: `backend/app/services/data_ingestion_worker.py:391`

**Problem**: Cannot fetch OHLCV data due to circuit breaker error

**Evidence from Logs**:
```
2026-04-23 02:11:25,517 [ERROR] app.services.data_ingestion_worker: Unexpected error | 
SUMEETINDS|1hour|2023-10-10→2024-01-06 | 'CircuitBreaker' object does not support the 
asynchronous context manager protocol
```

**Impact**:
- 2553 instruments failing to fetch data
- No historical backfill
- No real-time data updates
- Database remains empty
- ML models cannot make predictions
- Scanner cannot analyze stocks

---

## Root Cause Analysis

### Primary Issue: Incorrect Circuit Breaker Usage

The `aiobreaker` library provides **three ways** to use circuit breakers:

#### 1. Decorator Pattern (Recommended)
```python
@upstox_breaker
async def fetch_data():
    return await api_call()
```

#### 2. Call Method
```python
result = await upstox_breaker.call(api_call, *args, **kwargs)
```

#### 3. Manual Try/Catch
```python
try:
    result = await api_call()
    upstox_breaker.call_succeeded()
except Exception as e:
    upstox_breaker.call_failed()
    raise
```

**❌ NOT SUPPORTED: Async Context Manager**
```python
async with upstox_breaker:  # ❌ WRONG - Not supported
    result = await api_call()
```

---

## Solution Architecture

### Approach 1: Decorator Pattern (Recommended)

**Pros**:
- ✅ Clean, Pythonic syntax
- ✅ Automatic error handling
- ✅ No boilerplate code
- ✅ Industry standard pattern

**Cons**:
- Requires refactoring `_request` method

**Implementation**:
```python
@upstox_breaker
async def _request_with_breaker(self, method: str, path: str, **kwargs):
    """Make HTTP request with circuit breaker protection."""
    response = await self._client.request(
        method,
        encoded_path,
        headers={**self._auth_headers(), **kwargs.pop("headers", {})},
        **kwargs,
    )
    response.raise_for_status()
    return response.json()

async def _request(self, method: str, path: str, **kwargs):
    """Public request method."""
    try:
        return await self._request_with_breaker(method, path, **kwargs)
    except CircuitBreakerError:
        handle_circuit_breaker_error("upstox", CircuitBreakerError())
        if settings.ENVIRONMENT == "development":
            return self._get_mock_response(path)
        raise
```

### Approach 2: Call Method (Alternative)

**Pros**:
- ✅ Minimal code changes
- ✅ Inline usage
- ✅ Explicit control

**Cons**:
- More verbose
- Requires lambda or functools.partial

**Implementation**:
```python
try:
    result = await upstox_breaker.call(
        self._client.request,
        method,
        encoded_path,
        headers={**self._auth_headers(), **kwargs.pop("headers", {})},
        **kwargs,
    )
    result.raise_for_status()
    return result.json()
except CircuitBreakerError:
    handle_circuit_breaker_error("upstox", CircuitBreakerError())
    if settings.ENVIRONMENT == "development":
        return self._get_mock_response(path)
    raise
```

### Approach 3: Manual State Management (Not Recommended)

**Pros**:
- Full control

**Cons**:
- ❌ Boilerplate code
- ❌ Error-prone
- ❌ Must manually track success/failure

---

## Implementation Plan

### Phase 1: Fix Circuit Breaker (15 minutes)

**File**: `backend/app/services/upstox_client.py`

**Changes**:
1. Remove `async with upstox_breaker:` (line 209)
2. Implement decorator pattern
3. Add proper error handling
4. Test with mock data
5. Verify circuit breaker metrics

**Testing**:
- [ ] Circuit breaker opens after 5 failures
- [ ] Circuit breaker closes after recovery timeout
- [ ] Metrics tracked correctly
- [ ] Mock data returned in development
- [ ] Production errors handled gracefully

### Phase 2: Fix WebSocket Logging (5 minutes)

**File**: `backend/app/main.py` or uvicorn startup

**Changes**:
1. Set uvicorn log level to INFO (not DEBUG)
2. Add custom logging filter for WebSocket messages
3. Keep ERROR and WARNING logs
4. Remove DEBUG ping/pong spam

**Options**:

#### Option A: Change Log Level (Quick Fix)
```python
# In main.py or startup
logging.getLogger("uvicorn.error").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
```

#### Option B: Custom Filter (Production Grade)
```python
class WebSocketPingPongFilter(logging.Filter):
    """Filter out WebSocket ping/pong debug messages."""
    def filter(self, record):
        if record.levelno == logging.DEBUG:
            message = record.getMessage()
            if any(x in message for x in ['keepalive ping', 'keepalive pong', 'PING', 'PONG']):
                return False
        return True

# Apply filter
logging.getLogger("uvicorn.error").addFilter(WebSocketPingPongFilter())
```

### Phase 3: Verify Data Ingestion (10 minutes)

**Steps**:
1. Restart worker with fixed circuit breaker
2. Monitor logs for successful API calls
3. Verify OHLCV data being inserted
4. Check database for new records
5. Validate circuit breaker metrics

**Success Criteria**:
- [ ] No circuit breaker errors
- [ ] API calls succeeding
- [ ] Data being inserted into database
- [ ] Worker logs clean (no spam)
- [ ] Prometheus metrics showing success

### Phase 4: Frontend WebSocket Fix (30 minutes)

**File**: `frontend/src/hooks/useWebSocket.ts`

**Changes**: (As documented in FRONTEND_ERROR_ANALYSIS.md)
1. Complete rewrite with stable references
2. Single useEffect for connection
3. Functional state updates
4. Proper cleanup

---

## Production-Grade Circuit Breaker Pattern

### Recommended Implementation

```python
# backend/app/services/upstox_client.py

from functools import wraps
from aiobreaker import CircuitBreakerError
from app.core.circuit_breaker import upstox_breaker, handle_circuit_breaker_error

class UpstoxClient:
    
    @upstox_breaker
    async def _make_request(
        self,
        method: str,
        encoded_path: str,
        headers: dict,
        **kwargs
    ) -> dict:
        """
        Make HTTP request with circuit breaker protection.
        
        This method is decorated with @upstox_breaker which:
        - Tracks failures and opens circuit after threshold
        - Fails fast when circuit is open
        - Automatically tests recovery in half-open state
        - Updates Prometheus metrics
        """
        try:
            response = await self._client.request(
                method,
                encoded_path,
                headers=headers,
                **kwargs,
            )
            response.raise_for_status()
            return response.json()
        
        except httpx.HTTPStatusError as exc:
            # Handle 401 in development
            if exc.response.status_code == 401 and settings.ENVIRONMENT == "development":
                logger.warning(f"Upstox 401 error - returning mock data for {method} {encoded_path}")
                return self._get_mock_response(encoded_path)
            
            logger.error(
                "Upstox API error: %s %s → %d",
                method,
                encoded_path,
                exc.response.status_code,
            )
            raise UpstoxAPIError(
                f"Upstream API returned {exc.response.status_code}",
                upstream_status=exc.response.status_code,
            )
        
        except httpx.TimeoutException:
            logger.error("Upstox API timeout: %s %s", method, encoded_path)
            raise UpstoxConnectionError("Request to market data API timed out")
        
        except httpx.ConnectError:
            logger.error("Upstox connection error: %s %s", method, encoded_path)
            raise UpstoxConnectionError("Cannot reach market data API")
    
    async def _request(self, method: str, path: str, **kwargs) -> dict:
        """
        Public request method with circuit breaker error handling.
        """
        # Development mode: Return mock data if no token
        if not self._access_token and settings.ENVIRONMENT == "development":
            logger.warning(f"Upstox token not configured - returning mock data for {method} {path}")
            return self._get_mock_response(path)
        
        # Build encoded path
        from urllib.parse import quote
        encoded_path = '/'.join(
            quote(segment, safe='') for segment in path.split('/') if segment
        )
        
        # Prepare headers
        headers = {**self._auth_headers(), **kwargs.pop("headers", {})}
        
        try:
            # Call circuit breaker protected method
            return await self._make_request(method, encoded_path, headers, **kwargs)
        
        except CircuitBreakerError:
            # Circuit is open - fail fast
            handle_circuit_breaker_error("upstox", CircuitBreakerError())
            
            # In development, return mock data when circuit is open
            if settings.ENVIRONMENT == "development":
                logger.warning(f"Circuit breaker open - returning mock data for {method} {path}")
                return self._get_mock_response(path)
            
            # In production, raise error
            raise UpstoxConnectionError(
                "Upstox API circuit breaker is open (service unavailable)"
            )
```

---

## Testing Strategy

### Unit Tests
```python
# tests/unit/test_circuit_breaker.py

@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_failures():
    """Test circuit opens after threshold failures."""
    client = UpstoxClient()
    
    # Simulate 5 failures
    for _ in range(5):
        with pytest.raises(UpstoxAPIError):
            await client.get("/invalid-endpoint")
    
    # Circuit should be open
    assert upstox_breaker.current_state == 'open'
    
    # Next call should fail fast
    with pytest.raises(CircuitBreakerError):
        await client.get("/any-endpoint")

@pytest.mark.asyncio
async def test_circuit_breaker_recovers():
    """Test circuit recovers after timeout."""
    client = UpstoxClient()
    
    # Open circuit
    for _ in range(5):
        with pytest.raises(UpstoxAPIError):
            await client.get("/invalid-endpoint")
    
    # Wait for recovery timeout
    await asyncio.sleep(61)
    
    # Circuit should be half-open
    assert upstox_breaker.current_state == 'half_open'
    
    # Successful call should close circuit
    result = await client.get("/valid-endpoint")
    assert upstox_breaker.current_state == 'closed'
```

### Integration Tests
```python
# tests/integration/test_data_ingestion.py

@pytest.mark.asyncio
async def test_data_ingestion_with_circuit_breaker():
    """Test data ingestion handles circuit breaker correctly."""
    worker = DataIngestionWorker(upstox_client)
    
    # Should succeed with valid API
    result = await worker.fetch_chunk("NSE_EQ|INE002A01018", "1d", start, end)
    assert result is not None
    
    # Should handle circuit breaker gracefully
    # (simulate by opening circuit manually)
    upstox_breaker._state = 'open'
    
    with pytest.raises(CircuitBreakerError):
        await worker.fetch_chunk("NSE_EQ|INE002A01018", "1d", start, end)
```

---

## Success Criteria

### Must Have ✅
- [ ] No circuit breaker async context manager errors
- [ ] Data ingestion worker fetching data successfully
- [ ] WebSocket logs not spamming (< 10 logs/minute)
- [ ] Circuit breaker metrics tracked correctly
- [ ] Proper error handling in all scenarios

### Performance Targets
- [ ] API call latency: <500ms (P95)
- [ ] Circuit breaker overhead: <1ms
- [ ] Log volume: <100 MB/hour
- [ ] Worker throughput: 400 requests/minute

### Code Quality
- [ ] Type hints: 100% coverage
- [ ] Unit tests: >80% coverage
- [ ] Integration tests: Critical paths covered
- [ ] Documentation: Complete

---

## Risk Assessment

### Low Risk ✅
- Circuit breaker fix (well-tested pattern)
- Log level changes (reversible)

### Medium Risk ⚠️
- Data ingestion restart (may need backfill)
- WebSocket logging filter (test thoroughly)

### Mitigation
1. Test in development first
2. Monitor Prometheus metrics
3. Keep old code as backup
4. Gradual rollout
5. Rollback plan ready

---

## Timeline

**Total Estimated Time**: 60 minutes

1. **Circuit Breaker Fix**: 15 min
2. **WebSocket Logging Fix**: 5 min
3. **Testing & Verification**: 10 min
4. **Frontend WebSocket Fix**: 30 min

---

## Next Steps

**AWAITING APPROVAL** to proceed with implementation.

**Questions**:
1. Approve decorator pattern for circuit breaker?
2. Approve log level change to INFO?
3. Should we add custom WebSocket logging filter?
4. Restart services after fixes?

---

## References

- [aiobreaker Documentation](https://github.com/abulte/aiobreaker)
- [Circuit Breaker Pattern - Martin Fowler](https://martinfowler.com/bliki/CircuitBreaker.html)
- [Production Logging Best Practices 2026](https://copyprogramming.com/howto/python-logging-best-practices)

---

**Report Generated**: 2026-04-23  
**Status**: Ready for Implementation  
**Priority**: CRITICAL - Production Blocker
