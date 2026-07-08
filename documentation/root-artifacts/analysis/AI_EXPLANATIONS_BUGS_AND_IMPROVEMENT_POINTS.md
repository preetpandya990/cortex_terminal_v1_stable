# Bugs & Improvement Points — AI Explanations

## SOP: Why This Document Exists

The AI Explanation feature (Gemini-generated narratives explaining ML predictions/signals for
instruments) is undergoing a review because the explanations and the pipeline that produces them
have drifted from the quality bar the underlying ML/signal system is capable of. Specifically:
ML-generated values (forecasts, sentiment, pattern signals) are sometimes not present in the
prompt sent to Gemini, meaning the explanation is generated without the very data it's supposed to
explain. This document is the working record of verified findings from that review — root causes,
file:line references, and why each one matters — so that fixes can be planned and tracked without
re-deriving the investigation each time.

This is a living document: findings are appended/refined as the review continues, and each item
should be marked resolved once fixed and verified.

---

## Confirmed Root Cause: ML Values Missing From Prompt

**Symptom:** Explanations sometimes omit ML-generated data (forecast direction, rationale,
sentiment) even though that data exists — because it hadn't finished computing at the moment the
explanation job was built.

- `backend/app/ai/correlation/engine.py:973-1097` (`_compute_consensus`) snapshots the
  `TradeSuggestion` (`ml_signal`, `ai_signal`, `scanner_signal`) and publishes the explanation job
  to `cortex.explanation.jobs` in the same function, before async news/ML forecasting has settled.
- `backend/app/ai/fusion/signal_assembler.py:309-379` (`gather_news_forecast`): on a cache miss —
  the common case for a symbol/event/indicator combo seen for the first time — it enqueues a job
  to `forecast_batch_worker` and immediately returns a `_fallback("batch_pending")` shape built
  from `event_signals` (`signal_assembler.py:271-303`), which only has
  `score/confidence/event_count/events/available`. It never has `direction`, `rationale`, or
  `sentiment_label`.
- `backend/app/ai/intelligence/explanation_worker.py:558-576` (`_build_explanation_prompt`) reads
  `ai.get("direction")`, `ai.get("rationale")`, `ai.get("sentiment_label")` — all absent on the
  batch-pending shape — so the "News Forecaster Lean/View" section is silently dropped from the
  prompt. Not because there's no news, but because the real Gemini forecast hadn't landed yet.
- `backend/app/ai/fusion/forecast_batch_worker.py:396-457`: the batch window is 60s
  (`NEWS_FORECAST_BATCH_WINDOW_SECS`) plus up to ~22s Gemini latency — a realistic 1–2 minute miss
  window, not a rare edge case.
- `forecast_batch_worker.py:471-476`: once computed, the real forecast is written to the same
  Redis cache key but is **never propagated back** to the already-committed
  `TradeSuggestion.ai_signal` — confirmed no UPDATE ever touches that column. The next correlation
  cycle for the same symbol benefits from the cached forecast; this specific suggestion's
  explanation never does.

---

## Findings by Area

### 1. Content Quality
- Guardrails (`explanation_worker.py:388-424`) only regex-filter price predictions and check for
  presence of a `[Source ...]` citation. There is no post-hoc check that numbers cited in
  `full_explanation` actually match the numbers that were in the prompt — a hallucinated
  confidence % or RSI value would pass all guardrails silently.
- The "GROUND every ML/technical claim" instruction (`explanation_worker.py:279-291`) is text-only
  guidance to the model; nothing enforces it programmatically.

### 2. Prompt / Context Design
- `_render_scanner` (`explanation_worker.py:485-498`) silently omits the entire "Technical
  Scanner Readings" section when `scanner_signal` is empty/unavailable. Unlike the news-context
  branch (`explanation_worker.py:598-605`), there's no explicit "no technical data available"
  fallback line — Gemini has no signal that data was expected but missing vs. genuinely neutral.
- Pathway-2 (news-triggered) suggestions get a synthetic `scanner_signal` fallback from
  `_resolve_scanner_signal_for_symbol` (engine.py) with `available=False` — this degrades to a
  near-empty technical section for a whole class of suggestions, not just rare edge cases.
