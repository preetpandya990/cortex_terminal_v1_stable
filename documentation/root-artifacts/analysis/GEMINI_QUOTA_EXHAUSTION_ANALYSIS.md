# Gemini API Quota Exhaustion — Root Cause Analysis

**Investigated:** 2026-06-24  
**Trigger:** All 3 Gemini API keys hit their daily quota and had circuit breakers opened simultaneously at 13:11 UTC on a day with zero ML suggestions generated and only 1 manual watchlist request from the user. This was unexpected and prompted a deep investigation into what was actually consuming quota.

---

## What Happened

At 13:11 UTC, all three Gemini API keys became circuit-broken within 11 seconds of each other:

```
13:11:20  llm: Gemini generate key=-NlXkftg quota EXHAUSTED — removing from rotation
13:11:30  llm: Gemini generate key=jZfei_tA quota EXHAUSTED — removing from rotation
13:11:30  llm: Gemini generate key=GWUf3hHg quota EXHAUSTED — removing from rotation
```

No AI explanations, market context, or any LLM-powered features could function for the remainder of the day. The circuit breakers reset automatically at midnight PT.

---

## Call Breakdown — Where the Quota Went

Source of truth: `ai_llm_audit_log` table (captures all Gemini calls across all processes).

### Today (June 24) — 288 total generate calls

| Invocation Type | Calls | Source |
|-----------------|-------|--------|
| `sentiment` | 221 | Background: `event_processing_loop` (1 call per news event) |
| `instrument_context` | 41 | Watchlist item opened + re-trigger loop (see below) |
| `news_forecast` | 26 | Background: `correlation_engine` Pathway 2 (1 call per event per affected symbol) |
| **Total** | **288** | |

The user's 1 manual watchlist request accounts for approximately **5 calls**. The remaining **283 calls came from silent background workers**.

### Daily volumes for context (last 7 days)

| Date | Sentiment | News Forecast | Context | Explanation | Total |
|------|-----------|---------------|---------|-------------|-------|
| 2026-06-24 | 221 | 26 | 41 | 0 | **288** |
| 2026-06-23 | 368 | 298 | 0 | 4 | **670** |
| 2026-06-22 | 475 | 72 | 2 | 0 | **549** |
| 2026-06-21 | 41 | 17 | 1 | 0 | **59** |
| 2026-06-20 | 76 | 14 | 1 | 0 | **91** |
| 2026-06-19 | 214 | 85 | 0 | 5 | **304** |
| 2026-06-18 | 363 | 275 | 0 | 2 | **640** |

---

## Root Cause 1 — Background Workers Process Every News Event with Gemini

The `event_processing_loop` runs continuously in the worker sidecar and makes Gemini API calls for every ingested news event, regardless of whether any suggestions are active or the user is doing anything:

- **`NLPEngine.process_event()`** → 1 `SentimentOutput` generate call per event
- **`correlation_engine` Pathway 2** → 1 `NewsForecastOutput` generate call per event per affected symbol

Today had **153 new news events** ingested. That alone generated ~247 background Gemini calls. No suggestions, no user action, no signals — just news arriving and being processed.

This is the structural reason quota gets consumed on "quiet" days: the pipeline is event-driven, not user-driven. A day with heavy news flow (June 22 had 302 events, June 23 had 263) consumes proportionally more quota regardless of user activity.

---

## Root Cause 2 — Token Bucket Is Per-Process; Three Processes Share the Same Keys

`GEMINI_GENERATE_RPM=30` is set in `.env`. This creates one token bucket **per process** allowing 30 RPM. But the system runs three concurrent processes:

- `main:app` on port 8000 (1 process)
- `worker_app:app --workers 2` on port 8001 (2 worker processes)

Each process has its own independent 30 RPM bucket. All three processes share the same API keys in round-robin. The combined effective rate hitting each key:

```
3 processes × 30 RPM ÷ 3 API keys = 30 RPM per key hitting the API
Free tier actual limit per key      = 10 RPM
```

**The system sends 3× the free tier rate limit to each key under concurrent load.**

This was directly observed in the per-minute audit log at peak processing time:

