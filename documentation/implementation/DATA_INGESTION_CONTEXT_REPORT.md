# Data Ingestion System - Complete Context Report

**Generated:** 2026-04-16  
**System:** Cortex AI Trading Platform  
**Version:** 1.0.0  
**Purpose:** Complete architectural context for data ingestion work

---

## Executive Summary

The Cortex AI Trading Platform implements a **dual-pipeline data ingestion architecture**:

1. **Market Data Pipeline** - Real-time + historical OHLCV data from Upstox API
2. **News Intelligence Pipeline** - RSS feed ingestion with AI-powered event processing

Both pipelines are production-ready with:
- ✅ Async/await architecture for high throughput
- ✅ Batch upsert operations (1000 rows/batch)
- ✅ Deduplication via content hashing
- ✅ TimescaleDB hypertables for time-series optimization
- ✅ Redis pub/sub for real-time event distribution
- ✅ Graceful shutdown with SIGTERM handling
- ✅ Comprehensive error handling and logging

---

## 1. Market Data Ingestion Pipeline

### 1.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                    MARKET DATA INGESTION FLOW                        │
└─────────────────────────────────────────────────────────────────────┘

┌──────────────┐     REST API      ┌──────────────────┐
│   Upstox     │ ─────────────────>│  UpstoxClient    │
│   API        │                    │  (HTTP Client)   │
└──────────────┘                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │ DataIngestion    │
                                    │ Service          │
                                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │ bulk_upsert_     │
                                    │ ohlcv()          │
                                    │ (Batch 1000)     │
                                    └──────────────────┘
                                             │
                                             v
                    ┌────────────────────────┴────────────────────────┐
                    │                                                  │
                    v                                                  v
        ┌──────────────────┐                              ┌──────────────────┐
        │  upstox_ohlcv    │                              │  stock_ohlcv     │
        │  (Legacy)        │                              │  (TimescaleDB    │
        │                  │                              │   Hypertable)    │
        └──────────────────┘                              └──────────────────┘

┌──────────────┐   WebSocket       ┌──────────────────┐
│   Upstox     │ ─────────────────>│  TickStream      │
│   WS Feed    │                    │  Service         │
└──────────────┘                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │  Redis Pub/Sub   │
                                    │  market_data.    │
                                    │  tick            │
                                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │  upstox_ticks    │
                                    │  (DB Table)      │
                                    └──────────────────┘
```

### 1.2 Core Components

#### 1.2.1 UpstoxClient (`backend/app/services/upstox_client.py`)

**Purpose:** HTTP client wrapper for Upstox REST API

**Key Features:**
- Async HTTP client (httpx)
- Automatic token refresh
- Rate limiting compliance
- Typed error handling

**Key Methods:**
```python
async def get(path: str, params: dict = None) -> dict
async def post(path: str, json: dict = None) -> dict
async def get_access_token(code: str) -> dict
```

**Error Handling:**
- `UpstoxAPIError` - API errors (4xx/5xx)
- `UpstoxConnectionError` - Network failures
- Automatic retry with exponential backoff

#### 1.2.2 DataIngestionService (`backend/app/services/data_ingestion.py`)

**Purpose:** High-level service for OHLCV data ingestion

**Key Features:**
- Batch upsert operations (1000 rows default)
- PostgreSQL ON CONFLICT DO NOTHING for idempotency
- Configurable batch size via `BULK_INSERT_BATCH_SIZE`
- Comprehensive logging

**Key Methods:**
```python
async def fetch_and_store_candles(
    session: AsyncSession,
    instrument_key: str,
    timeframe: str,
    from_date: str,
    to_date: str,
) -> int
```

**Batch Upsert Logic:**
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
    size = batch_size or settings.BULK_INSERT_BATCH_SIZE
    
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
    
    await session.commit()
    return total
```

#### 1.2.3 TickStreamService (`backend/app/services/tick_stream.py`)

**Purpose:** Real-time WebSocket tick ingestion and fan-out

**Key Features:**
- Single WebSocket connection to Upstox
- Fan-out to multiple subscribers
- Automatic reconnection with exponential backoff
- Symbol subscription management

**Architecture:**
```python
class TickStreamService(BaseWebSocketService):
    def __init__(self, upstox_client: UpstoxClient):
        self._subscribers: dict[str, TickCallback] = {}
        self._subscribed_symbols: set[str] = set()
    
    async def on_message(self, message: bytes | str):
        """Parse tick and fan out to all subscribers"""
        tick = json.loads(message)
        await asyncio.gather(
            *(cb(tick) for cb in self._subscribers.values()),
            return_exceptions=True,
        )
```

**Subscription Flow:**
1. Client calls `/api/v1/upstox/stream/start` with instrument keys
2. Service subscribes to Upstox WebSocket
3. Incoming ticks are fanned out to all registered callbacks
4. Ticks are stored in `upstox_ticks` table
5. Ticks are published to Redis `market_data.tick` channel

### 1.3 Database Schema

#### 1.3.1 StockOHLCV (TimescaleDB Hypertable)

**Table:** `stock_ohlcv`  
**Purpose:** Normalized OHLCV data optimized for time-series queries

