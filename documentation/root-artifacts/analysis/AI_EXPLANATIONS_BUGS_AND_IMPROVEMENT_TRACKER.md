# AI Explanations — Bugs & Improvement Tracker

> Review opened: **2026-07-04** (root cause found) · latest findings added **2026-07-05** ·
> full forensic codebase re-scan (7 parallel deep-dives) added **2026-07-05**.
> Status: all items below are **confirmed via code review, not yet fixed/implemented**.
> Purpose: the Gemini AI Explanation feature (narratives explaining ML predictions/signals) has
> drifted from the quality bar the ML/signal system is capable of. This is the working record of
> verified root causes and agreed designs, so fixes can be planned without re-deriving the
> investigation each time. Living document — check items off as they land.

---

## ☐ 0. Root Cause — ML Values Missing From Prompt

**Symptom:** explanations sometimes omit ML-generated data (forecast direction, rationale,
sentiment) even though it exists, because it hadn't finished computing when the explanation job
was built.

**Call chain:**

- `backend/app/ai/correlation/engine.py:973-1097` (`_compute_consensus`) snapshots the
  `TradeSuggestion` (`ml_signal`, `ai_signal`, `scanner_signal`) and publishes the explanation job
  to `cortex.explanation.jobs` in the same function — before async news/ML forecasting settles.
- `backend/app/ai/fusion/signal_assembler.py:309-379` (`gather_news_forecast`) — on a cache miss
  (the common case for a first-seen symbol/event/indicator combo) enqueues a job to
  `forecast_batch_worker` and immediately returns a `_fallback("batch_pending")` shape built from
  `event_signals` (`signal_assembler.py:271-303`), which only has
  `score/confidence/event_count/events/available` — never `direction`, `rationale`, or
  `sentiment_label`.
- `backend/app/ai/intelligence/explanation_worker.py:558-576` (`_build_explanation_prompt`) reads
  `ai.get("direction")`, `ai.get("rationale")`, `ai.get("sentiment_label")` — all absent on the
  batch-pending shape — so the "News Forecaster Lean/View" section is silently dropped. Not
  because there's no news, but because the real forecast hadn't landed yet.
- `backend/app/ai/fusion/forecast_batch_worker.py:396-457` — batch window is 60s
  (`NEWS_FORECAST_BATCH_WINDOW_SECS`) + up to ~22s Gemini latency: a realistic 1-2 minute miss
  window, not a rare edge case.
- `forecast_batch_worker.py:471-476` — once computed, the real forecast is written to the same
  Redis cache key but **never propagated back** to the already-committed
  `TradeSuggestion.ai_signal` (confirmed: no UPDATE ever touches that column). The next
  correlation cycle for the same symbol benefits; this specific suggestion's explanation never
  does.

**Key files:** `correlation/engine.py:973-1097` · `fusion/signal_assembler.py:309-379` ·
`fusion/forecast_batch_worker.py:396-476` · `intelligence/explanation_worker.py:558-576`

**Additional gaps found — codebase deep-dive, 2026-07-05 (verified, not yet fixed):**

- **`ai_signal["sentiment_label"]` is dead code — never populated by any producer, at any point
  in time.** `explanation_worker.py:558,573-574` reads `ai.get("sentiment_label") or
  ai.get("sentiment")` to render the "Sentiment:" line. But `forecast_batch_worker._build_result`
  (`fusion/forecast_batch_worker.py:527-545`, the function that writes the *landed* Gemini
  forecast to cache) only ever sets `score, confidence, available, events, event_count,
  direction, rationale, forecast_source, model` — no `sentiment_label`/`sentiment` key.
  `signal_assembler._build_event_dict` (`fusion/signal_assembler.py:184-207`) doesn't attach it
  either. `sentiment_label` is a real field, but it lives in an entirely separate subsystem
  (`AINLPResult` / `sentiment_analysis_service.py` / `api/v1/intelligence.py`) that is never wired
  into `ai_signal`. Unlike the timing-based root cause above, this gap can never self-resolve —
  even a perfectly-landed forecast (direction + rationale both present) will never render a
  sentiment line, because no code path writes the key the prompt reads.
- **Per-agent circuit breakers in `EventCorrelationEngine` are constructed but never invoked.**
  `correlation/engine.py:162-166` builds `self.circuit_breakers = {"scanner": ..., "ai": ...,
  "ml": ...}`, and the class docstring (lines 130-134) claims "Circuit breakers per agent for
  fault tolerance." `CircuitBreaker.record_success()/record_failure()/can_attempt()`
  (lines 89-120) are never called anywhere else in the file (confirmed via grep — only their own
  definitions match), and `self.circuit_breakers` is never read again after construction. If the
  scanner, AI, or ML agent starts failing repeatedly (e.g. a DB outage breaks the ML feature
  loader), there is no per-agent breaker to short-circuit further attempts — every scanner
  anomaly/news event still re-runs the full gather→consensus pipeline against a known-broken
  agent at full cost, with no backoff. The documented resilience mechanism does not exist in
  practice.
- **Forecast-batch dedup TTL (600s) mismatches the demand-driven drain cadence, causing
  duplicate re-enqueues and wasted Gemini spend.** `signal_assembler._enqueue_for_batch_forecast`
  (`fusion/signal_assembler.py:437-439`) hardcodes a `SET NX EX 600` dedup key. This was tuned for
  the old near-real-time auto-drain model. Under the now-live demand-driven config
  (`FORECAST_AUTO_DISPATCH=False`, per project memory), the queue is only drained by an explicit
  admin dispatch or by `AIProcessingSafetyNet` (`workers/ai_processing_safety_net.py:103-150,
  220-240`), whose loop wakes once daily (`AI_SAFETY_NET_RUN_TIME_IST`, default `09:00` IST) and
  only checks pending-count against a threshold at that single wake — no mid-day polling.
  `correlation_loop` scans all active instruments every 30s during market hours
  (`worker.py:836-837`); for a symbol whose news/indicator snapshot is unchanged across scans, the
  cache-miss path recomputes the identical `cache_key` every cycle, and once the 10-minute dedup
  key expires the same payload is republished to `cortex.forecast.batch` again — roughly every
  ~10 minutes per actively-scanned, cache-missed symbol, for the rest of the trading day (nothing
  drains it until the next day's 09:00 sweep, which runs before market open). `_absorb`'s
  `seen_symbols` dedup (`forecast_batch_worker.py:245-250`) only dedupes within one drain window
  (`NEWS_FORECAST_BATCH_SIZE=5`), so a large backlog can re-forecast the same unchanged symbol
  across multiple separate Gemini calls when eventually flushed — a distinct cost-control gap from
  the watched/unwatched-symbol issue in Proposal A.

---

## Findings By Area

### ☐ 1. Content Quality

- No post-hoc check that numbers cited in `full_explanation` match numbers actually in the
  prompt — a hallucinated confidence % or RSI value passes all guardrails silently.
  (`explanation_worker.py:388-424`, regex-only: price predictions + `[Source ...]` presence.)
- The "GROUND every ML/technical claim" instruction (`explanation_worker.py:279-291`) is
  text-only model guidance — nothing enforces it programmatically.

### ☐ 2. Prompt / Context Design

- `_render_scanner` (`explanation_worker.py:485-498`) silently omits the entire "Technical
  Scanner Readings" section when `scanner_signal` is empty/unavailable — no explicit "no
  technical data available" fallback line (unlike the news-context branch,
  `explanation_worker.py:598-605`). Gemini can't tell "missing" from "genuinely neutral."
