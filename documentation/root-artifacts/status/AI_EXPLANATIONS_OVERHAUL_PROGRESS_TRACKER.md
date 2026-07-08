# AI Explanations Overhaul — Progress Tracker

> Companion to `AI_EXPLANATIONS_FIX_PLAN.md` (the audited, implementation-ready plan) and
> `AI_EXPLANATIONS_BUGS_AND_IMPROVEMENTS.md` (the finding tracker).
> Created **2026-07-06** after the full plan audit (7 verification passes + 4 user decisions).
>
> **How to use:** every task has a checkbox and a Status cell. Statuses: `⬜ NOT STARTED` ·
> `🟡 IN PROGRESS` · `🔵 IN REVIEW/TESTING` · `✅ DONE` · `⛔ BLOCKED (note why)`.
> Update the workstream Status line + the dashboard when a WS changes state. Each WS also has
> a **Verification** block — a WS is not ✅ until its verification items pass.

---

## Dashboard

| WS | Title | Depends on | Status | Started | Completed |
|---|---|---|---|---|---|
| WS1 | Hygiene & dead code | — | ✅ DONE | 2026-07-06 | 2026-07-06 |
| WS2 | Ingestion & RAG correctness | WS1 | ✅ DONE | 2026-07-06 | 2026-07-06 |
| WS3 | Credibility wiring | WS1 (∥ WS2) | ✅ DONE | 2026-07-06 | 2026-07-06 |
| WS4 | Sentiment accuracy | WS1 (∥ WS2/3) | ✅ DONE | 2026-07-06 | 2026-07-06 |
| WS5 | Consensus & classifier | WS2–4 | ✅ DONE | 2026-07-06 | 2026-07-06 |
| WS6 | Forecast hardening + demand gating | WS5 | ✅ DONE | 2026-07-06 | 2026-07-06 |
| WS7 | On-demand explanations (centerpiece) | WS6 | ✅ DONE | 2026-07-06 | 2026-07-06 |
| WS8 | Delivery / SSE / frontend | WS7 | ✅ DONE | 2026-07-06 | 2026-07-06 |
| WS9 | Safety engine + observability | WS1 (∥-safe) | ✅ DONE | 2026-07-06 | 2026-07-06 |
| WS10 | C.B2 retraining feedback (LAST) | WS7 + eval gate | ✅ DONE | 2026-07-06 | 2026-07-06 |

**Feature flags:** `FORECAST_DEMAND_GATING` = 🔵 created, OFF (flip = rollout stage 2) · `EXPLANATION_ON_DEMAND` = 🔵 created, OFF (flip = rollout stage 3, with WS8) · `FEEDBACK_LLM_ASSESSMENT_ENABLED` = 🔵 created, OFF (flip = rollout stage 5, after offline gate)
**Migrations:** 0050 ✅ · 0051 ✅ (15,306 rows backfilled) · 0052 ✅ · 0053 ✅ — all applied 2026-07-06
**Rollout stage:** ALL CODE COMPLETE — stage 1 (worker restart + market-hours observation) is unblocked

---

## Binding decisions (user-confirmed 2026-07-06 — do not relitigate)

1. Demand explanation Gemini priority = **HIGH** (not CRITICAL).
2. `EXPLANATION_ON_DEMAND` **also gates the Watchlist Pre-Warming scheduler** (context jobs); it does **not** gate the bypass endpoint or worker retry/DLQ requeues.
3. WS9 **reuses existing `LOSS_LIMIT_THRESHOLD`** (no new key) + implements a **real `paused_signals` handler** + **kill-switch auto-deactivation/cooldown**.
4. WS4 keeps `analyze_sentiment_batch` alive **only for fallback events lacking an `AINLPResult`**; classified-path events reuse stored full-body scores.
5. (From planning) Proposal C.B2 last + eval-gated; Proposal A = on-demand generation with loading state; FinBERT permanently removed (statistical drift monitoring).

---

## WS1 — Hygiene & dead code

**Status: ✅ DONE (2026-07-06)** · Files: `.gitignore`, `models/ml_feedback.py`, migration 0050, `ml/training/feedback_loader.py`, `rag/retriever.py`, `correlation/engine.py` (+ `correlation/__init__.py` + 2 test files), `requirements.txt`, `scripts/scheduled_retrain.py`, `api/v1/admin_training.py`, `frontend/src/hooks/useTypewriter.ts`

- [x] `git rm --cached backend/.scheduled_retrain.lock` + `.gitignore` entry + rationale in `scheduled_retrain.py` module docstring + probe-TOCTOU documented in `_probe_lock` docstring
- [x] Migration **0050** written AND applied — constraint verified live: `CHECK ((attempt_count >= 1))`; model updated with ownership comment
- [x] `build_panel_weights` counts bundle-key hits directly (weight-1.0 matches no longer undercounted)
- [x] Dead `CircuitBreaker` deleted: class + construction + docstring claim + `correlation/__init__.py` export + 9-test `TestCircuitBreaker` unit class + permanently-skipped integration test asserting the never-integrated feature
- [x] `retriever.py` docstring → gemini-embedding-001 / 768-dim (with config pointers)
- [x] `transformers`/`tf-keras`/`sentencepiece` removed from requirements.txt with removal rationale; torch comment updated (no longer claims FinBERT); torch stays
- [x] `useTypewriter.ts`: bounded LRU cache (`TYPED_CACHE_MAX_ENTRIES=100`, delete-then-add recency refresh, oldest-evicted)

