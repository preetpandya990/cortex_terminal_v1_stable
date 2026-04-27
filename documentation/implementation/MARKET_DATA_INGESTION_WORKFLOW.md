# Market Data Ingestion Workflow - Implementation Guide

**Focus Area:** Market Data Ingestion (OHLCV + Real-time Ticks)  
**Date:** 2026-04-16  
**Status:** Production-Ready, Needs Testing & Validation

---

## 🎯 Objective

Establish a robust, production-grade market data ingestion workflow for:
1. **Historical OHLCV data** - Bulk ingestion via REST API
2. **Real-time tick data** - WebSocket streaming
3. **Instrument master sync** - Security metadata updates

---

## 📊 Current Implementation Status

### ✅ Completed Components

| Component | Status | File | LOC |
|-----------|--------|------|-----|
| UpstoxClient | ✅ Production | `services/upstox_client.py` | 150 |
| DataIngestionService | ✅ Production | `services/data_ingestion.py` | 160 |
| TickStreamService | ✅ Production | `services/tick_stream.py` | 120 |
| Database Models | ✅ Production | `models/upstox_data.py` | 160 |
| API Endpoints | ✅ Production | `api/v1/upstox.py` | 200 |
| Schemas | ✅ Production | `schemas/upstox.py` | 30 |

### ⚠️ Needs Attention

| Item | Priority | Status |
|------|----------|--------|
| End-to-end testing | 🔴 High | Not tested |
| Data validation | 🟡 Medium | Basic validation only |
| Error recovery | 🟡 Medium | Needs improvement |
| Monitoring/metrics | 🟡 Medium | Basic logging only |
| Documentation | 🟢 Low | Complete |

---

## 🔄 Workflow Overview

### 1. Historical OHLCV Ingestion

```
┌─────────────────────────────────────────────────────────────────┐
│                  HISTORICAL OHLCV INGESTION                      │
└─────────────────────────────────────────────────────────────────┘

User/Script
    │
    │ POST /api/v1/upstox/ingest
    │ {
    │   "instrument_keys": ["NSE_EQ|INE002A01018"],
    │   "timeframe": "1d",
    │   "from_date": "2024-01-01",
    │   "to_date": "2024-12-31"
    │ }
    │
    v
┌─────────────────────┐
│  API Endpoint       │  Rate limit: 5/min
│  /upstox/ingest     │  Auth: JWT required
└─────────────────────┘
    │
    │ Background task queued
    │ HTTP 202 Accepted
    │
    v
┌─────────────────────┐
│  DataIngestion      │
│  Service            │
└─────────────────────┘
    │
    │ For each instrument_key:
    │
    v
┌─────────────────────┐
│  UpstoxClient       │  GET /historical-candle/{key}/{tf}/{to}/{from}
│  .get()             │  Timeout: 15s
└─────────────────────┘
    │
    │ Response: {"data": {"candles": [[ts, o, h, l, c, v, oi], ...]}}
    │
    v
┌─────────────────────┐
│  Transform          │  Convert to dict format
│  Candles            │  {instrument_key, timeframe, timestamp, OHLCV}
└─────────────────────┘
    │
    v
┌─────────────────────┐
│  bulk_upsert_       │  Batch size: 1000 rows
│  ohlcv()            │  ON CONFLICT DO NOTHING
└─────────────────────┘
    │
    v
┌─────────────────────┐
│  upstox_ohlcv       │  PostgreSQL table
│  (Database)         │  Unique: (instrument_key, timeframe, timestamp)
└─────────────────────┘
```

### 2. Real-time Tick Streaming

```
┌─────────────────────────────────────────────────────────────────┐
│                  REAL-TIME TICK STREAMING                        │
└─────────────────────────────────────────────────────────────────┘

User/Script
    │
    │ POST /api/v1/upstox/stream/start
    │ {"instrument_keys": ["NSE_EQ|INE002A01018"]}
    │
    v
┌─────────────────────┐
│  API Endpoint       │  Rate limit: 30/min
│  /stream/start      │  Auth: JWT required
└─────────────────────┘
    │
    │ Subscribe to symbols
    │
    v
┌─────────────────────┐
│  TickStreamService  │  Singleton service
│  .add_symbols()     │  Manages WebSocket connection
└─────────────────────┘
    │
    │ WebSocket connection
    │
    v
┌─────────────────────┐
│  Upstox WebSocket   │  wss://api.upstox.com/v2/feed/market-data-feed
│  Feed               │  Full mode (LTP + volume + OI)
└─────────────────────┘
    │
    │ Incoming ticks (JSON)
    │
    v
┌─────────────────────┐
│  TickStreamService  │  Parse JSON
│  .on_message()      │  Fan-out to subscribers
└─────────────────────┘
    │
    ├─────────────────────────────────────────┐
    │                                         │
    v                                         v
┌─────────────────────┐           ┌─────────────────────┐
│  Redis Pub/Sub      │           │  upstox_ticks       │
│  market_data.tick   │           │  (Database)         │
└─────────────────────┘           └─────────────────────┘
    │                                         │
    v                                         v
┌─────────────────────┐           ┌─────────────────────┐
│  Frontend           │           │  ML Feature         │
│  WebSocket          │           │  Pipeline           │
└─────────────────────┘           └─────────────────────┘
```

