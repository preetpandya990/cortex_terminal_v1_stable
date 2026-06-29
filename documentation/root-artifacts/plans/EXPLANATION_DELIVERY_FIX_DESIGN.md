# Explanation Delivery Fix — Design Document

**Audited:** 2026-06-23  
**Author:** Het Trivedi  
**Status:** Approved for implementation  
**Scope:** `explanation_worker.py`, `ai_stream.py`, `engine.py`, `redis.py`, `metrics.py`, `AIExplanationPanel.tsx`, `analysis.ts`

---

## 1. Problem Statement

### System Context

The AI explanation pipeline generates LLM-powered plain-English explanations for trade suggestions and watchlist instruments. The pipeline has two distinct halves:

**Job intake (how work arrives at the worker):**  
`engine.py:1067` or `ai_stream.py:355` → `PUBLISH` to a Redis pub/sub channel → `explanation_worker()` in `main.py` subscribes and processes.

**Result delivery (how the output reaches the browser):**  
`explanation_worker.py:908` → `PUBLISH cortex:llm:explanation:ready:{suggestion_id}` → `_watch_explanations()` in `ai_stream.py` → SSE event to browser.

Both halves are broken. The worker runs as a single sequential asyncio task inside the API process (`main.py:210`) — the same process serving all HTTP traffic on port 8000. There is no second process, no separate worker for on-demand tasks.

### The 13 Gaps

#### CRITICAL — Guaranteed delivery drops

**Gap 1 — Messages dropped during processing (`explanation_worker.py:1199–1240`)**  
The worker calls `pubsub.get_message()` then enters `_generate_explanation()` which blocks for 10–120 seconds (Phase 2 LLM call). Redis pub/sub is fire-and-forget: any `PUBLISH` to `cortex:llm:explanation:pending` during that processing window arrives at the subscribed socket, is not read, and is permanently discarded. A second trade signal fired while the first explanation is generating is silently lost. There is no persistence, no acknowledgment, no retry path.

**Gap 2 — SSE subscription race on connect (`ai_stream.py:694–716`)**  
`asyncio.create_task(_watch_explanations())` schedules the coroutine but does not run it immediately. The task reaches `pubsub.psubscribe(...)` only after the consumer loop yields for the first time (at `yield ServerSentEvent(comment="stream-init")`). On fast workers — where the LLM call completes before the SSE client's first event loop yield — the `PUBLISH cortex:llm:explanation:ready:{id}` fires before `psubscribe` has registered with the Redis server. The browser misses the real-time push and waits up to 30 seconds for the fallback poll.

#### HIGH — Latency spikes and partial recovery failures

**Gap 3 — Sources permanently lost on missed push (`ai_stream.py:223`)**  
`_build_explanation_payload()` hardcodes `sources=[]` on the poll path. RAG source citations (news articles, timestamps, URLs) are only carried in the Redis pub/sub payload on the push path. A missed push means the browser's explanation card shows no source attribution — ever — for that suggestion. There is no retry path for sources.

**Gap 4 — Silent publish failure (`explanation_worker.py:909`)**  
The `except Exception` block around `get_redis().publish(ready_channel, payload)` logs a warning but fires no metric. The explanation is correctly written to the database but the browser is never notified. Recovery is via the 30-second poll cycle (average 15-second delay). This gap is unmonitored — there is no counter, no alert.

**Gap 5 — SSE reconnect loses in-flight explanation (`ai_stream.py:716`)**  
The SSE endpoint emits `id=str(int(time.time() * 1000))` on every event (`ai_stream.py:478`) but the `analysis_stream` route handler (`ai_stream.py:389`) never reads the `Last-Event-ID` request header. A browser tab that disconnects for 5 seconds and reconnects misses any explanation that completed during the outage. Recovery is the 30-second poll.

**Gap 6 — Context lock TTL equals LLM timeout ceiling (`ai_stream.py:149`)**  
`_CONTEXT_LOCK_TTL_SECS = 120` and `_LLM_CALL_TIMEOUT_SECS = 120.0`. The distributed lock preventing duplicate context generation (`SET NX EX 120`) expires at the exact moment the LLM call times out. A reconnecting SSE client that triggers Stage 3 just as the lock expires re-triggers context generation — wasting quota and adding a second sequential call to the queue.

