# Market Data Ingestion - Action Checklist

**Date:** 2026-04-16  
**Focus:** Market Data Ingestion Testing & Validation

---

## ✅ Pre-flight Checks

- [ ] Backend API running (`http://localhost:8000`)
- [ ] PostgreSQL running (port 5433)
- [ ] Redis running (port 6379)
- [ ] Upstox access token configured
- [ ] JWT token obtained for API calls

**Quick Check:**
```bash
# Check services
curl http://localhost:8000/health
psql -d cortex_db -c "SELECT 1;"
redis-cli PING

# Get JWT token
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your-password"}'
```

---

## 🎯 Test 1: Historical OHLCV Ingestion

### Step 1: Ingest Sample Data

```bash
export API_URL="http://localhost:8000"
export TOKEN="your-jwt-token-here"

# Ingest 1 year of daily data for Reliance (INE002A01018)
curl -X POST "$API_URL/api/v1/upstox/ingest" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["NSE_EQ|INE002A01018"],
    "timeframe": "1d",
    "from_date": "2024-01-01",
    "to_date": "2024-12-31"
  }'
```

**Expected Response:**
```json
{
  "status": "accepted",
  "message": "Ingestion queued for 1 instruments",
  "instrument_count": 1
}
```

**Checklist:**
- [ ] HTTP 200 response received
- [ ] Status is "accepted"
- [ ] Message confirms queued ingestion

### Step 2: Monitor Logs

```bash
# Watch API logs for ingestion progress
tail -f backend/logs/api.log | grep -E "Ingested|Upserted|OHLCV"
```

**Expected Log Output:**
```
INFO: Upserted batch of 1000 OHLCV rows
INFO: Bulk OHLCV upsert complete: 250 rows processed
INFO: Ingested 250 candles for NSE_EQ|INE002A01018
```

**Checklist:**
- [ ] Batch upsert logs appear
- [ ] Final ingestion count logged
- [ ] No error messages

### Step 3: Verify in Database

```bash
# Check row count
psql -d cortex_db -c "
  SELECT COUNT(*) as total_rows
  FROM upstox_ohlcv 
  WHERE instrument_key = 'NSE_EQ|INE002A01018' 
    AND timeframe = '1d';
"

# Check date range
psql -d cortex_db -c "
  SELECT 
    MIN(timestamp) as earliest,
    MAX(timestamp) as latest,
    COUNT(*) as total
  FROM upstox_ohlcv 
  WHERE instrument_key = 'NSE_EQ|INE002A01018' 
    AND timeframe = '1d';
"

# Sample data
psql -d cortex_db -c "
  SELECT * FROM upstox_ohlcv 
  WHERE instrument_key = 'NSE_EQ|INE002A01018' 
    AND timeframe = '1d'
  ORDER BY timestamp DESC 
  LIMIT 5;
"
```

**Expected Results:**
- Total rows: ~250 (trading days in 2024)
- Date range: 2024-01-01 to 2024-12-31
- OHLCV values look reasonable (no zeros, no extreme outliers)

**Checklist:**
- [ ] Row count matches expected (~250 days)
- [ ] Date range is correct
- [ ] OHLCV values are valid (O/H/L/C relationships correct)
- [ ] No NULL values in critical columns

### Step 4: Test Idempotency

```bash
# Re-run same ingestion request
curl -X POST "$API_URL/api/v1/upstox/ingest" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["NSE_EQ|INE002A01018"],
    "timeframe": "1d",
    "from_date": "2024-01-01",
    "to_date": "2024-12-31"
  }'

# Wait 5 seconds
sleep 5

# Check row count again (should be same)
psql -d cortex_db -c "
  SELECT COUNT(*) FROM upstox_ohlcv 
  WHERE instrument_key = 'NSE_EQ|INE002A01018' 
    AND timeframe = '1d';
"
```

**Checklist:**
- [ ] Row count unchanged (no duplicates)
- [ ] ON CONFLICT DO NOTHING working correctly

---

## 🎯 Test 2: Real-time Tick Streaming

### Step 1: Start Stream

```bash
# Start WebSocket stream
curl -X POST "$API_URL/api/v1/upstox/stream/start" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["NSE_EQ|INE002A01018"]
  }'
```