```sql
CREATE TABLE stock_ohlcv (
    timestamp TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    exchange VARCHAR(10) NOT NULL,
    timeframe VARCHAR(10) NOT NULL,
    open NUMERIC(14, 4),
    high NUMERIC(14, 4),
    low NUMERIC(14, 4),
    close NUMERIC(14, 4),
    volume BIGINT,
    adj_close NUMERIC(14, 4),
    PRIMARY KEY (timestamp, symbol, exchange, timeframe)
);

-- TimescaleDB hypertable partitioning
SELECT create_hypertable('stock_ohlcv', 'timestamp');

-- Indexes
CREATE INDEX idx_ohlcv_symbol_timeframe_ts 
    ON stock_ohlcv (symbol, timeframe, timestamp);
CREATE INDEX idx_ohlcv_exchange_ts 
    ON stock_ohlcv (exchange, timestamp);
```

**Key Features:**
- Composite primary key prevents duplicates
- Partitioned by time for efficient queries
- Optimized for time-range scans
- Supports multiple exchanges and timeframes

#### 1.3.2 UpstoxOHLCV (Legacy)

**Table:** `upstox_ohlcv`  
**Purpose:** Raw OHLCV data from Upstox API

```sql
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
    CONSTRAINT uq_stock_ohlcv 
        UNIQUE (instrument_key, timeframe, timestamp)
);

CREATE INDEX idx_upstox_instrument_timeframe_ts 
    ON upstox_ohlcv (instrument_key, timeframe, timestamp);
```

#### 1.3.3 UpstoxTick

**Table:** `upstox_ticks`  
**Purpose:** Real-time tick data from WebSocket

```sql
CREATE TABLE upstox_ticks (
    id BIGSERIAL PRIMARY KEY,
    instrument_key VARCHAR(100) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    last_price NUMERIC(18, 4) NOT NULL,
    volume BIGINT,
    oi BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_upstox_tick_instrument_ts 
    ON upstox_ticks (instrument_key, timestamp);
```

#### 1.3.4 InstrumentMaster

**Table:** `instrument_master`  
**Purpose:** Security metadata (symbols, ISINs, exchange info)

```sql
CREATE TABLE instrument_master (
    id BIGSERIAL PRIMARY KEY,
    instrument_key VARCHAR(200) UNIQUE NOT NULL,
    symbol VARCHAR(50) NOT NULL,
    name VARCHAR(200),
    exchange_segment VARCHAR(20) NOT NULL,
    isin VARCHAR(12),
    source VARCHAR(20),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_instrument_symbol ON instrument_master (symbol);
CREATE INDEX idx_instrument_exchange ON instrument_master (exchange_segment);
```

### 1.4 API Endpoints

#### 1.4.1 Candle Ingestion

**Endpoint:** `POST /api/v1/upstox/ingest`

**Request:**
```json
{
  "instrument_keys": ["NSE_EQ|INE002A01018"],
  "timeframe": "1d",
  "from_date": "2024-01-01",
  "to_date": "2024-12-31"
}
```

**Response:**
```json
{
  "status": "success",
  "message": "Ingestion started for 1 instruments",
  "instrument_count": 1
}
```

**Rate Limit:** 5 requests/minute  
**Authentication:** Required (JWT)

#### 1.4.2 Stream Management

**Start Stream:** `POST /api/v1/upstox/stream/start`
```json
{
  "instrument_keys": ["NSE_EQ|INE002A01018", "NSE_EQ|INE009A01021"]
}
```

**Stop Stream:** `POST /api/v1/upstox/stream/stop`

**Stream Status:** `GET /api/v1/upstox/stream/status`

### 1.5 Configuration

**Environment Variables:**
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
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5433/cortex_db
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
```

---

## 2. News Intelligence Pipeline (RSS Ingestion)

### 2.1 Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                    RSS INGESTION FLOW                                │
└─────────────────────────────────────────────────────────────────────┘

┌──────────────┐                    ┌──────────────────┐
│  RSS Feeds   │                    │  RSSFetcher      │
│  (5 sources) │ ─────────────────> │  (httpx async)   │
└──────────────┘                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │  feedparser      │
                                    │  (Parse RSS)     │
                                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │  Content Hash    │
                                    │  (SHA-256)       │
                                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │  Deduplication   │
                                    │  Check           │
                                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │  ai_raw_events   │
                                    │  (DB Table)      │
                                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │  Event           │
                                    │  Processing      │
                                    │  Loop            │
                                    └──────────────────┘
                                             │
                    ┌────────────────────────┼────────────────────────┐
                    │                        │                        │
                    v                        v                        v
        ┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
        │  NLP Engine      │    │  Event           │    │  Fake News       │
        │  (Ollama LLM)    │    │  Classifier      │    │  Detector        │
        └──────────────────┘    └──────────────────┘    └──────────────────┘
                    │                        │                        │
                    └────────────────────────┼────────────────────────┘
                                             v
                                    ┌──────────────────┐
                                    │  ai_processed_   │
                                    │  events          │
                                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │  ai_trading_     │
                                    │  signals         │
                                    └──────────────────┘
                                             │
                                             v
                                    ┌──────────────────┐
                                    │  Redis Pub/Sub   │
                                    │  ai.signal       │
                                    └──────────────────┘
```

### 2.2 Core Components