#### MEDIUM — Scalability and latency amplifiers

**Gap 7 — Sequential worker with no queue depth guard (`explanation_worker.py:1185–1357`)**  
`explanation_worker()` processes one job at a time. With N concurrent trade signals, signal N waits up to `(N-1) × 120` seconds. There is no concurrency, no queue depth metric, no backpressure. During the processing window for each job, all subsequent pub/sub messages are dropped (Gap 1).

**Gap 8 — Rate-limit requeue blocks all other items for 8 seconds (`explanation_worker.py:1253`)**  
On `GeminiRateLimitError`, the worker calls `asyncio.sleep(_RATE_LIMIT_REQUEUE_DELAY_SECS)` — 8 seconds — inside the sequential message loop before re-publishing the failed job. Every other pending explanation waits at least 8 extra seconds behind the rate-limited one. Worse, the re-publish goes back to the same pub/sub channel, which is itself fire-and-forget — if the worker is sleeping, the re-published message may be dropped immediately.

**Gap 9 — Refresher 25s timeout + 30s sleep = 55s dead zone (`ai_stream.py:131, 590`)**  
`_OPERATION_TIMEOUT_SECS = 25` and `_EXPLANATION_REFRESH_SECS = 30`. When `_refresh_explanation()` hits the 25-second timeout, `_refresher()` logs a warning and immediately calls `await asyncio.sleep(interval)` — another 30 seconds — before retrying. Maximum recovery gap from a timed-out DB query: 55 seconds instead of 30.

**Gap 10 — N DB queries per push, N-1 are wasted (`ai_stream.py:606–613`)**  
Every `cortex:llm:explanation:ready:*` publish triggers `_handle_push()` on every open SSE connection. Each handler opens a new `AsyncSessionLocal()` and queries `TradeSuggestion` by `suggestion_id + instrument_key`. For 50 open connections, 49 of those queries return `None` because the push is for a different instrument. The pub/sub payload carries `suggestion_id` but not `instrument_key`, so there is no way to pre-filter without the DB query.

**Gap 11 — `_watch_explanations` tight-polls at 0.5s per connection (`ai_stream.py:665–669`)**  
The watcher loop calls `pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)` in a `while True` loop. On empty return it calls `continue` immediately. With 100 open SSE connections: 200 no-op event loop awakenings per second, all competing for the asyncio thread. This adds measurable CPU contention that delays real message delivery.

#### LOW

**Gap 12 — `idle_in_transaction_session_timeout` placement unverified**  
The 300-second guard must be set in `app/core/database.py` at session factory level. Unverifiable from the four audited files — risk of DB connections held idle in transaction exhausting the pool under load.

**Gap 13 — Rate-limit retry + SSE reconnect = compound double-miss**  
Gaps 2 and 8 combine: a user who reconnects during the 8-second rate-limit sleep re-enters the subscription race (Gap 2) and can miss the eventual delivery entirely because the re-queued pub/sub message may have already fired before `psubscribe` completed.

---

## 2. Solution Options Considered

### Option Set A — Minimum viable patches (rejected)

Apply targeted fixes to each gap individually without changing the delivery architecture:

- Gap 1/7/8: Wrap `get_message()` in a local `asyncio.Queue`, pre-buffer messages while processing.
- Gap 2: Do an immediate DB poll before yielding `stream-init`.
- Gap 3: Store sources in a Redis hash keyed by `suggestion_id` with a TTL.
- Gap 5: Store the last explanation payload in Redis per-instrument; read on reconnect.
- Gap 10: Add `instrument_key` to the pub/sub payload.
- Gap 11: Replace tight-poll with `pubsub.listen()` iterator.

**Why rejected:** Gaps 1, 7, and 8 cannot be properly fixed without replacing pub/sub for the job queue. A local `asyncio.Queue` pre-buffer fixes the drop problem within a single process restart but loses all messages on crash or restart. The re-queue on rate-limit (`asyncio.sleep(8)` + `PUBLISH`) remains broken because the re-published message can be dropped again immediately. Patching each gap individually produces a system that is slightly less broken but architecturally still fragile — six separate Redis keys, no unified durability guarantee, no crash recovery.