- Pathway-2 (news-triggered) suggestions get a synthetic `scanner_signal` fallback from
  `_resolve_scanner_signal_for_symbol` (engine.py) with `available=False` — degrades to a
  near-empty technical section for a whole class of suggestions, not just rare edge cases.
- `_EXPLANATION_SYSTEM_PROMPT` and `_CONTEXT_SYSTEM_PROMPT`
  (`explanation_worker.py:242-341`) are ~95% duplicated text — a rule fixed in one is easily
  missed in the other.

### ☐ 3. Pipeline / Trigger Logic

- `backend/app/ai/safety/safety_trigger_engine.py` is an unrelated global kill-switch
  (signal-rate/volatility) with hardcoded placeholder metrics (e.g. `"signal_rate": 0`). It does
  not gate explanation dispatch on ML freshness — there is no freshness check anywhere between
  "ML finished computing" and "explanation job fires."
- `backend/app/api/worker_ai_processing.py:103-125` (`dispatch_forecast`) drains the forecast
  batch queue on demand but never re-reads/updates `TradeSuggestion.ai_signal`.
- Context-job `prediction_data` (`explanation_worker.py:1638-1645`) is built in
  `backend/app/api/v1/ai_stream.py:461-463` from an in-memory `state.prediction`, refreshed only
  every 60s (`_PREDICTION_REFRESH_SECS`, `ai_stream.py:596-629`) — up to 60s stale at dispatch
  time, independent of the Kafka job's own age.

### ☐ 4. Delivery / UX

- `ai_stream.py:187-214, 650-663` — once `state.explanation` is populated it is never
  invalidated even if `state.prediction` later flips direction on a subsequent 60s refresh; two
  independent producers with no cross-check. A delivered BUY explanation can sit next to a
  freshly-refreshed SELL prediction card in the UI.
- `ai_stream.py:319-408` (Stage 1) — explanations are served from whichever `TradeSuggestion` row
  is most recent within a 7-day lookback, with no re-check against current signal state; a stale
  explanation persists until an entirely new suggestion is created.

**Additional gaps found — codebase deep-dive, 2026-07-05 (verified, not yet fixed):**

- **Push-path explanation delivery has no anti-downgrade guard — a delayed retry can silently
  overwrite a fresher, correct explanation with a stale one.** `ai_stream.py:708-786`
  (`_handle_push`, `cortex:llm:explanation:ready:*` branch) filters only on `instrument_key`
  (lines 720-721) and then unconditionally does `state.explanation = payload` (line 780) — no
  comparison of `suggestion_id`/`created_at` against what's already in `state.explanation`. The
  poll path has exactly this guard (`_should_apply_polled_explanation`, lines 187-214); the push
  path doesn't. `explanation_worker.py` retries on transient failures (`MAX_ATTEMPTS=3`,
  `_RETRY_DELAY_SECS=60`, up to ~3 min latency): if Suggestion A's job is mid-retry when
  Suggestion B supersedes A for the same instrument and B's explanation lands first, A's delayed
  retry can still complete afterward and its push overwrites B's correct, fresher explanation with
  A's stale one — with no re-check of which suggestion is actually current.
- **Silent, invisible staleness on prediction/pattern/sentiment refresh failures, compounded by a
  shared `sseConnected` flag that disables the frontend fallback for every card at once.**
  `ai_stream.py:602-629` (`_refresh_prediction`) only catches `except ValueError` (line 627); any
  other exception propagates out and skips `_emit_update()` for that cycle entirely.
  `_refresh_pattern`/`_refresh_sentiment` (`ai_stream.py:631-648`) have no try/except at all —
  same effect. Both are only caught by the generic wrapper (`ai_stream.py:696-704`), which logs a
  warning and calls `_emit_error(...)` but never touches the stale state, which is retained and
  re-broadcast on every subsequent emit triggered by a *different*, healthy producer. Frontend:
  `AnalysisCardsSection.tsx:105-107`'s `error` SSE listener is a literal no-op, so the error never
  reaches the UI; `AnalysisCardsSection.tsx:234,247,264,293`'s React Query `refetchInterval`
  fallback for each card is gated on one shared `sseConnected` boolean, set `true` by *any*
  `analysis_update` event regardless of which component's data it carries
  (`AnalysisCardsSection.tsx:98`). A predictor that throws a non-`ValueError` every cycle can
  freeze the prediction card on stale data indefinitely — no error banner, no fallback poll — as
  long as pattern/sentiment/explanation traffic keeps `sseConnected` true.
- **Full AI-panel state wipe + forced SSE reconnect on every silent access-token refresh (~every
  29 min), unrelated to any instrument change.** `AuthContext.tsx:79-83`'s silent refresh loop
  re-arms via `setTimeout` at `(expires_in - 60)s` — with the default
  `ACCESS_TOKEN_EXPIRE_MINUTES=30` (`core/config.py:64`), this fires roughly every 29 minutes for
  any open session. `AnalysisCardsSection.tsx:117`'s `connect` callback has `accessToken` in its
  dependency array, so it gets a new identity on every refresh; the mount effect
  (`AnalysisCardsSection.tsx:119-133`) depends on `connect` and re-runs, executing lines 121-125
  (`setPredictionData(null); setPatternData(null); setSentimentData(null);
  setExplanationData(null);`) and reconnecting — tearing down the live `EventSource` and
  re-running the full Stage 1-3 explanation lookup plus fresh prediction/pattern/sentiment
  queries — even though nothing the user is viewing changed. This also contradicts the module
  docstring's assumption (`ai_stream.py:512`, "Browser will auto-reconnect after 5 seconds on
  disconnect") — the frontend manages reconnects manually and does not rely on native
  `EventSource` reconnection.
