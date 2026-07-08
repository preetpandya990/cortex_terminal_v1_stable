# AI Explanations Overhaul — Integrated Fix Plan

> Recovered 2026-07-06 from the plan-mode approval submitted 2026-07-05 18:17 IST
> (session `10455a1f-1d86-4ea5-b910-9ff99f24a7d1`) — approved into plan mode but never
> flushed to a file before the session ended. Status: **not yet implemented.**
>
> **Audited 2026-07-06** against the actual codebase (7 parallel verification passes + web
> best-practice checks): all factual errors corrected inline, ~15 gaps folded in, and 4 open
> decisions resolved with the user (Gemini priority = HIGH; pre-warming gated under
> `EXPLANATION_ON_DEMAND`; reuse `LOSS_LIMIT_THRESHOLD` + real `paused_signals` handler;
> fallback events batch-analyzed only when unscored). Working tree is clean (no uncommitted
> collisions with plan targets); migration slots 0050–0053 confirmed free.

## Context

`AI_EXPLANATIONS_BUGS_AND_IMPROVEMENTS.md` records ~40 verified bugs/gaps and 6 agreed proposals (A–F) in the Gemini AI-explanation feature: explanations fire before ML/news forecasts land (root cause §0), the landed forecast is never written back to `TradeSuggestion.ai_signal`, consensus manufactures direction agreement from pending/fabricated signals, delivery has silent-staleness and anti-downgrade gaps plus an XSS surface, and the ingestion/RAG/sentiment stack has data-integrity bugs (shared AsyncSession, embedding mispairing, orphaned credibility scorer, headline-only re-analysis burning duplicate Gemini calls).

This plan fixes **everything** in one dependency-ordered effort. Binding decisions already made with the user:
- One integrated plan; **Proposal C.B2 last**, eval-gated before it touches real training.
- **Proposal A = on-demand explanation generation**: first view shows a loading state (~5–20s), result cached; typewriter reveal masks latency.
- **FinBERT is permanently removed** — no FinBERT drift detection; drift monitoring is statistical.

## Global architecture decisions

**D1 — Proposal A resolution (consensus vs demand):**
1. *Suggestion-time consensus never waits for Gemini.* Background forecast path stays, but a `batch_pending`/fallback `ai_signal` is treated as **unavailable** (F.A): 40% AI weight renormalizes to scanner/ML; pending AI is excluded from the unanimity vote (F.B — no force-align). Blocking the 30s scan loop on 5–22s Gemini calls is not viable, and (3) makes creation-time forecasts non-load-bearing for what users read.
2. *Background forecasts become demand-scoped*: enqueue only for symbols in the in-demand set (watchlist ∪ active suggestions). `forecast_batch_worker` + `cortex.forecast.batch` stay, at demand volume.
3. *Explanations regenerate fresh at first view*: engine stops auto-publishing explanation jobs (flag-gated). Demand job → worker synchronously retrieves full article text (RAG → `AIRawEvent.raw_content`), builds an OHLCV price-action summary (C.A), makes **one** structured Gemini call returning narrative + news-forecast fields, then writes back `TradeSuggestion.ai_signal` and the forecast cache. Closes §0 propagation, dead `sentiment_label`, and §5 semantic mismatch in one write. **No separate landed-forecast backfill pipeline is built** (moot).

**D2 — FinBERT:** remove `transformers`, `tf-keras`, `sentencepiece` from `backend/requirements.txt` (verified FinBERT-only; sole import `backend/poc/finbert_sentiment_poc.py`; `nlp_engine.py` mentions FinBERT in comments only; `run_eval.py` reads fixture values, never imports it). **`torch` stays** — used by `ml/training/trainer.py`, `timeframe_trainer.py`, `tuner.py`, `train_all_timeframes.py`, `ml/models/multi_output_model.py`, `ml/inference/onnx_converter.py`, `scripts/bench_tft_cpu.py`, `scripts/preflight_check.py` (audit: broader than originally listed). `run_eval.py` fixture `finbert_score` values kept as frozen static reference anchors; gate hardened to require **≥15 fixtures** (currently accepts 2). Audit correction: `gold_set.jsonl` actually has **115 records, 20 of them sentiment-calibration (SC001–SC020)** — not 15 — so the ≥15 floor is satisfiable with headroom.

**D3 — Dead `CircuitBreaker` (`correlation/engine.py:79-120,162-166`): delete honestly.** The one real external dependency (Gemini) already has a live breaker (`signal_assembler._forecast_breaker:361`).

**D4 — `safety_trigger_engine.py`: wire minimal real metrics, don't delete.** Loop is live (`backend/app/worker.py:41`, `workers/registry.py:176`, supervised as `safety_monitoring: 90` in `TASK_EXPECTED_INTERVAL_SECONDS`). `signal_rate` = suggestions/hour (`trade_suggestions.created_at`); `volatility` = high-volatility regime fraction from `AIRegimeDetection`; implement the missing `loss_pct` branch from paper-trading realized P&L (`PaperPnlSnapshot.total_realized_pnl` / `PaperTradeOutcome.net_pnl`). **Reuse the existing, currently-unread `LOSS_LIMIT_THRESHOLD=0.05` (config.py:642)** — do NOT add a new `SAFETY_MAX_SESSION_LOSS_PCT` key (audit finding: the config already exists, only the consumer is missing). Two further gaps the original plan missed (user-confirmed scope):
- **`paused_signals` is currently a no-op** — only the `kill_switch_activated` action has a real effect (`signal_pipeline.py:175-178` checks the global kill switch). WS9 must implement a real handler for `paused_signals` (pipeline checks it the same way the kill switch is checked), otherwise the volatility trigger records a DB row and does nothing.
- **No auto-deactivation** — with real metrics wired, one spurious spike would latch the global kill switch until manual reset. Add an auto-deactivation/cooldown path (condition clear for N consecutive checks → deactivate, with logging), so the safety engine can't flap or stick off.

