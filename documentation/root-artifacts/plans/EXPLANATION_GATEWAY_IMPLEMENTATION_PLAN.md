# Explanation Gateway & Watchlist Pre-Warming — Implementation Plan

**Status:** READY TO IMPLEMENT  
**Source spec:** `EXPLANATION_GATEWAY_PLAN.md`  
**Author:** Session 2026-06-25

---

## 1. Scope Summary

Feature 1 gates LLM explanation jobs behind `consensus_score >= 75.0` and surfaces a "weak signal" placeholder in the AI panel with a user-driven refresh button that fires a 202-returning bypass endpoint. Feature 2 adds a new supervised worker task (`watchlist_scheduler`) that pre-warms AI market context for all watchlist instruments 4× per trading day at fixed IST times, feeding the existing `context_worker` pipeline.

No DB migrations. No new Redis streams or consumer groups. One new Python file; eight modified files.

---

## 2. Phase Breakdown

### Phase 0 — Foundations (no inter-dependencies; do atomically in one pass each)

**0-A: `backend/app/core/config.py`**  
Pre-check: Line 244 is `CONSENSUS_GATHER_TIMEOUT: float = Field(6.0, ge=3.0, le=15.0)`. Insert after line 244, before the `# Per-symbol forecast cache` comment at line 245. Add explanation gate + watchlist scheduler settings in one contiguous block.

**0-B: `backend/app/core/metrics.py`**  
Pre-check: Find the last metric definition before `def init_metrics`. Insert four new watchlist metrics immediately before `init_metrics`.

### Phase 1 — Backend Core (depends on Phase 0)

**1-A: `backend/app/ai/correlation/engine.py`** — consensus gate  
Pre-check: XADD block at lines 1062–1085. `get_settings` is NOT imported at module top — use lazy import inside the try block (same pattern as `RedisStreams` import on line 1067).

**1-B: `backend/app/ai/intelligence/explanation_worker.py`** — force flag  
Pre-check: `_generate_instrument_context` signature at line 1059; idempotency block at lines 1096–1114; `_process_context_message` at line 1656; field parsing at lines 1680–1697; call site at line 1722. `_write_audit_entry` at line 1270 is in Phase 3 — it remains unconditional.

**1-C: `backend/app/workers/watchlist_context_scheduler.py`** — new file  
No pre-check (file does not exist).

### Phase 2 — Registry + SSE API (depends on Phase 1)

**2-A: `backend/app/workers/registry.py`** — task registration  
Pre-check: `TASK_NAMES` tuple ends at line 82 (`"sl_tp_worker",`). Registry dict ends at line 200. Mismatch guard at line 202. **Both `TASK_NAMES` and `registry` dict must be updated in the same edit** or startup raises `RuntimeError`.

**2-B: `backend/app/api/v1/ai_stream.py`** — weak-signal state + bypass endpoint + poll guard  
Pre-check:
- `_should_apply_polled_explanation` at lines 188–209
- `_build_explanation_payload` at lines 214–246
- `_build_context_payload` at lines 249–273
- `_fetch_explanation_for_instrument` at lines 276–436
- Stage 1 target block at lines 329–352
- SSE handler ends around line 879 — bypass endpoint appended after
- `decode_token`, `CortexInvalidTokenError`, `AsyncSessionLocal`, `select`, `TradeSuggestion`, `get_redis`, `RedisStreams`, `JSONResponse`, `status`, `Query`, `UUID` are ALL already imported — no new imports needed

### Phase 3 — Frontend (depends on Phase 2-B contract)

**3-A: `frontend/src/types/analysis.ts`** — extend ExplanationData  
Pre-check: `ExplanationData` interface closes at line 261. `signal_generated_at` is at line 260.

**3-B: `frontend/src/components/AIExplanationPanel.tsx`** — weak-signal UI  
Pre-check: `memo` only imported from React at line 34. `AlertTriangle` and `Sparkles` already imported (line 35). `PanelFailed` ends at line 176. `AIExplanationPanelProps` at lines 343–347. `AIExplanationPanelComponent` at lines 349–400.

**3-C: `frontend/src/components/AnalysisCardsSection.tsx`** — wire `onRequestExplanation` prop  
Pre-check: Already imports `useAuth` and reads `accessToken` from it (line 146). Find the `<AIExplanationPanel ...>` JSX and add the `onRequestExplanation` callback.

---

## 3. Per-Step Edit Instructions

### Step 0-A — config.py: two new settings blocks

**Location:** After line 244, before line 245.

