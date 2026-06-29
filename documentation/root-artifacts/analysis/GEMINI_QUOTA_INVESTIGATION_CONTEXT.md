# Gemini API Quota Exhaustion — Investigation Context & Narrative

**Date:** 2026-06-24  
**Companion doc:** `GEMINI_QUOTA_EXHAUSTION_ANALYSIS.md` (technical root cause breakdown)

---

## Why This Was Investigated

### The Trigger — Watchlist Explanation Not Appearing

The investigation began not with quota monitoring, but with a user-facing symptom: a watchlist item was opened and after **more than a minute**, the AI explanation panel was still showing a skeleton loader with no content.

This was already unusual. Under normal operation the SSE endpoint (`/api/v1/ai-stream/explanation/{suggestion_id}`) delivers a fully populated explanation within 10–15 seconds. A 60+ second stall with no content indicated something had broken silently in the pipeline.

The initial hypothesis was a slow Gemini response or a network hiccup. The actual root cause turned out to be far more structural.

---

## Investigation Path

### Step 1 — SSE Pipeline Audit

The first step was to trace the 3-stage SSE lookup defined in `ai_stream.py`:

- **Stage 1:** Check Redis event store for a recent explanation from an active suggestion.
- **Stage 2:** Check DB for a cached `ai_instrument_context` record that hasn't expired.
- **Stage 3:** Trigger a new context generation job via Redis Stream (`cortex:stream:context:jobs`) and poll for 45 seconds.

Stage 3 was firing — a job was being enqueued. But the explanation was never arriving.

### Step 2 — Redis State Inspection

Redis inspection revealed:

- A stale lock key `cortex:instrument_context:generating:NSE_EQ|INE00WV01027` that had persisted well beyond its 120-second TTL. This was blocking all Stage 3 re-triggers because the lock check prevented a new job from being enqueued while an "in-progress" generation appeared active.
- `cortex:llm:context:pending` had **0 subscribers** — the pub/sub channel that the context_worker listens on was empty, meaning the worker task inside uvicorn had crashed silently at some earlier point.

This confirmed the explanation worker was dead and had been dead for approximately 2 days without any alert or log surfacing the failure.

### Step 3 — Worker Task Crash Investigation

The `explanation_worker()` asyncio task runs inside the uvicorn process with no supervisor. An unhandled exception kills the task permanently; uvicorn itself keeps running and returns 200s on all HTTP endpoints — nothing in the process health indicates a dead background task.

The stale lock was the only observable artifact. Without someone manually checking Redis key state or pub/sub subscribers, this failure mode is invisible.

**Immediate mitigation applied:** deleted the stale lock key via `redis-cli del`, restarted backend to revive the worker task.

### Step 4 — Post-Restart Behaviour Observation

After restart the worker came back up. The explanation job was picked up from the Redis Stream PEL (Pending Entries List), processed, and the DB record was written. The explanation appeared in the frontend.

However, monitoring the logs during generation revealed a new, different error:

```
explanation_worker: RAG retrieval failed for context NSE_EQ|INE00WV01027
    (continuing with no context): 'HalfVector' object is not iterable
```

This was appearing on **every** context generation attempt — RAG retrieval was failing completely. The LLM was still being called (graceful fallback continues without RAG context), but the generation cycle was breaking down: the context write to `ai_instrument_context` would fail or the lock would expire before the DB write completed, causing Stage 3 to re-trigger on the next 30-second SSE poll.

### Step 5 — HalfVector Bug Root Cause

The `HalfVector object is not iterable` error traces to migration 0048 (`0048_halfvec_dim_768.py`), which changed the `ai_document_embeddings.embedding` column from `vector` to `halfvec(768)`.

pgvector's `HALFVEC` SQLAlchemy type returns `HalfVector` objects on reads — not `list[float]` like the old `Vector` type. The retriever's `list(row.embedding)` call fails because `HalfVector` has no `__iter__` protocol. The migration had been deployed and the corpus re-embedded, but no one had updated the retriever to handle the new return type.

**Fix applied:** A `_HalfVecFloatList` SQLAlchemy `TypeDecorator` was added to `app/ai/fusion/models.py`. It wraps the `HALFVEC` column and calls `value.to_list()` in `process_result_value`, enforcing `list[float]` at the ORM boundary for all consumers — no downstream code needed to change.

### Step 6 — Circuit Breaker Discovery

While examining the logs around the re-trigger loop, the following appeared within an 11-second window:

```
13:11:20  llm: Gemini generate key=-NlXkftg quota EXHAUSTED — removing from rotation
13:11:30  llm: Gemini generate key=jZfei_tA quota EXHAUSTED — removing from rotation
13:11:30  llm: Gemini generate key=GWUf3hHg quota EXHAUSTED — removing from rotation
```

All three API keys had hit their daily quota and been circuit-broken. This was the moment the explanation stopped working again — not because the worker crashed, but because no LLM calls could succeed at all until midnight PT.

This raised a second question: **why had all three keys exhausted their quota on a day with no suggestions generated and only 1 manual user interaction?**

---

## Why The Quota Investigation Was Warranted

The circuit breaker opening on all three keys simultaneously on what appeared to be a completely quiet day was a significant anomaly. The mental model at the time was that quota consumption should correlate with user activity — no trades, no suggestions, minimal interaction should mean minimal Gemini usage.

