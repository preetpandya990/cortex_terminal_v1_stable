# Demand-Driven AI Processing — Architecture Plan
**Date:** 2026-07-01
**Status:** Proposal — awaiting implementation decision

---

## 1. Problem Statement

The system is **push-based**: background workers continuously call Gemini as events arrive,
regardless of whether any user is actively viewing the results. When RSS floods in at market
open (~09:30 UTC), three workers fire simultaneously — sentiment batcher, event classifier,
and forecast queue — exhausting the 100 RPD daily budget before the trading day properly
starts. The calls that actually matter to a user (trade explanations, watchlist context)
then have no quota left.

**Root cause:** High-volume, low-priority background enrichment (sentiment, classification,
forecasting) competes on the same daily quota as low-volume, high-value user-facing calls
(explanations, instrument context). Automatic dispatch means the low-priority callers always
win by volume.

---

## 2. Industry Context

This is a well-established architectural shift known as **pull-based inference** or
**on-demand batch processing**. Production SaaS products (Notion AI, Figma AI) gate AI
feature execution behind explicit user actions specifically to control per-call costs under
tight quotas. The pattern is documented in LLM gateway literature as separating:

- **Synchronous user-facing calls** — user is waiting; must fire immediately
- **Async enrichment calls** — user is not waiting; can be deferred and batched

---

## 3. Proposed Model — Two Permanent Tiers

### Tier 1: Always Automatic (no change)
These are synchronous and directly user-facing. A real user is waiting for the result.

| Caller | Schema | Priority | Trigger |
|---|---|---|---|
| `explanation_worker` — trade suggestions | `ExplanationOutput` | HIGH | Redis Stream, per signal |
| `explanation_worker` — instrument context | `ExplanationOutput` | HIGH | Redis Stream, per watchlist item |

These two callers consume ~14 calls/day combined. They are never throttled by the budget
guard (HIGH priority). No change to their behaviour.

### Tier 2: Queued, Never Auto-Dispatched (the change)
These enrich the data pipeline but no user is waiting on them in real-time. They accumulate
in Redis queues and are dispatched **only when the admin explicitly triggers them** via the
Worker Control Panel.

| Caller | Schema | Current Priority | Current Auto-Trigger | Proposed |
|---|---|---|---|---|
| `nlp_engine._flusher_loop` | `SentimentOutput` / `_SentimentBatchOutput` | BACKGROUND | Auto-flush every 5s or 8 articles | Accumulate indefinitely; flush on admin trigger |
| `event_classifier` (Gemini path) | `_ClassificationSchema` | LOW | Auto-fires in 30s event loop | Queue events in Redis; Gemini call on admin trigger |
| `forecast_batch_worker` | `NewsForecastBatchOutput` | LOW | Auto-drains queue every 2s | Queue drains only on admin trigger |

---

## 4. Projected Quota Impact

| Caller | Current auto calls/day | Under new model |
|---|---|---|
| Sentiment | ~40–80 (continuous auto-flush) | ~2–4 (admin triggers 2× per day) |
| Event classifier | ~20–40 (continuous 30s loop) | ~2–4 (admin triggers 2× per day) |
| Forecast batch | ~10–20 (auto-drain every 2s) | ~4–6 (admin triggers before market events) |
| Explanations (HIGH) | ~10 | ~10 (unchanged) |
| Instrument context (HIGH) | ~4 | ~4 (unchanged) |
| **Total** | **~85–155/day — exhausts budget** | **~22–28/day (~25% of 100 RPD budget)** |

**Net result:** ~75 calls/day freed. Budget exhaustion at 09:30 UTC eliminated.

---

## 5. User Experience — Admin Console

The existing **Worker Control Panel** gains a new **"AI Processing Queue"** section showing
live pending counts per category and a dispatch button for each:

```
┌─────────────────────────────────────────────────────────────┐
│  AI Processing Queue                                        │
├─────────────────────────────────────────────────────────────┤
│  Sentiment Analysis     47 articles pending   [Dispatch]   │
│  Event Classification   12 events pending     [Dispatch]   │
│  News Forecasts          8 symbols pending    [Dispatch]   │
└─────────────────────────────────────────────────────────────┘
```

Each **[Dispatch]** button fires one POST to a new admin API endpoint, which:
1. Pops all pending items from the relevant Redis queue
2. Builds one efficiently-batched Gemini call
3. Writes results to cache / DB
4. Returns the count of processed items to the UI

**Typical admin workflow:**
- Once before market open (~09:15 IST): dispatch all three queues — ~8 Gemini calls total
- Once mid-session (~12:00 IST): dispatch again for any new articles — ~4 Gemini calls
- Total background enrichment cost: **~12 calls/day** vs current ~100–155