- `_EXPLANATION_SYSTEM_PROMPT` and `_CONTEXT_SYSTEM_PROMPT` (`explanation_worker.py:242-341`) are
  ~95% duplicated text — a maintenance risk, since a rule fixed in one is easily missed in the
  other.

### 3. Pipeline / Trigger Logic
- `backend/app/ai/safety/safety_trigger_engine.py` is an unrelated global kill-switch
  (signal-rate/volatility) with hardcoded placeholder metrics (e.g. `"signal_rate": 0`). It does
  not gate explanation dispatch on ML freshness at all — there is no freshness check anywhere
  between "ML finished computing" and "explanation job fires."
- `backend/app/api/worker_ai_processing.py:103-125` (`dispatch_forecast`) drains the forecast
  batch queue on demand but never re-reads/updates `TradeSuggestion.ai_signal`.
- Context-job `prediction_data` (`explanation_worker.py:1638-1645`) is built in
  `backend/app/api/v1/ai_stream.py:461-463` from an in-memory `state.prediction`, refreshed only
  every 60s (`_PREDICTION_REFRESH_SECS`, `ai_stream.py:596-629`) — up to 60s stale at dispatch
  time, independent of the Kafka job's own age.

### 4. Delivery / UX
- `ai_stream.py:187-214, 650-663`: once `state.explanation` is populated, it is never invalidated
  even if `state.prediction` later flips direction on a subsequent 60s refresh — two independent
  producers with no cross-check. A delivered BUY explanation can sit next to a freshly-refreshed
  SELL prediction card in the UI.
- `ai_stream.py:319-408` (Stage 1): explanations are served from whichever `TradeSuggestion` row
  is most recent within a 7-day lookback, with no re-check against current signal state — a stale
  explanation persists until an entirely new suggestion is created.

### 5. Business Logic
- Direct consequence of the root-cause bug above: a suggestion's `llm_explanation` can claim "no
  significant news signal" in the News Context section while a real, materially
  bullish/bearish Gemini forecast exists in cache seconds later — a semantic mismatch between what
  the explanation claims and what the system actually knows. Currently undiscoverable by the user,
  since nothing flags the mismatch.

---

## Key Files Referenced

- `backend/app/ai/correlation/engine.py:973-1097`
- `backend/app/ai/fusion/signal_assembler.py:309-379`
- `backend/app/ai/fusion/forecast_batch_worker.py:396-476`
- `backend/app/ai/intelligence/explanation_worker.py:242-341, 388-424, 485-607, 558-576, 1638-1645`
- `backend/app/api/v1/ai_stream.py:187-214, 319-408, 461-463, 596-663`
- `backend/app/ai/safety/safety_trigger_engine.py:31-172`
- `backend/app/api/worker_ai_processing.py:103-125`

---

---

## Improvement Proposal: Demand-Gated Analysis + Full-Article Prompting

### Why

Today, event-to-instrument tagging is already deterministic and free (regex ticker/name extraction
+ spaCy NER, resolved against `instrument_master` — no LLM call). But the downstream Gemini calls
that actually analyze events are not gated by real demand:

- `backend/app/ai/intelligence/event_classifier.py:839-935` — Gemini event-type classification,
  fires for the ~5-10% of articles the keyword heuristic can't bucket.
- `backend/app/ai/fusion/signal_assembler.py:309-379` (`gather_news_forecast`) — a separate
  per-symbol Gemini forecast call. On cache miss it only checks a circuit breaker
  (`_forecast_breaker.allow()`, line 361) and a Redis dedup key (`_enqueue_for_batch_forecast`,
  lines 419-468) before publishing to Kafka (line 466) — **no watchlist or active-trade-suggestion
  check anywhere in this path.**

Confirmed call chain that drives this: `backend/app/worker.py`'s `correlation_loop` (~line 600)
runs `scanner_svc.scan_all()` (line 695) → `MarketScannerService._fetch_db_baselines`
(`backend/app/services/market_scanner.py:338`) selects **every actively-traded instrument** with
recent OHLCV data (filtered only by `im.is_active IS DISTINCT FROM FALSE`, line 369) — not a
watchlist subset. Anomalies above threshold (`worker.py:710`) call `engine.on_scanner_anomaly`
(`correlation/engine.py:468`) → `gather_news_forecast` (line 492). Separately,
`engine.on_news_event` (line 368) iterates `event.affected_symbols` unconditionally (line 400) and
also calls `gather_news_forecast` (line 428). Result: forecast jobs fire for any symbol the
technical-anomaly or news-event filters surface, whether or not anyone is watching it — this is a
real, uncapped RPD cost driver.

