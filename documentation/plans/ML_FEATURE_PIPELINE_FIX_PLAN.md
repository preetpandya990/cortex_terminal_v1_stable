# ML Feature Pipeline — Fix Implementation Plan

**Date:** 2026-06-15
**Branch:** feat/gemini-ai-service
**Severity:** High — affects every ML inference call system-wide
**Scope:** 5 files, 0 new files, 2 independent fixes

---

## Background

Every ML signal assembly call is logging `"Failed to load features"` for all instruments
processed by the event_processor. Two compounding root causes were identified and confirmed
via code audit and DB inspection. Full diagnosis in `ML_FEATURE_PIPELINE_ISSUES.md`.

---

## Fix A — Feature Store Staleness (Issue 1 — PRIMARY)

**Files:** `scripts/compute_production_features.py`, `app/worker.py`, `app/workers/registry.py`

### Root Cause

`get_symbols_to_process()` line 130 skips any symbol that has *any* row in `ml_features`
regardless of row age:

```python
symbols_to_process = [s for s in all_symbols if s not in existing_symbols]
```

Since all 2,415 symbols have entries (from 2026-03-10), the script processes **0 symbols**
and silently exits with "All symbols already have features computed". Running it today
is a no-op.

Additionally, `compute_features_for_symbol_safe()` passes `include_sentiment=False` — a
workaround for a timezone mismatch bug that was already fixed in `sentiment_features.py`
(lines 358–367: tz-strip before join). The workaround was never cleaned up.

---

### `scripts/compute_production_features.py`

**Change 1 — Add `mode` and `stale_days` to `FeatureComputationPipeline.__init__`:**

```python
def __init__(
    self,
    db_url: str,
    lookback_days: int = 90,
    batch_size: int = 10,
    max_workers: int = 5,
    mode: str = "incremental",   # NEW: "incremental" | "refresh"
    stale_days: int = 3,         # NEW: refresh threshold in calendar days
):
```

**Change 2 — Rewrite `get_symbols_to_process()` to branch on mode:**

```python
async def get_symbols_to_process(self) -> list[str]:
    async with self.session_factory() as session:
        # All symbols with OHLCV data
        result = await session.execute(text('''
            SELECT DISTINCT instrument_key
            FROM upstox_ohlcv
            WHERE timeframe = '1D'
            ORDER BY instrument_key
        '''))
        all_symbols = [row[0] for row in result.fetchall()]
        logger.info("Found %d symbols with OHLCV data", len(all_symbols))

        if self.mode == "incremental":
            # Only symbols with NO features at all (original behavior)
            result2 = await session.execute(text('''
                SELECT DISTINCT symbol FROM ml_features
                WHERE feature_version = 'v1.0'
            '''))
            existing = set(row[0] for row in result2.fetchall())
            symbols = [s for s in all_symbols if s not in existing]
            logger.info(
                "Mode=incremental: %d symbols need first-time computation",
                len(symbols),
            )
            return symbols

        # mode == "refresh"
        # Symbols where MAX(ml_features.timestamp) < CURRENT_DATE - stale_days
        # OR symbols in OHLCV but not in ml_features at all
        result2 = await session.execute(
            text('''
                SELECT symbol, MAX(timestamp)::date AS latest
                FROM ml_features
                WHERE feature_version = 'v1.0'
                GROUP BY symbol
            ''')
        )
        latest_by_symbol = {row[0]: row[1] for row in result2.fetchall()}

        from datetime import date, timedelta
        threshold = date.today() - timedelta(days=self.stale_days)

        stale = []
        for sym in all_symbols:
            latest = latest_by_symbol.get(sym)
            if latest is None or latest < threshold:
                stale.append(sym)

        logger.info(
            "Mode=refresh (stale_days=%d, threshold=%s): %d/%d symbols need refresh",
            self.stale_days, threshold, len(stale), len(all_symbols),
        )
        return stale
```

**Change 3 — Remove `include_sentiment=False` from `compute_features_for_symbol_safe()`:**

```python
# Before:
features_df = await compute_features_for_symbol(
    symbol=symbol,
    start_date=start_date,
    end_date=end_date,
    timeframe='1D',
    db=session,
    include_sentiment=False,   # ← REMOVE this line
)

# After:
features_df = await compute_features_for_symbol(
    symbol=symbol,
    start_date=start_date,
    end_date=end_date,
    timeframe='1D',
    db=session,
)
```

Rationale: `merge_sentiment_with_ohlcv()` already strips tz from both sides before the
join (sentiment_features.py lines 358–367). The `include_sentiment=False` flag was a
workaround for that bug and was never removed after the fix. Removing it aligns the
feature store's 69-feature output with the Tier 2 on-demand path.

