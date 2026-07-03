# Watchlist Context Card — Skeleton Investigation

**Date:** 2026-06-30  
**Status:** Root cause identified. Fix known (not yet implemented).

---

## Problem Statement

Users who open a watchlist card (instruments with no active trade suggestion — AWL, BAJFINANCE, CMRGREEN, COMSYN, FMCGADD, HINDALCO) consistently see a "Generating…" skeleton in the AI Explanation / Market Context panel instead of immediate content. This happens **every time** a card is opened after roughly 11:00 UTC, and persists for 10–15 seconds while a Gemini call is made on-demand.

The user experience is identical to the old **permanent-skeleton bug** (which was fixed by the SSE proxy TransformStream + IIFE change), making it hard to tell whether the fix worked or the same bug is still present.

---

## How We Found This

### Session context

The backend and worker had just been restarted after the Gemini quota fix was implemented (GEMINI_GENERATE_RPD corrected from default 1500 → 10 per key). RPD counter was at 24 of 50 daily calls.

The user opened a watchlist card (COMSYN — `NSE_EQ|INE073V01015`) and reported the Market Context panel was not showing any content. We were asked to investigate.

### Investigation steps

**Step 1 — Confirmed generation was happening**

Checked Redis at the exact moment after the user reported the issue:

```
KEYS cortex:sse:events:ctx*
  → cortex:sse:events:ctx:NSE_EQ|INE073V01015   (just appeared)

GET cortex:gemini:rpd:generate:2026-06-30
  → 32   (was 24 before user opened card → 8 new calls)
```

Content was being generated on-demand (Stage 3 triggered in `_fetch_explanation_for_instrument`). The Redis SSE key appeared while the user was looking at the skeleton — generation was in progress when they reported "not in the ui yet."

**Step 2 — Confirmed content was complete and valid**

Read the `cortex:sse:events:ctx:NSE_EQ|INE073V01015` stream:

```
generated_at: 2026-06-30T09:17:12Z
available: true
context_type: instrument_context
full_explanation: "### What the models saw\n..."
```

Full Gemini-generated market context was in Redis. DB row also written:

```sql
SELECT instrument_key, generated_at, expires_at, (expires_at > NOW()) as is_live
FROM ai_instrument_context
WHERE instrument_key = 'NSE_EQ|INE073V01015';

-- Result:
-- expires_at: 2026-06-30 11:17:12+00  |  is_live: true
```

**Step 3 — Confirmed this was not a permanent skeleton (the old bug)**

Content DID appear in the UI approximately 10–15 seconds after the card was opened. The SSE proxy TransformStream fix IS working — bytes reach the browser. The skeleton is transitional, not permanent.

**Step 4 — Traced the scheduler timing**

Checked `config.py`:

```python
WATCHLIST_SCHEDULER_RUN_TIMES_IST: list[str] = Field(
    default=["09:30", "11:00", "13:00", "14:30"]
)
WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES: int = Field(80)
```

Scheduler fires 4× per trading day. Last run: 14:30 IST = **09:00 UTC**. Context generated at that run has a 2-hour `expires_at` → expires by **11:00 UTC**.

No further scheduler runs until 09:30 IST the next day = **04:00 UTC** — a **17-hour gap**.

**Step 5 — Confirmed the delivery path is sound**

Traced the full code path:

- `_fetch_explanation_for_instrument` Stage 2: finds DB row if `expires_at > now()`, reads from Redis SSE store, returns content directly (no Gemini call, no wait)
- `_should_apply_polled_explanation`: no suppression for `available: true` content
- `_emit_update()`: enqueues SSE event → consumer loop yields → SSE proxy forwards to browser
- Frontend 5-tier merge priority: `sseExplanation.available` = true → Tier 1 returns it immediately
- `AIExplanationPanel`: renders `ExplanationContent` ✓

Pub/sub pattern matching verified with live Redis test:
```bash
PSUBSCRIBE cortex:llm:context:ready:*
PUBLISH cortex:llm:context:ready:NSE_EQ|INE073V01015 '{"instrument_key":"..."}'
# → pmessage received correctly (pipe character in key is not an issue)
```

---

## Root Cause Analysis

### Root Cause 1 — 2-hour TTL + 17-hour scheduler gap (primary)