This proposal also directly addresses the root-cause bug above (ML values missing from the
explanation prompt): that bug exists because the forecast is pre-computed asynchronously, ahead of
and disconnected from the moment the explanation is actually built, creating a race. Moving
forecast/analysis to run *at* explanation-demand time, in the same call, eliminates the race
entirely for that pathway.

### Proposed Design

The proposal splits into two independent, complementary pieces — because the forecast/classification
signal currently serves two different consumers that can't be gated identically:

1. **Suggestion creation/ranking** (pathway1/pathway2 in `correlation/engine.py`) still needs some
   signal to decide whether a symbol becomes a trade suggestion in the first place — this can't be
   gated purely on "already an active trade suggestion," since that's circular.
2. **Explanation generation** is the part this proposal targets cleanly: nobody needs an
   explanation until a human opens a watchlist card or a trade-suggestion detail view. This part
   can be fully demand-gated.

**A. Local demand-tag service** — a lightweight local process, no LLM calls, that continuously
cross-references `instrument_master` against the `watchlist` and active `trade_suggestions` tables
(cheap DB query, refreshed on a short interval or via DB trigger/pub-sub) and maintains a live
"in-demand symbol set." Used to:
   - Suppress background forecast-batch enqueues for symbols that are neither watched nor already
     anomaly-flagged, cutting speculative RPD spend on symbols nobody will ever see.
   - Prioritize the in-demand set first when the batch worker drains its queue.

**B. Explanation-time full-article prompting** — when a user actually demands an explanation,
`explanation_worker` fetches the raw article text for the relevant event(s) directly and feeds the
**full article** into the Gemini prompt at that moment, in one call — instead of assembling the
prompt from pre-computed, possibly-stale-or-missing classification/forecast fields written by an
earlier async job. This does instrument-relevance, forecast, and narrative generation together,
synchronously, grounded in the actual source text.

### Why This Hits Multiple Targets At Once

1. **RPD budget** — background analysis is no longer wasted on symbols nobody is watching or
   holding a suggestion for.
2. **Meaningful explanations** — fixes the root-cause bug: no more race between an async forecast
   job and prompt-build time, because analysis happens fresh, in the same call, at demand time.
3. **Insightful responses** — Gemini reasons over the full source article rather than a
   pre-digested, sometimes-empty intermediate summary field.

### Status

Proposal agreed in principle (2026-07-04). Scope split (suggestion-scoring pathway stays
essentially as-is; explanation generation becomes demand-triggered + full-article) confirmed.
**Not yet finalized into an implementation plan — revisit in final drafting pass.**

---

## Delivery/UX Invalidation Fix: Direction-Mismatch Flag

### Why

`backend/app/api/v1/ai_stream.py:650-663` (`_refresh_explanation`) and `ai_stream.py:596-629`
(`_refresh_prediction`) are two independent producers writing to the same per-connection `state`
object, each on its own refresh interval, with `_emit_update()` firing after either one changes.
Nothing compares `state.prediction.get("direction")` against
`state.explanation.get("signal_direction")` at emit time.

On the frontend, `AIExplanationPanel.tsx:387-388` (`showStalenessBanner`) only checks
`context_type === 'suggestion_explanation' && signal_generated_at` — this is purely an **age**
banner ("Based on BUY signal · 6h ago"). There is no comparison against the live prediction card's
current direction, even though both values arrive in the same SSE payload.

Net effect: a user can see a confident BUY narrative sitting beside a freshly refreshed SELL
prediction card, with the UI signaling only that the explanation is *old*, never that it's
*contradicted*.

### Decided Fix — Option 1: Backend-Computed Mismatch Flag

Compare `state.prediction.get("direction")` vs `state.explanation.get("signal_direction")` at the
point `_emit_update()` fires (or inside `_refresh_explanation`, `ai_stream.py:650-663`), and add a
`direction_mismatch: bool` field to the SSE payload. The frontend renders a stronger "⚠ Signal has
changed since this explanation" banner when true, alongside (not replacing) the existing age-based
staleness banner.