### Option Set B — Redis Streams for the job queue only (partially considered)

Replace `PUBLISH/SUBSCRIBE` for the job intake channel with `XADD/XREADGROUP`. Keep the delivery channel (ready notifications) as pub/sub with the existing `_watch_explanations` watcher.

**Why partially considered:** Fixes Gaps 1, 7, 8 cleanly. Does not fix Gaps 2, 3, 5 (delivery side). The pub/sub subscription race and the sources-lost-on-poll problems remain. Would require a second pass.

### Option Set C — Full delivery architecture replacement (selected)

Replace pub/sub for the job queue with Redis Streams AND add a per-suggestion durable event store for the delivery side. Pub/sub is retained only as a lightweight wakeup signal — the actual payload always lives in the stream. This fixes all 13 gaps in a single coherent change.

---

## 3. Final Design — What We Are Building

### Decision log

| Question | Answer | Rationale |
|---|---|---|
| Parallel explanation workers | 2 | Doubles throughput vs current; avoids the quota pressure of 3× given past rate-limit incidents |
| Event store TTL — suggestion explanations | 24 hours | Matches `SUGGESTION_EXPIRY_HOURS = 24` in `engine.py:48`; a suggestion lives 24h so the replay window should too |
| Event store TTL — instrument context | 1 hour | Matches the watchlist card refresh cadence; context regenerates every 2h in DB (`explanation_worker.py:1061`) |
| DLQ behaviour | Option B — failed state in UI | Spinning skeleton forever on exhausted retries is broken UX; need a third state |
| Deduplication | In-flight Redis key + DB idempotency check | Prevents duplicate Gemini calls for same `suggestion_id` from concurrent triggers or XCLAIM redelivery |

---

### 3.1 — Job Queue: pub/sub → Redis Streams

**Problem solved:** Gaps 1, 7, 8, 13

**Before:**  
```
engine.py:1067   → redis.publish("cortex:llm:explanation:pending", payload)
ai_stream.py:355 → redis.publish("cortex:llm:context:pending", payload)
explanation_worker.py:1187 → pubsub.subscribe(LLM_EXPLANATION_PENDING, LLM_CONTEXT_PENDING)
```

**After:**  
```
engine.py        → redis.xadd("cortex:stream:explanation:jobs", payload, maxlen=5000)
ai_stream.py     → redis.xadd("cortex:stream:context:jobs", payload, maxlen=1000)
explanation_worker → xreadgroup(GROUP, CONSUMER, {"cortex:stream:explanation:jobs": ">"})
                  → xreadgroup(GROUP, CONSUMER, {"cortex:stream:context:jobs": ">"})
                  → xack(...) only after successful completion
```

**Consumer group:** `cortex-explanation-workers`  
**Consumers:** `explanation-worker-0`, `explanation-worker-1` (two asyncio tasks, spawned in `main.py`)  
**Context stream:** single consumer `context-worker-0` (context jobs are low-frequency and already protected by distributed lock)  
**Max stream length:** `MAXLEN ~ 5000` for explanation jobs, `MAXLEN ~ 1000` for context jobs (approximate trim, O(1) amortized)

**Rate-limit handling:** On `GeminiRateLimitError`, do NOT `XACK`. The message stays in the Pending Entries List (PEL). A housekeeping coroutine running every 30 seconds claims any PEL entry idle for more than 60 seconds via `XCLAIM` and re-delivers it. No `asyncio.sleep(8)` in the main loop. No re-publish to a fire-and-forget channel. The rate-limit backoff happens naturally as the message waits in PEL.

**Dead-letter queue:** After `delivery_count > MAX_ATTEMPTS (3)` via `XPENDING_RANGE`, move to `cortex:stream:explanation:dlq`. On DLQ write, publish a final SSE failed-state event (see Section 3.3). Fire `llm_explanation_dlq_total` Prometheus counter.

**Crash recovery on startup:** Before entering the `>` (new messages) read loop, each consumer drains its own PEL by reading with cursor `0` until empty. Any message delivered before the last crash but not acknowledged is reprocessed.

---

### 3.2 — Delivery: Event Store + Race-Free SSE Watcher

**Problem solved:** Gaps 1, 2, 3, 5, 10, 11

