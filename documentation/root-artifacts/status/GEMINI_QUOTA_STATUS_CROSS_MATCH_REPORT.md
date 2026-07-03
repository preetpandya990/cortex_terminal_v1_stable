# Gemini Quota Status — Cross-Match Report

**Generated:** 2026-06-30  
**Source:** GEMINI_API_USAGE_AUDIT.md + all quota-related session memories

---

## 1. Hard Constraints

- **5 AI Studio keys**, each an independent Google account: `jZfei_tA`, `GWUf3hHg`, `-NlXkftg`, `R3N8QMBw`, `VGUK4RlA`
- **Per-key limit:** ~10 RPD (empirically confirmed free tier)
- **Total daily generate budget: 50 calls/day**
- **Embed quota is separate** (`gemini-embedding-001`) — unaffected by generate exhaustion
- **Quota resets:** midnight PT + buffer (auto-reset watcher in `request_manager.py`)

---

## 2. Observed Quota Burn (Audit, 2026-06-29 + 2026-06-30)

| Consumer | Calls (2 active days) | % of Generate Quota | Priority |
|---|---|---|---|
| `SentimentOutput` (per-article) | 125 | 32% | MEDIUM (now BACKGROUND after fix) |
| `NewsForecastOutput` (per-article) | 76 | 19% | MEDIUM |
| `_ClassificationSchema` (event classifier) | 39 | 10% | MEDIUM (now LOW after fix) |
| `ExplanationOutput` (trade + context) | 30 | 8% | HIGH |
| `_SentimentBatchOutput` (batched) | 9 | 2% | BACKGROUND |
| **Total generate** | **~245** | **100%** | — |

**Pattern:** RSS pipeline (sentiment + forecast + classifier) burns ~82% of daily RPD within the first 90 minutes of each trading day. All 5 keys exhaust by ~09:30 UTC. Every post-09:30 context/explanation call fails with `GeminiQuotaExhausted`.

**Critical spike:** At `2026-06-30T09:15:35`, the forecaster fired 11 `NewsForecastOutput` calls in under 2 seconds (unthrottled batch dump). This single event consumed ~12% of the daily RPD and triggered the final cascade to full exhaustion 15 minutes later.

---

## 3. Fixes Built (All UNCOMMITTED + UNDEPLOYED)

### Fix 1 — Circuit Breaker + Fast-Fail (`llm_client.py`) — 2026-06-12/13
- Regex-based daily-quota detector (`_DAILY_QUOTA_RE`) covers both full-body and minimal-body 429 formats
- In-memory circuit opens immediately on daily-quota 429; subsequent callers short-circuit in <1ms (zero network calls)
- Server `retryDelay` hint honoured via `_gemini_wait` (prevents futile fast-retries)
- `GeminiQuotaExhausted(LLMFallbackExhausted)` subclass — existing callers transparent

### Fix 2 — GeminiRequestManager (`request_manager.py`) — 2026-06-13 → 2026-06-22
- Priority queue + per-minute token buckets gate all 6 callers through a single coordinator
- Per-key Redis circuit breaker — survives restarts
- Multi-key round-robin (`_get_next_client()`) across all 5 keys
- Auto quota-reset watcher fires at midnight PT + buffer
- **Priority hierarchy:**

| Priority | Caller |
|---|---|
| HIGH | Trade explanation (`explanation_worker._generate_explanation`) |
| MEDIUM | Instrument context/watchlist (`_generate_instrument_context`) ↑ bumped from LOW |
| LOW | Event classifier Gemini path ↓ demoted from MEDIUM; safety responses |
| BACKGROUND | Embeddings; sentiment (after Change 8 below) |

### Fix 3 — Budget Guard (`request_manager.py`) — 2026-06-27
- `GEMINI_GENERATE_RPD=10` per key, total=50/day (corrected from wrong default of 1500)
- Config validator fixed: `ge=100` → `ge=1` (was blocking free-tier value)
- Soft daily guard: blocks MEDIUM/LOW/BACKGROUND when RPD near-exhausted; HIGH/CRITICAL always pass
- RPD counter persisted in Redis (`cortex:gemini:rpd:generate:{date_pt}`) — survives restarts
- Adaptive flush cadence: `max(1, total_daily_budget // 20)` = every 2 calls on free tier
- 4 Prometheus alert rules: `GeminiRPDBudgetLow`, `GeminiRPDBudgetCritical`, `GeminiAllCircuitsOpen`, `GeminiKeyCircuitOpen`

### Fix 4 — DLQ Self-Healing (`explanation_worker.py`) — 2026-06-30
- `_quota_reset_listener()` (worker_id=0) subscribes to `cai:gemini:quota:reset` pub/sub
- On quota reset signal: auto-requeues DLQ entries via `_requeue_quota_dlq_entries(trigger="quota_reset")`
- Boot-time scan requeues previous-day DLQ entries missed by pub/sub
- Dedup guard: `SET NX cortex:gemini:dlq:requeued:{suggestion_id}` (48h TTL)

