# Sentiment Batching — Implementation Plan

**Status:** Plan approved, pending blocker resolution (see bottom)
**Prerequisite:** Budget guard + 24h TTL + Priority.BACKGROUND already shipped (2026-06-27)

---

## What we're solving

Sentiment accounts for 67.6% of daily Gemini generate calls. The budget guard and cache TTL
fix (already shipped) solve the hard-circuit and recirculation problems. Batching addresses
the structural volume problem: N articles → 1 API call instead of N, reducing steady-state
call count by ~87% at batch size 8.

---

## Decisions resolved from the critique session

| Issue | Resolution |
|---|---|
| NLPEngine not a true singleton | Class-level batch queue and trigger — shared across all instances without enforcing `__new__`. Existing `NLPEngine()` call pattern at all call sites preserved |
| Audit log race condition (`_last_*` instance attrs read after `await db.flush()`) | Eliminated completely: new private method `_analyze_with_audit_meta()` returns metadata as a **return value**, not side-effecting instance state. `process_event()` consumes it directly — no shared state, no race |
| SSE path latency regression (30s wait on first load) | Two-method API: `analyze_sentiment(text)` queues for accumulation (event pipeline, background); `analyze_sentiment_batch(texts)` bypasses the queue entirely — does an immediate multi-item LLM call, used only by the SSE path |
| No index field for ordering validation | `_SentimentBatchItem` adds mandatory `index: int` field. Flusher validates result index set exactly matches submitted set and fills any gap with neutral fallback |
| asyncio dual-trigger has no clean primitive | `asyncio.Event` as size trigger + `asyncio.wait_for` as time trigger. Clear event BEFORE draining (not after) to close the race window |
| Token bucket estimate wrong for batches | Batch call passes `max_tokens = 256 * actual_batch_size`. TPM bucket handles it — `GEMINI_GENERATE_TPM = 1_000_000` at this load is not a constraint |
| Gemini default field rejection | `SentimentOutput` has no Python defaults — only Pydantic validators (`ge`, `le`, `max_length`) which are constraints, not defaults. Safe as-is |
| SSE gathers as exceptions | `analyze_sentiment_batch()` never raises — all errors return neutral dicts in-line. Existing `isinstance(sentiment, Exception)` check in `_compute()` becomes dead code but stays for defense |

---

## Internal schemas (new, private to `nlp_engine.py`)

```python
@dataclass
class _SentimentAuditMeta:
    invocation_id: UUID
    prompt_hash: str
    latency_ms: int
    error: str | None
    is_cache_hit: bool

@dataclass
class _BatchEntry:
    prompt_hash: str
    text: str
    future: asyncio.Future[dict[str, Any]]

class _SentimentBatchItem(BaseModel):
    # No Python defaults — Gemini structured output safe
    index: int = Field(description="Zero-based position matching the article's input order.")
    label: Literal["positive", "negative", "neutral"]
    score: float = Field(ge=-1.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    # reasoning field: see BLOCKER QUESTION at bottom of this document

class _SentimentBatchOutput(BaseModel):
    results: list[_SentimentBatchItem]
```

> **Note on `reasoning` max_length**: `max_length` is safe on the top-level `SentimentOutput`
> but can cause serialization issues in nested list schemas with some SDK versions. Omit it
> from `_SentimentBatchItem` regardless of whether reasoning is included.

---

## Class-level state added to `NLPEngine`

```python
class NLPEngine:
    _initialized: bool = False            # existing
    _queue: asyncio.Queue[_BatchEntry]    # NEW — class-level, shared across all instances
    _flush_trigger: asyncio.Event         # NEW — set when queue reaches batch size
    _flusher_task: asyncio.Task | None    # NEW — background flush loop
```

Set as **class attributes** by `initialize()`. All `NLPEngine()` instances share them.
Creating multiple instances (as `event_processor.py` and `sentiment_analysis_service.py` do)
is fine — they all route to the same queue.

---

## Methods — new and changed

### `initialize()` — extended
After existing no-op guard, additionally:
- Creates `cls._queue = asyncio.Queue()` (unbounded — bounded would block producers; backpressure
  is handled by the budget guard upstream)
- Creates `cls._flush_trigger = asyncio.Event()`
- Starts `cls._flusher_task = asyncio.create_task(cls._flusher_loop(), name="nlp_sentiment_flusher")`

### `aclose()` — new classmethod
- Cancels `_flusher_task`, awaits cancellation with 3s timeout
- Drains remaining `_queue` items, resolves all pending futures with neutral fallback
- Logs count of abandoned items (operator observability)
- Called from `main.py` lifespan shutdown **before** `GeminiRequestManager.aclose()`

### `_analyze_with_audit_meta(text)` — new private method → `tuple[dict, _SentimentAuditMeta]`
Replaces the body of the current `analyze_sentiment()`. Key behaviour:
1. Compute `prompt_hash`
2. Check Redis cache → on hit: return `(cached_dict, _SentimentAuditMeta(is_cache_hit=True, latency_ms=0, ...))`
3. On miss: create `asyncio.Future`, create `_BatchEntry(prompt_hash, text, future)`, enqueue
4. Check if `cls._queue.qsize() >= settings.SENTIMENT_BATCH_SIZE` → set `cls._flush_trigger`
   (unblocks flusher early, before the 30s window expires)
