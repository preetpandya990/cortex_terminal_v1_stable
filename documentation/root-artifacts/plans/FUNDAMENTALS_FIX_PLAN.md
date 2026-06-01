# Fundamentals Data — Complete Remediation Plan

**Date:** 2026-05-18  
**Status:** Approved, pending implementation  
**Scope:** Backend service, ML features, scheduler, migration, frontend

---

## Architecture Decisions (Locked)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Financial statement type | **Standalone** | Removes subsidiary noise from parent-company ML features |
| Key ratios type | **As-is** (Upstox pre-computes, no `type` param) | Not controllable at fetch time |
| Income statement view | **Combined yearly + quarterly with toggle** | Both time scales in one section |
| API fetch path | **Live-fetch kept**, Redis `SET NX` distributed lock | No coverage gaps for new instruments |
| Refresh ownership | **Worker** owns scheduled refresh; API only calls live-fetch if DB is empty | Single process controls burst rate |
| Rate limiter | **Redis SET NX per-instrument lock** prevents API live-fetch + worker schedule collision | Eliminates dual-process rate violation |
| Data freshness display | **Per-section `fetched_at`** shown in FundamentalsTab | Users can see staleness per data type |

---

## All Issues — Indexed

| ID | Category | Issue | Severity |
|----|----------|-------|----------|
| A1 | Correctness | Financial statements fetched as `consolidated`, must be `standalone` | Critical |
| A2 | Correctness | `_assemble_*` functions missing `statement_type` filter — silent data corruption if both types coexist | Critical |
| A3 | Correctness | ML feature queries missing `statement_type` filter — same corruption risk | Critical |
| A4 | Architecture | No staleness detection — DB path always returns data regardless of age | Critical |
| A5 | Architecture | Module-level rate limiter broken across API + worker processes (each has independent counter) | Critical |
| B1 | Data gap | Quarterly income statement never fetched — missing TTM and recent-quarter data | High |
| B2 | Data gap | Key ratios schema rigid — only 6 hardcoded columns, extra ratios silently discarded | Medium |
| B3 | Data gap | Corporate action date stored as raw string — unsortable, unqueryable | Medium |
| C1 | Automation | No daily key-ratios refresh (P/E, P/B move with price every market session) | Critical |
| C2 | Automation | No daily corporate-actions refresh (no Upstox webhook; poll required) | High |
| C3 | Automation | No quarterly financials + shareholdings refresh (SEBI filing deadlines) | Critical |
| C4 | Automation | No monthly profile + competitors refresh | Low |
| C5 | Automation | Redis not explicitly invalidated on scheduled refresh writes | High |
| C6 | Automation | Upstox token expiry silently crashes refresh loops | High |
| D1 | Code quality | `_fetch()` docstring says 7 req/s — actual is 1.0 req/s (stale) | Low |
| D2 | Code quality | Backfill docstring says `Semaphore(40)` — actual is `_CONCURRENCY = 5` (stale) | Low |
| D3 | UX | Data freshness not visible to user; per-section `fetched_at` must be shown | Medium |

---

## Phase 1 — Correctness Fixes

*Fix what is wrong before building anything new. No new dependencies.*

### 1.1 — New Migration: `0036_fundamentals_schema_corrections.py`

**`company_key_ratios` table:**
- Add `extra_ratios JSONB NULLABLE` — stores the full raw ratio array from Upstox, so no API data is ever silently discarded. The 6 ML-critical columns (pe, pb, roa, roe, roce, ev_ebitda) are kept as explicit columns for query performance.

**`company_corporate_actions` table:**
- Add `expiry_date DATE NULLABLE` — parsed from existing `expiry_date_str` at write time.
- Add index: `idx_cca_instrument_expiry ON company_corporate_actions(instrument_key, expiry_date)` — enables date-range queries and future/past action filtering.

Fixes: B2, B3

---

### 1.2 — `app/services/fundamentals_service.py`

**Change A — `_fetch_all_raw()`: switch financial statements to standalone** (lines 232–234)

