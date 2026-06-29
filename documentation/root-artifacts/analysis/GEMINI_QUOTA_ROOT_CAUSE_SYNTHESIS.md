# Gemini API Quota Exhaustion — Root Cause Synthesis

**Investigation period:** 2026-06-12 to 2026-06-26  
**Status:** Root cause identified. Fixes not yet implemented.

---

## Problem Statement

The Cortex system has been exhausting its Gemini API daily quota repeatedly across multiple sessions, preventing user-facing LLM features (AI explanations, trade suggestion analysis) from functioning for the remainder of each day. The exhaustion happens quickly — sometimes within 90 seconds of the daily quota resetting at midnight PT — which means the primary user-facing feature (the AI Explanation Panel) never gets served even after the quota restores.

The problem has persisted despite multiple rounds of fixes: a Redis cache on sentiment calls, a circuit breaker to fast-fail on quota 429s, a multi-key load balancer (5 keys), a priority-queue request manager, an event-classifier heuristic pre-filter, and a fake news detector cache. None of these resolved the exhaustion because they each addressed a symptom rather than the structural cause.

---

## System Architecture: Relevant Call Sites

All Gemini `generate_content` calls flow through `CortexIntelligenceClient` in `backend/app/ai/intelligence/llm_client.py`. There are four background processes that make generate calls:

| Caller | File | Priority | Trigger |
|---|---|---|---|
| `sentiment` | `nlp_engine.py` → `analyze_sentiment()` | MEDIUM | Every ingested news event |
| `news_forecast` | `news_forecaster.py` → `generate_news_forecast()` | MEDIUM | Every news event × affected symbol |
| `instrument_context` | `explanation_worker.py` → `_generate_instrument_context()` | MEDIUM | Every watchlist page open (if no recent suggestion) |
| `explanation` | `explanation_worker.py` → `_generate_explanation()` | HIGH | Trade suggestion generated |
| `event_classifier` (LLM path) | `event_classifier.py` → `_classify_with_ollama()` | LOW | ~5–10% of articles (post-heuristic-filter) |
| `fake_news` | `fake_news_detector.py` → `_llm_reasoning()` | MEDIUM (default) | Every article without a cached result |

The `embed` quota track (RAG pipeline) is completely separate — exhausting it does not affect `generate` callers.

---

## Findings

### Finding 1: Sentiment is the highest-volume caller by a large margin

Source of truth: `ai_llm_audit_log` table (records every Gemini call: invocation_type, model_provider, input/output tokens, latency_ms, error_message, output_preview).

**7-day breakdown (queried 2026-06-26):**

| Date | Sentiment | News Forecast | Instrument Context | Explanation | Total |
|---|---|---|---|---|---|
| 2026-06-24 | 221 | 26 | 41 | 0 | 288 |
| 2026-06-23 | 368 | 298 | 0 | 4 | 670 |
| 2026-06-22 | 475 | 72 | 2 | 0 | 549 |
| 2026-06-21 | 41 | 17 | 1 | 0 | 59 |
| 2026-06-20 | 76 | 14 | 1 | 0 | 91 |
| 2026-06-19 | 214 | 85 | 0 | 5 | 304 |
| 2026-06-18 | 363 | 275 | 0 | 2 | 640 |

**Across all 7 days: sentiment accounts for 1758 of 2601 total calls (67.6%). Explanation — the primary user-facing output — accounts for 11 calls (0.4%).**

Sentiment fires on every news event ingested, regardless of whether the user is active, whether any signals are in progress, or whether any suggestions have been generated. On the highest-volume days (2026-06-22, 302 events; 2026-06-23, 263 events), the quota was exhausted solely by background processing with minimal user interaction.

### Finding 2: The startup/resume burst burns quota before any user-facing call is served

Verified by querying per-minute call breakdown from `ai_llm_audit_log` immediately after the quota reset watcher fired at 10:06:xx UTC on 2026-06-26:

```
10:06:xx UTC  quota reset watcher fires — all 4 active key circuits cleared
10:06 UTC     sentiment=12  news_forecast=5  instrument_context=1  →  18 calls in <60s
10:07 UTC     sentiment=4   news_forecast=3                        →  7 calls in 60s
              ─────── all 4 keys hit daily quota again ───────────────────────────────
```

