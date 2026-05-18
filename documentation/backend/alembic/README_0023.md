# Migration 0023: ML Prediction Outcomes Table

## Overview

This migration creates the `ml_prediction_outcomes` table for unified ML governance and accuracy tracking.

## Purpose

Track ALL ML predictions (pattern detection, sentiment analysis, ensemble) for:
- **ML Governance**: Measure accuracy of all predictions, not just traded ones
- **Continuous Monitoring**: Track model performance over time
- **Pattern Analysis**: Identify which patterns have highest success rates
- **Confidence Calibration**: Validate if HIGH confidence predictions perform better
- **Slippage Analysis**: Compare predicted vs actual entry prices
- **Outcome Measurement**: Measure directional accuracy and TP/SL hit rates

## Key Features

### 1. Unified Tracking
- Tracks ALL predictions (traded + non-traded)
- Links to paper trading outcomes via `position_id` (optional)
- Supports multiple prediction types: PATTERN, SENTIMENT, ENSEMBLE

### 2. Outcome Measurement
- Measures outcomes for ALL predictions (not just traded)
- Tracks price movement in measurement window (default 5 days)
- Computes directional accuracy, TP/SL hit rates
- Calculates max favorable/adverse moves

### 3. Performance Indexes
- 5 optimized indexes for fast queries
- Supports pattern-specific accuracy analysis
- Enables confidence calibration queries
- Facilitates traded vs non-traded comparison

## Schema

```sql
CREATE TABLE ml_prediction_outcomes (
    -- Primary Key & Instrument
    id UUID PRIMARY KEY,
    symbol VARCHAR(50),
    instrument_key VARCHAR(100),
    predicted_at TIMESTAMP WITH TIME ZONE,
    
    -- ML Prediction Data
    model_version VARCHAR(50),
    prediction_type VARCHAR(50),  -- 'PATTERN', 'SENTIMENT', 'ENSEMBLE'
    pattern_name VARCHAR(50),
    pattern_timeframe VARCHAR(10),
    signal_direction VARCHAR(4),  -- 'BUY', 'SELL'
    confidence_score NUMERIC(5,4),
    confidence_level VARCHAR(10),  -- 'HIGH', 'MEDIUM', 'LOW'
    
    -- Price Targets
    predicted_entry_price NUMERIC(12,4),
    predicted_stop_loss NUMERIC(12,4),
    predicted_tp1 NUMERIC(12,4),
    predicted_tp2 NUMERIC(12,4),
    predicted_tp3 NUMERIC(12,4),
    
    -- Execution Data (NULL if not traded)
    was_traded BOOLEAN DEFAULT FALSE,
    portfolio_id UUID,
    position_id UUID,
    actual_entry_price NUMERIC(12,4),
    actual_exit_price NUMERIC(12,4),
    gross_pnl NUMERIC(14,4),
    net_pnl NUMERIC(14,4),
    
    -- Outcome Measurement (for ALL predictions)
    outcome_status VARCHAR(20) DEFAULT 'PENDING',
    ml_direction_correct BOOLEAN,
    hit_predicted_tp1 BOOLEAN DEFAULT FALSE,
    hit_predicted_tp2 BOOLEAN DEFAULT FALSE,
    hit_predicted_tp3 BOOLEAN DEFAULT FALSE,
    hit_predicted_sl BOOLEAN DEFAULT FALSE,
    max_favorable_move_pct NUMERIC(8,4),
    max_adverse_move_pct NUMERIC(8,4),
    final_move_pct NUMERIC(8,4),
    measurement_window_days INTEGER DEFAULT 5,
    measured_at TIMESTAMP WITH TIME ZONE,
    
    -- Metadata
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

## Indexes

1. **idx_ml_pred_outcomes_symbol_predicted**: Fast lookup by symbol + time
2. **idx_ml_pred_outcomes_status**: Filter by outcome status
3. **idx_ml_pred_outcomes_pattern**: Pattern-specific queries
4. **idx_ml_pred_outcomes_confidence**: Confidence calibration analysis
5. **idx_ml_pred_outcomes_predicted_at**: Time-series queries

## Usage Examples

### 1. Pattern-Specific Accuracy
```sql
SELECT 
    pattern_name,
    pattern_timeframe,
    confidence_level,
    COUNT(*) as total_predictions,
    AVG(CASE WHEN ml_direction_correct THEN 1.0 ELSE 0.0 END) as accuracy,
    AVG(final_move_pct) as avg_move_pct,
    AVG(CASE WHEN hit_predicted_tp1 THEN 1.0 ELSE 0.0 END) as tp1_hit_rate