- **Unsanitized externally-sourced `source_url` rendered as a clickable link (XSS surface).**
  `ingestion/rss_fetcher.py:127` takes `item["link"]` straight from parsed third-party RSS/XML
  content with no scheme validation (no allowlist rejecting `javascript:`/`data:` URIs) before
  persisting it. It flows unmodified through `rag/retriever.py:92/105/200/274` into the SSE
  `sources` array, and `AIExplanationPanel.tsx:286-297` (`SourcesList`) renders it directly as
  `<a href={src.source_url} target="_blank" rel="noopener noreferrer">` with no scheme check on
  either side of the stack. Any of the 7 configured feeds (`rss_fetcher.py:28-40`) serving a
  malicious `<link>` value would have it ingested verbatim and eventually surfaced as a clickable
  citation.
- **Minor:** `useTypewriter.ts:11`'s module-scoped `typedCache` (a `Set<string>`) never evicts —
  every unique `full_explanation` string shown in a session accumulates for the tab's lifetime
  (slow, unbounded growth). `AnalysisCardsSection.tsx:52-53,109-116`'s SSE reconnect gives up
  permanently after `SSE_MAX_RETRIES=3` (~15s), so once real-time push delivery drops for the
  connection it never resumes for the rest of the session — only a full remount (instrument
  change) resets it — even though the REST polling fallback keeps basic data flowing.

### ☐ 5. Business Logic

- Direct consequence of the root-cause bug: a suggestion's `llm_explanation` can claim "no
  significant news signal" in the News Context section while a real, materially
  bullish/bearish Gemini forecast exists in cache seconds later — a semantic mismatch between
  what the explanation claims and what the system actually knows. Currently undiscoverable by
  the user; nothing flags the mismatch.

**Key files referenced across this section:**
`correlation/engine.py:973-1097` · `fusion/signal_assembler.py:309-379` ·
`fusion/forecast_batch_worker.py:396-476` ·
`intelligence/explanation_worker.py:242-341, 388-424, 485-607, 558-576, 1638-1645` ·
`api/v1/ai_stream.py:187-214, 319-408, 461-463, 596-663` ·
`ai/safety/safety_trigger_engine.py:31-172` · `api/worker_ai_processing.py:103-125`

---

## Improvement Proposals

All proposals below are **agreed in principle, not yet finalized into an implementation plan** —
revisit together in the final drafting pass, since several share the same race-condition surface
as the root-cause bug.

### ☐ A. Demand-Gated Analysis + Full-Article Prompting

**Why:** event→instrument tagging is already deterministic and free (regex + spaCy NER against
`instrument_master`), but the downstream Gemini analysis calls are not gated by real demand:

- `intelligence/event_classifier.py:839-935` — Gemini event-type classification, fires for the
  ~5-10% of articles the keyword heuristic can't bucket.
- `fusion/signal_assembler.py:309-379` (`gather_news_forecast`) — on cache miss, only checks a
  circuit breaker (line 361) and a Redis dedup key (`_enqueue_for_batch_forecast`, lines 419-468)
  before publishing to Kafka (line 466). **No watchlist or active-trade-suggestion check
  anywhere in this path.**

  **Additional gap found — codebase deep-dive, 2026-07-05 (verified, not yet fixed):**
  `flush_pending_forecasts` silently under-drains and reports false success when it hits a run
  of stale messages, even though a valid backlog remains. `fusion/forecast_batch_worker.py:339-361`
  (the drain loop) and `ForecastQueueConsumer.drain()` (lines 127-149): `drain()` calls
  `consumer.getmany(max_records=batch_size)` and returns only payloads passing `_is_stale()`
  (lines 141-148, messages older than `_STALE_AFTER_SECS=3600` are dropped) — but the raw
  `getmany()` call already advances the consumer's fetch position past those offsets regardless.
  Back in `flush_pending_forecasts`, `batch` is only ever empty when `items` is empty (since
  staleness filtering happens inside `drain()`), so "drained only stale/malformed payloads" and
  "broker returned nothing" are indistinguishable at the call site — both hit
  `if not items and (await consumer.position(tp)) < end_snapshot: break`. Under the demand-driven
  config (`FORECAST_AUTO_DISPATCH=False`), if a contiguous run of &gt;1-hour-old messages sits at
  the head of `cortex.forecast.batch` (e.g. after an outage or a long low-traffic window), one
  admin dispatch call (`POST /ai-processing/forecast/dispatch`, or the daily safety-net check)
  does one `getmany()` of up to `NEWS_FORECAST_BATCH_SIZE=5` records, finds them all stale,
  commits past just those 5, and breaks the outer loop — returning
  `{"dispatched": 0, "calls_made": 0}` that looks like a clean no-op, even though fresher, valid
  forecast requests sit later in the topic before `end_snapshot`. The docstring promises "drain
  the forecast topic to a snapshot of its end offset right now" (lines 313-314); the call silently
  under-drains instead, with zero error signal, and the admin has to click dispatch repeatedly
  (5 stale records advanced per click) to work through the stale run. This interaction between
  per-call staleness filtering and the "empty result ⇒ stop" heuristic is introduced by the
  Redis→Redpanda migration — the pre-migration Redis path had no staleness filter in the pop
  path at all.

Confirmed call chain driving the cost: `worker.py`'s `correlation_loop` (~line 600) runs
`scanner_svc.scan_all()` (line 695) → `MarketScannerService._fetch_db_baselines`
(`services/market_scanner.py:338`) selects **every actively-traded instrument** with recent OHLCV
data (filtered only by `im.is_active IS DISTINCT FROM FALSE`, line 369) — not a watchlist subset.
Anomalies above threshold (`worker.py:710`) call `engine.on_scanner_anomaly`
(`correlation/engine.py:468`) → `gather_news_forecast` (line 492). Separately,
`engine.on_news_event` (line 368) iterates `event.affected_symbols` unconditionally (line 400)
and also calls `gather_news_forecast` (line 428). Result: forecast jobs fire for any symbol the
technical-anomaly or news-event filters surface, watched or not — an uncapped RPD cost driver.

This also directly fixes the root-cause bug: moving forecast/analysis to run *at*
explanation-demand time, in the same call, eliminates the race entirely for that pathway.

**Design:**

1. Suggestion creation/ranking (pathway1/pathway2 in `correlation/engine.py`) still needs some
   signal to decide whether a symbol becomes a trade suggestion at all — can't be gated purely on
   "already an active suggestion" (circular). **Stays essentially as-is.**