Rejected alternative: computing the comparison client-side in `AIExplanationPanel`'s parent. Would
avoid a backend change but duplicates "what counts as a mismatch" logic across every consumer
(panel, admin queue card, etc.) and risks drifting from the backend's definition of direction —
one authoritative comparison point in the backend was preferred instead.

### Status

Design decided (2026-07-04), **not yet implemented** — revisit in final drafting pass alongside
the demand-gated redesign above (which should also shrink the mismatch window by moving
explanation generation closer to demand time).

---

## Improvement Proposal: OHLCV-Grounded Prompts + Structured Retraining Feedback (B2)

### Why

The ML model is still in an early/learning stage and makes frequent errors. Today the explanation
prompt has no raw OHLCV price-action data — only pre-aggregated scalars from `scanner_signal`
(`last_price, previous_close, volume, volume_ratio, price_change_pct, rsi`;
`backend/app/ai/intelligence/explanation_worker.py:472-482, 552-556`). `explanation_worker.py` has
no existing wiring to bar-series OHLCV data at all (no `FeatureLoader` or equivalent import) —
fetching it at prompt-build time is new work, not a rewire.

Separately, the retraining pipeline is purely quantitative today: `compute_sample_weight`
(`backend/app/ml/training/feedback_loader.py:148-159`) and its `_FEEDBACK_QUERY` (189-210) reweight
training samples using only realized outcomes — `ml_direction_correct, hit_tp1/2/3, hit_sl,
confidence_score`. There is no structured "here's *why* the model was wrong" signal anywhere;
`MLFeedbackError` (`backend/app/models/ml_feedback.py`) logs pipeline/computation crashes, not
prediction misses. So today Gemini's reasoning about price action, even if added to the prompt,
has no path into the retraining loop — this proposal builds that path.

### Proposed Design

**A. OHLCV in the outgoing prompt** — add a compact, token-conscious price-action summary (recent
trend, swing high/low, volatility over a short bar window — not a raw per-bar dump) to
`_build_explanation_prompt`/`_build_context_prompt`, so Gemini can reason over actual price
behavior instead of only derived scalars, and catch cases where the ML's directional call
contradicts what price is actually doing.

**B2. Structured, automatable retraining signal** — constrain Gemini's output schema to include
enumerated, machine-consumable fields (e.g. `likely_missed_pattern: <enum>`,
`confidence_should_have_been_lower: bool`) reflecting its OHLCV-grounded assessment of the ML
call. Wire these fields into `compute_sample_weight`
(`backend/app/ml/training/feedback_loader.py:148-159`) as an additional multiplier alongside the
existing outcome-factor (B1) and confidence-hard-negative-factor (B2 in the current formula,
lines 79-90, 127-145) — requires designing the enum/output schema and extending the sample-weight
formula to fold in this new Gemini-derived term.

### Status

Direction confirmed (2026-07-04): B2 (structured signal wired directly into
`compute_sample_weight`), not B1 (human-review-only annotations). **Not yet designed in detail or
implemented — revisit in final drafting pass.**

---

## Improvement Proposal: RAG Embedding Source Quality

### Why

The RAG index that supplies retrieved source context to the explanation prompt treats every
ingested article as an equal-weight, equal-trust vector — a 12-character snippet and a 1,124-char
article, a regulatory filing and a news blog, are all embedded and retrieved identically:

- **No pre-embedding quality filter.** `fetch_feed` (`backend/app/ai/ingestion/rss_fetcher.py:65-95`)
  only rejects empty content — no minimum length (`ingester.py` docstring notes bodies range
  12–1,124 chars, median 273, with nothing enforced) and no chunking (`ingester.py:6-10` — one RSS
  item = one embedding row).
- **No source-authority signal reaches RAG at all.** A `CredibilityScorer`/`AISourceCredibility`
  system already exists (`backend/app/ai/intelligence/credibility_scorer.py`,
  `fake_news_detector.py:177-182`, 0-100 score) but is wired only into
  `backend/app/api/v1/intelligence.py` and `llm_client.py` — never called from `rss_fetcher.py`,
  `ingester.py`, or `retriever.py`. Exchange filings (NSE/BSE) and general news outlets carry
  identical weight in retrieval.
