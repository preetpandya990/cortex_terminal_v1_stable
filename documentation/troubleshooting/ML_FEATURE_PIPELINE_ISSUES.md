# ML Feature Pipeline — Issue Report

**Date:** 2026-06-15  
**Investigator:** Claude Code  
**Severity:** High — affects every ML inference call system-wide

---

## Problem Statement

Every ML signal assembly call in the system is logging `"Failed to load features"` for
instruments processed by the event_processor. The errors are widespread, uniform, and
systemic — not instrument-specific data gaps.

Two compounding root causes were identified.

---

## Issue 1 — `ml_features` Table is 3 Months Stale (PRIMARY)

### What is happening

The pre-computed feature store (`ml_features`) has data only up to **2026-03-10**.
Today is **2026-06-15** — a **97-day gap**.

`FeatureLoader` Tier 1 queries the last 150 days:

```
window: 2026-01-16 → 2026-06-15
ml_features data available in window: 2026-01-16 → 2026-03-10  (~33 trading days)
sequence_length required: 60 rows
result: 33 < 60 → Tier 1 ALWAYS FAILS for every instrument
```

### Evidence

- Every instrument in the logs shows exactly **"only 33 rows"** — uniform across all
  symbols, which confirms this is a table-level staleness issue, not per-instrument gaps.
- Direct DB query: `SELECT COUNT(*), MIN(timestamp)::date, MAX(timestamp)::date FROM ml_features`
  → `148,639 rows | 2415 symbols | 2025-11-30 → 2026-03-10`

### Impact

Every single ML inference call across the entire system is bypassing the fast Tier 1
path (< 50 ms) and being forced through the slower Tier 2 on-demand OHLCV computation
(< 500 ms). Under load this creates significant latency pressure and drives Issue 2.

### Root Cause

The ML training / feature harvest pipeline (`harvest_sft_data.py`) has not run since
approximately 2026-03-10. The `ml_features` table is never auto-refreshed by the
running worker — it must be populated by an explicit offline pipeline run.

### Fix Required

Re-run the feature harvest pipeline to populate `ml_features` with data covering at
least the last 90 trading days. Once the table is current, Tier 1 will succeed for all
instruments and Tier 2 will no longer be attempted.

---

## Issue 2 — On-Demand OHLCV Query Returns Empty in Event Processor Path (SECONDARY)

### What is happening

Once Tier 1 fails, `FeatureLoader._compute_on_demand` is the fallback. In the
**correlation_engine path** this works correctly — signals are assembled successfully.
In the **event_processor path** it fails with:

```
No OHLCV data found for NSE_EQ|INE242A01010 between 2026-01-16 ... and 2026-06-15 ...
No OHLCV data for NSE_EQ|INE242A01010 — cannot compute features
ML prediction failed for IOC: Failed to load features for 'IOC'
```

### Evidence

- Direct DB query confirms data EXISTS for the failing instruments:
  - IOC (`NSE_EQ|INE242A01010`): **98 rows** in the 150-day window, `timeframe='1D'`
  - MRF (`NSE_EQ|INE883A01011`): rows confirmed present
  - TOP15IETF (`NSE_EQ|INF109K1A344`): 234 total rows, `timeframe='1D'`
- The same on-demand computation succeeds in the correlation_engine path for the same
  instruments — ruling out a data or ORM model issue.

### Root Cause

The event_processor binds a single DB session to the `FeatureLoader` at the start of
each batch cycle:

```python
# event_processor.py
async with session_factory() as db:
    processor.signal_assembler.feature_loader = FeatureLoader(db=db, ...)
    processed_count = await processor.process_batch(db)   # ← multiple commits happen here
    processor.signal_assembler.feature_loader = None
```

Inside `process_batch`, intermediate writes (NLP results, event classifications,
trading signals) are committed to the **same** `db` session before
`gather_ml_signals` is reached. After each intermediate commit, asyncpg autobegins
a new transaction on the connection — but the session's internal state changes.
The `fetch_ohlcv_data` ORM query then runs on this post-commit session state and
returns empty, even though the data is present in the DB.

The correlation_engine path does not exhibit this because it creates a fresh
`FeatureLoader` bound to a clean session with no prior writes or commits in that
session's lifetime before feature loading is attempted.

### Fix Required

The `FeatureLoader` in the event_processor path must not share the long-lived batch
session. Instead it should use a **dedicated short-lived session** for its OHLCV
and feature store queries — completely isolated from the write transaction of
`process_batch`. This matches the intent documented in `feature_loader.py`:

> "Loading strategy: 1. TimescaleDB ml_features table (pre-computed, < 50 ms)
>  2. On-demand computation from raw OHLCV (< 500 ms)"

Both tiers are read-only operations; they should never share a session with concurrent
write transactions.

---

## Affected Files

| File | Role in the issue |
|---|---|
| `backend/app/ml/features/feature_store.py` | `load_features_from_db` — queries stale `ml_features` |
| `backend/app/ml/inference/feature_loader.py` | Tier 1 / Tier 2 loading logic; holds stale `db` ref |
| `backend/app/ml/features/feature_pipeline.py` | `fetch_ohlcv_data` — ORM query that returns empty |
| `backend/app/ai/intelligence/event_processor.py` | Session reuse bug — binds FeatureLoader to batch session |
| `backend/scripts/harvest_sft_data.py` | Feature harvest pipeline — has not run since 2026-03-10 |

---

## Fix Priority

| # | Fix | Impact | Effort |
|---|---|---|---|
| 1 | Run `harvest_sft_data.py` to refresh `ml_features` | Eliminates Issue 1 entirely; reduces pressure on Issue 2 | Low (ops task) |
| 2 | Give `FeatureLoader` a dedicated read-only session in `event_processor.py` | Eliminates Issue 2 entirely | Low–Medium |