#### 2.2.1 RSSFetcher (`backend/app/ai/ingestion/rss_fetcher.py`)

**Purpose:** Async RSS feed fetcher with rate limiting

**Key Features:**
- Async HTTP client (httpx)
- feedparser for RSS parsing
- SHA-256 content hashing for deduplication
- Configurable polling intervals with jitter
- Concurrent feed fetching

**Default RSS Sources:**
```python
DEFAULT_RSS_FEEDS = [
    ("Economic Times Markets", 
     "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol", 
     "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("Business Standard", 
     "https://www.business-standard.com/rss/markets-106.rss"),
    ("LiveMint Markets", 
     "https://www.livemint.com/rss/markets"),
    ("Reuters India Business", 
     "https://feeds.reuters.com/reuters/INbusinessNews"),
]
```

**Key Methods:**
```python
async def fetch_feed(self, url: str) -> list[dict]:
    """Fetch and parse RSS feed"""
    response = await self.client.get(url)
    feed = feedparser.parse(response.text)
    
    entries = []
    for entry in feed.entries:
        raw_content = f"{entry.title}\n\n{entry.summary}"
        content_hash = hashlib.sha256(raw_content.encode()).hexdigest()
        
        entries.append({
            "raw_content": raw_content,
            "event_timestamp": parse_timestamp(entry),
            "link": entry.link,
            "metadata": {...}
        })
    
    return entries

async def ingest_feed(
    self,
    db: AsyncSession,
    source_name: str,
    source_url: str,
) -> int:
    """Ingest RSS feed with deduplication"""
    items = await self.fetch_feed(source_url)
    ingested_count = 0
    
    for item in items:
        content_hash = hashlib.sha256(item["raw_content"].encode()).hexdigest()
        
        # Check for duplicate
        stmt = select(AIRawEvent).where(AIRawEvent.content_hash == content_hash)
        result = await db.execute(stmt)
        if result.scalar_one_or_none():
            continue  # Skip duplicate
        
        event = AIRawEvent(
            event_timestamp=item["event_timestamp"],
            source_type="rss",
            source_url=item["link"],
            source_name=source_name,
            content_hash=content_hash,
            raw_content=item["raw_content"],
            language="en",
            metadata=item["metadata"],
        )
        
        db.add(event)
        ingested_count += 1
    
    if ingested_count > 0:
        await db.commit()
    
    return ingested_count
```

#### 2.2.2 RSS Ingestion Loop (`backend/app/worker.py`)

**Purpose:** Background worker loop for continuous RSS ingestion

**Key Features:**
- Runs in separate worker process
- Jittered polling (300-900s + 0-60s jitter)
- Concurrent feed fetching
- Graceful shutdown with SIGTERM
- Comprehensive error handling

**Implementation:**
```python
async def rss_ingestion_loop(session_factory: async_sessionmaker) -> None:
    """RSS ingestion background loop"""
    logger.info("RSS ingestion loop started")
    fetcher = RSSFetcher()
    
    try:
        while True:
            try:
                async with session_factory() as db:
                    # Fetch all feeds concurrently
                    tasks = [
                        fetcher.ingest_feed(db, name, url)
                        for name, url in DEFAULT_RSS_FEEDS
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    total_ingested = sum(r for r in results if isinstance(r, int))
                    errors = sum(1 for r in results if isinstance(r, Exception))
                    
                    logger.info(
                        f"RSS ingestion cycle complete: "
                        f"{total_ingested} events, {errors} errors"
                    )
            
            except Exception as e:
                logger.error(f"RSS ingestion error: {e}", exc_info=True)
            
            # Jittered sleep
            jitter = random.randint(0, settings.RSS_POLL_JITTER_SECONDS)
            sleep_time = random.randint(
                settings.RSS_MIN_POLL_SECONDS,
                settings.RSS_MAX_POLL_SECONDS
            ) + jitter
            
            await asyncio.sleep(sleep_time)
    
    except asyncio.CancelledError:
        logger.info("RSS ingestion loop cancelled")
        raise
    finally:
        await fetcher.close()
```

**Configuration:**
```bash
RSS_MIN_POLL_SECONDS=300      # 5 minutes
RSS_MAX_POLL_SECONDS=900      # 15 minutes
RSS_POLL_JITTER_SECONDS=60    # 0-60s random jitter
RSS_CONCURRENCY=5             # Max concurrent feeds
```


### 2.3 Database Schema

#### 2.3.1 AIRawEvent

**Table:** `ai_raw_events`  
**Purpose:** Store raw RSS feed entries before processing

```sql
CREATE TABLE ai_raw_events (
    id BIGSERIAL PRIMARY KEY,
    event_timestamp TIMESTAMPTZ NOT NULL,
    source_type VARCHAR(50) NOT NULL,
    source_url VARCHAR(500) NOT NULL,
    source_name VARCHAR(200) NOT NULL,
    content_hash VARCHAR(64) NOT NULL UNIQUE,
    raw_content TEXT NOT NULL,
    language VARCHAR(10),
    metadata JSONB,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_raw_event_timestamp ON ai_raw_events (event_timestamp);
CREATE INDEX idx_raw_event_source_type ON ai_raw_events (source_type);
CREATE INDEX idx_raw_event_content_hash ON ai_raw_events (content_hash);
```

