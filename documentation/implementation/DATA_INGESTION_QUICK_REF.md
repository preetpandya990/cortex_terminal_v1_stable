# Data Ingestion - Quick Reference

**Last Updated:** 2026-04-16

---

## System Overview

Cortex AI implements **dual-pipeline data ingestion**:

1. **Market Data Pipeline** - Real-time + historical OHLCV from Upstox
2. **News Intelligence Pipeline** - RSS feeds → AI processing → Trading signals

---

## Key Components

### Market Data Ingestion

| Component | File | Purpose |
|-----------|------|---------|
| UpstoxClient | `services/upstox_client.py` | HTTP client for Upstox API |
| DataIngestionService | `services/data_ingestion.py` | Bulk OHLCV ingestion (1000 rows/batch) |
| TickStreamService | `services/tick_stream.py` | WebSocket real-time ticks |

### RSS Ingestion

| Component | File | Purpose |
|-----------|------|---------|
| RSSFetcher | `ai/ingestion/rss_fetcher.py` | Async RSS feed fetcher |
| EventProcessor | `ai/intelligence/event_processor.py` | Process raw events → signals |
| NLPEngine | `ai/intelligence/nlp_engine.py` | Entity extraction (Ollama) |
| EventClassifier | `ai/intelligence/event_classifier.py` | Event classification (LLM) |

### Worker Process

| Component | File | Purpose |
|-----------|------|---------|
| Worker | `app/worker.py` | Orchestrates 6 background loops |

---

## Database Tables

### Market Data

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `stock_ohlcv` | TimescaleDB hypertable | timestamp, symbol, exchange, timeframe, OHLCV |
| `upstox_ohlcv` | Legacy OHLCV | instrument_key, timeframe, timestamp, OHLCV |
| `upstox_ticks` | Real-time ticks | instrument_key, timestamp, last_price |
| `instrument_master` | Security metadata | instrument_key, symbol, exchange, ISIN |

### RSS/Events

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `ai_raw_events` | Raw RSS entries | content_hash, raw_content, source_name |
| `ai_processed_events` | Processed events | raw_event_id, processing_status |
| `ai_event_classifications` | LLM classifications | event_type, impact_score, affected_symbols |
| `ai_trading_signals` | Generated signals | symbol, action, confidence_score |

---

## API Endpoints

### Market Data

```bash
# Ingest historical candles
POST /api/v1/upstox/ingest
{
  "instrument_keys": ["NSE_EQ|INE002A01018"],
  "timeframe": "1d",
  "from_date": "2024-01-01",
  "to_date": "2024-12-31"
}

# Start WebSocket stream
POST /api/v1/upstox/stream/start
{"instrument_keys": ["NSE_EQ|INE002A01018"]}

# Stream status
GET /api/v1/upstox/stream/status
```

### RSS/Events

```bash
# Get raw events
GET /api/v1/ingestion/events/raw?limit=20

# Get processed events
GET /api/v1/ingestion/events/processed?limit=20
```

---

## Configuration

### Environment Variables

```bash
# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5433/cortex_db
DB_POOL_SIZE=10

# Redis
REDIS_URL=redis://localhost:6379/0

# Upstox
UPSTOX_API_KEY=your-key
UPSTOX_API_SECRET=your-secret
UPSTOX_ACCESS_TOKEN=your-token

# RSS Ingestion
RSS_MIN_POLL_SECONDS=300      # 5 min
RSS_MAX_POLL_SECONDS=900      # 15 min
RSS_POLL_JITTER_SECONDS=60    # 0-60s jitter

# Ollama
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b

# Worker
ENABLE_BACKGROUND_TASKS=true
WORKER_SHUTDOWN_TIMEOUT=30

# Ingestion
BULK_INSERT_BATCH_SIZE=1000
```

---

## Worker Loops

The worker process runs 6 concurrent background loops:

1. **RSS Ingestion** - Fetch RSS feeds (5-15 min interval)
2. **Event Processing** - Process raw events → signals
3. **Regime Detection** - Detect market regime changes
4. **Drift Detection** - Monitor ML model drift
5. **Safety Monitoring** - Check safety triggers
6. **Heartbeat** - Health check (30s interval)

