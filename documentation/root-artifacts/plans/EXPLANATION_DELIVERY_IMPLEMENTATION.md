# Explanation Delivery Fix — Implementation Document

**Prerequisite reading:** `explanation-delivery-fix.md` (design document)  
**Status:** Ready for implementation  
**Implementation order is strict** — each phase depends on the one before it.

---

## Confirmed Pre-checks (Do Not Re-verify)

- `idle_in_transaction_session_timeout = 300000ms` is set on both `engine` and `worker_engine` in `database.py:97,115` — Gap 12 is already closed, no change needed.
- No new pip packages required. All patterns use `redis.asyncio` (already installed) and `asyncio` stdlib.
- No database migrations required.

---

## Implementation Order

```
Phase 1 → metrics.py          (add new counters/gauges — no deps)
Phase 2 → redis.py            (add stream key constants — no deps)
Phase 3 → explanation_worker.py   (core worker rewrite — deps: Phase 1 + 2)
Phase 4 → engine.py           (PUBLISH → XADD — deps: Phase 2)
Phase 5 → ai_stream.py        (SSE delivery rewrite — deps: Phase 2 + 3)
Phase 6 → main.py             (spawn 2 workers — deps: Phase 3)
Phase 7 → analysis.ts         (add failed field — no deps)
Phase 8 → AIExplanationPanel.tsx  (failed state UI — deps: Phase 7)
```

Phases 1 and 2 can be done simultaneously. Phases 7 and 8 can be done simultaneously with any backend phase. Phases 3, 4, 5, 6 must be sequential.

---

## Phase 1 — `backend/app/core/metrics.py`

**Add at the bottom of the file, under a new section header `── Explanation Delivery Metrics ──`:**

Five new metrics:

```python
# ── Explanation Delivery Metrics ──────────────────────────────────────────────

llm_explanation_dlq_total = Counter(
    'llm_explanation_dlq_total',
    'Total explanation/context jobs moved to the dead-letter stream after exhausting retries',
    ['job_type'],   # explanation | context
)

llm_ready_publish_failures_total = Counter(
    'llm_ready_publish_failures_total',
    'Total failures publishing the SSE wakeup signal after a successful LLM generation',
    ['job_type'],   # explanation | context
)

llm_explanation_dedup_total = Counter(
    'llm_explanation_dedup_total',
    'Total duplicate Gemini calls prevented by the two-layer deduplication guard',
    ['layer'],      # db_idempotency | inflight_key
)

llm_stream_queue_depth = Gauge(
    'llm_stream_queue_depth',
    'Current number of pending entries in the Redis Stream job queue',
    ['stream'],     # explanation | context
)

llm_explanation_worker_active = Gauge(
    'llm_explanation_worker_active',
    'Number of explanation worker coroutines currently inside an LLM call',
)
```

**No other changes to this file.**

---

## Phase 2 — `backend/app/core/redis.py`

**Add a new `RedisStreams` class immediately after the `RedisChannels` class (before the `# ── Lifecycle ──` section).**

The class holds only string constants — same pattern as `RedisChannels`. No methods needed.