## Execution order

```
WS1 hygiene → {WS2 ingestion, WS3 credibility, WS4 sentiment (parallelizable)}
→ WS5 consensus/classifier → WS6 forecast demand-gating
→ WS7 on-demand explanations (centerpiece) → WS8 delivery/frontend
→ WS9 safety+observability (parallel-safe after WS1) → WS10 C.B2 (last)
```

---

## WS1 — Hygiene & dead code (zero-risk)

Files: `.gitignore`, `backend/app/models/ml_feedback.py`, migration 0050, `backend/app/ml/training/feedback_loader.py`, `backend/app/ai/rag/retriever.py`, `backend/app/ai/correlation/engine.py`, `backend/requirements.txt`, `frontend/src/hooks/useTypewriter.ts`.

- `git rm --cached backend/.scheduled_retrain.lock`; add to `.gitignore`; note in `scripts/scheduled_retrain.py` header why (mutable runtime state doesn't belong in git — permanently-dirty tree trains operators to ignore real changes; and the lock's correctness rests on the held `fcntl.flock`, inode-scoped). **Audit correction:** the real concurrency caveat is that `admin_training._probe_lock` (:371-409) releases the lock before returning — a PASS is a point-in-time snapshot (TOCTOU), so the held `LOCK_EX` in `scheduled_retrain.py:173-183` remains the actual guard, not the probe. Document that in the probe's docstring while touching this area (no inode-swap scenario exists — nothing unlinks/recreates the file).
- Migration **0050_ml_feedback_attempt_count_uncap**: `CheckConstraint("attempt_count BETWEEN 1 AND 5")` → `attempt_count >= 1` (ceiling belongs to `core/retry.py:_MAX_ATTEMPTS=3`, which is confirmed to govern `compute_ml_feedback_with_retry`; note table is `ml_feedback_errors`). Update model `:48-53`.
- `build_panel_weights:562-574` — matched-count from actual bundle key hits, not `weights != 1.0` (a genuine match can legitimately weigh exactly 1.0: `direction_only`/`unknown` outcome × neutral confidence factor — and WS10's extra factor makes exact-1.0 matches more common, so this must land before WS10).
- Delete `CircuitBreaker` + construction + docstring claim (D3).
- `retriever.py:22` docstring → `gemini-embedding-001`, 768-dim.
- Remove FinBERT-only packages (D2).
- `useTypewriter.ts:11` — bounded insertion-ordered cache (cap ~100, evict oldest) replacing the unbounded module `Set`.

Tests: eviction tests in `useTypewriter.test.ts`; greenfield `tests/ml/training/test_feedback_loader_telemetry.py`.

## WS2 — Ingestion & RAG correctness

Files: `backend/app/ai/ingestion/rss_fetcher.py`, `backend/app/ai/rag/ingester.py`, `backend/app/ai/rag/embedder.py`, `backend/app/ai/intelligence/llm_client.py`, `backend/app/ai/rag/retriever.py`, migration 0051.

- **Session-per-coroutine**: each `ingest_feed` opens its own session from the factory; `rss_ingestion_loop:169-177` stops sharing one session across the 7-feed gather. Log each swallowed exception with feed name. (Audit notes: the cited `watchlist_context_scheduler.py:273` / `forecast_batch_worker.py:562` patterns are session-per-*operation*, used sequentially — sound to copy, but ours is the first concurrent fan-out, per SQLAlchemy 2.x official guidance: one `AsyncSession` per asyncio task via a shared `async_sessionmaker`. The concrete failure today is worse than "sharing": each feed's `db.commit()` at :139 commits sibling coroutines' pending adds mid-flight, plus asyncpg "another operation is in progress" errors.)
- **MANDATORY companion fix — `ON CONFLICT DO NOTHING` on raw-event insert.** `AIRawEvent.content_hash` is `unique=True`, and today's plain `db.add()+commit()` only survives cross-feed same-cycle duplicates because the shared session serializes the dedup SELECTs. Splitting sessions un-serializes them: two feeds carrying the same syndicated article would both SELECT-miss, both INSERT, and the loser's whole-feed batch rolls back on `IntegrityError` (silent data loss under `return_exceptions=True`). Use `pg_insert(...).on_conflict_do_nothing()` (same pattern as `rag/ingester.py:135`) — the session split must not ship without it.
- **feedparser gets bytes**: `response.content` not `response.text` (`:55-59`) — feedparser does its own charset detection (BOM, XML declaration) only when given bytes. Pass the HTTP `Content-Type` charset hint via feedparser's headers support where available, and log `bozo`/`CharacterEncodingOverride` for observability.
- **Batched dedup**: one `content_hash IN (...)` query per feed replacing the per-item SELECT loop (`:114-122`).
- **URL scheme validation at ingest** (`:127`): allow only `http`/`https`; else persist empty + log.
- **Near-dup dedup (D.B)**: `normalize_for_hash(title, summary)` (lowercase, NFC, collapse whitespace, strip byline prefixes/punctuation). Migration **0051_raw_events_normalized_hash**: `ai_raw_events.normalized_hash VARCHAR(64)` indexed non-unique + backfill. Dedup checks both hashes.
- **Quality floor — tag not reject (D.A)**: `len(raw_content) < RAG_MIN_CONTENT_CHARS` (new, 200) → `extra_data["low_confidence_source"]=True`; consumed by WS3 ranking. **Audit gap: SQLAlchemy does not track in-place JSONB mutation** — `extra_data` maps to the `metadata` JSONB column (`models.py:56`); set the flag by assigning a new dict (`event.extra_data = {**(event.extra_data or {}), "low_confidence_source": True}`) or switch the column to `MutableDict.as_mutable(JSONB)`, otherwise the write silently never persists.
- **Embedding count guards**: assert `len(resp.embeddings) == len(texts)` in `llm_client.embed` (copy `embed_batch_job:730-751` pattern); same count check in `embedder.embed_texts:85-96`; `ingester._build_embed_rows:94` → `zip(..., strict=True)`. Failure leaves rows unembedded → anti-join retries (no corruption).
- **Symbol-query cap**: `retriever._load_candidates:239-249` symbol-specific step gets `.order_by(recency desc).limit(_MAX_CANDIDATES)` + truncation warning.

Tests (greenfield): `tests/ai/ingestion/test_rss_fetcher.py`, `tests/ai/rag/test_ingester_dedup.py`, `test_embedder_guards.py`.

## WS3 — Credibility wiring (D.C + E.C + fake-news)

Files: `credibility_scorer.py`, `rss_fetcher.py`, `retriever.py`, `sentiment_analysis_service.py`, `fake_news_detector.py`.

- **Populate `AISourceCredibility`**: `ingest_feed` calls `get_or_create_source` per feed per cycle; seed defaults by source type (exchange/filing 80, general news 50).
- **Audit gap — scale mismatch (live bug once populated):** `credibility_score` is stored 0–100 (`Numeric(5,2)`, default 50.0) but `fake_news_detector._check_source_credibility:182` returns it raw into a weighted sum expecting 0–1 — populating the table without rescaling would let credibility (~50.0) dominate `final_score`. Normalize to 0–1 at the consumer (`score / 100`) in the same change that populates the table.
- **Audit gap — source-identity key mismatch:** credibility rows will be keyed by feed display name (e.g. "Economic Times Markets"), but `detect(..., source)` passes a URL into `_check_source_credibility`'s `source_name ==` lookup, and sentiment `_SOURCE_WEIGHTS` also keys by display name. Standardize on ONE identity key (feed display name; derive it from the URL/feed registry at the detector call site) or the three consumers will never join.
- Fix unlocked read-modify-write in `update_credibility:47-77` with `.with_for_update()`; wire it to fake-news detection outcomes (so `_check_source_credibility:161-185` finally reads real scores).
- **Retriever**: credibility as a third RRF term (batch score lookup, 900s in-process cache, ~0.5× weight of BM25/cosine); `low_confidence_source` tag rank-demotes.
- **Sentiment source weights (E.C)**: replace 2-entry `_SOURCE_WEIGHTS:59-63` with credibility 0–100 → weight 0.4–1.0 linear map, default 0.7 for unknown.
- **Fake-news cross-ref (`:187-229`)**: corroboration requires `affected_symbols` array overlap, not event_type alone.
- **Word boundaries (`:246-261`)**: `\b`-anchored regex replacing raw substring checks ("up" in "group" etc.).

Tests: `test_credibility_scorer.py` (concurrent update, no lost write), extend `test_fake_news_detector.py`, `tests/ai/rag/test_retriever_ranking.py`.

## WS4 — Sentiment accuracy (E minus FinBERT)

Files: `sentiment_analysis_service.py`, `nlp_engine.py`, `eval/run_eval.py`, new small `workers/sentiment_drift_monitor.py`, `core/config.py`.

- **Reuse full-body `AINLPResult` scores** (the big win): the classified-events path (`_query_classified_events:322-351`) inner-joins through `AINLPResult`, so every event on that path already carries a stored full-article `sentiment_score/label/confidence`. **Audit corrections to scope:** (1) the query currently does `select(AIRawEvent)` only — the AINLPResult columns must be **added to the projection**, this is not just deleting the re-analysis; (2) the raw-text fallback path (`_query_raw_text_events`) returns events with NO `AINLPResult` row — **decision (user-confirmed): keep `analyze_sentiment_batch` alive solely for those unscored fallback events**; reuse stored scores everywhere they exist. Still eliminates the duplicate-Gemini-call class for the common path and upgrades the trading-facing signal from headline-only to full-body.
- **Label↔score sign validation**: Pydantic `model_validator` on `SentimentOutput` + `_SentimentBatchItem`; on violation coerce label from score sign, cap confidence 0.5, counter `sentiment_sign_violations_total`.
- **Batch auditability (E.A)**: add short `reasoning` to `_SentimentBatchItem`; heuristic misattribution spot-check (≥1 content token from the title must appear in reasoning) — halve confidence + counter on failure. No second LLM call (right-sized).
- **L1 TTL (E.D)**: `(value, expires_at)` entries in the class LRU, `SENTIMENT_L1_TTL_SECS=900`. **Audit gap — there are THREE cache layers, not two:** L1 in-process LRU (500 entries, no TTL), L2 Redis aggregate (`_L2_TTL=900`, service line 56/414), **plus a per-article Redis cache inside `analyze_sentiment_batch` itself (`_SENTIMENT_CACHE_TTL_SECS=86_400`, nlp_engine.py:71,448)**. The 24h per-article layer is intentional (articles don't change) and stays — but note it means a mis-scored article persists 24h; the sign-validation fix above must run BEFORE the cache write so violations aren't cached.
- **Drift monitor (E.B redesign)**: daily task — 7-day rolling label distribution + mean score vs 30-day baseline from `ai_nlp_results`; Prometheus gauges; warn thresholds. No FinBERT.
- **Eval gate**: require ≥15 fixture points; document frozen-reference semantics.

Tests: `test_nlp_engine_validation.py`, `tests/services/test_sentiment_aggregate.py`.

## WS5 — Consensus & classifier fixes (F.A/F.B/F.C, pathway-2 direction)

Files: `event_classifier.py`, migration 0052, `correlation/engine.py`, reuse `services/regime_service.py`.

- Migration **0052_event_classification_sentiment**: `ai_event_classifications.sentiment VARCHAR(10)` (bullish/bearish/neutral). `_persist_classification:457-495` writes it (both constructor calls) — the classifier already computes it, it was just never persisted.
- **Pathway-2 fallback** (`_resolve_scanner_signal_for_symbol:564-573`): direction from persisted `sentiment` (bullish→buy, bearish→sell); neutral/NULL → `available=False`, no synthetic direction. Never derive direction from unsigned `impact_score` again (was effectively hardcoded "buy" — could manufacture BUY suggestions off bearish news).
- **F.A** (`ai_available:785`): any `_fallback(...)` shape = unavailable → weight renormalizes to scanner/ML. **Audit note on shapes:** actual `forecast_source` values are `"gemini_batch"` (healthy, only shape carrying `direction`), `"fallback"` (with `fallback_reason` ∈ {`circuit_breaker_open`, `batch_pending`}), `"stale_data"`, `"no_news"`. Gate = "unavailable unless `forecast_source == 'gemini_batch'` AND `direction` present" (or WS7's `"demand"` source) — don't enumerate fallback reasons.
- **F.B** (`:715`): delete `ai_dir = scanner_dir` force-align; unanimity vote only over components with genuine direction. **Audit finding — the vote-exclusion half is MANDATORY, not a refinement:** the force-align is currently the *only* mechanism letting scanner+ML agreement pass the unanimity gate (:742-765) when AI has no events. Removing it without excluding unavailable AI from the vote turns every no-news scanner+ML consensus into `DIRECTION_MISMATCH` and silently kills the no-news suggestion pathway. Both halves land in one change with a test asserting the no-news pathway still produces suggestions.
- **F.C**: `get_instrument_regime` wired into `_compute_consensus`; real regime stored in `regime_type:838-840` (replacing placeholder); static 2-entry weight-override map (`high_volatility → scanner .25 / ai .30 / ml .45`); lookup failure → defaults + warning. No learned weighting. **Audit notes:** (1) `get_instrument_regime(db, instrument_key, trading_symbol, company_name)` (regime_service.py:469-485) needs trading_symbol + company_name — both are on the suggestion/instrument row; (2) all `regime_type` consumers treat it as a freeform string with `or "unknown"` fallbacks (verified: suggestion_compliance, market_context, explanation_worker, price_target_service, outcome_service) — safe to widen the label set — **except** `signal_assembler.py:1199-1209` and `outcome_service.py:380` use `regime_type` as a lookup key against `AIActiveStrategy.regime_type`; run a data check that the strategy table has rows for the new labels (`high_volatility`, `sideways_range`, `low_liquidity`) or those joins silently miss.

Tests: extend correlation tests — pending-AI renormalization, **no-news scanner+ML pathway still creates suggestions after F.B**, bearish-event pathway-2 direction, regime override.

## WS6 — Forecast pipeline hardening + demand gating

Files: new `backend/app/services/demand_registry.py`, `signal_assembler.py`, `forecast_batch_worker.py`, `core/config.py`.

- **Demand registry** (~80 lines, no new worker loop): `load_in_demand_symbols(db)` = watchlist ∪ active suggestions; `is_in_demand(redis, db, symbol)` backed by Redis SET `cortex:demand:symbols` + freshness key, lazily rebuilt every `DEMAND_SYMBOLS_TTL_SECS=60`. **Audit corrections:** the watchlist half copies `SELECT DISTINCT instrument_key FROM watchlist_items` (`watchlist_context_scheduler.py:274-276` — table is **`watchlist_items`**, not `watchlist`); the scheduler does NOT query suggestions, so the second half is new: `trade_suggestions WHERE status = 'active'` (confirmed valid status value; CHECK allows active/expired/executed/invalidated/superseded).
- **Enqueue gating** (`_enqueue_for_batch_forecast:419-468`) behind `FORECAST_DEMAND_GATING` (default False; **must not enable before WS5 ships** — gated symbols rely on F.A renormalization): not in demand → skip enqueue, return `_fallback("demand_gated")`.
- **Audit correction — `FORECAST_AUTO_DISPATCH` defaults to `True` in code** (config.py:487), not False; the live demand-driven behavior comes from the deployment env override. WS6 must not assume demand-only drain is the shipped default: the gating logic and the under-drain fix below must be correct under BOTH auto-dispatch and demand-dispatch configurations.
- **Dedup TTL**: hardcoded `EX 600` (`:437-439`) → `FORECAST_ENQUEUE_DEDUP_TTL_SECS=3600` (= `_STALE_AFTER_SECS`), killing the ~10-min republish churn under demand-driven config.
- **Under-drain fix**: `drain():127-149` returns `DrainResult(items, raw_count)`; `flush_pending_forecasts` (audit: actual span is `:309-381`, break at `:359-361`) continues while `raw_count > 0` (all-stale batches keep draining) and breaks only on `raw_count == 0 and position >= end_snapshot`; response gains `stale_dropped` count. **Audit note:** the post-Redpanda code already commits past stale records before breaking (defers fresh items rather than losing them — this is NOT the old LPOP-loss class), so the fix's value is correctness of the continue-vs-break decision and honest `dispatched/stale_dropped` reporting, not data recovery.

Tests: `tests/services/test_demand_registry.py`; kafka-integration `test_flush_drains_past_stale_run` (10 stale + 5 fresh → one flush dispatches 5, reports 10 dropped).

## WS7 — On-demand explanation generation (Proposal A + C.A + §1/§2/§3/§5) — centerpiece

Files: `correlation/engine.py`, `api/v1/ai_stream.py`, `intelligence/explanation_worker.py`, new `intelligence/explanation_service.py`, reuse `rag/retriever.py` + `regime_service.py` (promote `_load_ohlcv` → public `load_ohlcv`), `core/config.py`.

**Trigger side:**
- Engine auto-publish `:1067-1097` wrapped in `if not settings.EXPLANATION_ON_DEMAND:` (legacy path = rollback lever; the `EXPLANATION_CONSENSUS_THRESHOLD` gate — config-driven, default 75.0, config.py:317-318 — stays meaningful only in legacy mode; in on-demand mode, demand *is* the gate).
- **Audit finding — flag scope must be explicit; there are FOUR producers publishing to `cortex.explanation.jobs` plus one adjacent spend path.** The flag gates: **(a)** engine auto-publish (above), and — user-confirmed decision — **(b)** the Watchlist Pre-Warming scheduler (`watchlist_context_scheduler.py:329-330`, which publishes *context* jobs to the separate `cortex.context.jobs` topic; wrap its publish loop in the same `if not settings.EXPLANATION_ON_DEMAND:` so on-demand mode fully owns Gemini spend — first view of a context card then goes through the same generating→ready flow). The flag must NOT gate: **(c)** the user bypass endpoint (`ai_stream.py:1173` — it IS the demand path), and **(d)** worker retry/requeue + DLQ/quota-reset requeue (`explanation_worker.py:1580,1592,1935,2078,2093` — gating these breaks retry semantics for jobs already in flight).
- New `explanation_service.ensure_explanation(db, redis, suggestion) -> dict`: explanation present & not superseded → `{"status":"ready",...}`; else acquire an in-flight lock key, publish `{suggestion_id, id, instrument_key, trigger:"demand"}` to `EXPLANATION_JOBS`, return `{"status":"generating",...}`. SSE Stage 1 (`:309-492`), REST `/explanation` (`:940-1055`), and bypass `POST /explanation/{id}/request` (`:1066-1203`) all delegate to it (dedupes the bypass logic). **Audit correction:** the existing bypass debounce key is `SET NX EX 60` (`_BYPASS_DEBOUNCE_TTL_SECS=60`, ai_stream.py:1062), not 150s — the demand in-flight lock gets its own constant, TTL 150s (covers worst-case generation + retry), deleted on completion/failure like the bypass key is on error paths.

**Worker side (demand jobs):**
- `_gather_demand_context(db, suggestion)`: full article text via `retriever.retrieve` → `AIRawEvent.raw_content`, capped `EXPLANATION_MAX_ARTICLES=3` × `EXPLANATION_ARTICLE_MAX_CHARS=4000`, with source headers for `[Source ...]` citations; **price-action summary (C.A)** from `RegimeService.load_ohlcv(db, instrument_key, limit=60)` — trend, swing high/low, volatility, last-5-bar behavior (~6 text lines, no raw bar dump); regime rendered into prompt. (Audit: `_load_ohlcv` at regime_service.py:331-348 is already a `@staticmethod` querying `upstox_ohlcv` 1D bars, returning oldest-first `{ts,open,high,low,close,volume}` dicts — promotion to public is a rename; a `_load_ohlcv_batch:351-393` multi-instrument variant also exists if batching is ever needed.)
- **One structured Gemini call** (house pattern: `generate_structured_with_usage`, same as the existing worker call at `:887-893`), schema `{summary, full_explanation, news_forecast:{direction, sentiment_label, confidence, rationale}}`, priority **`HIGH`** (user-confirmed decision — the request manager's documented convention assigns HIGH to user-facing trade-suggestion explanations and reserves CRITICAL for health/startup probes; HIGH is already never budget-throttled, so latency is unaffected).
- **Audit gap — backpressure is a real user-facing failure mode the demand path must handle:** `request_manager.acquire()` can raise `GeminiRateLimitError` (queue full at `GEMINI_MAX_QUEUE_DEPTH`) or `GeminiQuotaExhausted` (circuits open), and the blocking wait is bounded by `GEMINI_PERMIT_TIMEOUT`. Map all three to the `{"status":"failed"}` push (frontend shows `PanelFailed` + retry) rather than letting them count as retryable transient errors — a user staring at a skeleton must get a terminal answer within the permit timeout.
- **Post-success, same transaction**: existing LLM-column write (`llm_summary`/`llm_explanation`, `:974-975`) **plus** `TradeSuggestion.ai_signal = {**existing, direction, sentiment_label, rationale, confidence, forecast_source:"demand", refreshed_at}` **plus** `redis.setex` into the forecast cache (next consensus cycle reuses it). **Audit gap — the cache-key builder is private:** `_forecast_cache_key` lives inside `signal_assembler` (`:381-399`, digest over symbol + event-ids + rounded indicators). Extract it into a shared module-level `forecast_cache_key(symbol, event_ids, indicators)` helper that both the assembler and the demand path import — a reimplementation would produce non-colliding keys and the cache write would be silently dead. The demand path reconstructs the same inputs (events it retrieved + current indicator snapshot). Note: background `gemini_batch` results still won't carry `sentiment_label` (`_build_result:527-545` omits it) — that's acceptable; the prompt's sentiment line simply stays absent for background-sourced forecasts, and F.A treats both sources uniformly via the `direction`-present check.
- **Demand retry policy**: 2 attempts / 5s delay (legacy jobs keep 3/60s); final failure publishes `{"status":"failed"}` → frontend `PanelFailed` + retry button (bypass endpoint).

**Prompt/content fixes folded in:**
- Dedupe the ~95%-duplicated system prompts (`:242-341`) into `_BASE_SYSTEM_PROMPT` + two mode suffixes.
- `_render_scanner:485-498`: explicit "Technical scanner data unavailable — do not infer technical readings." line when empty.
- **Numeric grounding check (§1)**: post-generation, every `%`/`RSI n` token in `full_explanation` must appear in the prompt; violating sentences stripped (same mechanism as `_strip_price_predictions`) + counter. Deliberately limited to %/RSI (full numeric NLI = over-engineering).
- **Context-job staleness (§3)**: `prediction_generated_at` added to `prediction_data` (`ai_stream.py:461-464`), rendered "as of <t>" in prompt. ≤60s staleness acceptable; documented.

**Frontend contract**: explanation payloads gain `status: "ready"|"generating"|"failed"`, `suggestion_id`, `created_at` — additive optional keys per existing SSE convention (`_build_explanation_payload:219-251`, `frontend/src/types/analysis.ts`). **Audit notes:** `suggestion_id?` already exists in the TS type (`analysis.ts:277`) and on the weak-signal payload (`:294`) — the gap is that `_build_explanation_payload` and `_build_context_payload` never populate it, so this is a backend-population change, not a type change. The suggestion's `created_at` is currently exposed as `signal_generated_at` (`:247-250`); the new `created_at` field means the *explanation's* generation time — name and document both to avoid conflation. The explanation-job consumer parses payloads via `msg.value` + `.get(...)` guards, so the added `trigger` key is additive-safe (verified).

Tests: `test_explanation_service.py` (state machine, lock contention), `test_explanation_worker_demand.py` (ai_signal write-back, cache write, failed path), `test_prompt_building.py` (scanner line, price-action block, grounding strip), kafka-integration demand-job round trip.

## WS8 — Delivery/SSE/frontend (§4 + Proposal B)

Files: `api/v1/ai_stream.py`, `AnalysisCardsSection.tsx`, `AIExplanationPanel.tsx`, new `frontend/src/lib/sanitizeUrl.ts`, `frontend/src/types/analysis.ts`. (`AuthContext.tsx` unchanged — fix lives in the consumer.)

Backend:
- **Anti-downgrade on push**: refactor `_should_apply_polled_explanation:187-214` into shared `_should_apply_explanation(current, incoming)` comparing `suggestion_id`/`created_at` (now in payloads per WS7); apply before **BOTH** unconditional writes — the suggestion push branch (`state.explanation = payload` at `:780`) **and the context push branch (`:822`)**, which the original plan missed; a late context push can clobber a richer suggestion explanation just as easily.
- **`direction_mismatch` (Proposal B)**: computed in `_emit_update` (prediction direction vs explanation `signal_direction`), added to payload. One authoritative comparison point. **Audit correction:** the frontend's existing `showStalenessBanner:387-388` is *presence*-based (`context_type === 'suggestion_explanation' && !!signal_generated_at`) — its docstring describes a timestamp comparison that was never implemented, so today it fires for every suggestion explanation. Fix the banner condition in the same change (age-based from `signal_generated_at`), with the mismatch banner layered on top.
- **Per-component errors**: `_refresh_prediction` catches `Exception` not just `ValueError` (`:627`); pattern/sentiment refreshers get try/except; explanation refresher spawn (`:901-903`) gets `error_component`; refresh failure emits `analysis_error {component, message, stale:true}` while retaining last data; payloads gain per-component `updated_at`.
- **SSE auth (documented deviation, no change this pass)**: the EventSource token rides in the query string (`AnalysisCardsSection.tsx:79` → `ai_stream.py:531`) and is validated **only at connect** — the stream can outlive token expiry, and query tokens can land in access logs. Acceptable for a read-only analysis stream, and it's exactly why the token-ref fix works (fresh token used on any future reconnect; no live reauth possible or needed on SSE, unlike the WS in-band reauth at `useWebSocket.ts:440-446`). Record it in the module docstring — also fix the docstring's false claim (`ai_stream.py:10-14`) that a Next.js proxy converts an Authorization header.

Frontend:
- **Token-ref reconnect** (copy `useWebSocket.ts:227-238,436-446` pattern): `tokenRef`, `accessToken` out of `connect` deps, mount effect keyed on `instrumentKey` only. Kills the ~29-min full wipe/reconnect.
- **Retry backoff**: exponential 5s→60s cap, never permanently gives up (replaces `SSE_MAX_RETRIES=3`); reset on `open`.
- **Per-component connectivity**: `lastEventAt` per component replaces the shared `sseConnected` boolean; each React Query `refetchInterval` gates on its own component's freshness. Wire the no-op `error` listener (`:105-107`). **Audit additions:** connectivity should also flip on the SSE `open`/init event, not only on the first `analysis_update` (`:98`) — today a connected-but-quiet stream leaves polling running in parallel (double-fetch). And audit the `invalidateQueries(['trade-suggestions'])` call on every available explanation (`:166-170`) while reworking these effects — combined with reconnects it's redundant refetch churn.
- **Staleness/error UI**: per-card inline "live data delayed" indicator (consistent with `PanelFailed` styling; no toasts).
- **Mismatch banner**: `direction_mismatch` → "⚠ Signal has changed since this explanation" alongside the age banner.
- **Generating skeleton**: `status==="generating"` → shimmer; ready → typewriter; failed → `PanelFailed` + retry.
- **`sanitizeUrl.ts`**: allow only `http:`/`https:`; `SourcesList` renders plain text when rejected (pairs with WS2 ingest-side validation).

Tests: greenfield `AnalysisCardsSection.test.tsx` (token refresh survives, backoff, per-component gating), extend `AIExplanationPanel.test.tsx` (banner, skeleton, `javascript:` URL as text), `sanitizeUrl.test.ts`; backend greenfield `tests/api/v1/test_ai_stream_guards.py`.

## WS9 — Safety engine + observability

Files: `safety/safety_trigger_engine.py`, `core/config.py`, Grafana dashboard JSON.

Per D4 (as amended): real `signal_rate`/`volatility` queries + new `loss_pct` branch consuming the **existing `LOSS_LIMIT_THRESHOLD=0.05`** (config.py:642 — present but never read; we add the missing consumer, not a new key) + a **real `paused_signals` handler** (signal pipeline checks it alongside the kill switch — today only `kill_switch_activated` has any effect) + **auto-deactivation/cooldown** so a metric spike can't latch the global kill switch until manual reset.

New Prometheus metrics: `explanation_demand_latency_seconds`, `explanation_ungrounded_numbers_total`, `sentiment_sign_violations_total`, `sentiment_batch_misattribution_total`, `forecast_enqueue_gated_total`, `forecast_stale_dropped_total`, sentiment drift gauges — all as module-level `Counter`/`Histogram`/`Gauge` in `backend/app/core/metrics.py` (verified house pattern, exported via `/metrics` in main.py; names verified conformant with Prometheus conventions). Grafana JSONs live at `backend/grafana/` + `monitoring/grafana/`. **Audit note:** the explanation worker is a standalone Kafka consumer, NOT in the supervised task registry (`TASK_EXPECTED_INTERVAL_SECONDS`) — demand-latency alerting is genuinely new coverage, and none of the WS2/WS6/WS7 cadence changes affect existing freshness ratios (verified: those heartbeats are poll-tick-driven, not throughput-driven).

## WS10 — C.B2 structured retraining feedback (LAST, eval-gated)

Files: `explanation_worker.py` (schema extension), migration 0053, `feedback_loader.py`, `core/config.py`, offline comparison script.

- WS7 schema gains `ml_assessment: {likely_missed_pattern: enum[none, trend_reversal, breakout_failure, volume_divergence, news_shock, range_bound_chop, other], confidence_should_have_been_lower: bool, price_action_agreement: enum[agrees, partial, contradicts]}` (near-zero marginal tokens — model already reasons over OHLCV).
- Migration **0053_trade_suggestions_llm_ml_assessment**: `trade_suggestions.llm_ml_assessment JSONB`. **Audit addition (LLM-as-judge best practice):** store the judge identity alongside the verdict — `{...assessment, model: "<gemini model id>", prompt_version: N}` — because judge-model version bumps shift score distributions and make weights from different versions non-comparable; the retrain comparison gate must be re-run on judge model changes, and weights should be recalibrated against outcome-based samples periodically.
- `compute_sample_weight` gains `assessment_factor` (contradicts+wrong-direction ×1.3; should-have-been-lower+hard-negative ×1.2; NULL→1.0; clip stays 0.1–5.0) **only when `FEEDBACK_LLM_ASSESSMENT_ENABLED=True`** (default False). (Requires WS1's matched-count telemetry fix first — the extra factor makes exact-1.0 weights more common, worsening the `weights != 1.0` undercount if unfixed.)
- **Gate**: offline retrain comparison (flag off vs on, same feedback window) + full `run_eval.py` pass + assessment-coverage % report (only viewed suggestions carry assessments). Flip flag only on non-regression.

Tests: `test_feedback_loader_assessment.py` (factor math, NULL passthrough, flag-off = bit-identical weights).

---

## Config inventory (new keys)

| Key | Default | WS |
|---|---|---|
| `RAG_MIN_CONTENT_CHARS` | 200 | 2 |
| `SENTIMENT_L1_TTL_SECS` | 900 | 4 |
| `DEMAND_SYMBOLS_TTL_SECS` | 60 | 6 |
| `FORECAST_DEMAND_GATING` | False → flip after WS5 verified | 6 |
| `FORECAST_ENQUEUE_DEDUP_TTL_SECS` | 3600 | 6 |
| `EXPLANATION_ON_DEMAND` | False → flip after WS7/8 verified | 7 |
| `EXPLANATION_MAX_ARTICLES` / `EXPLANATION_ARTICLE_MAX_CHARS` | 3 / 4000 | 7 |
| ~~`SAFETY_MAX_SESSION_LOSS_PCT`~~ — **dropped (audit)**: reuse existing `LOSS_LIMIT_THRESHOLD=0.05` (config.py:642, currently unread) | — | 9 |
| `FEEDBACK_LLM_ASSESSMENT_ENABLED` | False | 10 |

All keys verified absent from `core/config.py` (no collisions). Note `FORECAST_ENQUEUE_DEDUP_TTL_SECS` replaces a hardcoded `ex=600` at `signal_assembler.py:439`; the other hardcoded constants the plan touches are scattered (`_BYPASS_DEBOUNCE_TTL_SECS=60` ai_stream.py:1062, 7-day lookback ai_stream.py:149, `_STALE_AFTER_SECS=3600` forecast_batch_worker.py:97) — each is an edit at its own site.

## Migrations (after 0049)

0050 `ml_feedback_attempt_count_uncap` · 0051 `raw_events_normalized_hash` (+backfill) · 0052 `event_classification_sentiment` · 0053 `trade_suggestions_llm_ml_assessment`

## Kafka/Redis

No new topics. `cortex.explanation.jobs` payload gains optional `trigger` (verified additive-safe — consumer parses via `.get()`). New Redis: `cortex:demand:symbols` (+freshness key). Reused: in-flight key (new constant, 150s — the existing bypass debounce is 60s), SSE event store, ready channel (payload gains status/suggestion_id/created_at), forecast cache key (builder extracted from `signal_assembler` into a shared helper, now also written by demand path).

## Test infrastructure (audit findings — set up before writing tests)

- `backend/pytest.ini` is the effective config (a divergent `[tool.pytest.ini_options]` also exists in `pyproject.toml` — pytest.ini wins; keep edits in pytest.ini only).
- `--strict-markers` is on and **no kafka marker exists** — register `kafka_integration` in pytest.ini before any `@pytest.mark.kafka_integration` test lands, or the whole suite errors.
- `filterwarnings = error` — any new warning-emitting code fails tests; treat new deprecation warnings as build breaks.
- Directories: `backend/tests/ai/` exists; **`backend/tests/services/` and `backend/tests/ml/training/` must be created** (with `__init__.py` if the suite uses package-style collection).
- Frontend: vitest (`frontend/vitest.config.ts`). `AIExplanationPanel.test.tsx` exists at `frontend/src/components/__tests__/` → extend; `AnalysisCardsSection.test.tsx` → greenfield. The SSE hook (retry cap, token-refresh wipe, 5-tier explanation merge) currently has ZERO coverage — the WS8 token-ref fix is regression-prone, so its tests are on the critical path.

## Right-sizing decisions (deliberate deviations from the tracker)

1. No synchronous consensus wait on Gemini (D1) — demand-time regeneration makes it unnecessary.
2. No landed-forecast→ai_signal backfill pipeline — superseded; `dispatch_forecast` §3 finding moot.
3. Demand-tag "service" = lazily-refreshed 60s Redis cache, not a continuous process/DB trigger.
4. `CircuitBreaker` deleted, not wired; `safety_trigger_engine` wired minimally, not deleted — but "minimal" now includes a real `paused_signals` handler and auto-deactivation (audit found the action was a no-op and the kill switch latches), and reuses `LOSS_LIMIT_THRESHOLD` instead of a new key.
5. No FinBERT dual-write (binding) → statistical distribution monitor + hardened static gate.
6. Grounding check = %/RSI tokens only; misattribution check heuristic, not a second LLM call.
7. Regime weighting = static 2-entry override map, not learned.
8. `torch` stays in requirements (ML stack dependency); only transformers/tf-keras/sentencepiece removed.

## Rollout & verification

1. **Land WS1–WS6 with both flags False** — behavior-compatible bug fixes. Full backend pytest + one bare-metal market-hours session. Watch: 7/7 RSS feeds ingesting with zero swallowed errors, `ai_source_credibility` populating, sentiment Gemini call-rate drop (WS4), suggestion-rate change from F.B (expected — no more manufactured unanimity), forecast `pending_count`.
2. **Flip `FORECAST_DEMAND_GATING=True`**: Gemini RPD drops; watched symbols still get real forecasts (cache keys populate); unwatched symbols renormalize.
3. **Land WS7+WS8 together** (frontend needs the status contract). Flip `EXPLANATION_ON_DEMAND=True` locally: open a card with NULL `llm_explanation` → `generating` skeleton → ready push in ~5–20s → typewriter; verify `ai_signal.forecast_source=="demand"` + sentiment line renders; verify a context card (no suggestion) also goes generating → ready, since the flag now gates the pre-warming scheduler too; kill worker mid-generation → failed + retry works; simulate Gemini queue-full/quota-exhausted → failed status within `GEMINI_PERMIT_TIMEOUT`, not a hung skeleton; force short token expiry → cards survive refresh; `javascript:` source_url fixture renders as text. Then flip in prod — legacy auto-publish + pre-warming stay one env var away as rollback.
4. WS9 Grafana panels live throughout; `explanation_demand_latency_seconds` p95 target <20s.
5. **WS10 last**, flag off; enable only after offline retrain comparison shows non-regression.

Behavioral changes to expect (not bugs): suggestion counts will shift after F.B — direction depends on the vote-exclusion pairing: no-news scanner+ML consensus keeps working (excluded AI no longer votes), while news-bearing mismatches now reject honestly instead of being force-aligned; in on-demand mode every opened suggestion gets an explanation (consensus-75 gate applies only to legacy auto-publish) and watchlist context cards generate on first view instead of pre-warming (accepted trade-off, user-confirmed); first-view explanation latency ~5–20s (accepted trade-off).
