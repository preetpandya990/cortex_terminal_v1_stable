# TimescaleDB Migration Plan - Production-Grade Implementation

**Status**: 📋 PLANNED (Not Yet Implemented)  
**Priority**: P2 - Performance Optimization  
**Risk Level**: HIGH (4M+ production records)  
**Estimated Downtime**: 0 minutes (zero-downtime migration)

---

## Executive Summary

This document outlines a production-grade migration plan to enable TimescaleDB extension and convert time-series tables to hypertables. The migration will improve query performance for time-series data and enable native compression for cost optimization.

**Current State**: PostgreSQL 16 with 4M+ OHLCV records  
**Target State**: TimescaleDB-enabled with hypertables and compression  
**Business Value**: 10-100x faster time-series queries, 90% storage reduction on compressed data

---

## 1. Pre-Migration Assessment

### Current Database State
```
Database: PostgreSQL 16 (postgres:16-alpine image)
Tables: 25 tables, 4,014,901 OHLCV records
Size: 1.5 GB total (1256 MB OHLCV, 259 MB features)
Performance: Acceptable for current scale
Issues: None identified
```

### Why Migrate to TimescaleDB?

**Benefits**:
- **10-100x faster** time-series queries (range scans, aggregations)
- **90% storage reduction** with native compression
- **Automatic partitioning** by time (chunks)
- **Continuous aggregates** for real-time analytics
- **Data retention policies** for automatic cleanup
- **Better scalability** for billions of rows