- **Dedup is exact-hash only, not near-duplicate aware.** Both `_dedup_events` (intra-batch) and
  the cross-source check (`ingester.py:65-80, 209-226`) are SHA-256 on concatenated
  title+summary. Syndicated wire copy appearing across the 7 configured feeds
  (`rss_fetcher.py:28-40`) with even a one-character difference (byline prefix, whitespace
  reformat) is not caught, letting near-identical entries pollute the index.
- **Retrieval ranking has no authority term.** `retriever.py` ranks purely via hybrid BM25 +
  cosine similarity through Reciprocal Rank Fusion, scoped by symbol/24h window — source identity
  plays zero role today.

### Decided Design

**A. Pre-embedding quality floor — tag, not reject.** Content below a minimum length threshold is
still embedded (preserves recall for genuinely short-but-real disclosures, e.g. a one-line filing
notice) but tagged `low_confidence_source` so it remains searchable while ranking low.

**B. Near-duplicate dedup — normalized-hash.** Replace/augment the exact-SHA-256 check
(`ingester.py:65-80, 209-226`) with a hash over normalized text (stripped whitespace, bylines,
boilerplate prefixes/suffixes) to catch syndicated wire copy that differs only in formatting.
Chosen over simhash/embedding-similarity dedup as the cheaper first pass, given modest RSS ingest
volume — escalate only if wire-copy duplication remains common after this.

**C. Source-authority weighting in retrieval.** Wire the existing (currently orphaned)
`CredibilityScorer`/`AISourceCredibility` score into `retriever.py`'s ranking as a third term in
the Reciprocal Rank Fusion alongside BM25 and cosine similarity, so a high-trust exchange filing
outranks a low-trust blog post at equal lexical/semantic relevance.

### Bonus Fix (not a design decision, just a correction)

`retriever.py:22`'s module docstring claims embeddings come from "nv-embedqa-e5-v5 (1024-dim)";
actual implementation (`embedder.py:10`, `config.py:183-184`) uses `gemini-embedding-001` at
768-dim. Stale comment, not a functional bug — fix alongside the above.

### Status

Design decided (2026-07-05): tag-not-reject for short content, normalized-hash for dedup, plus
credibility-score wiring into RRF ranking. **Not yet implemented — revisit in final drafting
pass.**

---

## Improvement Proposal: Sentiment Scoring Accuracy

### Why

Sentiment (feeding both explanations and trading signals) is pure-Gemini today — FinBERT was
fully removed from production after a one-time calibration check
(`backend/app/ai/intelligence/nlp_engine.py:36-37`, Pearson r=0.995 against fixtures). Four
concrete accuracy risks were found:

- **Batching cross-contamination.** `_call_batch_llm`
  (`backend/app/ai/intelligence/nlp_engine.py:753-840`) concatenates unrelated articles into one
  prompt and asks Gemini for per-index results in a single generation. Validation only checks
  index completeness/no-duplicates — never that a returned label is actually about that article's
  content, so a structurally valid but misattributed response passes silently. Batch mode also
  drops the `reasoning` field entirely to save tokens, unlike the single-article path — the common
  case (batched calls) has zero post-hoc auditability.
- **No live FinBERT drift detection.** FinBERT survives only as a one-time offline fixture gate
  in `backend/eval/run_eval.py` (r≥0.80). Nothing catches Gemini's sentiment calibration drifting
  after launch, in production.
- **Aggregation is a weighted mean, not majority vote.**
  `backend/app/services/sentiment_analysis_service.py:169-211` —
  `weight = recency_decay(12h half-life) × source_weight × max(confidence, 0.3)`. A single very
  recent, high-confidence article can dominate over many older, disagreeing ones. Source
  weighting is a separate hardcoded 2-entry dict (`_SOURCE_WEIGHTS`, everything else defaults to
  0.7) — the same orphaned `credibility_scorer.py` found in the RAG source-quality discussion is
  unused here as well.
- **L1 cache has no time TTL.** Per-instrument aggregate sentiment has a 15-min Redis (L2) TTL by
  design, but the in-process L1 LRU cache (`sentiment_analysis_service.py`, line 80) is bounded
  only by LRU eviction size, not time — a stale aggregate can persist in a worker process past the
  intended 15-minute window.

### Decided Fixes

