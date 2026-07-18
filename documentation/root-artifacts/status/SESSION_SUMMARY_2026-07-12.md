# Session Summary — 2026-07-12

Two pieces of work in this session: (1) a full fix for the RAG news-relevance
bug documented in `NEWS_CONTEXT_RELEVANCE_GAP_FINDING.md`, and (2) a new
"Suggested Action" feature added to AI explanations. Both are implemented,
live-verified against real data, and currently **uncommitted**.

---

## 1. RAG Relevance Gate Fix

### Problem
The AI instrument-context explanation for `COMSYN` (an Indian industrial
packaging company) cited SK Hynix Nasdaq-debut news as "relevant context" —
zero actual company or industry relationship. Root cause (already documented
in `NEWS_CONTEXT_RELEVANCE_GAP_FINDING.md`): the retrieval query was bare
boilerplate with no company/sector signal, and there was no relevance floor
anywhere in the ranking chain — any nonzero candidate reached the LLM as
"factual basis," regardless of actual topical relevance.

### Fix
- **Three-tier candidate model** in `app/ai/rag/retriever.py`: `exact`
  (symbol-tagged docs, trusted outright — ingestion-time tagging already
  validated them), `sector` (new — docs tagged with a sector-peer symbol,
  previously never even queried for), `generic` (symbol IS NULL).
- **Relevance gate** (`_apply_relevance_gate`): `sector`/`generic` tier
  candidates must clear an absolute cosine-similarity floor
  (`RAG_MIN_GENERIC_COSINE_SIMILARITY`) before entering BM25/cosine ranking
  at all — dropped from the pool, not merely down-ranked.
- **New module** `app/ai/rag/sector_resolver.py` — reconciles
  `CompanyFundamentalsProfile.sector` (curated) vs. `sector_map.py` (static
  fallback) into one resolver, feeding the new sector tier.
- **Query enrichment**: both `explanation_worker.py` call sites now build
  queries from company name + sector instead of bare boilerplate.
- **Calibration**: threshold `0.68`, derived empirically via
  `scripts/calibrate_rag_relevance_floor.py` against the real corpus — not
  guessed. Worth noting for future work: the *first* automated calibration
  pass looked clean but was wrong — several "positive anchor" top matches
  turned out to be generic sector chatter that outscored the real, dedicated
  article for that company on pure cosine similarity, and a production
  safety cap (`_MAX_CANDIDATES=500`) was silently truncating the widened
  calibration window and hiding genuine matches. Both were caught by
  manually reading the cited content rather than trusting the score, and the
  script/methodology was corrected before finalizing the number.
- No feature flag — this ships as a direct correctness fix. Worst case
  (over-filtering) collapses into the already-correct "no news" state, so
  there's no new failure mode to gate.

### Verification
- 352+ relevant tests passing (19 new/extended retriever unit tests, 6
  sector-resolver integration tests, 2 prompt-collapse tests). All pre-shown
  test-suite failures are pre-existing and unrelated (httpx version drift,
  ML checkpoint API drift, etc.).
- Live-verified against the running dev backend: low-coverage symbols
  (RELIANCE outside its 24h window, HINDALCO, FMCGADD) correctly produced
  "no news context" instead of forced filler; a widened-window retrieval for
  RELIANCE correctly admitted only the 5 genuinely relevant articles out of
  500 candidates (99% rejection rate on the generic pool).

### Files changed
`app/ai/rag/retriever.py`, `app/ai/rag/sector_resolver.py` (new),
`app/ai/rag/pipeline.py`, `app/ai/intelligence/explanation_worker.py`,
`app/core/config.py`, `app/core/metrics.py`,
`scripts/calibrate_rag_relevance_floor.py` (new), plus new/extended tests.

---

## 2. Suggested Action Feature

### Request and the compliance tension
User asked for a new section in AI explanations stating what action to take
"to make profit," with a learning-phase/due-diligence disclaimer. Research
before implementing surfaced a real tension: this codebase's entire
compliance architecture is built around "Describe; do NOT advise" — every
system prompt states the platform is NOT a licensed financial advisor, an
offline eval harness has 20 adversarial tests that auto-fail any
"you should buy/sell" language, and SEBI's 2026 algo-trading framework
restricts actionable algorithmic signal advice to SEBI-registered Research
Analysts (which this platform is explicitly documented elsewhere as not
being). This was flagged explicitly to the user before building anything;
the user was informed of the risk and chose to proceed with real actionable
language anyway.