**Change 4 — Add `--mode` and `--stale-days` to the CLI:**

```python
parser.add_argument(
    "--mode",
    choices=["incremental", "refresh"],
    default="incremental",
    help=(
        "incremental: only process symbols with no features at all (default). "
        "refresh: recompute symbols where features are older than --stale-days."
    ),
)
parser.add_argument(
    "--stale-days",
    type=int,
    default=3,
    dest="stale_days",
    help="Refresh threshold in calendar days (default: 3). Used in refresh mode only.",
)
```

And pass to the pipeline in `main()`:

```python
pipeline = FeatureComputationPipeline(
    db_url=db_url,
    lookback_days=lookback_days,
    batch_size=batch_size,
    max_workers=max_workers,
    mode=args.mode,
    stale_days=args.stale_days,
)
```

**No change to `save_features_to_db()`** — it already uses
`ON CONFLICT (symbol, timestamp, feature_version) DO UPDATE` which handles upsert correctly.

---

### `app/worker.py`

Add `feature_refresh_loop()` after the existing `expiry_loop()` function:

```python
async def feature_refresh_loop(
    session_factory: async_sessionmaker,
    shutdown: asyncio.Event,
) -> None:
    """
    Daily feature store refresh — runs at 16:00 IST (30 min after NSE close at 15:30).

    Instantiates FeatureComputationPipeline in refresh mode and recomputes features
    for all symbols where ml_features.MAX(timestamp) is older than stale_days.
    Uses its own dedicated DB connection pool (not the worker's shared pool) to
    prevent resource contention during the batch operation.

    Respects the cooperative shutdown event: wakes every 60 s to check for shutdown
    instead of sleeping through the full inter-run interval.
    """
    from zoneinfo import ZoneInfo
    from datetime import time as dt_time

    _IST = ZoneInfo("Asia/Kolkata")
    _REFRESH_HOUR   = 16
    _REFRESH_MINUTE = 0
    _STALE_DAYS     = 3
    _LOOKBACK_DAYS  = 90

    logger.info("Feature refresh loop started — daily at %02d:%02d IST", _REFRESH_HOUR, _REFRESH_MINUTE)

    while not shutdown.is_set():
        now_ist  = datetime.now(_IST)
        target   = now_ist.replace(
            hour=_REFRESH_HOUR, minute=_REFRESH_MINUTE, second=0, microsecond=0
        )
        if now_ist >= target:
            target += timedelta(days=1)

        wait_total = (target - now_ist).total_seconds()
        logger.info(
            "Feature refresh: next run at %s IST (in %.0f min)",
            target.strftime("%Y-%m-%d %H:%M"), wait_total / 60,
        )

        # Sleep in 60 s chunks so shutdown is honoured promptly
        slept = 0.0
        while slept < wait_total and not shutdown.is_set():
            chunk = min(60.0, wait_total - slept)
            await asyncio.sleep(chunk)
            slept += chunk

        if shutdown.is_set():
            break

        logger.info("Feature refresh: starting daily run (mode=refresh, stale_days=%d)", _STALE_DAYS)
        try:
            # Import here to avoid circular deps at module load time
            sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
            from compute_production_features import FeatureComputationPipeline

            pipeline = FeatureComputationPipeline(
                db_url=str(settings.DATABASE_URL),
                lookback_days=_LOOKBACK_DAYS,
                batch_size=10,
                max_workers=5,
                mode="refresh",
                stale_days=_STALE_DAYS,
            )
            await pipeline.run()
            logger.info("Feature refresh: daily run complete")
        except Exception as exc:
            logger.error("Feature refresh: run failed — %s", exc, exc_info=True)

    logger.info("Feature refresh loop stopped")
```

Required additions to existing imports at the top of `worker.py`:

```python
import sys
from pathlib import Path
```

---

### `app/workers/registry.py`

**Change 1 — Add `"feature_refresh"` to `TASK_NAMES`:**

```python
TASK_NAMES: tuple[str, ...] = (
    # ── Native loops (pause/trigger-aware) ───────────────────────────────────
    "heartbeat",
    "cache_invalidation",
    "suggestion_expiry",
    "correlation_engine",
    # ── Imported loops (CancelledError-only shutdown) ─────────────────────────
    "rss_ingestion",
    "event_processing",
    "regime_detection",
    "drift_detection",
    "safety_monitoring",
    "data_ingestion",
    "fundamentals_refresh",
    "feature_refresh",          # ← NEW
    # ── Migrated from main.py ────────────────────────────────────────────────
    "pnl_worker",
    "sl_tp_worker",
)
```

