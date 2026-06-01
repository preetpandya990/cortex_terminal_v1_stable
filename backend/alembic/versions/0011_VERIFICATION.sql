-- Migration 0011 Verification Script
-- ====================================
-- Run this after applying the migration to verify everything works correctly

-- ══════════════════════════════════════════════════════════════════════════════
-- 1. VERIFY HYPERTABLES CREATED
-- ══════════════════════════════════════════════════════════════════════════════
SELECT 
    hypertable_name,
    chunk_time_interval,
    num_dimensions,
    compression_enabled
FROM timescaledb_information.hypertables 
WHERE hypertable_name IN ('trade_suggestions', 'event_correlations')
ORDER BY hypertable_name;

-- Expected: 2 rows showing both tables as hypertables with 1-day intervals

-- ══════════════════════════════════════════════════════════════════════════════
-- 2. VERIFY INDEXES
-- ══════════════════════════════════════════════════════════════════════════════
SELECT 
    tablename,
    indexname,
    indexdef
FROM pg_indexes 
WHERE tablename IN ('trade_suggestions', 'event_correlations')
  AND indexname NOT LIKE '%_hyper_%'  -- Exclude TimescaleDB internal indexes
ORDER BY tablename, indexname;

-- Expected: 7 indexes for trade_suggestions, 5 for event_correlations

-- ══════════════════════════════════════════════════════════════════════════════
-- 3. VERIFY RETENTION POLICIES
-- ══════════════════════════════════════════════════════════════════════════════
SELECT 
    hypertable_name,
    proc_name,
    config::json->>'drop_after' as retention_period,
    scheduled
FROM timescaledb_information.jobs 
WHERE proc_name = 'policy_retention'
  AND hypertable_name IN ('trade_suggestions', 'event_correlations')
ORDER BY hypertable_name;

-- Expected: 2 jobs (7 days for suggestions, 30 days for correlations)

-- ══════════════════════════════════════════════════════════════════════════════
-- 4. VERIFY CONSTRAINTS
-- ══════════════════════════════════════════════════════════════════════════════
SELECT 
    conname as constraint_name,
    contype as constraint_type,
    pg_get_constraintdef(oid) as definition
FROM pg_constraint
WHERE conrelid IN (
    'trade_suggestions'::regclass,
    'event_correlations'::regclass
)
ORDER BY conrelid::text, contype, conname;

-- Expected: CHECK constraints for consensus_score, confidence_level, signal_direction, etc.

-- ══════════════════════════════════════════════════════════════════════════════
-- 5. TEST LANDING PAGE QUERY PERFORMANCE
-- ══════════════════════════════════════════════════════════════════════════════
EXPLAIN ANALYZE
SELECT 
    suggestion_id,
    symbol,
    trading_symbol,
    consensus_score,
    confidence_level,
    signal_direction,
    entry_price,
    stop_loss,
    risk_reward_ratio,
    generated_at,
    expires_at
FROM trade_suggestions
WHERE status = 'active' 
  AND consensus_score >= 60
ORDER BY consensus_score DESC, generated_at DESC
LIMIT 50;

-- Expected: 
-- - Index Scan using idx_suggestions_landing_page
-- - Execution time: <10ms (on empty table, <5ms)

-- ══════════════════════════════════════════════════════════════════════════════
-- 6. TEST INSERT WITH VALID DATA
-- ══════════════════════════════════════════════════════════════════════════════
INSERT INTO trade_suggestions (
    symbol,
    instrument_key,
    trading_symbol,
    consensus_score,
    confidence_level,
    signal_direction,
    trigger_pathway,
    scanner_signal,
    ai_signal,
    ml_signal,
    entry_price,
    stop_loss,
    risk_reward_ratio,
    take_profit_1,
    expires_at
) VALUES (
    'RELIANCE',
    'NSE_EQ|INE002A01018',
    'RELIANCE-EQ',
    85.50,
    'HIGH',
    'BUY',
    'TECHNICAL_FIRST',
    '{"score": 8.5, "indicators": {"rsi": 65, "macd": "bullish"}}'::jsonb,
    '{"sentiment": "positive", "impact_score": 85}'::jsonb,
    '{"prediction": "BUY", "confidence": 0.87}'::jsonb,
    2450.00,
    2400.00,
    2.5,
    2550.00,
    NOW() + INTERVAL '4 hours'
);

-- Expected: 1 row inserted successfully

-- ══════════════════════════════════════════════════════════════════════════════
-- 7. TEST CONSTRAINT VALIDATION (Should FAIL)
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 1: Invalid confidence_level
DO $$
BEGIN
    INSERT INTO trade_suggestions (
        symbol, instrument_key, consensus_score, confidence_level, 
        signal_direction, trigger_pathway, scanner_signal, ai_signal, ml_signal, expires_at
    ) VALUES (
        'TEST', 'TEST_KEY', 75.0, 'INVALID', 'BUY', 'TECHNICAL_FIRST', 
        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, NOW() + INTERVAL '1 hour'
    );
    RAISE EXCEPTION 'Constraint check failed: invalid confidence_level was accepted';
