# Gemini Multi-Key Load Balancer — Implementation Plan

**Branch:** `feat/gemini-multikey-lb`  
**Scope:** Round-robin load balancer across multiple Gemini API keys with per-key Redis-backed circuit breakers. No auto-quota-recovery — circuits stay open until manually cleared via Redis.

---

## Background

The existing `GeminiRequestManager` coordinates a single API key through a priority queue + token buckets + Redis circuit breaker. With multiple paid-tier Gemini keys (each with an independent quota pool), we can multiply effective capacity by routing calls across them.

**Constraint from original design discussion:** On free tier, multi-key is ToS-violating. This implementation is written for paid tier where each key has independent quota. Circuit breakers track exhaustion but do NOT auto-reset at midnight PT — the operator manually clears exhausted keys.

---

## Architecture Overview

```
Caller (explanation_worker, nlp_engine, etc.)
        │
        ▼
CortexIntelligenceClient._acall() / .embed()
        │  acquire permit (shared queue + token buckets)
        ▼
GeminiRequestManager
        │  grant permit (rpm/tpm budgeted)
        ▼
CortexIntelligenceClient._get_next_client(op)
        │  round-robin, skip circuit-open keys
        ▼
  [key_A genai.Client]  [key_B genai.Client]  [key_C genai.Client]
        │                      │                      │
   circuit OK            circuit OK            circuit OPEN (skip)
        │
        ▼
  Gemini API call
        │
  on quota 429 ──► mark key_A circuit OPEN ──► retry with key_B
```

### Key design decisions

1. **Single shared queue / token buckets** — the manager still paces the aggregate call rate. Set `GEMINI_GENERATE_RPM` to the sum of your keys' per-key RPM (e.g., 3 keys × 150 RPM = set 450).
2. **Key ID** — `last8(api_key)` — stable, loggable, safe to write to Redis.
3. **Circuit open = no TTL** — Redis key `cortex:gemini:circuit:{op}:{key_id}` written without expiry. Clear it manually to re-enable the key.
4. **All-keys-exhausted** — `circuit_open(op)` returns `True` only when every registered key is open. This is the fast-path gate in `acquire()` and the trigger for cancelling queued permits.
5. **Caller sites untouched** — all 7 wired callers keep their existing `priority=` args; no changes outside the 4 files listed below.

---

## Files Changed

| File | Nature of change |
|------|-----------------|
| `backend/app/core/config.py` | +1 field: `GEMINI_API_KEYS` |
| `backend/app/ai/intelligence/request_manager.py` | Per-key circuit state; `key_ids` param; new Redis key pattern |
| `backend/app/ai/intelligence/llm_client.py` | Key pool; round-robin; per-key quota; retry threading |
| `backend/app/main.py` | Pass `key_ids` to `GeminiRequestManager.initialize()` |

---

## Tasks

### Task 1 — Config field

**File:** `backend/app/core/config.py`

Add after `GEMINI_API_KEY`:

```python
GEMINI_API_KEYS: list[str] = Field(
    default_factory=list,
    description=(
        "Additional Gemini API keys for multi-key load balancing (paid tier only). "
        "Combined with GEMINI_API_KEY to form the key pool. "
        "Each key must belong to a separate GCP project to have independent quota."
    ),
)
```

**Also add** a helper at module level (or as a `@property` on Settings) that returns the deduplicated, non-empty combined key list:

```python
@property
def gemini_api_key_pool(self) -> list[str]:
    """Ordered list of all configured Gemini API keys (primary first, no dupes)."""
    seen: set[str] = set()
    pool: list[str] = []
    for k in ([self.GEMINI_API_KEY] if self.GEMINI_API_KEY else []) + list(self.GEMINI_API_KEYS):
        if k and k not in seen:
            seen.add(k)
            pool.append(k)
    return pool
```

**Acceptance:** `settings.gemini_api_key_pool` returns `["key1", "key2", ...]` when both fields are set; returns `["key1"]` when only `GEMINI_API_KEY` set; returns `[]` when neither set.

---

### Task 2 — `request_manager.py` — per-key circuit state

**File:** `backend/app/ai/intelligence/request_manager.py`

#### 2a. Redis key helper

Replace `_CIRCUIT_REDIS_KEYS` dict with a function:

```python
def _circuit_redis_key(op: str, key_id: str) -> str:
    """Redis key for a per-key per-op circuit breaker."""
    return f"cortex:gemini:circuit:{op}:{key_id}"
```

Remove the old `_CIRCUIT_REDIS_KEYS` dict entirely.

#### 2b. `initialize()` signature change

```python
@classmethod
async def initialize(cls, *, redis: Redis, key_ids: list[str]) -> None:
```

- Store `inst._key_ids: list[str] = list(key_ids)` on the instance.
- Change `_circuit_state` from `dict[str, datetime | None]` to `dict[str, dict[str, bool]]`:

```python
inst._circuit_state: dict[str, dict[str, bool]] = {
    op: {kid: False for kid in key_ids}
    for op in _ALL_OPERATIONS
}
```

`True` = circuit open (key exhausted). No datetime needed — no auto-recovery.

#### 2c. `_load_circuit_state_from_redis()`

Replace the per-op loop with a per-op × per-key_id loop:

```python
for op in _ALL_OPERATIONS:
    for kid in self._key_ids:
        key = _circuit_redis_key(op, kid)
        try:
            raw = await self._redis.get(key)
            is_open = bool(raw)
            self._circuit_state[op][kid] = is_open
            _copen.labels(op=op).set(1 if self._any_circuit_open(op) else 0)
            if is_open:
                logger.warning(
                    "request_manager: Gemini %s circuit pre-loaded OPEN for key=%s",
                    op, kid,
                )
        except Exception as exc:
            logger.warning(
                "request_manager: Redis circuit-state read failed for %s key=%s (%s) "
                "— assuming closed.",
                op, kid, exc,
            )
            self._circuit_state[op][kid] = False
```

#### 2d. `_is_circuit_open(operation)` → `_all_keys_exhausted(operation)`

Rename and change semantics: returns `True` only when **every** key for the operation is marked open.

```python
def _all_keys_exhausted(self, operation: str) -> bool:
    """True only when every registered key's circuit is open for operation."""
    states = self._circuit_state.get(operation, {})
    return bool(states) and all(states.values())
```

Add a helper used for metrics:

```python
def _any_circuit_open(self, operation: str) -> bool:
    states = self._circuit_state.get(operation, {})
    return any(states.values())
```

#### 2e. `open_circuit()` signature change

```python
def open_circuit(self, operation: str, *, key_id: str) -> None:
```

- Mark `self._circuit_state[operation][key_id] = True`.
- Write to Redis **without TTL**: `await self._redis.set(key, "1")` (no `ex=` argument).
- Call `_cancel_queued_permits(operation)` **only if** `_all_keys_exhausted(operation)` becomes True after this update.
- Update the `gemini_circuit_open` metric using `_any_circuit_open()`.
- Log at ERROR level noting which key_id tripped and whether all keys are now exhausted.

```python
def open_circuit(self, operation: str, *, key_id: str) -> None:
    if key_id not in self._circuit_state.get(operation, {}):
        logger.warning(
            "request_manager: open_circuit called for unknown key_id=%s op=%s — ignored.",
            key_id, operation,
        )
        return

    self._circuit_state[operation][key_id] = True
    self._emit_circuit_metric(operation, open_=self._any_circuit_open(operation))

    asyncio.create_task(
        self._write_circuit_to_redis(operation, key_id),
        name=f"gemini_circuit_write_{operation}_{key_id}",
    )

    all_gone = self._all_keys_exhausted(operation)
    if all_gone:
        cancelled = self._cancel_queued_permits(operation)
        logger.error(
            "request_manager: ALL Gemini %s keys exhausted. "
            "Last key=%s. %d queued permits cancelled.",
            operation, key_id, cancelled,
        )
    else:
        logger.error(
            "request_manager: Gemini %s circuit OPENED for key=%s — "
            "key exhausted. Remaining keys still active.",
            operation, key_id,
        )
```

#### 2f. `_write_circuit_to_redis()` change

```python
async def _write_circuit_to_redis(self, operation: str, key_id: str) -> None:
    key = _circuit_redis_key(operation, key_id)
    try:
        await self._redis.set(key, "1")  # No TTL — user clears manually
        logger.info(
            "request_manager: Redis circuit key %s written (no TTL — clear manually to re-enable).",
            key,
        )
    except Exception as exc:
        logger.error(
            "request_manager: Failed to write circuit key %s to Redis: %s",
            key, exc,
        )
```