2. Explanation generation is fully demand-gateable: nobody needs an explanation until a human
   opens a watchlist card or trade-suggestion detail view.

   **A. Local demand-tag service** — lightweight, no-LLM process that continuously
   cross-references `instrument_master` against `watchlist` + active `trade_suggestions` (cheap
   DB query, short interval or DB trigger/pub-sub), maintaining a live "in-demand symbol set."
   Used to suppress background forecast-batch enqueues for symbols nobody's watching, and to
   prioritize the in-demand set when the batch worker drains its queue.

   **B. Explanation-time full-article prompting** — when a user demands an explanation,
   `explanation_worker` fetches the raw article text for the relevant event(s) and feeds the
   **full article** into the Gemini prompt in that one call — instead of assembling the prompt
   from pre-computed, possibly-stale-or-missing fields written by an earlier async job.
   Instrument-relevance, forecast, and narrative generation happen together, synchronously,
   grounded in the source text.

**Hits three targets at once:** RPD budget (no wasted background analysis on unwatched symbols),
meaningful explanations (no more async-forecast race), insightful responses (Gemini reasons over
full source article, not a pre-digested/sometimes-empty summary field).

**Status:** scope split confirmed (2026-07-04). Not yet finalized into an implementation plan.

---

### ☐ B. Delivery/UX — Direction-Mismatch Flag

**Why:** `api/v1/ai_stream.py:650-663` (`_refresh_explanation`) and `ai_stream.py:596-629`
(`_refresh_prediction`) are two independent producers writing to the same per-connection `state`,
each on its own refresh interval, with `_emit_update()` firing after either changes. Nothing
compares `state.prediction.get("direction")` against `state.explanation.get("signal_direction")`
at emit time.

Frontend-side, `AIExplanationPanel.tsx:387-388` (`showStalenessBanner`) only checks
`context_type === 'suggestion_explanation' && signal_generated_at` — a pure **age** banner
("Based on BUY signal · 6h ago"), with no comparison against the live prediction card's current
direction, even though both values arrive in the same SSE payload.

Net effect: a confident BUY narrative can sit beside a freshly-refreshed SELL prediction card,
with the UI only signaling the explanation is *old*, never that it's *contradicted*.

**Decided fix — Option 1: backend-computed mismatch flag.** Compare
`state.prediction.get("direction")` vs `state.explanation.get("signal_direction")` at
`_emit_update()` time (or inside `_refresh_explanation`, `ai_stream.py:650-663`), add a
`direction_mismatch: bool` field to the SSE payload. Frontend renders a stronger "⚠ Signal has
changed since this explanation" banner when true, alongside (not replacing) the existing
age-based banner.

**Rejected alternative:** client-side comparison in `AIExplanationPanel`'s parent — avoids a
backend change but duplicates "what counts as a mismatch" logic across every consumer (panel,
admin queue card, etc.) and risks drifting from the backend's definition. One authoritative
comparison point in the backend was preferred.

**Status:** design decided (2026-07-04). Not yet implemented. Should land alongside Proposal A,
which also shrinks the mismatch window by moving explanation generation closer to demand time.

---

### ☐ C. OHLCV-Grounded Prompts + Structured Retraining Feedback (B2)

**Why:** the ML model is early-stage and errs often. Today's explanation prompt has no raw OHLCV
price-action data — only pre-aggregated scalars from `scanner_signal` (`last_price,
previous_close, volume, volume_ratio, price_change_pct, rsi`;
`intelligence/explanation_worker.py:472-482, 552-556`). `explanation_worker.py` has no existing
wiring to bar-series OHLCV at all (no `FeatureLoader` import) — fetching it at prompt-build time
is new work, not a rewire.

Separately, retraining is purely quantitative: `compute_sample_weight`
(`ml/training/feedback_loader.py:148-159`) and `_FEEDBACK_QUERY` (189-210) reweight training
samples using only realized outcomes (`ml_direction_correct, hit_tp1/2/3, hit_sl,
confidence_score`). No structured "here's *why* the model was wrong" signal exists anywhere;
`MLFeedbackError` (`models/ml_feedback.py`) logs pipeline/computation crashes, not prediction
misses. Today, even if Gemini reasoned about price action, that reasoning has no path into the
retraining loop — this proposal builds that path.

**Design:**

- **A. OHLCV in the outgoing prompt** — add a compact, token-conscious price-action summary
  (recent trend, swing high/low, volatility over a short bar window — not a raw per-bar dump) to
  `_build_explanation_prompt`/`_build_context_prompt`, so Gemini reasons over actual price
  behavior instead of only derived scalars, catching cases where the ML directional call
  contradicts what price is actually doing.
- **B2. Structured, automatable retraining signal** — constrain Gemini's output schema to include
  enumerated, machine-consumable fields (e.g. `likely_missed_pattern: <enum>`,
  `confidence_should_have_been_lower: bool`) reflecting its OHLCV-grounded assessment of the ML
  call. Wire these into `compute_sample_weight` (`ml/training/feedback_loader.py:148-159`) as an
  additional multiplier alongside the existing outcome factor (B1) and confidence
  hard-negative factor (current lines 79-90, 127-145) — requires designing the enum/output
  schema and extending the sample-weight formula.

**Status:** direction confirmed (2026-07-04) — B2 chosen over B1 (human-review-only
annotations). Not yet designed in detail or implemented.

**Additional gaps found — codebase deep-dive, 2026-07-05 (verified, not yet fixed):**

- **`build_panel_weights` match telemetry undercounts real matches — a monitoring blind spot,
  not a weight-computation bug.** `ml/training/feedback_loader.py:562-574` computes its coverage
  log line (`"XGBoost panel weights: %d / %d rows matched feedback bundle"`) by comparing the
  assigned weight to the sentinel `1.0`: `matched = int(np.sum(weights != 1.0))`. But `1.0` is
  also a legitimate, commonly-occurring *real* weight — `_outcome_factor` returns `1.0` for both
  the `direction_only` bucket (any MANUAL/EXPIRED-exit trade with no TP/SL touched but correct
  direction) and the `unknown` bucket (no TP/SL, direction wrong, confidence < 0.70) — see
  `feedback_loader.py:134-139`; `_confidence_factor` is also `1.0` in the non-hard-negative case
  (lines 142-145). Any row in either ordinary bucket is a genuine match with a correctly-assigned
  neutral weight, but this logging line silently reclassifies it as "unmatched" — the only
  coverage signal operators have for this pipeline understates real coverage whenever the
  outcome mix skews toward manual/expired exits or sub-70%-confidence misses.