25 calls in ~90 seconds re-exhausted the daily quota across 4 keys. 0 explanations were generated. The priority queue (request manager) was serving MEDIUM-priority sentiment and instrument_context calls concurrently with MEDIUM-priority news_forecast. The HIGH-priority explanation never got a permit because the batch of MEDIUM calls already in flight consumed the remaining RPD budget before any explanation was requested.

The burst source: when the system resumes after a multi-hour sleep (WSL2 freezes the monotonic clock, so the quota reset watcher fires late on wake), a backlog of news events has accumulated. Every event in the backlog triggers a sentiment call. Every watchlist item with expired context triggers an instrument_context job. All of these fire simultaneously.

### Finding 3: The Redis sentiment cache does not protect against the startup burst

`analyze_sentiment()` checks `nlp:sentiment:{sha256(prompt)}` in Redis before calling the API (TTL: 3600s). This works well during normal running — the same article recycled within an hour returns a cache hit at zero API cost.

It does not protect against the startup burst because:
- Articles that arrived during a multi-hour sleep are genuinely new — the cache has never seen them.
- Articles seen before the sleep have TTL-expired entries (sleep > 1 hour).
- After a long resume, the cache is cold for all backlogged events.

Verified: the 2026-06-26 audit log burst shows 16 sentiment calls with 0 cache hits immediately after the circuit reset.

### Finding 4: Sentiment's output does not feed the signal or trade suggestion pipeline

The `analyze_sentiment()` result is stored in `ai_nlp_results` and consumed by two paths:

**Path A — Event classification (structural prerequisite, not numerical input):**  
`event_processor.py` → `nlp_engine.process_event()` writes `AINLPResult`. `event_classifier.classify()` uses `nlp_result_id` as a foreign key to create `AIEventClassification`. The `impact_score` on the classification (−100 to +100, weighted at 35–50% of the final fused consensus signal) is computed **independently** by the classifier's own LLM call or heuristic — not derived from the NLP sentiment score. `named_entities` from the NLP result pass to the classifier for symbol extraction, but the sentiment label and score do not affect the impact score.

**Path B — Sentiment card (display only):**  
`SentimentAnalysisService` in `ai_stream.py` refreshes every 120 seconds and calls `analyze_sentiment()` per-event for the SSE stream. This produces the AI Sentiment card in the frontend (±100 impact score, breakdown bar, top headline). If this fails, the card shows "Unable to load sentiment analysis." The other three analysis cards (ML Ensemble, Prediction Summary, AI Explanation) are completely unaffected.

**Conclusion on impact:** `analyze_sentiment()` is called on every news event for a structural FK relationship whose only downstream numerical output is the sentiment card — a purely informational display. No trade suggestion, position, or signal is gated on it. The graceful degradation path (`{label: "neutral", score: 0.0}`) has zero effect on anything else in the pipeline.

### Finding 5: Instrument context is user-visible but not on the critical signal path

`_generate_instrument_context()` is the primary LLM narrative for watchlist items that have no active trade suggestion. The user sees it in the AI Explanation Panel under the "Market Context" heading — a 5-section markdown breakdown of what the models saw, technicals, news context, what the system suggests, and key risks.

When it fails: the panel shows an animated skeleton ("Generating explanation from recent news…") indefinitely. There is no DLQ and no `failed=true` state, so the panel never transitions to an error state — it spins forever.

Unlike sentiment, instrument_context IS visible to the user when it fails. However it does not affect the signal pipeline, signals, or trade suggestions.

---

## How the Findings Were Verified

### Audit log queries (2026-06-26 session)

All call counts above were verified by direct SQL queries against the `ai_llm_audit_log` table:

```sql
-- 7-day breakdown by invocation type
SELECT
  DATE(created_at) AS day,
  invocation_type,
  COUNT(*) AS calls
FROM ai_llm_audit_log
WHERE model_provider = 'gemini'
  AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY 1, 2
ORDER BY 1, 3 DESC;

-- Per-minute view around the burst
SELECT
  DATE_TRUNC('minute', created_at) AS minute,
  invocation_type,
  COUNT(*) AS calls
FROM ai_llm_audit_log
WHERE model_provider = 'gemini'
  AND created_at >= '2026-06-26 10:05:00'
  AND created_at < '2026-06-26 10:10:00'
GROUP BY 1, 2
ORDER BY 1, 3 DESC;
```