#### 2g. `acquire()` fast-path update

Change:

```python
if self._is_circuit_open(operation):
```

to:

```python
if self._all_keys_exhausted(operation):
```

#### 2h. `circuit_open()` public method update

```python
def circuit_open(self, operation: str) -> bool:
    """Return True if ALL registered keys' quota circuits are open for operation."""
    return self._all_keys_exhausted(operation)
```

Add a new public method for per-key queries (used by llm_client):

```python
def key_circuit_open(self, operation: str, key_id: str) -> bool:
    """Return True if this specific key's quota circuit is open."""
    return self._circuit_state.get(operation, {}).get(key_id, False)
```

#### 2i. Dispatcher `_dispatch_loop()` fast-fail step

Change `_is_circuit_open` reference to `_all_keys_exhausted`:

```python
if self._all_keys_exhausted(permit.operation):
```

#### 2j. Remove `_quota_reset_at()` from `request_manager.py`

The function `_quota_reset_at()` at module bottom is no longer used in the manager (no auto-reset). Remove it. (`llm_client.py` has its own copy for its legacy single-key path — that will also be removed in Task 3.)

**Acceptance:** Unit-test `_all_keys_exhausted()` with 1/2/all keys open. Confirm `open_circuit()` only cancels permits when the last key trips.

---

### Task 3 — `llm_client.py` — key pool + round-robin + per-key quota

**File:** `backend/app/ai/intelligence/llm_client.py`

#### 3a. Remove module-level quota state

Remove:
```python
_quota_open_until: datetime | None = None
```
And the three functions: `_quota_reset_at()`, `_quota_exhausted()`, `_open_quota_circuit()`, `_check_quota_circuit()`.

#### 3b. Key ID helper

Add at module level:

```python
def _key_id(api_key: str) -> str:
    """Stable short identifier for a key — last 8 chars, safe for logging and Redis."""
    return api_key[-8:] if len(api_key) >= 8 else api_key
```

#### 3c. `_configure()` — build key pool

Replace:
```python
self._api_key: str | None = settings.GEMINI_API_KEY
self._genai: genai.Client | None = (
    genai.Client(api_key=self._api_key) if self._api_key else None
)
```

With:
```python
raw_keys = settings.gemini_api_key_pool  # uses the new property from Task 1
self._key_pool: list[tuple[str, genai.Client]] = [
    (_key_id(k), genai.Client(api_key=k)) for k in raw_keys
]
self._next_key_idx: int = 0

# Per-key in-process quota exhaustion: op → key_id → bool
self._per_key_quota: dict[str, dict[str, bool]] = {
    _rm.Operation.GENERATE: {kid: False for kid, _ in self._key_pool},
    _rm.Operation.EMBED:    {kid: False for kid, _ in self._key_pool},
}

# Keep _genai pointing to first client for health_check() and _require_client()
self._genai: genai.Client | None = self._key_pool[0][1] if self._key_pool else None
```

#### 3d. `_require_client()` — unchanged logic, update message

```python
def _require_client(self) -> None:
    if not self._key_pool:
        raise LLMFallbackExhausted(
            "Gemini is not configured. Set GEMINI_API_KEY (and optionally "
            "GEMINI_API_KEYS) in backend/.env and restart."
        )
```

#### 3e. `_get_next_client(op)` — round-robin with circuit skip

```python
def _get_next_client(self, op: str) -> tuple[str, genai.Client]:
    """
    Return the next (key_id, genai.Client) in round-robin order,
    skipping keys whose circuit is open.

    Also syncs in-process state from GeminiRequestManager for keys that
    were marked open in a previous process (loaded from Redis).

    Raises GeminiQuotaExhausted if every key's circuit is open.
    """
    manager = _try_get_request_manager()
    n = len(self._key_pool)
    for _ in range(n):
        idx = self._next_key_idx % n
        self._next_key_idx += 1
        kid, client = self._key_pool[idx]

        # Sync from manager (covers Redis-pre-loaded state on restart)
        if manager is not None and manager.key_circuit_open(op, kid):
            self._per_key_quota[op][kid] = True

        if not self._per_key_quota[op].get(kid, False):
            return kid, client

    raise GeminiQuotaExhausted(
        f"All {n} Gemini {op} API key(s) are quota-exhausted. "
        f"Clear Redis keys cortex:gemini:circuit:{op}:* and restart to re-enable."
    )
```

