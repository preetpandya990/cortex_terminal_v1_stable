# Tier 3 Context Report: Operational Excellence

**Date**: 2026-04-23  
**Status**: Context Gathering Complete  
**Scope**: Enhancement 3.3 (Database Query Optimization) + Enhancement 3.4 (Rate Limiting per User)

---

## Executive Summary

After comprehensive codebase analysis, I've identified:

1. **Rate Limiting (Enhancement 3.4)**: Current implementation is IP-based, NOT per-user. Needs modification.
2. **Database Query Optimization (Enhancement 3.3)**: Multiple optimization opportunities identified across 5 critical query patterns.

---

## Enhancement 3.4: Rate Limiting Analysis

### Current Implementation Status

**FINDING**: Rate limiting is **NOT** per-user despite user's claim. Current implementation uses IP addresses.

#### Evidence from `backend/app/core/limiter.py`:
```python
limiter = Limiter(
    key_func=get_remote_address,  # ❌ IP-based, NOT user-based
    storage_uri=str(settings.REDIS_URL),
    default_limits=["100000/hour"],
)
```

#### Current Rate Limits (from `backend/app/core/config.py`):
- Scanner: `10/minute`
- Search: `60/minute`
- Market Data: `120/minute`
- ML Prediction: `100/minute`

#### Router Authentication:
All routers have `Depends(get_current_user_id)` but the limiter **ignores** it and uses IP address instead.

### Required Changes

1. **Modify `key_func`** to extract `user_id` from request context
2. **Add role-based limits** (admin, premium, free tiers)
3. **Add rate limit headers** (X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset)
4. **Add Prometheus metrics** for rate limiting
5. **Test with multiple users** from same IP

### Implementation Complexity: **15 minutes** ✅

---

## Enhancement 3.3: Database Query Optimization

### Overview

Analyzed 5 critical query patterns across:
- Trade Suggestions API (`backend/app/api/v1/trade_suggestions.py`)
- Market Scanner (`backend/app/services/market_scanner.py`)
- ML Predictions (`backend/app/api/v1/ml_predictions.py`)
- Feature Loader (`backend/app/ml/inference/feature_loader.py`)
- Background Worker (`backend/app/worker.py`)

---

## Critical Query Patterns Identified

### 1. Trade Suggestions - `get_stats()` Query (HIGH PRIORITY)

**Location**: `backend/app/api/v1/trade_suggestions.py:get_stats()`

**Current Implementation**: **8 separate queries** for statistics
```python
# 1. Active suggestions count
active_stmt = select(func.count()).where(TradeSuggestion.status == "active")

# 2. Today's suggestions count
today_stmt = select(func.count()).where(TradeSuggestion.created_at >= today_start)

# 3. Average consensus score
avg_score_stmt = select(func.avg(TradeSuggestion.consensus_score)).where(...)

# 4. High confidence count
high_conf_stmt = select(func.count()).where(...)

# 5. Buy count
buy_stmt = select(func.count()).where(...)

# 6. Sell count
sell_stmt = select(func.count()).where(...)

# 7. Average latency
avg_latency_stmt = select(func.avg(EventCorrelation.total_latency_ms)).where(...)

# 8. Consensus rate (2 queries: total + successful)
total_corr_stmt = select(func.count()).where(...)
success_corr_stmt = select(func.count()).where(...)
```

**Problem**: 
- 8-9 round trips to database
- Each query scans the same table
- No query plan reuse
- High latency (estimated 50-100ms total)

**Optimization Strategy**:
```sql
-- Single query using FILTER clause (PostgreSQL 9.4+)
SELECT 
    COUNT(*) FILTER (WHERE status = 'active') AS total_active,
    COUNT(*) FILTER (WHERE created_at >= :today_start) AS total_today,
    AVG(consensus_score) FILTER (WHERE status = 'active') AS avg_consensus_score,
    COUNT(*) FILTER (WHERE status = 'active' AND confidence_level = 'HIGH') AS high_confidence_count,
    COUNT(*) FILTER (WHERE status = 'active' AND signal_direction = 'BUY') AS buy_count,
    COUNT(*) FILTER (WHERE status = 'active' AND signal_direction = 'SELL') AS sell_count
FROM trade_suggestions;

-- Separate query for correlations (different table)
SELECT 
    AVG(total_latency_ms) AS avg_latency_ms,
    COUNT(*) AS total_correlations,
    COUNT(*) FILTER (WHERE consensus_reached = true) AS successful_correlations
FROM event_correlations
WHERE created_at >= :today_start;
```

