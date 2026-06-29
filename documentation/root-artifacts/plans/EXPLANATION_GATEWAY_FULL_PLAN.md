# Explanation Gateway & Watchlist Pre-Warming — Full Implementation Plan

**Status:** PLAN — not yet implemented  
**Scope:** Two coordinated features that refine when and how LLM explanation generation is triggered  
**Author:** Session 2026-06-25 — produced from deep codebase read + deep-research workflow (107 agents, 1.8M tokens)

---

## 1. Problem Statement

The current system fires one Gemini LLM call for every trade suggestion committed to the DB, regardless of signal quality. This means:

- **Quota waste**: Low-confidence suggestions (consensus_score 60–74) consume ~40% of daily Gemini budget while producing the least actionable AI analysis.
- **Blind watchlist**: Watchlist instruments with no active signal have no pre-warmed AI context. The first user to open a watchlist item waits 10–15 s for on-demand generation — a poor first impression.
- **No user agency**: When an explanation is not generated (or generation fails), the user has no escape hatch. There is no way to request an explanation manually.

---

## 2. Solution Overview

### Feature 1 — Consensus Gate with On-Demand Bypass

Gate `XADD` to `cortex:stream:explanation:jobs` behind `consensus_score >= 75.0` (configurable). Signals below the threshold show a "weak signal" placeholder with a **refresh button** that fires an on-demand explanation job at `Priority.HIGH`.

### Feature 2 — Collective Watchlist Context Scheduler

A new worker task (`watchlist_scheduler`) pre-warms AI context for every instrument across all users' watchlists, running 4× per trading day at fixed IST wall-clock times (09:30 / 11:00 / 13:00 / 14:30). Jobs fan into the existing `context_worker` pipeline; all open SSE connections receive the result via the existing pub/sub routing-signal path.

---

## 3. Architecture Map (Existing System — Read Before Changing Anything)

```
SIGNAL CREATION PATH
─────────────────────
engine.py _compute_consensus()
  → commits TradeSuggestion (consensus_score, signal_direction, confidence_level)
  → XADD cortex:stream:explanation:jobs  ← FEATURE 1 GATE GOES HERE
  → PUBLISH cai:suggestions:new (WS fan-out)

EXPLANATION DELIVERY PATH (two complementary sub-paths)
────────────────────────────────────────────────────────
Push path (real-time):
  explanation_worker (2 instances, Priority.HIGH)
    → XREADGROUP cortex:stream:explanation:jobs
    → _generate_explanation() [RAG + Gemini structured output]
    → UPDATE trade_suggestions (llm_summary, llm_explanation)
    → XADD cortex:sse:events:{suggestion_id}    ← SSE event store (24h TTL)
    → PUBLISH cortex:llm:explanation:ready:{suggestion_id}  ← routing signal
      → ai_stream.py _watch_explanations() receives
      → reads SSE event store
      → _emit_update() → frontend

Poll path (30s fallback):
  ai_stream.py _refresh_explanation()
    → _fetch_explanation_for_instrument() [3-stage lookup]
      Stage 1: recent suggestion with explanation → return payload
      Stage 2: non-expired ai_instrument_context → return payload
      Stage 3: XADD cortex:stream:context:jobs + return pending payload

WATCHLIST CONTEXT PATH (on-demand, Stage 3 triggered)
──────────────────────────────────────────────────────
ai_stream.py Stage 3
  → SET NX EX 45 cortex:instrument_context:generating:{key}  ← lock
  → XADD cortex:stream:context:jobs

context_worker (1 instance, Priority.LOW)
  → XREADGROUP cortex:stream:context:jobs
  → _generate_instrument_context() [RAG + Gemini structured output]
  → UPSERT ai_instrument_context (expires_at = now + 2h)
  → XADD cortex:sse:events:ctx:{instrument_key}
  → PUBLISH cortex:llm:context:ready:{instrument_key}
    → ai_stream.py _watch_explanations() receives
    → reads SSE context event store
    → _emit_update() → frontend
```

---

## 4. Feature 1 — Consensus Gate with On-Demand Bypass

### 4.1 Threshold Basis

`consensus_score` is a deterministic weighted composite:  
`scanner × 0.30 + AI × 0.40 + ML × 0.30`

This is NOT a raw LLM confidence score (calibration warnings from research do not apply). Live production data: max consensus_score ≈ 89.7. Threshold = **75.0** gates out the 60–74 band (~56% of signals at current system maturity) without touching the high-conviction signals that make AI analysis most valuable.

When the model suite matures and scores normalise upward, raise the threshold toward 80 via `.env` — no code change required.

### 4.2 File Changes — Feature 1

---

#### FILE 1: `backend/app/core/config.py`

**Location:** After the `CONSENSUS_GATHER_TIMEOUT` block (line ~244), before `# ── Signal Scheduler`.

**Add a new settings section:**