```python
class RedisStreams:
    """
    Redis Stream key constants for durable job queuing and SSE event storage.

    Streams replace pub/sub channels for the LLM explanation pipeline:
      - Job queues:    XADD by publishers, XREADGROUP by workers, XACK on success.
      - Event stores:  XADD by workers, XRANGE by SSE handlers for replay.
      - DLQ:          XADD when a job exhausts MAX_DELIVERIES retries.

    Key naming follows the same cortex: namespace as RedisChannels.
    """

    # ── Explanation job queue ──────────────────────────────────────────────────
    EXPLANATION_JOBS = "cortex:stream:explanation:jobs"
    """
    Durable job queue for suggestion explanation requests.
    Replaces: RedisChannels.LLM_EXPLANATION_PENDING

    Publisher:  engine.py (after suggestion commit)
    Consumer:   explanation_worker.py (XREADGROUP, group=cortex-explanation-workers)
    Max length: ~5000 (approximate trim)
    Fields:     suggestion_id (str UUID), id (int PK)
    """

    CONTEXT_JOBS = "cortex:stream:context:jobs"
    """
    Durable job queue for instrument context requests.
    Replaces: RedisChannels.LLM_CONTEXT_PENDING

    Publisher:  ai_stream.py Stage 3 of _fetch_explanation_for_instrument
    Consumer:   explanation_worker.py (XREADGROUP, group=cortex-explanation-workers)
    Max length: ~1000 (approximate trim)
    Fields:     instrument_key (str), symbol (str|null), prediction_data (json|null)
    """

    EXPLANATION_DLQ = "cortex:stream:explanation:dlq"
    """
    Dead-letter stream. Jobs land here after exceeding MAX_DELIVERIES (3).
    No consumer group — entries are for manual inspection / replay only.
    Fields:     all original job fields + failed_msg_id, delivery_count, reason, job_type
    """

    # ── SSE event stores (per-job, TTL-bound) ─────────────────────────────────
    SSE_EVENTS_SUGGESTION = "cortex:sse:events:{suggestion_id}"
    """
    Per-suggestion event store for SSE replay.

    Written by:   explanation_worker.py after successful DB write (XADD)
    Read by:      ai_stream.py on connect (XRANGE from Last-Event-ID) and
                  on push signal (XRANGE from cursor)
    TTL:          86400s (24h — matches SUGGESTION_EXPIRY_HOURS)
    Max entries:  20 (MAXLEN trim; only the latest state matters)
    Fields:       full explanation payload including sources, available, failed flag
    """

    SSE_EVENTS_CONTEXT = "cortex:sse:events:ctx:{instrument_key}"
    """
    Per-instrument context event store for SSE replay.

    Written by:   explanation_worker.py after successful context DB write (XADD)
    Read by:      ai_stream.py on connect and on push signal
    TTL:          3600s (1h — matches watchlist refresh cadence)
    Max entries:  5 (MAXLEN trim)
    Fields:       full context payload including sources
    """

    # ── Consumer group name ────────────────────────────────────────────────────
    EXPLANATION_CONSUMER_GROUP = "cortex-explanation-workers"
    """Consumer group name used for both EXPLANATION_JOBS and CONTEXT_JOBS streams."""

    # ── Deduplication key (SET NX EX 150) ─────────────────────────────────────
    INFLIGHT_KEY = "cortex:llm:inflight:{suggestion_id}"
    """
    In-flight deduplication key. SET NX EX 150 before LLM call; DEL on completion.
    Prevents duplicate Gemini calls when two workers claim the same stream entry
    via XCLAIM or when a publisher fires the same suggestion_id twice.
    TTL: 150s (> _LLM_CALL_TIMEOUT_SECS=120 + Phase 3 DB write margin)
    """
```

**Also update the docstring in `RedisChannels.LLM_EXPLANATION_PENDING` and `LLM_CONTEXT_PENDING`** to note they are superseded by `RedisStreams.EXPLANATION_JOBS` and `CONTEXT_JOBS`. Do not delete the constants — they may appear in tests.

**Update `RedisChannels.LLM_EXPLANATION_READY` and `LLM_CONTEXT_READY` docstrings** to note the payload shape change: now carries only routing fields `{ suggestion_id, instrument_key, failed? }` — no explanation text, no sources. Explanation text lives in the event store.

---

## Phase 3 — `backend/app/ai/intelligence/explanation_worker.py`

This is the largest change. The existing file structure is:

```
Lines 1–60:    module docstring
Lines 61–105:  imports + constants
Lines 109–145: ExplanationOutput model
Lines 149–295: system prompts + guardrails
Lines 300–347: _apply_guardrails()
Lines 352–630: prompt builders
Lines 635–690: _write_audit_entry()
Lines 694–919: _generate_explanation()
Lines 924–1154: _generate_instrument_context()
Lines 1159–1385: explanation_worker() main loop
```

**Changes are confined to three areas:**

### 3a — Update constants block (`lines 96–105`)

Add alongside existing constants:

```python
# Stream consumer configuration
_CONSUMER_GROUP    = RedisStreams.EXPLANATION_CONSUMER_GROUP  # "cortex-explanation-workers"
_XCLAIM_IDLE_MS    = 60_000   # reclaim PEL entries idle for 60s (crash recovery)
_XCLAIM_INTERVAL_S = 30       # housekeeping runs every 30s
_MAX_DELIVERIES    = 3        # jobs exceeding this → DLQ
_STREAM_BLOCK_MS   = 5_000    # XREADGROUP block timeout in ms (yields event loop)

# Context lock heartbeat
_LOCK_INITIAL_TTL_MS = 45_000   # 45s initial TTL (replaces _CONTEXT_LOCK_TTL_SECS=120)
_LOCK_RENEW_EVERY_S  = 15       # renew every 15s while work is in progress

# Deduplication
_INFLIGHT_TTL_S = 150           # in-flight key TTL (> _LLM_CALL_TIMEOUT_SECS + write margin)
```

Remove `_RATE_LIMIT_REQUEUE_DELAY_SECS` — it is no longer used.

### 3b — Add new helpers after `_write_audit_entry()` and before `_generate_explanation()`

**Helper 1 — `_ensure_consumer_group()`**

Idempotent group creation. Called once at worker startup for both streams.

```python
async def _ensure_consumer_group(redis, stream_key: str) -> None:
    """Create the consumer group if it does not already exist. Idempotent."""
    try:
        await redis.xgroup_create(
            stream_key, _CONSUMER_GROUP, id="0", mkstream=True
        )
        logger.info("explanation_worker: created consumer group on %s", stream_key)
    except Exception as exc:
        if "BUSYGROUP" in str(exc):
            pass  # group already exists — expected on every restart after first run
        else:
            raise
```