**A.** Keep the `reasoning` field in batch mode (cheap, few extra tokens) for auditability, and
add a lightweight content-consistency spot-check (entity/keyword overlap between article and
returned label) to catch misattribution in `_call_batch_llm`.

**B.** Add periodic live dual-write sampling (e.g. weekly, small % of traffic) against offline
FinBERT to catch production calibration drift, reusing the existing r≥0.80 gate logic instead of
running it once against fixtures only.

**C.** Wire `credibility_scorer.py`'s existing score into `_SOURCE_WEIGHTS`
(`sentiment_analysis_service.py`) instead of the hardcoded 2-entry dict — same fix shape as the
RAG source-authority proposal above, reusing the same currently-orphaned component.

**D.** Add an explicit TTL to the L1 in-process cache matching/bounding the L2 900s window, so
aggregate staleness can't silently exceed the intended 15 minutes.

### Status

All four fixes confirmed (2026-07-05). **Not yet implemented — revisit in final drafting pass.**
Note: fix C should be implemented alongside the RAG proposal's credibility-scorer wiring, since
both consume the same currently-unused component.

---

## Improvement Proposal: Correlation Engine Consensus Scoring

### Why

`_compute_consensus` (`backend/app/ai/correlation/engine.py:630-799`) blends `scanner_signal`,
`ai_signal` (news forecast), and `ml_signal` via static weights
(`SCANNER_WEIGHT=0.30, AI_WEIGHT=0.40, ML_WEIGHT=0.30`, lines 51-53) into `consensus_score`. Three
issues were found, one of which directly compounds the root-cause bug documented above.

- **Missing-component handling doesn't account for `batch_pending`.** The `ai_available` gate
  (line 785) only checks `available` + `event_count > 0` — it does not check whether `ai_signal`
  is still `batch_pending` (missing direction/rationale/sentiment_label, per the root-cause
  finding at the top of this document). When the news forecast hasn't landed yet, `ai_available`
  still evaluates True, the full 0.40 weight is retained, and `ai_conf` silently defaults to 0.0
  via `.get(..., 0.0)` — dragging the consensus score down as if AI genuinely found nothing,
  instead of excluding the component and renormalizing to scanner/ML (which the code already does
  correctly for genuine unavailability).
- **Artificial unanimity from forced direction alignment.** Direction agreement is a hard binary
  gate (lines 742-765): unanimous BUY/SELL is required, any mismatch → `DIRECTION_MISMATCH`, score
  forced to 0, suggestion rejected. But when `ai_score == 0` — which includes the `batch_pending`
  case above — direction is coerced to align with `scanner_dir` (line 715) rather than excluded
  from the vote. This manufactures artificial unanimity: a suggestion can be created as BUY purely
  because the pending AI signal was forced to agree, when the real forecast (landing moments
  later) might have said SELL. This is the scoring-side twin of the prompt-side root-cause bug.
- **No regime awareness in scoring.** `regime_type` stored on the suggestion
  (`correlation/engine.py:838-840`) is a post-hoc placeholder (`"bull_trending" if all_buy else
  "bear_trending"`, explicitly commented as such) — not real regime classification.
  `regime_detector.py` is never imported into `engine.py`; weights and thresholds
  (`CONSENSUS_HIGH=80`, `CONSENSUS_MEDIUM=60`) are static regardless of market regime.

### Decided Fixes

**A.** Fix `ai_available` to also check for `batch_pending`/missing-direction state — treat
pending as genuinely unavailable, renormalizing weight to scanner/ML instead of silently
zero-dragging the score.

**B.** Stop force-aligning `ai_score == 0` to `scanner_dir` when the zero is due to pending state
(vs. a genuinely computed neutral) — exclude pending AI from the unanimity vote entirely rather
than manufacturing agreement.

**C.** Wire real `regime_detector.py` output into consensus weighting (e.g. adjust
SCANNER/AI/ML weights by regime confidence) — replacing the current post-hoc placeholder label
with an actual input. Larger architectural change than A/B.

### Status

All three fixes confirmed (2026-07-05). **Not yet implemented — revisit in final drafting pass.**
Note: fixes A/B should land together with (or before) the demand-gated redesign above, since both
concern the same `batch_pending`/race-condition surface.

---

## Status

All items above: **confirmed via code review, not yet fixed.**