### Design
Given the informed decision to proceed, the feature was built to be as
responsible as the request allows:
- **Dedicated schema field** (`suggested_action: str | None` on
  `ExplanationOutput`), not a 6th markdown section — chosen specifically
  because this content needs genuinely different guardrail rules than the
  rest of the explanation (it must reference the system's own real
  entry/stop/target numbers, which the existing guardrail explicitly blocks
  elsewhere as "target price"/"price target" language).
- **Trade-signal path**: states the real entry/stop-loss/exit level from the
  already-computed `TradeSuggestion` row (`take_profit_1` newly added to the
  prompt), using "exit level"/"profit-booking level" phrasing to avoid the
  blocked phrase while still being concrete.
- **No-signal path** (per user's specific request): no real trade parameters
  exist here, so it states a conditional monitoring trigger instead — "if
  price moves ±X% within Y days, that reinforces/contradicts the lean, check
  back then" — grounded in a real fetched current price and a
  volatility-derived move threshold (same sqrt-of-time scaling convention
  already used in `price_target_service.py`), never fabricated.
- **New tailored guardrail** (`_apply_suggested_action_guardrails`): still
  blocks absolute guarantee/certainty language and ungrounded ₹/% figures,
  but deliberately does not apply the existing "target price" phrase-block.
  Never hard-fails — falls back to a fixed safe string if everything is
  stripped.
- **Distinct disclaimer** (`🧪` sentinel, materially different wording from
  the existing `⚠` regulatory disclaimer) — appended unconditionally, code-
  generated (not LLM-generated) so it can never be omitted.
- **New eval-harness category** (`suggested_action_safety`, 10 fixtures)
  that calls the real production guardrail function directly, so the gate
  has no daylight from what ships. Existing safety fixtures (a separate
  pipeline) were left untouched.
- **Frontend**: a visually distinct indigo/violet callout card in
  `AIExplanationPanel.tsx`, separate from the amber regulatory-disclaimer box.
- Migration `0054` adds the two backing DB columns (`trade_suggestions.
  llm_suggested_action`, `ai_instrument_context.suggested_action`).

### Verification
- 21 new unit tests, all passing (prompt-injection gating, move-threshold
  math, guardrails, disclaimer wording, flag-off regression). Full backend
  suite re-run clean (only pre-existing, unrelated failures).
- New eval category: 10/10 fixtures passing against the real guardrail code.
- Live-verified with the flag flipped on and real Gemini calls: the
  trade-signal path (ASIANHOTNR BUY) produced a clean, correctly-worded
  suggested action with zero guardrail interventions; the no-signal path
  (TCS, and CMRGREEN via a real UI-triggered generation) produced correctly
  grounded ±3% / 5-day monitoring triggers with no fabricated numbers.

### Rollout decision
Shipped behind `Settings.SUGGESTED_ACTION_ENABLED` (default `False` in code).
**The user decided on 2026-07-12 to keep this flag enabled permanently**,
set via `SUGGESTED_ACTION_ENABLED=true` in `backend/.env` — a deliberate,
informed choice made with full knowledge of the compliance discussion above,
not a temporary test setting. This is recorded in memory
(`project_suggested_action_feature.md`) so future sessions don't
second-guess or silently revert it.

### Files changed
`app/ai/intelligence/explanation_worker.py`,
`alembic/versions/0054_suggested_action.py` (new),
`app/models/trade_suggestions.py`, `app/ai/fusion/models.py`,
`app/schemas/trade_suggestions.py`, `app/api/v1/ai_stream.py`,
`app/core/config.py`, `frontend/src/components/AIExplanationPanel.tsx`,
`frontend/src/components/AnalysisCardsSection.tsx`,
`frontend/src/types/analysis.ts`, `frontend/src/types/trade_suggestions.ts`,
`eval/gold_set.jsonl`, `eval/run_eval.py`, plus new/extended tests.

---

## Current state

- Both features are code-complete, tested, and live-verified against the
  running local dev backend/frontend (started this session, still running).
- Nothing has been committed to git.
- Local dev servers: backend (`uvicorn --reload`, port 8000) and frontend
  (`next dev`, port 3000) are running against this uncommitted code; Docker
  containers for `api`/`worker`/`frontend` were stopped at the start of the
  session to free the ports, while infra containers (Postgres, Redis,
  Redpanda, Prometheus, Grafana) were left running.
- `SUGGESTED_ACTION_ENABLED=true` is set in `backend/.env` per the user's
  permanent-enable decision; the code default remains `False`.