```python
    # ── Explanation Confidence Gate ────────────────────────────────────────────
    # Minimum consensus_score (0–100) required before the correlation engine
    # enqueues an LLM explanation job. consensus_score = scanner×0.30 + AI×0.40
    # + ML×0.30 (deterministic composite, NOT an LLM-verbalized confidence).
    # Live-data max ≈ 89.7; 75.0 gates the 60–74 band (~56% of signals at
    # current maturity). Raise toward 80 via .env as model scores normalize.
    EXPLANATION_CONSENSUS_THRESHOLD: float = Field(
        75.0,
        ge=50.0,
        le=95.0,
        description=(
            "Minimum consensus_score for automatic LLM explanation generation. "
            "Signals below this show a weak-signal placeholder with a user-driven "
            "refresh button that triggers on-demand explanation at Priority.HIGH."
        ),
    )

    # ── Watchlist Context Scheduler ────────────────────────────────────────────
    # Pre-warms AI context for all watchlist instruments on a fixed intraday
    # schedule. Fires 4× per NSE trading day; only stale instruments are queued.
    WATCHLIST_SCHEDULER_RUN_TIMES_IST: list[str] = Field(
        default=["09:30", "11:00", "13:00", "14:30"],
        description=(
            "IST times (HH:MM) at which the watchlist context scheduler runs. "
            "Must be within market hours. JSON array in .env."
        ),
    )
    WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES: int = Field(
        80,
        ge=10,
        le=180,
        description=(
            "An instrument's context is re-enqueued only if it expires within this "
            "many minutes. Prevents redundant LLM calls for recently generated context."
        ),
    )
    WATCHLIST_SCHEDULER_BATCH_CAP: int = Field(
        200,
        ge=10,
        le=500,
        description="Maximum unique instruments enqueued per scheduler run.",
    )
```

---

### Step 0-B — metrics.py: four watchlist scheduler metrics

**Location:** Find the last metric `Gauge`/`Counter`/`Histogram` definition before `def init_metrics`. Insert before that function:

```python
# ── Watchlist Context Scheduler Metrics ──────────────────────────────────────

watchlist_scheduler_runs_total = Counter(
    'watchlist_scheduler_runs_total',
    'Total watchlist context scheduler batch runs by final status',
    ['status'],  # success | error | skipped_empty
)

watchlist_scheduler_instruments_queued_total = Counter(
    'watchlist_scheduler_instruments_queued_total',
    'Total instrument context jobs enqueued across all watchlist scheduler runs',
)

watchlist_scheduler_duration_seconds = Histogram(
    'watchlist_scheduler_duration_seconds',
    'Wall-clock duration of each watchlist context scheduler batch run (seconds)',
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)

watchlist_scheduler_last_run_timestamp = Gauge(
    'watchlist_scheduler_last_run_timestamp',
    'Unix timestamp of the last successful watchlist scheduler run',
)
```

---

### Step 1-A — engine.py: gate XADD behind consensus_score

**Location:** Lines 1062–1085. Replace the entire XADD try/except block with:

```python
        # Trigger async LLM explanation generation — gated by consensus_score.
        # Only enqueue if consensus_score meets EXPLANATION_CONSENSUS_THRESHOLD.
        # Signals below the gate render a weak-signal placeholder in the AI panel;
        # users can still request on-demand explanation via the bypass endpoint.
        try:
            from app.core.config import get_settings as _get_settings
            from app.core.redis import RedisStreams
            _threshold = _get_settings().EXPLANATION_CONSENSUS_THRESHOLD
            if float(suggestion.consensus_score) >= _threshold:
                await self.redis.xadd(
                    RedisStreams.EXPLANATION_JOBS,
                    {
                        "suggestion_id":  str(suggestion.suggestion_id),
                        "id":             str(suggestion.id),
                        "instrument_key": suggestion.instrument_key,
                    },
                    maxlen=5000,
                    approximate=True,
                )
                logger.debug(
                    "explanation job enqueued: suggestion=%s consensus_score=%.1f",
                    suggestion.suggestion_id, float(suggestion.consensus_score),
                )
            else:
                logger.info(
                    "explanation job skipped (weak signal): suggestion=%s "
                    "consensus_score=%.1f < threshold=%.1f",
                    suggestion.suggestion_id,
                    float(suggestion.consensus_score),
                    _threshold,
                )
        except Exception as exc:
            logger.warning(
                "Failed to enqueue explanation job for suggestion %s (non-fatal): %s",
                suggestion.suggestion_id, exc,
            )
```

Note: `get_settings` is lazy-imported inside the try block, matching the existing pattern (`from app.core.redis import RedisStreams` on line 1067).

---

### Step 1-B — explanation_worker.py: force flag (4 edits)

**Edit 1: `_generate_instrument_context` signature** (line 1059) — add `force: bool = False`:

