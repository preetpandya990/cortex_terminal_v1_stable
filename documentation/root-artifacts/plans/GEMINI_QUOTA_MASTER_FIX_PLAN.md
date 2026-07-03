# Gemini Quota — Master Fix Plan
**Date:** 2026-06-30  
**Status:** Awaiting RPD verification before implementation begins  
**Author:** Architecture session — cross-referenced against audit logs, source code, and web research

---

## 1. Situation Assessment

### Hard Facts

| Item | Value |
|---|---|
| API keys | 5 keys across 5 independent GCP projects |
| Empirically observed RPD per key | ~10 (inferred from log: 50 total calls before exhaustion) |
| Documented free-tier RPD (web research, 2026) | **250 RPD per project** for `gemini-2.5-flash` |
| **Budget discrepancy** | **UNRESOLVED — must be verified before building** |
| Days with full quota exhaustion | 3 of last 5 trading days |
| Time of exhaustion | ~09:30 UTC — within 90 minutes of market open |
| User-facing impact | Zero trade explanations and zero watchlist context for the remaining ~7 hours of each trading day |

### Budget Math (Two Scenarios)

| Scenario | RPD per key | Total daily budget | Status |
|---|---|---|---|
| **A** — Dashboard shows 250 RPD | 250 | **1,250 calls/day** | Existing fixes are sufficient |
| **B** — Dashboard confirms ~10 RPD | 10 | **50 calls/day** | Full batch architecture required |

### Current Call Volume (Active Trading Day)

| Consumer | Calls/day | Priority (current) | Priority (correct) |
|---|---|---|---|
| `SentimentOutput` (per-article) | 125 | BACKGROUND ✓ | BACKGROUND |
| `NewsForecastOutput` (per-symbol) | 76 | **MEDIUM ✗** | **LOW** |
| `_ClassificationSchema` (event classifier) | 39 | LOW ✓ | LOW |
| `ExplanationOutput` (trade suggestions) | ~30 | HIGH ✓ | HIGH |
| `ExplanationOutput` (instrument context) | ~4 | **MEDIUM ✗** | **HIGH** |
| **Total generate** | **~245** | — | — |

### What Already Exists (7 Fixes, All UNCOMMITTED + UNDEPLOYED)

All 7 fixes were built in prior sessions. They reduce ~245 → ~119 calls/day when deployed:

| Fix | File(s) | Effect |
|---|---|---|
| 1. Circuit breaker fast-fail | `llm_client.py` | Zero network I/O on quota-exhausted keys |
| 2. Priority queue + token buckets | `request_manager.py` | All callers serialised through a single coordinator |
| 3. Budget guard (RPD tracking) | `request_manager.py` | Soft daily limit throttles MEDIUM/LOW/BACKGROUND before HIGH exhausts |
| 4. DLQ self-healing on quota reset | `explanation_worker.py` | Auto-requeues stranded explanation jobs at midnight PT |
| 5. Caching (sentiment 24h, classifier 30min, fake-news 1h, forecast 5min, health check debounce) | multiple files | Eliminates repeat calls for identical inputs |
| 6. Event classifier heuristic pre-filter | `event_classifier.py` | ~90% of articles bypass Gemini entirely |
| 7. Sentiment batching (15 articles → 1 call) | `nlp_engine.py` | ~93% reduction in sentiment RPD |

After all 7 are deployed: **~119 calls/day**. Still 2.4× over the 50-call budget under Scenario B.

### The Single Remaining Root Cause (Scenario B only)

`NewsForecastOutput` fires **one Gemini call per symbol per signal assembly run** with no batching, no heuristic bypass, and wrong priority. On active trading days this produces 37–76 individual calls. On 2026-06-30, 11 calls fired simultaneously in under 2 seconds at 09:15:35 UTC, consuming 12% of the daily budget in a single uncontrolled burst.

---

## 2. Critical Pre-Build Action Required

**Before writing a single line of new code:**