### Fix 5 — Caching Everywhere — 2026-06-13 to 2026-06-27
| Cache | TTL | Key | Condition |
|---|---|---|---|
| Sentiment (`nlp_engine.py`) | 24h | `nlp:sentiment:{prompt_hash}` | Successful LLM result only |
| Event classifier (`event_classifier.py`) | 1800s | `cortex:event_class:{sha256(content[:1500])}` | `confidence >= 0.7` only |
| Fake news detector (`fake_news_detector.py`) | 3600s | `cortex:fakenews:{sha256(url or content[:500])}` | Always |
| News forecast (`config.py`) | 300s (↑ from 30s) | Existing key | ~80% RPM reduction for repeated articles |
| Health check (`llm_client.py`) | 60s debounce | In-process | Both healthy + unhealthy cached |

### Fix 6 — Event Classifier Heuristic Pre-Filter (`event_classifier.py`) — 2026-06-25
- Keyword-based heuristic (IPO/FPO, dividend/buyback, AGM/board, credit rating, insider trading)
- `confidence=0.75` (clears `< 0.7` GPT-4o gate)
- ~90% of classifiable articles bypass Gemini entirely
- Only ~5–10% of articles reach the Gemini path

### Fix 7 — Sentiment Batching (`nlp_engine.py`) — 2026-06-27
- N articles → 1 Gemini call via `_SentimentBatchOutput` schema (batch size 15, flush window 60s)
- Dual-path: background queue/flusher path + SSE on-demand immediate batch
- **Claimed reduction: ~93% of sentiment RPD**
- Sentiment priority demoted to BACKGROUND (lowest tier)

---

## 4. Post-Fix Budget Projection

After all fixes land, estimated daily call budget against the 50-call ceiling:

| Consumer | Raw Calls (audit) | After Fix | Net |
|---|---|---|---|
| Sentiment | 125 | ~9 (batching + 24h cache) | **–116** |
| Event classifier | 39 | ~3–4 (heuristic bypass) | **–35** |
| News forecast | 76 | ~70–76 (cache helps repeats only) | **–0 to –6** |
| Explanations | 30 | ~30 (unchanged, HIGH priority) | **0** |
| **Total** | **~245** | **~112–119** | vs. **50-call budget** |

**The budget is still blown by ~2.2–2.4×** after all fixes are applied.

---

## 5. The Remaining Budget Eater — `NewsForecastOutput`

`NewsForecastOutput` is the last standing dominant consumer with **zero dedicated fix built for it.**

**Why it survives all current fixes:**
- No batching (only sentiment was batched)
- No heuristic bypass (only event classifier got one)
- Cache TTL bump (30s → 300s) only helps repeated articles — the initial burst of new articles all miss cache
- Priority is still MEDIUM — budget guard blocks it when budget is near-zero, but the burst fires at market open before the guard can intervene
- The 11-call unthrottled burst at `09:15:35` fires faster than the token bucket can gate it — all 11 requests are in-flight simultaneously before any 429 is received

**Scope of the problem:**
- On active trading days, `NewsForecastOutput` alone (~76 calls) consumes **152% of the 50-call daily budget**
- Even if sentiment and classifier are fully zeroed out, forecast + explanations (~106 calls) still exceeds budget 2×
- The investigation memory explicitly flags this as "Fix needed — UNFIXED"

**What a fix would require (not yet built):**
1. Rate-gate the forecaster batch through the request manager (currently bypasses throttling on batch dump)
2. OR deprioritize `NewsForecastOutput` below `ExplanationOutput` (currently both MEDIUM)
3. OR batch multiple article forecasts into one Gemini call (analogous to sentiment batching)
4. OR disable `NewsForecastOutput` entirely on free-tier keys and re-enable on paid tier

---

## 6. Operator Actions Still Required (Before Any Fix Is Effective)

1. Commit and deploy all changes listed in Section 3
2. Set correct `.env` values:
   ```
   GEMINI_GENERATE_RPD=10
   GEMINI_HIGH_PRIORITY_RPD_RESERVE=8
   GEMINI_GENERATE_RPM=50
   GEMINI_EMBED_RPM=450
   ```
3. Restart backend + worker processes
4. After midnight PT auto-reset, DLQ recovery fires automatically — no manual requeue needed
5. When upgrading to a paid Gemini key: update `GEMINI_GENERATE_RPD`, `GEMINI_HIGH_PRIORITY_RPD_RESERVE`, `GEMINI_GENERATE_RPM` in `.env`