---

## 6. What Needs to Be Built

### Backend — 3 changes to existing workers

**6.1 `nlp_engine.py` — disable auto-flush**
- Remove or gate the `_flusher_loop` auto-dispatch on a new config flag
  `SENTIMENT_AUTO_FLUSH: bool = False`
- Articles continue accumulating in the internal batch queue
- New method: `flush_pending() -> int` — dispatches all accumulated articles in one
  batched Gemini call, returns count processed

**6.2 `event_classifier.py` — disable auto-Gemini path**
- Events that pass the heuristic pre-filter currently go straight to Gemini
- Change: store them in a Redis list `cortex:event:classifier:pending` instead
- New function: `flush_pending_classifications(redis, session_factory) -> int` — pops
  all pending events, calls Gemini once (batched), writes results

**6.3 `forecast_batch_worker.py` — disable auto-drain**
- The 2s poll loop currently drains the queue automatically
- Change: add a config flag `FORECAST_AUTO_DISPATCH: bool = False`; when False the loop
  idles (queue depth metric still updates)
- Manual trigger calls the existing `_flush_batch()` directly

### Backend — 3 new admin API endpoints

All under `/api/v1/admin/ai-processing/` (authenticated, internal secret required):

```
POST /api/v1/admin/ai-processing/sentiment/dispatch
  → flushes nlp_engine batch queue
  → returns {"dispatched": N, "calls_made": M}

POST /api/v1/admin/ai-processing/events/dispatch
  → flushes cortex:event:classifier:pending
  → returns {"dispatched": N, "calls_made": M}

POST /api/v1/admin/ai-processing/forecasts/dispatch
  → flushes cortex:forecast:batch:queue
  → returns {"dispatched": N, "calls_made": M}

GET  /api/v1/admin/ai-processing/status
  → returns pending counts for all three queues
  → {"sentiment_pending": 47, "events_pending": 12, "forecasts_pending": 8}
```

### Frontend — extend Worker Control Panel

- New **"AI Processing Queue"** card in the existing admin worker dashboard
- Polls `GET /api/v1/admin/ai-processing/status` every 30s for live queue depths
- Three dispatch buttons (one per category), each with:
  - Loading state during dispatch
  - Success toast: "Dispatched 47 articles — 3 Gemini calls used"
  - Error handling

### Config — 2 new flags in `config.py` / `.env`

```env
SENTIMENT_AUTO_FLUSH=false       # disable nlp_engine auto-dispatch
FORECAST_AUTO_DISPATCH=false     # disable forecast_batch_worker auto-drain
# event_classifier auto-Gemini is disabled by code change (no flag needed)
```

---

## 7. What Does NOT Change

- RSS ingestion — no Gemini calls, runs as-is
- Trade explanation worker — always-on, HIGH priority, user-facing
- Instrument context worker — always-on, HIGH priority, user-facing
- RAG embedder — embed operation uses a separate quota bucket (GEMINI_EMBED_RPM),
  unaffected by generate RPD; no change needed
- Safety response — already rare, on-demand only, no change
- All existing caching, circuit breakers, budget guard, priority queue — unchanged;
  they protect the Tier 1 callers as before

---

## 8. Implementation Scope

| Item | Files | Complexity |
|---|---|---|
| Disable sentiment auto-flush | `nlp_engine.py`, `config.py` | Low |
| Disable event classifier auto-Gemini | `event_classifier.py` | Low |
| Disable forecast auto-drain | `forecast_batch_worker.py`, `config.py` | Low |
| 3 admin API endpoints | New file under `api/v1/admin/` | Medium |
| Frontend AI Processing Queue card | 1 new component + hook | Medium |
| **Total** | **~6 files changed, 1 new API file, 1 new frontend component** | **~1 day** |

No database migrations. No new dependencies. No changes to the quota management
infrastructure already in place.

---

## 9. Open Questions Before Implementation

1. Should "dispatch" buttons be per-category (3 buttons) or a single "Dispatch All"?
2. Should there be a scheduled auto-dispatch as a fallback (e.g., if admin forgets)?
   - Option A: Purely manual — never auto-dispatch (maximum quota control)
   - Option B: Manual-first with a daily safety-net auto-dispatch at a fixed time
     (e.g., 09:00 IST) if queue depth exceeds a threshold
3. Should the `/dispatch` endpoints be synchronous (wait for Gemini call to complete)
   or fire-and-forget (return immediately, process in background)?
   - Synchronous is simpler and gives immediate feedback on calls used
   - Fire-and-forget is faster for the UI but harder to report results