**Change 2 — Add factory to `build_task_registry()`:**

```python
from app.worker import (
    cache_invalidation_loop,
    correlation_loop,
    expiry_loop,
    feature_refresh_loop,    # ← NEW import
    heartbeat_loop,
)

# In registry dict:
"feature_refresh": lambda: feature_refresh_loop(
    session_factory=session_factory,
    shutdown=shutdown,
),
```

---

## Fix B — Session Contamination in Event Processor (Issue 2 — SECONDARY)

**Files:** `app/ml/inference/feature_loader.py`, `app/ai/intelligence/event_processor.py`

### Root Cause

The event processing call chain:

```
event_processing_loop
 └── async with session_factory() as db:
      ├── FeatureLoader(db=db, ...)          ← bound to batch session
      └── processor.process_batch(db)
           └── process_raw_event(db, event)
                ├── db.flush()               # obtains processed_event.id
                ├── nlp_engine.process_event(db)
                │    └── db.commit()         # nlp_engine.py:316 — COMMIT 1
                ├── event_classifier.classify(db)
                │    └── db.commit()         # event_classifier.py:420 — COMMIT 2
                └── signal_assembler.assemble_signal(db)
                     └── feature_loader.load_features()
                          └── _compute_on_demand(self.db)
                               └── fetch_ohlcv_data(db=self.db)
                                    └── ORM SELECT → EMPTY  ← BUG
```

After each `db.commit()`, asyncpg's implicit transaction handling starts a new transaction
on the same connection. By the time `fetch_ohlcv_data` runs, the ORM session is in a
post-commit state that causes the query to return 0 rows — even though the data exists.

The intermediate commits are intentional: NLPEngine and EventClassifier must persist their
audit logs and classifications independently of whether signal assembly succeeds. The fix
is isolation, not removing commits.

---

### `app/ml/inference/feature_loader.py`

**Change 1 — Add `session_factory` parameter to `__init__`:**

```python
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import async_sessionmaker   # new import

class FeatureLoader:
    def __init__(
        self,
        db:               AsyncSession,
        redis:            Redis,
        session_factory:  async_sessionmaker | None = None,  # NEW (keyword-only)
        sequence_length:  int = 60,
        n_features:       int = 49,
        feature_names:    tuple[str, ...] | list[str] = (),
    ) -> None:
        self.db               = db
        self.redis            = redis
        self._session_factory = session_factory
        self.sequence_length  = sequence_length
        self.n_features       = n_features
        self.feature_names    = list(feature_names)
```

Fully backward compatible: existing callers that pass only `db` and `redis` are unaffected.

**Change 2 — Add `_read_session()` async context manager:**

```python
@asynccontextmanager
async def _read_session(self):
    """
    Yields an isolated read-only session when session_factory is available.
    Falls back to self.db for callers that did not provide a session_factory
    (e.g. correlation engine, which creates a fresh db per cycle with no
    prior commits — no isolation needed there).
    """
    if self._session_factory is not None:
        async with self._session_factory() as session:
            yield session
    else:
        yield self.db
```

**Change 3 — Update `_load_from_database()` to use `_read_session()`:**

```python
async def _load_from_database(self, instrument_key: str, timeframe: str) -> pd.DataFrame:
    end_date   = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=self.sequence_length * 2 + 30)

    async with self._read_session() as db:                     # ← NEW
        features_df = await load_features_from_db(
            symbol=instrument_key,
            start_date=start_date,
            end_date=end_date,
            db=db,                                             # ← uses isolated session
        )

        if features_df.empty:
            return features_df

        timeframe_map = {"1d": "1D", "1D": "1D", "1w": "1week", "1week": "1week"}
        db_tf = timeframe_map.get(timeframe, timeframe)

        from sqlalchemy import text as sa_text
        row = (
            await db.execute(                                  # ← uses same isolated session
                sa_text("""
                    SELECT close
                    FROM   upstox_ohlcv
                    WHERE  instrument_key = :ik
                      AND  timeframe       = :tf
                    ORDER  BY timestamp DESC
                    LIMIT  1
                """),
                {"ik": instrument_key, "tf": db_tf},
            )
        ).fetchone()

    if row:
        features_df["close"] = float(row[0])

    return features_df
```

**Change 4 — Update `_compute_on_demand()` to use `_read_session()`:**

```python
async def _compute_on_demand(self, instrument_key: str, timeframe: str) -> pd.DataFrame:
    end_date   = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=self.sequence_length * 2 + 30)

    _tf_map      = {"1d": "1D", "1w": "1week"}
    db_timeframe = _tf_map.get(timeframe, timeframe)

    async with self._read_session() as db:                     # ← NEW
        return await compute_features_for_symbol(
            symbol=instrument_key,
            start_date=start_date,
            end_date=end_date,
            timeframe=db_timeframe,
            db=db,                                             # ← uses isolated session
        )
```