```python
# ── Explanation Confidence Gate ────────────────────────────────────────────
# Minimum consensus_score (0–100) required before the correlation engine
# enqueues an LLM explanation job for a generated suggestion.
#
# consensus_score = scanner×0.30 + AI×0.40 + ML×0.30 (deterministic, NOT
# an LLM-verbalized confidence score — calibration concerns do not apply).
# Signals below this threshold render a "weak signal" placeholder in the AI
# panel; users can manually request an explanation via the refresh button
# (POST /api/v1/ai/explanation/{suggestion_id}/request), which queues the
# job at Priority.HIGH regardless of score.
#
# Threshold basis: live-data max consensus_score ≈ 89.7. 75.0 gates the
# 60–74 band (~56% of signals at current system maturity) while preserving
# explanation generation for high-conviction signals. Raise toward 80 as
# model scores normalise upward — a single .env change, no code required.
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
```

Also add the Watchlist Scheduler settings in the same pass (see §5.2 File 1).

---

#### FILE 2: `backend/app/ai/correlation/engine.py`

**Location:** The XADD block starting at line 1062.

**Current code:**
```python
# Trigger async LLM explanation generation — durable Redis Stream delivery.
try:
    from app.core.redis import RedisStreams
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
        "explanation job enqueued for suggestion %s", suggestion.suggestion_id
    )
except Exception as exc:
    logger.warning(
        "Failed to enqueue explanation job for suggestion %s (non-fatal): %s",
        suggestion.suggestion_id, exc,
    )
```

**Replace with:**
```python
# Trigger async LLM explanation generation — gated by consensus_score.
# Only enqueue if consensus_score meets the configured threshold. Signals
# below the gate show a weak-signal placeholder in the AI panel; the user
# can still request an on-demand explanation via the refresh button.
try:
    from app.core.config import get_settings as _get_settings
    from app.core.redis import RedisStreams
    _settings = _get_settings()
    _threshold = _settings.EXPLANATION_CONSENSUS_THRESHOLD
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

**Note on imports:** `get_settings` is already imported at module top in most files. Confirm with grep — if already imported at top of engine.py, use the module-level import rather than the lazy one shown above.

---

#### FILE 3: `backend/app/api/v1/ai_stream.py`

**Two changes:**

**Change A — `_build_weak_signal_payload()` helper (add after `_build_context_payload`):**

```python
def _build_weak_signal_payload(suggestion: TradeSuggestion) -> dict[str, Any]:
    """
    Payload emitted when a suggestion's consensus_score is below the
    EXPLANATION_CONSENSUS_THRESHOLD — the AI panel shows a "weak signal"
    placeholder with a user-driven refresh button.

    ``weak_signal=True`` is the frontend discriminator for this state.
    ``suggestion_id`` is included so the frontend can fire the bypass request.
    ``consensus_score`` is exposed so the frontend can show the actual value.
    """
    return {
        "available":          False,
        "failed":             False,
        "weak_signal":        True,
        "suggestion_id":      str(suggestion.suggestion_id),
        "consensus_score":    float(suggestion.consensus_score),
        "summary":            None,
        "full_explanation":   None,
        "model":              None,
        "generated_at":       None,
        "sources":            [],
        "context_type":       "suggestion_explanation",
        "signal_direction":   suggestion.signal_direction,
        "signal_generated_at": (
            suggestion.created_at.isoformat() if suggestion.created_at else None
        ),
    }
```

**Change B — Modify Stage 1 inside `_fetch_explanation_for_instrument()`:**

Find the block (around line 329–347) where Stage 1 checks `has_explanation` and `is_active`. Replace:

```python
if suggestion is not None:
    has_explanation = suggestion.llm_summary is not None
    is_active       = suggestion.status == "active"
    if has_explanation or is_active:
        # ... existing SSE event store check + return _build_explanation_payload
```

With:

```python
if suggestion is not None:
    has_explanation = suggestion.llm_summary is not None
    is_active       = suggestion.status == "active"
    if has_explanation or is_active:
        # Weak-signal gate: active suggestion but explanation was intentionally
        # skipped because consensus_score is below EXPLANATION_CONSENSUS_THRESHOLD.
        # Return the weak-signal payload so the frontend renders the placeholder
        # with the refresh button instead of an eternal generating skeleton.
        if is_active and not has_explanation:
            from app.core.config import get_settings as _cfg
            if float(suggestion.consensus_score) < _cfg().EXPLANATION_CONSENSUS_THRESHOLD:
                return _build_weak_signal_payload(suggestion)

        # Normal path: explanation ready or in-flight generation.
        if has_explanation:
            try:
                ess_key = RedisStreams.sse_explanation_key(
                    str(suggestion.suggestion_id)
                )
                entries = await redis.xrevrange(ess_key, max="+", min="-", count=1)
                if entries:
                    _, fields = entries[0]
                    return json.loads(fields["data"])
            except Exception:
                pass
        return _build_explanation_payload(suggestion)
    # Non-active + no explanation → fall through to Stage 2/3
    logger.debug(
        "SSE stage-1 skip: instrument=%s suggestion=%s status=%s "
        "has_explanation=False — falling through to context generation",
        instrument_key, suggestion.suggestion_id, suggestion.status,
    )
```

**Change C — New bypass endpoint (add after the `analysis_stream` router):**

```python
# ── On-demand explanation bypass ───────────────────────────────────────────────
# Rate-limited per suggestion_id (60s debounce via Redis key) to prevent
# quota abuse. Returns 202 immediately; the SSE push path delivers the result.

