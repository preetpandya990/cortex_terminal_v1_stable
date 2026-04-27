# API Response Caching Guide

## Overview

Production-grade API response caching system for trade suggestions endpoints using Redis as the caching backend. Implements cache-aside pattern with automatic invalidation, TTL-based expiration, and comprehensive observability.

**Enhancement**: 2.1 - API Response Caching (Tier 2: Performance Optimization)

## Architecture

### Components

1. **Cache Decorator** (`app/core/cache_decorator.py`)
   - Production-grade decorator for FastAPI endpoints
   - Automatic cache key generation from function signature
   - TTL-based expiration with jitter support
   - Pydantic model serialization
   - Error resilience (cache failures don't break endpoints)

2. **Cache Invalidation Worker** (`app/worker.py`)
   - Background task subscribing to Redis pub/sub
   - Listens to `cai:suggestions:new` channel
   - Invalidates list caches on new suggestion events
   - Tracks invalidation metrics

3. **Metrics** (`app/core/metrics.py`)
   - `api_cache_hits_total` - Total cache hits by endpoint
   - `api_cache_misses_total` - Total cache misses by endpoint
   - `api_cache_response_time_seconds` - Response time histogram by cache status
   - `api_cache_invalidations_total` - Total cache invalidations by pattern

4. **Configuration** (`app/core/config.py`)
   - `CACHE_TTL_SUGGESTIONS_LIST` - List endpoint TTL (default: 30s)
   - `CACHE_TTL_SUGGESTIONS_DETAIL` - Detail endpoint TTL (default: 60s)
   - `ENABLE_API_RESPONSE_CACHING` - Global cache enable/disable flag

## Cached Endpoints

### 1. List Suggestions (`GET /api/v1/trade-suggestions`)

**Cache Configuration:**
- TTL: 30 seconds
- Jitter: ±5 seconds (prevents cache stampede)
- Key Format: `suggestions:list:{func_name}:{param_hash}`
- Invalidation: Pattern-based on new suggestion (`suggestions:list:*`)

**Cache Key Examples:**
```
suggestions:list:list_suggestions:a1b2c3d4  # No filters
suggestions:list:list_suggestions:b2c3d4e5  # direction=BUY
suggestions:list:list_suggestions:c3d4e5f6  # symbol=AAPL
suggestions:list:list_suggestions:d4e5f6g7  # direction=BUY&symbol=AAPL
```

**Query Parameters Affecting Cache Key:**
- `status` - Filter by suggestion status
- `direction` - Filter by signal direction (BUY/SELL)
- `confidence_level` - Filter by confidence level
- `min_confidence` - Minimum consensus score
- `symbol` - Filter by symbol
- `page` - Page number
- `page_size` - Results per page

**Excluded from Cache Key:**
- `request` - FastAPI Request object
- `response` - FastAPI Response object
- `session` - Database session
- `user_id` - User ID (authentication)

### 2. Get Suggestion Detail (`GET /api/v1/trade-suggestions/{suggestion_id}`)

**Cache Configuration:**
- TTL: 60 seconds
- Jitter: ±5 seconds
- Key Format: `suggestions:detail:{func_name}:{suggestion_id_hash}`
- Invalidation: Not automatically invalidated (detail data rarely changes)

**Cache Key Example:**
```
suggestions:detail:get_suggestion:550e8400
```

## Cache Invalidation Strategy

### Event-Driven Invalidation

When a new trade suggestion is generated:

1. **Correlation Engine** publishes to `cai:suggestions:new` channel:
   ```json
   {
     "suggestion_id": "550e8400-e29b-41d4-a716-446655440000",
     "symbol": "AAPL",
     "signal_direction": "BUY",
     "consensus_score": 85.5,
     "confidence_level": "HIGH",
     "trigger_type": "SCANNER_ANOMALY",
     "generated_at": "2026-04-23T12:00:00Z"
   }
   ```

2. **Cache Invalidation Worker** receives event and:
   - Deletes all list cache keys matching `suggestions:list:*`
   - Tracks invalidation metric
   - Logs invalidation event

3. **Next API Request** results in cache MISS and fresh data fetch

### Manual Invalidation

For manual cache invalidation (e.g., admin operations):

```python
from app.core.cache_decorator import invalidate_cache_pattern, invalidate_cache_key

# Invalidate all list caches
await invalidate_cache_pattern("suggestions:list:*")

# Invalidate specific detail cache
await invalidate_cache_key("suggestions:detail:get_suggestion:550e8400")
```

## Response Headers

All cached endpoints include `X-Cache-Status` header:

- `X-Cache-Status: HIT` - Response served from cache
- `X-Cache-Status: MISS` - Response generated from database

**Example:**
```bash
curl -H "Authorization: Bearer <token>" \
  http://localhost:8000/api/v1/trade-suggestions

HTTP/1.1 200 OK
X-Cache-Status: HIT
Content-Type: application/json
...
```

## Performance Characteristics

### Latency Improvements

**List Endpoint (30s TTL):**
- Cache MISS: ~50-150ms (database query + serialization)
- Cache HIT: ~2-5ms (Redis GET + deserialization)
- **Improvement: 10-75x faster**

**Detail Endpoint (60s TTL):**
- Cache MISS: ~30-80ms (database query + correlation fetch)
- Cache HIT: ~2-5ms (Redis GET + deserialization)
- **Improvement: 15-40x faster**

### Cache Hit Rates (Expected)

- **List Endpoint**: 70-85% hit rate (high traffic, frequent reads)
- **Detail Endpoint**: 50-70% hit rate (lower traffic, longer TTL)

### Resource Usage

- **Redis Memory**: ~1-5KB per cached response
- **Network**: Minimal (Redis on localhost)
- **CPU**: <1% overhead for serialization/deserialization

## Monitoring & Observability

### Prometheus Metrics

**Cache Hit Rate:**
```promql
# Overall hit rate
sum(rate(api_cache_hits_total[5m])) / 
(sum(rate(api_cache_hits_total[5m])) + sum(rate(api_cache_misses_total[5m])))

# Per-endpoint hit rate
sum(rate(api_cache_hits_total{endpoint="GET:/api/v1/trade-suggestions"}[5m])) / 
(sum(rate(api_cache_hits_total{endpoint="GET:/api/v1/trade-suggestions"}[5m])) + 
 sum(rate(api_cache_misses_total{endpoint="GET:/api/v1/trade-suggestions"}[5m])))
```

**Cache Response Time:**
```promql
# P95 response time by cache status
histogram_quantile(0.95, 
  sum(rate(api_cache_response_time_seconds_bucket[5m])) by (le, cache_status)
)

# Average response time improvement
avg(rate(api_cache_response_time_seconds_sum{cache_status="HIT"}[5m])) /
avg(rate(api_cache_response_time_seconds_sum{cache_status="MISS"}[5m]))
```

**Invalidation Rate:**
```promql
# Invalidations per minute
rate(api_cache_invalidations_total[1m]) * 60

# Invalidations by trigger
sum(rate(api_cache_invalidations_total[5m])) by (trigger)
```

### Grafana Dashboard Panels

**Recommended Panels:**

1. **Cache Hit Rate (Gauge)**
   - Query: Overall hit rate
   - Thresholds: Green >70%, Yellow 50-70%, Red <50%

2. **Response Time Comparison (Graph)**
   - Query: P95 response time by cache status
   - Legend: HIT vs MISS

3. **Cache Operations (Counter)**
   - Query: Hits, Misses, Invalidations
   - Stacked area chart

4. **Invalidation Events (Table)**
   - Query: Recent invalidations by pattern and trigger
   - Time series

### Logging

**Cache HIT:**
```json
{
  "level": "DEBUG",
  "message": "Cache HIT: suggestions:list:list_suggestions:a1b2c3d4 (2.45ms)",
  "cache_key": "suggestions:list:list_suggestions:a1b2c3d4",
  "cache_status": "HIT",
  "elapsed_ms": 2.45
}
```

**Cache MISS:**
```json
{
  "level": "DEBUG",
  "message": "Cache MISS: suggestions:list:list_suggestions:a1b2c3d4 (125.67ms)",
  "cache_key": "suggestions:list:list_suggestions:a1b2c3d4",
  "cache_status": "MISS",
  "elapsed_ms": 125.67
}
```

**Cache Invalidation:**
```json
{
  "level": "INFO",
  "message": "Cache invalidation: 12 keys deleted",
  "pattern": "suggestions:list:*",
  "deleted_count": 12,
  "suggestion_id": "550e8400-e29b-41d4-a716-446655440000",
  "symbol": "AAPL"
}
```

## Configuration

### Environment Variables

```bash
# Cache TTLs (seconds)
CACHE_TTL_SUGGESTIONS_LIST=30      # List endpoint cache TTL
CACHE_TTL_SUGGESTIONS_DETAIL=60    # Detail endpoint cache TTL

# Global cache toggle
ENABLE_API_RESPONSE_CACHING=true   # Enable/disable caching
```

### Runtime Configuration

**Disable Caching (Emergency):**
```bash
# Set environment variable and restart API
export ENABLE_API_RESPONSE_CACHING=false
docker-compose restart backend
```

**Adjust TTLs:**
```bash
# Increase list cache TTL to 60s
export CACHE_TTL_SUGGESTIONS_LIST=60
docker-compose restart backend
```

## Best Practices

### When to Use Caching

✅ **Good Use Cases:**
- High-traffic read endpoints
- Data that changes infrequently
- Expensive database queries
- Aggregation/statistics endpoints

❌ **Avoid Caching:**
- Write endpoints (POST, PUT, DELETE)
- User-specific data (unless keyed by user_id)
- Real-time data requirements (<1s latency)
- Endpoints with side effects

### Cache Key Design

**Hierarchical Keys:**
```
{namespace}:{entity}:{operation}:{params_hash}
suggestions:list:list_suggestions:a1b2c3d4
```

**Benefits:**
- Pattern-based invalidation (`suggestions:list:*`)
- Easy debugging and monitoring
- Namespace isolation

### TTL Selection

**Guidelines:**
- **High-frequency updates**: 10-30s TTL
- **Medium-frequency updates**: 30-120s TTL
- **Low-frequency updates**: 2-10min TTL
- **Static data**: 10-60min TTL

**Trade-offs:**
- Longer TTL = Higher hit rate, stale data risk
- Shorter TTL = Fresher data, lower hit rate

### Error Handling

**Cache Failures:**
- Decorator catches all cache exceptions
- Falls back to database query
- Logs warning but doesn't fail request
- Ensures high availability

**Redis Unavailable:**
- Endpoints continue to work
- All requests result in cache MISS
- Automatic recovery when Redis returns

## Testing

### Integration Tests

Run the comprehensive test suite:

```bash
cd backend
python -m pytest tests/integration/test_api_response_caching.py -v
```

**Test Coverage:**
- ✅ Cache hit/miss behavior
- ✅ X-Cache-Status header
- ✅ Cache invalidation on new suggestion
- ✅ Metrics tracking
- ✅ TTL expiration
- ✅ Cache key generation with filters
- ✅ Cache disabled when config=false
- ✅ Resilience on Redis failure

### Manual Testing

**Test Cache HIT/MISS:**
```bash
# First request - MISS
curl -H "Authorization: Bearer <token>" \
  http://localhost:8000/api/v1/trade-suggestions \
  -i | grep X-Cache-Status
# X-Cache-Status: MISS

# Second request - HIT
curl -H "Authorization: Bearer <token>" \
  http://localhost:8000/api/v1/trade-suggestions \
  -i | grep X-Cache-Status
# X-Cache-Status: HIT
```

**Test Cache Invalidation:**
```bash
# Populate cache
curl -H "Authorization: Bearer <token>" \
  http://localhost:8000/api/v1/trade-suggestions

# Trigger new suggestion (via correlation engine or manual)
# ... wait for invalidation event ...

# Next request should be MISS
curl -H "Authorization: Bearer <token>" \
  http://localhost:8000/api/v1/trade-suggestions \
  -i | grep X-Cache-Status
# X-Cache-Status: MISS
```

**Test Metrics:**
```bash
# Check Prometheus metrics
curl http://localhost:8000/metrics | grep api_cache

# Expected output:
# api_cache_hits_total{endpoint="GET:/api/v1/trade-suggestions",method="GET"} 42.0
# api_cache_misses_total{endpoint="GET:/api/v1/trade-suggestions",method="GET"} 8.0
# api_cache_invalidations_total{pattern="suggestions:list:*",trigger="new_suggestion"} 5.0
```

## Troubleshooting

### Issue: Low Cache Hit Rate (<50%)

**Possible Causes:**
- TTL too short
- High invalidation rate
- Query parameters vary too much

**Solutions:**
- Increase TTL (if data freshness allows)
- Review invalidation triggers
- Normalize query parameters

### Issue: Stale Data in Cache

**Possible Causes:**
- Invalidation not working
- Worker not running
- Redis pub/sub issue

**Solutions:**
- Check worker logs for cache invalidation events
- Verify Redis pub/sub subscription
- Manually invalidate: `redis-cli KEYS "suggestions:list:*" | xargs redis-cli DEL`

### Issue: High Redis Memory Usage

**Possible Causes:**
- Too many cache keys
- Large response payloads
- TTL too long

**Solutions:**
- Reduce TTL
- Implement cache size limits
- Monitor Redis memory: `redis-cli INFO memory`

### Issue: Cache Not Working

**Possible Causes:**
- `ENABLE_API_RESPONSE_CACHING=false`
- Redis unavailable
- Decorator not applied

**Solutions:**
- Check config: `echo $ENABLE_API_RESPONSE_CACHING`
- Check Redis: `redis-cli PING`
- Verify decorator in endpoint code

## Rollback Plan

If caching causes issues in production:

1. **Immediate Disable:**
   ```bash
   export ENABLE_API_RESPONSE_CACHING=false
   docker-compose restart backend
   ```

2. **Verify Endpoints Work:**
   ```bash
   curl -H "Authorization: Bearer <token>" \
     http://localhost:8000/api/v1/trade-suggestions
   ```

3. **Clear All Caches:**
   ```bash
   redis-cli KEYS "suggestions:*" | xargs redis-cli DEL
   ```

4. **Monitor Metrics:**
   - Check response times return to normal
   - Verify no cache-related errors in logs

## Future Enhancements

### Potential Improvements

1. **Adaptive TTL**
   - Adjust TTL based on data volatility
   - Shorter TTL during market hours
   - Longer TTL during off-hours

2. **Cache Warming**
   - Pre-populate cache on worker startup
   - Proactive refresh before expiration

3. **Multi-Level Caching**
   - L1: In-memory cache (process-local)
   - L2: Redis cache (shared)
   - Reduces Redis load for hot keys

4. **Cache Compression**
   - Compress large responses before caching
   - Reduces Redis memory usage
   - Trade-off: CPU vs memory

5. **Smart Invalidation**
   - Invalidate only affected cache keys
   - Example: New AAPL suggestion → invalidate only AAPL-filtered caches
   - Reduces unnecessary invalidations

## References

- **Cache Decorator**: `backend/app/core/cache_decorator.py`
- **Worker Implementation**: `backend/app/worker.py` (cache_invalidation_loop)
- **Metrics**: `backend/app/core/metrics.py`
- **Configuration**: `backend/app/core/config.py`
- **Integration Tests**: `backend/tests/integration/test_api_response_caching.py`
- **Redis Channels**: `backend/app/core/redis.py` (RedisChannels)

## Support

For issues or questions:
- Check logs: `docker-compose logs -f backend worker`
- Check metrics: `http://localhost:8000/metrics`
- Check Redis: `redis-cli MONITOR`
- Review integration tests for examples

---

**Last Updated**: 2026-04-23  
**Enhancement**: 2.1 - API Response Caching  
**Status**: ✅ Complete
