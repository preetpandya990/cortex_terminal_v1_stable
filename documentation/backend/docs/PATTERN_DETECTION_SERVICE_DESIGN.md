# Pattern Detection Service - Production Architecture Design
**Date**: 2026-05-11  
**Version**: 1.0  
**Quality Standard**: Billion-dollar app - world-class, production-ready, industry standards

---

## Executive Summary

This document defines the production architecture for the Pattern Detection Service, a critical component of the AI Analysis Cards feature. The design follows 2026 industry best practices for ML inference serving, incorporating multi-tier caching, dynamic batching, graceful degradation, and comprehensive observability.

**Key Design Principles**:
- ✅ **Performance**: Sub-100ms p95 latency, >10K requests/second throughput
- ✅ **Reliability**: 99.9% uptime with graceful degradation
- ✅ **Scalability**: Horizontal scaling with stateless design
- ✅ **Observability**: Comprehensive metrics, logging, and tracing
- ✅ **Security**: JWT auth, rate limiting, input validation

---

## 1. Service Architecture

### 1.1 High-Level Design

```
┌─────────────────────────────────────────────────────────────────┐
│                     API Gateway Layer                            │
│  • Rate Limiting (100/min)                                       │
│  • JWT Authentication                                            │
│  • Request Validation                                            │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              Pattern Detection Service                           │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  L1 Cache (In-Process)                                   │   │
│  │  • Hot patterns (last 5 min)                             │   │
│  │  • LRU eviction                                          │   │
│  │  • 100MB capacity                                        │   │
│  │  • Latency: <0.1ms                                       │   │
│  └──────────────────────────────────────────────────────────┘   │
│                         │ Cache Miss                             │
│                         ▼                                        │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  L2 Cache (Redis)                                        │   │
│  │  • All patterns (5-min TTL)                              │   │
│  │  • Distributed across cluster                            │   │
│  │  • Latency: 1-5ms                                        │   │
│  └──────────────────────────────────────────────────────────┘   │
│                         │ Cache Miss                             │
│                         ▼                                        │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Pattern Detection Engine                                │   │
│  │  • TA-Lib (61 patterns)                                  │   │
│  │  • Async execution (asyncio.to_thread)                   │   │
│  │  • Latency: 2-5ms                                        │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Data Layer                                     │
│  • CandleService (OHLCV data)                                    │
│  • TimescaleDB (historical data)                                 │
│  • DB-first fetching strategy                                    │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Component Responsibilities

**API Layer** (`pattern_analysis.py`):
- Request validation (Pydantic schemas)
- Authentication (JWT)
- Rate limiting (100/minute)
- Error handling and logging
- Response formatting

**Service Layer** (`pattern_detection_service.py`):
- Cache management (L1 + L2)
- Pattern detection orchestration
- OHLCV data fetching
- Async execution management
- Performance monitoring

**Detection Engine** (TA-Lib wrapper):
- 61 candlestick pattern detection
- Thread pool execution
- Result formatting
- Confidence scoring

---

## 2. Caching Strategy (2026 Best Practices)

### 2.1 Three-Tier Cache Hierarchy

Based on 2026 ML caching research, we implement a three-tier hierarchy:

**Tier 1: In-Process Cache (L1)**
```python
from cachetools import LRUCache
import threading

class L1Cache:
    """
    In-process LRU cache for hot patterns.
    
    Characteristics:
    - Latency: <0.1ms (nanosecond access)
    - Capacity: 100MB (~1000 pattern results)
    - Eviction: LRU (Least Recently Used)
    - Thread-safe: Yes (with lock)
    """
    def __init__(self, maxsize: int = 1000):
        self._cache = LRUCache(maxsize=maxsize)
        self._lock = threading.Lock()
    
    def get(self, key: str) -> dict | None:
        with self._lock:
            return self._cache.get(key)
    
    def set(self, key: str, value: dict):
        with self._lock:
            self._cache[key] = value
```

**Tier 2: Distributed Cache (L2 - Redis)**
```python
async def get_from_redis(
    redis: Redis,
    instrument_key: str,
    timeframe: str,
) -> dict | None:
    """
    Get cached patterns from Redis.
    
    Characteristics:
    - Latency: 1-5ms (network RTT)
    - Capacity: Terabytes (distributed)
    - TTL: 5 minutes (300 seconds)
    - Eviction: TTL-based automatic expiry
    """
    cache_key = f"pattern:{instrument_key}:{timeframe}"
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)
    return None