Go to [aistudio.google.com](https://aistudio.google.com) → each of your 5 keys → Quota / Rate Limits page.  
**What RPD does it show for `gemini-2.5-flash` today?**

The answer determines which implementation path to follow. If it shows 250 RPD, Scenario A applies and the batch architecture is unnecessary. If it shows ~10–20 RPD, Scenario B applies and the full plan below is required.

Note: the December 2025 Google quota cut dropped limits to as low as 20 RPD for some accounts before stabilising. Individual account history may produce different results. The AI Studio dashboard is the authoritative source — third-party documentation is frequently stale.

---

## 3. Implementation Plan

### Phase 0 — Deploy Everything Already Built (Both Scenarios)

**Zero new code. Prerequisite for all subsequent phases.**

Commit and deploy all 7 uncommitted fixes with the correct `.env` values.

**.env values (Scenario B — 10 RPD/key):**
```env
GEMINI_GENERATE_RPD=10
GEMINI_HIGH_PRIORITY_RPD_RESERVE=20
GEMINI_GENERATE_RPM=50
GEMINI_EMBED_RPM=450
```

**.env values (Scenario A — 250 RPD/key):**
```env
GEMINI_GENERATE_RPD=250
GEMINI_HIGH_PRIORITY_RPD_RESERVE=50
GEMINI_GENERATE_RPM=50
GEMINI_EMBED_RPM=450
```

`GEMINI_HIGH_PRIORITY_RPD_RESERVE` should be ~10–20% of total daily budget. At 10 RPD × 5 keys = 50 total → reserve=20 (40%). At 250 RPD × 5 keys = 1,250 total → reserve=50 (4%). These protect the HIGH-priority reservation band (explanations + instrument context) from being consumed by background work before users can access it.

**After Phase 0 (Scenario B):** ~119 calls/day — still over budget, but priority queue is live and background work begins getting throttled before HIGH callers are impacted.

**After Phase 0 (Scenario A):** ~119 calls/day against 1,250 budget — **10% utilisation. Problem is solved.**

---

### Phase 1 — Priority Hierarchy Correction (Both Scenarios)

**Two surgical line changes. Architecturally correct regardless of quota level.**

#### Change 1 — Instrument context → HIGH
**File:** `backend/app/ai/intelligence/explanation_worker.py:1181`

```python
# Before
priority=Priority.MEDIUM,   # instrument context generation

# After
priority=Priority.HIGH,     # instrument context is user-facing — same tier as trade explanations
```

**Rationale:** Instrument context powers the watchlist panel. When a user opens their watchlist and nothing loads, that is a product failure. It belongs at HIGH priority, identical to trade explanations. Currently at MEDIUM, it competes with the news forecaster and can be throttled by the budget guard when it should never be.

#### Change 2 — News forecaster → LOW
**File:** `backend/app/ai/fusion/signal_assembler.py:387`

```python
# Before
priority=Priority.MEDIUM,   # gather_news_forecast

# After
priority=Priority.LOW,      # forecaster is signal-enhancement, not user-facing
```

**Rationale:** The forecaster contributes to the consensus score but has a deterministic NLP fallback. Users never see a "forecaster failed" error — they get a signal from NLP + ML without the Gemini news layer. Signal quality is reduced but the product experience is unaffected. LOW is architecturally correct: signal enhancement sits below user-facing features in the priority stack.

**Effect of both changes with `RESERVE=20`:** When budget drops below 20 remaining calls:
- HIGH (explanations + instrument context): always admitted — never throttled
- LOW (forecaster + event classifier Gemini path): immediately throttled → NLP/deterministic fallback
- BACKGROUND (sentiment batching): immediately throttled → silence

---

### Phase 2 — Forecaster Async Batch Architecture (Scenario B only)

**Required only if RPD is confirmed at ~10/key (50 total/day).**  
**Skip entirely under Scenario A.**

#### Design Rationale

The sentiment batcher (Fix 7, already built) blocks the caller with `await future` — callers suspend until the batch resolves. This is correct for the RSS pipeline, which can absorb 60-second latency.

The news forecaster cannot use that pattern. Signal assembly must complete in seconds. The correct pattern is **fire-and-cache**:

1. `gather_news_forecast` checks Redis cache first. Cache hit → return Gemini result immediately.
2. Cache miss → enqueue the (symbol + context) to a Redis batch queue, return NLP fallback immediately. Zero Gemini latency on the hot path.
3. A background `forecast_batch_worker` task drains the queue in batches of N symbols, fires one Gemini call per batch, and writes each symbol's result to the existing cache keys.
4. The **next** signal assembly for the same symbol — within the same 15-minute scheduler cycle on unchanged news — hits cache and receives the Gemini result.

This decouples signal assembly latency from Gemini availability. A Gemini outage or budget exhaustion degrades gracefully to NLP fallback and self-heals automatically when quota is available. The first signal for a new article set uses NLP fallback; every subsequent signal within the cache TTL uses the Gemini result.

#### Structured Output Reliability Constraints (from web research)

Web research identified two confirmed open bugs in Gemini 2.5 structured output for multi-item responses:
- Field values can repeat indefinitely until `max_output_tokens` is hit, returning `None` for both `response.text` and `response.parsed`.
- Fields can be silently omitted or mixed between items at larger N.

**Mitigations required:**
1. **Batch size ≤ 5 for the forecaster.** The forecaster schema is complex (15 indicators + 6 events + 3-field output per symbol). Community-validated safe ceiling for complex multi-item structured output is 5–8 items. Default `NEWS_FORECAST_BATCH_SIZE=5`.
2. **Per-item validation.** Every `SymbolForecast` in the batch response is validated: `symbol` must match an input symbol, `confidence` must be in `[0.0, 1.0]`, `direction` must be `BUY/SELL/HOLD`. Any item failing validation is silently dropped; that symbol retains its NLP fallback. No exceptions, no retries.
3. **Echo-back verification.** The batch output schema requires `symbol: str` on each result. A mismatch between echoed symbol and input symbol indicates item-mixing — that result is discarded.
4. **`max_output_tokens` set generously.** ~150 tokens per symbol × 5 symbols = 750 tokens. Set to 1,000 with 33% headroom to avoid silent `None` returns.
5. **Never use the Gemini Async Batch API.** Web research confirmed a ~70% hallucination rate (repetitive loops exhausting token limits) in the async `.jsonl` Batch API. All batch calls go through synchronous `generate_content` with `response_schema`.

#### Files to Change or Create

**New file: `backend/app/ai/fusion/forecast_batch_worker.py`**

Supervised background task. Responsibilities:
- Drain Redis list `cortex:forecast:batch:queue` using non-blocking LMPOP
- Accumulate up to `NEWS_FORECAST_BATCH_SIZE` unique symbols (dedup by `symbol:events_hash`)
- Flush when: batch is full, OR `NEWS_FORECAST_BATCH_WINDOW_SECS` (60s) elapsed since first enqueue
- Acquire `Priority.LOW` permit from `GeminiRequestManager` before calling Gemini
- Build multi-symbol prompt with `NewsForecastBatchOutput` schema
- Validate each item in the response individually
- Write valid results to `cortex:news_forecast:{symbol}:{digest}` cache keys (same format as synchronous path)
- Write one `ai_llm_audit_log` row per batch call (governance requirement)
- On `GeminiQuotaExhausted` or `GeminiBudgetThrottled`: drop batch, log, dedup keys expire naturally

**Modified: `backend/app/ai/fusion/news_forecaster.py`**

Add two new schemas alongside the existing `NewsForecastOutput`:

```python
class SymbolForecast(BaseModel):
    """Single symbol result within a batch forecast response."""
    symbol: str = Field(
        description="Exact symbol string from the request — echo back verbatim"
    )
    rationale: str = Field(
        description="≤45 words grounded in the specific indicators and news provided"
    )
    direction: Literal["BUY", "SELL", "HOLD"]
    confidence: float = Field(ge=0.0, le=1.0)


class NewsForecastBatchOutput(BaseModel):
    """Batch forecast response — one entry per requested symbol."""
    forecasts: list[SymbolForecast] = Field(
        description=(
            "One forecast per symbol, in the same order as the input. "
            "Echo the symbol name exactly. If evidence is thin for a symbol, "
            "use HOLD with confidence ≤ 0.4 rather than omitting the entry."
        )
    )
```

**Modified: `backend/app/ai/fusion/signal_assembler.py`**

`gather_news_forecast` logic change (after cache miss):

```python
# Cache miss — enqueue for background batch processing, return NLP fallback immediately.
# The next signal assembly for this symbol within the cache TTL (5 min) will hit cache.
await self._enqueue_for_batch_forecast(symbol, events, indicators, cache_key)
llm_news_forecasts_total.labels(outcome="batch_enqueued").inc()
return _fallback("batch_pending")
```

`_enqueue_for_batch_forecast` implementation:
1. Compute `dedup_key = f"cortex:forecast:batch:dedup:{symbol}:{digest}"`
2. `SETNX dedup_key 1 EX 600` — if key already exists, symbol is already queued; return immediately
3. `LPUSH cortex:forecast:batch:queue <json_payload>`
4. If queue depth > `NEWS_FORECAST_BATCH_SIZE × 5` (25): LPOP one item to bound the queue size

**Modified: `backend/app/core/config.py`**

```python
NEWS_FORECAST_BATCH_SIZE: int = Field(
    5,
    ge=1,
    le=10,
    description=(
        "Symbols per batched Gemini forecast call. "
        "Research on multi-item structured output reliability places the safe ceiling "
        "at 5–8 items for complex schemas (15 indicators + 6 events per symbol). "
        "Each item validated individually; failures fall back to NLP."
    ),
)
NEWS_FORECAST_BATCH_WINDOW_SECS: float = Field(
    60.0,
    ge=5.0,
    le=120.0,
    description=(
        "Maximum seconds the batch accumulator waits before flushing an incomplete batch. "
        "60s matches the sentiment batch window. The 15-minute signal scheduler cycle "
        "ensures subsequent signals for the same symbol hit the populated cache."
    ),
)
```

**Modified: `backend/app/workers/registry.py`**

```python
TASK_NAMES: tuple[str, ...] = (
    ...existing names...
    "forecast_batch",   # async-batch news forecaster (Scenario B only)
)
```

```python
from app.ai.fusion.forecast_batch_worker import forecast_batch_loop

"forecast_batch": lambda: forecast_batch_loop(
    redis=redis_client._redis,
    session_factory=session_factory,
    shutdown=shutdown,
),
```

**Modified: `backend/app/core/metrics.py`**

```python
news_forecast_batch_size_histogram = Histogram(
    'news_forecast_batch_size',
    'Symbols per batch Gemini forecast call',
    buckets=(1, 2, 3, 4, 5, 6, 7, 8, 10),
)
news_forecast_batch_calls_total = Counter(
    'news_forecast_batch_calls_total',
    'Batch forecast Gemini calls by outcome',
    ['outcome'],   # success | quota_exhausted | budget_throttled | validation_partial | error
)
news_forecast_queue_depth = Gauge(
    'news_forecast_queue_depth',
    'Symbols currently pending in the batch forecast queue',
)
```

#### Redis Key Inventory (Scenario B)

| Key | Type | TTL | Purpose |
|---|---|---|---|
| `cortex:forecast:batch:queue` | List | none | Pending symbol forecasts (LPUSH/LMPOP) |
| `cortex:forecast:batch:dedup:{symbol}:{digest}` | String | 10 min | Prevents duplicate enqueue within one window |
| `cortex:news_forecast:{symbol}:{digest}` | String | 5 min | Result cache (same keys used by synchronous path) |

#### Projected Call Reduction (Scenario B, post all phases)

| Consumer | Before | After all phases |
|---|---|---|
| Sentiment | ~42/day | ~3 batch calls |
| Event classifier | ~21/day | ~2 calls (90% heuristic bypass) |
| News forecaster | ~37/day | ~7–8 batch calls (5 symbols/call) |
| Instrument context | ~4/day | ~4/day |
| Trade explanations | ~10/day | ~10/day |
| **Total** | **~114/day** | **~26–27/day** |

**52–54% of the 50-call daily budget.** Approximately 23 calls of headroom.

---

### Phase 3 — Observability (Both Scenarios)

**The current outage went undetected for 4+ days. This cannot happen again at any quota level.**

#### Four Prometheus Alert Rules (`prometheus.yml`)

```yaml
groups:
  - name: gemini_quota
    interval: 60s
    rules:

      - alert: GeminiRPDBudgetLow
        expr: gemini_rpd_budget_remaining < gemini_high_priority_rpd_reserve * 2
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "Gemini daily budget below 2× HIGH-priority reserve"
          description: >
            {{ $value }} generate calls remain today. Background callers are
            now throttled. HIGH-priority callers (explanations, context) still
            have headroom but the window is closing. Check
            gemini_requests_total by priority label to identify the dominant consumer.

      - alert: GeminiRPDBudgetCritical
        expr: gemini_rpd_budget_remaining < gemini_high_priority_rpd_reserve
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Gemini daily generate budget critically low — explanations at risk"
          description: >
            Only {{ $value }} generate calls remain. The budget guard is active.
            MEDIUM/LOW/BACKGROUND callers are throttled. HIGH callers admitted
            but headroom is minimal. Quota resets at midnight Pacific Time + 15 min.

      - alert: GeminiAllCircuitsOpen
        expr: gemini_all_keys_exhausted{op="generate"} == 1
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "ALL Gemini generate circuits OPEN — explanations and context are dead"
          description: >
            All Gemini API keys have exhausted their daily generate quota.
            Trade explanations and watchlist context cannot be generated.
            DLQ self-heal fires automatically at midnight Pacific Time + 15 min.
            To manually recover after verifying quota reset in AI Studio:
            DEL cortex:gemini:circuit:generate:* in Redis.

      - alert: GeminiForecastBatchLagging
        expr: news_forecast_queue_depth > 20
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Forecast batch queue backed up — batch worker may be stalled"
          description: >
            {{ $value }} symbols are queued for batch forecasting and not draining.
            Check news_forecast_batch_calls_total{outcome="budget_throttled"} —
            if high, the budget guard is blocking the batch worker (expected and
            correct near quota exhaustion). If outcome="error" is rising,
            the batch worker may have crashed.
```

#### Grafana Panel

One new panel on the main ops dashboard: **Gemini Daily Quota Burn-Down**

- Primary series: `gemini_rpd_budget_remaining` (step chart, updates every scrape)
- Red horizontal line: `GEMINI_HIGH_PRIORITY_RPD_RESERVE` (critical threshold)
- Yellow horizontal line: `GEMINI_HIGH_PRIORITY_RPD_RESERVE × 2` (warning threshold)
- Secondary panel: `gemini_requests_total` rate by `priority` label — shows which tier is consuming quota

---

## 4. What Is NOT Being Built

| Item | Reason |
|---|---|
| More API keys from more accounts | Each account at free tier adds marginal RPD; operational overhead of rotating 10+ keys is not worth it at this scale |
| Removing the news forecaster | It provides signal quality improvement when budget allows. LOW priority + NLP fallback is architecturally correct — it degrades gracefully and self-heals |
| Gemini Async Batch API | Confirmed ~70% hallucination rate (GitHub issue #1984). Do not use |
| Higher forecast cache TTL | 5-minute TTL is calibrated to the 15-minute signal scheduler. Extending it serves stale forecasts on genuinely fresh news |
| Per-article forecasting | One forecast per symbol per signal assembly is architecturally correct. Article-level forecasting would multiply RPD proportionally to article volume |
| Retry logic for batch validation failures | Retrying on validation failure risks doubling RPD under degraded quota. Silent fallback to NLP is the correct behaviour |

---

## 5. File Change Summary

| File | Change | Phase |
|---|---|---|
| `backend/.env` | GEMINI_GENERATE_RPD, RESERVE, RPM | 0 |
| `backend/app/ai/intelligence/explanation_worker.py:1181` | MEDIUM → HIGH | 1 |
| `backend/app/ai/fusion/signal_assembler.py:387` | MEDIUM → LOW + enqueue path | 1+2 |
| `backend/app/ai/fusion/news_forecaster.py` | Add SymbolForecast + NewsForecastBatchOutput | 2 |
| `backend/app/ai/fusion/forecast_batch_worker.py` | **New file** — supervised batch worker task | 2 |
| `backend/app/core/config.py` | NEWS_FORECAST_BATCH_SIZE, NEWS_FORECAST_BATCH_WINDOW_SECS | 2 |
| `backend/app/workers/registry.py` | forecast_batch in TASK_NAMES + registry | 2 |
| `backend/app/core/metrics.py` | 3 new batch forecaster metrics | 2 |
| `prometheus.yml` | 4 Gemini quota alert rules | 3 |

**No database migrations. No new Python dependencies. No changes to the existing synchronous `generate_news_forecast` path.**

---

## 6. Decision Gate

```
┌─────────────────────────────────────────────────────────┐
│  Check AI Studio dashboard for each of the 5 keys.      │
│  What is the RPD for gemini-2.5-flash today?             │
└─────────────────────────────────────────────────────────┘
              │                         │
              ▼                         ▼
        RPD ≈ 250                  RPD ≈ 10–20
    (Scenario A)                  (Scenario B)
              │                         │
              ▼                         ▼
   Phase 0 + Phase 1           Phase 0 + Phase 1
   + Phase 3                   + Phase 2 + Phase 3
              │                         │
              ▼                         ▼
   ~2 hours effort             ~3 days effort
   No new files                1 new file + 5 modified
```