#### 3f. `_mark_key_exhausted(key_id, op)` — per-key quota open

```python
def _mark_key_exhausted(self, key_id: str, op: str) -> None:
    """Mark a specific key's quota circuit open (in-process + manager + Redis)."""
    self._per_key_quota[op][key_id] = True
    logger.error(
        "llm: Gemini %s key=%s quota EXHAUSTED — removing from rotation. "
        "To re-enable: DEL cortex:gemini:circuit:%s:%s in Redis and restart.",
        op, key_id, op, key_id,
    )
    manager = _try_get_request_manager()
    if manager is not None:
        manager.open_circuit(op, key_id=key_id)
```

#### 3g. `_retry()` — add `key_id` parameter

Change signature:
```python
async def _retry(self, fn: Any, *, op: str, key_id: str) -> Any:
```

- Remove the `_check_quota_circuit(op)` fast-fail at the top (replaced by `_get_next_client` in callers).
- Change the `_is_daily_quota_exhausted` branch:

```python
if _is_daily_quota_exhausted(exc):
    self._mark_key_exhausted(key_id, op)
    raise GeminiQuotaExhausted(
        f"Gemini {op} key={key_id} aborted — daily quota exhausted."
    ) from exc
```

Remove `_open_quota_circuit(op)` call entirely.

#### 3h. `_acall()` — key-selecting loop

Replace the current body with a loop over available keys:

```python
async def _acall(
    self,
    contents: Any,
    config: genai_types.GenerateContentConfig,
    *,
    priority: Priority = Priority.MEDIUM,
    estimated_tokens: int = _DEFAULT_ESTIMATED_TOKENS,
) -> tuple[Any, int]:
    """Execute one generate_content with quota management and retry."""
    self._require_client()
    t0 = time.monotonic()
    op = _rm.Operation.GENERATE

    manager = _try_get_request_manager()
    permit = None
    if manager is not None:
        try:
            permit = await manager.acquire(op, priority, estimated_tokens)
        except _rm.GeminiQuotaExhausted as exc:
            raise GeminiQuotaExhausted(str(exc)) from exc

    last_exc: Exception | None = None
    n = len(self._key_pool)
    for _ in range(n):
        try:
            key_id, client = self._get_next_client(op)
        except GeminiQuotaExhausted:
            break  # all keys exhausted — fall through to raise below

        def _do(c: genai.Client = client) -> Any:
            return c.aio.models.generate_content(
                model=self._model, contents=contents, config=config,
            )

        try:
            response = await self._retry(_do, op=op, key_id=key_id)
            if permit is not None:
                manager.release(permit, outcome="success")
            return response, int((time.monotonic() - t0) * 1000)
        except GeminiQuotaExhausted as exc:
            last_exc = exc
            continue  # key was marked exhausted in _retry; try next key
        except Exception as exc:
            if permit is not None:
                manager.release(permit, outcome="error")
            raise

    # All keys exhausted
    if permit is not None:
        manager.release(permit, outcome="error")
    raise GeminiQuotaExhausted(
        f"All Gemini generate keys are quota-exhausted."
    ) from last_exc
```

> **Note on `_do` closure:** Use a default-argument capture (`c: genai.Client = client`) to avoid the loop-variable capture bug.

#### 3i. `embed()` — same key-selecting loop

Apply the same pattern as `_acall()`:
- Acquire permit once (before the key loop).
- Loop over `_get_next_client(Operation.EMBED)`.
- On `GeminiQuotaExhausted` from `_retry`, continue to next key.
- Release permit on first success or on final failure.

The per-key `_do` closure captures `(c: genai.Client = client)`.

#### 3j. `health_check()` — make key-aware

Currently calls `self._genai.aio.models.generate_content(...)` directly (bypasses the manager). Update to use `_get_next_client(Operation.GENERATE)` to pick a non-exhausted client:

```python
kid, client = self._get_next_client(_rm.Operation.GENERATE)
await client.aio.models.generate_content(...)
```

Catch `GeminiQuotaExhausted` and set `result["gemini"] = "all_keys_exhausted"`.

#### 3k. `get_intelligence_client()` lazy-init path

The lazy init still calls `initialize()` under the hood — no changes needed beyond the updated `_configure()`.