FROM ml_prediction_outcomes
WHERE outcome_status IN ('SUCCESS', 'FAILURE')
  AND pattern_name IS NOT NULL
GROUP BY pattern_name, pattern_timeframe, confidence_level
ORDER BY accuracy DESC;
```

### 2. Confidence Calibration
```sql
SELECT 
    confidence_level,
    COUNT(*) as predictions,
    AVG(CASE WHEN ml_direction_correct THEN 1.0 ELSE 0.0 END) as accuracy,
    AVG(max_favorable_move_pct) as avg_best_move,
    AVG(max_adverse_move_pct) as avg_worst_move
FROM ml_prediction_outcomes
WHERE outcome_status IN ('SUCCESS', 'FAILURE')
GROUP BY confidence_level
ORDER BY 
    CASE confidence_level 
        WHEN 'HIGH' THEN 1 
        WHEN 'MEDIUM' THEN 2 
        WHEN 'LOW' THEN 3 
    END;
```

### 3. Traded vs Non-Traded Performance
```sql
SELECT 
    was_traded,
    COUNT(*) as predictions,
    AVG(CASE WHEN ml_direction_correct THEN 1.0 ELSE 0.0 END) as ml_accuracy,
    AVG(final_move_pct) as avg_move,
    AVG(net_pnl) FILTER (WHERE was_traded) as avg_pnl_if_traded
FROM ml_prediction_outcomes
WHERE outcome_status IN ('SUCCESS', 'FAILURE')
GROUP BY was_traded;
```

## Application Steps

### Prerequisites
1. Database running and accessible
2. `.env` file configured with correct DATABASE_URL
3. Python virtual environment activated

### Apply Migration

```bash
cd backend
source .venv/bin/activate
alembic upgrade head
```

### Verify Migration

```bash
# Check current migration
alembic current

# Should show: 0023 (head)

# Verify table exists
psql $DATABASE_URL -c "\d ml_prediction_outcomes"
```

### Rollback (if needed)

```bash
# Rollback one step
alembic downgrade -1

# Rollback to specific revision
alembic downgrade 0022
```

## Integration

### SQLAlchemy Model
The model is defined in `backend/app/models/ml_data.py` as `MLPredictionOutcome`.

### API Endpoint
Pattern analysis endpoint: `GET /api/v1/ml/pattern-analysis`
- Detects patterns using TA-Lib
- Returns historical accuracy from this table
- Caches results for performance

### Background Job
A background job (to be implemented) will:
1. Query pending predictions
2. Fetch OHLCV data for measurement window
3. Calculate outcomes (direction correct, TP/SL hits)
4. Update outcome_status to SUCCESS/FAILURE

## Performance Considerations

### Indexes
All critical query patterns are covered by indexes for optimal performance.

### TimescaleDB (Optional)
For time-series optimization, convert to hypertable:
```sql
SELECT create_hypertable('ml_prediction_outcomes', 'predicted_at', if_not_exists => TRUE);
```

### Partitioning (Future)
Consider partitioning by `predicted_at` for large datasets (>10M rows).

## Monitoring

### Key Metrics
- Total predictions per day
- Outcome measurement lag (predicted_at → measured_at)
- Accuracy by pattern/confidence
- TP/SL hit rates

### Alerts
- Outcome measurement lag > 7 days
- Accuracy drop > 10% for any pattern
- High-confidence predictions accuracy < 60%

## Related Files

- **Migration**: `backend/alembic/versions/0023_ml_prediction_outcomes.py`
- **Model**: `backend/app/models/ml_data.py` (MLPredictionOutcome)
- **Schema**: `backend/app/schemas/pattern_analysis.py`
- **Service**: `backend/app/services/pattern_detection_service.py`
- **API**: `backend/app/api/v1/ml_patterns.py`

## Status

- [x] Migration file created
- [x] SQLAlchemy model defined
- [x] API endpoint implemented
- [x] Router registered
- [ ] Migration applied to database
- [ ] Background outcome measurement job
- [ ] Historical backfill (10 years OHLCV data)
- [ ] Admin dashboard for ML governance

## Next Steps

1. **Apply Migration**: Run `alembic upgrade head`
2. **Test Endpoint**: Verify pattern detection works
3. **Implement Background Job**: Outcome measurement worker
4. **Historical Backfill**: Populate with past pattern detections
5. **Dashboard**: Build ML governance metrics dashboard