**When to Migrate**:
- ✅ Data volume > 10M rows (we're at 4M, approaching threshold)
- ✅ Frequent time-range queries (we do this constantly)
- ✅ Storage costs becoming significant
- ✅ Query performance degrading over time

---

## 2. Risk Analysis

### High-Risk Factors
1. **Data Loss Risk**: 4M+ production records at stake
2. **Downtime Risk**: Database unavailable during migration
3. **Rollback Complexity**: Reverting hypertables is non-trivial
4. **Application Compatibility**: Schema changes may break queries

### Mitigation Strategies
1. ✅ **Full backup** before any changes
2. ✅ **Staging environment** testing first
3. ✅ **Zero-downtime** migration approach
4. ✅ **Automated rollback** procedures
5. ✅ **Monitoring** during and after migration

---

## 3. Migration Strategy: Zero-Downtime Approach

### Option A: Blue-Green Deployment (Recommended)

**Steps**:
1. Create new TimescaleDB instance (green)
2. Replicate data from old instance (blue)
3. Enable continuous replication
4. Switch application to green instance
5. Verify, then decommission blue

**Pros**: Zero downtime, easy rollback  
**Cons**: Requires 2x storage temporarily  
**Duration**: 2-4 hours (mostly replication)

### Option B: In-Place Migration with Read Replica

**Steps**:
1. Create read replica for queries
2. Migrate primary to TimescaleDB
3. Convert tables to hypertables
4. Switch back to primary
5. Decommission replica

**Pros**: Less storage overhead  
**Cons**: More complex, brief read-only period  
**Duration**: 1-2 hours

### Option C: Maintenance Window Migration

**Steps**:
1. Schedule maintenance window (2 AM - 4 AM)
2. Stop application
3. Backup database
4. Migrate to TimescaleDB
5. Restart application

**Pros**: Simplest approach  
**Cons**: Requires downtime  
**Duration**: 30-60 minutes downtime

**Recommendation**: **Option A (Blue-Green)** for production

---

## 4. Detailed Migration Steps (Blue-Green)

### Phase 1: Preparation (Day 1)

```bash
# 1. Create full backup
docker exec cortex-db pg_dump -U cortex -Fc cortex_db > backup_$(date +%Y%m%d).dump

# 2. Verify backup integrity
pg_restore --list backup_$(date +%Y%m%d).dump | wc -l

# 3. Test restore on staging
docker exec cortex-db-staging pg_restore -U cortex -d cortex_db_test backup_$(date +%Y%m%d).dump

# 4. Document current performance baselines
psql -U cortex -d cortex_db << 'EOF'
\timing on
-- Benchmark queries
SELECT COUNT(*) FROM upstox_ohlcv WHERE timestamp > NOW() - INTERVAL '7 days';
SELECT instrument_key, AVG(close) FROM upstox_ohlcv 
WHERE timestamp > NOW() - INTERVAL '30 days' 
GROUP BY instrument_key;
EOF
```

### Phase 2: Create Green Instance (Day 2)

```yaml
# docker-compose.green.yml
services:
  db-green:
    image: timescale/timescaledb-ha:pg16
    container_name: cortex-db-green
    environment:
      POSTGRES_USER: cortex
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: cortex_db
    volumes:
      - postgres_data_green:/var/lib/postgresql/data
    ports:
      - "5434:5432"  # Different port
```

```bash
# Start green instance
docker compose -f docker-compose.green.yml up -d

# Restore backup to green
docker exec -i cortex-db-green pg_restore -U cortex -d cortex_db < backup_$(date +%Y%m%d).dump

# Enable TimescaleDB
docker exec cortex-db-green psql -U cortex -d cortex_db -c "CREATE EXTENSION timescaledb;"
```

### Phase 3: Convert to Hypertables (Day 2)

```sql
-- Connect to green instance
\c postgresql://cortex:password@localhost:5434/cortex_db

-- Convert OHLCV to hypertable (largest table, most critical)
SELECT create_hypertable(
    'upstox_ohlcv', 
    'timestamp',
    chunk_time_interval => INTERVAL '1 week',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

-- Convert other time-series tables
SELECT create_hypertable('upstox_ticks', 'timestamp', 
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE, migrate_data => TRUE);

SELECT create_hypertable('ml_predictions', 'timestamp',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE, migrate_data => TRUE);

SELECT create_hypertable('ai_trading_signals', 'created_at',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE, migrate_data => TRUE);

-- Verify hypertables
SELECT 
    hypertable_name,
    num_chunks,
    compression_enabled,
    pg_size_pretty(total_bytes) as total_size
FROM timescaledb_information.hypertables
ORDER BY total_bytes DESC;
```

### Phase 4: Enable Compression (Day 2)

```sql
-- Enable compression on OHLCV (segment by instrument for better compression)
ALTER TABLE upstox_ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_key,timeframe',
    timescaledb.compress_orderby = 'timestamp DESC'
);

-- Add compression policy (compress chunks older than 7 days)
SELECT add_compression_policy('upstox_ohlcv', INTERVAL '7 days');

-- Manually compress existing old data
SELECT compress_chunk(i, if_not_compressed => true)
FROM show_chunks('upstox_ohlcv', older_than => INTERVAL '7 days') i;

-- Verify compression ratio
SELECT 
    pg_size_pretty(before_compression_total_bytes) as uncompressed,
    pg_size_pretty(after_compression_total_bytes) as compressed,
    ROUND(100 - (after_compression_total_bytes::numeric / before_compression_total_bytes::numeric * 100), 2) as compression_ratio
FROM timescaledb_information.compressed_chunk_stats
WHERE hypertable_name = 'upstox_ohlcv';
```

### Phase 5: Performance Testing (Day 3)

```bash
# Run same benchmark queries on green instance
psql -U cortex -h localhost -p 5434 -d cortex_db << 'EOF'
\timing on
-- Same queries as baseline
SELECT COUNT(*) FROM upstox_ohlcv WHERE timestamp > NOW() - INTERVAL '7 days';
SELECT instrument_key, AVG(close) FROM upstox_ohlcv 
WHERE timestamp > NOW() - INTERVAL '30 days' 
GROUP BY instrument_key;
EOF

# Compare results
# Expected: 10-50x faster for time-range queries
```

### Phase 6: Continuous Replication (Day 3)

```bash
# Set up logical replication from blue to green
# This keeps green in sync with blue until cutover

# On blue (source)
docker exec cortex-db psql -U cortex -d cortex_db << 'EOF'
-- Create publication
CREATE PUBLICATION cortex_pub FOR ALL TABLES;
EOF

# On green (target)
docker exec cortex-db-green psql -U cortex -d cortex_db << 'EOF'
-- Create subscription
CREATE SUBSCRIPTION cortex_sub
CONNECTION 'host=cortex-db port=5432 dbname=cortex_db user=cortex password=...'
PUBLICATION cortex_pub;
EOF

# Monitor replication lag
docker exec cortex-db-green psql -U cortex -d cortex_db -c "
SELECT 
    subscription_name,
    received_lsn,
    latest_end_lsn,
    latest_end_time
FROM pg_stat_subscription;
"
```

### Phase 7: Application Cutover (Day 4 - Low Traffic Period)

```bash
# 1. Update application DATABASE_URL to point to green instance
# backend/.env
DATABASE_URL=postgresql+asyncpg://cortex:password@localhost:5434/cortex_db

# 2. Restart API (rolling restart, no downtime)
docker compose restart api

# 3. Monitor for errors
docker logs -f cortex-api | grep ERROR

# 4. Verify queries working
curl http://localhost:8000/health
curl http://localhost:8000/api/v1/market-data/ohlcv?symbol=NSE_EQ|INE669E01016&limit=100

# 5. Monitor performance metrics
curl http://localhost:8000/metrics | grep http_request_duration
```

### Phase 8: Verification & Cleanup (Day 4-5)

```bash
# Monitor for 24 hours
# - Check error rates (should be 0%)
# - Check query performance (should be faster)
# - Check data consistency (row counts match)

# After 24 hours of stable operation:
# 1. Stop replication
docker exec cortex-db-green psql -U cortex -d cortex_db -c "DROP SUBSCRIPTION cortex_sub;"

# 2. Backup green instance
docker exec cortex-db-green pg_dump -U cortex -Fc cortex_db > backup_green_$(date +%Y%m%d).dump

# 3. Decommission blue instance
docker stop cortex-db
docker rm cortex-db

# 4. Rename green to primary
docker rename cortex-db-green cortex-db
# Update docker-compose.yml to use port 5433

# 5. Clean up old volume (after final verification)
docker volume rm cortex_merge_ai-ml_postgres_data
```

---

## 5. Rollback Procedures

### If Issues Detected During Testing (Phase 5)

```bash
# Simply don't cutover - keep using blue instance
# Green instance can be destroyed
docker stop cortex-db-green
docker rm cortex-db-green
docker volume rm postgres_data_green
```

### If Issues Detected After Cutover (Phase 7)

```bash
# 1. Immediately switch back to blue instance
# Update backend/.env
DATABASE_URL=postgresql+asyncpg://cortex:password@localhost:5433/cortex_db

# 2. Restart API
docker compose restart api

# 3. Verify blue instance is still running
docker ps | grep cortex-db

# 4. Investigate issues on green instance
# 5. Fix and retry migration later
```

### If Data Loss Detected

```bash
# Restore from backup
docker exec -i cortex-db pg_restore -U cortex -d cortex_db < backup_$(date +%Y%m%d).dump
```

---

## 6. Monitoring & Validation

### Key Metrics to Monitor

```sql
-- Query performance (should improve)
SELECT 
    query,
    calls,
    mean_exec_time,
    max_exec_time
FROM pg_stat_statements
WHERE query LIKE '%upstox_ohlcv%'
ORDER BY mean_exec_time DESC
LIMIT 10;

-- Compression effectiveness
SELECT 
    hypertable_name,
    pg_size_pretty(before_compression_total_bytes) as before,
    pg_size_pretty(after_compression_total_bytes) as after,
    ROUND(100 - (after_compression_total_bytes::numeric / before_compression_total_bytes::numeric * 100), 2) as savings_pct
FROM timescaledb_information.compressed_chunk_stats;

-- Chunk health
SELECT 
    hypertable_name,
    chunk_name,
    range_start,
    range_end,
    is_compressed,
    pg_size_pretty(total_bytes) as size
FROM timescaledb_information.chunks
ORDER BY range_start DESC
LIMIT 20;

-- Replication lag (during migration)
SELECT 
    EXTRACT(EPOCH FROM (now() - latest_end_time)) as lag_seconds
FROM pg_stat_subscription;
```

### Success Criteria

- ✅ 0% error rate in application
- ✅ Query performance improved or same
- ✅ All row counts match pre-migration
- ✅ Compression ratio > 50% on old data
- ✅ No replication lag > 5 seconds
- ✅ API response times < baseline

---

## 7. Post-Migration Optimizations

### Continuous Aggregates (Real-time Analytics)

```sql
-- Create materialized view for daily OHLCV aggregates
CREATE MATERIALIZED VIEW ohlcv_daily
WITH (timescaledb.continuous) AS
SELECT 
    time_bucket('1 day', timestamp) AS day,
    instrument_key,
    timeframe,
    FIRST(open, timestamp) as open,
    MAX(high) as high,
    MIN(low) as low,
    LAST(close, timestamp) as close,
    SUM(volume) as volume
FROM upstox_ohlcv
GROUP BY day, instrument_key, timeframe;

-- Add refresh policy (update every hour)
SELECT add_continuous_aggregate_policy('ohlcv_daily',
    start_offset => INTERVAL '3 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour');
```

### Data Retention Policies

```sql
-- Automatically drop chunks older than 2 years
SELECT add_retention_policy('upstox_ohlcv', INTERVAL '2 years');

-- Drop tick data older than 30 days (high volume)
SELECT add_retention_policy('upstox_ticks', INTERVAL '30 days');
```

### Query Optimization

```sql
-- Add indexes on commonly queried columns
CREATE INDEX idx_ohlcv_instrument_time ON upstox_ohlcv (instrument_key, timestamp DESC);

-- Use time_bucket for aggregations (10x faster)
SELECT 
    time_bucket('1 hour', timestamp) AS hour,
    instrument_key,
    AVG(close) as avg_close
FROM upstox_ohlcv
WHERE timestamp > NOW() - INTERVAL '7 days'
GROUP BY hour, instrument_key
ORDER BY hour DESC;
```

---

## 8. Cost-Benefit Analysis

### Storage Savings (Estimated)

```
Current: 1256 MB OHLCV data
After compression (90%): ~125 MB
Savings: 1131 MB (~$0.10/GB/month = $11/month)

At 100M rows: 31 GB → 3.1 GB
Savings: 28 GB (~$280/month)
```

### Performance Improvements (Estimated)

```
Time-range queries: 10-100x faster
Aggregations: 5-50x faster
Inserts: Same or slightly slower (chunking overhead)
Storage scans: 90% less I/O
```

### Development Time

```
Planning: 1 day
Testing on staging: 2 days
Migration execution: 1 day
Monitoring: 2 days
Total: 6 days (1 engineer)
```

### ROI

```
Cost: 6 engineer-days (~$3,000)
Benefit: 
  - $280/month storage savings (at scale)
  - 10x faster queries (better UX)
  - Better scalability (future-proof)
  
Payback: 11 months (storage only)
Real value: Query performance + scalability
```

---

## 9. Decision Matrix

### Migrate Now If:
- ✅ Data volume > 10M rows
- ✅ Query performance degrading
- ✅ Storage costs significant
- ✅ Have staging environment
- ✅ Can afford 6 days engineering time

### Defer Migration If:
- ✅ Current performance acceptable (our case)
- ✅ Data volume < 10M rows (we're at 4M)
- ✅ No performance complaints
- ✅ Other priorities more urgent
- ✅ Want to avoid risk

**Current Recommendation**: **DEFER** until data volume reaches 10M rows or performance issues arise.

---

## 10. Implementation Checklist

### Pre-Migration
- [ ] Full database backup verified
- [ ] Staging environment tested
- [ ] Performance baselines documented
- [ ] Rollback procedures tested
- [ ] Team trained on TimescaleDB
- [ ] Monitoring dashboards ready

### Migration Day
- [ ] Low-traffic period scheduled
- [ ] On-call engineer available
- [ ] Green instance created
- [ ] Data replicated and verified
- [ ] Hypertables converted
- [ ] Compression enabled
- [ ] Performance tests passed
- [ ] Application cutover completed

### Post-Migration
- [ ] 24-hour monitoring completed
- [ ] Performance metrics improved
- [ ] No errors detected
- [ ] Blue instance decommissioned
- [ ] Documentation updated
- [ ] Team notified

---

## 11. References & Resources

### Official Documentation
- [TimescaleDB Best Practices](https://docs.timescale.com/timescaledb/latest/how-to-guides/hypertables/)
- [Migration Guide](https://docs.timescale.com/timescaledb/latest/how-to-guides/migrate-data/)
- [Compression Guide](https://docs.timescale.com/timescaledb/latest/how-to-guides/compression/)

### Performance Benchmarks
- [TimescaleDB vs PostgreSQL](https://www.timescale.com/blog/timescaledb-vs-postgresql-for-time-series-data/)
- [Compression Benchmarks](https://www.timescale.com/blog/building-columnar-compression-in-a-row-oriented-database/)

### Community Resources
- [TimescaleDB Slack](https://timescaledb.slack.com)
- [GitHub Issues](https://github.com/timescale/timescaledb/issues)

---

## Conclusion

This migration plan provides a **production-grade, zero-downtime approach** to enabling TimescaleDB. The blue-green deployment strategy minimizes risk while maximizing benefits.

**Current Status**: Migration **DEFERRED** until data volume or performance requirements justify the effort.

**Next Review**: When OHLCV data reaches **10M rows** or query performance degrades below acceptable levels.

**Prepared By**: Kiro AI  
**Date**: 2026-04-15  
**Version**: 1.0