**Core principle:** Pub/sub carries only a routing signal — `{ suggestion_id, instrument_key }`. The actual payload (summary, full_explanation, sources, model, generated_at) lives in a per-suggestion Redis Stream (event store). The SSE handler always reads from the store, never from the pub/sub payload.

**Worker side — after writing to DB (Phase 3 of `_generate_explanation`):**
```
XADD cortex:sse:events:{suggestion_id}  MAXLEN 20  {full payload including sources}
EXPIRE cortex:sse:events:{suggestion_id}  86400    ← 24-hour TTL (suggestion lifetime)

PUBLISH cortex:llm:explanation:ready:{suggestion_id}  {"suggestion_id": "...", "instrument_key": "..."}
                                                       ↑ signal only — no payload data
```

**Worker side — context generation:**
```
XADD cortex:sse:events:ctx:{instrument_key}  MAXLEN 5  {full context payload}
EXPIRE cortex:sse:events:ctx:{instrument_key}  3600    ← 1-hour TTL

PUBLISH cortex:llm:context:ready:{instrument_key}  {"instrument_key": "...", "type": "context"}
```

**SSE handler — on connect:**
```
1. Read Last-Event-ID header (standard W3C SSE resume header)
2. Determine suggestion_id for this instrument_key (from DB, same Stage 1 lookup)
3. If Last-Event-ID is set: XRANGE cortex:sse:events:{suggestion_id} (Last-Event-ID, +]
   → replay missed events immediately before entering live mode
4. await pubsub.psubscribe(...)   ← awaited directly, not in a create_task
5. yield ServerSentEvent(comment="stream-init")   ← subscription confirmed before this
```

**SSE handler — on pub/sub signal:**
```
1. Parse instrument_key from signal payload
2. If instrument_key != this connection's instrument_key: return  ← zero DB queries (Gap 10)
3. XRANGE cortex:sse:events:{suggestion_id} from (last_seen_cursor, +]
   → read payload from event store, not from pub/sub signal
4. Yield SSE event with id = stream entry ID
```

**SSE watcher — replace tight-poll with event-driven listener:**
```python
# Before (Gap 11 — 200 no-op awakenings/second with 100 connections):
while True:
    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
    if msg is None or msg.get("type") != "pmessage":
        continue

# After (0 CPU until a message arrives):
async for msg in pubsub.listen():
    if msg.get("type") not in ("pmessage", "message"):
        continue
```

**Why this eliminates all delivery gaps:**
- Gap 1 (drop during processing): Events are in the stream before `PUBLISH` fires. Poll path reads from stream on next cycle anyway.
- Gap 2 (subscription race): Even if the `psubscribe` races, the `XRANGE` replay on connect recovers any missed events immediately. Pub/sub is now just a wakeup — missing it costs at most one 30-second poll cycle, not a permanent loss.
- Gap 3 (sources lost on poll): Sources are stored in the event store entry. Both push and poll paths read from the same store.
- Gap 5 (reconnect miss): `Last-Event-ID` + `XRANGE` gives exact resume. Browser reconnects mid-generation and gets all missed events.
- Gap 10 (N DB queries): `instrument_key` is in the pub/sub signal. Non-matching connections return before opening any DB session.
- Gap 11 (tight poll): `pubsub.listen()` is event-driven.

---

### 3.3 — DLQ Failed State: UI Feedback (Option B)

**Problem solved:** Gap 7 (partial), user experience for exhausted retries

**Backend:** When a job is moved to `cortex:stream:explanation:dlq`, the worker publishes a final SSE event to the event store before terminating:
```
XADD cortex:sse:events:{suggestion_id}  { "failed": true, "available": false, "reason": "analysis_unavailable" }
PUBLISH cortex:llm:explanation:ready:{suggestion_id}  { "suggestion_id": "...", "instrument_key": "...", "failed": true }
```

**Frontend type change (`analysis.ts`):**  
Add `failed?: boolean` to `ExplanationData` interface. When `failed === true`, the panel renders "Analysis unavailable" instead of the skeleton.

**Frontend component change (`AIExplanationPanel.tsx`):**  
Add a third render branch between skeleton and content:
```
data === null           → panel hidden
data.available = false
  data.failed = true    → "Analysis unavailable for this signal" (permanent, no spinner)
  data.failed != true   → skeleton / generating spinner
data.available = true   → full explanation content
```