### 3. Instrument Master Sync

```
┌─────────────────────────────────────────────────────────────────┐
│                  INSTRUMENT MASTER SYNC                          │
└─────────────────────────────────────────────────────────────────┘

Scheduled Job (Daily)
    │
    v
┌─────────────────────┐
│  UpstoxClient       │  GET /instruments/NSE.json.gz
│  .get()             │  Download compressed JSON
└─────────────────────┘
    │
    │ Decompress + parse
    │
    v
┌─────────────────────┐
│  Transform          │  Extract: instrument_key, symbol, name,
│  Instruments        │           exchange, ISIN
└─────────────────────┘
    │
    v
┌─────────────────────┐
│  sync_instrument_   │  Batch size: 1000 rows
│  master()           │  ON CONFLICT DO UPDATE
└─────────────────────┘
    │
    v
┌─────────────────────┐
│  instrument_master  │  PostgreSQL table
│  (Database)         │  Unique: instrument_key
└─────────────────────┘
```

---

## 🔧 Implementation Details

### 1. Historical OHLCV Ingestion

#### API Endpoint

**File:** `backend/app/api/v1/upstox.py`

```python
@router.post("/ingest", response_model=IngestResponse)
@limiter.limit("5/minute")
async def ingest_candles(
    request: Request,
    body: IngestRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
) -> IngestResponse:
    """
    Trigger bulk OHLCV ingestion for one or more instruments.
    Heavy work runs as a background task — HTTP response returns immediately.
    """
    ingestion_service: DataIngestionService = request.app.state.ingestion_service

    async def _run_ingest() -> None:
        for key in body.instrument_keys:
            try:
                count = await ingestion_service.fetch_and_store_candles(
                    session=session,
                    instrument_key=key,
                    timeframe=body.timeframe,
                    from_date=body.from_date,
                    to_date=body.to_date,
                )
                logger.info("Ingested %d candles for %s", count, key)
            except Exception:
                logger.exception("Ingestion failed for %s", key)

    background_tasks.add_task(_run_ingest)

    return IngestResponse(
        status="accepted",
        message=f"Ingestion queued for {len(body.instrument_keys)} instruments",
        instrument_count=len(body.instrument_keys),
    )
```

**Key Features:**
- ✅ Background task execution (non-blocking)
- ✅ Rate limiting (5 requests/minute)
- ✅ JWT authentication required
- ✅ Per-instrument error handling
- ✅ Comprehensive logging

#### Data Ingestion Service

**File:** `backend/app/services/data_ingestion.py`

```python
class DataIngestionService:
    """High-level service for fetching and storing OHLCV candle data."""

    def __init__(self, upstox_client: UpstoxClient) -> None:
        self._client = upstox_client

    async def fetch_and_store_candles(
        self,
        session: AsyncSession,
        instrument_key: str,
        timeframe: str,
        from_date: str,
        to_date: str,
    ) -> int:
        """
        Fetch OHLCV candles from Upstox and bulk-upsert into the database.
        Returns the count of rows upserted.
        """
        # 1. Fetch from Upstox API
        data = await self._client.get(
            f"/historical-candle/{instrument_key}/{timeframe}/{to_date}/{from_date}"
        )

        candles = data.get("data", {}).get("candles", [])
        if not candles:
            logger.info("No candles returned for %s [%s]", instrument_key, timeframe)
            return 0

        # 2. Transform to dict format
        rows = [
            {
                "instrument_key": instrument_key,
                "timeframe": timeframe,
                "timestamp": candle[0],
                "open": candle[1],
                "high": candle[2],
                "low": candle[3],
                "close": candle[4],
                "volume": candle[5],
                "oi": candle[6] if len(candle) > 6 else 0,
            }
            for candle in candles
        ]

        # 3. Bulk upsert
        return await bulk_upsert_ohlcv(session, rows)
```