```python
# Before
_fetch(f"{isin}/income-statement", client, {"type": "consolidated", "time_period": "yearly"}),
_fetch(f"{isin}/balance-sheet",    client, {"type": "consolidated"}),
_fetch(f"{isin}/cash-flow",        client, {"type": "consolidated"}),

# After
_fetch(f"{isin}/income-statement", client, {"type": "standalone", "time_period": "yearly"}),
_fetch(f"{isin}/balance-sheet",    client, {"type": "standalone"}),
_fetch(f"{isin}/cash-flow",        client, {"type": "standalone"}),
```

Fixes: A1

---

**Change B — `_upsert_key_ratios()`: capture `extra_ratios`**

Add `"extra_ratios": data` (the full raw list) to the row dict before the pg_insert upsert. The existing 6 column mappings remain unchanged.

Fixes: B2

---

**Change C — `_replace_corporate_actions()`: parse `expiry_date`**

Add helper `_parse_action_date(s: str | None) -> date | None` that handles Upstox's `DD-MMM-YYYY` format (e.g. `"15-Jun-2026"`). Use `datetime.strptime(s, "%d-%b-%Y").date()` with try/except. Populate `expiry_date` in each row dict.

Fixes: B3

---

**Change D — `_assemble_income_statement()`: add `statement_type` filter** (line ~653)

```python
CompanyIncomeStatement.statement_type == "standalone",
```

Fixes: A2

---

**Change E — `_assemble_balance_sheet()`: add `statement_type` filter** (line ~699)

```python
CompanyBalanceSheet.statement_type == "standalone",
```

Fixes: A2

---

**Change F — `_assemble_cash_flow()`: add `statement_type` filter** (line ~720)

```python
CompanyCashFlow.statement_type == "standalone",
```

Fixes: A2

---

**Change G — `_fetch()` docstring** (line ~164)

Replace: `"This caps sustained throughput at 7 req/s = 420 req/min"`
With: `"This caps sustained throughput at 1.0 req/s = 1800 req/30min"`

Fixes: D1

---

**Change H — Per-section `fetched_at` in response**

Each assemble function (`_assemble_income_statement`, `_assemble_balance_sheet`, etc.) already returns the data list. Extend `assemble_from_db()` to also return a `section_fetched_at` dict that collects the `fetched_at` timestamp from each table's most recently written row:

```python
"section_fetched_at": {
    "key_ratios":        kr.fetched_at if kr else None,
    "income_statement":  max(r.fetched_at for r in income_rows) if income_rows else None,
    "balance_sheet":     max(r.fetched_at for r in bs_rows) if bs_rows else None,
    "cash_flow":         max(r.fetched_at for r in cf_rows) if cf_rows else None,
    "share_holdings":    max(r.fetched_at for r in sh_rows) if sh_rows else None,
    "corporate_actions": max(r.fetched_at for r in ca_rows) if ca_rows else None,
    "competitors":       max(r.fetched_at for r in comp_rows) if comp_rows else None,
    "profile":           profile.fetched_at,
}
```

Fixes: A4, D3

---

### 1.3 — `app/models/fundamentals.py`

- `CompanyKeyRatios`: add `extra_ratios: Mapped[list | None] = mapped_column(JSONB(astext_type=Text()), nullable=True)`
- `CompanyCorporateActions`: add `expiry_date: Mapped[date | None] = mapped_column(Date(), nullable=True)`

Fixes: B2, B3

---

### 1.4 — `app/schemas/fundamentals.py`

- `KeyRatioItem`: add `extra_ratios: list[dict] | None = None`
- `CorporateAction`: add `expiry_date: date | None = None`
- `FundamentalsFullResponse`: add `section_fetched_at: dict[str, datetime | None] | None = None`

Fixes: B2, B3, A4, D3

---

### 1.5 — `app/ml/features/fundamental_features.py`

Add `statement_type == 'standalone'` filter to all three financial statement queries:

- Income statement query (~line 149): `CompanyIncomeStatement.statement_type == "standalone"`
- Balance sheet query (~line 171): `CompanyBalanceSheet.statement_type == "standalone"`
- Cash flow query (~line 194): `CompanyCashFlow.statement_type == "standalone"`

