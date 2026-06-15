# Gemini Request Manager — Design Document

**Status:** Design approved, pending implementation  
**Branch:** `feat/gemini-ai-service`  
**Author:** Cortex AI Team  
**Date:** 2026-06-13

---

## 1. Problem Statement

Six independent callers share one Gemini API key with no coordination. Under
market-hours load they burst simultaneously, hit the per-minute limit, and
retries starve each other. A process restart loses circuit state, wasting the
first post-restart call on a guaranteed 429.

### Current call sites

| Caller | Method | Priority today |
|--------|--------|---------------|
| `explanation_worker` — signal explanation | `generate_structured_with_usage` | none |
| `explanation_worker` — instrument context | `generate_structured_with_usage` | none |
| `signal_assembler` — news forecaster | `generate_structured` | none |
| `nlp_engine` — sentiment | `generate_structured` | none |
| `event_classifier` | `generate_structured` | none |
| `embedder` — RAG backfill | `embed` | none |

### Known gaps

| Gap | Impact |
|-----|--------|
| No RPM tracking across callers | Callers collectively burst past the tier limit |
| No TPM tracking | No visibility into token consumption rate |
| Circuit breaker is process-local | Restart re-exposes the first call to a 429 |
| No per-operation budget allocation | RAG backfill competes with live signal pipeline |
| No Prometheus metrics for quota state | No alerting on circuit open, retry rate, RPM utilisation |

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  GeminiRequestManager                   │
│                   (new singleton)                       │
│                                                         │
│  ┌─────────────────┐     ┌─────────────────────────┐   │
│  │  Priority Queue  │     │    Async Token Buckets   │   │
│  │  asyncio.PQ      │     │                         │   │
│  │                 │     │  generate: RPM + TPM     │   │
│  │  CRITICAL  [1]  │──►  │  embed:    RPM only      │   │
│  │  HIGH      [2]  │     │                         │   │
│  │  MEDIUM    [3]  │     │  Refill: continuous      │   │
│  │  LOW       [4]  │     │  Callers wait, not poll  │   │
│  │  BACKGROUND[5]  │     └─────────────────────────┘   │
│  └─────────────────┘                                    │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Circuit Breaker  (Redis-backed)                 │   │
│  │  cortex:gemini:circuit:generate  TTL=til midnight│   │
│  │  cortex:gemini:circuit:embed     TTL=til midnight│   │
│  │  In-process cache → Redis check on miss          │   │
│  └──────────────────────────────────────────────────┘   │
│                                                         │
│  ┌──────────────────────────────────────────────────┐   │
│  │  Prometheus Metrics                              │   │
│  │  gemini_requests_total{op, priority, status}     │   │
│  │  gemini_rpm_utilisation{op}          gauge 0–1   │   │
│  │  gemini_tpm_utilisation              gauge 0–1   │   │
│  │  gemini_queue_depth{priority}        gauge       │   │
│  │  gemini_circuit_open{op}             gauge 0/1   │   │
│  │  gemini_permit_wait_seconds{op,pri}  histogram   │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
         │
         │  acquire(op, priority, est_tokens) → permit
         │  release(permit, actual_tokens)
         ▼
┌──────────────────────┐
│  CortexIntelligence  │  ← priority param added to all public methods
│  Client._acall()     │  ← calls manager.acquire() before every API call
│  .embed()            │
└──────────────────────┘
```

---

## 3. Component Detail

### 3.1 Priority enum

```python
class Priority(IntEnum):
    CRITICAL   = 1  # health_check, startup probe
    HIGH       = 2  # explanation_worker — signal explanation (user facing)
    MEDIUM     = 3  # forecaster, sentiment, event_classifier
    LOW        = 4  # instrument_context (watchlist background)
    BACKGROUND = 5  # RAG embeddings, eval harness