_BYPASS_DEBOUNCE_TTL_SECS = 60
_BYPASS_DEBOUNCE_KEY = "cortex:explanation:bypass:{suggestion_id}"


@router.post(
    "/explanation/{suggestion_id}/request",
    summary="Request on-demand LLM explanation for a suggestion (weak-signal bypass)",
    status_code=202,
    responses={
        202: {"description": "Explanation job enqueued — result delivered via SSE push"},
        400: {"description": "suggestion_id is not a valid UUID"},
        404: {"description": "Suggestion not found or not active"},
        409: {"description": "Explanation already exists or in-flight"},
        429: {"description": "Request rate-limited — wait 60 s between requests"},
    },
)
async def request_explanation(
    suggestion_id: str,
    token: str = Query(..., description="JWT access token"),
) -> JSONResponse:
    """
    Enqueue an on-demand LLM explanation for a suggestion that was gated by the
    consensus_score threshold or whose pipeline previously failed.

    - Validates the JWT (same as SSE endpoint).
    - 60-second per-suggestion debounce via Redis SET NX EX to prevent quota abuse.
    - Enqueues at Priority.HIGH — same as normal signal explanations.
    - The SSE push path (cortex:llm:explanation:ready:{suggestion_id}) delivers
      the result to all open SSE connections for this instrument.
    """
    # Validate JWT
    try:
        decode_token(token, expected_type="access")
    except CortexInvalidTokenError as exc:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_token", "message": str(exc)},
        )

    # Validate suggestion_id is a UUID
    try:
        from uuid import UUID
        _uuid = UUID(suggestion_id)
    except (ValueError, AttributeError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid_suggestion_id"},
        )

    redis = get_redis()

    # Debounce: prevent duplicate requests within 60 s
    debounce_key = _BYPASS_DEBOUNCE_KEY.format(suggestion_id=suggestion_id)
    acquired = await redis.set(debounce_key, "1", nx=True, ex=_BYPASS_DEBOUNCE_TTL_SECS)
    if not acquired:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": "rate_limited",
                "message": f"Explanation already requested for this suggestion. "
                           f"Wait {_BYPASS_DEBOUNCE_TTL_SECS}s before retrying.",
            },
        )

    # Verify suggestion exists and is active
    try:
        async with AsyncSessionLocal() as db:
            stmt = select(TradeSuggestion).where(
                TradeSuggestion.suggestion_id == _uuid
            )
            result = await db.execute(stmt)
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

    # Enqueue explanation job at HIGH priority
    try:
        await redis.xadd(
            RedisStreams.EXPLANATION_JOBS,
            {
                "suggestion_id":  suggestion_id,
                "id":             str(suggestion.id),
                "instrument_key": suggestion.instrument_key,
                "priority":       "high",   # hint for future priority-aware processing
            },
            maxlen=5000,
            approximate=True,
        )
        logger.info(
            "on-demand explanation bypass: suggestion=%s instrument=%s",
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
            "queued":         True,
            "suggestion_id":  suggestion_id,
            "message":        "Explanation job queued. Watch the SSE stream for delivery.",
        },
    )
```

---

#### FILE 4: `frontend/src/types/analysis.ts`

**Location:** The `ExplanationData` interface (around line 226).

**Add three optional fields** to the existing interface (after `signal_generated_at`):

```typescript
  /**
   * True when the suggestion's consensus_score was below EXPLANATION_CONSENSUS_THRESHOLD.
   * The panel renders a "weak signal" placeholder with a refresh button instead of
   * an eternal skeleton. The user can request an on-demand explanation via the button.
   * Mutually exclusive with ``available: true`` and ``failed: true``.
   */
  weak_signal?:    boolean;

  /**
   * UUID of the suggestion this payload belongs to.
   * Populated in the weak_signal state so the frontend can call the bypass endpoint.
   */
  suggestion_id?:  string;

  /**
   * The actual consensus_score (0–100) of the gated suggestion.
   * Allows the panel to render informational copy ("Signal confidence: 68/100").
   */
  consensus_score?: number;
```

---

#### FILE 5: `frontend/src/components/AIExplanationPanel.tsx`

**Two changes:**

**Change A — New `PanelWeakSignal` sub-component** (add after `PanelFailed`):

```tsx
interface PanelWeakSignalProps {
  data: ExplanationData;
  onRefresh: () => void;
  refreshing: boolean;
}