```python
async def _generate_instrument_context(
    instrument_key: str,
    symbol: str | None,
    ml_snapshot: dict | None,
    lock_key: str | None = None,
    lock_token: str | None = None,
    force: bool = False,
) -> None:
```

**Edit 2: idempotency block** (lines 1096–1114) — wrap with `if not force`:

```python
        # Idempotency check — skip when force=True (scheduler controls cadence).
        if not force:
            existing = await db.execute(
                select(AIInstrumentContext).where(
                    AIInstrumentContext.instrument_key == instrument_key,
                    AIInstrumentContext.expires_at > datetime.now(timezone.utc),
                )
            )
            if existing.scalar_one_or_none() is not None:
                logger.info(
                    "explanation_worker: context idempotency — unexpired record exists "
                    "for %s, skipping Gemini call (PEL re-delivery after Phase-3 crash)",
                    instrument_key,
                )
                llm_explanation_dedup_total.labels(layer="db_idempotency").inc()
                return
        else:
            logger.debug(
                "explanation_worker: force=True — bypassing idempotency check for %s "
                "(scheduler-initiated refresh)",
                instrument_key,
            )
```

**Edit 3: field parsing in `_process_context_message`** (after line 1694, in the fields parse try block) — add two lines after the `ml_snapshot` parsing:

```python
        force = fields.get("force", "0") == "1"
        source = fields.get("source", "on_demand")
```

**Edit 4: call site** (line 1722) — pass `force=force`:

```python
        await _generate_instrument_context(
            instrument_key, sym, ml_snapshot, lock_key, lock_token, force=force
        )
```

---

### Step 1-C — watchlist_context_scheduler.py: new file

Create `/home/preet/code/Cortex_Merge_AI-ML/backend/app/workers/watchlist_context_scheduler.py`:

