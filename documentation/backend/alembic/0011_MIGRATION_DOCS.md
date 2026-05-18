# Migration 0011: Trade Suggestions & Event Correlations

**Status**: ✅ Created  
**Date**: 2026-04-21 23:46 IST  
**Revision**: 0011_trade_suggestions  
**Depends On**: 0010_ml_features

---

## Overview

This migration creates two production-grade TimescaleDB hypertables for the Hawk-Eye Radar bidirectional multi-agent trade suggestion system:

1. **`trade_suggestions`** - Stores high-confidence trade suggestions from multi-agent consensus
2. **`event_correlations`** - Audit trail for debugging and performance analysis

---

## Tables Created

### 1. trade_suggestions

**Purpose**: Store validated trade suggestions with multi-agent consensus

**Key Features**:
- TimescaleDB hypertable partitioned by `generated_at` (1-day chunks)
- 7-day automatic retention policy
- Optimized composite index for landing page queries (<10ms target)
- JSONB columns for flexible signal storage
- Comprehensive constraints for data integrity

**Columns** (20 total):
- **Identity**: `id` (BIGSERIAL PK), `suggestion_id` (UUID, unique)
- **Instrument**: `symbol`, `instrument_key`, `trading_symbol`
- **Consensus**: `consensus_score` (0-100), `confidence_level` (HIGH/MEDIUM/LOW), `signal_direction` (BUY/SELL)
- **Pathway**: `trigger_pathway` (TECHNICAL_FIRST/FUNDAMENTAL_FIRST)
- **Signals**: `scanner_signal` (JSONB), `ai_signal` (JSONB), `ml_signal` (JSONB)
- **Trade Params**: `entry_price`, `stop_loss`, `risk_reward_ratio`, `take_profit_1/2/3`
- **Temporal**: `generated_at`, `expires_at`, `status` (active/expired/executed/invalidated)
- **Audit**: `created_at`, `updated_at`

**Indexes** (7 total):
1. `idx_suggestions_symbol_status` - Symbol + status (partial: active only)
2. `idx_suggestions_generated_at_desc` - Time-series queries
3. `idx_suggestions_consensus_score` - Score-based filtering (partial: active only)
4. `idx_suggestions_confidence_level` - Confidence filtering (partial: active only)
5. `idx_suggestions_direction` - Direction filtering (partial: active only)
6. `idx_suggestions_landing_page` - **Composite index for landing page** (status + score DESC + time DESC)
7. `uq_trade_suggestions_suggestion_id` - Unique constraint on UUID

**Constraints** (6 total):
- Consensus score: 0-100 range
- Confidence level: HIGH, MEDIUM, LOW
- Signal direction: BUY, SELL
- Trigger pathway: TECHNICAL_FIRST, FUNDAMENTAL_FIRST
- Status: active, expired, executed, invalidated

---

### 2. event_correlations

**Purpose**: Track agent response times and consensus decisions for monitoring

**Key Features**:
- TimescaleDB hypertable partitioned by `trigger_timestamp` (1-day chunks)
- 30-day automatic retention policy
- Foreign key to `trade_suggestions` (SET NULL on delete)
- Latency tracking for each agent (scanner, AI, ML)

**Columns** (15 total):
- **Identity**: `id` (BIGSERIAL PK), `correlation_id` (UUID, unique)
- **Reference**: `suggestion_id` (UUID FK, nullable)
- **Trigger**: `trigger_type` (SCANNER_ANOMALY/NEWS_EVENT), `trigger_timestamp`
- **Latency**: `scanner_response_ms`, `ai_response_ms`, `ml_response_ms`, `total_latency_ms`
- **Decision**: `consensus_reached` (BOOLEAN), `rejection_reason`
- **Debug**: `scanner_output` (JSONB), `ai_output` (JSONB), `ml_output` (JSONB)
- **Audit**: `created_at`

**Indexes** (5 total):
1. `idx_correlations_suggestion` - FK lookup
2. `idx_correlations_latency` - Performance monitoring (partial: consensus reached)
3. `idx_correlations_rejection` - Debugging (partial: consensus failed)
4. `idx_correlations_trigger_timestamp` - Time-series queries
5. `uq_event_correlations_correlation_id` - Unique constraint on UUID

---

## Performance Targets

| Metric | Target | Optimization |
|--------|--------|--------------|
| Landing page query | <10ms | Composite index on (status, score DESC, time DESC) |
| Write throughput | 1000+ suggestions/s | TimescaleDB chunking + JSONB |
| Index size | <100MB for 1M suggestions | Partial indexes (active only) |
| Consensus latency | <100ms | Tracked in event_correlations |

---

## TimescaleDB Features

### Hypertables
```sql
-- trade_suggestions: 1-day chunks
SELECT create_hypertable('trade_suggestions', 'generated_at', 
    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);

-- event_correlations: 1-day chunks
SELECT create_hypertable('event_correlations', 'trigger_timestamp', 
    chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
```