**Key Features:**
- `content_hash` (SHA-256) ensures deduplication
- `metadata` JSONB stores RSS-specific fields (author, tags, etc.)
- Indexed by timestamp for time-range queries

#### 2.3.2 AIProcessedEvent

**Table:** `ai_processed_events`  
**Purpose:** Store processed events after NLP/classification

```sql
CREATE TABLE ai_processed_events (
    id BIGSERIAL PRIMARY KEY,
    raw_event_id BIGINT NOT NULL,
    processed_timestamp TIMESTAMPTZ NOT NULL,
    translated_content TEXT,
    detected_language VARCHAR(10),
    translation_confidence NUMERIC(5, 2),
    processing_status VARCHAR(20) DEFAULT 'pending',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_processed_event_raw_id ON ai_processed_events (raw_event_id);
CREATE INDEX idx_processed_event_timestamp ON ai_processed_events (processed_timestamp);
CREATE INDEX idx_processed_event_status ON ai_processed_events (processing_status);
```

#### 2.3.3 AIEventClassification

**Table:** `ai_event_classifications`  
**Purpose:** Store LLM-generated event classifications

```sql
CREATE TABLE ai_event_classifications (
    id BIGSERIAL PRIMARY KEY,
    nlp_result_id BIGINT NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    impact_score NUMERIC(5, 2) NOT NULL,
    affected_symbols VARCHAR(20)[],
    classification_confidence NUMERIC(5, 2) NOT NULL,
    reasoning TEXT,
    decay_half_life_hours INTEGER DEFAULT 24,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_event_class_nlp_id ON ai_event_classifications (nlp_result_id);
CREATE INDEX idx_event_class_type ON ai_event_classifications (event_type);
CREATE INDEX idx_event_class_symbols ON ai_event_classifications USING GIN (affected_symbols);
```

#### 2.3.4 AITradingSignal

**Table:** `ai_trading_signals`  
**Purpose:** Store generated trading signals

```sql
CREATE TABLE ai_trading_signals (
    id BIGSERIAL PRIMARY KEY,
    signal_timestamp TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL,
    action VARCHAR(10) NOT NULL,
    confidence_score NUMERIC(5, 2) NOT NULL,
    strategy_id VARCHAR(100) NOT NULL,
    regime_type VARCHAR(50) NOT NULL,
    contributing_events JSONB,
    ml_predictions JSONB,
    technical_indicators JSONB,
    reasoning TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_signal_timestamp ON ai_trading_signals (signal_timestamp);
CREATE INDEX idx_signal_symbol ON ai_trading_signals (symbol);
CREATE INDEX idx_signal_action ON ai_trading_signals (action);
CREATE INDEX idx_signal_strategy ON ai_trading_signals (strategy_id);
```

### 2.4 Event Processing Pipeline

#### 2.4.1 Event Processing Loop

**File:** `backend/app/ai/intelligence/event_processor.py`

**Flow:**
1. Poll `ai_raw_events` for unprocessed events
2. Extract entities using NLP engine
3. Classify event type using Ollama LLM
4. Detect fake news using multi-layer validation
5. Generate trading signals if applicable
6. Publish signals to Redis pub/sub

**Implementation:**
```python
async def event_processing_loop(
    session_factory: async_sessionmaker,
    ensemble_predictor=None,
    feature_loader=None,
) -> None:
    """Event processing background loop"""
    logger.info("Event processing loop started")
    
    try:
        while True:
            try:
                async with session_factory() as db:
                    # Get unprocessed events
                    stmt = (
                        select(AIRawEvent)
                        .where(~exists(
                            select(1)
                            .where(AIProcessedEvent.raw_event_id == AIRawEvent.id)
                        ))
                        .order_by(AIRawEvent.event_timestamp.desc())
                        .limit(10)
                    )
                    
                    result = await db.execute(stmt)
                    events = result.scalars().all()
                    
                    if not events:
                        await asyncio.sleep(30)
                        continue
                    
                    # Process each event
                    for event in events:
                        try:
                            # 1. NLP extraction
                            nlp_result = await extract_entities(event.raw_content)
                            
                            # 2. Event classification
                            classification = await classify_event(
                                event.raw_content,
                                nlp_result
                            )
                            
                            # 3. Fake news detection
                            fake_news_flag = await detect_fake_news(
                                event,
                                classification
                            )
                            
                            # 4. Generate trading signal if applicable
                            if (classification.impact_score > 0.7 and
                                fake_news_flag.flag_status == "verified"):
                                
                                signal = await generate_trading_signal(
                                    event,
                                    classification,
                                    ensemble_predictor,
                                    feature_loader,
                                )
                                
                                # Publish to Redis
                                await publish_signal(signal)
                            
                            # Mark as processed
                            processed_event = AIProcessedEvent(
                                raw_event_id=event.id,
                                processed_timestamp=datetime.now(timezone.utc),
                                processing_status="completed",
                            )
                            db.add(processed_event)
                            await db.commit()
                        
                        except Exception as e:
                            logger.error(f"Event processing error: {e}", exc_info=True)
                            # Mark as failed
                            processed_event = AIProcessedEvent(
                                raw_event_id=event.id,
                                processed_timestamp=datetime.now(timezone.utc),
                                processing_status="failed",
                                error_message=str(e),
                            )
                            db.add(processed_event)
                            await db.commit()
            
            except Exception as e:
                logger.error(f"Event processing loop error: {e}", exc_info=True)
            
            await asyncio.sleep(10)
    
    except asyncio.CancelledError:
        logger.info("Event processing loop cancelled")
        raise
```