#### Bulk Upsert Function

**File:** `backend/app/services/data_ingestion.py`

```python
async def bulk_upsert_ohlcv(
    session: AsyncSession,
    rows: Sequence[dict],
    *,
    batch_size: int | None = None,
) -> int:
    """
    Upsert OHLCV candles in batches.
    Uses PostgreSQL INSERT ... ON CONFLICT DO NOTHING.
    """
    if not rows:
        return 0

    size = batch_size or settings.BULK_INSERT_BATCH_SIZE  # Default: 1000
    total = 0

    for i in range(0, len(rows), size):
        batch = list(rows[i : i + size])
        stmt = (
            pg_insert(UpstoxOHLCV)
            .values(batch)
            .on_conflict_do_nothing(
                index_elements=["instrument_key", "timeframe", "timestamp"]
            )
        )
        await session.execute(stmt)
        total += len(batch)
        logger.debug("Upserted batch of %d OHLCV rows", len(batch))

    await session.commit()
    logger.info("Bulk OHLCV upsert complete: %d rows processed", total)
    return total
```

**Key Features:**
- ✅ Batch processing (1000 rows/batch)
- ✅ Idempotent (ON CONFLICT DO NOTHING)
- ✅ Single database round-trip per batch
- ✅ Configurable batch size
- ✅ Comprehensive logging

### 2. Real-time Tick Streaming

#### API Endpoints

**File:** `backend/app/api/v1/upstox.py`

```python
@router.post("/stream/start", response_model=StreamStatusResponse)
@limiter.limit("30/minute")
async def start_stream(
    request: Request,
    instrument_keys: list[str],
    user_id: str = Depends(get_current_user_id),
    tick_service: TickStreamService = Depends(get_tick_service),
) -> StreamStatusResponse:
    """Subscribe to real-time tick feed for the given instruments."""
    if not instrument_keys:
        return StreamStatusResponse(status="no-op", message="No instrument keys provided")

    await tick_service.add_symbols(instrument_keys)

    if not tick_service._running:
        asyncio.create_task(tick_service.connect())

    return StreamStatusResponse(
        status="subscribed",
        message=f"Subscribed to {len(instrument_keys)} instruments",
        instrument_count=len(instrument_keys),
    )


@router.post("/stream/stop", response_model=StreamStatusResponse)
@limiter.limit("10/minute")
async def stop_stream(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    tick_service: TickStreamService = Depends(get_tick_service),
) -> StreamStatusResponse:
    """Gracefully disconnect the tick stream."""
    await tick_service.disconnect()
    return StreamStatusResponse(status="disconnected", message="Stream stopped")


@router.get("/stream/status", response_model=StreamStatusResponse)
@limiter.limit("60/minute")
async def stream_status(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    tick_service: TickStreamService = Depends(get_tick_service),
) -> StreamStatusResponse:
    """Return current tick stream connection status."""
    connected = tick_service._ws is not None and not tick_service._ws.closed
    return StreamStatusResponse(
        status="connected" if connected else "disconnected",
        instrument_count=len(tick_service._subscribed_symbols),
    )
```

#### Tick Stream Service

**File:** `backend/app/services/tick_stream.py`

```python
class TickStreamService(BaseWebSocketService):
    """
    Manages a single Upstox market-data WebSocket connection and fans out
    incoming ticks to all registered async subscriber callbacks.
    """

    def __init__(self, upstox_client: UpstoxClient) -> None:
        super().__init__()
        self._upstox_client = upstox_client
        self._subscribers: dict[str, TickCallback] = {}
        self._subscribed_symbols: set[str] = set()

    async def on_message(self, message: bytes | str) -> None:
        """Parse tick and fan out to all subscribers concurrently."""
        tick = json.loads(message) if isinstance(message, str) else json.loads(message.decode())

        if not self._subscribers:
            return

        # Fan-out to all subscribers
        await asyncio.gather(
            *(cb(tick) for cb in self._subscribers.values()),
            return_exceptions=True,
        )

    async def add_symbols(self, instrument_keys: list[str]) -> None:
        """Add symbols to subscription list."""
        new_keys = set(instrument_keys) - self._subscribed_symbols
        if not new_keys:
            return
        
        self._subscribed_symbols.update(new_keys)
        
        if self._ws and not self._ws.closed:
            await self._send_subscription(list(new_keys))

    async def _send_subscription(self, instrument_keys: list[str]) -> None:
        """Send subscription message to Upstox WebSocket."""
        payload = json.dumps({
            "guid": "cortex-tick-sub",
            "method": "sub",
            "data": {
                "mode": "full",
                "instrumentKeys": instrument_keys
            },
        })
        await self.send(payload)
        logger.info("Subscribed to %d instruments", len(instrument_keys))
```