**Helper 2 — `_write_event_store()`**

Writes the explanation/context payload to the SSE event store stream after a successful DB write. Called at the end of Phase 3 in both `_generate_explanation` and `_generate_instrument_context`, before the `PUBLISH` signal.

```python
async def _write_event_store(
    redis,
    stream_key: str,
    ttl_seconds: int,
    payload: dict,
) -> str | None:
    """
    XADD the payload to the per-job SSE event store, then EXPIRE the stream.
    Returns the stream entry ID (used as the SSE event id), or None on failure.
    Failure is non-fatal: the 30s poll cycle recovers the explanation from DB.
    """
    try:
        entry_id = await redis.xadd(
            stream_key,
            {k: json.dumps(v, default=str) if not isinstance(v, str) else v
             for k, v in payload.items()},
            maxlen=20,
            approximate=True,
        )
        await redis.expire(stream_key, ttl_seconds)
        return entry_id
    except Exception as exc:
        logger.warning(
            "explanation_worker: failed to write event store %s (non-fatal): %s",
            stream_key, exc,
        )
        return None
```

**Helper 3 — `_write_dlq()`**

Moves an exhausted job to the dead-letter stream, publishes a failed-state SSE event, and fires the DLQ metric.

```python
async def _write_dlq(
    redis,
    stream_key: str,
    msg_id: bytes,
    fields: dict,
    delivery_count: int,
    job_type: str,           # "explanation" | "context"
    suggestion_id: str | None = None,
    instrument_key: str | None = None,
) -> None:
    """Move an exhausted job to the DLQ, emit a failed SSE state, fire metrics."""
    from app.core.metrics import llm_explanation_dlq_total, llm_ready_publish_failures_total

    try:
        await redis.xadd(
            RedisStreams.EXPLANATION_DLQ,
            {
                **fields,
                "failed_msg_id":   msg_id.decode() if isinstance(msg_id, bytes) else msg_id,
                "delivery_count":  str(delivery_count),
                "reason":          "max_deliveries_exceeded",
                "job_type":        job_type,
            },
        )
        llm_explanation_dlq_total.labels(job_type=job_type).inc()
        logger.error(
            "explanation_worker: job moved to DLQ job_type=%s delivery_count=%d "
            "msg_id=%s suggestion_id=%s instrument_key=%s",
            job_type, delivery_count, msg_id, suggestion_id, instrument_key,
        )
    except Exception as exc:
        logger.error("explanation_worker: failed to write DLQ entry: %s", exc)

    # Publish a failed-state SSE event so the browser shows "Analysis unavailable"
    # instead of spinning the skeleton forever.
    try:
        if suggestion_id:
            event_store_key = RedisStreams.SSE_EVENTS_SUGGESTION.format(
                suggestion_id=suggestion_id
            )
            failed_payload = {
                "available": "false",
                "failed":    "true",
                "reason":    "analysis_unavailable",
            }
            await _write_event_store(redis, event_store_key, 86400, failed_payload)

            ready_channel = RedisChannels.LLM_EXPLANATION_READY.format(
                suggestion_id=suggestion_id
            )
            # instrument_key may not be in fields for older messages — graceful fallback
            ik = fields.get("instrument_key", "")
            await redis.publish(
                ready_channel,
                json.dumps({
                    "suggestion_id":  suggestion_id,
                    "instrument_key": ik,
                    "failed":         True,
                }, default=str),
            )
    except Exception as exc:
        logger.warning(
            "explanation_worker: failed to publish DLQ failed-state event "
            "for suggestion %s: %s", suggestion_id, exc,
        )
```

**Helper 4 — `_lock_heartbeat()`**

Async context manager that acquires a short-TTL lock with token ownership and renews it every `_LOCK_RENEW_EVERY_S` seconds.

```python
_LOCK_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
else
    return 0
end
"""

_LOCK_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

import contextlib
import secrets

@contextlib.asynccontextmanager
async def _lock_heartbeat(redis, lock_key: str):
    """
    Acquire a Redis lock with a short TTL and renew it every _LOCK_RENEW_EVERY_S
    seconds for the lifetime of the context. Releases atomically on exit.

    Raises RuntimeError if the lock cannot be acquired (already held).
    """
    token = secrets.token_hex(16)
    acquired = await redis.set(lock_key, token, nx=True, px=_LOCK_INITIAL_TTL_MS)
    if not acquired:
        raise RuntimeError(f"Lock already held: {lock_key}")

    stop_event = asyncio.Event()

    async def _renew():
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.sleep(_LOCK_RENEW_EVERY_S)),
                    timeout=_LOCK_RENEW_EVERY_S + 1,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                break
            if stop_event.is_set():
                break
            result = await redis.eval(
                _LOCK_RENEW_SCRIPT, 1, lock_key, token, _LOCK_INITIAL_TTL_MS
            )
            if result != 1:
                logger.warning(
                    "explanation_worker: lock %s lost during heartbeat renewal",
                    lock_key,
                )
                break

    renew_task = asyncio.create_task(_renew())
    try:
        yield
    finally:
        stop_event.set()
        renew_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await renew_task
        with contextlib.suppress(Exception):
            await redis.eval(_LOCK_RELEASE_SCRIPT, 1, lock_key, token)
```

