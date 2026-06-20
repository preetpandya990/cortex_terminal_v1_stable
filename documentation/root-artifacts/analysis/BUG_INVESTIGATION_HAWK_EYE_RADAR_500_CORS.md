# Bug Investigation: HawkEye Radar 500 Errors + Missing CORS Headers

**Last updated: 2026-06-18 — Full deep investigation completed. Original root cause was WRONG. See corrected findings below.**

---

## Failing Endpoints

1. `GET /api/v1/trade-suggestions?status=active&page=1&page_size=50`
2. `GET /api/v1/trade-suggestions/correlations/recent?limit=50`

Both return `500 Internal Server Error` with **no CORS headers**, breaking the browser with a CORS error before the 500 even surfaces.

---

## ✅ CONFIRMED: Root Cause A — Why CORS headers are absent on 500s

FastAPI registers the catch-all `@app.exception_handler(Exception)` inside **`ServerErrorMiddleware`** (outermost layer), not inside `ExceptionMiddleware`. The middleware stack executes in this order:

```
ServerErrorMiddleware  ← catch-all handler lives HERE (outermost)
  → MetricsMiddleware
    → SlowAPIMiddleware
      → GZipMiddleware
        → RequestIDMiddleware
          → CORSMiddleware  ← NEVER runs on the error response path
            → TrustedHostMiddleware
              → ExceptionMiddleware
                → Router (raises the exception)
```

When the router raises an unhandled exception it propagates to `ServerErrorMiddleware`, which calls `unhandled_exception_handler` (`app/core/exception_handlers.py:101`) and sends the 500 JSONResponse **directly**, bypassing `CORSMiddleware`. The browser therefore sees no `Access-Control-Allow-Origin` header and fires a CORS preflight error before the 500 even surfaces.

**This is NOT a CORS config bug.** `localhost:3000` is in the allowlist. It is a Starlette architectural fact: any truly unhandled exception will always produce a CORS-less 500 response.

---

## ❌ ORIGINAL Root Cause B WAS WRONG — Enum Case Mismatch Hypothesis Disproved

The original investigation claimed:

> "Every enum column stores UPPER_SNAKE_CASE but the Python enums and Pydantic models expect lowercase."

**This is false.** Live code inspection confirms:

### Python enum definitions (`app/schemas/trade_suggestions.py:19–51`)
```python
class SignalDirection(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"

class ConfidenceLevel(str, Enum):
    HIGH   = "HIGH"
    MEDIUM = "MEDIUM"
    LOW    = "LOW"

class TriggerPathway(str, Enum):
    TECHNICAL_FIRST    = "TECHNICAL_FIRST"
    FUNDAMENTAL_FIRST  = "FUNDAMENTAL_FIRST"

class TriggerType(str, Enum):
    SCANNER_ANOMALY = "SCANNER_ANOMALY"
    NEWS_EVENT      = "NEWS_EVENT"
```

### DB CHECK constraints (`alembic/versions/0011_trade_suggestions.py:82–84, 135`)
```sql
CHECK (confidence_level  IN ('HIGH', 'MEDIUM', 'LOW'))
CHECK (signal_direction  IN ('BUY', 'SELL'))
CHECK (trigger_pathway   IN ('TECHNICAL_FIRST', 'FUNDAMENTAL_FIRST'))
CHECK (trigger_type      IN ('SCANNER_ANOMALY', 'NEWS_EVENT'))
```

### Engine writer (`app/ai/correlation/engine.py:803–855`)
```python
confidence_level = "HIGH"   # or "MEDIUM"
direction        = "BUY" if all_buy else "SELL"
trigger_pathway  = "TECHNICAL_FIRST" if trigger_type == "SCANNER_ANOMALY" else "FUNDAMENTAL_FIRST"
```

**DB, code enums, and engine writer are ALL consistently UPPER_SNAKE_CASE.** `TradeSuggestionResponse.model_validate()` was tested against live DB rows — it succeeds without error. There is NO enum mismatch. Pydantic validation is NOT the crash source.

---

## ✅ CONFIRMED: Actual Root Cause B — DB Connection Pool Exhaustion

### Live evidence

```sql
SELECT count(*), state FROM pg_stat_activity
WHERE datname='cortex_db' GROUP BY state;

-- Result (2026-06-18):
--  1   | active
-- 26   | idle
-- 30   | idle in transaction   ← ALL 30 pool slots occupied
```