```

**Tier 3: Compute (TA-Lib)**
```python
async def detect_patterns(
    open_prices: np.ndarray,
    high_prices: np.ndarray,
    low_prices: np.ndarray,
    close_prices: np.ndarray,
) -> list[dict]:
    """
    Detect patterns using TA-Lib.
    
    Characteristics:
    - Latency: 2-5ms (CPU-bound)
    - Throughput: 14.3M candles/sec
    - Execution: Thread pool (non-blocking)
    """
    return await asyncio.to_thread(
        _detect_patterns_sync,
        open_prices, high_prices, low_prices, close_prices
    )
```

### 2.2 Cache Key Design

**Hierarchical Naming Convention**:
```python
# Pattern detection results
f"pattern:{instrument_key}:{timeframe}"
# Example: "pattern:NSE_EQ|INE002A01018:1D"

# Pattern metadata (accuracy, confidence)
f"pattern:meta:{pattern_name}"
# Example: "pattern:meta:HAMMER"

# User-specific cache (if personalized)
f"pattern:{user_id}:{instrument_key}:{timeframe}"
```

**Key Insights**:
- ✅ Colon-separated hierarchy for scanning
- ✅ Predictable structure for debugging
- ✅ Supports pattern-based invalidation

### 2.3 Cache Invalidation Strategy

**Time-Based (Primary)**:
```python
# 5-minute TTL for pattern results
TTL_PATTERN_RESULTS = 300  # seconds

# Rationale:
# - Patterns change slowly (daily candles)
# - 5-min staleness acceptable for analysis
# - Balances freshness vs performance
```

**Event-Based (Future Enhancement)**:
```python
# Invalidate on new candle data
async def on_new_candle(instrument_key: str, timeframe: str):
    """Invalidate cache when new candle arrives."""
    cache_key = f"pattern:{instrument_key}:{timeframe}"
    await redis.delete(cache_key)
    logger.info(f"Invalidated cache: {cache_key}")
```

### 2.4 Cache Warming Strategy

**Pre-Warming on Deploy**:
```python
async def warm_cache_on_startup(
    popular_symbols: list[str],
    timeframes: list[str] = ["1D", "1H"],
):
    """
    Pre-warm cache with popular symbols before serving traffic.
    
    Strategy:
    - Top 100 most-traded NSE symbols
    - 1D and 1H timeframes
    - Parallel execution (10 concurrent)
    """
    tasks = [
        detect_patterns(symbol, tf)
        for symbol in popular_symbols
        for tf in timeframes
    ]
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info(f"Cache warmed: {len(tasks)} patterns")
```

---

## 3. Performance Optimization

### 3.1 Async Execution Pattern

**CPU-Bound Work Offloading**:
```python
async def detect_patterns_async(
    open_prices: np.ndarray,
    high_prices: np.ndarray,
    low_prices: np.ndarray,
    close_prices: np.ndarray,
) -> list[dict]:
    """
    Async wrapper for CPU-bound TA-Lib calls.
    
    Pattern: asyncio.to_thread() (Python 3.9+)
    - Offloads to thread pool
    - Non-blocking event loop
    - Automatic thread management
    """
    def _detect():
        # CPU-bound TA-Lib calls
        detected = []
        for pattern_name in PATTERNS:
            pattern_func = getattr(talib, pattern_name)
            result = pattern_func(open_prices, high_prices, low_prices, close_prices)
            # Process result
            detected.extend(...)
        return detected
    
    # Run in thread pool (non-blocking)
    return await asyncio.to_thread(_detect)
