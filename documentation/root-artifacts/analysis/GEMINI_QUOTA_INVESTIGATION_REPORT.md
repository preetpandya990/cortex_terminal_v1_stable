# Gemini Quota Exhaustion — Investigation Report
**Date:** 2026-06-29  
**Symptom:** Trade suggestion explanations stale since 2026-06-25

---

## Summary

All 5 Gemini API keys are circuit-broken with genuine `GenerateRequestsPerDay` 429 errors from Google. The circuit breaker code works correctly — all 5 keys independently hit their actual daily quota limit. The root cause is that `GEMINI_GENERATE_RPD` is configured at 1,500 per key, but the actual Google free-tier limit for `gemini-2.5-flash` is approximately **10 RPD per key** (50 total across 5 keys). The budget guard therefore never throttles low-priority work, allowing sentiment batching to consume all quota at startup — leaving nothing for explanation generation.

---

## Current State

| Item | Value |
|---|---|
| Watchlist instruments | 6 |
| Active trade suggestions | 5 |
| Qualifying for explanation (score ≥ 75) | 1 — `NSE_EQ\|INE050001010` (76.89) |
| That suggestion's status | **DLQ'd** (`reason: gemini_quota_exhausted`) |
| Explanation queue depth | 1 job (the DLQ'd one — no auto-retry) |
| All 5 circuit breakers | **OPEN** in Redis |
| Circuit breaker TTL | ~16.8 hours from time of investigation — resets midnight PT |
| Today's generate RPD used | 50 calls total |
| Last successful explanation | 2026-06-25 13:35 UTC |
| `ai_instrument_context` rows | 15 total, all expired (0 live) |

---

## Timeline of Today's Failure (2026-06-29)

| Time (UTC) | Event |
|---|---|
| ~09:03 | Backend and worker processes started |
| 09:24:44 | Key `jZfei_tA` — first genuine `GenerateRequestsPerDay` 429 |
| 09:27:35–36 | Keys `-NlXkftg`, `R3N8QMBw`, `VGUK4RlA`, `GWUf3hHg` — all hit 429 within ~1 second |
| 09:29:02–29 | All 5 keys log additional exhaustion events (duplicate entries — two uvicorn worker processes racing to mark the same keys; idempotent in the manager, no actual harm) |
| 09:54:21 | Explanation worker picks up suggestion 1091 for delivery |
| 09:54:22 | `acquire()` fast-path sees all circuits open → raises `GeminiQuotaExhausted` immediately (no API call attempted) → job moved to DLQ |

The sentiment batching startup sweep (fired within minutes of process start) consumed all 50 available generate calls across the 5 keys before explanations got a turn.

---

## Root Cause

### Misconfigured `GEMINI_GENERATE_RPD`

```
Configured:  GEMINI_GENERATE_RPD = 1,500 per key × 5 keys = 7,500 assumed total RPD
Actual:      ~10 RPD per key × 5 keys = 50 actual total RPD
```

`GEMINI_GENERATE_RPD` is used by the budget guard to reserve headroom for HIGH-priority callers and throttle BACKGROUND/MEDIUM callers when quota runs low. With 1,500 configured but only 10 real, the guard never activates — sentiment analysis (BACKGROUND priority) burns through all quota unchecked every morning.

The actual Google free-tier RPD limit for `gemini-2.5-flash` should be confirmed in Google AI Studio, but all evidence points to ~10 RPD per key on the current plan.

### Why the circuit breaker code is NOT the problem

The code correctly:
1. Tries each key in round-robin order when one fails
2. Independently circuit-breaks each key only when IT receives a `GenerateRequestsPerDay` 429
3. Does not cascade — one key opening does not affect others
4. Persists state to Redis so restarts don't waste a quota call to re-learn the circuit state

The duplicate log entries (same key appearing exhausted 2–3 times) are a cosmetic artefact: two uvicorn worker processes share Redis but have separate in-process `_per_key_quota` dicts. Worker 2 can attempt a key that Worker 1 already circuit-broke before the async Redis write completes. Both workers log the error; the manager's idempotency guard prevents double-counting.

---

## Fix Required

### 1. Set the correct RPD limit in `backend/.env`

Check Google AI Studio for the actual per-key daily quota for `gemini-2.5-flash`, then set:

```env
GEMINI_GENERATE_RPD=<actual_per_key_rpd>   # e.g. 10 if free tier
```

This activates the budget guard correctly: it will reserve headroom for HIGH-priority (explanation) calls and throttle BACKGROUND-priority (sentiment) calls before quota is exhausted.

### 2. Requeue the DLQ'd explanation job

The explanation for suggestion `NSE_EQ|INE050001010` (ID 1091, UUID `11a45408-ac2f-4959-8b18-3c77e5c0b017`) is permanently stuck in the DLQ. After midnight PT (circuit reset), it needs to be manually requeued or triggered via the on-demand bypass endpoint.

### 3. Upgrade to a paid Gemini plan (recommended long-term)

With 6 watchlist instruments, sentiment batching, context pre-warming, and explanation generation all competing for the same quota, the free tier is fundamentally insufficient regardless of priority tuning. A paid plan removes the per-key RPD cap and makes quota a non-issue at this scale.

---

## Redis Keys for Immediate Recovery (after midnight PT)

If you need to unblock before midnight PT, DEL these keys after verifying quota has reset in Google AI Studio:

```bash
docker exec cortex_merge_ai-ml-redis-1 redis-cli DEL \
  cortex:gemini:circuit:generate:jZfei_tA \
  cortex:gemini:circuit:generate:GWUf3hHg \
  cortex:gemini:circuit:generate:-NlXkftg \
  cortex:gemini:circuit:generate:R3N8QMBw \
  cortex:gemini:circuit:generate:VGUK4RlA
```

**Do not run this before quota actually resets** — the next call will immediately hit another 429 and re-open all circuits.