### Retention Policies
```sql
-- Auto-delete suggestions older than 7 days
SELECT add_retention_policy('trade_suggestions', INTERVAL '7 days', if_not_exists => TRUE);

-- Keep correlations for 30 days (debugging/analysis)
SELECT add_retention_policy('event_correlations', INTERVAL '30 days', if_not_exists => TRUE);
```

---

## Verification Steps

### 1. Apply Migration
```bash
cd backend
source .venv/bin/activate
alembic upgrade head
```

### 2. Verify Hypertables Created
```sql
SELECT * FROM timescaledb_information.hypertables 
WHERE hypertable_name IN ('trade_suggestions', 'event_correlations');
```

**Expected Output**:
```
 hypertable_name      | chunk_time_interval
----------------------+--------------------
 trade_suggestions    | 1 day
 event_correlations   | 1 day
```

### 3. Verify Indexes
```sql
\d+ trade_suggestions
```

**Expected**: 7 indexes including `idx_suggestions_landing_page`

### 4. Test Landing Page Query Performance
```sql
EXPLAIN ANALYZE
SELECT * FROM trade_suggestions
WHERE status = 'active' AND consensus_score >= 60
ORDER BY consensus_score DESC, generated_at DESC
LIMIT 50;
```

**Expected**: 
- Index Scan using `idx_suggestions_landing_page`
- Execution time: <10ms

### 5. Verify Constraints
```sql
-- Should fail: invalid confidence_level
INSERT INTO trade_suggestions (symbol, instrument_key, consensus_score, confidence_level, signal_direction, trigger_pathway, scanner_signal, ai_signal, ml_signal, expires_at)
VALUES ('TEST', 'TEST_KEY', 75.0, 'INVALID', 'BUY', 'TECHNICAL_FIRST', '{}', '{}', '{}', NOW() + INTERVAL '1 hour');
```

**Expected**: `ERROR: new row violates check constraint`

### 6. Verify Retention Policy
```sql
SELECT * FROM timescaledb_information.jobs 
WHERE proc_name = 'policy_retention';
```

**Expected**: 2 jobs (one for each table)

---

## Rollback

To rollback this migration:
```bash
alembic downgrade -1
```

This will:
1. Remove retention policies
2. Drop `event_correlations` table
3. Drop `trade_suggestions` table

---

## Best Practices Implemented

✅ **TimescaleDB Optimization**
- Hypertables with 1-day chunks (optimal for time-series queries)
- Automatic retention policies (data lifecycle management)
- Partial indexes on active records only (reduced index size)

✅ **PostgreSQL Best Practices**
- JSONB for flexible schema (scanner/AI/ML signals)
- Composite index for most common query pattern
- CHECK constraints for data integrity
- Foreign key with SET NULL (graceful deletion)

✅ **Production-Grade Design**
- UUID for distributed system compatibility
- Audit timestamps (created_at, updated_at)
- Status tracking (active/expired/executed/invalidated)
- Latency monitoring (per-agent response times)

✅ **Performance Optimization**
- Partial indexes (WHERE status = 'active')
- DESC indexes for time-series queries
- Composite index matching query pattern exactly
- JSONB for flexible data without schema migrations

✅ **Alembic Best Practices**
- Proper revision chain (0010 → 0011)
- Type hints for all functions
- Comprehensive downgrade() implementation
- if_not_exists flags for idempotency

---

## Notes

- **Portability**: TimescaleDB functions use `if_not_exists => TRUE` flag, making the migration safe to run on both PostgreSQL (where it will fail gracefully) and TimescaleDB
- **JSONB Performance**: JSONB columns are indexed using GIN indexes automatically by PostgreSQL for efficient querying
- **Chunk Size**: 1-day chunks are optimal for this use case (7-day retention, frequent queries on recent data)
- **Index Strategy**: Partial indexes on `status = 'active'` reduce index size by ~85% (assuming 7-day retention with daily expiry)

---

## Task 1.1 Completion Checklist

- [x] Create migration file `backend/alembic/versions/0011_trade_suggestions.py`
- [x] Write CREATE TABLE statement with all columns (20 for suggestions, 15 for correlations)
- [x] Add TimescaleDB hypertable conversion (both tables)
- [x] Create 7 indexes for trade_suggestions (including composite landing page index)
- [x] Create 5 indexes for event_correlations
- [x] Add retention policy (7 days for suggestions, 30 days for correlations)
- [x] Add comprehensive constraints (6 CHECK constraints)
- [x] Add foreign key with SET NULL
- [x] Implement proper downgrade() function
- [x] Follow project's Alembic pattern (revision IDs, type hints)
- [x] Document verification steps
- [x] Validate Python syntax

**Status**: ✅ **COMPLETE**