**Verification**
- [x] `tests/ml/training/test_feedback_loader_telemetry.py` — 4 tests green (dir + `__init__.py` created)
- [x] `useTypewriter.test.ts` — 8/8 green incl. 2 new (eviction past cap; LRU recency refresh)
- [x] Touched suites re-run: `tests/ml` + `tests/ai/correlation` → 76 passed, 1 pre-existing failure (`test_regression_e1` CheckpointManager signature drift — confirmed failing on clean tree, unrelated)
- [x] Import sanity green; `tsc --noEmit` clean on touched files; no stray `CircuitBreaker` refs (remaining hits are the separate live aiobreaker/signal_pipeline implementations)
- [x] Lock file untracked (`git ls-files` clean), gitignore rule verified via `git check-ignore`
- ⚠️ Operator note: the live venv still *has* transformers/tf-keras/sentencepiece installed (uninstall optional — nothing imports them); next fresh env build will simply omit them
- 🐛 Pre-existing failure logged for later: `tests/ml/test_regression_e1.py::TestKillResumeParityProxy::test_training_state_round_trips_correctly` (not WS1 scope)

---

## WS2 — Ingestion & RAG correctness

**Status: ✅ DONE (2026-07-06)** — code complete + tested + migration applied; live 7/7-feed verification pending worker restart (operator action, see note) · Files: `ai/ingestion/rss_fetcher.py`, `ai/rag/ingester.py`, `ai/rag/embedder.py`, `ai/intelligence/llm_client.py`, `ai/rag/retriever.py`, `ai/fusion/models.py`, `core/config.py`, migration 0051

- [x] Session-per-coroutine: `_ingest_one` opens its own session per feed inside the gather; each failed feed logged by name with full traceback (module + loop docstrings document the old shared-session failure mode)
- [x] `pg_insert(...).on_conflict_do_nothing()` multirow insert — cross-feed same-article races now drop the loser row instead of aborting the feed batch; `rowcount` returned as ingested count
- [x] feedparser gets `response.content` (bytes) + forwarded Content-Type header so HTTP charset participates in detection; bozo warnings now include the exception type (surfaces `CharacterEncodingOverride`)
- [x] Batched dedup: ONE `SELECT ... WHERE content_hash IN (...) OR normalized_hash IN (...)` per feed (2 round-trips/feed, was ~N+1)
- [x] URL scheme validation: `sanitize_source_url` allowlists http/https; rejected links log + fall back to the feed URL (never persisted)
- [x] Near-dup dedup: `normalize_for_hash` (casefold, NFC, byline strip, punctuation strip, whitespace collapse) + `normalized_hash` column; intra-batch layer keeps first occurrence. Migration **0051** applied: 15,306 rows backfilled — immediately surfaced **56 pre-existing near-dup groups** in the corpus
- [x] Quality floor: `RAG_MIN_CONTENT_CHARS=200` config; tag set at row construction (new rows — no JSONB mutation trap; `build_event_rows` extracted as a pure, unit-testable function)
- [x] Embedding count guards: inside `llm_client.embed`'s `_do` (retry-wrapped) + `embedder.embed_texts` per-batch check + `zip(..., strict=True)` in `_build_embed_rows` — failure leaves rows unembedded for anti-join retry, never mispaired
- [x] Symbol-query cap: `.limit(limit)` on the symbol-specific step + truncation warning; docstring updated from "always included regardless of count" to the capped strategy

**Verification**
- [x] 37 new tests green: `test_rss_fetcher.py` (28: normalization equivalences incl. NFC + syndicated-variant collision, URL allowlist incl. `javascript:`/`data:`/scheme-relative, intra-batch dedup, quality tag, ISO-8859-1 charset via bytes handoff, layered ingest with mocked session), `test_ingester_dedup.py` (5: strict-zip both directions, positional integrity), `test_embedder_guards.py` (4: count/dim mismatch, batch order)
- [x] Full `tests/ai` suite: 180 passed (5 pre-existing governance fixture errors confirmed on clean tree, unrelated)
- [x] Migration 0051 applied + verified: 15,306/15,306 backfilled, index present
- ⚠️ Live verification (7/7 feeds, zero swallowed errors) requires a **worker restart** to load the new code — operator action; watch one cycle's "RSS ingestion cycle complete" log afterwards
- 🐛 Pre-existing failures logged for later: 5 errors in `tests/ai/governance/test_unified_model_registry.py` (`UnifiedModelRegistry() takes no arguments` fixture drift)

---

## WS3 — Credibility wiring

**Status: ✅ DONE (2026-07-06)** · Files: `credibility_scorer.py` (rebuilt), `rss_fetcher.py`, `retriever.py`, `sentiment_analysis_service.py`, `fake_news_detector.py`, `api/v1/intelligence.py`