**Expected Improvement**: 
- 8-9 queries → 2 queries
- Latency: 50-100ms → 10-20ms (5x faster)
- Single table scan instead of 8

---

### 2. Trade Suggestions - Missing Indexes (HIGH PRIORITY)

**Current Indexes** (from `backend/app/models/trade_suggestions.py`):
```python
# Only 1 index found:
# idx_trade_suggestions_pagination (consensus_score DESC, created_at DESC, id DESC) 
# WHERE status = 'active'
```

**Frequently Filtered Columns** (from query analysis):
- `status` - Used in almost EVERY query
- `direction` - Filter in list_suggestions()
- `confidence_level` - Filter in list_suggestions()
- `symbol` - Filter in list_suggestions()
- `created_at` - Time-based queries
- `expires_at` - Expiry worker queries

**Missing Indexes**:
```sql
-- 1. Status index (most critical)
CREATE INDEX idx_trade_suggestions_status ON trade_suggestions(status);

-- 2. Composite index for common filters
CREATE INDEX idx_trade_suggestions_filters 
ON trade_suggestions(status, signal_direction, confidence_level);

-- 3. Symbol lookup
CREATE INDEX idx_trade_suggestions_symbol ON trade_suggestions(symbol);

-- 4. Expiry worker optimization
CREATE INDEX idx_trade_suggestions_expiry 
ON trade_suggestions(status, expires_at) 
WHERE status = 'active';

-- 5. Time-based queries
CREATE INDEX idx_trade_suggestions_created_at ON trade_suggestions(created_at);
```

**Event Correlations Missing Indexes**:
```sql
-- 1. Foreign key index (critical for joins)
CREATE INDEX idx_event_correlations_suggestion_id 
ON event_correlations(suggestion_id);

-- 2. Time-based queries
CREATE INDEX idx_event_correlations_created_at 
ON event_correlations(created_at);

-- 3. Composite for stats queries
CREATE INDEX idx_event_correlations_stats 
ON event_correlations(created_at, consensus_reached);
```

**Expected Improvement**:
- Query planning: 100ms → 5ms
- List queries: 50ms → 10ms
- Stats queries: 100ms → 20ms

---

### 3. Market Scanner - OHLCV Query (MEDIUM PRIORITY)

**Location**: `backend/app/services/market_scanner.py:_run_scan()`

**Current Implementation**:
```python
# Fetches 60 days of data for ALL instruments
candles_query = text("""
    SELECT o.instrument_key, o.timestamp, o.open, o.high, o.low, o.close, o.volume,
           im.trading_symbol, im.name
    FROM upstox_ohlcv o
    LEFT JOIN instrument_master im USING (instrument_key)
    WHERE o.timeframe = :timeframe
      AND o.timestamp >= :since
""")
```

**Problem**:
- Fetches 60 days of data (CANDLE_LOOKBACK_DAYS = 60)
- For 500+ instruments = 30,000+ rows
- In-memory grouping and processing
- No LIMIT on instruments

**Current Indexes** (from grep search):
```python
Index("idx_upstox_instrument_timeframe_ts", "instrument_key", "timeframe", "timestamp")
```

**Optimization Strategy**:
1. **Reduce lookback period**: 60 days → 40 days (sufficient for 30 candles)
2. **Add LIMIT**: Process top N instruments by volume/liquidity
3. **Use window functions**: Get last N candles per instrument in SQL
4. **Materialized view**: Pre-compute scanner results every 5 minutes

**Optimized Query**:
```sql
-- Use window function to get last 30 candles per instrument
WITH ranked_candles AS (
    SELECT 
        o.*,
        im.trading_symbol,
        im.name,
        ROW_NUMBER() OVER (
            PARTITION BY o.instrument_key 
            ORDER BY o.timestamp DESC
        ) AS rn
    FROM upstox_ohlcv o
    LEFT JOIN instrument_master im USING (instrument_key)
    WHERE o.timeframe = :timeframe
      AND o.timestamp >= :since
)
SELECT * FROM ranked_candles
WHERE rn <= 30  -- Only last 30 candles per instrument
ORDER BY instrument_key, timestamp;
```

**Expected Improvement**:
- Data fetched: 30,000 rows → 15,000 rows (50% reduction)
- Query time: 50ms → 20ms
- Memory usage: 50% reduction

---

### 4. Feature Loader - Database Queries (MEDIUM PRIORITY)

**Location**: `backend/app/ml/inference/feature_loader.py:_load_from_database()`