```python
"""
Watchlist Context Scheduler
============================
Pre-warms AI market context for all instruments held in any user's watchlist,
running 4× per NSE trading day at configurable fixed IST wall-clock times.

Architecture
------------
  Pure asyncio while-True loop with cooperative sleep — matches every other
  worker in this codebase. Reads all distinct instrument_keys from
  watchlist_items, skips fresh instruments, XADDs stale ones to
  cortex:stream:context:jobs with force="1" so the context_worker regenerates
  even if unexpired context exists.

Market hours guard
------------------
  Scheduled runs only fire during NSE market hours (NSE_MARKET_OPEN_IST to
  NSE_MARKET_CLOSE_IST). Admin-triggered runs (TriggerToken) bypass this guard.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.metrics import (
    watchlist_scheduler_duration_seconds,
    watchlist_scheduler_instruments_queued_total,
    watchlist_scheduler_last_run_timestamp,
    watchlist_scheduler_runs_total,
)
from app.core.redis import RedisStreams
from app.workers.supervisor import PauseToken, TriggerToken

logger = logging.getLogger(__name__)

_STREAM_MAXLEN_CONTEXT = 1_000
_IST_OFFSET = timedelta(hours=5, minutes=30)
_SCHEDULE_POLL_SECS = 30


def _now_ist() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(_IST_OFFSET))


def _parse_run_times(times_ist: list[str]) -> list[tuple[int, int]]:
    parsed: list[tuple[int, int]] = []
    for t in times_ist:
        try:
            h, m = t.split(":")
            parsed.append((int(h), int(m)))
        except (ValueError, AttributeError):
            logger.warning(
                "watchlist_scheduler: ignoring malformed run time %r — expected HH:MM", t
            )
    return sorted(parsed)


def _is_market_hours(now_ist: datetime) -> bool:
    settings = get_settings()
    open_h, open_m   = map(int, settings.NSE_MARKET_OPEN_IST.split(":"))
    close_h, close_m = map(int, settings.NSE_MARKET_CLOSE_IST.split(":"))
    now_min   = now_ist.hour * 60 + now_ist.minute
    open_min  = open_h * 60 + open_m
    close_min = close_h * 60 + close_m
    return open_min <= now_min <= close_min


def _seconds_until_next_run(
    now_ist: datetime,
    run_times: list[tuple[int, int]],
) -> float:
    today = now_ist.date()
    candidates: list[datetime] = []
    for h, m in run_times:
        dt = datetime(today.year, today.month, today.day, h, m, 0, tzinfo=now_ist.tzinfo)
        if dt > now_ist:
            candidates.append(dt)
    if not candidates:
        tomorrow = today + timedelta(days=1)
        h, m = run_times[0]
        dt = datetime(tomorrow.year, tomorrow.month, tomorrow.day, h, m, 0, tzinfo=now_ist.tzinfo)
        candidates.append(dt)
    return (min(candidates) - now_ist).total_seconds()


class WatchlistContextScheduler:
    """
    Pre-warm watchlist instrument contexts on a fixed intraday schedule.

    Registered as a supervised() task in the worker registry. Responds to
    PauseToken (pause/resume) and TriggerToken (immediate run) from the
    control plane — same contract as all native worker tasks.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        redis: Any,
        shutdown: asyncio.Event,
        pause: PauseToken,
        trigger: TriggerToken,
    ) -> None:
        self._session_factory = session_factory
        self._redis            = redis
        self._shutdown         = shutdown
        self._pause            = pause
        self._trigger          = trigger

    async def run(self) -> None:
        settings  = get_settings()
        run_times = _parse_run_times(settings.WATCHLIST_SCHEDULER_RUN_TIMES_IST)

        if not run_times:
            logger.error(
                "watchlist_scheduler: no valid run times configured — task exiting. "
                "Fix WATCHLIST_SCHEDULER_RUN_TIMES_IST in .env."
            )
            return

        logger.info(
            "watchlist_scheduler: starting. schedule=%s (IST)",
            settings.WATCHLIST_SCHEDULER_RUN_TIMES_IST,
        )

        while not self._shutdown.is_set():
            await self._pause.checkpoint()

            now_ist = _now_ist()
            secs    = _seconds_until_next_run(now_ist, run_times)

            logger.debug(
                "watchlist_scheduler: next run in %.0fs (at %s IST)",
                secs,
                (now_ist + timedelta(seconds=secs)).strftime("%H:%M"),
            )

            triggered = await self._trigger.wait_or_timeout(max(secs, 1.0))

            if self._shutdown.is_set():
                break

            # Market hours guard: skip scheduled runs outside market hours.
            # Admin-triggered runs (triggered=True) bypass this guard.
            if not triggered and not _is_market_hours(_now_ist()):
                logger.debug("watchlist_scheduler: outside market hours — skipping run")
                continue

            await self._run_batch()

    async def _run_batch(self) -> None:
        import time as _time
        t0       = _time.monotonic()
        settings = get_settings()
        margin   = timedelta(minutes=settings.WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES)
        cap      = settings.WATCHLIST_SCHEDULER_BATCH_CAP
        now_utc  = datetime.now(timezone.utc)

        logger.info("watchlist_scheduler: run starting at %s IST", _now_ist().strftime("%H:%M"))

        try:
            async with self._session_factory() as db:
                rows = (await db.execute(
                    text("SELECT DISTINCT instrument_key FROM watchlist_items")
                )).all()
            all_keys = [r[0] for r in rows if r[0]]

            if not all_keys:
                logger.info("watchlist_scheduler: no watchlist instruments — skipping run")
                watchlist_scheduler_runs_total.labels(status="skipped_empty").inc()
                watchlist_scheduler_last_run_timestamp.set(now_utc.timestamp())
                return

            from app.ai.fusion.models import AIInstrumentContext
            async with self._session_factory() as db:
                fresh_stmt = select(AIInstrumentContext.instrument_key).where(
                    AIInstrumentContext.expires_at > now_utc + margin
                )
                fresh_keys = {r[0] for r in (await db.execute(fresh_stmt)).all()}

            stale_keys = [k for k in all_keys if k not in fresh_keys]

            if not stale_keys:
                logger.info(
                    "watchlist_scheduler: all %d instruments are fresh — no jobs enqueued",
                    len(all_keys),
                )
                watchlist_scheduler_runs_total.labels(status="success").inc()
                watchlist_scheduler_last_run_timestamp.set(now_utc.timestamp())
                return

            to_enqueue = stale_keys[:cap]
            if len(stale_keys) > cap:
                logger.warning(
                    "watchlist_scheduler: %d stale instruments exceeds batch cap %d — "
                    "enqueuing first %d; remainder refreshed next run",
                    len(stale_keys), cap, cap,
                )

            enqueued = 0
            for key in to_enqueue:
                symbol = key.split("|")[-1] if "|" in key else key
                try:
                    await self._redis.xadd(
                        RedisStreams.CONTEXT_JOBS,
                        {
                            "instrument_key":  key,
                            "symbol":          symbol,
                            "prediction_data": "",
                            "lock_key":        "",
                            "lock_token":      "",
                            "force":           "1",
                            "source":          "watchlist_scheduler",
                        },
                        maxlen=_STREAM_MAXLEN_CONTEXT,
                        approximate=True,
                    )
                    enqueued += 1
                except Exception as exc:
                    logger.warning(
                        "watchlist_scheduler: XADD failed for instrument=%s: %s", key, exc
                    )

            duration = _time.monotonic() - t0
            watchlist_scheduler_runs_total.labels(status="success").inc()
            watchlist_scheduler_instruments_queued_total.inc(enqueued)
            watchlist_scheduler_duration_seconds.observe(duration)
            watchlist_scheduler_last_run_timestamp.set(now_utc.timestamp())

            logger.info(
                "watchlist_scheduler: run complete — "
                "total=%d fresh=%d stale=%d enqueued=%d duration_ms=%d",
                len(all_keys), len(fresh_keys), len(stale_keys),
                enqueued, int(duration * 1000),
            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            watchlist_scheduler_runs_total.labels(status="error").inc()
            logger.error("watchlist_scheduler: batch run failed: %s", exc, exc_info=True)
```