**Expected Response:**
```json
{
  "status": "subscribed",
  "message": "Subscribed to 1 instruments",
  "instrument_count": 1
}
```

**Checklist:**
- [ ] HTTP 200 response
- [ ] Status is "subscribed"
- [ ] Instrument count is 1

### Step 2: Check Stream Status

```bash
# Check connection status
curl "$API_URL/api/v1/upstox/stream/status" \
  -H "Authorization: Bearer $TOKEN"
```

**Expected Response:**
```json
{
  "status": "connected",
  "instrument_count": 1
}
```

**Checklist:**
- [ ] Status is "connected"
- [ ] Instrument count is 1

### Step 3: Monitor Ticks

```bash
# Watch for incoming ticks (wait 30 seconds)
sleep 30

# Check ticks in database
psql -d cortex_db -c "
  SELECT 
    instrument_key,
    timestamp,
    last_price,
    volume
  FROM upstox_ticks 
  WHERE instrument_key = 'NSE_EQ|INE002A01018' 
  ORDER BY timestamp DESC 
  LIMIT 10;
"
```

**Expected Results:**
- Recent ticks (within last 30 seconds)
- Last price values look reasonable
- Timestamps are recent

**Checklist:**
- [ ] Ticks are being received
- [ ] Timestamps are recent (within last minute)
- [ ] Last price values are reasonable
- [ ] No NULL values

### Step 4: Stop Stream

```bash
# Stop WebSocket stream
curl -X POST "$API_URL/api/v1/upstox/stream/stop" \
  -H "Authorization: Bearer $TOKEN"
```

**Expected Response:**
```json
{
  "status": "disconnected",
  "message": "Stream stopped"
}
```

**Checklist:**
- [ ] HTTP 200 response
- [ ] Status is "disconnected"

### Step 5: Verify Stream Stopped

```bash
# Check status again
curl "$API_URL/api/v1/upstox/stream/status" \
  -H "Authorization: Bearer $TOKEN"
```

**Expected Response:**
```json
{
  "status": "disconnected",
  "instrument_count": 0
}
```

**Checklist:**
- [ ] Status is "disconnected"
- [ ] Instrument count is 0

---

## 🎯 Test 3: Multiple Instruments

### Step 1: Ingest Multiple Symbols

```bash
# Ingest data for 3 symbols
curl -X POST "$API_URL/api/v1/upstox/ingest" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": [
      "NSE_EQ|INE002A01018",
      "NSE_EQ|INE009A01021",
      "NSE_EQ|INE040A01034"
    ],
    "timeframe": "1d",
    "from_date": "2024-01-01",
    "to_date": "2024-03-31"
  }'
```

**Checklist:**
- [ ] HTTP 200 response
- [ ] Message confirms 3 instruments queued

### Step 2: Verify All Symbols

```bash
# Check row counts per symbol
psql -d cortex_db -c "
  SELECT 
    instrument_key,
    COUNT(*) as row_count,
    MIN(timestamp) as earliest,
    MAX(timestamp) as latest
  FROM upstox_ohlcv 
  WHERE timeframe = '1d'
    AND timestamp >= '2024-01-01'
    AND timestamp <= '2024-03-31'
  GROUP BY instrument_key
  ORDER BY instrument_key;
"
```

**Expected Results:**
- 3 rows (one per symbol)
- Each symbol has ~60-65 rows (Q1 2024 trading days)
- Date ranges are correct

**Checklist:**
- [ ] All 3 symbols present
- [ ] Row counts are similar (~60-65 each)
- [ ] Date ranges match request

---

## 🎯 Test 4: Error Handling

### Step 1: Test Invalid Instrument Key

```bash
# Try to ingest with invalid key
curl -X POST "$API_URL/api/v1/upstox/ingest" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["INVALID_KEY"],
    "timeframe": "1d",
    "from_date": "2024-01-01",
    "to_date": "2024-12-31"
  }'
```

**Expected Behavior:**
- HTTP 200 (accepted)
- Background task logs error
- No data ingested

**Checklist:**
- [ ] API accepts request (background task)
- [ ] Error logged in API logs
- [ ] No invalid data in database

### Step 2: Test Invalid Date Range