**Current Implementation**:
```python
# 1. Load features from feature_store
features_df = await load_features_from_db(
    symbol=symbol,
    start_date=start_date,
    end_date=end_date,
    db=self.db,
)

# 2. Separate query for latest OHLCV
stmt = select(UpstoxOHLCV).where(
    UpstoxOHLCV.instrument_key == symbol,
    UpstoxOHLCV.timeframe == db_timeframe
).order_by(desc(UpstoxOHLCV.timestamp)).limit(1)
```

**Problem**:
- 2 separate queries (features + OHLCV)
- Could be combined with JOIN or CTE
- No caching of feature computation results

**Optimization Strategy**:
1. **Combine queries**: Use JOIN to fetch features + latest OHLCV in one query
2. **Add Redis caching**: Cache computed features for 5 minutes
3. **Batch loading**: Load features for multiple symbols in parallel

**Expected Improvement**:
- Queries: 2 → 1
- Latency: 50ms → 20ms
- Cache hit rate: 0% → 80% (with Redis caching)

---

### 5. Worker Queries - Already Optimized ✅

**Location**: `backend/app/worker.py`

**Expiry Loop** (ALREADY OPTIMIZED):
```python
stmt = (
    update(TradeSuggestion)
    .where(
        TradeSuggestion.status == "active",
        TradeSuggestion.expires_at <= now,
    )
    .values(status="expired", updated_at=now)
    .returning(...)
    .limit(100)  # ✅ Batch processing
)
```

**Correlation Loop** (ALREADY OPTIMIZED):
```python
# Uses scanner service with caching
# Filters high-conviction anomalies in-memory
# Batch processing with error handling
```

**Status**: ✅ No optimization needed

---

## Index Strategy Summary

### Existing Indexes (from grep search)

**Trade Suggestions**:
- `idx_trade_suggestions_pagination` (consensus_score DESC, created_at DESC, id DESC) WHERE status = 'active'

**Upstox OHLCV**:
- `idx_upstox_instrument_timeframe_ts` (instrument_key, timeframe, timestamp)
- `idx_ohlcv_symbol_timeframe_ts` (symbol, timeframe, timestamp)
- `idx_ohlcv_exchange_ts` (exchange, timestamp)
- `stock_ohlcv_timestamp_idx` (timestamp)

**Instrument Master**:
- `idx_instrument_symbol` (trading_symbol)
- `idx_instrument_exchange` (exchange)

**AI Models** (various tables):
- Multiple indexes on event processing tables
- Regime detection indexes
- Model registry indexes

### Recommended New Indexes

**Priority 1 (Critical)**:
1. `idx_trade_suggestions_status` - Used in every query
2. `idx_event_correlations_suggestion_id` - FK join performance
3. `idx_trade_suggestions_expiry` - Worker performance

**Priority 2 (High Impact)**:
4. `idx_trade_suggestions_filters` - List query performance
5. `idx_event_correlations_stats` - Stats query performance
6. `idx_trade_suggestions_symbol` - Symbol filtering

**Priority 3 (Nice to Have)**:
7. `idx_trade_suggestions_created_at` - Time-based queries

---

## Performance Metrics Baseline

### Current Performance (Estimated)

| Query | Current | Target | Improvement |
|-------|---------|--------|-------------|
| `get_stats()` | 50-100ms | 10-20ms | 5x faster |
| `list_suggestions()` | 20-50ms | 5-10ms | 4x faster |
| `get_suggestion()` | 10-20ms | 5-10ms | 2x faster |
| Scanner `_run_scan()` | 50-100ms | 20-30ms | 3x faster |
| Feature loading | 50-100ms | 20-30ms | 3x faster |

### Expected Overall Impact

- **API Response Time**: 30-40% reduction
- **Database Load**: 50% reduction (fewer queries)
- **Cache Hit Rate**: 80%+ (with Redis caching)
- **Concurrent Users**: 2x capacity increase

---

## Implementation Plan

### Phase 1: Rate Limiting (15 minutes)

1. Modify `backend/app/core/limiter.py`:
   - Change `key_func` to use `user_id` from request context
   - Add role-based limits (admin, premium, free)
   - Add rate limit headers

2. Add Prometheus metrics:
   - `api_rate_limit_hits_total`
   - `api_rate_limit_exceeded_total`

3. Test with multiple users from same IP

### Phase 2: Critical Indexes (10 minutes)

1. Create migration file for new indexes
2. Add indexes in priority order:
   - `idx_trade_suggestions_status`
   - `idx_event_correlations_suggestion_id`
   - `idx_trade_suggestions_expiry`