---

### 3.4 — Lock TTL Heartbeat for Context Generation

**Problem solved:** Gap 6

The context generation lock at `ai_stream.py:353`:
```python
acquired = await redis.set(lock_key, "1", nx=True, ex=_CONTEXT_LOCK_TTL_SECS)  # 120s
```

Replace with a 45-second initial TTL and an asyncio heartbeat task that renews every 15 seconds using an atomic Lua script that checks ownership before extending:
```lua
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
```

The lock token is a `secrets.token_hex(16)` unique per acquisition. If the process crashes, the lock expires in ≤45 seconds instead of ≤120 seconds. If the LLM call runs slow (90 seconds), the heartbeat has renewed the lock 4 times. Duplicate generation from reconnecting clients is impossible as long as the original worker is alive.

---

### 3.5 — Gemini Deduplication

**Problem solved:** Prevents wasteful duplicate Gemini calls from concurrent triggers or XCLAIM redelivery

Two layers:

**Layer 1 — DB idempotency check (before acquiring Gemini permit):**  
At the start of `_generate_explanation()`, after Phase 1 DB read, check if `suggestion.llm_summary is not None`. If the explanation already exists, skip Phase 2 entirely, write a `skipped` audit entry, and `XACK` immediately. This handles XCLAIM redelivery after a crash where Phase 3 completed but the ACK did not.

**Layer 2 — In-flight Redis key (across concurrent workers):**  
Before entering the LLM call, set `SET cortex:llm:inflight:{suggestion_id} 1 NX EX 150`. If the key already exists, another worker has this job. Release the stream entry (do not ACK — let it be claimed by the other worker after it finishes), log a dedup event, return. The TTL is 150 seconds (longer than `_LLM_CALL_TIMEOUT_SECS = 120` to cover Phase 3 DB write). On normal completion, delete the key immediately rather than waiting for TTL expiry.

---

### 3.6 — Recovery Sleep (Gap 9)

**Problem solved:** Gap 9 — 55-second dead zone after timeout

In `_refresher()` at `ai_stream.py:556`, after a `TimeoutError`:
```python
# Before: always sleeps full interval (30s after a 25s timeout = 55s dead zone)
await asyncio.sleep(interval)

# After: shorter recovery on timeout only
_TIMEOUT_RECOVERY_SECS = 10
await asyncio.sleep(_TIMEOUT_RECOVERY_SECS if timed_out else interval)
```

Maximum dead zone after timeout: 25s + 10s = 35 seconds.

---

### 3.7 — Publish Failure Metric (Gap 4)

Add `llm_ready_publish_failures_total` Counter to `metrics.py` with label `job_type` (`explanation | context`). Increment inside the `except` block in `_generate_explanation():909` and `_generate_instrument_context():1144`. Zero code-path change — one counter increment added.

---

### 3.8 — `idle_in_transaction_session_timeout` Verification (Gap 12)

Read `backend/app/core/database.py` and confirm `idle_in_transaction_session_timeout` is set in the session factory `connect_args`. If missing, add `"options": "-c idle_in_transaction_session_timeout=300000"` (300 seconds in milliseconds for PostgreSQL). This is a one-line change if absent.

---

## 4. New Redis Key Namespace

```
# Job streams (durable, consumer groups)
cortex:stream:explanation:jobs       XADD by engine.py
cortex:stream:context:jobs           XADD by ai_stream.py Stage 3
cortex:stream:explanation:dlq        XADD on exhausted retries

# SSE event stores (per-job, TTL-bound)
cortex:sse:events:{suggestion_id}    TTL: 86400s (24h)
cortex:sse:events:ctx:{instrument_key}  TTL: 3600s (1h)

# Deduplication
cortex:llm:inflight:{suggestion_id}  TTL: 150s, SET NX, deleted on completion

# Context generation lock (existing key, new lock implementation)
cortex:instrument_context:generating:{instrument_key}  TTL: 45s + heartbeat renewal

# Existing pub/sub channels — retained as wakeup signals only
cortex:llm:explanation:ready:{suggestion_id}   payload: { suggestion_id, instrument_key, failed? }
cortex:llm:context:ready:{instrument_key}      payload: { instrument_key, type: "context" }

# Existing pub/sub job channels — REMOVED after stream migration
cortex:llm:explanation:pending  ← replaced by cortex:stream:explanation:jobs
cortex:llm:context:pending      ← replaced by cortex:stream:context:jobs
```

