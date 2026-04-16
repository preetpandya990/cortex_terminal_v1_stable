# Cortex AI — Developer Guide
## How to Change Code & How to Test the System

**Version:** Post-Remediation · April 2026  
**Stack:** FastAPI 0.115 · Python 3.12 · PostgreSQL 16 · Redis 7 · Next.js 15 · TypeScript

---

## Table of Contents

1. [Complete File Structure](#1-complete-file-structure)
2. [First-Time Setup](#2-first-time-setup)
3. [How the Code is Organised](#3-how-the-code-is-organised)
4. [How to Change the Code](#4-how-to-change-the-code)
   - 4.1 Adding a new API endpoint
   - 4.2 Adding a new technical indicator
   - 4.3 Adding a new database table
   - 4.4 Changing configuration or secrets
   - 4.5 Adding a new exception type
   - 4.6 Changing rate limits or cache TTLs
   - 4.7 Modifying the scanner signals
   - 4.8 Adding a new frontend page or component
5. [How to Run the Tests](#5-how-to-run-the-tests)
   - 5.1 Running the full test suite
   - 5.2 Running specific test categories
   - 5.3 Running a single test
   - 5.4 Running with coverage report
   - 5.5 What each test file covers
6. [How to Test Manually (End-to-End)](#6-how-to-test-manually-end-to-end)
   - 6.1 Starting the full stack
   - 6.2 Testing the health check
   - 6.3 Testing authentication flow
   - 6.4 Testing the scanner
   - 6.5 Testing the HawkEye radar
   - 6.6 Testing live market data
   - 6.7 Testing tick stream WebSocket
   - 6.8 Testing candle ingestion
7. [Code Quality Checks](#7-code-quality-checks)
8. [Database Operations](#8-database-operations)
9. [Debugging Common Issues](#9-debugging-common-issues)
10. [Rules That Must Never Be Broken](#10-rules-that-must-never-be-broken)

---

## 1. Complete File Structure

```
Cortex_Merge_AI-ML/
│
├── docker-compose.yml              ← Start the full stack (db + redis + api + frontend)
│
├── backend/
│   ├── .env.example                ← Copy to .env and fill in your values
│   ├── .env                        ← Your secrets (never commit this)
│   ├── Dockerfile                  ← Multi-stage production image
│   ├── alembic.ini                 ← Alembic configuration
│   ├── pytest.ini                  ← Test runner configuration
│   ├── ruff.toml                   ← Linter + formatter configuration
│   ├── requirements.txt            ← Production dependencies
│   ├── requirements-dev.txt        ← Dev/test dependencies
│   │
│   ├── alembic/
│   │   ├── env.py                  ← Alembic uses async engine from config
│   │   └── versions/
│   │       └── 0001_initial.py     ← Baseline schema migration
│   │
│   ├── app/
│   │   ├── main.py                 ← App factory, lifespan, middleware, router registration
│   │   ├── exceptions.py           ← All typed exception classes
│   │   │
│   │   ├── core/                   ← Infrastructure singletons (never business logic)
│   │   │   ├── config.py           ← All settings via pydantic-settings (.env)
│   │   │   ├── database.py         ← Async engine + get_db() dependency
│   │   │   ├── redis.py            ← Redis pool + CacheService
│   │   │   ├── security.py         ← JWT create/decode + get_current_user_id dependency
│   │   │   ├── limiter.py          ← SlowAPI rate limiter singleton
│   │   │   └── exception_handlers.py ← Unified error response shapes
│   │   │
│   │   ├── api/
│   │   │   └── v1/                 ← All HTTP route handlers live here
│   │   │       ├── auth.py         ← /api/v1/auth/* (login, callback, logout)
│   │   │       ├── market_data.py  ← /api/v1/market-data/* (live price, search)
│   │   │       ├── scanner.py      ← /api/v1/scanner/* (run scan)
│   │   │       ├── upstox.py       ← /api/v1/upstox/* (stream, ingest)
│   │   │       └── hawk_eye.py     ← /api/v1/hawk-eye/* (multi-timeframe scan)
│   │   │
│   │   ├── models/                 ← SQLAlchemy ORM table definitions
│   │   │   └── upstox_data.py      ← UpstoxOHLCV, UpstoxTick, InstrumentMaster
│   │   │
│   │   ├── schemas/                ← Pydantic request/response shapes
│   │   │   ├── scanner.py          ← ScanResult, ScanResponse, ScanSignal
│   │   │   ├── hawk_eye.py         ← HawkEyeResult, HawkEyeResponse, HawkEyeSignal
│   │   │   ├── market_data.py      ← LivePriceResponse, InstrumentSearchResult
│   │   │   └── upstox.py           ← IngestRequest, StreamStatusResponse
│   │   │
│   │   └── services/               ← All business logic. No HTTP, no direct DB in routers.
│   │       ├── base_websocket.py   ← Abstract WebSocket lifecycle (connect/reconnect/TLS)
│   │       ├── upstox_client.py    ← Shared httpx pool + Upstox API calls
│   │       ├── tick_stream.py      ← Real-time tick fan-out (extends BaseWebSocketService)
│   │       ├── data_ingestion.py   ← Bulk OHLCV upsert (batch insert, no per-row loops)
│   │       ├── market_scanner.py   ← Single-query scanner + Redis cache
│   │       ├── hawk_eye.py         ← Multi-timeframe signal aggregation
│   │       ├── indicators.py       ← RSI, MACD, Bollinger, ATR, VWAP (pure functions)
│   │       └── upstox_persistence.py ← DB search + keyset pagination
│   │
│   └── tests/
│       ├── conftest.py             ← Shared fixtures (test DB, mock Redis, async client)
│       ├── unit/
│       │   ├── test_indicators.py  ← RSI, MACD, BB, ATR, VWAP (pure function tests)
│       │   ├── test_security.py    ← JWT lifecycle + config validator tests
│       │   └── test_hawk_eye.py    ← Multi-timeframe scoring tests
│       ├── api/
│       │   └── test_contracts.py   ← HTTP contract tests (all 5 routers)
│       └── integration/
│           └── test_db_operations.py ← Bulk upsert, instrument sync, scanner query
│
└── frontend/
    └── src/
        ├── app/
        │   ├── page.tsx                    ← Dashboard home
        │   ├── scanner/page.tsx            ← Scanner page
        │   ├── stocks/[symbol]/page.tsx    ← Stock detail page
        │   └── hawk-eye-radar/page.tsx     ← HawkEye multi-timeframe page ← NEW
        │
        ├── components/
        │   ├── charts/
        │   │   ├── CandlestickChart.tsx    ← Uses useChart() hook ← CHANGED
        │   │   └── PriceChart.tsx          ← Uses useChart() hook ← CHANGED
        │   ├── dashboard/
        │   │   └── DashboardMarketPanel.tsx ← Back-off polling + AbortController ← CHANGED
        │   ├── scanner/
        │   │   ├── ScanResults.tsx
        │   │   └── StockCard.tsx
        │   └── ml/
        │       ├── PredictionBadge.tsx
        │       └── PredictionPanel.tsx
        │
        ├── hooks/
        │   └── useChart.ts             ← Shared chart lifecycle hook ← NEW
        │
        └── types/
            ├── market.ts
            ├── ml.ts
            ├── upstox.ts
            └── hawk_eye.ts             ← HawkEyeResult, HawkEyeResponse types ← NEW
```

---

## 2. First-Time Setup

### Step 1 — Copy and fill in the `.env` file

```bash
cd backend
cp .env.example .env
```

Open `.env` and fill in every value. The three that are mandatory:

```bash
# Generate this with:
openssl rand -hex 32
# Paste the output as SECRET_KEY in .env

# Get these from https://developer.upstox.com
UPSTOX_API_KEY=your_actual_key
UPSTOX_API_SECRET=your_actual_secret
```

The app **refuses to start** if `SECRET_KEY` is missing or a placeholder, `DATABASE_URL` is absent, `REDIS_URL` is absent, or `CORS_ALLOWED_ORIGINS` is the wildcard `["*"]`. This is intentional.

### Step 2 — Install Python dependencies

```bash
cd backend

# Create a virtual environment (recommended)
python3.11.15 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install production + dev dependencies
pip install -r requirements.txt -r requirements-dev.txt
```

### Step 3 — Start infrastructure (PostgreSQL + Redis)

```bash
# From the project root (where docker-compose.yml is)
docker compose up db redis -d

# Verify both are healthy
docker compose ps
```

Expected output:
```
NAME             STATUS
cortex-db-1      running (healthy)
cortex-redis-1   running (healthy)
```

### Step 4 — Run database migrations

```bash
cd backend
alembic upgrade head
```

This creates the three tables: `upstox_ohlcv`, `upstox_ticks`, `instrument_master`.

### Step 5 — Start the API

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Visit `http://localhost:8000/health` — you should see:
```json
{"status": "ok", "version": "1.0.0"}
```

Visit `http://localhost:8000/docs` to see the interactive API docs (only available when `ENVIRONMENT=development`).

### Step 6 — Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Visit `http://localhost:3000`.

---

## 3. How the Code is Organised

There is one rule that everything else follows: **routers only handle HTTP, services own all logic, and the core layer owns infrastructure.**

```
HTTP Request
    │
    ▼
api/v1/scanner.py          ← validates input, calls service, returns response
    │
    ▼
services/market_scanner.py ← ALL business logic lives here (no HTTP, no direct SQL)
    │
    ├── core/database.py   ← AsyncSession from the DB pool
    ├── core/redis.py      ← CacheService for get/set
    └── services/indicators.py ← pure functions, no I/O
```

**Where things live and why:**

| Thing | Where | Why |
|---|---|---|
| Environment variables | `core/config.py` | Single source of truth, fail-fast on startup |
| HTTP route handlers | `api/v1/*.py` | Only auth, validation, call service, return schema |
| Business logic | `services/*.py` | Testable, no HTTP dependencies |
| DB table definitions | `models/upstox_data.py` | ORM only, no logic |
| Request/response shapes | `schemas/*.py` | Pydantic v2, typed I/O contracts |
| Error types | `exceptions.py` | Typed hierarchy, maps to HTTP status codes |
| Long-lived singletons | `app.state` via `main.py` lifespan | Created once, shared across all requests |

---

## 4. How to Change the Code

### 4.1 Adding a New API Endpoint

**Example:** Add `GET /api/v1/market-data/ohlcv/{instrument_key}`

**Step 1 — Add the schema** in `app/schemas/market_data.py`:
```python
class OHLCVResponse(BaseModel):
    instrument_key: str
    timeframe: str
    candles: list[dict]
    source: Literal["db", "api"]
```

**Step 2 — Add the service logic** in `app/services/` (new file or existing):
```python
async def get_ohlcv(
    session: AsyncSession,
    instrument_key: str,
    timeframe: str,
    limit: int,
) -> list[UpstoxOHLCV]:
    stmt = (
        select(UpstoxOHLCV)
        .where(
            UpstoxOHLCV.instrument_key == instrument_key,
            UpstoxOHLCV.timeframe == timeframe,
        )
        .order_by(UpstoxOHLCV.timestamp.desc())
        .limit(limit)
    )
    return (await session.execute(stmt)).scalars().all()
```

**Step 3 — Add the route** in `app/api/v1/market_data.py`:
```python
@router.get("/ohlcv/{instrument_key}", response_model=OHLCVResponse)
@limiter.limit(settings.RATE_LIMIT_MARKET_DATA)
async def get_ohlcv_data(
    request: Request,
    instrument_key: str,
    user_id: str = Depends(get_current_user_id),  # ← auth required — never omit this
    timeframe: str = Query(default="1d", pattern=r"^(1m|5m|15m|30m|1h|4h|1d|1w)$"),
    limit: int = Query(default=100, ge=1, le=1000),
    session: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
) -> OHLCVResponse:
    # ... implementation
```

**Step 4 — Register the router** (only if it's in a new file):
```python
# In app/main.py, inside create_app():
app.include_router(your_new_router, prefix="/api/v1/your-prefix", tags=["YourTag"])
```

**Step 5 — Write a contract test** in `tests/api/test_contracts.py`:
```python
@pytest.mark.asyncio
async def test_ohlcv_requires_auth(self) -> None:
    # test without token returns 401

@pytest.mark.asyncio
async def test_ohlcv_invalid_timeframe_rejected(self, async_client) -> None:
    response = await async_client.get("/api/v1/market-data/ohlcv/NSE_EQ|TEST?timeframe=2d")
    assert response.status_code == 422

@pytest.mark.asyncio
async def test_ohlcv_returns_correct_shape(self, async_client) -> None:
    # test with mocked service returns expected schema fields
```

---

### 4.2 Adding a New Technical Indicator

All indicators live in `app/services/indicators.py` as methods on the `TechnicalIndicators` class. They are **pure functions** — they take lists of floats and return lists of floats. No DB, no Redis, no HTTP.

**Example:** Add Stochastic Oscillator `%K`

**Step 1 — Add the method** in `indicators.py`:
```python
@staticmethod
def stochastic_k(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> list[float | None]:
    """
    Stochastic %K: (close - lowest_low) / (highest_high - lowest_low) × 100
    Returns None for the first period-1 values.
    """
    n = len(closes)
    if n < period:
        return [None] * n

    result: list[float | None] = [None] * (period - 1)
    for i in range(period - 1, n):
        window_h = max(highs[i - period + 1 : i + 1])
        window_l = min(lows[i - period + 1 : i + 1])
        if window_h == window_l:
            result.append(50.0)  # flat market
        else:
            result.append((closes[i] - window_l) / (window_h - window_l) * 100)

    return result
```

**Step 2 — Write the unit tests** in `tests/unit/test_indicators.py`:
```python
class TestStochasticK:
    def test_returns_none_for_insufficient_data(self, ind):
        result = ind.stochastic_k([1.0]*5, [0.9]*5, [1.0]*5, period=14)
        assert all(v is None for v in result)

    def test_result_in_range_0_to_100(self, ind):
        highs  = [float(10 + i) for i in range(30)]
        lows   = [float(8 + i)  for i in range(30)]
        closes = [float(9 + i)  for i in range(30)]
        result = ind.stochastic_k(highs, lows, closes)
        defined = [v for v in result if v is not None]
        assert all(0.0 <= v <= 100.0 for v in defined)

    def test_result_length_matches_input(self, ind):
        n = 30
        result = ind.stochastic_k([10.0]*n, [9.0]*n, [9.5]*n)
        assert len(result) == n
```

**Step 3 — Use it in the scanner** in `services/market_scanner.py` inside `_score_instrument()`:
```python
stoch = self._indicators.stochastic_k(highs, lows, closes)
if stoch:
    defined = [v for v in stoch if v is not None]
    if defined and defined[-1] < 20:
        signals.append(ScanSignal(name="STOCH_OVERSOLD", value=defined[-1], direction="buy"))
        buy_score += 1
```

---

### 4.3 Adding a New Database Table

**Step 1 — Define the model** in `app/models/upstox_data.py` (or a new file in `models/`):
```python
class UserWatchlist(Base):
    __tablename__ = "user_watchlist"
    __table_args__ = (
        UniqueConstraint("user_id", "instrument_key", name="uq_watchlist_user_instrument"),
        Index("idx_watchlist_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    instrument_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

> **Critical rules:**  
> - Always use `server_default=func.now()` for timestamps — never `default=datetime.utcnow`  
> - Always add a `UniqueConstraint` for any composite uniqueness requirement  
> - Always add an `Index` for columns that will appear in `WHERE` clauses  

**Step 2 — Import it in `alembic/env.py`** so autogenerate can see it:
```python
import app.models.upstox_data   # existing
import app.models.your_new_file  # add this line
```

**Step 3 — Generate and apply the migration:**
```bash
cd backend
alembic revision --autogenerate -m "add_user_watchlist"
# Review the generated file in alembic/versions/
alembic upgrade head
```

**Step 4 — Verify the migration:**
```bash
alembic current        # shows current revision
alembic history        # shows all migrations
```

---

### 4.4 Changing Configuration or Secrets

All configuration lives in `app/core/config.py` and is read from the `.env` file at startup via `pydantic-settings`.

**To add a new config variable:**
```python
# In app/core/config.py, inside the Settings class:
MY_NEW_SETTING: int = Field(default=100, ge=1, le=10000)

# For a required secret (no default):
MY_NEW_SECRET: str = Field(..., description="What this is for")
```

**To read it anywhere in the app:**
```python
from app.core.config import get_settings
settings = get_settings()
print(settings.MY_NEW_SETTING)
```

**To add it to `.env`:**
```bash
# In .env
MY_NEW_SETTING=500
```

**To document it for teammates**, add it to `.env.example` with a comment.

> **Never** set a default value for anything that is a secret (keys, passwords, tokens). Use `Field(...)` — the three dots mean "required, no default".

---

### 4.5 Adding a New Exception Type

All exceptions live in `app/exceptions.py`. Every exception maps to an HTTP status code automatically via the exception handler in `app/core/exception_handlers.py`.

**Example:**
```python
# In app/exceptions.py
class InsufficientDataError(CortexBaseError):
    default_message = "Insufficient historical data for this instrument"
    status_code = 422
```

**Use it in a service:**
```python
from app.exceptions import InsufficientDataError

if len(candles) < 20:
    raise InsufficientDataError("Need at least 20 candles for signal calculation")
```

The exception handler will automatically return:
```json
{
  "error": {
    "message": "Need at least 20 candles for signal calculation",
    "status_code": 422,
    "correlation_id": "uuid-here"
  }
}
```

No `try/except` needed in the router. Never use `HTTPException` directly — always raise a typed domain exception.

---

### 4.6 Changing Rate Limits or Cache TTLs

**Rate limits** are defined in `.env` and read via `settings`:
```bash
# In .env — format is "N/period" where period is second/minute/hour/day
RATE_LIMIT_SCANNER=10/minute
RATE_LIMIT_SEARCH=60/minute
RATE_LIMIT_MARKET_DATA=120/minute
```

Applied in routers via:
```python
@limiter.limit(settings.RATE_LIMIT_SCANNER)
```

To add a rate limit to a new endpoint:
```python
from app.core.limiter import limiter
from app.core.config import get_settings
settings = get_settings()

@router.get("/my-endpoint")
@limiter.limit("30/minute")          # hard-coded, or
@limiter.limit(settings.RATE_LIMIT_SCANNER)  # from .env
async def my_endpoint(request: Request, ...):
```

> The `request: Request` parameter must be present — SlowAPI requires it.

**Cache TTLs** are also in `.env`:
```bash
CACHE_TTL_SCANNER=60        # seconds
CACHE_TTL_INSTRUMENTS=300
CACHE_TTL_LIVE_QUOTE=1
CACHE_TTL_MARKET_STATUS=30
```

Used in services:
```python
await cache.set(cache_key, data, ttl=settings.CACHE_TTL_SCANNER)
```

---

### 4.7 Modifying the Scanner Signals

Scanner signal logic lives in `app/services/market_scanner.py` inside `_score_instrument()`.

To add a new signal:
```python
# Inside _score_instrument() in market_scanner.py

# 1. Calculate the indicator
atr_vals = self._indicators.atr(highs, lows, closes)
defined_atr = [v for v in atr_vals if v is not None]

# 2. Apply your signal logic
if defined_atr and closes[-1] > 0:
    atr_pct = defined_atr[-1] / closes[-1] * 100
    if atr_pct > 3.0:   # high volatility
        signals.append(ScanSignal(
            name="HIGH_VOLATILITY",
            value=round(atr_pct, 2),
            direction="neutral",
        ))
        # adjust buy_score or sell_score as appropriate
```

To change scoring weights, modify the `buy_score` / `sell_score` integers next to each signal. Higher numbers = more influence on the composite direction.

After any change to scoring logic, run the scanner unit tests:
```bash
pytest tests/unit/test_hawk_eye.py -v
pytest tests/integration/test_db_operations.py::TestScannerService -v
```

---

### 4.8 Adding a New Frontend Page or Component

**New page:**
```
frontend/src/app/your-page/page.tsx
```

The page is automatically available at `http://localhost:3000/your-page` (Next.js App Router).

**New chart component** — always use the `useChart` hook:
```typescript
// frontend/src/components/charts/MyNewChart.tsx
"use client";

import { useEffect, useRef } from "react";
import { ISeriesApi, UTCTimestamp } from "lightweight-charts";
import { useChart } from "@/hooks/useChart";  // ← always use this

export function MyNewChart({ data }: { data: MyDataType[] }) {
  const { chartRef, containerRef } = useChart();
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);

  useEffect(() => {
    if (!chartRef.current || seriesRef.current) return;
    seriesRef.current = chartRef.current.addLineSeries({ color: "#6366f1" });
  }, [chartRef.current]);

  useEffect(() => {
    if (!seriesRef.current || !data.length) return;
    seriesRef.current.setData(
      data.map(d => ({
        time: (new Date(d.timestamp).getTime() / 1000) as UTCTimestamp,
        value: d.value,
      })).sort((a, b) => (a.time as number) - (b.time as number))
    );
  }, [data]);

  return <div ref={containerRef} style={{ width: "100%", height: 300 }} />;
}
```

**New type** — add to `frontend/src/types/`:
```typescript
// frontend/src/types/my_feature.ts
export interface MyFeatureResult {
  instrument_key: string;
  value: number;
  // ...
}
```

**Fetching data** — always handle loading, error, and empty states:
```typescript
const [data, setData] = useState<MyFeatureResult | null>(null);
const [loading, setLoading] = useState(true);
const [error, setError] = useState<string | null>(null);

useEffect(() => {
  fetch("/api/v1/my-endpoint")
    .then(r => r.ok ? r.json() : Promise.reject(r))
    .then(setData)
    .catch(() => setError("Failed to load"))
    .finally(() => setLoading(false));
}, []);
```

---

## 5. How to Run the Tests

### 5.1 Running the Full Test Suite

```bash
cd backend
pytest
```

Expected output:
```
================= 67 passed in X.Xs =================
```

The test database is **SQLite in-memory** — no PostgreSQL or Redis needed to run tests. Everything is mocked or uses the test DB defined in `tests/conftest.py`.

### 5.2 Running Specific Test Categories

```bash
# Unit tests only (fastest — no I/O, no DB, no Redis)
pytest tests/unit/ -v

# API contract tests only
pytest tests/api/ -v

# Integration tests only (uses SQLite in-memory test DB)
pytest tests/integration/ -v

# All tests matching a marker
pytest -m unit -v
pytest -m integration -v
pytest -m api -v
```

### 5.3 Running a Single Test

```bash
# Run one specific test class
pytest tests/unit/test_indicators.py::TestRSI -v

# Run one specific test function
pytest tests/unit/test_indicators.py::TestRSI::test_all_up_prices_approach_100 -v

# Run one test file
pytest tests/unit/test_security.py -v

# Run all tests whose name contains a keyword
pytest -k "rsi" -v
pytest -k "auth" -v
pytest -k "bulk" -v
```

### 5.4 Running with Coverage Report

```bash
# Coverage report in terminal
pytest --cov=app --cov-report=term-missing

# HTML coverage report (open htmlcov/index.html in a browser)
pytest --cov=app --cov-report=html

# Fail if coverage drops below 70%
pytest --cov=app --cov-fail-under=70
```

### 5.5 What Each Test File Covers

#### `tests/unit/test_indicators.py`

Tests every indicator in `app/services/indicators.py` with known inputs and expected outputs. **No DB, no network, no mocks needed.**

| Class | What it tests |
|---|---|
| `TestRSI` | Wilder smoothing seed, 0–100 bounds, all-up → 100, all-down → 0, flat prices, empty input |
| `TestMACD` | Histogram identity (MACD − Signal = Histogram), leading Nones, custom periods |
| `TestBollingerBands` | Upper ≥ Middle ≥ Lower always, flat prices → zero bandwidth, custom std multiplier |
| `TestATR` | ATR always positive, insufficient data → all None |
| `TestVWAP` | Bounds within high/low, flat price → VWAP equals price, rolling window |

Run these whenever you touch `app/services/indicators.py`.

#### `tests/unit/test_security.py`

Tests `app/core/security.py` and `app/core/config.py`.

| Test | What it verifies |
|---|---|
| `test_access_token_decodes_correctly` | Token round-trip: create → decode → assert sub and type |
| `test_refresh_token_rejected_as_access` | Token type is enforced — access token cannot decode as refresh |
| `test_tampered_token_raises` | Changing the signature raises `InvalidTokenError` |
| `test_expired_token_raises` | Past-expiry token raises `InvalidTokenError` |
| `test_each_token_has_unique_jti` | Two tokens for same user have different JTIs |
| `TestConfigValidation::test_wildcard_cors_raises` | `["*"]` as CORS origin raises `ValidationError` on startup |
| `TestConfigValidation::test_default_secret_key_raises` | Placeholder SECRET_KEY raises `ValidationError` on startup |

Run these whenever you touch `app/core/security.py` or `app/core/config.py`.

#### `tests/unit/test_hawk_eye.py`

Tests `app/services/hawk_eye.py` scoring logic.

Run these whenever you touch `app/services/hawk_eye.py`.

#### `tests/api/test_contracts.py`

Uses `httpx.AsyncClient` to make real HTTP requests against the FastAPI app (no network — in-process ASGI). Auth is bypassed via dependency override. Redis is mocked.

| Class | What it tests |
|---|---|
| `TestAuthEnforcement` | Every protected route returns 401 when called without a Bearer token |
| `TestMarketDataEndpoints` | Search min-2-char enforcement, 404 for unknown instrument, 502 maps correctly, error body shape |
| `TestScannerEndpoints` | Response has `results`, `total`, `timeframe`, `failed_instruments`; invalid timeframe → 422 |
| `TestUpstoxEndpoints` | Ingest accepts valid body, rejects empty keys, rejects invalid timeframe |
| `TestOAuthSecurity` | Callback rejects unknown state (401); successful callback never returns `access_token` in body |

Run these whenever you touch any file in `app/api/v1/`.

#### `tests/integration/test_db_operations.py`

Runs against a **SQLite in-memory database** that is created fresh for each test session and rolled back after each test.

| Class | What it tests |
|---|---|
| `TestBulkUpsert` | 5 rows insert, duplicates are no-ops (idempotency), 150 rows split into 3 batches of 50, empty input returns 0 |
| `TestInstrumentSync` | Instrument inserts correctly, re-running with updated name updates the row (not duplicates) |
| `TestScannerService` | Scanner with 3 instruments seeded returns results; scanner with no data returns empty list |

Run these whenever you touch `app/services/data_ingestion.py`, `app/services/market_scanner.py`, or `app/models/upstox_data.py`.

---

## 6. How to Test Manually (End-to-End)

Start the full stack first:

```bash
# From project root
docker compose up --build
```

Or for local development without Docker:
```bash
# Terminal 1 — infrastructure
docker compose up db redis -d

# Terminal 2 — API
cd backend && uvicorn app.main:app --reload --port 8000

# Terminal 3 — Frontend
cd frontend && npm run dev
```

### 6.1 Testing the Health Check

```bash
curl http://localhost:8000/health
```

Expected:
```json
{"status": "ok", "version": "1.0.0"}
```

If this fails, the app did not start — check the terminal for the error. Common causes: missing `.env` variable, DB not running, Redis not running.

### 6.2 Testing the Authentication Flow

The auth flow requires Upstox credentials configured in `.env`.

**Step 1 — Initiate login:**
```bash
# Open in browser (it redirects to Upstox)
http://localhost:8000/api/v1/auth/login
```

**Step 2 — After Upstox redirects back**, the callback hits:
```
http://localhost:8000/api/v1/auth/callback?code=AUTH_CODE&state=STATE
```

If it works, the response sets a `cortex_session` cookie and returns:
```json
{"status": "authenticated", "session_id": "..."}
```

**To test that the state validation works (CSRF protection):**
```bash
curl "http://localhost:8000/api/v1/auth/callback?code=fake&state=tampered"
```

Expected: `401 Unauthorized` — not `500`.

**To test auth is enforced on a protected route:**
```bash
# No token → should get 401
curl http://localhost:8000/api/v1/scanner/run
```

Expected:
```json
{"detail": "Not authenticated"}
```

### 6.3 Getting a Token for Testing

After the OAuth flow completes, you have a `cortex_session` cookie. For API testing with curl, get a JWT by implementing the user endpoint (not yet built) or temporarily issue one from a Python shell:

```python
# In a Python shell with the backend virtualenv active:
import sys; sys.path.insert(0, 'backend')
from app.core.security import create_access_token
print(create_access_token("test-user-123"))
```

Use this token in all subsequent curl calls:
```bash
export TOKEN="paste_token_here"
```

### 6.4 Testing the Scanner

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/scanner/run?timeframe=1d"
```

Expected response shape:
```json
{
  "results": [],
  "total": 0,
  "timeframe": "1d",
  "failed_instruments": 0,
  "cached": false
}
```

`results` will be empty until OHLCV data has been ingested (see 6.8).

**Test cache:** Call it twice in quick succession — the second call should be faster (served from Redis). Add `?force_refresh=true` to bypass cache.

**Test invalid timeframe:**
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/scanner/run?timeframe=2d"
```
Expected: `422 Unprocessable Entity`.

**Test rate limiting:**
```bash
# Hit the scanner more than 10 times in a minute
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code}\n" \
    -H "Authorization: Bearer $TOKEN" \
    "http://localhost:8000/api/v1/scanner/run"
done
```
Expected: first 10 return `200`, then `429 Too Many Requests`.

### 6.5 Testing the HawkEye Radar

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/hawk-eye/scan?timeframes=1d&timeframes=4h"
```

Expected response shape:
```json
{
  "results": [],
  "total": 0,
  "timeframes_scanned": ["1d", "4h"],
  "scanned_at": "2026-04-04T..."
}
```

**Test invalid timeframe:**
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/hawk-eye/scan?timeframes=2d"
```
Expected: `422` with a message listing valid timeframes.

### 6.6 Testing Live Market Data

```bash
# Replace with a real instrument key from Upstox
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/market-data/live/NSE_EQ|INE009A01021"
```

This requires a valid Upstox access token to be set (via the OAuth flow).

**Test the instrument search (min 2 chars):**
```bash
# Should work (2 chars)
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/market-data/instruments/search?q=RE"

# Should fail (1 char)
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/market-data/instruments/search?q=R"
```
Expected for 1 char: `422`.

### 6.7 Testing the Tick Stream WebSocket

```bash
# Start streaming for an instrument
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '["NSE_EQ|INE009A01021"]' \
  "http://localhost:8000/api/v1/upstox/stream/start"

# Check status
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/upstox/stream/status"

# Stop streaming
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/upstox/stream/stop"
```

Note: The WebSocket connection to Upstox will only succeed with a valid access token configured. In development without a live Upstox token, the stream will attempt to connect and back off — this is expected behaviour.

### 6.8 Testing Candle Ingestion

```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "instrument_keys": ["NSE_EQ|INE009A01021"],
    "timeframe": "1d",
    "from_date": "2024-01-01",
    "to_date": "2024-12-31"
  }' \
  "http://localhost:8000/api/v1/upstox/ingest"
```

Expected: `200` with `{"status": "accepted", ...}` immediately — the actual ingestion runs in the background.

**Verify the data was written to the DB:**
```bash
# In a new terminal
docker exec -it cortex-db-1 psql -U cortex -d cortex_db \
  -c "SELECT instrument_key, count(*) FROM upstox_ohlcv GROUP BY 1;"
```

**Test invalid timeframe:**
```bash
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"instrument_keys":["NSE_EQ|TEST"],"timeframe":"2d","from_date":"2024-01-01","to_date":"2024-12-31"}' \
  "http://localhost:8000/api/v1/upstox/ingest"
```
Expected: `422`.

---

## 7. Code Quality Checks

Run these before every commit:

```bash
cd backend

# 1. Lint (check for bugs, style, deprecated APIs)
ruff check .

# 2. Lint and auto-fix what can be fixed
ruff check . --fix

# 3. Format code (replaces black)
ruff format .

# 4. Type checking
mypy app/

# 5. Run all tests
pytest

# 6. Run tests with coverage
pytest --cov=app --cov-report=term-missing
```

Run all of the above in one line:
```bash
ruff check . --fix && ruff format . && mypy app/ && pytest --cov=app
```

**What ruff catches automatically:**
- `datetime.utcnow()` — flagged by the `DTZ` rule group
- Stray `print()` statements — flagged by `T20`
- Unused imports — flagged by `F401`
- Missing f-string prefix — flagged by `F541`
- Import order — auto-fixed by `I`

---

## 8. Database Operations

### View current schema state
```bash
cd backend
alembic current   # shows which revision the DB is on
alembic history   # shows full migration chain
```

### Create a new migration
```bash
# After adding/changing a model
alembic revision --autogenerate -m "short_description_of_change"

# Always review the generated file before applying!
cat alembic/versions/XXXX_short_description_of_change.py

# Apply it
alembic upgrade head
```

### Roll back one migration
```bash
alembic downgrade -1
```

### Connect to the database directly
```bash
docker exec -it cortex-db-1 psql -U cortex -d cortex_db

# Useful queries:
\dt                                                    -- list all tables
\d upstox_ohlcv                                        -- describe a table
SELECT count(*) FROM upstox_ohlcv;                    -- count OHLCV rows
SELECT count(*) FROM instrument_master;                -- count instruments
SELECT * FROM upstox_ohlcv LIMIT 5;                   -- sample data
```

### Resetting the database in development
```bash
docker compose down -v        # remove volumes (deletes all data)
docker compose up db redis -d
cd backend && alembic upgrade head
```

---

## 9. Debugging Common Issues

### App fails to start: "ValidationError"

The app startup validation caught a problem in your `.env`. Read the full error message — it will tell you exactly which field failed and why. Common causes:

- `SECRET_KEY` is missing or is still the placeholder `REPLACE_WITH_32_BYTE_RANDOM_HEX`
- `DATABASE_URL` format is wrong (must be `postgresql+asyncpg://` not `postgresql://`)
- `CORS_ALLOWED_ORIGINS` contains `"*"` — use `["http://localhost:3000"]` instead

### App starts but database operations fail

```bash
# Check DB is running and healthy
docker compose ps

# Check DB is reachable from the API container
docker exec cortex-api-1 pg_isready -h db -U cortex

# Check the DATABASE_URL in .env matches what docker-compose expects
grep DATABASE_URL backend/.env
```

### Redis errors: "Redis not initialised"

Redis must be running before the API starts. The lifespan connects to Redis on startup — if Redis is down, the startup fails with a clear error.

```bash
docker compose up redis -d
# Then restart the API
```

### Tests fail with "RuntimeError: no running event loop"

Make sure `pytest.ini` has `asyncio_mode = auto`. This is already configured. If you're adding a new test file, you don't need `@pytest.mark.asyncio` on individual functions — it's applied globally.

### A route returns 422 unexpectedly

Look at the response body:
```json
{
  "error": {
    "message": "Validation failed",
    "errors": [
      {"field": "body.timeframe", "message": "String should match pattern..."}
    ]
  }
}
```

The `errors` array tells you exactly which field failed and why.

### Rate limit hit in development

```bash
# Flush all rate limit keys in Redis
docker exec cortex-redis-1 redis-cli FLUSHDB
```

Or raise the limits temporarily in `.env`:
```bash
RATE_LIMIT_SCANNER=1000/minute
```

### Cache stale in development

```bash
# Flush all cache keys
docker exec cortex-redis-1 redis-cli FLUSHDB
```

Or bypass the cache on the endpoint:
```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/scanner/run?force_refresh=true"
```

### "Import could not be resolved" in IDE

Make sure your IDE's Python interpreter is set to the virtual environment:
```bash
# The venv is at backend/.venv
# In VS Code: Ctrl+Shift+P → "Python: Select Interpreter" → choose backend/.venv/bin/python
```

---

## 10. Rules That Must Never Be Broken

These are not preferences — breaking them will reopen security vulnerabilities that were explicitly closed.

**1. Never add defaults for secrets in `config.py`.**  
`SECRET_KEY`, `DATABASE_URL`, `UPSTOX_API_KEY`, `UPSTOX_API_SECRET`, `REDIS_URL`, `CORS_ALLOWED_ORIGINS` must remain `Field(...)`. If the app starts without them set, it will use the wrong values silently.

**2. Never use `datetime.utcnow()` — use `server_default=func.now()` in models or `datetime.now(timezone.utc)` in code.**  
Ruff's `DTZ` rule group will catch this automatically if you forget.

**3. Never disable TLS verification.**  
Do not set `ssl.CERT_NONE` or `check_hostname = False` anywhere. If Upstox uses a self-signed cert in any environment, load the cert explicitly with `ssl_context.load_verify_locations()` instead.

**4. Never return a raw exception's `str()` in an HTTP response.**  
Always raise a typed exception from `app/exceptions.py`. The exception handler will format the response correctly without leaking internals.

**5. Never add a route without authentication.**  
Every route handler in `api/v1/` except `auth.py` must have `user_id: str = Depends(get_current_user_id)` as a parameter, or the router must have `dependencies=[Depends(get_current_user_id)]`. The `/health` endpoint in `main.py` is the only intentional exception.

**6. Never use `["*"]` for `CORS_ALLOWED_ORIGINS`.**  
The `@model_validator` in `config.py` will reject it on startup, but do not try to work around it. Add your actual origin to the list.

**7. Never import from `app.main` in a router.**  
The `limiter` lives in `app.core.limiter` for exactly this reason — to prevent circular imports. Always `from app.core.limiter import limiter`.

**8. Never write per-row insert loops for bulk data.**  
Always use `pg_insert().values(batch).on_conflict_do_nothing()` with batching. A for-loop that calls `session.execute()` per row will destroy performance at scale.

**9. Every new service method that talks to Postgres or Redis must have an integration test.**  
The test uses SQLite in-memory — it is fast and requires no infrastructure. There is no acceptable reason to skip it.

**10. Never commit `.env` to git.**  
`.env` is in `.gitignore`. If you accidentally do this, rotate every secret in that file immediately — GitHub and GitLab scan for secrets automatically and notify attackers within minutes.

---

*End of guide. Last updated: 2026-04-04. Questions → open an issue or ping the engineering channel.*