```

**Performance Characteristics**:
- Thread pool overhead: ~0.1-0.5ms
- TA-Lib computation: 0.16ms (10 patterns, 235 candles)
- Total latency: <1ms for pattern detection
- Event loop: Never blocked

### 3.2 Batch Processing (Future Enhancement)

**Dynamic Batching for Multiple Symbols**:
```python
class PatternBatcher:
    """
    Batch multiple pattern detection requests.
    
    Strategy:
    - Collect requests for max_wait_ms
    - Process batch when max_batch reached or timeout
    - Return individual results to each requester
    """
    def __init__(self, max_batch: int = 10, max_wait_ms: float = 10):
        self.max_batch = max_batch
        self.max_wait_ms = max_wait_ms
        self.queue: list[tuple[str, asyncio.Future]] = []
    
    async def detect(self, instrument_key: str) -> dict:
        future = asyncio.Future()
        self.queue.append((instrument_key, future))
        
        if len(self.queue) >= self.max_batch:
            await self._flush()
        else:
            # Wait for more requests or timeout
            await asyncio.sleep(self.max_wait_ms / 1000)
            await self._flush()
        
        return await future
    
    async def _flush(self):
        if not self.queue:
            return
        
        # Process batch in parallel
        tasks = [
            detect_patterns_single(key)
            for key, _ in self.queue
        ]
        results = await asyncio.gather(*tasks)
        
        # Return results to individual futures
        for (_, future), result in zip(self.queue, results):
            future.set_result(result)
        
        self.queue.clear()
```

**Benefits**:
- Amortizes overhead across multiple requests
- Better resource utilization
- Maintains individual request latency SLA

---

## 4. Reliability & Fault Tolerance

### 4.1 Graceful Degradation

**Fallback Hierarchy**:
```python
async def detect_patterns_with_fallback(
    instrument_key: str,
    timeframe: str,
) -> dict:
    """
    Multi-tier fallback strategy.
    
    Hierarchy:
    1. L1 Cache (in-process)
    2. L2 Cache (Redis)
    3. Fresh computation (TA-Lib)
    4. Cached stale data (if available)
    5. Empty result with error flag
    """
    try:
        # Tier 1: L1 Cache
        result = l1_cache.get(cache_key)
        if result:
            return result
        
        # Tier 2: L2 Cache
        result = await redis.get(cache_key)
        if result:
            l1_cache.set(cache_key, result)
            return result
        
        # Tier 3: Fresh computation
        result = await detect_patterns_fresh(instrument_key, timeframe)
        await cache_result(result)
        return result
    
    except RedisError as e:
        logger.warning(f"Redis unavailable: {e}")
        # Fallback: Try fresh computation without caching
        try:
            return await detect_patterns_fresh(instrument_key, timeframe)
        except Exception as e2:
            logger.error(f"Pattern detection failed: {e2}")
            # Fallback: Return stale cache if available
            stale = await get_stale_cache(cache_key)
            if stale:
                return {**stale, "stale": True}
            # Final fallback: Empty result
            return {
                "patterns": [],
                "error": "service_unavailable",
                "available": False,
            }
```

### 4.2 Circuit Breaker Pattern

**Prevent Cascade Failures**:
```python
from pybreaker import CircuitBreaker

# Circuit breaker for Redis
redis_breaker = CircuitBreaker(
    fail_max=5,           # Open after 5 failures
    timeout_duration=30,  # Stay open for 30 seconds
    name="redis_cache"
)

@redis_breaker
async def get_from_redis_safe(key: str) -> dict | None:
    """Redis access with circuit breaker."""
    return await redis.get(key)
```

**States**:
- **Closed**: Normal operation
- **Open**: All requests fail fast (no Redis calls)
- **Half-Open**: Test if Redis recovered

### 4.3 Timeout Management

**Layered Timeouts**:
```python
# API endpoint timeout
@router.post("/pattern-analysis", timeout=5.0)  # 5 seconds max

# Service method timeout
async def detect_patterns(self, ...):
    try:
        return await asyncio.wait_for(
            self._detect_internal(...),
            timeout=3.0  # 3 seconds for detection
        )
    except asyncio.TimeoutError:
        logger.error("Pattern detection timeout")
        return {"error": "timeout", "available": False}

# Database query timeout (configured in connection)
# statement_timeout: 30000ms (30 seconds)
```

---

## 5. Observability & Monitoring

### 5.1 Key Metrics

**Performance Metrics**:
```python
from prometheus_client import Histogram, Counter, Gauge

# Latency
PATTERN_DETECTION_LATENCY = Histogram(
    'pattern_detection_latency_seconds',
    'Pattern detection latency',
    ['cache_tier', 'timeframe'],
    buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0]
)

# Cache performance
CACHE_HITS = Counter(
    'pattern_cache_hits_total',
    'Cache hits',
    ['tier']  # L1, L2
)