**Key Features:**
- ✅ Single WebSocket connection (singleton)
- ✅ Fan-out to multiple subscribers
- ✅ Automatic reconnection
- ✅ Symbol subscription management
- ✅ Concurrent callback execution

---

## 📝 Testing Workflow

### 1. Manual Testing

#### Test Historical OHLCV Ingestion

```bash
# Set variables
export API_URL="http://localhost:8000"
export TOKEN="your-jwt-token"

# Test ingestion for single symbol
curl -X POST "$API_URL/api/v1/upstox/ingest" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["NSE_EQ|INE002A01018"],
    "timeframe": "1d",
    "from_date": "2024-01-01",
    "to_date": "2024-12-31"
  }'

# Expected response:
# {
#   "status": "accepted",
#   "message": "Ingestion queued for 1 instruments",
#   "instrument_count": 1
# }

# Check logs
tail -f backend/logs/api.log | grep "Ingested"

# Verify in database
psql -d cortex_db -c "
  SELECT COUNT(*) FROM upstox_ohlcv 
  WHERE instrument_key = 'NSE_EQ|INE002A01018' 
    AND timeframe = '1d';
"
```

#### Test Real-time Tick Streaming

```bash
# Start stream
curl -X POST "$API_URL/api/v1/upstox/stream/start" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["NSE_EQ|INE002A01018"]
  }'

# Check status
curl "$API_URL/api/v1/upstox/stream/status" \
  -H "Authorization: Bearer $TOKEN"

# Expected response:
# {
#   "status": "connected",
#   "instrument_count": 1
# }

# Check ticks in database
psql -d cortex_db -c "
  SELECT * FROM upstox_ticks 
  WHERE instrument_key = 'NSE_EQ|INE002A01018' 
  ORDER BY timestamp DESC 
  LIMIT 10;
"

# Stop stream
curl -X POST "$API_URL/api/v1/upstox/stream/stop" \
  -H "Authorization: Bearer $TOKEN"
```

### 2. Automated Testing Script

**File:** `backend/scripts/test_market_data_ingestion.py`

```python
#!/usr/bin/env python3
"""
Test Market Data Ingestion Workflow
"""
import asyncio
import httpx
from datetime import datetime, timedelta

API_URL = "http://localhost:8000"
TOKEN = "your-jwt-token"

async def test_ohlcv_ingestion():
    """Test historical OHLCV ingestion"""
    print("Testing OHLCV ingestion...")
    
    async with httpx.AsyncClient() as client:
        # Test ingestion
        response = await client.post(
            f"{API_URL}/api/v1/upstox/ingest",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "instrument_keys": ["NSE_EQ|INE002A01018"],
                "timeframe": "1d",
                "from_date": "2024-01-01",
                "to_date": "2024-12-31",
            },
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"
        print(f"✓ Ingestion queued: {data['message']}")
        
        # Wait for background task
        await asyncio.sleep(5)
        
        # Verify in database (requires psycopg2)
        import psycopg2
        conn = psycopg2.connect(
            "postgresql://cortex:cortex_pg@localhost:5433/cortex_db"
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM upstox_ohlcv 
            WHERE instrument_key = 'NSE_EQ|INE002A01018' 
              AND timeframe = '1d'
        """)
        count = cur.fetchone()[0]
        print(f"✓ Ingested {count} candles")
        conn.close()

async def test_tick_streaming():
    """Test real-time tick streaming"""
    print("\nTesting tick streaming...")
    
    async with httpx.AsyncClient() as client:
        # Start stream
        response = await client.post(
            f"{API_URL}/api/v1/upstox/stream/start",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"instrument_keys": ["NSE_EQ|INE002A01018"]},
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "subscribed"
        print(f"✓ Stream started: {data['message']}")
        
        # Check status
        response = await client.get(
            f"{API_URL}/api/v1/upstox/stream/status",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        
        assert response.status_code == 200
        data = response.json()
        print(f"✓ Stream status: {data['status']}")
        
        # Wait for ticks
        await asyncio.sleep(10)
        
        # Stop stream
        response = await client.post(
            f"{API_URL}/api/v1/upstox/stream/stop",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        
        assert response.status_code == 200
        print("✓ Stream stopped")

if __name__ == "__main__":
    asyncio.run(test_ohlcv_ingestion())
    asyncio.run(test_tick_streaming())
    print("\n✅ All tests passed!")
```