All 30 connections are stuck in `idle in transaction` — some for **over 4 hours**. The last query on every stuck connection is identical:

```sql
SELECT ai_document_embeddings.source_id,
       ai_document_embeddings.symbol,
       ai_document_embeddings.embedding,
       ai_document_embeddings.as_of_timestamp,
       ai_raw_events.raw_content,
       ai_raw_events.source_name,
       ai_raw_events.source_url
FROM   ai_document_embeddings
JOIN   ai_raw_events ON ai_raw_events.id = ai_document_embeddings.source_id
WHERE  ai_document_embeddings.symbol IS NULL
AND    ai_document_embeddings.as_of_timestamp >= $1
ORDER  BY ai_document_embeddings.as_of_timestamp DESC
LIMIT  $2
```

This is the **"general market docs" step** of the RAG retriever (`app/ai/rag/retriever.py:_load_candidates`, `general_stmt` branch, line ~255). The query executed and returned results, but no `COMMIT`/`ROLLBACK` ever followed — leaving the connection in `idle in transaction` forever.

### Why the pool is full

The API engine is configured with `pool_size=20, max_overflow=10` → **30 connections maximum** (`app/core/database.py:85–99`). All 30 are held by stuck background tasks. Any new request that calls `Depends(get_db)` or `Depends(get_redis)` → pool checkout → waits 30s (`pool_timeout=30`) → `sqlalchemy.exc.TimeoutError` → unhandled → `ServerErrorMiddleware` → 500 with no CORS headers.

This is why EVERY authenticated endpoint is now 500ing, not just the two listed above. `dev-login` (which uses `get_db`) also returns 500.

### Root cause of the leak: DB session held open during LLM API call

The leak originates in `app/ai/intelligence/explanation_worker.py: _generate_explanation()` (line 683). The session lifecycle is:

```
async with AsyncSessionLocal() as db:          # session opened
    suggestion = await db.execute(...)         # DB read — connection enters idle-in-transaction
    chunks     = await retrieve(db=db, ...)    # RAG: runs 2 more SELECTs — still idle-in-transaction
    
    # ⚠️  DB session held open across this entire LLM API call:
    raw_output, usage = await client.generate_structured_with_usage(
        prompt=prompt,
        response_model=ExplanationOutput,
        ...                                    # can take 10–15s normally; INFINITE if Gemini
    )                                          # quota exhausted or NIM/Ollama is down

    await db.execute(update(TradeSuggestion)...)   # write back
    await db.commit()
    await _write_audit_entry(db, ...)
# session closed here — BUT only if the LLM call above ever returns
```

If the LLM backend hangs (Gemini quota exhausted with a wait queue, NIM/Ollama down, or circuit breaker holding the request), `await client.generate_structured_with_usage(...)` never returns. The `async with` block never exits. The DB session is never closed. The connection never leaves `idle in transaction`.

With 30 trade suggestions each triggering an explanation job simultaneously, all 30 pool connections are consumed. The pool is exhausted. The server becomes entirely unable to serve authenticated API requests.

`_generate_instrument_context()` (line 871 of the same file) has the **identical anti-pattern** — DB session held open across the LLM call — and is an additional source of the same leak.

---

## ✅ CONFIRMED: Actual Root Cause C — Concurrent AsyncSession Execute (Endpoint 2)

`app/api/v1/trade_suggestions.py:480–483`:

```python
(completed_rows, rejected_rows), inflight_items = await asyncio.gather(
    asyncio.gather(db.execute(completed_stmt), db.execute(rejected_stmt)),  # ← UNSAFE
    _fetch_inflight_correlations(get_redis()),
)
```

`asyncio.gather(db.execute(stmt1), db.execute(stmt2))` starts both coroutines concurrently on the **same `AsyncSession`**. SQLAlchemy's `AsyncSession` uses a single underlying `asyncpg` connection. `asyncpg` does not support concurrent operations on a single connection and raises `asyncpg.exceptions.InternalClientError: another operation is in progress` — an unhandled exception → 500.

The outer gather correctly runs the two DB queries and the Redis call in parallel, but the inner gather on the same session is the bug.

---

## ✅ CONFIRMED: Actual Root Cause D — Unguarded Enum Coercions in `_build_item` (Endpoint 2)

`app/api/v1/trade_suggestions.py:496–520`:

```python
direction   = SignalDirection(suggestion.signal_direction)   # line 496 — unguarded
confidence  = ConfidenceLevel(suggestion.confidence_level)  # line 498 — unguarded
...
trigger_type = TriggerType(corr.trigger_type)               # line 520 — unguarded
```