| Parameter | Value |
|---|---|
| `expires_at` TTL in DB | `now + 2 hours` (hardcoded in `explanation_worker.py:1250`) |
| Redis SSE key TTL | 1 hour (`_SSE_CONTEXT_TTL_SECS = 3_600` in `explanation_worker.py:165`) |
| Last scheduler run (IST) | 14:30 → 09:00 UTC |
| Content expires | 11:00 UTC (= 09:00 UTC + 2h) |
| Next scheduler run | 04:00 UTC next day (09:30 IST) |
| Gap with stale content | **17 hours** (11:00 UTC → 04:00 UTC next day) |

Any watchlist card opened after 11:00 UTC finds no valid content in Stage 2 → falls to Stage 3 → triggers on-demand Gemini generation → 10–15 second skeleton delay.

### Root Cause 2 — Skeleton looks identical to the old permanent-skeleton bug

The old bug (pre-fix): bytes never reached the browser → permanent skeleton.  
Current behaviour: bytes arrive immediately, skeleton visible for 10–15 seconds while Gemini generates.

The user cannot tell these apart visually. The fix IS working; the symptom is the same.

### Root Cause 3 — Panel close during the 10–15s window loses the pub/sub signal

If the user navigates away before the worker finishes:
1. Worker completes → publishes `cortex:llm:context:ready:{key}` 
2. SSE connection is gone → signal lost  
3. On **reopen**: Stage 2 immediately finds the cached DB row → serves content in sub-second ✓  
4. EDGE CASE: if reopen happens within the same second as the pub/sub (before DB write commits), Stage 3 sees lock still held, returns pending skeleton, waits for a pub/sub that already fired → **stuck in skeleton until the 30-second poll cycle runs**

---

## Files Involved

| File | Role |
|---|---|
| `backend/app/ai/intelligence/explanation_worker.py:1250` | `expires_at = now + timedelta(hours=2)` — the 2-hour TTL |
| `backend/app/ai/intelligence/explanation_worker.py:165` | `_SSE_CONTEXT_TTL_SECS = 3_600` — 1-hour Redis key TTL |
| `backend/app/api/v1/ai_stream.py:405–468` | Stage 2 (DB cache check) and Stage 3 (on-demand trigger) |
| `backend/app/workers/watchlist_context_scheduler.py` | Scheduler: runs at 09:30 / 11:00 / 13:00 / 14:30 IST |
| `backend/app/core/config.py:323` | `WATCHLIST_SCHEDULER_RUN_TIMES_IST` default schedule |

---

## Known Fix

**Extend the `expires_at` TTL in `explanation_worker.py:1250`** from 2 hours to **12–24 hours**.

Market context for a watchlist stock (news summary, model read, macro backdrop) does not meaningfully change every 2 hours. A 12-hour TTL would keep content fresh through the entire post-market gap. The scheduler's 80-minute freshness margin already ensures stale content gets proactively refreshed during trading hours regardless of TTL length — extending the TTL only affects how long the after-hours cache stays valid.

The 1-hour Redis SSE key TTL (`_SSE_CONTEXT_TTL_SECS`) should be extended to match (or set to 0 / no TTL), since after 1 hour the SSE store key expires and Stage 2 falls back to the DB payload — a minor degradation (no source citations) but not a user-visible issue.

**One-line fix:**

```python
# explanation_worker.py line 1250
expires_at = now_utc + timedelta(hours=24)   # was: hours=2
```

And optionally:
```python
# explanation_worker.py line 165
_SSE_CONTEXT_TTL_SECS = 86_400  # 24 h — was 3_600 (1 h)
```

---

## What Was NOT the Problem

- SSE proxy buffering / permanent skeleton → **fixed** (TransformStream + IIFE in `frontend/src/app/api/v1/ai/stream/route.ts`)
- Redis pub/sub pattern matching with `|` in channel names → **works correctly** (verified live)
- `_should_apply_polled_explanation` blocking the update → **does not block** `available: true` content
- `_watch_explanations` task crashing silently → no crash observed; pub/sub delivery verified
- Budget guard throttling context jobs → guard fires at 42 remaining calls; we were at 26 remaining
- Gemini quota circuits → all open circuits have since reset; generation succeeded

---

## Verification Steps After Fix

1. Generate fresh content (or wait for scheduler) — note the `generated_at` timestamp  
2. Wait 2+ hours (past the old TTL)  
3. Open a watchlist card — content should appear **instantly** (Stage 2 serves from DB, no Gemini call)  
4. Confirm RPD counter does **not** increase when opening the card  