5. `await future` — suspends until flusher resolves it
6. Return `(future.result(), _SentimentAuditMeta(is_cache_hit=False, latency_ms=elapsed, ...))`

Latency tracking: `t0 = time.monotonic()` before enqueue; `latency_ms` measured when the
future resolves. This captures true end-to-end wait including accumulation time — accurate
for the audit log.

### `analyze_sentiment(text)` — simplified (existing public API preserved)
Thin wrapper: calls `_analyze_with_audit_meta(text)`, discards meta, returns dict.
**Signature and return type unchanged.** All existing callers work without modification.

### `process_event(db, processed_event_id, content)` — race condition fixed
Calls `_analyze_with_audit_meta(content)` instead of `analyze_sentiment(content)`.
Uses the returned `_SentimentAuditMeta` directly to build the `AILLMAuditLog` row.
**No `_last_*` instance attributes are read.** The `_last_*` attributes are removed entirely.

### `analyze_sentiment_batch(texts: list[str]) -> list[dict]` — new public method
For the SSE path. Never queues, never waits:
1. For each text: compute hash, check Redis cache
2. Partition into hits (resolved from cache) and misses (need LLM)
3. If misses: fire ONE `_call_batch_llm(miss_items)` call immediately, await result
4. Re-assemble full result list in original input order
5. Cache each miss result (same TTL as single-item path)
6. Never raises — all errors return neutral dicts for the affected positions

### `_call_batch_llm(texts_with_hashes: list[tuple[str, str]]) -> list[dict]` — new private method
Shared core used by both the flusher and `analyze_sentiment_batch`.
Receives `(text, prompt_hash)` pairs:
1. Build the numbered multi-article prompt (see prompt structure below)
2. Call `client.generate_structured(prompt, response_model=_SentimentBatchOutput,
   system=_SENTIMENT_SYSTEM_PROMPT, max_tokens=256 * len(items), priority=Priority.BACKGROUND)`
3. Parse `response.results`
4. Validate: build `{item.index: item}` dict; for every expected index not present → neutral fallback + warning log
5. Detect duplicated indices → keep first occurrence, neutral fallback for duplicates + warning log
6. Return list of dicts in submission order

### `_flusher_loop()` — new private classmethod
```
while True:
    try:
        await asyncio.wait_for(cls._flush_trigger.wait(), timeout=SENTIMENT_BATCH_WINDOW_SECS)
    except asyncio.TimeoutError:
        pass                          # window expired — flush whatever is queued
    cls._flush_trigger.clear()        # clear BEFORE drain to close the race window
    while not cls._queue.empty():
        await cls._flush_batch()      # pops up to BATCH_SIZE items per call
```

After each `_flush_batch()` call, immediately checks if more items remain. This handles
startup bursts correctly: 16 articles queued → flushed 8 → immediately flushed remaining 8,
without re-waiting 30s.

### `_flush_batch()` — new private classmethod
1. Pop up to `settings.SENTIMENT_BATCH_SIZE` items from `cls._queue` using `get_nowait()` in a loop
2. Filter: skip items whose future is already cancelled (caller timed out or was abandoned)
3. If 0 items remain after filter: return immediately
4. **Single-item shortcut**: if 1 item, call existing single-item LLM path — no batch overhead
   for singletons (avoids building a multi-item prompt for a lone article)
5. Multi-item: call `_call_batch_llm(items)`, resolve each future with its result, cache results
6. On any exception: resolve ALL pending futures with neutral fallback, log at ERROR level

---

## Prompt structure for batch calls

**System prompt**: reuse `_SENTIMENT_SYSTEM_PROMPT` unchanged.

**User prompt** (built in `_call_batch_llm`):

```
Analyze the financial sentiment of each numbered article below.
Return one result per article in the SAME ORDER as input.
Set the 'index' field to match the article number (0-based).

[0]
<article text>

[1]
<article text>

...
```

The numbered format + explicit `index` instruction + schema enforcement gives three layers of
ordering defense. Research (STED framework, arxiv:2512.23712, 2025) confirms position-sensitive
drift in LLM batch classification — explicit index anchoring in the output schema is the
correct mitigation.

---

## `sentiment_analysis_service.py` change (3 lines)

Replace lines 167–169:

```python
# BEFORE
titles = [self._extract_title(e.extra_data, e.raw_content) for e in events]
sentiment_tasks = [self._nlp.analyze_sentiment(t) for t in titles]
sentiments = await asyncio.gather(*sentiment_tasks, return_exceptions=True)

# AFTER
titles = [self._extract_title(e.extra_data, e.raw_content) for e in events]
sentiments = await self._nlp.analyze_sentiment_batch(titles)
```

The `isinstance(sentiment, Exception)` check in the loop below stays — dead code with the
new API but harmless and defensive.