- [x] `AISourceCredibility` populated: feed registry now `(name, url, type)` 3-tuples; `_ingest_one` registers each feed per cycle via race-safe `get_or_create_source` (INSERT ON CONFLICT DO NOTHING + re-select); seeds exchange 80 / news 50. **Table pre-seeded live for all 7 feeds** (first rows it has ever had)
- [x] **Scale fix:** `_check_source_credibility` rescales 0–100 → 0–1 (clamped) with docstring explaining the ~50× dominance bug it prevents
- [x] **Identity-key fix:** feed display name documented as THE canonical key in credibility_scorer module docstring, detector docstrings, and the `DetectFakeNewsRequest.source` field description
- [x] `update_credibility`: `SELECT ... FOR UPDATE` row lock; unknown sources skipped (never auto-created — keeps arbitrary API strings out of the registry); invalidates the in-process score cache; wired to fake-news outcomes in `detect()` (cleared→confirmed, flagged→contradicted, suspected→neither; non-fatal on failure)
- [x] Retriever: new shared `load_credibility_scores` batch loader (one SELECT for misses, 900s in-process TTL cache, no negative caching) → dense per-source credibility rank → third RRF term at 0.5× weight **applied only to relevance-ranked docs** (authority refines, never rescues irrelevant docs); `low_confidence_source` docs get 0.5× final-score demotion (demoted, never dropped); lookup failure degrades to pure relevance
- [x] Sentiment: `credibility_to_weight` linear 0.4–1.0 map over live scores (50→0.7 ≡ old default, 80 exchange seed→0.88); same shared batch loader + cache; hardcoded 2-entry dict deleted
- [x] Cross-ref: requires `affected_symbols` && overlap when symbols exist; falls back to event_type-only for market-wide (symbol-less) events; **also excludes the classification from corroborating itself** (bonus bug found during implementation)
- [x] Word boundaries: explicit inflected-forms regexes (`\b(?:surges?|surged|...)\b`) — no stem+`\w*` so "mission" can never match "miss"

**Verification**
- [x] 26 new/updated tests green: `test_credibility_scorer.py` (13: FOR UPDATE emitted, conflict-safe insert + seeds, arithmetic, unknown-skip, cache invalidation/batching/no-negative-cache), `test_retriever_ranking.py` (9: tie-break by authority, no-rescue, relevance dominance, demotion, degradation), `test_sentiment_source_weights.py` (7), fake-news tests updated (old ones encoded the 0.95-in-a-0-100-column scale bug) + 4 new (overlap in SQL, self-exclusion, symbol-less fallback, word-boundary FPs + inflections)
- [x] Full `tests/ai` + `tests/services`: **210 passed** (4 pre-existing skips; governance dir still pre-broken, excluded)
- [x] Live: `ai_source_credibility` seeded and verified for all 7 feeds against the real DB
- [x] "XYZ Group" regulatory-article regression pinned by test (scores 0.9, not 0.3)
- ⚠️ Same operator note as WS2: worker restart required for the loop-side registration + new ranking to go live

---

## WS4 — Sentiment accuracy

**Status: ✅ DONE (2026-07-06)** · Files: `sentiment_analysis_service.py`, `nlp_engine.py`, `eval/run_eval.py`, new `workers/sentiment_drift_monitor.py`, `workers/registry.py`, `core/metrics.py`, `core/config.py`