#### 2.4.2 NLP Engine (Ollama Integration)

**File:** `backend/app/ai/intelligence/nlp_engine.py`

**Purpose:** Extract entities and sentiment using Ollama LLM

**Key Features:**
- Llama 3.1 8B model
- Named entity recognition (companies, people, locations)
- Sentiment analysis
- Keyword extraction

**Example:**
```python
async def extract_entities(content: str) -> dict:
    """Extract entities using Ollama"""
    prompt = f"""
    Extract the following from this financial news:
    1. Companies mentioned (with stock symbols if available)
    2. Key people mentioned
    3. Sentiment (positive/negative/neutral)
    4. Keywords (max 5)
    
    News: {content}
    
    Return JSON format.
    """
    
    response = await ollama_client.generate(
        model="llama3.1:8b",
        prompt=prompt,
        temperature=0.1,
    )
    
    return json.loads(response["response"])
```

#### 2.4.3 Event Classifier

**File:** `backend/app/ai/intelligence/event_classifier.py`

**Purpose:** Classify event type and impact using LLM

**Event Types:**
- `earnings_report` - Quarterly/annual earnings
- `merger_acquisition` - M&A announcements
- `regulatory_change` - Policy/regulation changes
- `product_launch` - New product announcements
- `management_change` - Leadership changes
- `market_news` - General market news

**Example:**
```python
async def classify_event(content: str, nlp_result: dict) -> AIEventClassification:
    """Classify event using Ollama"""
    prompt = f"""
    Classify this financial event:
    
    Content: {content}
    Entities: {nlp_result}
    
    Provide:
    1. Event type (earnings_report, merger_acquisition, etc.)
    2. Impact score (0-100)
    3. Affected stock symbols
    4. Confidence (0-100)
    5. Reasoning
    
    Return JSON format.
    """
    
    response = await ollama_client.generate(
        model="llama3.1:8b",
        prompt=prompt,
        temperature=0.2,
    )
    
    result = json.loads(response["response"])
    
    return AIEventClassification(
        event_type=result["event_type"],
        impact_score=result["impact_score"] / 100,
        affected_symbols=result["affected_symbols"],
        classification_confidence=result["confidence"] / 100,
        reasoning=result["reasoning"],
    )
```

#### 2.4.4 Fake News Detector

**File:** `backend/app/ai/intelligence/fake_news_detector.py`

**Purpose:** Multi-layer fake news detection

**Detection Layers:**
1. **Source Credibility** - Check source reputation score
2. **Cross-Reference** - Verify with other sources
3. **Temporal Consistency** - Check for contradictions over time
4. **LLM Verification** - Use Ollama for fact-checking

**Example:**
```python
async def detect_fake_news(
    event: AIRawEvent,
    classification: AIEventClassification,
) -> AIFakeNewsFlag:
    """Multi-layer fake news detection"""
    
    # Layer 1: Source credibility
    source_score = await get_source_credibility(event.source_name)
    
    # Layer 2: Cross-reference
    similar_events = await find_similar_events(event.content_hash)
    cross_ref_score = len(similar_events) / 3  # Normalize
    
    # Layer 3: LLM verification
    llm_score = await verify_with_llm(event.raw_content)
    
    # Aggregate scores
    detection_layers = {
        "source_credibility": source_score,
        "cross_reference": cross_ref_score,
        "llm_verification": llm_score,
    }
    
    avg_score = sum(detection_layers.values()) / len(detection_layers)
    
    if avg_score > 0.7:
        flag_status = "verified"
    elif avg_score > 0.4:
        flag_status = "uncertain"
    else:
        flag_status = "flagged"
    
    return AIFakeNewsFlag(
        event_classification_id=classification.id,
        flag_status=flag_status,
        detection_layers=detection_layers,
        reasoning=f"Aggregate score: {avg_score:.2f}",
    )
```

### 2.5 API Endpoints

#### 2.5.1 Raw Events

**Endpoint:** `GET /api/v1/ingestion/events/raw`

**Query Parameters:**
- `limit` (int, default=20) - Max events to return

**Response:**
```json
[
  {
    "event_id": 123,
    "source_type": "rss",
    "source_name": "Economic Times Markets",
    "source_url": "https://...",
    "content_preview": "Reliance Industries reports...",
    "timestamp": "2024-04-16T10:30:00Z"
  }
]
```

#### 2.5.2 Processed Events

**Endpoint:** `GET /api/v1/ingestion/events/processed`

**Query Parameters:**
- `limit` (int, default=20) - Max events to return

**Response:**
```json
[
  {
    "event_id": 456,
    "raw_event_id": 123,
    "processing_status": "completed",
    "timestamp": "2024-04-16T10:31:00Z"
  }
]
```

---

## 3. Worker Process Architecture

### 3.1 Overview

The worker process (`backend/app/worker.py`) orchestrates 6 concurrent background loops:

1. **RSS Ingestion** - Fetch RSS feeds every 5-15 minutes
2. **Event Processing** - Process raw events into trading signals
3. **Regime Detection** - Detect market regime changes
4. **Drift Detection** - Monitor ML model drift
5. **Safety Monitoring** - Check safety triggers
6. **Heartbeat** - Health check every 30s

### 3.2 Lifecycle Management

**Startup:**
```python
async def main() -> None:
    """Main worker orchestration"""
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    async with worker_lifespan() as (session_factory, redis_client, ml_components):
        # Create 6 background tasks
        tasks = [
            asyncio.create_task(rss_ingestion_loop(session_factory)),
            asyncio.create_task(event_processing_loop(session_factory)),
            asyncio.create_task(regime_detection_loop(session_factory)),
            asyncio.create_task(drift_detection_loop(session_factory)),
            asyncio.create_task(safety_monitoring_loop(session_factory)),
            asyncio.create_task(heartbeat_loop()),
        ]
        
        # Wait for shutdown signal
        await shutdown_event.wait()
        
        # Cancel all tasks
        for task in tasks:
            task.cancel()
        
        # Wait for graceful shutdown (30s timeout)
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=settings.WORKER_SHUTDOWN_TIMEOUT
        )
```

**Graceful Shutdown:**
1. Receive SIGTERM/SIGINT signal
2. Set `shutdown_event` flag
3. Cancel all background tasks
4. Wait up to 30s for tasks to complete
5. Force kill remaining tasks
6. Close database connections
7. Close Redis connections

### 3.3 Resource Management

**Database:**
- Separate connection pool from API (10 connections)
- `pool_pre_ping=True` for connection health checks
- Automatic reconnection on failure

**Redis:**
- Shared Redis client for caching and pub/sub
- Connection pooling (50 max connections)
- Automatic reconnection with exponential backoff

**ML Models:**
- Loaded once at startup
- Shared across all loops
- ONNX format for CPU inference

### 3.4 Error Handling

**Per-Loop Error Handling:**
```python
async def rss_ingestion_loop(session_factory):
    try:
        while True:
            try:
                # Main loop logic
                ...
            except Exception as e:
                logger.error(f"RSS ingestion error: {e}", exc_info=True)
                # Continue loop, don't crash
            
            await asyncio.sleep(sleep_time)
    
    except asyncio.CancelledError:
        logger.info("RSS ingestion loop cancelled")
        raise  # Propagate cancellation
    finally:
        # Cleanup resources
        await cleanup()
```

**Global Error Handling:**
- Uncaught exceptions are logged with full traceback
- Worker process exits with non-zero code
- Systemd/Docker restarts worker automatically

### 3.5 Monitoring

**Heartbeat:**
- Writes timestamp to Redis every 30s
- Key: `worker:heartbeat`
- TTL: 60s
- External monitoring checks for stale heartbeat

**Metrics:**
- RSS ingestion: events/cycle, errors/cycle
- Event processing: events/minute, latency
- Drift detection: drift score, accuracy drop
- Safety monitoring: triggers/hour

**Logging:**
```
2024-04-16 10:30:00 [INFO] RSS ingestion cycle complete: 25 events, 0 errors
2024-04-16 10:30:10 [INFO] Event processing: 5 events processed, 2 signals generated
2024-04-16 10:30:20 [INFO] Regime detection: trending regime detected (confidence: 0.85)
2024-04-16 10:30:30 [DEBUG] Heartbeat: 2024-04-16T10:30:30Z
```

---

## 4. Configuration Reference

### 4.1 Environment Variables

**Database:**
```bash
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5433/cortex_db
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=20
DB_POOL_TIMEOUT=30
DB_POOL_RECYCLE=1800
```

**Redis:**
```bash
REDIS_URL=redis://localhost:6379/0
REDIS_MAX_CONNECTIONS=50
```

**Upstox:**
```bash
UPSTOX_API_KEY=your-api-key
UPSTOX_API_SECRET=your-api-secret
UPSTOX_ACCESS_TOKEN=your-access-token
UPSTOX_BASE_URL=https://api.upstox.com/v3
UPSTOX_WS_URL=wss://api.upstox.com/v2/feed/market-data-feed
```

**RSS Ingestion:**
```bash
RSS_MIN_POLL_SECONDS=300      # 5 minutes
RSS_MAX_POLL_SECONDS=900      # 15 minutes
RSS_POLL_JITTER_SECONDS=60    # 0-60s random jitter
RSS_CONCURRENCY=5             # Max concurrent feeds
```

**Ollama:**
```bash
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=llama3.1:8b
OLLAMA_TIMEOUT=30
```

**Worker:**
```bash
ENABLE_BACKGROUND_TASKS=true
WORKER_SHUTDOWN_TIMEOUT=30
```

**Ingestion:**
```bash
BULK_INSERT_BATCH_SIZE=1000
```

### 4.2 Settings Class

**File:** `backend/app/core/config.py`

