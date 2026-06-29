# Gemini API Quota Reduction Plan

**Date:** 2026-06-20  
**Status:** Pending Implementation  
**Scope:** Reduce outgoing Gemini API call volume to stay within free-tier limits (1500 RPD)

---

## Root Causes (Summary)

Five independent issues compound to exhaust the daily quota:

1. `news_forecaster` re-forecasts every symbol every **30 seconds** — largest single source of waste
2. `event_classifier` and `fake_news_detector` have **no caching** — same content triggers new calls on every pipeline run or worker restart
3. `health_check()` in `llm_client.py` **bypasses the request manager entirely** — invisible quota drain, not tracked or throttled
4. `explanation_worker` miscounts a **queue-full error as a failed attempt**, triggering a retry and doubling queue pressure under load
5. `LLM_MAX_RETRIES = 3` amplifies transient 429 bursts by **3x** before the circuit breaker trips

---

## Changes

### Change 1 — Config tuning
**File:** `backend/app/core/config.py`  
**Risk:** Zero — config-only, no logic changes  

| Setting | Current | New | Reason |
|---|---|---|---|
| `NEWS_FORECAST_CACHE_TTL` | `30` | `300` | 30s means N symbols × 2/min = biggest RPM leak |
| `LLM_MAX_RETRIES` | `3` | `2` | 3 retries triples quota burn on burst errors |
| `GEMINI_PERMIT_TIMEOUT` | `30.0` | `60.0` | Fires inside explanation_worker's 120s window, miscounted as retry (see Change 4) |

---

### Change 2 — Fix health_check quota leak
**File:** `backend/app/ai/intelligence/llm_client.py`  
**Line:** ~652  
**Risk:** Low  

`health_check()` calls `self._genai.aio.models.generate_content` directly, bypassing `GeminiRequestManager`. Every Docker/k8s health probe or startup check burns 1 RPM invisibly.

**Fix:** Add a module-level debounce — if `health_check()` was called within the last 60 seconds, return the cached result. No manager acquire needed; a simple `_last_health_check: datetime | None` class variable with a TTL guard is sufficient.

---

### Change 3 — Add Redis cache to event_classifier
**File:** `backend/app/ai/intelligence/event_classifier.py`  
**Risk:** Low  

No cache exists. Every event classification is a live Gemini call. On worker restart, previously-seen events are reclassified from scratch.

**Fix:**
- Cache key: `SHA-256(event_content[:1500] + symbol)` → Redis key `cortex:event_class:<hash>`
- TTL: `1800s` (30 min) — events don't change after first classification
- On cache hit: return cached `ClassificationResult` directly, skip LLM call
- On cache miss: classify, write result to cache, return
- Cache only when `confidence >= 0.7` — don't cache low-confidence fallbacks

---

### Change 4 — Add Redis cache to fake_news_detector
**File:** `backend/app/ai/intelligence/fake_news_detector.py`  
**Risk:** Low  

No cache exists. Same article URL or content re-triggers a Gemini call on every pipeline pass.

**Fix:**
- Cache key: `SHA-256(article_url or content[:500])` → Redis key `cortex:fakenews:<hash>`
- TTL: `3600s` (1 hour) — article credibility doesn't change within a trading session
- On cache hit: return cached score directly, skip LLM call
- On cache miss: detect, write score to cache, return

---

### Change 5 — Fix explanation_worker retry miscounting
**File:** `backend/app/ai/intelligence/explanation_worker.py`  
**Lines:** ~786, ~1175–1186  
**Risk:** Medium (retry flow change)  

**Current bug:** `GEMINI_PERMIT_TIMEOUT = 30s` fires inside the `120s` wall-clock timeout as `GeminiRateLimitError`. This is caught by the bare `except Exception` at line ~786, counted as a failed attempt, and triggers attempt 2. Under load, this doubles the number of generate calls queued.

**Fix:** Catch `GeminiRateLimitError` separately from `LLMFallbackExhausted` and other errors:
- `GeminiRateLimitError` (queue full / permit timeout) → **do not count as attempt**, re-queue the work item after a short backoff (5–10s) instead of burning the retry budget
- `GeminiQuotaExhausted` → abandon immediately, no retry, write `quota_exhausted` to audit log
- `LLMFallbackExhausted` / other → count as attempt, existing behaviour

---

### Change 6 — Verify RAG batch size
**File:** `backend/app/core/config.py` and `backend/app/ai/rag/ingester.py`  
**Risk:** Zero (verification only)  

Memory records that `RAG_EMBED_BATCH_SIZE` was already increased to 96 as a quick win (2026-06-20), but `config.py` currently reads `32`. Verify which value is live before touching. If 32 is the current deployed value, update to 96 as part of this change set.

---

## Implementation Order

1. **Change 1** (config) — deploy first, immediate effect, no code risk
2. **Change 2** (health_check debounce) — small, self-contained
3. **Change 3** (event_classifier cache) — independent of others
4. **Change 4** (fake_news_detector cache) — independent of others
5. **Change 6** (RAG batch size verify) — confirm then patch if needed
6. **Change 5** (explanation_worker retry fix) — last, most complex, needs careful testing

---

## Expected Impact

| Change | Estimated RPM Reduction |
|---|---|
| Cache TTL 30s → 300s (news_forecaster) | ~80% reduction in forecast calls |
| event_classifier cache | Eliminates duplicate calls on restart + repeated events |
| fake_news_detector cache | Eliminates same-article re-checks |
| health_check debounce | Removes invisible per-probe quota drain |
| Retry reduction (3→2) | Cuts retry amplification by 33% on burst errors |
| Explanation worker fix | Halves queued generate calls under rate-limited conditions |

---

## Out of Scope (Deferred)

- **Multi-key load balancer** — blocked on free-tier ToS; revisit on upgrade to paid key
- **news_forecaster per-symbol dedup** — handled by TTL increase in Change 1
- **RAG backfill quota coordination** — backfill's local TokenBucket (8 calls/min) is already more restrictive than the manager's embed RPM cap (90 RPM); no change needed