- [x] **Stored-score reuse (the big win):** all three event queries now select `AINLPResult` sentiment columns — classified path via its existing inner join, raw-text/general paths via new LEFT JOINs so partially-processed events reuse too; `_ScoredEvent` carries `(event, stored)`; the batched headline LLM call fires **only for events with no stored score** (binding decision #4), with per-request reuse/analyzed counts logged. Duplicate-row guard keeps first per event id.
- [x] Label↔score sign validation: `model_validator(mode="after")` on BOTH `SentimentOutput` and `_SentimentBatchItem` — contradiction matrix honors the prompt's ±0.1 neutral tolerance (small-positive scores are NOT violations); coerces label from score sign, caps confidence at 0.5, increments `sentiment_sign_violations_total{path}`. Runs at Pydantic parse time → **always before the 24h per-article cache write**.
- [x] Batch auditability (E.A): `reasoning` restored to `_SentimentBatchItem` (supersedes old Option A) + prompt instruction to cite THAT article; `_reasoning_matches_content` token-overlap spot-check (True/False/None-unjudgeable) — zero overlap halves confidence + `sentiment_batch_misattribution_total`; reasoning included in cached result dicts for audit.
- [x] L1 TTL (E.D): `(payload, expires_at)` entries via `_l1_get`/`_l1_set`, `SENTIMENT_L1_TTL_SECS=900` config; expired entries evicted on read; L2-hit copy stored so tier mutation can't leak into cache.
- [x] Drift monitor (E.B): new `sentiment_drift_monitor.py` (rag_cleanup template — hourly tick, daily Redis-epoch gate); SQL-side GROUP BY per window; pure `build_window_stats` + `total_variation_distance`; publishes label-fraction/mean-score gauges per window + TVD gauge; warns at TVD>0.25 or mean-shift>0.30; `insufficient_data` below 50 rows/window. Registered as 19th supervised task (TASK_NAMES + interval 10800 + factory + docstring; registry tests updated 18→19).
- [x] Eval gate: `_MIN_CALIBRATION_POINTS=15` (was 2) — below floor scores 0.0 and fails loudly; frozen-reference semantics documented at the constant and the gate.

**Verification**
- [x] 61 new/updated tests green: `test_nlp_engine_validation.py` (24: contradiction matrix, coercion both models, cap-not-floor, misattribution incl. unjudgeable-short-article), `test_sentiment_aggregate.py` (9: batch called ONLY with unscored titles, zero-LLM-call all-stored path, row shaping, L1 expiry-on-read), `test_sentiment_drift_monitor.py` (8: weighted means, TVD properties), registry 16
- [x] Full touched suites: **278 passed** (tests/ai + tests/services + tests/workers; pre-broken governance + 2 supervisor timing tests excluded — both confirmed failing on clean HEAD via worktree)
- [x] All 6 new metrics defined in `core/metrics.py` house style
- ⚠️ Live call-rate drop + drift gauges appear after **worker restart** (same standing operator action as WS2/WS3)
- 🐛 Pre-existing failures logged: `test_supervisor.py::test_retries_with_back_off_on_crash` + `test_shutdown_during_back_off_exits_immediately` (timing-sensitive, fail on clean HEAD)

---

## WS5 — Consensus & classifier

**Status: ✅ DONE (2026-07-06)** · Files: `event_classifier.py`, migration 0052, `ai/fusion/models.py`, `correlation/engine.py`, `services/regime_service.py` (reuse), both correlation test files

- [x] Migration **0052** written AND applied; model column added; `_persist_classification` writes sentiment in BOTH branches. **Bonus root-cause found:** the Gemini path's result dict dropped `sentiment` even though the response schema computed it — fixed there + in the error-fallback dict, so all four classification paths (cache/heuristic/LLM/rule-based) now carry it. `_detect_sentiment` also got the word-boundary explicit-forms fix ("gain" in "again", "fall" in "shortfall" no longer fabricate direction — it's now load-bearing for trades)
- [x] Pathway-2 fallback direction from persisted `sentiment` (bullish→buy, bearish→sell, neutral/NULL→"neutral" direction → consensus hard-rejects via NEUTRAL_SIGNAL); `impact_score` never consulted for direction (docstring documents the old always-BUY bug incl. the fraud-probe-→-BUY case)
- [x] **F.A**: `LIVE_FORECAST_SOURCES = {"gemini_batch","demand"}`; `ai_available` requires live source AND explicit BUY/SELL direction; weight renormalizes to scanner/ML from the regime-conditioned base otherwise (no more zero-drag from `batch_pending`)
- [x] **F.B (single change with F.A)**: force-align deleted; unanimity vote over voting components only — pending/fallback/HOLD AI ABSTAINS (neither agrees nor vetoes); live conflicting forecast still vetoes
- [x] **F.C**: `RegimeService.get_instrument_regime` wired in (fetched after the unanimity gate so rejected candidates never pay the OHLCV read); real regime stored in `regime_type` (placeholder deleted); `REGIME_WEIGHT_OVERRIDES = {"high_volatility": (0.25, 0.30, 0.45)}`; failure → defaults + `"unknown"` + warning, never blocks consensus
- [x] Data check run: **`ai_active_strategies` is EMPTY** (zero rows, any label) — the strategy joins already match nothing today, so widening the label set changes nothing; strategy seeding is a separate concern (flagged, out of WS5 scope)

**Verification**
- [x] 9 new tests green: batch_pending abstains+renormalizes with exact 50/50 score math (**no-news pathway alive** — the load-bearing case) · genuine-HOLD abstains · live SELL still vetoes · high_volatility override with exact 0.25/0.30/0.45 math + regime_type stored · regime-failure degradation · bearish→sell / bullish→buy / neutral+NULL→no-direction pathway-2
- [x] All 15 existing unit tests updated to the live-forecast contract and green (24/24 unit; fixtures previously relied on the force-align bug)
- [x] **Repaired 5 pre-existing broken integration tests** (confirmed broken on clean HEAD): fixture stubbed `gather_event_signals` but the engine calls `gather_news_forecast` — now 7/7 green, run twice for idempotency
- [x] **Fixed integration-test DB pollution**: `test_concurrent_requests_isolated` commits synthetic suggestions to the shared DB and its own re-run then tripped the dedup guard; added idempotency preamble + post-test cleanup; 5 leftover fake suggestion rows deleted from the live DB (they were visible-to-UI artifacts)
- [x] Full sweep: **263 passed** (tests/ai + tests/services + correlation integration)
- ⚠️ Live suggestion-rate shift observation requires **worker restart** (standing operator action, now covering WS2–WS5)

---

## WS6 — Forecast hardening + demand gating

**Status: ✅ DONE (2026-07-06)** · Files: new `services/demand_registry.py`, `fusion/signal_assembler.py`, `fusion/forecast_batch_worker.py`, `core/config.py`, `core/metrics.py`, 3 test files (1 new unit, 1 new gating, kafka suite extended)

- [x] Demand registry (~120 lines, no worker loop): `load_in_demand_symbols` = `watchlist_items` ∪ active `trade_suggestions`, carrying **ALL identity forms** (instrument_key + trading_symbol + suggestion symbol — Pathway 1 passes keys, Pathway 2 passes tickers, so a single-form set would silently never match one pathway); lazily rebuilt Redis SET + freshness key (`DEMAND_SYMBOLS_TTL_SECS=60`, set TTL padded 3× to cover rebuild races); **fails OPEN** on any Redis/DB error — infrastructure blips degrade to pre-gating behavior, never starve watched symbols
- [x] Enqueue gating behind `FORECAST_DEMAND_GATING=False` at strictly the enqueue point — cache hits always served (cost nothing); gated → `_fallback("demand_gated")` which F.A renormalizes away; `forecast_enqueue_gated_total` + `llm_news_forecasts_total{demand_gated}` counters
- [x] Dedup TTL: `FORECAST_ENQUEUE_DEDUP_TTL_SECS=3600` (was hardcoded 600s), aligned with the consumer's `_STALE_AFTER_SECS` — kills the ~10-min republish churn under demand-driven dispatch
- [x] Under-drain fix: `DrainResult(items, raw_count, stale_dropped)` NamedTuple; flush continues through all-stale reads (`raw_count > 0`) and breaks only on a genuine broker stall; response gains `stale_dropped`; `forecast_stale_dropped_total` metric; both drain call sites (idle loop + flush) adapted