### 3c — Modify `_generate_explanation()` (`lines 694–919`)

Three targeted changes inside the existing three-phase structure. **Do not change Phase 1 or Phase 2.** Only Phase 3 and the publish block change.

**Change 1 — DB idempotency check (Layer 1 dedup) — insert after Phase 1 DB read, before Phase 2**

After `suggestion_symbol = suggestion.symbol` and after the session closes, add:

```python
# ── Layer 1 dedup: skip if explanation already written (handles XCLAIM redelivery) ──
if suggestion.llm_summary is not None:
    from app.core.metrics import llm_explanation_dedup_total
    llm_explanation_dedup_total.labels(layer="db_idempotency").inc()
    logger.info(
        "explanation_worker: suggestion %s already has explanation — skipping "
        "(XCLAIM redelivery or duplicate publish)",
        suggestion_id,
    )
    return   # caller must XACK this entry
```

Note: This check must happen inside the `async with AsyncSessionLocal()` block, after `suggestion` is loaded and before the session closes.

**Change 2 — In-flight dedup key (Layer 2 dedup) — insert between Phase 1 and Phase 2**

After the DB session closes and after the idempotency check:

```python
# ── Layer 2 dedup: claim in-flight key; skip if another worker has it ──────
from app.core.metrics import llm_explanation_dedup_total
from app.core.redis import get_redis as _get_redis_inner, RedisStreams

_redis_inner = _get_redis_inner()
_inflight_key = RedisStreams.INFLIGHT_KEY.format(suggestion_id=suggestion_id)
_claimed_inflight = await _redis_inner.set(_inflight_key, "1", nx=True, ex=_INFLIGHT_TTL_S)
if not _claimed_inflight:
    llm_explanation_dedup_total.labels(layer="inflight_key").inc()
    logger.info(
        "explanation_worker: suggestion %s is already in-flight in another worker — skipping",
        suggestion_id,
    )
    return   # caller must NOT XACK — other worker will XACK on completion
```

**Change 3 — Replace the publish block (lines 886–914) with event store write + signal publish**

Replace the entire `# ── Publish ready notification` block with:

```python
# ── Write event store + publish wakeup signal ────────────────────────────────
if final_output is not None:
    from app.core.redis import get_redis as _get_redis_pub, RedisStreams
    from app.core.metrics import llm_ready_publish_failures_total

    _redis_pub = _get_redis_pub()

    # Write full payload (including sources) to the durable event store.
    # The SSE handler reads from here — not from the pub/sub signal.
    event_store_key = RedisStreams.SSE_EVENTS_SUGGESTION.format(
        suggestion_id=suggestion_id
    )
    event_store_payload = {
        "available":        "true",
        "failed":           "false",
        "suggestion_id":    suggestion_id,
        "instrument_key":   suggestion_symbol,   # used for SSE pre-filtering
        "llm_summary":      final_output.summary,
        "model":            f"{model_provider}/{model_id}",
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "sources":          json.dumps(sources_payload, default=str),
    }
    await _write_event_store(_redis_pub, event_store_key, 86400, event_store_payload)

    # Publish lightweight wakeup signal — routing IDs only, no payload data.
    try:
        ready_channel = RedisChannels.LLM_EXPLANATION_READY.format(
            suggestion_id=suggestion_id
        )
        await _redis_pub.publish(
            ready_channel,
            json.dumps({
                "suggestion_id":  suggestion_id,
                "instrument_key": suggestion_symbol,
                "failed":         False,
            }, default=str),
        )
    except Exception as exc:
        from app.core.metrics import llm_ready_publish_failures_total
        llm_ready_publish_failures_total.labels(job_type="explanation").inc()
        logger.warning(
            "explanation_worker: failed to publish ready signal for %s "
            "(non-fatal — poll will recover): %s",
            suggestion_id, exc,
        )

    # Release in-flight dedup key now that work is complete.
    with contextlib.suppress(Exception):
        await _redis_inner.delete(_inflight_key)
```

**Important:** The in-flight key delete must also be in a `finally` block to ensure it is always released even if Phase 3 raises. Wrap the Phase 2 + Phase 3 block in a try/finally that deletes `_inflight_key`.

### 3d — Modify `_generate_instrument_context()` (`lines 924–1154`)

**Change 1 — Replace the lock in `ai_stream.py` Stage 3** 

The context lock is acquired in `ai_stream.py:353`, not in this function. The lock heartbeat replaces that lock (see Phase 5). No change to this function's lock logic.

**Change 2 — Replace the publish block (lines 1128–1149) with event store write + signal publish**

Replace the `# ── Publish ready notification` block with:

```python
# ── Write event store + publish wakeup signal ────────────────────────────────
if final_output is not None:
    from app.core.redis import get_redis as _get_redis_pub, RedisStreams
    from app.core.metrics import llm_ready_publish_failures_total

    _redis_pub = _get_redis_pub()

    event_store_key = RedisStreams.SSE_EVENTS_CONTEXT.format(
        instrument_key=instrument_key
    )
    event_store_payload = {
        "available":       "true",
        "failed":          "false",
        "instrument_key":  instrument_key,
        "context_summary": final_output.summary,
        "context_full":    final_output.full_explanation,
        "model":           f"{model_provider}/{model_id}",
        "generated_at":    datetime.now(timezone.utc).isoformat(),
        "sources":         json.dumps(sources_payload, default=str),
    }
    await _write_event_store(_redis_pub, event_store_key, 3600, event_store_payload)

    try:
        ready_channel = RedisChannels.LLM_CONTEXT_READY.format(
            instrument_key=instrument_key
        )
        await _redis_pub.publish(
            ready_channel,
            json.dumps({
                "instrument_key": instrument_key,
                "type":           "context",
                "failed":         False,
            }, default=str),
        )
    except Exception as exc:
        llm_ready_publish_failures_total.labels(job_type="context").inc()
        logger.warning(
            "explanation_worker: failed to publish context ready signal for %s "
            "(non-fatal — poll will recover): %s",
            instrument_key, exc,
        )
```

### 3e — Replace `explanation_worker()` main loop (`lines 1159–1385`)

The entire function is replaced. Key design points to implement:

**Startup sequence (once, before the read loop):**
1. Call `_ensure_consumer_group(redis, RedisStreams.EXPLANATION_JOBS)`
2. Call `_ensure_consumer_group(redis, RedisStreams.CONTEXT_JOBS)`
3. Drain own PEL on both streams before reading new messages (cursor `"0"`, not `">"`)

**Parallel consumers:**  
The function now accepts `consumer_name: str` as a parameter — `"explanation-worker-0"` or `"explanation-worker-1"`. This is passed from `main.py` when spawning two tasks.

**Main read loop:**
```
XREADGROUP GROUP cortex-explanation-workers CONSUMER {consumer_name}
    COUNT 1 BLOCK 5000
    STREAMS cortex:stream:explanation:jobs cortex:stream:context:jobs
    > >
```

Route by stream name (first element of the result tuple). Call `_generate_explanation()` or `_generate_instrument_context()` depending on which stream the message came from.

**XACK logic:**
- On clean return from `_generate_explanation()` → `XACK`
- On `GeminiRateLimitError` → do NOT `XACK` — message stays in PEL for XCLAIM
- On `GeminiQuotaExhausted` → `XACK` (abandoning — daily quota is gone)
- On `asyncio.CancelledError` → do NOT `XACK`, re-raise
- On any other `Exception` (retry attempt < `MAX_ATTEMPTS`) → do NOT `XACK`
- When `delivery_count > _MAX_DELIVERIES` → call `_write_dlq()`, then `XACK`

**Housekeeping coroutine (runs concurrently inside the same task via `asyncio.create_task`):**

Every `_XCLAIM_INTERVAL_S` seconds, claim any PEL entries idle for `_XCLAIM_IDLE_MS` ms. This handles crashed-worker recovery. The housekeeping task is started once at `explanation_worker()` startup and cancelled on shutdown.

**Queue depth metric update:**  
Inside the housekeeping loop (or a separate 30s tick), read `XLEN cortex:stream:explanation:jobs` and `XLEN cortex:stream:context:jobs` and update `llm_stream_queue_depth` gauge.

**Shutdown:**  
On `asyncio.CancelledError`, cancel the housekeeping task, unblock the `XREADGROUP` via a local flag, and re-raise. Do not `XACK` any in-progress message — it will be reclaimed on next startup.

**Function signature change:**
```python
async def explanation_worker(consumer_name: str = "explanation-worker-0") -> None:
```

---

## Phase 4 — `backend/app/ai/correlation/engine.py`

**One change only at line 1067.**

Replace:
```python
await self.redis.publish(
    RedisChannels.LLM_EXPLANATION_PENDING,
    json.dumps({
        "suggestion_id": str(suggestion.suggestion_id),
        "id":            suggestion.id,
    }, default=str),
)
```

With:
```python
from app.core.redis import RedisStreams
await self.redis.xadd(
    RedisStreams.EXPLANATION_JOBS,
    {
        "suggestion_id":  str(suggestion.suggestion_id),
        "id":             str(suggestion.id),
        "instrument_key": str(suggestion.instrument_key),  # needed for SSE pre-filter
    },
    maxlen=5000,
    approximate=True,
)
```

Note: `instrument_key` is added to the job payload — the worker needs it to include in the event store and pub/sub signal for SSE pre-filtering.

Update the surrounding comment and the `logger.debug` line to say "stream enqueued" instead of "trigger published".