---

## Data Flow

### Market Data Flow

```
Upstox API → UpstoxClient → DataIngestionService → bulk_upsert_ohlcv() 
  → stock_ohlcv (TimescaleDB)

Upstox WS → TickStreamService → Redis pub/sub → upstox_ticks
```

### RSS Flow

```
RSS Feeds → RSSFetcher → ai_raw_events (dedup via SHA-256)
  → EventProcessor → NLPEngine (Ollama) → EventClassifier (LLM)
  → ai_processed_events → ai_event_classifications
  → ai_trading_signals → Redis pub/sub
```

---

## Key Features

✅ **Async/await** - High throughput with asyncio  
✅ **Batch operations** - 1000 rows/batch for efficiency  
✅ **Deduplication** - SHA-256 content hashing  
✅ **TimescaleDB** - Optimized time-series storage  
✅ **Redis pub/sub** - Real-time event distribution  
✅ **Graceful shutdown** - SIGTERM handling (30s timeout)  
✅ **Error handling** - Comprehensive logging + retry logic  
✅ **Rate limiting** - Upstox API compliance  
✅ **LLM integration** - Ollama for event classification  

---

## Testing

### Manual Testing

```bash
# Check RSS ingestion
curl http://localhost:8000/api/v1/ingestion/events/raw?limit=20 \
  -H "Authorization: Bearer $TOKEN"

# Check OHLCV data
psql -d cortex_db -c "SELECT COUNT(*) FROM stock_ohlcv;"

# Check worker logs
tail -f backend/logs/worker.log | grep "RSS"

# Check heartbeat
redis-cli GET worker:heartbeat
```

### Database Validation

```sql
-- Raw events
SELECT COUNT(*) FROM ai_raw_events;
SELECT source_name, COUNT(*) FROM ai_raw_events GROUP BY source_name;

-- OHLCV data
SELECT COUNT(*) FROM stock_ohlcv;
SELECT symbol, timeframe, COUNT(*) FROM stock_ohlcv GROUP BY symbol, timeframe;

-- Ticks
SELECT COUNT(*) FROM upstox_ticks;
```

---

## Troubleshooting

### No RSS events ingested

```bash
# Check RSS feeds manually
curl -v https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms

# Check worker logs
tail -f backend/logs/worker.log | grep "RSS"

# Check database
psql -d cortex_db -c "SELECT COUNT(*) FROM ai_raw_events;"
```

### OHLCV ingestion fails

```bash
# Check Upstox API
curl -H "Authorization: Bearer $TOKEN" \
  https://api.upstox.com/v3/user/profile

# Check database connection
psql -d cortex_db -c "SELECT 1;"
```

### Worker crashes

```bash
# Check logs
tail -100 backend/logs/worker.log

# Check resources
free -h
df -h

# Restart worker
systemctl restart cortex-worker
```

---

## Performance Tuning

```bash
# Increase batch size (faster ingestion)
BULK_INSERT_BATCH_SIZE=2000

# Increase database pool
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=40

# Reduce RSS polling frequency
RSS_MIN_POLL_SECONDS=600
RSS_MAX_POLL_SECONDS=1800
```

---

## Next Steps

1. ✅ Verify RSS ingestion working
2. ✅ Test OHLCV ingestion for sample symbols
3. ✅ Monitor worker health (heartbeat, logs)
4. ⏳ Add more RSS sources
5. ⏳ Implement data quality checks
6. ⏳ Add Prometheus metrics
7. ⏳ Implement alerting

---

## Documentation

- **Full Report:** `DATA_INGESTION_CONTEXT_REPORT.md`
- **Architecture:** `documentation/architecture/ARCHITECTURE.md`
- **API Docs:** `documentation/api/API_ENDPOINTS_DOCUMENTATION.md`
- **Guides:** `documentation/guides/`

---

**For detailed information, see:** `DATA_INGESTION_CONTEXT_REPORT.md`