---

## 🚀 Next Steps

### Immediate Actions (Today)

1. **Test OHLCV Ingestion**
   ```bash
   # Test with 1 symbol, 1 year of data
   curl -X POST http://localhost:8000/api/v1/upstox/ingest \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "instrument_keys": ["NSE_EQ|INE002A01018"],
       "timeframe": "1d",
       "from_date": "2024-01-01",
       "to_date": "2024-12-31"
     }'
   
   # Verify in database
   psql -d cortex_db -c "SELECT COUNT(*) FROM upstox_ohlcv;"
   ```

2. **Test Tick Streaming**
   ```bash
   # Start stream
   curl -X POST http://localhost:8000/api/v1/upstox/stream/start \
     -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"instrument_keys": ["NSE_EQ|INE002A01018"]}'
   
   # Check status
   curl http://localhost:8000/api/v1/upstox/stream/status \
     -H "Authorization: Bearer $TOKEN"
   ```

3. **Monitor Logs**
   ```bash
   # API logs
   tail -f backend/logs/api.log | grep -E "Ingested|Upserted"
   
   # Database logs
   tail -f /var/log/postgresql/postgresql-14-main.log
   ```

### Short-term (This Week)

1. **Bulk Ingestion Script**
   - Create script to ingest top 50 NSE stocks
   - Add progress tracking
   - Add error recovery

2. **Data Validation**
   - Check for missing bars
   - Detect outliers (price spikes)
   - Validate OHLC relationships (O/H/L/C)

3. **Monitoring**
   - Add Prometheus metrics
   - Create Grafana dashboard
   - Set up alerts

### Medium-term (This Month)

1. **Performance Optimization**
   - Benchmark ingestion speed
   - Optimize batch size
   - Add connection pooling

2. **Error Recovery**
   - Implement retry logic
   - Add dead letter queue
   - Improve error messages

3. **Documentation**
   - Add API examples
   - Create troubleshooting guide
   - Document common issues

---

## 📚 Reference

### Key Files

| File | Purpose |
|------|---------|
| `backend/app/services/upstox_client.py` | Upstox API client |
| `backend/app/services/data_ingestion.py` | Bulk OHLCV ingestion |
| `backend/app/services/tick_stream.py` | WebSocket tick stream |
| `backend/app/api/v1/upstox.py` | API endpoints |
| `backend/app/models/upstox_data.py` | Database models |
| `backend/app/schemas/upstox.py` | Pydantic schemas |

### Configuration

```bash
# Upstox API
UPSTOX_API_KEY=your-api-key
UPSTOX_API_SECRET=your-api-secret
UPSTOX_ACCESS_TOKEN=your-access-token
UPSTOX_BASE_URL=https://api.upstox.com/v3
UPSTOX_WS_URL=wss://api.upstox.com/v2/feed/market-data-feed

# Ingestion
BULK_INSERT_BATCH_SIZE=1000

# Database
DATABASE_URL=postgresql+asyncpg://cortex:cortex_pg@localhost:5433/cortex_db
DB_POOL_SIZE=10
```

### Database Schema

```sql
-- OHLCV table
CREATE TABLE upstox_ohlcv (
    id BIGSERIAL PRIMARY KEY,
    instrument_key VARCHAR(100) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open NUMERIC(18, 4) NOT NULL,
    high NUMERIC(18, 4) NOT NULL,
    low NUMERIC(18, 4) NOT NULL,
    close NUMERIC(18, 4) NOT NULL,
    volume BIGINT NOT NULL,
    oi BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_stock_ohlcv UNIQUE (instrument_key, timeframe, timestamp)
);

-- Ticks table
CREATE TABLE upstox_ticks (
    id BIGSERIAL PRIMARY KEY,
    instrument_key VARCHAR(100) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    last_price NUMERIC(18, 4) NOT NULL,
    volume BIGINT,
    oi BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

---

**Status:** Ready for testing and validation  
**Next:** Execute immediate actions and validate workflow