**No other changes to engine.py.**

---

## Phase 5 — `backend/app/api/v1/ai_stream.py`

Four independent changes, each in a different part of the file.

### 5a — Replace Stage 3 context lock with `_lock_heartbeat` (`lines 350–362`)

Replace:
```python
lock_key = f"cortex:instrument_context:generating:{instrument_key}"
acquired = await redis.set(lock_key, "1", nx=True, ex=_CONTEXT_LOCK_TTL_SECS)
if acquired:
    await redis.publish(
        RedisChannels.LLM_CONTEXT_PENDING,
        json.dumps({...}, default=str),
    )
```

With:
```python
from app.core.redis import RedisStreams
from app.ai.intelligence.explanation_worker import _lock_heartbeat

lock_key = f"cortex:instrument_context:generating:{instrument_key}"
try:
    async with _lock_heartbeat(redis, lock_key):
        await redis.xadd(
            RedisStreams.CONTEXT_JOBS,
            {
                "instrument_key":  instrument_key,
                "symbol":          symbol or "",
                "prediction_data": json.dumps(prediction_snapshot, default=str),
            },
            maxlen=1000,
            approximate=True,
        )
        logger.info(
            "SSE triggered instrument context generation: instrument=%s",
            instrument_key,
        )
except RuntimeError:
    # Lock already held — another SSE connection already triggered generation.
    pass
```

Remove `_CONTEXT_LOCK_TTL_SECS = 120` from the constants block at the top of the file (or leave it with a deprecation comment — do not use it).

### 5b — Replace `_watch_explanations()` (`lines 652–691`)

Replace the entire function body. The new implementation:

1. Opens a pubsub and awaits `psubscribe` directly (not inside `create_task`)
2. Reads Last-Event-ID from the outer scope (passed in as a parameter or captured via closure)
3. On connect, XRANGEs the event store to replay missed events
4. Uses `pubsub.listen()` async iterator (event-driven, zero tight-poll)
5. On signal, reads payload from event store (not from pub/sub signal body)
6. Pre-filters by `instrument_key` from signal payload before any DB query

**New function signature:** `_watch_explanations(last_event_id: str | None) -> None`

**New implementation structure:**

```python
async def _watch_explanations(last_event_id: str | None) -> None:
    from app.core.redis import RedisStreams

    pubsub = redis.pubsub()
    try:
        # Await psubscribe directly — subscription is confirmed before this
        # coroutine yields, eliminating the subscription race (Gap 2).
        await pubsub.psubscribe(
            _EXPLANATION_READY_PATTERN,
            _CONTEXT_READY_PATTERN,
        )

        # ── Replay missed events from event store (Gap 5 fix) ────────────────
        # This runs before the live loop. If Last-Event-ID is set (reconnect),
        # XRANGE returns everything that arrived since the client last saw.
        # If no Last-Event-ID, cursor is "0" — returns the most recent entry
        # (MAXLEN 20 means this is at most 20 entries, typically 1–2).
        await _replay_event_store(last_event_id)

        # ── Live event loop — event-driven, zero tight-poll (Gap 11 fix) ────
        cursor_suggestion: str = last_event_id or "0"
        cursor_context:    str = "0"

        async for msg in pubsub.listen():
            if msg.get("type") not in ("pmessage", "message"):
                continue
            channel = msg.get("channel", "")
            try:
                signal = json.loads(msg["data"])
            except (json.JSONDecodeError, TypeError):
                continue

            # ── Suggestion explanation ready ─────────────────────────────────
            if channel and str(channel).startswith("cortex:llm:explanation:ready:"):
                # Pre-filter: check instrument_key from signal — no DB query (Gap 10)
                signal_instrument_key = signal.get("instrument_key", "")
                if signal_instrument_key and signal_instrument_key != instrument_key:
                    continue

                sid = signal.get("suggestion_id")
                if not sid:
                    continue

                # Read payload from event store — not from signal body (Gap 3 fix)
                event_key = RedisStreams.SSE_EVENTS_SUGGESTION.format(
                    suggestion_id=sid
                )
                entries = await redis.xrange(
                    event_key,
                    min=f"({cursor_suggestion}" if cursor_suggestion != "0" else "-",
                    max="+",
                )
                for entry_id, fields in entries:
                    cursor_suggestion = entry_id if isinstance(entry_id, str) else entry_id.decode()
                    await _handle_store_entry(channel=f"cortex:llm:explanation:ready:{sid}", fields=fields, entry_id=cursor_suggestion)

            # ── Instrument context ready ──────────────────────────────────────
            elif channel and str(channel).startswith("cortex:llm:context:ready:"):
                signal_ik = signal.get("instrument_key", "")
                if signal_ik and signal_ik != instrument_key:
                    continue

                event_key = RedisStreams.SSE_EVENTS_CONTEXT.format(
                    instrument_key=instrument_key
                )
                entries = await redis.xrange(
                    event_key,
                    min=f"({cursor_context}" if cursor_context != "0" else "-",
                    max="+",
                )
                for entry_id, fields in entries:
                    cursor_context = entry_id if isinstance(entry_id, str) else entry_id.decode()
                    await _handle_store_entry(channel=f"cortex:llm:context:ready:{instrument_key}", fields=fields, entry_id=cursor_context)

    except asyncio.CancelledError:
        raise
    finally:
        with contextlib.suppress(Exception):
            await pubsub.punsubscribe(
                _EXPLANATION_READY_PATTERN,
                _CONTEXT_READY_PATTERN,
            )
            await pubsub.aclose()
```