EXCEPTION
    WHEN check_violation THEN
        RAISE NOTICE '✓ Constraint working: Invalid confidence_level rejected';
END $$;

-- Test 2: Invalid consensus_score (>100)
DO $$
BEGIN
    INSERT INTO trade_suggestions (
        symbol, instrument_key, consensus_score, confidence_level, 
        signal_direction, trigger_pathway, scanner_signal, ai_signal, ml_signal, expires_at
    ) VALUES (
        'TEST', 'TEST_KEY', 150.0, 'HIGH', 'BUY', 'TECHNICAL_FIRST', 
        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, NOW() + INTERVAL '1 hour'
    );
    RAISE EXCEPTION 'Constraint check failed: consensus_score > 100 was accepted';
EXCEPTION
    WHEN check_violation THEN
        RAISE NOTICE '✓ Constraint working: consensus_score > 100 rejected';
END $$;

-- Test 3: Invalid signal_direction
DO $$
BEGIN
    INSERT INTO trade_suggestions (
        symbol, instrument_key, consensus_score, confidence_level, 
        signal_direction, trigger_pathway, scanner_signal, ai_signal, ml_signal, expires_at
    ) VALUES (
        'TEST', 'TEST_KEY', 75.0, 'HIGH', 'HOLD', 'TECHNICAL_FIRST', 
        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, NOW() + INTERVAL '1 hour'
    );
    RAISE EXCEPTION 'Constraint check failed: invalid signal_direction was accepted';
EXCEPTION
    WHEN check_violation THEN
        RAISE NOTICE '✓ Constraint working: Invalid signal_direction rejected';
END $$;

-- ══════════════════════════════════════════════════════════════════════════════
-- 8. TEST EVENT CORRELATION INSERT
-- ══════════════════════════════════════════════════════════════════════════════
INSERT INTO event_correlations (
    suggestion_id,
    trigger_type,
    trigger_timestamp,
    scanner_response_ms,
    ai_response_ms,
    ml_response_ms,
    total_latency_ms,
    consensus_reached,
    scanner_output,
    ai_output,
    ml_output
) VALUES (
    (SELECT suggestion_id FROM trade_suggestions WHERE symbol = 'RELIANCE' LIMIT 1),
    'SCANNER_ANOMALY',
    NOW(),
    45,
    78,
    52,
    175,
    TRUE,
    '{"score": 8.5}'::jsonb,
    '{"sentiment": "positive"}'::jsonb,
    '{"prediction": "BUY"}'::jsonb
);

-- Expected: 1 row inserted successfully

-- ══════════════════════════════════════════════════════════════════════════════
-- 9. TEST FOREIGN KEY RELATIONSHIP
-- ══════════════════════════════════════════════════════════════════════════════
SELECT 
    ts.symbol,
    ts.consensus_score,
    ec.total_latency_ms,
    ec.consensus_reached
FROM trade_suggestions ts
JOIN event_correlations ec ON ts.suggestion_id = ec.suggestion_id
WHERE ts.symbol = 'RELIANCE';

-- Expected: 1 row showing the joined data

-- ══════════════════════════════════════════════════════════════════════════════
-- 10. TEST MONITORING QUERIES
-- ══════════════════════════════════════════════════════════════════════════════

-- Average consensus latency (last hour)
SELECT 
    AVG(total_latency_ms) as avg_latency_ms,
    MIN(total_latency_ms) as min_latency_ms,
    MAX(total_latency_ms) as max_latency_ms,
    COUNT(*) as total_correlations
FROM event_correlations
WHERE consensus_reached = TRUE
  AND trigger_timestamp >= NOW() - INTERVAL '1 hour';

-- Expected: Shows latency statistics (target: avg < 100ms)

-- Rejection rate by reason
SELECT 
    rejection_reason,
    COUNT(*) as count,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 2) as percentage
FROM event_correlations
WHERE consensus_reached = FALSE
  AND trigger_timestamp >= NOW() - INTERVAL '24 hours'
GROUP BY rejection_reason
ORDER BY count DESC;

-- Expected: Shows rejection reasons (if any)

-- ══════════════════════════════════════════════════════════════════════════════
-- 11. CLEANUP TEST DATA
-- ══════════════════════════════════════════════════════════════════════════════
DELETE FROM event_correlations WHERE trigger_type = 'SCANNER_ANOMALY';
DELETE FROM trade_suggestions WHERE symbol IN ('RELIANCE', 'TEST');

-- Expected: Test data removed

-- ══════════════════════════════════════════════════════════════════════════════
-- VERIFICATION COMPLETE
-- ══════════════════════════════════════════════════════════════════════════════
SELECT '✓ Migration 0011 verification complete!' as status;