All three keys being circuit-broken simultaneously on a day when the user did nothing beyond open one watchlist item suggested either:

1. The mental model was wrong — quota consumption was not user-correlated.
2. There was a runaway loop or bug consuming far more quota than expected.
3. The free tier limits were lower than assumed.

All three turned out to be true to varying degrees.

---

## What The Quota Investigation Found

The full technical breakdown is in `GEMINI_QUOTA_EXHAUSTION_ANALYSIS.md`. The high-level finding was:

**The system's Gemini consumption is structurally decoupled from user activity.** Two background pipelines run continuously and make LLM calls against every ingested news event, independent of whether anyone is using the app:

- `event_processing_loop` → 1 sentiment call per news event
- `correlation_engine` Pathway 2 → 1 news forecast call per event × affected symbols

On June 24, 153 news events were ingested. That alone generated ~247 background calls with no user involvement. The 41 context calls for one watchlist open were an amplification caused by the HalfVector bug's re-trigger loop — normal context generation should be 1–2 calls, not 41.

The second discovery was that the token bucket configuration (`GEMINI_GENERATE_RPM=30`) is per-process. With 3 concurrent uvicorn processes all sharing the same 3 API keys in round-robin, the effective rate hitting each key under concurrent load is 30 RPM/key — 3× the free tier limit of 10 RPM. Bursts of concurrent processing trigger 429 rate-limit errors, which the circuit breaker treats identically to daily quota exhaustion, opening the key until midnight PT.

---

## Key Learnings

### About Observability

- A crashed asyncio background task leaves no process-level signal. The only observable artifact was a stale Redis lock key and a pub/sub channel with 0 subscribers. Without proactive Redis health checks, this failure mode is invisible for days.
- The new Redis Streams architecture (`cortex:stream:context:jobs`) eliminates this — XREADGROUP consumer groups survive crashes; PEL drain on startup recovers unprocessed jobs automatically. But this code is still **uncommitted and undeployed** as of 2026-06-24.

### About Schema Migrations

- Migration 0048 changed the ORM column's underlying PostgreSQL type from `vector` to `halfvec(768)`. The Python return type changed silently — the ORM annotations still showed `list[float]` but the runtime was delivering `HalfVector` objects. This type contract violation propagated silently until the first code path that assumed `list[float]` hit it.
- The correct fix is at the ORM boundary (TypeDecorator), not at each call site — enforcing the contract once, centrally, so that no downstream code ever needs to handle the pgvector-internal type.

### About Rate Limiting in Multi-Process Deployments

- A per-process token bucket does not model the actual API rate seen by the provider. In a 3-process deployment, `RPM=30` per process means `RPM=90` total across the process pool before key rotation distributes it. For a 3-key pool with 10 RPM free tier per key, the budget is 30 RPM total — matching one process, not three.
- Correcting this requires either cross-process coordination (Redis-backed rate limiter), lowering the per-process RPM to `GEMINI_GENERATE_RPM=10` to approximate correct per-key load, or moving to paid API keys where the RPM ceiling is high enough that this doesn't matter in practice.

### About The Circuit Breaker Design

- The current circuit breaker (`llm_client.py`) treats two distinct 429 error subtypes identically: actual daily quota exhaustion (`GenerateRequestsPerDay`) and per-minute rate limiting (`free_tier_requests`). The first is a hard daily wall; the second is a transient condition that self-resolves in ~60 seconds. Opening the circuit until midnight PT on a rate-limit burst converts a recoverable 60-second backoff into an all-day feature outage.
- A more correct design would use a short exponential backoff (30s → 60s → 120s) for `free_tier_requests` errors and reserve the full-day circuit open for confirmed `GenerateRequestsPerDay` exhaustion.

---

## Status of Fixes

| Fix | File(s) | Status |
|-----|---------|--------|
| HalfVector TypeDecorator | `app/ai/fusion/models.py`, `app/ai/rag/retriever.py` | UNCOMMITTED |
| context_worker 3-layer reliability (PEL drain, delivery cap, idempotency) | `app/ai/intelligence/explanation_worker.py` | UNCOMMITTED |
| Redis Streams architecture (all 9 files) | Multiple | UNCOMMITTED |
| Per-process RPM misconfiguration | `config.py` / `.env` | NOT STARTED |
| Circuit breaker distinguishing rate-limit vs daily exhaustion | `app/ai/intelligence/llm_client.py` | NOT STARTED |

---

## Timeline

| Time (UTC) | Event |
|------------|-------|
| ~2026-06-22 | explanation_worker asyncio task crashes silently; stale lock left in Redis |
| 2026-06-24 12:31 | User opens watchlist item; Stage 3 re-trigger loop begins |
| 2026-06-24 12:31–13:10 | HalfVector bug causes 41 context re-triggers for 1 instrument |
| 2026-06-24 13:04 | Backend restarted; worker revived; explanation delivered successfully |
| 2026-06-24 13:11 | All 3 circuit breakers open within 11 seconds; all LLM features offline |
| 2026-06-24 ~24:00 PT | Circuit breakers auto-reset (midnight PT watcher in `request_manager.py`) |
| 2026-06-24 | HalfVector TypeDecorator fix applied; context_worker 3-layer fix confirmed working from logs |