3. Run `EXPLAIN ANALYZE` on critical queries

### Phase 3: Query Optimization (15 minutes)

1. Optimize `get_stats()`:
   - Combine 8 queries into 2 queries
   - Use FILTER clause for conditional aggregation

2. Optimize scanner query:
   - Add window function for last N candles
   - Reduce lookback period

3. Add Redis caching to feature loader

### Phase 4: Testing & Validation (10 minutes)

1. Run performance tests:
   - Measure query latency before/after
   - Check index usage with `EXPLAIN ANALYZE`
   - Verify cache hit rates

2. Load testing:
   - 100 concurrent users
   - Monitor database CPU/memory
   - Check rate limiting behavior

---

## Risk Assessment

### Low Risk ✅
- Adding indexes (non-breaking, reversible)
- Query optimization (backward compatible)
- Rate limiting modification (transparent to users)

### Medium Risk ⚠️
- Scanner query changes (test thoroughly)
- Feature loader caching (cache invalidation strategy)

### Mitigation Strategy
- Deploy to staging first
- Monitor Prometheus metrics
- Rollback plan: Drop indexes if performance degrades
- Feature flags for new query patterns

---

## Files to Modify

### Rate Limiting
- `backend/app/core/limiter.py` - Modify key_func
- `backend/app/core/metrics.py` - Add rate limit metrics

### Database Optimization
- `backend/alembic/versions/XXX_add_query_indexes.py` - New migration
- `backend/app/api/v1/trade_suggestions.py` - Optimize get_stats()
- `backend/app/services/market_scanner.py` - Optimize scanner query
- `backend/app/ml/inference/feature_loader.py` - Add caching

### Testing
- `backend/tests/integration/test_rate_limiting.py` - New tests
- `backend/tests/performance/test_query_optimization.py` - New tests

---

## Success Criteria

### Rate Limiting
- ✅ Rate limiting uses `user_id` instead of IP
- ✅ Multiple users from same IP have independent limits
- ✅ Rate limit headers present in responses
- ✅ Prometheus metrics tracking rate limit hits

### Database Optimization
- ✅ `get_stats()` reduced from 8 queries to 2 queries
- ✅ All critical queries use indexes (verified with EXPLAIN)
- ✅ API response time reduced by 30-40%
- ✅ Database CPU usage reduced by 20-30%
- ✅ Cache hit rate >80% for feature loading

---

## Next Steps

**USER FEEDBACK**: No production data yet - postpone query optimization until data is available.

**REVISED SCOPE FOR TIER 3**:

### ✅ Can Do Now (Without Data)
1. **Rate Limiting per User** (Enhancement 3.4)
   - Modify `key_func` to use `user_id` instead of IP
   - Add role-based limits
   - Add rate limit headers
   - Add Prometheus metrics
   - **Time**: 15 minutes

2. **Create Index Migration (Preparation)**
   - Create migration file with all recommended indexes
   - Indexes will be applied when migration runs
   - No performance impact until data exists
   - **Time**: 10 minutes

3. **Document Query Optimization Strategy**
   - Document the 8-query → 2-query optimization for `get_stats()`
   - Document scanner optimization approach
   - Create TODO comments in code for future optimization
   - **Time**: 5 minutes

### ⏸️ Postpone Until Data Available
1. **Query Performance Testing**
   - Run EXPLAIN ANALYZE on queries
   - Benchmark before/after optimization
   - Validate index usage

2. **Query Optimization Implementation**
   - Optimize `get_stats()` (8 queries → 2)
   - Optimize scanner query (window functions)
   - Add feature loader caching

3. **Load Testing**
   - 100 concurrent users
   - Monitor database metrics
   - Validate rate limiting under load

**REVISED ESTIMATED TIME**: 30 minutes (15 + 10 + 5)

**Questions for User**:
1. Proceed with rate limiting per-user implementation?
2. Create index migration now (will apply when data exists)?
3. Document optimization strategy for future implementation?

---

## References

- Current implementation: `backend/app/core/limiter.py`
- Query patterns: `backend/app/api/v1/trade_suggestions.py`
- Scanner service: `backend/app/services/market_scanner.py`
- Feature loader: `backend/app/ml/inference/feature_loader.py`
- Worker queries: `backend/app/worker.py`
- Pagination (reference): `backend/app/core/pagination.py`

---

**Report Generated**: 2026-04-23  
**Status**: Ready for Implementation  
**Confidence**: High (based on comprehensive codebase analysis)