```python
class Settings(BaseSettings):
    # Database
    DATABASE_URL: PostgresDsn
    DB_POOL_SIZE: int = 10
    
    # Redis
    REDIS_URL: RedisDsn
    REDIS_MAX_CONNECTIONS: int = 50
    
    # Upstox
    UPSTOX_API_KEY: str
    UPSTOX_API_SECRET: str
    UPSTOX_ACCESS_TOKEN: str | None = None
    
    # RSS
    RSS_MIN_POLL_SECONDS: int = 300
    RSS_MAX_POLL_SECONDS: int = 900
    RSS_POLL_JITTER_SECONDS: int = 60
    
    # Ollama
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.1:8b"
    
    # Worker
    ENABLE_BACKGROUND_TASKS: bool = True
    WORKER_SHUTDOWN_TIMEOUT: int = 30
    
    # Ingestion
    BULK_INSERT_BATCH_SIZE: int = 1000
```

---

## 5. Testing & Validation

### 5.1 Manual Testing

**Test RSS Ingestion:**
```bash
# Check raw events
curl http://localhost:8000/api/v1/ingestion/events/raw?limit=20 \
  -H "Authorization: Bearer $TOKEN"

# Check processed events
curl http://localhost:8000/api/v1/ingestion/events/processed?limit=20 \
  -H "Authorization: Bearer $TOKEN"
```

**Test OHLCV Ingestion:**
```bash
# Ingest historical candles
curl -X POST http://localhost:8000/api/v1/upstox/ingest \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["NSE_EQ|INE002A01018"],
    "timeframe": "1d",
    "from_date": "2024-01-01",
    "to_date": "2024-12-31"
  }'
```

**Test WebSocket Stream:**
```bash
# Start stream
curl -X POST http://localhost:8000/api/v1/upstox/stream/start \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["NSE_EQ|INE002A01018"]
  }'

# Check status
curl http://localhost:8000/api/v1/upstox/stream/status \
  -H "Authorization: Bearer $TOKEN"
```

### 5.2 Worker Logs

**Check worker logs:**
```bash
# View worker logs
tail -f backend/logs/worker.log

# Expected output:
# INFO: RSS ingestion loop started
# INFO: Event processing loop started
# INFO: Regime detection loop started
# INFO: Drift detection loop started
# INFO: Safety monitoring loop started
# INFO: Heartbeat loop started
# INFO: RSS ingestion cycle complete: 25 events, 0 errors
```

### 5.3 Database Validation

**Check ingested data:**
```sql
-- Raw events
SELECT COUNT(*) FROM ai_raw_events;
SELECT source_name, COUNT(*) FROM ai_raw_events GROUP BY source_name;

-- Processed events
SELECT COUNT(*) FROM ai_processed_events;
SELECT processing_status, COUNT(*) FROM ai_processed_events GROUP BY processing_status;

-- OHLCV data
SELECT COUNT(*) FROM stock_ohlcv;
SELECT symbol, timeframe, COUNT(*) FROM stock_ohlcv GROUP BY symbol, timeframe;

-- Ticks
SELECT COUNT(*) FROM upstox_ticks;
SELECT instrument_key, COUNT(*) FROM upstox_ticks GROUP BY instrument_key;
```

---

## 6. Troubleshooting

### 6.1 Common Issues

**Issue: No RSS events ingested**

**Symptoms:**
- `ai_raw_events` table is empty
- Worker logs show "RSS ingestion cycle complete: 0 events, 0 errors"

**Causes:**
1. RSS feeds are down
2. Network connectivity issues
3. Content hashing collision (unlikely)

**Solutions:**
```bash
# Check RSS feed manually
curl -v https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms

# Check worker logs
tail -f backend/logs/worker.log | grep "RSS"

# Check database
psql -d cortex_db -c "SELECT COUNT(*) FROM ai_raw_events;"
```

**Issue: OHLCV ingestion fails**

**Symptoms:**
- API returns 500 error
- Logs show "OHLCV bulk upsert failed"

**Causes:**
1. Invalid instrument key
2. Invalid date format
3. Upstox API rate limit exceeded
4. Database connection failure

**Solutions:**
```bash
# Check Upstox API status
curl -H "Authorization: Bearer $UPSTOX_ACCESS_TOKEN" \
  https://api.upstox.com/v3/user/profile

# Check database connection
psql -d cortex_db -c "SELECT 1;"

# Check rate limits
curl http://localhost:8000/api/v1/upstox/stream/status \
  -H "Authorization: Bearer $TOKEN"
```

**Issue: Worker process crashes**

**Symptoms:**
- Worker process exits unexpectedly
- Logs show "Worker crashed: ..."

**Causes:**
1. Uncaught exception in background loop
2. Database connection failure
3. Redis connection failure
4. Out of memory

**Solutions:**
```bash
# Check worker logs
tail -100 backend/logs/worker.log

# Check system resources
free -h
df -h

# Restart worker
systemctl restart cortex-worker
```

### 6.2 Performance Tuning

**Optimize RSS ingestion:**
```bash
# Increase polling interval (reduce load)
RSS_MIN_POLL_SECONDS=600
RSS_MAX_POLL_SECONDS=1800

# Reduce concurrency (reduce memory)
RSS_CONCURRENCY=3
```

**Optimize OHLCV ingestion:**
```bash
# Increase batch size (faster ingestion)
BULK_INSERT_BATCH_SIZE=2000

# Increase database pool (more connections)
DB_POOL_SIZE=20
DB_MAX_OVERFLOW=40
```

