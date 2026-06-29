# AI Explanation Real-Time Delivery Gaps

Audited: 2026-06-23
Files: `explanation_worker.py`, `ai_stream.py`, `request_manager.py`, `llm_client.py`

---

## CRITICAL — Guaranteed delivery drops

### 1. Messages dropped while worker is processing (explanation_worker.py:1199–1240)
The worker calls `get_message()` then enters `_generate_explanation()` for 10–120s. Any Redis pub/sub messages arriving during that window are silently dropped — no persistence, no ACK, no recovery path. This is a guaranteed miss for any concurrent suggestion.

### 2. Subscription race on SSE connect (ai_stream.py:694–716)
`asyncio.create_task()` schedules `_watch_explanations` but does not run it. The task only reaches `psubscribe` after the consumer loop's first `await events.get()`. On fast workers, the `ready` publish fires before `psubscribe` completes. Recovery: next 30s poll tick (avg 15s delay).

---

## HIGH — Latency and partial recovery failures

### 3. Missed push — sources not recovered by poll (ai_stream.py:223)
`_build_explanation_payload` sets `sources=[]` explicitly on the poll path. Sources are only delivered via the push path. A missed push = permanently missing sources for that suggestion.

### 4. Redis publish failure → silent 0–30s delay (explanation_worker.py:909)
Publish exception is caught silently. Explanation is in DB but no notification fires. Poll recovers within 30s (avg 15s). Acceptable but unmonitored.

### 5. SSE reconnect loses in-flight explanation (ai_stream.py:716)
No `Last-Event-ID` resume logic. A 5-second browser disconnect that straddles a `ready` publish results in a miss recoverable only by the 30s poll. Individual events have IDs (`ai_stream.py:478`) but the endpoint ignores `Last-Event-ID` entirely.

### 6. Context lock TTL equals LLM timeout ceiling (ai_stream.py:149)
The distributed lock `SET NX EX 120` matches `_LLM_CALL_TIMEOUT_SECS = 120`. Any slow generation approaches the ceiling, the lock expires, and a reconnecting client re-triggers a duplicate generation — wasting quota and potentially causing a second sequential queue stall.

---

## MEDIUM — Scalability and latency amplifiers

### 7. Sequential worker with no queue depth guard (explanation_worker.py:1185–1357)
With N concurrent suggestions, the (N-1)th waits up to `(N-1) × 120s`. No backpressure, no admission control, no queue depth metric. Pub/sub messages arriving mid-processing are dropped (see gap 1).

### 8. Rate-limit requeue blocks all other items for 8s (explanation_worker.py:1253)
`asyncio.sleep(8)` inside the sequential loop on `GeminiRateLimitError` holds the entire queue. Every pending suggestion behind the rate-limited one waits at least 8s extra.

### 9. Refresher 25s timeout + 30s sleep = 55s dead zone (ai_stream.py:131, 123)
If Stage 1 DB query hits the 25s operation timeout, the refresher sleeps another 30s before retrying. Maximum recovery gap becomes 55s instead of 30s.

### 10. N DB queries per push — scales linearly with users (ai_stream.py:606–613)
Every `cortex:llm:explanation:ready:*` publish triggers a DB session open on every open SSE connection. N-1 of those queries return nothing (wrong instrument). The `context:ready` path correctly pre-filters in memory (`ai_stream.py:631`); the explanation path does not.

### 11. `_watch_explanations` tight-polls at 0.5s per connection (ai_stream.py:665–669)
No backoff on empty messages. 100 open connections = 200 no-op event loop awakenings/second, adding CPU contention that delays real message processing.

---

## LOW

### 12. `idle_in_transaction_session_timeout` placement unverified
The 300s guard must be set in `app/core/database.py` (session factory). Not verifiable from the four audited files. Confirm it covers all `AsyncSessionLocal` consumers.

### 13. Rate-limit retry + SSE reconnect = double-miss risk
Combining gaps 2 and 8: a user who reconnects during the 8s requeue sleep re-enters the subscription race, and can miss the eventual delivery entirely.

---

## Fix priority order

1. **Gap 1** — Replace pub/sub with Redis Stream (`XADD`/`XREAD`) for `LLM_EXPLANATION_PENDING` so messages are durable and the worker can ACK after processing.
2. **Gap 2** — Await `psubscribe` completion before yielding `stream-init`, or do an immediate DB poll before entering the push-watch loop.
3. **Gap 3** — Store sources in a Redis key alongside the payload, or always include them in the DB write so the poll path can return them.
4. **Gap 10** — Pre-filter explanation push by suggestion_id in-memory before opening a DB session.
5. **Gap 7** — Add a semaphore-bounded pool (2–3 workers) or move to a Redis Stream consumer group.
6. **Gap 5** — Implement `Last-Event-ID` resume in the SSE generator.
7. **Gap 6** — Set lock TTL to `_LLM_CALL_TIMEOUT_SECS + 30s` buffer.
8. **Gap 11** — Add `asyncio.sleep(0)` yield in the watcher loop or switch to `listen()` iterator.