```bash
# Try to ingest with future dates
curl -X POST "$API_URL/api/v1/upstox/ingest" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["NSE_EQ|INE002A01018"],
    "timeframe": "1d",
    "from_date": "2025-01-01",
    "to_date": "2025-12-31"
  }'
```

**Expected Behavior:**
- HTTP 200 (accepted)
- Upstox API returns no data
- Logs show "No candles returned"

**Checklist:**
- [ ] API accepts request
- [ ] Logs show "No candles returned"
- [ ] No future-dated data in database

### Step 3: Test Rate Limiting

```bash
# Send 6 requests rapidly (limit is 5/min)
for i in {1..6}; do
  curl -X POST "$API_URL/api/v1/upstox/ingest" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "instrument_keys": ["NSE_EQ|INE002A01018"],
      "timeframe": "1d",
      "from_date": "2024-01-01",
      "to_date": "2024-01-31"
    }'
  echo "Request $i sent"
done
```

**Expected Behavior:**
- First 5 requests: HTTP 200
- 6th request: HTTP 429 (Too Many Requests)

**Checklist:**
- [ ] First 5 requests succeed
- [ ] 6th request returns HTTP 429
- [ ] Rate limiting working correctly

---

## 🎯 Test 5: Performance

### Step 1: Benchmark Ingestion Speed

```bash
# Ingest 1 year of data and measure time
time curl -X POST "$API_URL/api/v1/upstox/ingest" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["NSE_EQ|INE002A01018"],
    "timeframe": "1d",
    "from_date": "2024-01-01",
    "to_date": "2024-12-31"
  }'

# Wait for background task to complete
sleep 10

# Check database insert time
psql -d cortex_db -c "
  SELECT 
    COUNT(*) as rows,
    MAX(created_at) - MIN(created_at) as duration
  FROM upstox_ohlcv 
  WHERE instrument_key = 'NSE_EQ|INE002A01018' 
    AND timeframe = '1d'
    AND created_at > NOW() - INTERVAL '1 minute';
"
```

**Expected Performance:**
- API response: <100ms
- Background task: <5 seconds for 250 rows
- Database insert: <1 second

**Checklist:**
- [ ] API response time <100ms
- [ ] Background task completes in <5s
- [ ] Database insert time <1s

### Step 2: Check Database Performance

```bash
# Check query performance
psql -d cortex_db -c "
  EXPLAIN ANALYZE
  SELECT * FROM upstox_ohlcv 
  WHERE instrument_key = 'NSE_EQ|INE002A01018' 
    AND timeframe = '1d'
    AND timestamp >= '2024-01-01'
    AND timestamp <= '2024-12-31';
"
```

**Expected Results:**
- Index scan (not sequential scan)
- Execution time <10ms

**Checklist:**
- [ ] Query uses index
- [ ] Execution time <10ms

---

## 📊 Summary Report

After completing all tests, fill out this summary:

### Test Results

| Test | Status | Notes |
|------|--------|-------|
| OHLCV Ingestion | ⬜ Pass / ⬜ Fail | |
| Tick Streaming | ⬜ Pass / ⬜ Fail | |
| Multiple Instruments | ⬜ Pass / ⬜ Fail | |
| Error Handling | ⬜ Pass / ⬜ Fail | |
| Performance | ⬜ Pass / ⬜ Fail | |

### Data Quality

| Metric | Value | Expected | Status |
|--------|-------|----------|--------|
| Total OHLCV rows | | >0 | ⬜ |
| Unique instruments | | >0 | ⬜ |
| Date range coverage | | 2024-01-01 to 2024-12-31 | ⬜ |
| NULL values | | 0 | ⬜ |
| Duplicate rows | | 0 | ⬜ |

### Performance Metrics

| Metric | Value | Target | Status |
|--------|-------|--------|--------|
| API response time | | <100ms | ⬜ |
| Ingestion time (250 rows) | | <5s | ⬜ |
| Database insert time | | <1s | ⬜ |
| Query time | | <10ms | ⬜ |

### Issues Found

1. 
2. 
3. 

### Next Steps

1. 
2. 
3. 

---

**Completed by:** _______________  
**Date:** _______________  
**Overall Status:** ⬜ Pass / ⬜ Fail / ⬜ Needs Work