**New helper `_handle_store_entry()`** — parses the Redis Stream entry fields back into a structured payload and calls `_handle_push()` or directly updates state. The stream stores all values as strings (Redis Streams are string-valued); parse `json.loads` on fields that are JSON (`sources`, `available`, `failed`).

**New helper `_replay_event_store()`** — for the active suggestion on this instrument (from Stage 1 lookup result stored in `state`), XRANGEs the event store from `Last-Event-ID` (or `"-"` if none) and processes any found entries. This runs synchronously at the start of `_watch_explanations`.

### 5c — Pass `Last-Event-ID` into the watcher

The `analysis_stream` route handler (`lines 389–453`) must:

1. Read the `Last-Event-ID` header from the incoming request. In FastAPI with sse-starlette, this arrives as a standard request header:
   ```python
   last_event_id: str | None = request.headers.get("last-event-id")
   ```

2. Pass it when spawning the watcher task:
   ```python
   asyncio.create_task(_watch_explanations(last_event_id=last_event_id))
   ```

### 5d — Fix recovery sleep in `_refresher()` (`lines 556–590`)

Add a local boolean flag `timed_out = False`. Set it to `True` in the `asyncio.TimeoutError` handler. Replace the final `await asyncio.sleep(interval)` with:

```python
_TIMEOUT_RECOVERY_SECS = 10
await asyncio.sleep(_TIMEOUT_RECOVERY_SECS if timed_out else interval)
timed_out = False  # reset for next iteration
```

---

## Phase 6 — `backend/app/main.py`

**Change only the explanation worker startup block (around lines 207–214).**

Replace the single `create_task(explanation_worker())` with two tasks:

```python
from app.ai.intelligence.explanation_worker import explanation_worker

explanation_worker_tasks = [
    asyncio.create_task(
        explanation_worker(consumer_name="explanation-worker-0"),
        name="llm_explanation_worker_0",
    ),
    asyncio.create_task(
        explanation_worker(consumer_name="explanation-worker-1"),
        name="llm_explanation_worker_1",
    ),
]
app.state.explanation_worker_tasks = explanation_worker_tasks
logger.info("LLM explanation workers started (2 parallel consumers)")
```

**Update the shutdown block (around lines 269–275)** to cancel and await both tasks:

```python
if hasattr(app.state, "explanation_worker_tasks") and app.state.explanation_worker_tasks:
    for task in app.state.explanation_worker_tasks:
        task.cancel()
    for task in app.state.explanation_worker_tasks:
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
```

Remove the old `app.state.explanation_worker_task` (singular) — update any reference to it in health check endpoints or admin routes if they exist.

---

## Phase 7 — `frontend/src/types/analysis.ts`

**One addition to `ExplanationData` interface (around line 226).**

Add `failed` field after `streaming`:

```typescript
/**
 * True when the explanation worker exhausted all retry attempts and moved the
 * job to the dead-letter queue. The panel renders "Analysis unavailable" instead
 * of the generating skeleton. Once failed, this state is permanent for the
 * lifetime of the suggestion.
 */
failed?: boolean;
```

**No other changes to this file.**

---

## Phase 8 — `frontend/src/components/AIExplanationPanel.tsx`

**One addition to the render logic (around lines 325–358).**

The current render decision tree is:
```
data === null                           → return null (hidden)
isLoading && data === null              → return <full skeleton>
data !== null && !available && !text    → return <skeleton>
data !== null && full_explanation       → render content
return null
```

Insert the `failed` check as the first branch after the null guard:

```typescript
// Permanent failure — worker exhausted all retries
if (data !== null && data.failed) {
  return (
    <Card className="...">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-muted-foreground">
          <Brain className="h-4 w-4" />
          AI Analysis
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">
          Analysis unavailable for this signal.
        </p>
      </CardContent>
    </Card>
  );
}
```

Style consistent with existing card variants in the file. Use `text-muted-foreground` (not red — this is a graceful degradation, not an error). No spinner, no retry button — the state is permanent.

**No other changes to this file.**

---

## Invariants to Maintain Throughout Implementation

1. **Never hold an `AsyncSessionLocal` session across a Redis call or an LLM call.** The three-phase pattern in `_generate_explanation` and `_generate_instrument_context` must not change — DB session opens and closes entirely within Phase 1 and Phase 3.