CACHE_MISSES = Counter(
    'pattern_cache_misses_total',
    'Cache misses',
    ['tier']
)

# Throughput
PATTERN_REQUESTS = Counter(
    'pattern_requests_total',
    'Total pattern detection requests',
    ['status']  # success, error, timeout
)

# Active requests
ACTIVE_REQUESTS = Gauge(
    'pattern_active_requests',
    'Currently processing requests'
)
```

**Business Metrics**:
```python
# Pattern detection results
PATTERNS_DETECTED = Histogram(
    'patterns_detected_count',
    'Number of patterns detected per request',
    buckets=[0, 1, 5, 10, 20, 50, 100]
)

# User engagement
PATTERN_API_USERS = Counter(
    'pattern_api_unique_users',
    'Unique users calling pattern API'
)
```

### 5.2 Structured Logging

**Log Format**:
```python
logger.info(
    "Pattern detection: user=%s instrument=%s timeframe=%s "
    "patterns=%d latency=%.2fms cache=%s",
    user_id,
    instrument_key,
    timeframe,
    len(patterns),
    latency_ms,
    cache_tier,  # L1, L2, or MISS
)
```

**Log Levels**:
- **INFO**: Successful requests, cache hits
- **WARNING**: Cache misses, fallback usage, slow requests
- **ERROR**: Failures, timeouts, exceptions

### 5.3 Distributed Tracing

**OpenTelemetry Integration**:
```python
from opentelemetry import trace

tracer = trace.get_tracer(__name__)

async def detect_patterns(self, instrument_key: str, timeframe: str):
    with tracer.start_as_current_span("pattern_detection") as span:
        span.set_attribute("instrument_key", instrument_key)
        span.set_attribute("timeframe", timeframe)
        
        # L1 Cache check
        with tracer.start_as_current_span("l1_cache_check"):
            result = l1_cache.get(cache_key)
        
        if not result:
            # L2 Cache check
            with tracer.start_as_current_span("l2_cache_check"):
                result = await redis.get(cache_key)
        
        if not result:
            # Fresh computation
            with tracer.start_as_current_span("talib_detection"):
                result = await self._detect_fresh(...)
        
        span.set_attribute("patterns_detected", len(result["patterns"]))
        return result
```

---

## 6. Security

### 6.1 Authentication & Authorization

**JWT Validation**:
```python
@router.post("/pattern-analysis")
@limiter.limit("100/minute")
async def analyze_patterns(
    request: Request,
    body: PatternAnalysisRequest,
    current_user: dict = Depends(get_current_user),  # JWT validation
):
    user_id = current_user.get("user_id")
    # User authenticated, proceed
```

### 6.2 Input Validation

**Pydantic Schemas**:
```python
class PatternAnalysisRequest(BaseModel):
    """Request validation with constraints."""
    instrument_key: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Z_|0-9]+$"  # NSE format
    )
    timeframe: str = Field(
        "1D",
        pattern=r"^(1m|5m|15m|1h|4h|1D|1W)$"
    )
    lookback_days: int = Field(
        365,
        ge=30,    # Minimum 30 days
        le=3650   # Maximum 10 years
    )
```

### 6.3 Rate Limiting

**Per-User Limits**:
```python
# Standard users: 100 requests/minute
@limiter.limit("100/minute")

# Premium users: 1000 requests/minute
@limiter.limit("1000/minute", key_func=lambda: get_user_tier())

# Admin users: Unlimited
@limiter.exempt
```

---

## 7. Deployment Architecture

### 7.1 Horizontal Scaling

**Stateless Design**:
- No in-memory state shared between instances
- L1 cache is per-instance (acceptable inconsistency)
- L2 cache (Redis) is shared across all instances
- Database connection pooling per instance

**Scaling Strategy**:
```yaml
# Kubernetes HPA (Horizontal Pod Autoscaler)
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: pattern-detection-service
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: pattern-detection-service
  minReplicas: 3
  maxReplicas: 20
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
  - type: Pods
    pods:
      metric:
        name: pattern_active_requests
      target:
        type: AverageValue
        averageValue: "100"
```

### 7.2 Health Checks

**Liveness Probe**:
```python
@router.get("/health/live")
async def liveness():
    """Check if service is alive."""
    return {"status": "alive"}