```

### 3.2 Async Token Buckets

Two independent buckets — generate and embed have separate Gemini quotas.

**Generate bucket**
```
rpm_capacity = GEMINI_GENERATE_RPM   (default: 150 — Paid Tier 1)
tpm_capacity = GEMINI_GENERATE_TPM   (default: 1_000_000)
refill rate  = rpm / 60 tokens/sec  (continuous, not burst)
burst cap    = min(rpm, 10)          (prevents one caller eating a minute's budget)
```

**Embed bucket**
```
rpm_capacity = GEMINI_EMBED_RPM      (default: 90 — free tier conservative)
tpm          = not tracked           (embed API limit is RPM-based only)
```

**Token accounting**
- Before call: acquire `1` RPM token + `est_tokens` TPM tokens
- `est_tokens` = `max_tokens` setting for generate, `len(texts)` for embed
- After call: credit back `(est_tokens - actual_output_tokens)` to TPM bucket
- Overshoot is rare; self-corrects within seconds

### 3.3 Priority Queue — single dispatcher

Internal permit dataclass (sorted by priority then sequence for FIFO within tier):

```python
@dataclass(order=True)
class _Permit:
    priority:         int            # sort key 1
    sequence:         int            # sort key 2 — FIFO within same priority
    operation:        Operation      # compare=False
    estimated_tokens: int            # compare=False
    ready:            asyncio.Event  # compare=False — set when caller may proceed
    cancelled:        bool = False   # compare=False — set on timeout or circuit open
```

**Dispatcher loop** (single background coroutine):
1. Peek at highest-priority permit in the queue
2. Check if token buckets can satisfy it now
3. **Yes:** deduct tokens → set `permit.ready` → pop
4. **No:** `asyncio.sleep(refill_delay)` → retry
5. **Circuit open:** set `permit.cancelled` → pop (caller raises `GeminiQuotaExhausted` instantly)

**Backpressure:** `GEMINI_MAX_QUEUE_DEPTH` (default 50) caps total queue depth.
Callers that exceed it receive `GeminiRateLimitError` immediately — no blocking.
The RAG backfill's own token bucket handles this gracefully already.

### 3.4 Redis-backed circuit breaker

**Redis keys**
```
cortex:gemini:circuit:generate  →  "1"   TTL = seconds until midnight PT
cortex:gemini:circuit:embed     →  "1"   TTL = seconds until midnight PT
```

**Read path (every call — fast)**
1. Check in-process `_quota_open_until` — sub-millisecond, covers 99.9% of calls
2. On miss (None): `GET cortex:gemini:circuit:{op}` — once per restart, then cached locally
3. If Redis key exists: populate in-process state and fast-fail

**Write path (on quota 429 detection)**
1. `SET cortex:gemini:circuit:{op} "1" EX <ttl>` — atomic, overwrites stale TTL
2. Set `_quota_open_until` in-process

**Restart behaviour**: first call checks Redis, never wastes an API call on a known-exhausted quota.

### 3.5 Caller priority assignments

| Caller | Method | Priority |
|--------|--------|----------|
| `explanation_worker._generate_explanation` | `generate_structured_with_usage` | `HIGH` |
| `explanation_worker._generate_instrument_context` | `generate_structured_with_usage` | `LOW` |
| `signal_assembler.gather_news_forecast` | `generate_structured` | `MEDIUM` |
| `nlp_engine.analyze_sentiment` | `generate_structured` | `MEDIUM` |
| `event_classifier._classify_with_llm` | `generate_structured` | `MEDIUM` |
| `nlp_engine.generate_safety_response` | `generate` | `LOW` |
| `embedder.embed_texts` | `embed` | `BACKGROUND` |

---

## 4. Public API

### `app/ai/intelligence/request_manager.py` (new file)

```python
class GeminiRequestManager:
    @classmethod
    async def initialize(cls, redis: Redis) -> None
        # Singleton init. Checks Redis circuit state before accepting calls.

    async def acquire(
        self,
        operation: Operation,
        priority: Priority,
        estimated_tokens: int = 1400,
    ) -> _Permit
        # Block (async) until a permit is granted or timeout expires.
        # Raises GeminiQuotaExhausted if circuit is open.
        # Raises GeminiRateLimitError if queue is full (backpressure).

    def release(self, permit: _Permit, actual_tokens: int = 0) -> None
        # Return unused token budget to the TPM bucket. Always call in finally.

    def open_circuit(self, operation: Operation) -> None
        # Open the circuit for this operation. Writes to Redis + in-process state.

    def circuit_open(self, operation: Operation) -> bool
        # True if the circuit is currently open.

    async def aclose(self) -> None
        # Cancel dispatcher, drain queue with cancellation.


def get_request_manager() -> GeminiRequestManager: ...
```

### Changes to `CortexIntelligenceClient`

All public methods gain an optional `priority` keyword argument:

```python
async def generate(self, ..., priority: Priority = Priority.MEDIUM) -> dict
async def generate_json(self, ..., priority: Priority = Priority.MEDIUM) -> dict
async def generate_structured(self, ..., priority: Priority = Priority.MEDIUM) -> T
async def generate_structured_with_usage(self, ..., priority: Priority = Priority.HIGH) -> tuple
async def embed(self, ..., priority: Priority = Priority.BACKGROUND) -> list
```

`_acall()` and `embed()` call `manager.acquire()` / `manager.release()` around every
API call. `_open_quota_circuit()` also calls `manager.open_circuit()` to keep both
state stores in sync.

---

## 5. Configuration

New fields in `app/core/config.py`:

```python
# Gemini quota budgets — set these to match your API key's tier.
# Upgrading tiers = single .env change, no code changes.
GEMINI_GENERATE_RPM:    int   = Field(150,       ge=1,   le=1500,
    description="Generate requests/min — 150=Tier1, 500=Tier2, 10=free")
GEMINI_GENERATE_TPM:    int   = Field(1_000_000, ge=1,   le=10_000_000,
    description="Generate tokens/min — 1M=Tier1, 2M=Tier2")
GEMINI_EMBED_RPM:       int   = Field(90,        ge=1,   le=2000,
    description="Embed requests/min — 90=free, higher on paid")
GEMINI_MAX_QUEUE_DEPTH: int   = Field(50,        ge=10,  le=500,
    description="Max permits in the priority queue before backpressure kicks in")
GEMINI_PERMIT_TIMEOUT:  float = Field(30.0,      ge=5.0, le=120.0,
    description="Max seconds a caller waits for a permit before timeout")
```

---

## 6. Metrics

New entries in `app/core/metrics.py`:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `gemini_requests_total` | Counter | `op`, `priority`, `status` | Every completed call (success/quota/rate/error) |
| `gemini_rpm_utilisation` | Gauge | `op` | Fraction of RPM budget consumed (0–1) |
| `gemini_tpm_utilisation` | Gauge | — | Fraction of TPM budget consumed (0–1) |
| `gemini_queue_depth` | Gauge | `priority` | Permits waiting per priority level |
| `gemini_circuit_open` | Gauge | `op` | 1 when circuit is open, 0 when closed |
| `gemini_permit_wait_seconds` | Histogram | `op`, `priority` | Time from `acquire()` to permit granted |

---

## 7. Files Changed

| File | Change |
|------|--------|
| `app/ai/intelligence/request_manager.py` | **New** — full manager implementation |
| `app/ai/intelligence/llm_client.py` | Add `priority` param + call manager in `_acall`/`embed` + delegate circuit to manager |
| `app/core/config.py` | 5 new config fields |
| `app/core/metrics.py` | 6 new Prometheus metrics |
| `app/main.py` | `await GeminiRequestManager.initialize(redis)` in lifespan |
| `app/ai/intelligence/explanation_worker.py` | `priority=Priority.HIGH` / `Priority.LOW` at 2 call sites |
| `app/ai/fusion/signal_assembler.py` | `priority=Priority.MEDIUM` |
| `app/ai/intelligence/nlp_engine.py` | `priority=Priority.MEDIUM` / `Priority.LOW` |
| `app/ai/intelligence/event_classifier.py` | `priority=Priority.MEDIUM` |
| `app/ai/rag/embedder.py` | `priority=Priority.BACKGROUND` |

---

## 8. What This Delivers

| Scenario | Before | After |
|----------|--------|-------|
| Explanation + 5 sentiment calls burst simultaneously | All compete equally, random 429s | Explanation served first; sentiment queues behind |
| App restart after quota exhaustion | First call hits 429, wastes 30s retrying | Redis circuit check at startup → instant fast-fail |
| RAG backfill during market hours | Competes with live signal pipeline | Sits at BACKGROUND; only runs when no higher-priority permits waiting |
| Move to Paid Tier 2 (500 RPM) | Would require code changes | Single `.env` change: `GEMINI_GENERATE_RPM=500` |
| Quota pressure visibility | No metrics | Grafana: RPM utilisation, queue depth by priority, circuit state gauge |
| TPM spike from long explanations | Invisible until 429 | `gemini_tpm_utilisation` gauge + alert rule |