These bare enum constructors raise `ValueError` for any unexpected value. Currently DB values match enum members, so this does not crash today. But it is a latent fragility: any `EventCorrelation` row written by a future engine version with a new `trigger_type` value would crash the **entire** `/correlations/recent` request — not just that one item. The exception is unhandled → 500.

---

## What Was Ruled Out

| Hypothesis | Status |
|---|---|
| Enum case mismatch (DB UPPER vs code lowercase) | ❌ **DISPROVED** — both DB and code use UPPER_SNAKE_CASE |
| `TradeSuggestionResponse.model_validate()` ValidationError | ❌ **DISPROVED** — tested live against all 4 active rows, all pass |
| Migration 0042 not applied | ❌ Ruled out — DB is at head (0046) |
| CORS origin misconfigured | ❌ Ruled out — `localhost:3000` is in allowlist |
| `llm_summary` / `llm_explanation` column missing | ❌ Ruled out — columns exist |
| `consensus_score` out of range | ❌ Ruled out — all rows are within 0–100 |
| `uid = int(user_id)` ValueError | ❌ Ruled out — `User.id` is `Integer`, JWT `sub` is always `str(int)` |
| `get_redis()` raising RuntimeError in production | ❌ Ruled out — Redis IS initialized in production lifespan |

---

## Full Bug List (Confirmed, Prioritised)

### Bug 1 — CRITICAL: DB connection pool exhaustion via session leak in explanation worker
- **File:** `app/ai/intelligence/explanation_worker.py`
- **Functions:** `_generate_explanation()` (line 683), `_generate_instrument_context()` (line 871)
- **Pattern:** DB session held open across blocking LLM API `await` call
- **Effect:** Pool exhaustion → 100% of authenticated endpoints return 500 immediately
- **Fix:** Restructure into three distinct phases — (1) read from DB + close session, (2) call LLM with explicit timeout, (3) open new session + write result

### Bug 2 — HIGH: CORS headers absent on all 500 responses
- **File:** `app/core/exception_handlers.py` + `app/main.py`
- **Cause:** Starlette middleware order — `ServerErrorMiddleware` handles errors outside `CORSMiddleware`
- **Effect:** Browser sees CORS error, masking the 500 — developer experience is broken
- **Fix:** Inject `Access-Control-Allow-Origin` header directly in `unhandled_exception_handler`, matching `request.headers.get("origin")` against `settings.cors_origins`

### Bug 3 — HIGH: Concurrent AsyncSession execute on single session (Endpoint 2)
- **File:** `app/api/v1/trade_suggestions.py:480–483`
- **Cause:** `asyncio.gather(db.execute(stmt1), db.execute(stmt2))` on one `AsyncSession`
- **Effect:** Intermittent `asyncpg` error → 500 on `/correlations/recent`
- **Fix:** Run the two DB queries sequentially; keep the Redis call in the outer gather

### Bug 4 — MEDIUM: Unguarded enum coercions in `_build_item` (Endpoint 2)
- **File:** `app/api/v1/trade_suggestions.py:496, 498, 520`
- **Cause:** Bare `EnumClass(value)` constructor with no try/except
- **Effect:** Any unexpected DB value crashes the entire `/correlations/recent` request
- **Fix:** Wrap each coercion in try/except ValueError, use None for unknown values (handled by Optional fields in the schema)

### Bug 5 — MEDIUM: No `idle_in_transaction_session_timeout` set in Postgres
- **Location:** DB connection configuration + PostgreSQL server
- **Cause:** No server-side timeout to kill connections stuck idle-in-transaction
- **Effect:** A session leak (like Bug 1) silently fills the pool; no automatic recovery
- **Fix:** Set `idle_in_transaction_session_timeout = '5min'` in `connect_args.server_settings` in `app/core/database.py`; also add it as a PostgreSQL config parameter

---

## Immediate Recovery Action (Before Code Fix)

Kill all stuck idle-in-transaction connections to restore service immediately:

```sql
SELECT pg_terminate_backend(pid)
FROM   pg_stat_activity
WHERE  datname = 'cortex_db'
AND    state   = 'idle in transaction'
AND    now() - query_start > interval '10 minutes';
```

This will restore the connection pool and make all API endpoints responsive again. The underlying leak (Bug 1) must still be fixed or they will fill up again.