---

## `config.py` additions (2 settings)

```python
SENTIMENT_BATCH_SIZE: int = Field(
    8, ge=1, le=20,
    description=(
        "Maximum articles per batched Gemini sentiment call. Research on LLM batch "
        "classification reliability (2025) places the safe ceiling at 10–15 items; "
        "8 is the conservative production default. Single-item batches bypass the "
        "multi-item prompt path entirely."
    ),
)
SENTIMENT_BATCH_WINDOW_SECS: float = Field(
    30.0, ge=1.0, le=120.0,
    description=(
        "Maximum seconds the batch accumulator waits before flushing incomplete "
        "batches. The 30s default is acceptable because: (a) the sentiment card "
        "refreshes every 120s, (b) the event pipeline is background-priority, "
        "and (c) the SSE path bypasses the queue entirely via analyze_sentiment_batch()."
    ),
)
```

---

## `main.py` additions

**Shutdown** — add immediately before `GeminiRequestManager.aclose()` (order matters):

```python
# Drain the NLP sentiment batch queue — resolves pending futures with neutral
# fallback so event pipeline coroutines unblock cleanly before the LLM
# transport is torn down.
try:
    from app.ai.intelligence.nlp_engine import NLPEngine
    await NLPEngine.aclose()
except Exception as exc:
    logger.debug("NLP engine close failed (non-fatal): %s", exc)
```

Order requirement: NLPEngine must close BEFORE GeminiRequestManager. Any in-flight batch
futures must receive neutral fallback, not a transport error from a closed Gemini client.

---

## Files touched

| File | Nature of change |
|---|---|
| `backend/app/ai/intelligence/nlp_engine.py` | Main rewrite — new dataclasses, class-level queue, flusher loop, `_analyze_with_audit_meta`, `analyze_sentiment_batch`, `_call_batch_llm`, `_flush_batch`, `aclose`, race condition fix in `process_event` |
| `backend/app/services/sentiment_analysis_service.py` | 3-line change — concurrent gather replaced with `analyze_sentiment_batch` |
| `backend/app/core/config.py` | 2 new settings |
| `backend/app/main.py` | 5-line shutdown hook |

`event_processor.py` — **no changes**. Calls `nlp_engine.process_event()` which is an
instance method; the race fix is internal to NLPEngine.

---

## Invariants to verify after implementation

1. `analyze_sentiment(text)` public signature and return type unchanged — `dict[str, Any]`
2. `process_event()` public signature unchanged — returns `AINLPResult`
3. Audit log writes exactly one row per `process_event()` call regardless of whether
   the result came from batch, single-item, or cache
4. On any LLM failure (budget throttle, quota exhaustion, 5xx), all affected futures resolve
   to `{"label": "neutral", "score": 0.0, "confidence": 0.0, "model": "unavailable"}` —
   identical to current graceful degradation
5. Result ordering: `analyze_sentiment_batch(texts)[i]` always corresponds to `texts[i]`
6. A startup burst of N articles results in `ceil(N/8)` LLM calls, not N calls
7. After `aclose()`, no flusher task is running and no futures are leaked

---

## What we are explicitly NOT doing

- No changes to `SentimentOutput` model (used by single-item path, kept as-is)
- No changes to Redis cache key format or TTL (already at 86_400 from previous session)
- No changes to `event_processor.py`
- No changes to `generate_safety_response()` or `extract_entities()`
- No bounded queue (would block producers under burst; budget guard is the backpressure layer)
- No per-entry latency tracking on batch cache-hit path (latency_ms=0 preserved, matching
  existing convention)

---

## BLOCKER — Decision required before implementation begins

**Should `reasoning` be included in `_SentimentBatchItem`?**

The existing `SentimentOutput` includes a `reasoning: str` field (max 300 chars, one-sentence
explanation of the sentiment direction). For single-item calls it is included.

For the batch schema `_SentimentBatchItem`, including it adds ~100 tokens per item
(~800 tokens per batch of 8) to the output. It is never stored in the database, never
displayed in the UI, and not consumed by any downstream signal or audit path.

**Options:**

**A — Omit `reasoning` from `_SentimentBatchItem` (recommended)**
- Saves ~800 tokens per batch call (~13% output token reduction per call)
- Reduces structured output surface area and schema complexity
- No information loss: reasoning is not consumed downstream
- Slight quality risk: reasoning acts as chain-of-thought that may improve label/score
  accuracy. Removing it might marginally degrade classification quality.

**B — Include `reasoning` in `_SentimentBatchItem`**
- Preserves chain-of-thought effect, potentially better label/score accuracy
- Costs ~800 extra output tokens per batch
- Adds 100 chars × 8 = 800 chars of output that is immediately discarded

**Recommendation: Option A.** The token cost is real, the downstream value is zero, and
the chain-of-thought benefit (if any) is speculative for a financial sentiment classification
task on a well-prompted model. If quality regression is observed in practice, add it back.

**Please confirm Option A or B before implementation starts.**