---

## 5. New Prometheus Metrics

```
llm_explanation_dlq_total           Counter  job_type=[explanation|context]
llm_ready_publish_failures_total    Counter  job_type=[explanation|context]
llm_explanation_dedup_total         Counter  layer=[db_idempotency|inflight_key]
llm_stream_queue_depth              Gauge    stream=[explanation|context]
llm_explanation_worker_active       Gauge    (number of workers currently in LLM call)
```

---

## 6. File Change Summary

| File | Nature of change |
|---|---|
| `backend/app/core/redis.py` | Add stream key constants; add `RedisStreams` class alongside `RedisChannels` |
| `backend/app/ai/intelligence/explanation_worker.py` | Replace pub/sub consumer loop with XREADGROUP consumer group; add PEL drain on startup; add in-flight dedup key; add event store write before PUBLISH; add DLQ path + failed-state event; add lock heartbeat to `_generate_instrument_context` |
| `backend/app/api/v1/ai_stream.py` | Replace `_watch_explanations` tight-poll with `listen()` iterator; add `Last-Event-ID` replay on connect; pre-filter pushes by `instrument_key` without DB query; read payload from event store not pub/sub; shorter timeout recovery sleep in `_refresher`; Stage 3 context lock uses heartbeat pattern |
| `backend/app/ai/correlation/engine.py` | `redis.publish(LLM_EXPLANATION_PENDING, ...)` → `redis.xadd("cortex:stream:explanation:jobs", ...)` |
| `backend/app/main.py` | Spawn 2 explanation worker tasks (`explanation-worker-0`, `explanation-worker-1`) instead of 1; cancel both on shutdown |
| `backend/app/core/metrics.py` | Add 5 new metrics (DLQ counter, publish failure counter, dedup counter, queue depth gauge, active workers gauge) |
| `backend/app/core/database.py` | Verify / add `idle_in_transaction_session_timeout` in session factory |
| `frontend/src/types/analysis.ts` | Add `failed?: boolean` to `ExplanationData` interface |
| `frontend/src/components/AIExplanationPanel.tsx` | Add third render branch for `failed === true` state |

**No new Python dependencies. No database migrations. No changes to any other frontend component.**

---

## 7. Gaps-to-Fix Traceability

| Gap | Severity | Fixed by |
|---|---|---|
| 1 — Drop during processing | CRITICAL | Section 3.1 (Redis Streams job queue) |
| 2 — SSE subscription race | CRITICAL | Section 3.2 (event store + XRANGE replay on connect) |
| 3 — Sources lost on poll | HIGH | Section 3.2 (sources in event store) |
| 4 — Silent publish failure | HIGH | Section 3.7 (metric counter) |
| 5 — Reconnect loses in-flight | HIGH | Section 3.2 (Last-Event-ID + XRANGE) |
| 6 — Lock TTL = LLM timeout | HIGH | Section 3.4 (lock heartbeat) |
| 7 — No queue depth guard | MEDIUM | Section 3.1 (streams + 2 consumers) |
| 8 — 8s rate-limit blocks queue | MEDIUM | Section 3.1 (no-ACK + XCLAIM replaces sleep+re-publish) |
| 9 — 55s dead zone | MEDIUM | Section 3.6 (shorter recovery sleep) |
| 10 — N DB queries per push | MEDIUM | Section 3.2 (instrument_key in signal, pre-filter) |
| 11 — 0.5s tight-poll | MEDIUM | Section 3.2 (pubsub.listen() iterator) |
| 12 — idle_in_transaction unverified | LOW | Section 3.8 (verify database.py) |
| 13 — Compound double-miss | LOW | Section 3.1 + 3.2 (Gaps 2 and 8 both fixed) |
| — Duplicate Gemini calls | New | Section 3.5 (two-layer deduplication) |
| — Skeleton forever on failure | New | Section 3.3 (DLQ failed state in UI) |