2. **The consumer group name must be identical everywhere.** `_CONSUMER_GROUP = "cortex-explanation-workers"` in `explanation_worker.py` and `RedisStreams.EXPLANATION_CONSUMER_GROUP` in `redis.py` must be the same string.

3. **XACK only on clean success or DLQ write.** Never XACK on `GeminiRateLimitError` or `asyncio.CancelledError`. Always XACK after `_write_dlq()` — the DLQ entry is the persistence, the stream entry can be released.

4. **The in-flight key must always be deleted.** Whether the LLM call succeeds, fails, or raises, the `cortex:llm:inflight:{suggestion_id}` key must be deleted in a `finally` block. Its 150s TTL is a safety net, not the primary release mechanism.

5. **Event store writes are non-fatal.** `_write_event_store()` must never raise. Wrap all Redis calls in try/except. The 30s poll cycle recovers the explanation from DB if the event store write fails.

6. **The pub/sub signal carries routing IDs only.** Never put explanation text, sources, or model name into the signal payload. The signal payload is: `{ suggestion_id, instrument_key, failed }` for explanations; `{ instrument_key, type, failed }` for context.

7. **`pubsub.listen()` must be cancelled cleanly.** The `_watch_explanations` task's `finally` block must `await pubsub.aclose()` — leaving the pubsub connection open on task cancel leaks a Redis connection from the pool.

8. **Two workers, one consumer group — no ordering guarantee.** The two workers consume from the same group. Job A may complete after Job B even if A was enqueued first. The SSE layer handles this correctly because each suggestion has its own event store stream keyed by `suggestion_id`.

---

## Verification Steps (After Implementation)

### Backend unit checks

1. Start the API (`uvicorn app.main:app --workers 1`). Confirm two log lines:
   ```
   LLM explanation worker started (2 parallel consumers)
   explanation_worker: created consumer group on cortex:stream:explanation:jobs
   explanation_worker: created consumer group on cortex:stream:context:jobs
   ```

2. Restart the API a second time. Confirm the `BUSYGROUP` path is silently swallowed (no error log — only the initial `created consumer group` log appears on first run).

3. Force a trade signal via the correlation engine. Confirm:
   - `XLEN cortex:stream:explanation:jobs` increments to 1 in Redis CLI
   - Within 120s: `XLEN cortex:stream:explanation:jobs` returns 0 (XACK'd)
   - `XRANGE cortex:sse:events:{suggestion_id} - +` returns 1 entry with `available=true`
   - The pub/sub signal fires (visible in `redis-cli SUBSCRIBE cortex:llm:explanation:ready:*`)

4. Trigger a second signal for the same `suggestion_id`. Confirm the dedup counter increments:
   `llm_explanation_dedup_total{layer="db_idempotency"}` or `{layer="inflight_key"}`

### SSE delivery checks

5. Open a browser tab, wait for explanation to arrive. Confirm `id=` field is set on the `analysis_update` SSE event (visible in browser DevTools → Network → EventStream).

6. Disconnect and reconnect within 5 seconds. Confirm `Last-Event-ID` header is sent on reconnect (visible in Network tab). Confirm explanation appears immediately without waiting for the 30s poll.

7. Open 50 concurrent SSE connections to the same instrument. Fire a signal. Confirm that only the matching connection's `_handle_store_entry` processes the event — others continue without DB queries.

### DLQ and failed state checks

8. Manually XADD a message to `cortex:stream:explanation:jobs` with an invalid `suggestion_id` (non-existent UUID). Let it fail 3 times. Confirm:
   - `XRANGE cortex:stream:explanation:dlq - +` contains the entry
   - `llm_explanation_dlq_total{job_type="explanation"}` increments
   - Browser SSE shows the failed state panel (not a skeleton)

### Lock heartbeat check

9. In Redis CLI, monitor `cortex:instrument_context:generating:{instrument_key}` while a context generation is in progress. Confirm TTL resets to ~45s every ~15 seconds while the LLM call is running.

---

## Deployment Notes

- **No downtime required.** The stream consumer group is created by `_ensure_consumer_group()` on first startup with `MKSTREAM=True`. If the streams do not exist yet, they are created atomically. Old pub/sub traffic (if any in-flight at deploy time) will be silently dropped — acceptable since the old system drops them anyway.

- **No Redis data migration.** Existing Redis keys from the old pub/sub system are unrelated and will expire naturally or be ignored.

- **Rollback:** If rollback is needed, revert `engine.py` (change `XADD` back to `PUBLISH`) and `main.py` (back to one worker). The stream entries will accumulate until manually flushed (`DEL cortex:stream:explanation:jobs`) or until the stream trim limit clears them. The SSE watcher change in `ai_stream.py` is backward-compatible with the old pub/sub signal format.

- **Monitor `llm_stream_queue_depth` gauge** after deploy. Under normal load this should be 0 or 1. A sustained non-zero reading indicates workers are slower than the publish rate.