function PanelWeakSignal({ data, onRefresh, refreshing }: PanelWeakSignalProps) {
  const score = data.consensus_score != null
    ? Math.round(data.consensus_score)
    : null;

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
                {score != null
                  ? `Consensus score: ${score}/100. `
                  : ''}
                If this feels like a valid signal, you can request a full AI
                explanation below.
              </p>
            </div>
          </div>

          <button
            onClick={onRefresh}
            disabled={refreshing}
            className={cn(
              "self-start flex items-center gap-1.5 rounded-md px-3 py-1.5",
              "text-xs font-medium transition-colors",
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

**Change B — Update `AIExplanationPanelComponent` to handle the weak-signal state:**

The component needs local state for the refresh action. Update the component signature and add state + handler:

```tsx
// Add to imports at top
import { memo, useState, useCallback } from 'react';

// Replace the existing component with this version:
function AIExplanationPanelComponent({
  data,
  isLoading,
  className,
}: AIExplanationPanelProps) {
  const [refreshing, setRefreshing] = useState(false);

  const handleWeakSignalRefresh = useCallback(async () => {
    if (!data?.suggestion_id || refreshing) return;
    setRefreshing(true);
    try {
      // The token must be passed from the parent or read from the auth context.
      // This example uses the window.__CORTEX_TOKEN__ global set by the auth layer.
      // Adjust to match your actual token-retrieval pattern.
      const token = (window as any).__CORTEX_TOKEN__ ?? '';
      await fetch(
        `/api/v1/ai/explanation/${data.suggestion_id}/request?token=${encodeURIComponent(token)}`,
        { method: 'POST' },
      );
      // On success, the SSE push path will deliver the explanation.
      // The panel will transition from weak_signal → skeleton → content automatically.
    } catch {
      // Non-fatal: user can click again after debounce expires.
    } finally {
      setRefreshing(false);
    }
  }, [data?.suggestion_id, refreshing]);

  if (!isLoading && data === null) return null;
  if (isLoading && data === null) {
    return <div className={cn(className)}><PanelSkeleton /></div>;
  }
  if (data !== null && data.failed) {
    return <div className={cn(className)}><PanelFailed /></div>;
  }

  // ── NEW: Weak-signal state ───────────────────────────────────────────────
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
  // ── END NEW ──────────────────────────────────────────────────────────────

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

**Important:** The token retrieval pattern (`window.__CORTEX_TOKEN__`) must match how the app currently surfaces the access token to frontend components that need to make API calls. Check the auth layer pattern (likely a React context or a token stored in a hook) and use the same approach. This is the one item that requires frontend-specific knowledge of the auth layer to wire correctly.

---

### 4.3 Transition Flow for Weak-Signal Suggestion

```
Signal committed (consensus_score = 68) — BELOW threshold
  → engine.py: explanation job SKIPPED (logged)
  → ai_stream.py Stage 1: finds active suggestion, consensus 68 < 75
  → returns _build_weak_signal_payload()
    { available: false, weak_signal: true, suggestion_id, consensus_score: 68, ... }
  → frontend: AIExplanationPanel renders PanelWeakSignal

User clicks "Request AI Explanation":
  → POST /api/v1/ai/explanation/{suggestion_id}/request?token=...
  → 202 Accepted — job enqueued at Priority.HIGH
  → frontend: setRefreshing(true) → button disabled briefly

Backend:
  → explanation_worker picks up the XADD
  → _generate_explanation() runs (same pipeline as normal)
  → XADD cortex:sse:events:{suggestion_id}  (full payload)
  → PUBLISH cortex:llm:explanation:ready:{suggestion_id}
    → ai_stream.py _watch_explanations() receives
    → state.explanation = full_payload   (available: true)
    → _emit_update() → frontend

Frontend:
  → SSE analysis_update received
  → data.weak_signal is gone, data.available = true
  → panel transitions to ExplanationContent ✓
```

---

## 5. Feature 2 — Collective Watchlist Context Scheduler

### 5.1 Schedule Design

```
Intraday schedule (IST):
  09:30  First pre-warm — 15 min after open, after initial signal burst settles
  11:00  Second pre-warm — mid-morning refresh
  13:00  Third pre-warm — post-lunch refresh
  14:30  Fourth pre-warm — pre-close refresh (60 min before 15:30 close)

Off-hours behaviour:
  - Scheduler sleeps until next market day at 09:30
  - Admin trigger endpoint bypasses the market-hours guard for manual runs
  - NSECalendarService (already in codebase) handles holiday detection
```

### 5.2 File Changes — Feature 2

---

#### FILE 1: `backend/app/core/config.py` (continuation of Feature 1 addition)

**Add after the `EXPLANATION_CONSENSUS_THRESHOLD` block:**

```python
# ── Watchlist Context Scheduler ────────────────────────────────────────────
# Pre-warms AI context for all watchlist instruments on a fixed intraday
# schedule. Fires 4× per NSE trading day; instruments already holding fresh
# context are skipped to conserve Gemini quota.
#
# Run times: HH:MM 24h IST. All must fall within market hours (09:15–15:30).
# Override via env: WATCHLIST_SCHEDULER_RUN_TIMES_IST='["09:30","11:00"]'
WATCHLIST_SCHEDULER_RUN_TIMES_IST: list[str] = Field(
    default=["09:30", "11:00", "13:00", "14:30"],
    description=(
        "IST times (HH:MM) at which the watchlist context scheduler runs. "
        "Must be within market hours. JSON array in env."
    ),
)
# Instruments with context expiring more than this many minutes in the future
# are considered fresh and NOT re-enqueued. Set to ~10 min less than the
# scheduler interval (~90 min) so every instrument is refreshed each cycle.
WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES: int = Field(
    80,
    ge=10,
    le=180,
    description=(
        "An instrument's context is re-enqueued only if it expires within this "
        "many minutes. Prevents redundant LLM calls for just-generated context."
    ),
)
# Safety cap on instruments per scheduler run. At 1 LLM call per instrument,
# this bounds the burst against the Gemini quota budget.
WATCHLIST_SCHEDULER_BATCH_CAP: int = Field(
    200,
    ge=10,
    le=500,
    description="Maximum unique instruments enqueued per scheduler run.",
)
```

---

#### FILE 2: `backend/app/workers/watchlist_context_scheduler.py` *(NEW FILE)*

```python
"""
Watchlist Context Scheduler
============================
Pre-warms AI market context for all instruments held in any user's watchlist,
running 4× per NSE trading day at configurable fixed IST wall-clock times.

Architecture
------------
  - Pure asyncio while-True loop with cooperative sleep — matches every other
    worker in this codebase (no Celery, no APScheduler, no thread pools).
  - Reads all distinct instrument_keys from watchlist_items in a single query.
  - Skips instruments whose ai_instrument_context expires more than
    WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES in the future (avoids
    redundant LLM calls for recently pre-warmed instruments).
  - Enqueues remaining instruments to cortex:stream:context:jobs with
    force="1" so the context_worker regenerates even if unexpired context
    exists (the scheduler is the authority on refresh cadence).
  - The context_worker handles at-least-once delivery (XREADGROUP + PEL)
    and fan-out (PUBLISH cortex:llm:context:ready:{key} → SSE push path).

Market hours guard
------------------
  During market hours (09:15–15:30 IST on NSE trading days): runs on schedule.
  Outside market hours: sleeps until the next scheduled run on the next trading day.
  Admin trigger (POST /tasks/watchlist_scheduler/trigger) bypasses the guard for
  manual off-hours runs (same TriggerToken mechanism as all native tasks).

Quota impact
------------
  At 100 unique watchlist instruments × 4 runs/day:
    ~400 context LLM calls/day × ~1500 tokens avg = ~600K tokens/day.
  This is 60% of the free-tier TPM budget (1M/day). The freshness margin
  limits actual calls to only stale instruments, which is typically far fewer
  than the full watchlist on runs 2–4 of the day.
  For paid-tier deployments (1M–2M TPM), this budget impact is negligible.
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

# Seconds to sleep between schedule checks. Fine-grained enough to not miss
# a scheduled time by more than this interval.
_SCHEDULE_POLL_SECS = 30


def _now_ist() -> datetime:
    """Current wall-clock time in IST (UTC+5:30), timezone-aware."""
    return datetime.now(timezone.utc).astimezone(
        timezone(_IST_OFFSET)
    )


def _parse_run_times(times_ist: list[str]) -> list[tuple[int, int]]:
    """
    Parse ["09:30", "11:00", ...] → [(9, 30), (11, 0), ...]
    Silently drops malformed entries with a warning.
    """
    parsed: list[tuple[int, int]] = []
    for t in times_ist:
        try:
            h, m = t.split(":")
            parsed.append((int(h), int(m)))
        except (ValueError, AttributeError):
            logger.warning(
                "watchlist_scheduler: ignoring malformed run time %r — "
                "expected HH:MM format",
                t,
            )
    return sorted(parsed)


def _is_market_hours(now_ist: datetime) -> bool:
    """Return True if now_ist falls within NSE market hours (09:15–15:30)."""
    settings = get_settings()
    open_h, open_m   = map(int, settings.NSE_MARKET_OPEN_IST.split(":"))
    close_h, close_m = map(int, settings.NSE_MARKET_CLOSE_IST.split(":"))
    now_minutes = now_ist.hour * 60 + now_ist.minute
    open_minutes  = open_h * 60 + open_m    # 555
    close_minutes = close_h * 60 + close_m  # 930
    return open_minutes <= now_minutes <= close_minutes


def _seconds_until_next_run(
    now_ist: datetime,
    run_times: list[tuple[int, int]],
) -> float:
    """
    Return seconds until the next scheduled run.

    If no scheduled time remains today, returns seconds until the first
    scheduled time tomorrow. This produces the correct sleep even at midnight.
    """
    today = now_ist.date()
    candidates: list[datetime] = []

    for h, m in run_times:
        dt = datetime(
            today.year, today.month, today.day, h, m, 0,
            tzinfo=now_ist.tzinfo,
        )
        if dt > now_ist:
            candidates.append(dt)

    if not candidates:
        # All times passed today — next is tomorrow's first slot.
        tomorrow = today + timedelta(days=1)
        h, m = run_times[0]
        dt = datetime(
            tomorrow.year, tomorrow.month, tomorrow.day, h, m, 0,
            tzinfo=now_ist.tzinfo,
        )
        candidates.append(dt)

    return (min(candidates) - now_ist).total_seconds()


class WatchlistContextScheduler:
    """
    Pre-warm watchlist instrument contexts on a fixed intraday schedule.

    Designed as a single supervised() task registered in the worker registry.
    Responds to PauseToken (pause/resume) and TriggerToken (immediate run)
    from the control plane — same contract as all native worker tasks.
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
        """Entry point called by the worker supervisor."""
        settings   = get_settings()
        run_times  = _parse_run_times(settings.WATCHLIST_SCHEDULER_RUN_TIMES_IST)

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

            # Sleep until the next scheduled time, or until a trigger fires.
            triggered = await self._trigger.wait_or_timeout(max(secs, 1.0))

            if self._shutdown.is_set():
                break

            # Market hours guard: scheduled runs only during market hours.
            # Admin-triggered runs bypass this guard.
            if not triggered and not _is_market_hours(_now_ist()):
                logger.debug(
                    "watchlist_scheduler: outside market hours — skipping run"
                )
                continue

            await self._run_batch()

    async def _run_batch(self) -> None:
        """Fetch watchlist instruments and enqueue stale ones for pre-warming."""
        import time as _time
        t0 = _time.monotonic()
        settings = get_settings()
        margin   = timedelta(minutes=settings.WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES)
        cap      = settings.WATCHLIST_SCHEDULER_BATCH_CAP
        now_utc  = datetime.now(timezone.utc)

        logger.info("watchlist_scheduler: run starting at %s IST", _now_ist().strftime("%H:%M"))

        try:
            # Step 1: fetch all distinct instrument_keys from watchlist_items.
            async with self._session_factory() as db:
                rows = (await db.execute(
                    text("SELECT DISTINCT instrument_key FROM watchlist_items")
                )).all()
            all_keys = [r[0] for r in rows if r[0]]

            if not all_keys:
                logger.info("watchlist_scheduler: no watchlist instruments found — skipping run")
                watchlist_scheduler_runs_total.labels(status="skipped_empty").inc()
                return

            # Step 2: find instruments needing refresh (context expires within margin).
            # An instrument is stale if: its ai_instrument_context is missing OR
            # expires_at <= now + margin.
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

            # Apply batch cap — log how many were dropped.
            to_enqueue = stale_keys[:cap]
            if len(stale_keys) > cap:
                logger.warning(
                    "watchlist_scheduler: %d stale instruments exceeds batch cap %d — "
                    "enqueuing first %d; remainder will be refreshed next run",
                    len(stale_keys), cap, cap,
                )

            # Step 3: XADD each stale instrument to cortex:stream:context:jobs.
            # force="1" bypasses the context_worker's idempotency check so the
            # scheduler controls refresh cadence rather than deferring to TTL.
            enqueued = 0
            for key in to_enqueue:
                # Derive the trading symbol from the instrument_key
                # (e.g. "NSE_EQ|INE00WV01027" → "INE00WV01027"; callers that have
                # the symbol separately should pass it — here we derive best-effort).
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
                len(all_keys),
                len(fresh_keys),
                len(stale_keys),
                enqueued,
                int(duration * 1000),
            )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            watchlist_scheduler_runs_total.labels(status="error").inc()
            logger.error("watchlist_scheduler: batch run failed: %s", exc, exc_info=True)
```

---

#### FILE 3: `backend/app/ai/intelligence/explanation_worker.py`

**One change: support the `force` field in context job messages.**

In `_process_context_message()` (around line 1680), add parsing of the `force` field:

```python
# Add alongside the other fields parsed from the message:
force = fields.get("force", "0") == "1"
source = fields.get("source", "on_demand")
```

Pass `force` through to `_generate_instrument_context()`:

```python
# In the call to _generate_instrument_context:
await _generate_instrument_context(
    instrument_key, sym, ml_snapshot, lock_key, lock_token, force=force
)
```

In `_generate_instrument_context()`, add the `force` parameter and skip the idempotency check when True:

```python
async def _generate_instrument_context(
    instrument_key: str,
    symbol: str | None,
    ml_snapshot: dict | None,
    lock_key: str | None = None,
    lock_token: str | None = None,
    force: bool = False,   # NEW: True = bypass idempotency check (scheduler path)
) -> None:
    ...
    async with AsyncSessionLocal() as db:
        # Idempotency check — skip when force=True (scheduler controls cadence)
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
                    "for %s, skipping Gemini call",
                    instrument_key,
                )
                llm_explanation_dedup_total.labels(layer="db_idempotency").inc()
                return
        # force=True path: log the bypass (scheduler knows what it's doing)
        elif force:
            logger.debug(
                "explanation_worker: force=True — bypassing idempotency check for %s "
                "(scheduler-initiated refresh)",
                instrument_key,
            )
        ...
```

---

#### FILE 4: `backend/app/workers/registry.py`

**Two changes:**

**Change A — Add to `TASK_NAMES` tuple** (after `"sl_tp_worker"`):

```python
TASK_NAMES: tuple[str, ...] = (
    # ... existing entries ...
    "sl_tp_worker",
    "watchlist_scheduler",   # NEW
)
```

**Change B — Add to the registry dict** (after `"sl_tp_worker"` entry, before the mismatch check):

In `build_task_registry()`:

```python
# In the imports block at the top of the function:
from app.workers.watchlist_context_scheduler import WatchlistContextScheduler

# Instantiate once like FundamentalsRefreshScheduler:
watchlist_scheduler = WatchlistContextScheduler(
    session_factory=session_factory,
    redis=redis_client._redis,
    shutdown=shutdown,
    pause=_state("watchlist_scheduler").pause_token,
    trigger=_state("watchlist_scheduler").trigger_token,
)

# In the registry dict:
"watchlist_scheduler": lambda: watchlist_scheduler.run(),
```

---

#### FILE 5: `backend/app/core/metrics.py`

**Add new metric block** (after the `rag_cleanup_runs_total` block, before `fundamentals_rate_slots_total`):

```python
# ── Watchlist Context Scheduler Metrics ──────────────────────────────────────

watchlist_scheduler_runs_total = Counter(
    'watchlist_scheduler_runs_total',
    'Total watchlist context scheduler batch runs by final status',
    ['status'],  # success | error | skipped_empty | skipped_market_closed
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
    'Unix timestamp of the last successful watchlist scheduler run (for staleness alerting)',
)
```

---

### 5.3 Fan-Out Flow (End-to-End)

```
09:30 IST — WatchlistContextScheduler._run_batch()
  → SELECT DISTINCT instrument_key FROM watchlist_items → [ADANI, RELIANCE, DIXON, ...]
  → filter stale (expires_at <= 10:50) → [ADANI, RELIANCE, DIXON, ...]
  → XADD cortex:stream:context:jobs × N instruments (force="1")

context_worker (Priority.LOW, 1 instance)
  → XREADGROUP — picks up ADANI job
  → _generate_instrument_context(force=True) — bypasses idempotency check
  → RAG retrieve + Gemini structured output (~10–15s)
  → UPSERT ai_instrument_context (ADANI, expires_at = now + 2h)
  → XADD cortex:sse:events:ctx:NSE_EQ|ADANI  (full payload)
  → PUBLISH cortex:llm:context:ready:NSE_EQ|ADANI

SSE watcher (for each connected user watching ADANI):
  → _watch_explanations() receives pmessage on cortex:llm:context:ready:*
  → _handle_push() routes to instrument context path
  → reads cortex:sse:events:ctx:NSE_EQ|ADANI event store
  → state.explanation = full_payload
  → _emit_update() → frontend renders AI context immediately ✓

Users NOT connected when job runs:
  → Open watchlist at 10:45 → ai_stream.py _refresh_explanation()
  → Stage 2: finds ai_instrument_context (expires_at = 11:30) → return payload ✓
  → No LLM call triggered (Stage 3 skipped)
```

---

## 6. Implementation Order

Dependencies must be respected. Independent items can be parallelised.

```
PHASE 0 — Foundations (can all be done in one pass, no dependencies between them)
  0-A  config.py            — Add EXPLANATION_CONSENSUS_THRESHOLD + all scheduler settings
  0-B  metrics.py           — Add watchlist_scheduler_* metrics

PHASE 1 — Backend core (depends on 0-A, 0-B)
  1-A  engine.py            — Gate XADD behind consensus_score check
  1-B  explanation_worker.py — Add force param + _process_context_message reads force
  1-C  watchlist_context_scheduler.py — NEW FILE (depends on 0-A, 0-B)

PHASE 2 — Registry + SSE API (depends on Phase 1)
  2-A  registry.py          — Add "watchlist_scheduler" to TASK_NAMES + registry dict
                              (depends on 1-C)
  2-B  ai_stream.py         — Add _build_weak_signal_payload + Stage 1 update + bypass endpoint
                              (depends on 0-A)

PHASE 3 — Frontend (depends on 2-B contract)
  3-A  types/analysis.ts    — Add weak_signal, suggestion_id, consensus_score to ExplanationData
  3-B  AIExplanationPanel.tsx — Add PanelWeakSignal component + refresh handler
                               (depends on 3-A)
```

---

## 7. Design Invariants to Preserve

The following system invariants must not be broken by any of the above changes:

1. **No DB connection held across LLM calls** — the three-phase pattern in `_generate_explanation` and `_generate_instrument_context` must not be altered. All new DB operations belong in Phase 1 (read) or Phase 3 (write), never across Phase 2 (LLM call).

2. **XACK after processing only** — the scheduler enqueues to `CONTEXT_JOBS` via XADD; the context_worker XACKs. The scheduler itself never touches consumer-group semantics.

3. **Audit log is mandatory** — `_write_audit_entry` must be called on every `_generate_instrument_context` path including the force=True path. It is a governance requirement (SR 11-7).

4. **Poll path must never downgrade a richer push state** — `_should_apply_polled_explanation()` in `ai_stream.py` already handles this. The `weak_signal` state does not have `available=True`, so a subsequent poll that returns `weak_signal=True` should NOT overwrite a successfully delivered explanation. This needs one additional condition in `_should_apply_polled_explanation`:

   ```python
   # Add to _should_apply_polled_explanation:
   # A weak_signal poll must not overwrite a successfully delivered explanation.
   if polled is not None and polled.get("weak_signal") and current is not None:
       if current.get("available") or current.get("streaming"):
           return False
   ```

5. **Consumer group registration** — both EXPLANATION_JOBS and CONTEXT_JOBS streams already have consumer groups (`cortex-explanation-workers`) registered at startup. The watchlist_scheduler writes to CONTEXT_JOBS; no new consumer group is needed.

6. **Supervisor restart correctness** — `watchlist_scheduler.run()` is a lambda factory, same pattern as `fundamentals_scheduler.run()`. The `WatchlistContextScheduler` instance is created once outside the lambda; `run()` returns a fresh coroutine each time. The scheduler's internal state (sleep position) is ephemeral — restarting after a crash simply recalculates the next scheduled run time and sleeps appropriately.

---

## 8. Testing Plan

### Unit tests to write

| Test | File | What to assert |
|------|------|----------------|
| Gate in engine.py | tests/test_correlation_engine.py | XADD NOT called when consensus_score < 75; called when >= 75 |
| _build_weak_signal_payload | tests/test_ai_stream.py | Returns dict with weak_signal=True, correct fields |
| Stage 1 weak signal branch | tests/test_ai_stream.py | Active suggestion + score < 75 → weak_signal payload |
| Bypass endpoint auth | tests/test_ai_stream.py | 401 on bad token |
| Bypass endpoint debounce | tests/test_ai_stream.py | 429 on second call within 60s |
| Bypass endpoint 404 | tests/test_ai_stream.py | 404 when suggestion not found |
| Bypass endpoint 409 | tests/test_ai_stream.py | 409 when explanation already exists |
| force flag in context worker | tests/test_explanation_worker.py | With force=True, idempotency check skipped even if context fresh |
| _parse_run_times | tests/test_watchlist_scheduler.py | Malformed entries dropped, valid entries sorted |
| _is_market_hours | tests/test_watchlist_scheduler.py | Edge cases: exactly 09:15, exactly 15:30, 09:14, 15:31 |
| _seconds_until_next_run | tests/test_watchlist_scheduler.py | Correct sleep for all times past, first slot tomorrow |
| _run_batch quota guard | tests/test_watchlist_scheduler.py | Fresh instruments skipped; cap enforced; XADD count correct |
| Frontend: PanelWeakSignal renders | — | Shows alert text, score, refresh button |
| Frontend: refresh button calls API | — | fetch called with correct URL; setRefreshing cycles |
| Frontend: weak_signal → skeleton on SSE | — | State transitions correctly on SSE update |

### Integration smoke test sequence (post-deploy)

```
1. Generate a signal with consensus_score < 75:
   → Verify explanation job NOT in cortex:stream:explanation:jobs (XLEN)
   → Open SSE for that instrument
   → Verify panel shows "weak signal" state with refresh button
   → Click refresh → verify 202 response
   → Verify XADD appears in cortex:stream:explanation:jobs
   → Wait → verify SSE push delivers explanation → panel shows content

2. Generate a signal with consensus_score >= 75:
   → Verify explanation job IS in cortex:stream:explanation:jobs
   → SSE panel shows generating skeleton → transitions to content (unchanged path)

3. Watchlist scheduler manual trigger:
   POST /api/v1/admin/worker/tasks/watchlist_scheduler/trigger
   → Verify context jobs appear in XLEN cortex:stream:context:jobs
   → Verify ai_instrument_context rows written with fresh expires_at
   → Open SSE for a watchlist instrument → verify Stage 2 serves cached context

4. Scheduled run (wait for 09:30 or trigger manually):
   → Verify watchlist_scheduler_runs_total incremented in Grafana
   → Verify watchlist_scheduler_instruments_queued_total incremented
   → Verify watchlist_scheduler_last_run_timestamp updated
```

---

## 9. Deployment Notes

1. **No DB migration required** — no new columns or tables. `consensus_score` is already on `trade_suggestions`. `ai_instrument_context` and `watchlist_items` are unchanged.

2. **No Redis migration required** — the scheduler writes to the existing `cortex:stream:context:jobs` stream. No new streams, no new consumer groups.

3. **New `.env` variable (optional)** — `WATCHLIST_SCHEDULER_RUN_TIMES_IST` defaults to `["09:30","11:00","13:00","14:30"]`. Override as a JSON array if needed. `EXPLANATION_CONSENSUS_THRESHOLD` defaults to `75.0`.

4. **Quota impact** — Feature 1 reduces daily Gemini calls by approximately 40–50% (gating out the low-confidence band). Feature 2 adds back scheduled context calls but only for stale instruments. Net impact on free-tier deployments: neutral to slightly positive, depending on watchlist size.

5. **Deploy order** — backend first (all Python changes), then frontend. The backend changes are backward-compatible with the current frontend (new `weak_signal` field is simply ignored by the old frontend). Deploy frontend when backend is stable.

6. **Rollback** — Feature 1 gate can be effectively disabled by setting `EXPLANATION_CONSENSUS_THRESHOLD=50.0` in `.env` (all signals pass). Feature 2 can be disabled by removing `watchlist_scheduler` from `TASK_NAMES` (supervisor restart required).

---

## 10. Files Touched — Summary

| File | Change Type | Feature |
|------|-------------|---------|
| `backend/app/core/config.py` | Modified | 1 + 2 |
| `backend/app/ai/correlation/engine.py` | Modified | 1 |
| `backend/app/api/v1/ai_stream.py` | Modified | 1 |
| `backend/app/ai/intelligence/explanation_worker.py` | Modified | 2 |
| `backend/app/workers/watchlist_context_scheduler.py` | **NEW** | 2 |
| `backend/app/workers/registry.py` | Modified | 2 |
| `backend/app/core/metrics.py` | Modified | 2 |
| `frontend/src/types/analysis.ts` | Modified | 1 |
| `frontend/src/components/AIExplanationPanel.tsx` | Modified | 1 |

**Total: 8 modified + 1 new file.**  
No DB migrations. No new Docker services. No new Redis streams or consumer groups.