```

**Readiness Probe**:
```python
@router.get("/health/ready")
async def readiness(db: AsyncSession = Depends(get_db)):
    """Check if service is ready to serve traffic."""
    try:
        # Check database
        await db.execute(text("SELECT 1"))
        
        # Check Redis
        await redis.ping()
        
        # Check TA-Lib
        _ = talib.get_functions()
        
        return {"status": "ready"}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "error": str(e)}
        )
```

---

## 8. Performance Targets

### 8.1 Latency SLA

| Percentile | Target | Measurement |
|------------|--------|-------------|
| **p50** | <10ms | L1 cache hit |
| **p95** | <50ms | L2 cache hit |
| **p99** | <100ms | Fresh computation |
| **p99.9** | <500ms | With fallback |

### 8.2 Throughput SLA

| Metric | Target | Notes |
|--------|--------|-------|
| **Requests/second** | >10,000 | Per instance |
| **Concurrent requests** | >1,000 | With async |
| **Cache hit rate** | >80% | After warmup |

### 8.3 Availability SLA

| Metric | Target | Measurement |
|--------|--------|-------------|
| **Uptime** | 99.9% | Monthly |
| **Error rate** | <0.1% | 4xx + 5xx |
| **Timeout rate** | <0.01% | Request timeouts |

---

## 9. Testing Strategy

### 9.1 Unit Tests

**Service Layer**:
```python
@pytest.mark.asyncio
async def test_pattern_detection_with_cache():
    """Test pattern detection with L1/L2 cache."""
    service = PatternDetectionService(db=mock_db, redis=mock_redis)
    
    # First call: cache miss
    result1 = await service.detect_patterns("NSE_EQ|INE002A01018", "1D")
    assert result1["total_detected"] > 0
    
    # Second call: cache hit
    result2 = await service.detect_patterns("NSE_EQ|INE002A01018", "1D")
    assert result2 == result1
    
    # Verify cache was used
    assert mock_redis.get.call_count == 1
```

### 9.2 Integration Tests

**API Endpoint**:
```python
@pytest.mark.asyncio
async def test_pattern_analysis_endpoint(async_client):
    """Test pattern analysis API endpoint."""
    response = await async_client.post(
        "/api/v1/ml/pattern-analysis",
        json={
            "instrument_key": "NSE_EQ|INE002A01018",
            "timeframe": "1D",
            "lookback_days": 365,
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "patterns" in data
    assert "total_detected" in data
    assert data["timeframe"] == "1D"
```

### 9.3 Load Tests

**Performance Validation**:
```python
# locust load test
from locust import HttpUser, task, between

class PatternAnalysisUser(HttpUser):
    wait_time = between(1, 3)
    
    @task
    def analyze_patterns(self):
        self.client.post(
            "/api/v1/ml/pattern-analysis",
            json={
                "instrument_key": "NSE_EQ|INE002A01018",
                "timeframe": "1D",
                "lookback_days": 365,
            },
            headers={"Authorization": f"Bearer {self.token}"}
        )
```

**Load Test Targets**:
- 10,000 requests/second sustained
- p95 latency <50ms
- 0% error rate

---

## 10. Summary

### Design Highlights

1. **Three-Tier Caching**: L1 (in-process) + L2 (Redis) + Compute (TA-Lib)
2. **Async Execution**: Non-blocking with `asyncio.to_thread()`
3. **Graceful Degradation**: Multi-tier fallback hierarchy
4. **Comprehensive Observability**: Metrics, logging, tracing
5. **Security First**: JWT auth, rate limiting, input validation
6. **Horizontal Scalability**: Stateless design, auto-scaling

### Performance Characteristics

- **Latency**: <10ms p50, <50ms p95, <100ms p99
- **Throughput**: >10,000 requests/second per instance
- **Cache Hit Rate**: >80% after warmup
- **Availability**: 99.9% uptime

### Next Steps

1. ✅ Design complete (Task 4)
2. ⏳ Implement service (Task 5)
3. ⏳ Create API endpoint (Task 11)
4. ⏳ Add caching layer (Task 14)
5. ⏳ Comprehensive testing (Tasks 15-17)

---

**Document Version**: 1.0  
**Last Updated**: 2026-05-11 22:35 IST  
**Next Review**: After implementation (Task 5)