Fixes: A3

---

### 1.6 — `scripts/backfill_fundamentals.py`

Fix docstring: replace `asyncio.Semaphore(40)` reference with `_CONCURRENCY = 5`.

Fixes: D2

---

## Phase 2 — Quarterly Income Data

*Fetch and store quarterly standalone income statements. Update frontend with yearly/quarterly toggle.*

### 2.1 — `app/services/fundamentals_service.py`

**Change A — `_fetch_all_raw()`: add quarterly income fetch**

Add a 9th concurrent fetch:
```python
_fetch(f"{isin}/income-statement", client, {"type": "standalone", "time_period": "quarterly"}),
```
Update `keys` list to include `"income_statement_quarterly"`. Total: 9 concurrent fetches per instrument (still within Upstox's burst limits).

**Change B — `fetch_and_store_all()`: upsert quarterly rows**

Call `_upsert_income_statement(instrument_key, isin, raw["income_statement_quarterly"] or {}, db)`. The existing unique constraint `uq_cis_instrument_period_type` on `(instrument_key, period_date, time_period, statement_type)` distinguishes quarterly rows cleanly. No schema change needed.

**Change C — `_assemble_income_statement()`: return both timeframes**

Change to accept `time_period: str` parameter. Call twice from `assemble_from_db`: once with `"yearly"`, once with `"quarterly"`. Return a structured dict:

```python
"income_statement": {
    "yearly":    <list[CategoryHistory]>,
    "quarterly": <list[CategoryHistory]>,   # up to 8 quarters
}
```

Fixes: B1

---

### 2.2 — `app/schemas/fundamentals.py`

Replace `income_statement: list[CategoryHistory] | None` with:

```python
class IncomeStatementData(BaseModel):
    yearly:    list[CategoryHistory]
    quarterly: list[CategoryHistory]

income_statement: IncomeStatementData | None = None
```

Fixes: B1 (schema)

---

### 2.3 — `frontend/src/types/fundamentals.ts`

```typescript
export interface IncomeStatementData {
  yearly:    CategoryHistory[];
  quarterly: CategoryHistory[];
}

// In FundamentalsFullResponse:
income_statement: IncomeStatementData | null;
```

---

### 2.4 — `frontend/src/components/hawk-eye-radar/FundamentalsTab.tsx`

Replace the flat `IncomeStatementSection` component with one that has a local `"yearly" | "quarterly"` toggle:

- Two pill buttons at the top of the section: **Annual** | **Quarterly**
- Default: Annual (4 columns max)
- Quarterly: last 8 quarters (most recent on right)
- Both datasets are already in the response — no re-fetch on toggle, purely local state
- Reuse existing `FinancialTable` component; pass it whichever dataset is active
- Per-section `fetched_at` shown as a small "Updated X days ago" line at the bottom of each collapsible section header (right-aligned, muted text, using `section_fetched_at` from response)

Fixes: B1 (UI), D3

---

## Phase 3 — Refresh Scheduler

*Automated data freshness for all cadences. Solve distributed rate limit.*

### 3.1 — New file: `app/services/fundamentals_refresh.py`

A single self-contained scheduler class following the same start/stop lifecycle pattern as `SignalScheduler`. All Upstox fundamentals HTTP calls that are scheduled (not user-triggered) run through this class exclusively.

**Class: `FundamentalsRefreshScheduler`**

```
FundamentalsRefreshScheduler
├── start()                          → spawns all loop tasks
├── stop()                           → cancels all loop tasks with 5s timeout
├── _daily_key_ratios_loop()         → daily at 15:45 IST post-market close
├── _daily_corporate_actions_loop()  → daily at 18:30 IST after NSE feed update
├── _quarterly_financials_loop()     → runs on SEBI Reg. 33 deadline dates
├── _quarterly_holdings_loop()       → runs on SEBI Reg. 31 deadline dates
└── _monthly_profile_loop()          → 1st business day of each month
```

**SEBI deadline calendar:**

| Quarter | Holdings deadline (Reg. 31, 21 days) | Financials deadline (Reg. 33, 45 days) |
|---------|--------------------------------------|----------------------------------------|
| Q1 (Apr–Jun) | Jul 21 | Aug 14 |
| Q2 (Jul–Sep) | Oct 21 | Nov 14 |
| Q3 (Oct–Dec) | Jan 21 | Feb 14 |
| Q4 (Jan–Mar) | Apr 21 | May 14 |

Each loop runs on the first business day on or after the deadline date (uses `nse_calendar.is_trading_day()`).

**Refresh scope strategy for daily jobs:**

- Priority tier 1 (runs immediately at trigger time): instruments that appear in `ai_trading_signals` from the last 7 days — typically 50–150 instruments.
- Priority tier 2 (runs nightly 01:00–05:00 IST): full 2,453-instrument universe at 1.0 req/s.

Tier 1 ensures the most-active instruments are always fresh. Tier 2 keeps the full universe current on a nightly basis.

**Redis distributed lock — fixes A5:**

Before any Upstox fetch (both scheduled refresh AND API live-fetch), acquire:
```
SET cai:fundamentals:lock:{instrument_key} 1 NX EX 30
```
- `NX`: only succeeds if key does not exist (no concurrent fetch in progress)
- `EX 30`: auto-expires in 30 seconds (safety net if process dies mid-fetch)
- Lock acquisition failure → skip instrument (in scheduler) or return DB data (in API live-fetch)
- Lock released by explicit `DEL` after DB write + Redis invalidation completes

This ensures the module-level `_rate_next_slot` counter never races between API and worker processes — at most one process fetches any given instrument at a time.

**Redis invalidation after refresh — fixes C5:**

After every successful `fetch_and_store_all()` call in the scheduler:
```python
await redis.delete(_redis_key(instrument_key))
```
Forces the next API read to re-assemble from freshly written DB rows.

**TTL jitter — fixes C5 thundering-herd:**

In `get_fundamentals()`, when writing to Redis after any path (live fetch or DB reassembly):
```python
import random
jitter = random.randint(-int(_REDIS_TTL * 0.1), int(_REDIS_TTL * 0.1))
await redis.set(key, serialized, ex=_REDIS_TTL + jitter)
```
Spreads expiry across a ±10% window (~±2.4 hours around the 24h TTL).

**Token expiry handling — fixes C6:**

```python
try:
    await fetch_and_store_all(instrument_key, isin, db)
except RuntimeError as exc:
    if "UPSTOX_ACCESS_TOKEN" in str(exc):
        logger.critical(
            "Upstox access token expired — fundamentals refresh cycle aborted. "
            "Renew token and restart worker."
        )
        return  # abort this cycle, do not crash the loop
    raise
```

---

### 3.2 — `app/worker.py`

Register `FundamentalsRefreshScheduler` in the worker's startup block alongside existing loops:

```python
from app.services.fundamentals_refresh import FundamentalsRefreshScheduler

fundamentals_scheduler = FundamentalsRefreshScheduler(
    db_factory=session_factory,
    redis=redis_client,
)
await fundamentals_scheduler.start()
```

Shut it down on SIGTERM with the same 5s timeout pattern used for other tasks.

---

### 3.3 — `app/core/config.py`

Add settings:
```python
FUNDAMENTALS_DAILY_RATIOS_TIME_IST:    str = "15:45"
FUNDAMENTALS_CORP_ACTIONS_TIME_IST:    str = "18:30"
FUNDAMENTALS_PRIORITY_LOOKBACK_DAYS:   int = 7
```

---

### 3.4 — `app/api/v1/fundamentals.py`

In the live-fetch fallback path, acquire the Redis `SET NX` lock before calling `fetch_and_store_all()`:

```python
lock_key = f"cai:fundamentals:lock:{instrument_key}"
lock_acquired = await redis.set(lock_key, "1", nx=True, ex=30)
if not lock_acquired:
    # Worker is already refreshing this instrument — return DB data if available,
    # else return a 503-style available=False with reason="refresh_in_progress"
    db_data = await assemble_from_db(instrument_key, db)
    if db_data:
        return FundamentalsFullResponse(**db_data)
    return FundamentalsFullResponse(
        available=False,
        instrument_key=instrument_key,
        reason="refresh_in_progress",
    )
try:
    live_data = await fetch_and_store_all(instrument_key, isin, db)
    ...
finally:
    await redis.delete(lock_key)
```

Fixes: A5

---

## Phase 4 — Backfill Re-run

### 4.1 — Pre-conditions

1. Phase 1 migration (`0036_fundamentals_schema_corrections.py`) must have run.
2. Phase 1 + Phase 2 service code must be deployed (standalone + quarterly fetches).
3. `UPSTOX_ACCESS_TOKEN` must be valid (token is daily — run backfill within the same day the token is issued).

### 4.2 — Execution

```bash
python scripts/backfill_fundamentals.py --fresh
```

- `--fresh` clears any existing checkpoint and drops/re-upserts all consolidated rows (the upsert constraint handles the overwrite cleanly — standalone rows have `statement_type='standalone'` which is different from existing `'consolidated'` rows, but those old rows remain unless purged).
- **Action required before backfill:** run a manual SQL purge of old consolidated financial statement rows:
  ```sql
  DELETE FROM company_income_statement WHERE statement_type = 'consolidated';
  DELETE FROM company_balance_sheet     WHERE statement_type = 'consolidated';
  DELETE FROM company_cash_flow         WHERE statement_type = 'consolidated';
  ```
  This avoids serving a mix of consolidated and standalone rows during the backfill window.

### 4.3 — Expected runtime

- 2,453 instruments × 9 endpoint calls (8 original + 1 quarterly income) = 22,077 requests
- At 1.0 req/s sustained → ~6.1 hours
- Run overnight with `nohup` or as a systemd oneshot service

### 4.4 — Verification

After backfill completes, spot-check 5 instruments:

```bash
# Via API
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/fundamentals/NSE_EQ|INE002A01018"  # RELIANCE
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/fundamentals/NSE_EQ|INE009A01021"  # INFY
curl -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/v1/fundamentals/NSE_EQ|INE467B01029"  # TCS

# Via DB
SELECT instrument_key, statement_type, COUNT(*), MIN(period_date), MAX(period_date)
FROM company_income_statement
GROUP BY instrument_key, statement_type
ORDER BY instrument_key
LIMIT 20;
```

Expected: `statement_type = 'standalone'` only, period coverage ~4 years yearly + ~8 quarters.

---

## File Change Summary

| File | Phase | Type | Issues Fixed |
|------|-------|------|-------------|
| `alembic/versions/0036_fundamentals_schema_corrections.py` | 1 | **New** | B2, B3 |
| `app/models/fundamentals.py` | 1 | Modify | B2, B3 |
| `app/schemas/fundamentals.py` | 1+2 | Modify | B2, B3, A4, D3, B1 |
| `app/services/fundamentals_service.py` | 1+2+3 | Modify | A1, A2, A4, A5, B1, B2, B3, C5, D1 |
| `app/ml/features/fundamental_features.py` | 1 | Modify | A3 |
| `app/api/v1/fundamentals.py` | 3 | Modify | A5 |
| `app/core/config.py` | 3 | Modify | C1, C2, C3, C4 |
| `app/services/fundamentals_refresh.py` | 3 | **New** | C1, C2, C3, C4, C5, C6 |
| `app/worker.py` | 3 | Modify | C1, C2, C3, C4 |
| `scripts/backfill_fundamentals.py` | 1+4 | Modify | D2 |
| `frontend/src/types/fundamentals.ts` | 1+2 | Modify | B1, B3, D3 |
| `frontend/src/components/hawk-eye-radar/FundamentalsTab.tsx` | 1+2 | Modify | B1, B3, D3 |

**Total: 10 modified files, 2 new files, 1 new migration.**  
All 17 issues resolved across 4 phases.

---

## Implementation Order

```
Phase 1  →  Phase 2  →  Phase 3  →  SQL purge  →  Phase 4 backfill
```

Do not run the backfill until Phases 1–3 are deployed. Do not run Phase 4 with a stale Upstox access token.
