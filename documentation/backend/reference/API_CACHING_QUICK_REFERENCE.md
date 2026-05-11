# API Response Caching - Quick Reference

## TL;DR

Production-grade API response caching for trade suggestions endpoints with automatic invalidation and comprehensive observability.

## Cached Endpoints

| Endpoint | TTL | Cache Key Pattern | Invalidation |
|----------|-----|-------------------|--------------|
| `GET /api/v1/trade-suggestions` | 30s | `suggestions:list:*` | On new suggestion |
| `GET /api/v1/trade-suggestions/{id}` | 60s | `suggestions:detail:*` | Manual only |

## Response Headers

```bash
X-Cache-Status: HIT   # Served from cache
X-Cache-Status: MISS  # Generated from database
```

## Configuration

```bash
# Environment variables
CACHE_TTL_SUGGESTIONS_LIST=30       # List endpoint TTL (seconds)
CACHE_TTL_SUGGESTIONS_DETAIL=60     # Detail endpoint TTL (seconds)
ENABLE_API_RESPONSE_CACHING=true    # Global toggle
```

## Metrics

```promql
# Cache hit rate
sum(rate(api_cache_hits_total[5m])) / 
(sum(rate(api_cache_hits_total[5m])) + sum(rate(api_cache_misses_total[5m])))

# Response time by cache status
histogram_quantile(0.95, 
  sum(rate(api_cache_response_time_seconds_bucket[5m])) by (le, cache_status)
)

# Invalidations per minute
rate(api_cache_invalidations_total[1m]) * 60
```

## Manual Operations

```python
# Invalidate all list caches
from app.core.cache_decorator import invalidate_cache_pattern
await invalidate_cache_pattern("suggestions:list:*")

# Invalidate specific detail cache
from app.core.cache_decorator import invalidate_cache_key
await invalidate_cache_key("suggestions:detail:get_suggestion:550e8400")
```

```bash
# Redis CLI
redis-cli KEYS "suggestions:list:*" | xargs redis-cli DEL
redis-cli DEL "suggestions:detail:get_suggestion:550e8400"
```

## Testing

```bash
# Run integration tests
cd backend
python -m pytest tests/integration/test_api_response_caching.py -v

# Manual test - cache HIT/MISS
curl -H "Authorization: Bearer <token>" \
  http://localhost:8000/api/v1/trade-suggestions \
  -i | grep X-Cache-Status

# Check metrics
curl http://localhost:8000/metrics | grep api_cache
```

## Emergency Disable

```bash
# Disable caching immediately
export ENABLE_API_RESPONSE_CACHING=false
docker-compose restart backend

# Clear all caches
redis-cli KEYS "suggestions:*" | xargs redis-cli DEL
```

## Performance

| Metric | Cache MISS | Cache HIT | Improvement |
|--------|-----------|-----------|-------------|
| List endpoint | 50-150ms | 2-5ms | **10-75x faster** |
| Detail endpoint | 30-80ms | 2-5ms | **15-40x faster** |
| Expected hit rate | - | 70-85% | - |

## Architecture

```
┌─────────────┐
│   Client    │
└──────┬──────┘
       │ GET /api/v1/trade-suggestions
       ▼
┌─────────────────────────────────────┐
│  FastAPI Endpoint                   │
│  @cache_response(ttl=30)            │
└──────┬──────────────────────────────┘
       │
       ├─► Redis GET (cache key)
       │   ├─► HIT → Return cached response
       │   └─► MISS → Query database
       │
       └─► Store in Redis (TTL=30s)
       
┌─────────────────────────────────────┐
│  Cache Invalidation Worker          │
│  Subscribes to: cai:suggestions:new │
└──────┬──────────────────────────────┘
       │
       └─► Invalidate: suggestions:list:*
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Low hit rate (<50%) | Increase TTL, check invalidation rate |
| Stale data | Check worker logs, verify pub/sub |
| High Redis memory | Reduce TTL, implement size limits |
| Cache not working | Check `ENABLE_API_RESPONSE_CACHING`, verify Redis |

## Files

- **Decorator**: `backend/app/core/cache_decorator.py`
- **Worker**: `backend/app/worker.py` (cache_invalidation_loop)
- **Metrics**: `backend/app/core/metrics.py`
- **Config**: `backend/app/core/config.py`
- **Tests**: `backend/tests/integration/test_api_response_caching.py`
- **Full Guide**: `backend/API_CACHING_GUIDE.md`

---

**Enhancement**: 2.1 - API Response Caching  
**Status**: ✅ Complete  
**Last Updated**: 2026-04-23