Database accessed via: `docker exec cortex-db psql -U cortex -d cortex_db`

### Code path tracing (2026-06-26 session)

- `nlp_engine.py` — confirmed `analyze_sentiment()` fires for every event in `process_event()`, priority MEDIUM, TTL 3600s cache
- `signal_assembler.py` + `event_classifier.py` — confirmed impact_score is computed independently; sentiment score is not read by the assembler
- `ai_stream.py` — confirmed sentiment card is the only output of the sentiment path that the user sees
- `explanation_worker.py` — confirmed instrument_context has no DLQ, priority MEDIUM (raised from LOW on 2026-06-25), 2-hour TTL on `AIInstrumentContext`
- `request_manager.py` — confirmed Priority enum: CRITICAL=1, HIGH=2, MEDIUM=3, LOW=4, BACKGROUND=5; all three burst callers are MEDIUM, same tier as news_forecast

### Historical fix log cross-reference

Chronological fix log with outcomes, cross-referenced against memory records:

| Date | Fix | Result |
|---|---|---|
| 2026-06-12 | Redis cache on sentiment (1h TTL) + daily quota circuit breaker | Reduced duplicate calls; did not reduce new-article volume |
| 2026-06-13 | Regex fix for minimal-body 429 format | Prevented false-positive circuit opens on rate-limit 429s |
| 2026-06-20 | fake_news cache (1h TTL), event_classifier cache (30min), news_forecast cache TTL 300s | Reduced non-sentiment callers; sentiment volume unchanged |
| 2026-06-22 | Multi-key load balancer (5 keys, ~7500 combined RPD) | Increased budget; did not reduce call rate |
| 2026-06-22 | Priority-queue request manager | Ensures explanation wins if queue has capacity; does not prevent budget exhaustion |
| 2026-06-25 | Heuristic pre-filter for event_classifier (5–10% reach Gemini) | Reduced classifier Gemini calls; sentiment volume unchanged |
| 2026-06-25 | instrument_context priority LOW → MEDIUM | Made context compete equally with news_forecast; contributed to 2026-06-26 burst |

None of the fixes reduced the volume of sentiment calls. Each fix addressed a different angle of the same underlying problem.

---

## Root Cause

**Sentiment is a high-volume, low-value caller running at equal priority with user-facing, high-value callers.**

- High volume: fires on every news event, no way to batch or defer; cache only helps for seen articles within 1 hour.
- Low value: output feeds only an informational display card; signal pipeline does not depend on it numerically.
- Equal priority: MEDIUM — same tier as `news_forecast` (which directly feeds trade signals) and `instrument_context` (which the user sees in the panel).

The mismatch between volume and value means that on any day with significant news flow, sentiment silently consumes the majority of the daily quota before the user ever opens the app. On startup after a sleep, the accumulated backlog fires all at once and burns the remaining quota in the first 90 seconds after the quota reset.

The consequence is that `explanation` (HIGH, the primary user-facing output) frequently gets no permit at all — not because the priority queue is broken, but because the budget has already been consumed by background enrichment work.

---

## What Has Not Been Tried

The structural fix — reducing the **volume** of sentiment calls, not just their priority — has not been implemented. Options include:

1. **Drop sentiment priority to BACKGROUND** — ensures explanation + news_forecast always drain first; sentiment only runs when truly idle.
2. **Extend cache TTL** — e.g., 24h instead of 1h. Same article from the same day would get one call total.
3. **Skip Gemini for sentiment entirely** — use a local model (FinBERT already in the stack) or rule-based scoring for the card. Gemini is not needed for a display-only sentiment indicator.
4. **Daily budget guard** — track RPD in Redis; when remaining budget < a reservation threshold for HIGH priority, block MEDIUM and below immediately.
5. **Defer startup backlog processing** — add a startup delay before the event_processing_loop drains its backlog, giving the system time to serve any pending user-facing requests first.

Options 3 and 4 address the root cause structurally. Options 1, 2, and 5 are mitigation without fixing the structural mismatch.