---

### Step 2-A — registry.py: register watchlist_scheduler

**Edit 1:** In `TASK_NAMES` tuple, add after `"sl_tp_worker"` (line 81):

```python
    "watchlist_scheduler",   # pause/trigger-aware via WatchlistContextScheduler
```

**Edit 2:** In `build_task_registry`, after the `fundamentals_scheduler` instantiation block (lines 139–145), add:

```python
    from app.workers.watchlist_context_scheduler import WatchlistContextScheduler
    watchlist_scheduler_instance = WatchlistContextScheduler(
        session_factory=session_factory,
        redis=redis_client._redis,
        shutdown=shutdown,
        pause=_state("watchlist_scheduler").pause_token,
        trigger=_state("watchlist_scheduler").trigger_token,
    )
```

**Edit 3:** In the `registry` dict, add after the `"sl_tp_worker"` entry (line 199):

```python
        "watchlist_scheduler": lambda: watchlist_scheduler_instance.run(),
```

**Edit 4:** Update the module docstring count from "15 background tasks" (line 6) to "16 background tasks" and list `watchlist_scheduler` in the Native section comment (line 22).

**Verification:** After both edits, `set(registry.keys()) == set(TASK_NAMES)` must hold. The guard at line 202 will catch any mismatch at startup.

---

### Step 2-B — ai_stream.py: four edits

**Edit 1: add `_build_weak_signal_payload` helper** — insert after `_build_context_payload` (which ends at line 273), before `_fetch_explanation_for_instrument` (line 276):

```python
def _build_weak_signal_payload(suggestion: TradeSuggestion) -> dict[str, Any]:
    """
    Payload emitted when a suggestion's consensus_score is below
    EXPLANATION_CONSENSUS_THRESHOLD.  ``weak_signal=True`` is the frontend
    discriminator; ``suggestion_id`` enables the user-driven bypass button.
    """
    return {
        "available":           False,
        "failed":              False,
        "weak_signal":         True,
        "suggestion_id":       str(suggestion.suggestion_id),
        "consensus_score":     float(suggestion.consensus_score),
        "summary":             None,
        "full_explanation":    None,
        "model":               None,
        "generated_at":        None,
        "sources":             [],
        "context_type":        "suggestion_explanation",
        "signal_direction":    suggestion.signal_direction,
        "signal_generated_at": (
            suggestion.created_at.isoformat() if suggestion.created_at else None
        ),
    }
```

**Edit 2: update Stage 1 weak-signal branch** — inside `_fetch_explanation_for_instrument`, after line 332 (`if has_explanation or is_active:`), insert before the existing `if has_explanation:` SSE check (line 335):

```python
                # Weak-signal gate: active but explanation was intentionally skipped
                # because consensus_score < EXPLANATION_CONSENSUS_THRESHOLD.
                if is_active and not has_explanation:
                    from app.core.config import get_settings as _cfg
                    if float(suggestion.consensus_score) < _cfg().EXPLANATION_CONSENSUS_THRESHOLD:
                        return _build_weak_signal_payload(suggestion)
```

**Edit 3: update `_should_apply_polled_explanation`** (lines 188–209) — add a weak-signal non-downgrade guard. Find the function and add before the final `return True`:

```python
    # A weak_signal poll must not overwrite a successfully delivered explanation
    # or a streaming state (e.g., user clicked refresh, explanation arrived via push).
    if polled is not None and polled.get("weak_signal") and current is not None:
        if current.get("available") or current.get("streaming"):
            return False
```

**Edit 4: bypass endpoint** — append after the SSE endpoint handler (after line 879). All needed symbols (`decode_token`, `CortexInvalidTokenError`, `AsyncSessionLocal`, `select`, `TradeSuggestion`, `get_redis`, `RedisStreams`, `JSONResponse`, `status`, `Query`, `UUID`) are already imported — no new imports:

```python
# ── On-demand explanation bypass ───────────────────────────────────────────────
_BYPASS_DEBOUNCE_TTL_SECS = 60
_BYPASS_DEBOUNCE_KEY = "cortex:explanation:bypass:{suggestion_id}"


@router.post(
    "/explanation/{suggestion_id}/request",
    summary="Request on-demand LLM explanation for a weak-signal suggestion",
    status_code=202,
)
async def request_explanation(
    suggestion_id: str,
    token: str = Query(..., description="JWT access token"),
) -> JSONResponse:
    """
    Enqueues an on-demand explanation job for a suggestion gated by the consensus
    threshold. 60-second per-suggestion debounce prevents quota abuse.
    Result delivered via SSE push (cortex:llm:explanation:ready:{suggestion_id}).
    """
    try:
        decode_token(token, expected_type="access")
    except CortexInvalidTokenError as exc:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_token", "message": str(exc)},
        )

    try:
        _uuid = UUID(suggestion_id)
    except (ValueError, AttributeError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid_suggestion_id"},
        )

    redis = get_redis()
    debounce_key = _BYPASS_DEBOUNCE_KEY.format(suggestion_id=suggestion_id)
    acquired = await redis.set(debounce_key, "1", nx=True, ex=_BYPASS_DEBOUNCE_TTL_SECS)
    if not acquired:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "rate_limited",
                "message": f"Wait {_BYPASS_DEBOUNCE_TTL_SECS}s before retrying.",
            },
        )

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TradeSuggestion).where(TradeSuggestion.suggestion_id == _uuid)
            )
            suggestion = result.scalar_one_or_none()

        if suggestion is None:
            await redis.delete(debounce_key)
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "suggestion_not_found"},
            )
        if suggestion.status != "active":
            await redis.delete(debounce_key)
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "suggestion_not_active", "status": suggestion.status},
            )
        if suggestion.llm_summary is not None:
            await redis.delete(debounce_key)
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": "explanation_already_exists"},
            )
    except Exception as exc:
        await redis.delete(debounce_key)
        logger.error("on-demand bypass: DB check failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "internal_error"},
        )

    try:
        await redis.xadd(
            RedisStreams.EXPLANATION_JOBS,
            {
                "suggestion_id":  suggestion_id,
                "id":             str(suggestion.id),
                "instrument_key": suggestion.instrument_key,
            },
            maxlen=5000,
            approximate=True,
        )
        logger.info(
            "on-demand bypass enqueued: suggestion=%s instrument=%s",
            suggestion_id, suggestion.instrument_key,
        )
    except Exception as exc:
        await redis.delete(debounce_key)
        logger.error("on-demand bypass: XADD failed: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "queue_error"},
        )

    return JSONResponse(
        status_code=202,
        content={
            "queued":        True,
            "suggestion_id": suggestion_id,
            "message":       "Watch the SSE stream for delivery.",
        },
    )
```

---

### Step 3-A — types/analysis.ts: extend ExplanationData

**Location:** After line 260 (`signal_generated_at: string | null;`), before line 261 (closing `}`):

```typescript
  /**
   * True when the suggestion's consensus_score was below EXPLANATION_CONSENSUS_THRESHOLD.
   * Mutually exclusive with ``available: true`` and ``failed: true``.
   * The panel renders a "weak signal" placeholder with a user-driven refresh button.
   */
  weak_signal?:     boolean;

  /**
   * UUID of the suggestion this payload belongs to.
   * Populated in the weak_signal state so the frontend can call the bypass endpoint.
   */
  suggestion_id?:   string;

  /**
   * Actual consensus_score (0–100) of the gated suggestion.
   */
  consensus_score?: number;
```

---

### Step 3-B — AIExplanationPanel.tsx: weak-signal UI (4 edits)

**Edit 1: update React import** (line 34):

```tsx
import { memo, useState, useCallback } from 'react';
```

**Edit 2: add `PanelWeakSignal` component** — insert after `PanelFailed` (which ends at line 176), before `interface SourcesListProps` (line 178):