- **`backend/.scheduled_retrain.lock` is a git-tracked, mutable runtime lock file.** Confirmed
  via `git ls-files` (tracked) and `git diff` (content — pid/started_at — changes on every
  retrain run, matching the `M backend/.scheduled_retrain.lock` seen in this session's own git
  status). It is not `.gitignore`d. Correctness depends solely on `fcntl.flock`
  (inode-scoped, auto-released on process exit — `scripts/scheduled_retrain.py:172-183`), not file
  content; any git operation that recreates the file on disk (checkout/pull/reset touching this
  path) changes its inode, so a concurrent `_probe_lock()`/launch call
  (`api/v1/admin_training.py:371-409`) against the new inode would see no lock held and could let
  an API-triggered retrain launch concurrently with a still-running systemd-timer job — the exact
  double-run scenario the lock exists to prevent. Separately, a permanently-dirty working tree
  after every retrain (this session's own `git status` is an example) trains operators to
  overlook real uncommitted changes hiding behind the expected noise.
- **`MLFeedbackError.attempt_count` DB check constraint hardcodes the retry ceiling, decoupled
  from the actual retry policy.** `models/ml_feedback.py:48-53`:
  `CheckConstraint("attempt_count BETWEEN 1 AND 5", ...)`. If `compute_ml_feedback()`'s retry
  policy is ever retuned (e.g. backoff extended to 8 attempts under load, or reduced to 3), any
  run that legitimately exhausts a different attempt count triggers a DB-level `CheckViolation`
  on insert — turning a routine retry-policy change into a crash in the audit trail itself (the
  last line of defense for spotting systematically failing symbols), rather than a no-op or
  warning. No migration/config coupling ties the retry-count constant to this constraint.

---

### ☐ D. RAG Embedding Source Quality

**Why:** the RAG index treats every ingested article as equal-weight, equal-trust — a
12-character snippet and a 1,124-char article, a regulatory filing and a news blog, are all
embedded and retrieved identically.

- **No pre-embedding quality filter.** `fetch_feed` (`ai/ingestion/rss_fetcher.py:65-95`) only
  rejects empty content — no minimum length (`ingester.py` docstring notes bodies range
  12-1,124 chars, median 273, nothing enforced) and no chunking (`ingester.py:6-10` — one RSS
  item = one embedding row).
- **No source-authority signal reaches RAG.** A `CredibilityScorer`/`AISourceCredibility` system
  already exists (`intelligence/credibility_scorer.py`, `fake_news_detector.py:177-182`,
  0-100 score) but is wired only into `api/v1/intelligence.py` and `llm_client.py` — never called
  from `rss_fetcher.py`, `ingester.py`, or `retriever.py`. Exchange filings (NSE/BSE) and general
  news outlets carry identical weight in retrieval.
- **Dedup is exact-hash only, not near-duplicate aware.** Both `_dedup_events` (intra-batch) and
  the cross-source check (`ingester.py:65-80, 209-226`) are SHA-256 on concatenated
  title+summary. Syndicated wire copy across the 7 configured feeds (`rss_fetcher.py:28-40`)
  with even a one-character difference (byline prefix, whitespace reformat) is not caught.
- **Retrieval ranking has no authority term.** `retriever.py` ranks purely via hybrid BM25 +
  cosine similarity through Reciprocal Rank Fusion, scoped by symbol/24h window — source
  identity plays zero role.

**Decided design:**

- **A. Pre-embedding quality floor — tag, not reject.** Content below a minimum length threshold
  is still embedded (preserves recall for short-but-real disclosures, e.g. a one-line filing
  notice) but tagged `low_confidence_source` so it stays searchable while ranking low.
- **B. Near-duplicate dedup — normalized-hash.** Replace/augment the exact-SHA-256 check
  (`ingester.py:65-80, 209-226`) with a hash over normalized text (stripped whitespace, bylines,
  boilerplate) to catch syndicated wire copy. Chosen over simhash/embedding-similarity dedup as
  the cheaper first pass given modest RSS ingest volume — escalate only if wire-copy duplication
  remains common.
- **C. Source-authority weighting in retrieval.** Wire the existing (currently orphaned)
  `CredibilityScorer`/`AISourceCredibility` score into `retriever.py`'s ranking as a third RRF
  term alongside BM25 and cosine similarity, so a high-trust exchange filing outranks a
  low-trust blog post at equal lexical/semantic relevance.

**Bonus fix (not a design decision, just a correction):** `retriever.py:22`'s module docstring
claims embeddings come from "nv-embedqa-e5-v5 (1024-dim)"; actual implementation
(`embedder.py:10`, `config.py:183-184`) uses `gemini-embedding-001` at 768-dim. Stale comment,
fix alongside the above.

**Status:** design decided (2026-07-05). Not yet implemented.

**Additional gaps found — codebase deep-dive, 2026-07-05 (verified, not yet fixed):**

- **Shared `AsyncSession` across concurrently-gathered RSS feed ingestions — session-safety
  violation, causing silent per-cycle ingestion failures.** `ingestion/rss_fetcher.py:169-177`
  (`rss_ingestion_loop`) opens one `db` session and passes it into all 7
  `fetcher.ingest_feed(db, ...)` coroutines, run concurrently via `asyncio.gather(*tasks)`. Each
  `ingest_feed` (lines 104-142) does its own SELECT-per-item duplicate check plus a
  `db.commit()`. SQLAlchemy `AsyncSession` is not safe for concurrent use by multiple coroutines
  — two feeds executing on the same session concurrently can hit `IllegalStateChangeError`/
  asyncpg `InterfaceError: another operation is in progress`, or one feed's failed statement can
  leave the session in a pending-rollback state that aborts sibling feeds' work mid-cycle. These
  failures are swallowed by `return_exceptions=True` (line 175) and only surface as an opaque
  `errors` count in the cycle-complete log — never diagnosed as a session-sharing bug. On a
  normal poll cycle, 2+ of the 7 feeds can silently fail to ingest every run purely from this
  race, independent of feed content.
- **Silent embedding misalignment (data corruption, not loss) on partial batch responses.**
  `intelligence/llm_client.py:564` builds `vectors` from `resp.embeddings` with no assertion that
  its length matches the input `texts`. `rag/embedder.py:85-96` (`embed_texts`) only checks the
  *dimension* of the first vector, never the *count* of vectors vs. the input batch.
  `rag/ingester.py:94` (`_build_embed_rows`) then `zip(deduped, vectors)` — if Gemini ever
  returns fewer embeddings than requested for a batch (e.g. one text safety-filtered out), every
  event after the dropped index gets *another event's embedding* written to
  `ai_document_embeddings` — a positional mispairing, not a dropped row. Because the row now has
  an embedding, `_fetch_unembedded_events`'s anti-join (`ingester.py:196-226`) never re-selects it
  for repair — the corruption is permanent. (The offline `embed_batch_job` path does check for
  this — "Response count mismatch" — but that check is absent from the synchronous `embed()` path
  the live RSS pipeline actually uses.)
- **`_load_candidates`'s symbol-specific query bypasses the documented 500-candidate safety
  cap.** `rag/retriever.py:239-249` — Step 1 ("fetch ALL instrument-specific docs... always
  included regardless of count") has no `.limit()`, while `_MAX_CANDIDATES=500` (line 65) is
  documented as the "safety cap: prevents pathological queries from pulling the entire corpus."
  For a heavily-covered symbol during earnings/M&A news flow, or the documented
  `window_hours=168` swing-signal case, symbol-specific rows can reach the thousands, bypassing
  the cap and blowing the ~10ms/130-candidate performance model right when retrieval matters most
  — a latency cliff opposite the documented 500ms p95 SLO assumption.
- **RSS bodies decoded twice with disagreeing decoders — silent mojibake corrupting both the
  dedup hash and the prompt text.** `ingestion/rss_fetcher.py:55-59` passes `response.text`
  (httpx's own charset-guessed decode) into `feedparser.parse()`, which is designed to receive
  raw bytes so it can do its own encoding autodetection (BOM, XML `encoding=` attribute, HTTP
  header). Feeding it pre-decoded text defeats that. A feed serving non-UTF-8 bytes without a
  charset in `Content-Type` (common for Indian financial sites using Windows-1252/ISO-8859-1 for
  currency symbols/dashes) can have httpx's guess and the XML-declared encoding disagree, silently
  mojibaking title/summary text before it's hashed (`content_hash`, line 116) and before it
  reaches Gemini prompts — corrupting both dedup determinism and the text actually reasoned over.
- **`CredibilityScorer`'s writer path has zero call sites anywhere — its one real consumer is
  permanently dead, more severe than "unwired into RAG."** Verified via grep:
  `CredibilityScorer.get_or_create_source`/`update_credibility`
  (`intelligence/credibility_scorer.py:20-77`) are never called from any file; the
  `AISourceCredibility` table is never populated in production. Its one genuine consumer,
  `intelligence/fake_news_detector.py:161-185` (`_check_source_credibility`, reachable via
  `api/v1/intelligence.py:174`), always gets `None` back and always falls through to the
  hardcoded neutral `return 0.5` — the credibility-check dimension of the already-built fake-news
  detector is functionally inert for every source, every call, not merely orphaned from RAG.
  Secondary: `update_credibility` (lines 47-77) is an unlocked read-modify-write with no row
  lock — concurrent updates for the same source can lose an update.
- **N+1 duplicate-check queries in RSS ingestion, compounding the session race above.**
  `ingestion/rss_fetcher.py:114-122` issues one `SELECT ... WHERE content_hash == :h` per RSS
  item in a Python loop rather than one batched `IN (...)` check — on the order of a couple
  hundred serialized round-trips per poll cycle across 7 feeds, all funneled through the single
  shared, concurrently-contended session described above.

---

### ☐ E. Sentiment Scoring Accuracy

**Why:** sentiment (feeding both explanations and trading signals) is pure-Gemini today — FinBERT
was fully removed from production after a one-time calibration check
(`intelligence/nlp_engine.py:36-37`, Pearson r=0.995 against fixtures). Four accuracy risks found:

- **Batching cross-contamination.** `_call_batch_llm` (`intelligence/nlp_engine.py:753-840`)
  concatenates unrelated articles into one prompt, asks Gemini for per-index results in a single
  generation. Validation only checks index completeness/no-duplicates — never that a returned
  label is actually about that article's content, so a structurally valid but misattributed
  response passes silently. Batch mode also drops the `reasoning` field entirely to save tokens
  (unlike the single-article path) — the common case (batched calls) has zero post-hoc
  auditability.
- **No live FinBERT drift detection.** FinBERT survives only as a one-time offline fixture gate
  in `eval/run_eval.py` (r≥0.80). Nothing catches Gemini's calibration drifting in production.
- **Aggregation is a weighted mean, not majority vote.**
  `services/sentiment_analysis_service.py:169-211` — `weight = recency_decay(12h half-life) ×
  source_weight × max(confidence, 0.3)`. A single very recent, high-confidence article can
  dominate over many older, disagreeing ones. Source weighting is a separate hardcoded 2-entry
  dict (`_SOURCE_WEIGHTS`, everything else defaults to 0.7) — the same orphaned
  `credibility_scorer.py` from Proposal D is unused here too.
- **L1 cache has no time TTL.** Per-instrument aggregate sentiment has a 15-min Redis (L2) TTL by
  design, but the in-process L1 LRU cache (`sentiment_analysis_service.py:80`) is bounded only by
  LRU eviction size, not time — a stale aggregate can persist in a worker process past the
  intended 15-minute window.

**Decided fixes:**

- **A.** Keep `reasoning` in batch mode (cheap, few extra tokens) for auditability; add a
  lightweight content-consistency spot-check (entity/keyword overlap between article and
  returned label) to `_call_batch_llm` to catch misattribution.
- **B.** Add periodic live dual-write sampling (e.g. weekly, small % of traffic) against offline
  FinBERT to catch production calibration drift, reusing the existing r≥0.80 gate logic instead
  of running it once against fixtures only.
- **C.** Wire `credibility_scorer.py`'s existing score into `_SOURCE_WEIGHTS`
  (`sentiment_analysis_service.py`) instead of the hardcoded 2-entry dict — same fix shape as
  Proposal D's source-authority wiring, reusing the same orphaned component.
- **D.** Add an explicit TTL to the L1 in-process cache matching/bounding the L2 900s window, so
  aggregate staleness can't silently exceed the intended 15 minutes.

**Status:** all four fixes confirmed (2026-07-05). Not yet implemented. Fix C should land
alongside Proposal D's credibility-scorer wiring — both consume the same currently-unused
component.

**Additional gaps found — codebase deep-dive, 2026-07-05 (verified, not yet fixed):**

- **Trading-facing aggregate sentiment discards an already-computed full-body score and
  recomputes from headline-only text — double LLM cost, weaker input.**
  `services/sentiment_analysis_service.py:166-167` extracts `titles` via `_extract_title` and
  calls `analyze_sentiment_batch(titles)`. But `_query_classified_events`
  (lines 323-351) JOINs through to `AINLPResult`, meaning every event returned already has a
  full-article-body sentiment score/label/confidence computed by `nlp_engine.py:340-351`
  (`process_event()`, which analyzes `content`, not a headline) — that value is never selected or
  reused. `_extract_title` (lines 393-400) returns only the title or first-300-chars of raw
  content, never the summary/body. Result: two independent Gemini calls per article (one in
  `process_event`, a second here), and the weaker headline-only signal is the one that actually
  feeds `impact_score` / the bullish-bearish label consumed by trading signals.
- **No cross-field validation between sentiment `label` and `score` sign — a hallucinated sign
  flip passes silently and corrupts label counts and the numeric aggregate in opposite
  directions.** `intelligence/nlp_engine.py:85-108` (`SentimentOutput`) and `:111-126`
  (`_SentimentBatchItem`) range-constrain `score` (`-1.0..1.0`) and `confidence` (`0.0..1.0`)
  independently — nothing enforces the prompt's own rule that positive labels imply `score > 0`
  and negative labels imply `score < 0` (`_SENTIMENT_SYSTEM_PROMPT:175-178`). If Gemini ever
  returns e.g. `label="negative", score=+0.4`, `sentiment_analysis_service.py:184-189` buckets it
  into `neg_count` (breakdown reads bearish-dominant) while `weighted_score_sum += score * w`
  (line 201) adds a *positive* contribution — the response becomes internally contradictory
  (`breakdown.negative_pct` high while `impact_score`/`sentiment_label` reads bullish) with
  nothing catching the mismatch before it reaches the trading-facing card.
- **`fake_news_detector.py`'s cross-reference corroboration layer (30% weight, the single
  highest-weighted layer) matches on `event_type` alone with zero entity/company overlap
  check.** `intelligence/fake_news_detector.py:187-229` (`_check_cross_reference`) queries
  `AIEventClassification` filtered only by `event_type == event_type` and a time cutoff — no
  filter on `affected_symbols`, source, or text/entity overlap — and returns `0.9`
  ("well corroborated") once `>= 3` matches exist. During earnings season, dozens of unrelated
  companies publish genuine `event_type="earnings"` classifications within any 24h window, so a
  fabricated earnings story about an unrelated company scores 0.9 purely because ≥3 *other*
  companies' real earnings news exist in the same window. Reachable in production via
  `POST /intelligence/fake-news/detect`.
- **`fake_news_detector.py`'s sentiment-consistency layer (20% weight) uses raw substring
  matching with no word boundaries, producing systematic false positives on common short
  tokens.** `intelligence/fake_news_detector.py:246-261` checks `"up" in content_lower` /
  `"down" in content_lower` / `"miss" in content_lower` etc. — `"up"` matches inside
  `"group"`/`"support"`/`"supply"`/`"startup"`; `"down"` matches inside
  `"showdown"`/`"breakdown"`; `"miss"` matches inside `"missing"`/`"dismiss"`/`"commissioner"`.
  A genuinely credible regulatory-action article containing e.g. "XYZ **Group**" can flip
  `_check_sentiment_consistency`'s output for `event_type in ["regulatory","geopolitical"]`
  (lines 265-271) from a correct 0.9 down to 0.3 based purely on an unrelated substring, dragging
  a credible article's `final_score` toward the fake-news-flagged threshold.
- **Secondary:** the FinBERT calibration gate (`eval/run_eval.py:723-756`,
  `_evaluate_sentiment_calibration`) draws from only 15 hardcoded fixture rows
  (`eval/gold_set.jsonl`, SC001-SC015) and requires just `len(finbert_scores) >= 2` to compute a
  Pearson r at all — a near-empty or filtered-down eval run could still produce a "passing" r
  from as few as 2 points, compounding (not duplicating) the doc's existing "no live drift
  detection" gap: the static one-time gate is itself statistically fragile.

---

### ☐ F. Correlation Engine Consensus Scoring

**Why:** `_compute_consensus` (`correlation/engine.py:630-799`) blends `scanner_signal`,
`ai_signal` (news forecast), and `ml_signal` via static weights (`SCANNER_WEIGHT=0.30,
AI_WEIGHT=0.40, ML_WEIGHT=0.30`, lines 51-53) into `consensus_score`. Three issues found, one of
which directly compounds the root-cause bug (§0).

- **Missing-component handling doesn't account for `batch_pending`.** The `ai_available` gate
  (line 785) only checks `available` + `event_count > 0` — it does not check whether `ai_signal`
  is still `batch_pending` (missing direction/rationale/sentiment_label, per §0). When the news
  forecast hasn't landed yet, `ai_available` still evaluates True, the full 0.40 weight is
  retained, and `ai_conf` silently defaults to 0.0 via `.get(..., 0.0)` — dragging the consensus
  score down as if AI genuinely found nothing, instead of excluding the component and
  renormalizing to scanner/ML (which the code already does correctly for genuine
  unavailability).
- **Artificial unanimity from forced direction alignment.** Direction agreement is a hard binary
  gate (lines 742-765): unanimous BUY/SELL required, any mismatch → `DIRECTION_MISMATCH`, score
  forced to 0, suggestion rejected. But when `ai_score == 0` (including the `batch_pending` case
  above), direction is coerced to align with `scanner_dir` (line 715) instead of being excluded
  from the vote. A suggestion can be created as BUY purely because the pending AI signal was
  forced to agree, when the real forecast (landing moments later) might say SELL — the
  scoring-side twin of the root-cause bug.
- **No regime awareness in scoring.** `regime_type` stored on the suggestion
  (`correlation/engine.py:838-840`) is a post-hoc placeholder (`"bull_trending" if all_buy else
  "bear_trending"`, explicitly commented as such) — not real regime classification.
  `regime_detector.py` is never imported into `engine.py`; weights/thresholds
  (`CONSENSUS_HIGH=80`, `CONSENSUS_MEDIUM=60`) are static regardless of market regime.

**Decided fixes:**

- **A.** Fix `ai_available` to also check for `batch_pending`/missing-direction state — treat
  pending as genuinely unavailable, renormalizing weight to scanner/ML instead of silently
  zero-dragging the score.
- **B.** Stop force-aligning `ai_score == 0` to `scanner_dir` when the zero is due to pending
  state (vs. a genuinely computed neutral) — exclude pending AI from the unanimity vote entirely
  rather than manufacturing agreement.
- **C.** Wire real `regime_detector.py` output into consensus weighting (e.g. adjust
  SCANNER/AI/ML weights by regime confidence), replacing the post-hoc placeholder label with an
  actual input. Larger architectural change than A/B.

**Status:** all three fixes confirmed (2026-07-05). Not yet implemented. Fixes A/B should land
together with (or before) Proposal A (demand-gated redesign) — both concern the same
`batch_pending`/race-condition surface.

**Additional gaps found — codebase deep-dive, 2026-07-05 (verified, not yet fixed):**

- **Pathway-2's synthetic scanner-fallback direction is derived from an unsigned severity
  magnitude, not an actual directional signal — effectively hardcoding "buy" for almost every
  news event.** `correlation/engine.py:563-566` (`_resolve_scanner_signal_for_symbol`):
  `impact = float(event.impact_score); fallback = {"direction": "buy" if impact > 0 else
  "sell", ...}`. `event.impact_score` (`AIEventClassification`, `Numeric(5,2)`,
  `intelligence/event_classifier.py:188-192`) is a **severity magnitude, 0-100** ("0 = no
  impact, 100 = extreme impact") with no sign — it does not encode bullish vs. bearish. The
  actual directional field, `sentiment: Literal["bullish","bearish","neutral"]`
  (`event_classifier.py:194-197,758,766,965-976`), is computed by the classifier but never
  written to the DB row — `_persist_classification` (`event_classifier.py:457-495`) has no
  `sentiment=` kwarg in either constructor call, and `AIEventClassification` has no such column.
  By the time `on_news_event` (`engine.py:368`) reads the persisted row, sentiment is gone; only
  the unsigned severity survives. Since `impact_score` defaults to 50.0 and is constrained
  `>= 0`, `impact > 0` is true for virtually every real event — this fallback returns `"buy"`
  almost unconditionally; `"sell"` only fires when `impact_score == 0.0` exactly. **Concrete
  compounding scenario:** a severely bearish event (fraud investigation, impact_score=85.0 for
  severity) triggers Pathway 2 for a symbol outside the scanner cache; the fallback returns
  `direction="buy"`. If the real AI forecast correctly computes SELL and ML also predicts SELL,
  the consensus direction-unanimity gate (`engine.py:742-765`) sees a 3-way mismatch and rejects a
  genuinely correct bearish consensus. Worse: if the AI forecast is still `batch_pending`
  (§0/this section's already-documented race), `ai_dir` force-aligns to `scanner_dir`
  (`engine.py:715`) — i.e. to this fabricated "buy" — and if ML leans BUY too, the system can
  manufacture a **BUY** trade suggestion directly off bearish news.
- **`safety_trigger_engine.py` has no loss/P&L circuit-breaker path at all — not just
  placeholder data for the metrics it does check.** `safety/safety_trigger_engine.py:150-154`
  gathers a `loss_pct` metric (commented "Placeholder" like `signal_rate`/`volatility`), and the
  surrounding comment (line 145) explicitly names "Recent P&L from trading signals" as an
  intended check — but `check_safety_conditions` (lines 31-69) only ever reads
  `metrics.get("signal_rate")` and `metrics.get("volatility")`; `loss_pct` is never referenced
  anywhere else in the file. This is distinct from the already-documented "hardcoded placeholder
  metrics" finding: even if `signal_rate`/`volatility` were wired to real data, there is
  structurally no drawdown/realized-loss kill-switch trigger in this engine — it can never fire
  on P&L deterioration, only on signal frequency or volatility, by construction.

---

## Key Files Index

| File | Relevant lines |
|---|---|
| `backend/app/ai/correlation/engine.py` | 162-166, 368, 400, 428, 468, 492, 563-566, 630-799, 715, 742-765, 785, 838-840, 973-1097 |
| `backend/app/ai/fusion/signal_assembler.py` | 184-207, 271-303, 309-379, 361, 419-468, 437-439 |
| `backend/app/ai/fusion/forecast_batch_worker.py` | 127-149, 245-250, 339-361, 396-476, 527-545 |
| `backend/app/ai/intelligence/explanation_worker.py` | 242-341, 279-291, 388-424, 472-482, 485-607, 552-576, 598-605, 1638-1645 |
| `backend/app/ai/intelligence/event_classifier.py` | 188-197, 457-495, 758, 766, 839-935, 965-976 |
| `backend/app/ai/intelligence/nlp_engine.py` | 36-37, 85-126, 340-351, 753-840 |
| `backend/app/ai/intelligence/credibility_scorer.py` | 20-77 (writer path never called from any file) |
| `backend/app/ai/intelligence/fake_news_detector.py` | 161-185, 187-229, 246-271 |
| `backend/app/ai/safety/safety_trigger_engine.py` | 31-69, 145, 150-154 |
| `backend/app/ai/intelligence/llm_client.py` | 564 |
| `backend/app/ai/ingestion/rss_fetcher.py` | 28-40, 55-59, 65-95, 104-142, 127, 169-177 |
| `backend/app/ai/ingestion/ingester.py` | 6-10, 65-80, 94, 196-226, 209-226 |
| `backend/app/ai/rag/retriever.py` | 22 (stale docstring), 65, 92, 105, 200, 239-249, 274 |
| `backend/app/ai/rag/embedder.py` | 10, 85-96 |
| `backend/app/api/v1/ai_stream.py` | 187-214, 319-408, 461-463, 512, 596-663, 696-786 |
| `backend/app/api/worker_ai_processing.py` | 103-125 |
| `backend/app/services/market_scanner.py` | 338, 369 |
| `backend/app/services/sentiment_analysis_service.py` | 80, 166-211, 323-351, 393-400 |
| `backend/app/ml/training/feedback_loader.py` | 79-90, 127-159, 189-210, 562-574 |
| `backend/app/models/ml_feedback.py` | 48-53 |
| `backend/scripts/scheduled_retrain.py` | 172-189 |
| `backend/.scheduled_retrain.lock` | git-tracked (should not be) |
| `backend/app/api/v1/admin_training.py` | 371-409 |
| `backend/app/worker.py` | ~600, 695, 710, 836-837 |
| `backend/eval/run_eval.py` | 723-756 (r≥0.80 gate, 15-fixture Pearson r) |
| `frontend/.../AIExplanationPanel.tsx` | 286-297, 387-388 |
| `frontend/src/components/AnalysisCardsSection.tsx` | 52-53, 98, 105-107, 109-133, 234, 247, 264, 293 |
| `frontend/src/contexts/AuthContext.tsx` | 79-83 |
| `frontend/src/hooks/useTypewriter.ts` | 11 |

---

*Superseded/source doc: `Bug's_and_Improvement_Points_of_the_Ai_Explanations.md` (raw review
notes). This file is the house-style, checklist-tracked version — keep both in sync until the
final drafting pass, then retire the source doc.*