```
12:34 UTC  sentiment=40  news_forecast=3   context=1   →  44 calls in 1 minute
12:35 UTC  sentiment=40  news_forecast=2   context=2   →  44 calls in 1 minute
12:36 UTC  sentiment=40  news_forecast=2               →  42 calls in 1 minute
12:37 UTC  sentiment=40  news_forecast=10  context=5   →  55 calls in 1 minute
```

40–55 calls per minute across 3 keys = 13–18 RPM per key against a 10 RPM limit. This generates 429 rate-limit responses. When the error body contains `free_tier_requests`, the circuit breaker treats it as a daily exhaustion and opens the key until midnight PT.

---

## Root Cause 3 — HalfVector Bug Caused 41 Context Calls for 1 Instrument

The user opened 1 watchlist item (CMRGREEN = `NSE_EQ|INE00WV01027`). Due to a bug introduced by migration 0048 (halfvec column type change), RAG retrieval failed on every context generation attempt:

```
explanation_worker: RAG retrieval failed for context NSE_EQ|INE00WV01027
    (continuing with no context): 'HalfVector' object is not iterable
```

The LLM was still called (graceful fallback — continues without RAG context), but each attempt's context write to `ai_instrument_context` either failed or the 45-second generation lock expired before the DB write completed. Stage 3 in `ai_stream.py` would re-trigger on the next 30-second poll, spawning another context job.

Observed pattern from the per-minute audit:

```
12:31 UTC  1 context call  (attempt — failed/no DB write)
12:32 UTC  1 context call  (re-trigger after lock expiry)
12:33 UTC  1 context call  (re-trigger)
12:34 UTC  1 context call  (re-trigger)
12:35 UTC  2 context calls (re-triggers)
12:37 UTC  5 context calls (re-triggers + success — DB record written 12:37:41)
...
13:11 UTC  9 more context calls (after backend restart, quota now exhausted)
```

6 LLM calls before one succeeded. The DB has exactly 1 context record for today. The other ~35 context calls were quota consumed by the re-trigger loop.

**This bug has been fixed** (2026-06-24): `_HalfVecFloatList` TypeDecorator added to `app/ai/fusion/models.py` ensures `HALFVEC` columns always return `list[float]` to Python callers. Context now generates cleanly on the first attempt.

---

## Why "Quiet Day" Is Misleading

The system's Gemini consumption is **decoupled from user activity**. Key background processes that consume quota continuously:

| Process | Trigger | Calls per event |
|---------|---------|-----------------|
| `event_processing_loop` | Every ingested news event | 1 (sentiment) |
| `correlation_engine` Pathway 2 | Every news event × affected symbols | 1 (news_forecast) |
| `explanation_worker` (context) | Any watchlist page open | 1–N (depends on lock/retry loop) |

On a day with no suggestions generated, no trades placed, and only 1 watchlist open: **283 of 288 Gemini calls were invisible to the user**.

---

## Contributing Factor — Circuit Breaker Behavior

The circuit breaker in `llm_client.py` treats two distinct 429 error types identically:

- `GenerateRequestsPerDay` → confirmed daily quota exhaustion
- `free_tier_requests` → **per-minute rate limit** for free tier (limit: 10–20 RPM)

Both open the circuit until midnight PT. When the per-minute rate limit is hit due to multi-process burst load, the key is removed from rotation for the rest of the day even though the issue was transient (would have self-resolved in ~60 seconds). This converts a rate limit burst into a full-day outage.

---

## Summary

| Factor | Calls wasted | Impact |
|--------|-------------|--------|
| Background sentiment/forecast on 153 news events | ~247 | Daily budget consumed by invisible work |
| HalfVector bug causing 6+ re-triggers per watchlist open | ~35 | Amplifier: each bug hit = 5–10× expected calls |
| Multi-process rate overload exceeding per-key RPM | multiplier | Triggers circuit breakers on burst → full-day outage |

The system is not "over-using" Gemini in the sense that each call is doing real work. The issue is that the free tier daily budget (~100–250 RPD per key) is insufficient for the volume of background processing the pipeline performs, and the multi-process deployment multiplies the instantaneous rate above what each key allows.