```tsx
interface PanelWeakSignalProps {
  data: ExplanationData;
  onRefresh: () => void;
  refreshing: boolean;
}

function PanelWeakSignal({ data, onRefresh, refreshing }: PanelWeakSignalProps) {
  const score = data.consensus_score != null ? Math.round(data.consensus_score) : null;
  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base font-semibold text-slate-700">
          <Brain className="h-5 w-5 text-slate-400" />
          AI Analysis
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <div
          className="flex flex-col gap-3 rounded-md border border-slate-200 bg-slate-50/80 px-4 py-3"
          role="status"
          aria-label="Signal confidence below threshold"
        >
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
            <div className="space-y-1">
              <p className="text-sm font-medium text-slate-700">
                Signal detected — confidence below AI analysis threshold
              </p>
              <p className="text-xs text-slate-500 leading-relaxed">
                {score != null ? `Consensus score: ${score}/100. ` : ''}
                If this feels like a valid signal, you can request a full AI explanation below.
              </p>
            </div>
          </div>
          <button
            onClick={onRefresh}
            disabled={refreshing}
            className={cn(
              "self-start flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors",
              refreshing
                ? "bg-slate-100 text-slate-400 cursor-not-allowed"
                : "bg-violet-50 text-violet-700 hover:bg-violet-100 border border-violet-200"
            )}
            aria-label="Request AI explanation for this signal"
          >
            <Sparkles className={cn("h-3.5 w-3.5", refreshing && "animate-pulse")} />
            {refreshing ? "Requesting…" : "Request AI Explanation"}
          </button>
        </div>
      </CardContent>
    </Card>
  );
}
```

**Edit 3: update `AIExplanationPanelProps`** (lines 343–347) — add optional prop:

```tsx
interface AIExplanationPanelProps {
  data: ExplanationData | null;
  isLoading: boolean;
  className?: string;
  onRequestExplanation?: () => Promise<void>;
}
```

**Edit 4: update `AIExplanationPanelComponent`** (lines 349–400) — add state, handler, and weak-signal branch:

```tsx
function AIExplanationPanelComponent({
  data,
  isLoading,
  className,
  onRequestExplanation,
}: AIExplanationPanelProps) {
  const [refreshing, setRefreshing] = useState(false);

  const handleWeakSignalRefresh = useCallback(async () => {
    if (!onRequestExplanation || refreshing) return;
    setRefreshing(true);
    try {
      await onRequestExplanation();
    } catch {
      // Non-fatal: user can click again after the 60s debounce expires.
    } finally {
      setRefreshing(false);
    }
  }, [onRequestExplanation, refreshing]);

  if (!isLoading && data === null) return null;
  if (isLoading && data === null) {
    return <div className={cn(className)}><PanelSkeleton /></div>;
  }
  if (data !== null && data.failed) {
    return <div className={cn(className)}><PanelFailed /></div>;
  }
  if (data !== null && data.weak_signal) {
    return (
      <div className={cn(className)}>
        <PanelWeakSignal
          data={data}
          onRefresh={handleWeakSignalRefresh}
          refreshing={refreshing}
        />
      </div>
    );
  }
  if (data !== null && !data.available && !data.full_explanation) {
    return <div className={cn(className)}><PanelSkeleton /></div>;
  }
  if (data !== null && data.full_explanation) {
    return (
      <div className={cn(className)}>
        <ExplanationContent data={data as ExplanationData & { full_explanation: string }} />
      </div>
    );
  }
  return null;
}
```

---

### Step 3-C — AnalysisCardsSection.tsx: wire onRequestExplanation

**Context:** This file already imports `useAuth` and reads `accessToken` at line 146. Find the `<AIExplanationPanel ...>` JSX render and add the `onRequestExplanation` prop:

```tsx
<AIExplanationPanel
  data={explanationData}
  isLoading={isExplanationLoading}
  onRequestExplanation={
    explanationData?.weak_signal && explanationData.suggestion_id
      ? async () => {
          const url =
            `/api/v1/ai/explanation/${explanationData.suggestion_id}/request` +
            `?token=${encodeURIComponent(accessToken ?? '')}`;
          const res = await fetch(url, { method: 'POST' });
          if (!res.ok && res.status !== 429) {
            throw new Error(`bypass failed: ${res.status}`);
          }
        }
      : undefined
  }
/>
```

The callback is `undefined` when `weak_signal` is not set — the panel renders without the refresh button on normal paths.

---

## 4. Invariants to Verify

**Invariant 1 — No DB connection held across LLM calls**  
`_generate_instrument_context` has three explicit phases. The Phase 1 `async with` block (lines 1095–1131) closes before the Phase 2 LLM call. The `force=True` early-return bypass lives inside Phase 1 — it doesn't alter the phase structure. The `_write_audit_entry` call at line 1270 is in Phase 3's own `async with` block. ✓

**Invariant 2 — XACK after processing only**  
The scheduler only XADDs to `CONTEXT_JOBS`. It never touches XACK, XREADGROUP, or consumer group semantics. The context_worker exclusively owns those. ✓

**Invariant 3 — Audit log is mandatory**  
`_write_audit_entry` at line 1270 is in Phase 3's `async with` block. The only early return added is in Phase 1's idempotency check (blocked by `force=True`). On the `force=True` path, execution proceeds past Phase 1 idempotency into Phase 2 and Phase 3 as normal. ✓