**Verification**
- [x] `test_demand_registry.py` — 10 tests green (identity-form union, lazy rebuild, empty-set freshness arming, fail-open on Redis AND DB errors)
- [x] `test_forecast_demand_gating.py` — 5 tests green (gated skip, in-demand enqueue, **flag-off = zero behavior change** incl. no demand lookups, cache-hit-even-when-gated, configured dedup TTL)
- [x] `test_flush_drains_past_stale_run` in BOTH forms: scripted-fake unit test (exact commit counts) AND **broker-backed kafka integration test against live Redpanda** (10 stale + 5 fresh → one dispatch: 5 dispatched, 10 dropped, queue fully drained). Existing `integration` marker reused — no new marker needed under strict-markers
- [x] **Live check against real data**: 12 in-demand symbols loaded from the actual watchlist/suggestions; Redis set built; membership hit (instrument-key form) and miss both correct
- [x] Full sweep: **298 passed** (tests/ai + tests/services + tests/integration/kafka)
- ⏭ Republish-churn observation + flag flip = rollout stage 2 (after worker restart deploys WS2–WS6)

---

## WS7 — On-demand explanation generation (CENTERPIECE)

**Status: ✅ DONE (2026-07-06)** — code complete + component-tested; end-to-end flag-on verification = rollout stage 3 (with WS8) · Files: `correlation/engine.py`, `api/v1/ai_stream.py`, `intelligence/explanation_worker.py`, new `intelligence/explanation_service.py`, new `fusion/forecast_cache.py`, `fusion/signal_assembler.py`, `workers/watchlist_context_scheduler.py`, `services/regime_service.py`, `core/config.py`, `core/metrics.py`