**Optimize event processing:**
```bash
# Increase Ollama timeout (slower but more reliable)
OLLAMA_TIMEOUT=60

# Reduce event processing batch size
EVENT_PROCESSING_BATCH_SIZE=5
```

---

## 7. Next Steps & Recommendations

### 7.1 Immediate Actions

1. **Verify RSS Ingestion**
   - Check `ai_raw_events` table for recent entries
   - Monitor worker logs for ingestion cycles
   - Validate all 5 RSS feeds are accessible

2. **Test OHLCV Ingestion**
   - Ingest sample data for 1-2 symbols
   - Verify data in `stock_ohlcv` table
   - Check for duplicates and data quality

3. **Monitor Worker Health**
   - Check heartbeat in Redis (`worker:heartbeat`)
   - Monitor CPU/memory usage
   - Review error logs

### 7.2 Future Enhancements

1. **RSS Ingestion**
   - Add more RSS sources (Bloomberg, CNBC, etc.)
   - Implement RSS feed health monitoring
   - Add retry logic for failed feeds
   - Support for non-English feeds

2. **OHLCV Ingestion**
   - Add support for multiple data sources (Yahoo Finance, Alpha Vantage)
   - Implement data quality checks (missing bars, outliers)
   - Add automatic gap filling
   - Support for intraday data (1min, 5min)

3. **Event Processing**
   - Improve LLM prompts for better classification
   - Add sentiment analysis for trading signals
   - Implement event correlation (related events)
   - Add backtesting for signal validation

4. **Monitoring**
   - Add Prometheus metrics
   - Implement alerting (PagerDuty, Slack)
   - Add performance dashboards (Grafana)
   - Implement distributed tracing (Jaeger)

### 7.3 Documentation Updates

1. **API Documentation**
   - Add OpenAPI/Swagger specs
   - Add example requests/responses
   - Document error codes

2. **Deployment Guide**
   - Add Docker Compose setup
   - Add Kubernetes manifests
   - Document infrastructure requirements

3. **Developer Guide**
   - Add contribution guidelines
   - Document code style
   - Add testing guidelines

---

## 8. Key Files Reference

### 8.1 Market Data Ingestion

| File | Purpose | LOC |
|------|---------|-----|
| `backend/app/services/upstox_client.py` | Upstox API client | 150 |
| `backend/app/services/data_ingestion.py` | OHLCV bulk ingestion | 160 |
| `backend/app/services/tick_stream.py` | WebSocket tick stream | 120 |
| `backend/app/models/upstox_data.py` | Database models | 160 |
| `backend/app/api/v1/upstox.py` | API endpoints | 200 |
| `backend/app/schemas/upstox.py` | Pydantic schemas | 30 |

### 8.2 RSS Ingestion

| File | Purpose | LOC |
|------|---------|-----|
| `backend/app/ai/ingestion/rss_fetcher.py` | RSS feed fetcher | 190 |
| `backend/app/ai/intelligence/event_processor.py` | Event processing loop | 250 |
| `backend/app/ai/intelligence/nlp_engine.py` | NLP entity extraction | 180 |
| `backend/app/ai/intelligence/event_classifier.py` | Event classification | 200 |
| `backend/app/ai/intelligence/fake_news_detector.py` | Fake news detection | 150 |
| `backend/app/ai/fusion/models.py` | Database models | 300 |
| `backend/app/api/v1/ingestion.py` | API endpoints | 75 |

### 8.3 Worker Process

| File | Purpose | LOC |
|------|---------|-----|
| `backend/app/worker.py` | Worker orchestration | 250 |
| `backend/app/core/config.py` | Configuration | 200 |
| `backend/app/core/database.py` | Database setup | 100 |
| `backend/app/core/redis.py` | Redis setup | 80 |

---

## 9. Glossary

**OHLCV** - Open, High, Low, Close, Volume - Standard format for candlestick data

**Hypertable** - TimescaleDB's time-series optimized table with automatic partitioning

**Tick** - Single price update from exchange (last traded price)

**Instrument Key** - Unique identifier for tradable security (e.g., `NSE_EQ|INE002A01018`)

**Timeframe** - Candle interval (1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w)

**Content Hash** - SHA-256 hash of content for deduplication

**Jitter** - Random delay added to polling interval to avoid thundering herd

**Ensemble Predictor** - ML model combining multiple models (XGBoost + GRU)

**Regime Detection** - Identifying market conditions (trending, ranging, volatile)

**Drift Detection** - Monitoring ML model performance degradation

**Safety Trigger** - Automated circuit breaker for risk management

**LLM** - Large Language Model (Ollama Llama 3.1 8B)

**NLP** - Natural Language Processing

**RBAC** - Role-Based Access Control

**JWT** - JSON Web Token for authentication

---

## 10. Contact & Support

**Project Repository:** [Internal GitLab]  
**Documentation:** `/home/preet/code/Cortex_Merge_AI-ML/documentation/`  
**API Docs:** http://localhost:8000/docs (development only)

**Key Contacts:**
- Backend Team: backend-team@cortex.ai
- ML Team: ml-team@cortex.ai
- DevOps Team: devops-team@cortex.ai

---

**End of Report**