**Change 5 — Update `create_feature_loader()` factory:**

```python
def create_feature_loader(
    db:               AsyncSession,
    redis:            Redis,
    session_factory:  async_sessionmaker | None = None,  # NEW
    sequence_length:  int = 60,
    n_features:       int = 49,
    feature_names:    tuple[str, ...] = (),
) -> FeatureLoader:
    return FeatureLoader(
        db=db,
        redis=redis,
        session_factory=session_factory,
        sequence_length=sequence_length,
        n_features=n_features,
        feature_names=feature_names,
    )
```

**`_resolve_instrument_key()` is NOT changed** — it uses `self.db` which is fine.
It runs a single read-only SELECT, hits the module-level `_INSTRUMENT_KEY_CACHE` after the
first call per symbol (process-lifetime cache), and executes before any intermediate
commits in the event pipeline.

---

### `app/ai/intelligence/event_processor.py`

**Change — Pass `session_factory` to `FeatureLoader` in `event_processing_loop()`:**

```python
# Before:
processor.signal_assembler.feature_loader = FeatureLoader(
    db=db,
    redis=redis,
    sequence_length=_seq_len,
    n_features=_n_features,
    feature_names=_feature_names,
)

# After:
processor.signal_assembler.feature_loader = FeatureLoader(
    db=db,
    redis=redis,
    session_factory=session_factory,   # ← NEW: isolated sessions for OHLCV reads
    sequence_length=_seq_len,
    n_features=_n_features,
    feature_names=_feature_names,
)
```

`session_factory` is already available in scope — it is the first argument to
`event_processing_loop()`.

---

## What is NOT changed

| File | Reason |
|---|---|
| `app/ml/features/feature_pipeline.py` | `fetch_ohlcv_data` query is correct; the bug was the session state it was given, not the query |
| `app/ml/features/feature_store.py` | `load_features_from_db` is correct for the same reason |
| `app/ai/intelligence/nlp_engine.py` | Intermediate commit is intentional; NLP audit logs must persist independently |
| `app/ai/intelligence/event_classifier.py` | Same — classification must persist independently |
| `app/ai/correlation/engine.py` | Already works correctly; creates a fresh FeatureLoader per cycle with a clean session |

---

## Deployment Sequence

### Step 1 — Deploy Fix B first (zero downtime, immediate effect)
Fix B is a pure code change with no data dependency. Deploy and restart the worker.
The session isolation takes effect immediately for all subsequent event processing cycles.

### Step 2 — Backfill the feature store (one-time, ~30–60 min)
After deploying the updated `compute_production_features.py`, run once manually:

```bash
cd backend
python -m scripts.compute_production_features --mode=refresh --stale-days=97 --lookback-days=90
```

`--stale-days=97` ensures all 2,415 symbols are caught in this first pass (data gap is 97 days).
Subsequent daily runs use the default `--stale-days=3`.

### Step 3 — Verify Tier 1 is hitting
After the backfill completes, confirm in logs:

```
Feature store hit for RELIANCE → NSE_EQ|INE002A01018 (N rows)
```

If still seeing "only 33 rows" — the refresh did not write new rows. Check
`MAX(ml_features.timestamp)` in the DB.

### Step 4 — Daily automation (Fix A worker task) goes live on next deploy
The `feature_refresh_loop` wakes at 16:00 IST every day and runs in refresh mode.
No operator action needed after deploy.

---

## Post-Fix Behaviour

| Condition | Before | After |
|---|---|---|
| Instrument in feature store, current | Tier 1 ✓ (< 50 ms) — but table stale so never happens | Tier 1 ✓ (< 50 ms) — daily refresh keeps table current |
| Instrument in feature store, stale | Tier 1 fails → Tier 2 fails (session bug) | Tier 1 fails → Tier 2 ✓ (isolated session) |
| New listing, no feature store row | Tier 1 fails → Tier 2 fails (session bug) | Tier 1 fails → Tier 2 ✓ (isolated session) |
| Off-hours, post-commit session state | Tier 2 returns empty → ValueError | Tier 2 uses fresh isolated session → succeeds |

---

## Open Question (deferred)

Should a `--force` mode (recompute ALL symbols regardless of staleness) be added for
schema-change scenarios (e.g. after a feature engineering change that requires rewriting
all stored rows)? Agreed to defer — can be added as a one-liner change to
`get_symbols_to_process()` when needed.