Trigger side:
- [x] Engine auto-publish flag-gated (`EXPLANATION_ON_DEMAND` skips it; legacy consensus-gated path = rollback lever)
- [x] Watchlist Pre-Warming scheduler flag-gated at `_run_batch` entry (binding decision #2) with `skipped_on_demand` run counter — one env var owns ALL background LLM spend
- [x] NOT gated: bypass endpoint, worker retry/requeue, DLQ/quota-reset requeue (verified untouched)
- [x] `explanation_service.ensure_explanation()` state machine (ready/generating/weak_signal/failed): demand in-flight lock `SET NX EX 150` (own constant, distinct from the 60s bypass debounce); first viewer publishes, concurrent viewers dedupe; **fails open on Redis errors** (worker idempotency absorbs a duplicate; never strand the user); enqueue failure frees the lock + returns terminal `failed`. SSE Stage 1 delegates (REST inherits via the shared 3-stage lookup); bypass reuses `publish_explanation_job(trigger="bypass")`

Worker side:
- [x] Demand context: full article bodies via `_format_demand_context` (top `EXPLANATION_MAX_ARTICLES=3` × `EXPLANATION_ARTICLE_MAX_CHARS=4000`, standard `[Source: ...]` headers — retriever chunks already carry full `raw_content`) + `RegimeService.load_ohlcv` promoted public → `_summarize_price_action` (trend/swings/realized-vol/last-5-bars, ~6 lines, honest under-5-bar fallback); enrichment failures degrade to the legacy path, never fail harder
- [x] One structured Gemini call, priority **HIGH** (user decision — request-manager convention), `DemandExplanationOutput = ExplanationOutput + news_forecast{direction, sentiment_label, confidence, rationale}`, max_tokens 1600
- [x] Backpressure terminal for demand: `GeminiRateLimitError` → DLQ + failed push (legacy keeps republish); quota → DLQ (both modes); permit timeout surfaces via the 120s call ceiling → fail-fast retry policy
- [x] Post-success same transaction: LLM columns + `ai_signal` write-back (`forecast_source:"demand"`, direction/sentiment_label/rationale/confidence/refreshed_at — the dead `sentiment_label` key finally has a producer) + forecast-cache `setex` via the **shared `forecast_cache.forecast_cache_key`** (extracted; assembler delegates — keys can never fork) with pathway-aware cache symbol; best-effort with warning
- [x] Demand retry: `_DEMAND_MAX_ATTEMPTS=2` / 5s (legacy keeps 3/60s); `trigger` travels in the payload (additive, old workers ignore it); final failure → DLQ → `{"status":"failed"}` push + demand-lock release; `explanation_demand_latency_seconds` observed on success

Prompt/content fixes:
- [x] System prompts composed from shared blocks (`_PROMPT_STRUCTURE_BLOCK` + `_PROMPT_RULES_BLOCK` + `_SHARED_GUIDANCE_NEWS`) + per-mode task/guidance — the ~95% duplication is gone; guardrail pass now uses `model_copy` so subclass fields survive
- [x] Explicit "Technical scanner data unavailable — do not infer…" line when scanner is empty (§2)
- [x] Ungrounded-number guardrail (§1): %/RSI tokens in output must appear in the prompt (int/decimal variants matched); violating sentences stripped per-line preserving markdown; `ungrounded_number_filter` guardrail event + `explanation_ungrounded_numbers_total`; applied to BOTH suggestion and context paths
- [x] `prediction_generated_at` stamped on every prediction refresh in ai_stream; `_build_context_prompt` renders "(Snapshot as of <t>)" (§3)

Contract:
- [x] Payloads gain `status` (ready/generating/failed/weak_signal) + populated `suggestion_id`; worker SSE payloads carry them; old event-store entries backfilled via `setdefault` at read time; `generated_at` documented as the explanation timestamp for WS8's anti-downgrade guard (no redundant `created_at` key — deliberate)

**Verification**
- [x] 28 new tests green: `test_explanation_service.py` (12: both modes, first-viewer-publishes, lock contention, weak-signal-on-demand-still-generates, fail-open on Redis, fail-terminal on broker) + `test_explanation_prompts.py` (16: prompt composition, scanner line, price-action math, demand caps, grounding matrix incl. header preservation + variant matching, subclass survival, snapshot-age rendering)
- [x] Broker-backed kafka explanation flows green (5, incl. trigger="auto" default propagation); **full sweep 342 passed**
- ⏭ Flag-on end-to-end (generating → ready ~5-20s, `ai_signal.forecast_source=="demand"`, kill-worker → failed+retry, quota → failed within timeout) = **rollout stage 3**, lands together with WS8's frontend status handling

---

## WS8 — Delivery / SSE / frontend

**Status: ✅ DONE (2026-07-06)** · Files: `api/v1/ai_stream.py`, `AnalysisCardsSection.tsx`, `AIExplanationPanel.tsx`, new `lib/sanitizeUrl.ts`, `types/analysis.ts`, 4 test files

Backend:
- [x] Shared `_should_apply_explanation` guard on the poll path AND **both** push branches (suggestion + context) **and the inline failed-state push**. Rules: identical-noop, skeleton-never-downgrades, weak-signal-never-downgrades, **older-suggestion-never-overwrites-newer** (ordered by `signal_generated_at` — catches the delayed-retry clobber even though the stale job finishes generating LAST), failed-for-other-suggestion-never-clobbers, and **context-never-replaces-suggestion** (mirrors Stage-1 precedence — a gap found during implementation)
- [x] `direction_mismatch` computed in `_StreamState.render()` at every emit via `_direction_mismatch` (the ONE authoritative comparison; HOLD is not a contradiction; only delivered suggestion explanations flag)
- [x] Per-component degradation: `_emit_error` renamed event to `analysis_error` with `{component, message, stale}`; last good data retained + re-broadcast; explanation refresher now passes `error_component` (was the only one missing); all four state slices stamped with `updated_at`
- [x] Module + endpoint docstrings corrected: no Next.js header-converting proxy exists (token goes in the query string directly, connect-time validation only, reconnects present a fresh token via the ref); `analysis_error` documented; false "browser auto-reconnects" claim replaced
- [x] WS7 failed-state push payload gains `status`/`suggestion_id` for the guard

Frontend:
- [x] Token-ref reconnect (useWebSocket pattern): `connect` deps exclude the token; lifecycle keyed on `instrumentKey` only — **the ~29-min full wipe/reconnect is dead**; any later reconnect presents the fresh token
- [x] Exponential backoff 5s→10s→20s→40s→60s cap, **never gives up**, reset on `open`
- [x] Per-component liveness replaces the shared boolean: connected flips on `open` (quiet stream no longer double-fetches), `analysis_error` marks a component degraded (its query alone resumes polling), a genuinely fresher slice (`updated_at` past the degradation moment) clears it, 180s event-silence horizon with a 30s freshness tick, and each of the four `refetchInterval`s gates on `isComponentLive(component)`
- [x] `invalidateQueries` churn fix: keyed on the explanation's `generated_at` identity, fires once per new explanation
- [x] UI: amber inline "Live data delayed for X — showing last received data" strip (no toasts); red `direction_mismatch` banner ALONGSIDE the age banner; **staleness banner now actually age-based** (>1h, was presence-based); `PanelFailed` gains a retry CTA wired through the bypass endpoint (parent passes the handler for failed as well as weak_signal); generating → skeleton → typewriter flow confirmed intact
- [x] `sanitizeUrl.ts` — http/https allowlist via `new URL().protocol` (allowlist, never blocklist; relative/malformed → null); `SourcesList` renders plain text when rejected

**Verification**
- [x] 21 new frontend tests green (41 total in the 4 suites): `AnalysisCardsSection.test.tsx` (7: token refresh survives without reconnect/wipe + fresh-token-on-reconnect, exact backoff ladder + never-gives-up + reset-on-open, instrument-change reset, degraded mark/clear semantics, silence horizon, connected-on-open) · panel additions (6: `javascript:` URL as text, https as link, mismatch banner on/off, age-based staleness fresh/old, retry button with/without handler) · `sanitizeUrl.test.ts` (8 incl. case/whitespace tricks, data:/vbscript:/blob:)
- [x] Backend `test_ai_stream_guards.py` — 19 tests green incl. the delayed-retry clobber case and both direction-mismatch matrices
- [x] Full backend sweep **368 passed** (5 pre-existing `test_trade_suggestions_api.py` auth-fixture failures confirmed on clean HEAD, unrelated); `tsc` clean on all touched source files
- ⏭ Manual token-expiry + full flag-on E2E = rollout stage 3
- 🐛 Pre-existing failures logged: `test_trade_suggestions_api.py` (5, `'test-user-id'` int-parse in auth fixture)

---

## WS9 — Safety engine + observability (parallel-safe after WS1)

**Status: ✅ DONE (2026-07-06)** · Files: `safety/safety_trigger_engine.py` (rebuilt), `fusion/signal_pipeline.py`, `core/metrics.py`, `backend/grafana/cortex-ai-dashboard.json`, new safety test suite

- [x] **All three metrics live** (every one was a hardcoded 0 — the loop could literally never fire): `signal_rate` = suggestions created in the last hour; `volatility` = high-vol regime prevalence **as a ratio vs its own 30-day baseline** (an absolute fraction could never cross the 3.0× multiplier threshold — the ratio design makes the existing config semantics actually coherent; near-zero baselines floored at 5%; no detections → no signal, never a spike); `loss_pct` = today's realized paper-trading loss ÷ total portfolio value (latest EOD snapshot per portfolio), **consuming the existing `LOSS_LIMIT_THRESHOLD`** — no new config key
- [x] New `session_loss_limit` trigger → global kill switch; audit values stored as PERCENTAGES (the `Numeric(10,2)` columns would crush a 0.054 fraction to 0.05)
- [x] **`paused_signals` has real teeth**: activates a `signal_pause` kill-switch type with a 30-min auto-expiry (volatility spikes are transient; self-heals even if the engine dies mid-episode); `signal_pipeline` now checks it right after the global switch — was a DB-row-only no-op
- [x] **Anti-latch auto-release**: after `RECOVERY_CLEAR_CHECKS=10` consecutive calm checks (~5 min), switches activated by the engine are deactivated; scoped strictly to `activated_by == "safety_trigger_engine"` — **manual kill switches are never auto-released**
- [x] **Anti-spam cooldown**: a sustained breach writes at most one trigger row per type per 5 min (the protective switch stays active throughout)
- [x] 3 new metrics: `safety_metric_value{metric}` gauge (all three live inputs, published every cycle), `safety_triggers_total{trigger_type}`, `safety_kill_switch_auto_releases_total{switch_type}` (WS4/6/7 metrics were added with their workstreams)
- [x] **7 Grafana panels appended** to `cortex-ai-dashboard.json` (v2, JSON-validated): demand latency p50/p95 · content guardrails (ungrounded/sign/misattribution) · forecast gating + stale drops · sentiment drift TVD + means · 7d label mix · live safety metrics · triggers & auto-releases

**Verification**
- [x] 11 new tests green: calm-fires-nothing, each threshold → its real action (incl. `signal_pause` with expiry kwargs and percent-stored loss values), cooldown suppression, release-exactly-at-N, breach-resets-streak, **manual-switch-never-touched**, volatility ratio math incl. no-data and baseline-floor cases
- [x] Full backend sweep **372 passed**
- ⏭ Live `/metrics` visibility + p95 < 20s panel = rollout stages 1/4 (after worker restart)

---

## WS10 — C.B2 structured retraining feedback (LAST, eval-gated)

**Status: ✅ DONE (2026-07-06)** — code complete + gate script live-verified; flag flip = rollout stage 5 · Files: `explanation_worker.py`, migration 0053, `models/trade_suggestions.py`, `ml/training/feedback_loader.py`, `core/config.py`, new `scripts/compare_feedback_assessment.py`, test suite

- [x] `MLAssessmentOutput` added to the demand schema (`likely_missed_pattern` 7-value enum · `confidence_should_have_been_lower` · `price_action_agreement`) with **coercion validators** (out-of-vocabulary values → safe defaults, never a hard failure — the narrative rides in the same structured response); prompt instructs judging ONLY from the Recent Price Action numbers, with explicit no-price-action defaults
- [x] Write-back carries **judge identity**: `{model, prompt_version (_ASSESSMENT_PROMPT_VERSION=1), assessed_at}` stored with every verdict — judge changes make verdicts non-comparable, and the gate script detects mixed generations
- [x] Migration **0053** written AND applied (column verified `jsonb` in live DB)
- [x] `_assessment_factor`: contradicts+wrong-direction ×1.3, overconfident+hard-negative ×1.2 (same conf≥0.70 definition as `_confidence_factor`), both stack, clip unchanged 0.1–5.0; **fires only on confirmed misses — never punishes winners**; NULL/malformed → 1.0. `compute_sample_weight(row, use_llm_assessment=False)` — the off path never reads the column; `build_feedback_weights_df` reads the flag from settings with an explicit override for the gate script; `_FEEDBACK_QUERY` selects `ts.llm_ml_assessment` via its existing LEFT JOIN
- [x] `FEEDBACK_LLM_ASSESSMENT_ENABLED = False` created with the full flip-preconditions documented at the key
- [x] **Offline comparison gate** (`scripts/compare_feedback_assessment.py`): builds off/on bundles from one identical window; reports coverage %, judge-identity mix (⚠ on mixed generations), verdict counts, per-row weight deltas; **verifies the flag-off path bit-identical to an assessment-blind computation** (exit 1 on violation)

**Verification**
- [x] 14 new tests green: full factor matrix (both conditions, stacking, winners-never-punished, sub-threshold overconfidence, None-direction), **flag-off bit-identical three ways** (default / explicit False / column absent), flag-on multiply+clip, DataFrame-apply parity, enum coercion never raises
- [x] **Gate script run against the live DB**: 34 matured outcomes, 0 assessed (expected — assessments accumulate only after `EXPLANATION_ON_DEMAND` goes live), 0 weight deltas, ✔ invariant verified on real rows
- [x] Final full sweep: **399 passed**
- ⏭ Flag flip (rollout stage 5) preconditions: assessments accumulated post-stage-3 → re-run gate (coverage + single judge generation) → off-vs-on retrain → full `run_eval.py` pass → non-regression review
- ⏭ Re-run the gate on any judge model/prompt-version bump (identity stored per verdict)

---

## Rollout stages

| Stage | Action | Gate to advance | Status |
|---|---|---|---|
| 1 | Land WS1–WS6, both flags False | Full backend pytest + one bare-metal market-hours session: 7/7 feeds clean, credibility populating, sentiment call-rate drop, suggestion-rate shift understood | ⬜ |
| 2 | Flip `FORECAST_DEMAND_GATING=True` | Gemini RPD drops; watched symbols still get real forecasts; unwatched renormalize cleanly | ⬜ |
| 3 | Land WS7+WS8 together; flip `EXPLANATION_ON_DEMAND=True` locally, then prod | Full local checklist in WS7/WS8 verification incl. context-card on-demand path + backpressure failure + token-expiry survival + XSS fixture | ⬜ |
| 4 | WS9 Grafana live throughout | `explanation_demand_latency_seconds` p95 < 20s | ⬜ |
| 5 | WS10, flag off → offline comparison → flip | Non-regression + eval pass + coverage report | ⬜ |

**Expected behavioral shifts (not bugs):** suggestion counts change after F.B (no more manufactured unanimity; no-news pathway preserved via vote exclusion) · every opened suggestion gets an explanation in on-demand mode · watchlist context cards generate on first view (~5–20s, typewriter masks) instead of pre-warming.

## Session log

| Date | Session focus | WS touched | Notes |
|---|---|---|---|
| 2026-07-06 | Plan audit + tracker created | — | 7 verification passes; 4 decisions locked; plan amended; implementation not started |
| 2026-07-06 | WS1 implemented + verified | WS1 ✅ | Migration 0050 applied live; 88 tests green (4 new backend + 2 new frontend); 1 pre-existing unrelated failure documented; graphify updated |
| 2026-07-06 | WS2 implemented + verified | WS2 ✅ | Migration 0051 applied (15,306 backfilled, 56 near-dup groups found); 37 new tests, 180 in tests/ai green; worker restart pending for live 7/7-feed check |
| 2026-07-06 | WS3 implemented + verified | WS3 ✅ | Credibility scorer rebuilt + wired into fake-news/sentiment/RAG; table seeded live (7 feeds); 26 new/updated tests, 210 green; self-corroboration bonus bug fixed |
| 2026-07-06 | WS4 implemented + verified | WS4 ✅ | Stored-score reuse (duplicate Gemini call class eliminated), sign validation, misattribution check, L1 TTL, drift monitor as 19th task, eval gate ≥15; 61 new tests, 278 green |
| 2026-07-06 | WS5 implemented + verified | WS5 ✅ | Migration 0052 applied; sentiment persisted (+ Gemini-path drop found); F.A/F.B abstain semantics + F.C regime weights; 9 new tests; 5 pre-broken integration tests repaired + DB pollution fixed; 263 green |
| 2026-07-06 | WS6 implemented + verified | WS6 ✅ | Demand registry (all identity forms, fail-open) + gating flag OFF + dedup TTL 3600 + DrainResult under-drain fix; 15 new unit + broker-backed stale-run test on live Redpanda; live 12-symbol demand-set check; 298 green |
| 2026-07-06 | WS7 implemented + verified | WS7 ✅ | On-demand centerpiece: explanation_service state machine, both flag gates, demand schema + ai_signal/forecast-cache write-back via shared key builder, fail-fast demand retries, prompt dedupe + grounding guardrail; 28 new tests; 342 green |
| 2026-07-06 | WS8 implemented + verified | WS8 ✅ | Anti-downgrade guard on all 3 write paths + direction_mismatch + analysis_error/per-component liveness; token-ref reconnect kills the 29-min wipe; backoff-forever; sanitizeUrl; 40 new tests (19 backend guard + 21 frontend); 368+41 green |
| 2026-07-06 | WS9 implemented + verified | WS9 ✅ | Safety engine live: 3 real metrics (ratio-based volatility), loss trigger, real signal_pause + pipeline check, anti-latch auto-release, anti-spam cooldown; 3 metrics + 7 Grafana panels; 11 new tests; 372 green |
| 2026-07-06 | WS10 implemented + verified — **ALL 10 WORKSTREAMS COMPLETE** | WS10 ✅ | ml_assessment schema + judge identity, migration 0053 applied, flag-gated assessment factor (bit-identical off), comparison gate script live-verified on real DB; 14 new tests; 399 green |