**Invariant 4 — Poll path must never downgrade a richer push state**  
`_should_apply_polled_explanation` is extended with an explicit `weak_signal` guard. A poll returning `weak_signal=True` will not overwrite `current` state where `available` or `streaming` is true. ✓

**Invariant 5 — Consumer group registration**  
`CONTEXT_JOBS` and `EXPLANATION_JOBS` already have consumer groups registered at startup (redis.py line 427). No new consumer groups needed. ✓

**Invariant 6 — Supervisor restart correctness**  
`watchlist_scheduler_instance` is created once at `build_task_registry` call time. The lambda `lambda: watchlist_scheduler_instance.run()` returns a fresh coroutine on each supervisor restart. `run()` has no persistent mutable state — it recalculates `_seconds_until_next_run` at the top of each loop. ✓

---

## 5. Frontend Token Wiring

The spec's original draft used `window.__CORTEX_TOKEN__` — **do not use this**. It is not how this codebase manages tokens.

**Correct pattern:** `AIExplanationPanel` receives an optional `onRequestExplanation?: () => Promise<void>` prop. The panel has no auth dependency — it only calls the callback when the user clicks the button. The parent `AnalysisCardsSection` constructs the callback using `accessToken` from its existing `useAuth()` call (line 146).

This keeps `AIExplanationPanel` a pure presentation component, consistent with the codebase pattern.

---

## 6. Post-Implementation Checklist

### Smoke Test 1 — Weak signal end-to-end
1. Trigger a signal with `consensus_score < 75`. Confirm with `XLEN cortex:stream:explanation:jobs` — count must NOT increase for this suggestion.
2. Open SSE stream. Verify panel renders "weak signal" card with score and "Request AI Explanation" button.
3. Click button. Verify `POST /api/v1/ai/explanation/{id}/request?token=...` returns `202`.
4. Verify `XLEN cortex:stream:explanation:jobs` incremented by 1.
5. Wait 10–15s. Verify SSE delivers `available=true` — panel transitions to `ExplanationContent`.
6. Click button again within 60s. Verify `429` response.

### Smoke Test 2 — Normal signal path (regression check)
1. Trigger signal with `consensus_score >= 75`. Verify `XLEN cortex:stream:explanation:jobs` incremented.
2. SSE panel shows generating skeleton → transitions to content. No change from pre-feature behavior.

### Smoke Test 3 — Watchlist scheduler manual trigger
1. `POST http://worker:8001/tasks/watchlist_scheduler/trigger`
2. Verify `XLEN cortex:stream:context:jobs` increased by the count of stale watchlist instruments.
3. After context_worker processes: verify `ai_instrument_context` rows have `expires_at ≈ now + 2h`.
4. Open SSE for a watchlist instrument. Verify Stage 2 serves cached context — no Stage 3 XADD fired.

### Smoke Test 4 — Scheduler metrics
1. After trigger run, check Prometheus: `watchlist_scheduler_runs_total{status="success"}` incremented, `watchlist_scheduler_instruments_queued_total` incremented, `watchlist_scheduler_last_run_timestamp` updated within 60s.
2. Trigger when all instruments fresh: verify `status="success"` still increments and `instruments_queued_total` does NOT change.

### Smoke Test 5 — Debounce key cleanup on errors
1. Call bypass endpoint with non-existent `suggestion_id`. Verify `404`.
2. Verify debounce key was deleted (immediate retry must NOT be rate-limited).

### Smoke Test 6 — Registry integrity
1. Start worker sidecar. Confirm no `RuntimeError: Registry/TASK_NAMES mismatch` in logs.
2. Verify `watchlist_scheduler` appears in worker task list.

---

## 7. Files Touched Summary

| File | Type | Phase |
|------|------|-------|
| `backend/app/core/config.py` | Modified | 0-A |
| `backend/app/core/metrics.py` | Modified | 0-B |
| `backend/app/ai/correlation/engine.py` | Modified | 1-A |
| `backend/app/ai/intelligence/explanation_worker.py` | Modified | 1-B |
| `backend/app/workers/watchlist_context_scheduler.py` | **NEW** | 1-C |
| `backend/app/workers/registry.py` | Modified | 2-A |
| `backend/app/api/v1/ai_stream.py` | Modified | 2-B |
| `frontend/src/types/analysis.ts` | Modified | 3-A |
| `frontend/src/components/AIExplanationPanel.tsx` | Modified | 3-B |
| `frontend/src/components/AnalysisCardsSection.tsx` | Modified | 3-C |

**Total: 9 modified + 1 new file. No DB migrations. No new Docker services. No new Redis streams or consumer groups.**