#### 3l. Remove now-unused imports/helpers

- Remove `from zoneinfo import ZoneInfo` (was only used by `_quota_reset_at()`).
- Remove `_quota_reset_at()`, `_quota_exhausted()`, `_open_quota_circuit()`, `_check_quota_circuit()`.

**Acceptance:** With 2 keys configured:
- First key's circuit is pre-loaded open from Redis → all calls go to key 2.
- Key 2 hits quota 429 mid-call → marked exhausted → raises `GeminiQuotaExhausted`.
- `embed()` with both keys exhausted → raises `GeminiQuotaExhausted` immediately (circuit fast-path in manager).

---

### Task 4 — `main.py` — wire key_ids into manager initialization

**File:** `backend/app/main.py`

Change the `GeminiRequestManager.initialize()` call:

```python
# Before:
await GeminiRequestManager.initialize(redis=get_redis())

# After:
_gemini_key_ids = [
    k[-8:] for k in get_settings().gemini_api_key_pool
    if len(k) >= 8
]
await GeminiRequestManager.initialize(redis=get_redis(), key_ids=_gemini_key_ids)
```

Or import the `_key_id()` function from `llm_client` to avoid duplicating the truncation logic:

```python
from app.ai.intelligence.llm_client import _key_id as _gemini_key_id

_gemini_key_ids = [_gemini_key_id(k) for k in get_settings().gemini_api_key_pool]
await GeminiRequestManager.initialize(redis=get_redis(), key_ids=_gemini_key_ids)
```

**Note:** If `key_ids` is empty (no keys configured), `GeminiRequestManager` should still initialize cleanly with an empty pool — all `_circuit_state` dicts will be empty, `_all_keys_exhausted()` will return `False` (vacuously — no keys = not exhausted), and the manager degrades gracefully.

---

### Task 5 — Operator actions (post-deploy)

These are not code changes — they must be done by the operator before/after deploying.

1. **Add keys to `.env`:**
   ```
   GEMINI_API_KEY=AIzaSy...key1...
   GEMINI_API_KEYS=["AIzaSy...key2...", "AIzaSy...key3..."]
   ```
   `GEMINI_API_KEYS` is parsed as a JSON array by Pydantic's `list[str]` field.

2. **Set aggregate RPM in `.env`:**
   ```
   GEMINI_GENERATE_RPM=450   # 3 keys × 150 RPM
   GEMINI_EMBED_RPM=270      # 3 keys × 90 RPM
   ```

3. **To manually clear an exhausted key's circuit:**
   ```bash
   redis-cli DEL cortex:gemini:circuit:generate:<last8_of_key>
   redis-cli DEL cortex:gemini:circuit:embed:<last8_of_key>
   ```
   Then restart the backend (manager pre-loads circuit state at startup).

---

## Unchanged files (no edits needed)

- All 7 caller sites: `explanation_worker.py`, `event_classifier.py`, `nlp_engine.py`, `fake_news_detector.py`, `embedder.py`, `signal_assembler.py` / `news_forecaster.py`
- `backfill_service.py`, `ingester.py`, `reembed_rag_corpus.py`
- All Prometheus metrics definitions in `metrics.py` — existing `gemini_circuit_open{op}` gauge reflects whether any key is open (same semantic as before for dashboards)

---

## Risk notes

- **`_do` closure capture:** In the key loop inside `_acall()` and `embed()`, capture `client` via default argument (`c=client`) to avoid all iterations sharing the final loop value.
- **Permit release on all-exhausted:** The permit acquired before the key loop must be released even when `GeminiQuotaExhausted` is raised. Ensure the `finally` path or explicit `manager.release(permit, outcome="error")` call is present before raising.
- **`_get_next_client` round-robin index:** `_next_key_idx` is mutated on every call. It is only accessed from the asyncio event loop (single-threaded), so no lock is needed.
- **Empty pool edge case:** If `gemini_api_key_pool` returns `[]`, `_get_next_client()` raises `GeminiQuotaExhausted` immediately (loop of 0). This is correct — same behavior as when the single key was not configured (previously raised `LLMFallbackExhausted`). The error message distinguishes the two cases.
- **Backward compatibility:** Single-key deployments (`GEMINI_API_KEY` only, `GEMINI_API_KEYS` empty) continue to work identically — the pool has one entry, round-robin always picks it.
