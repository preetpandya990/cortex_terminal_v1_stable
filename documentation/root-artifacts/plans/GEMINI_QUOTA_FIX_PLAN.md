# Gemini Quota Exhaustion — Fix Plan
**Date:** 2026-06-29
**Status:** Planning — awaiting clarification before implementation

---

## Problem Summary

All 5 Gemini API keys are circuit-broken. Trade suggestion explanations have been stale since
2026-06-25. One explanation is permanently stuck in the DLQ (suggestion 1091, score 76.89,
`NSE_EQ|INE050001010`).

---

## Root Cause Analysis

### Primary Bug — Misconfigured `GEMINI_GENERATE_RPD`

`GEMINI_GENERATE_RPD` defaults to `1,500` in `config.py` and is **not overridden in `.env`**.
The actual Google free-tier limit is ~10 RPD/key × 5 keys = **50 real calls/day**.

The budget guard computes headroom as `1,500 × 5 = 7,500` — it never fires. Every morning,
sentiment batching (BACKGROUND priority) burns all 50 real calls within ~25 minutes,
unconstrained. Circuits trip on genuine 429s. Explanations (HIGH priority) get zero calls.

```
Configured:  GEMINI_GENERATE_RPD = 1,500/key × 5 keys = 7,500 assumed budget
Actual:      ~10 RPD/key × 5 keys = 50 real daily calls

Budget guard fires when:  remaining < GEMINI_HIGH_PRIORITY_RPD_RESERVE (200)
With old config:          fires when used > 7,300  →  never reached
With correct config:      fires when used > (total - reserve)  →  works correctly
```

### Secondary Bug — Config Validator Blocks the Fix

`GEMINI_GENERATE_RPD` has a `ge=100` minimum validator in `config.py`. The actual free-tier
limit (~10) is below that floor. The correct value **cannot be set in `.env` without a code
change** to the validator first.

### Third Issue — Reserve Misconfiguration Cascade

Even after setting `GEMINI_GENERATE_RPD=10`, the default `GEMINI_HIGH_PRIORITY_RPD_RESERVE=200`
would mean `remaining (50) < reserve (200)` is **always true** from call #1 — MEDIUM/LOW/BACKGROUND
would be permanently throttled, blocking sentiment entirely. Both values must be set together.

### Fourth Issue — No Auto-Recovery from DLQ

When a quota-exhausted job lands in the DLQ, it stays there permanently. The midnight PT
circuit reset (`_reset_all_open_circuits()` in `request_manager.py`) clears the circuit
breaker state but does **nothing** to recover DLQ'd jobs. Every quota outage requires manual
intervention.

### Fifth Issue — Multi-Key Quota Architecture Question (Unconfirmed)

The 5 API keys — are they all from the **same Google account/project**, or 5 separate accounts?
This is a critical architectural question:

- **Same project**: Google's free RPD is a *per-project* limit shared across all keys.
  5 keys sharing 1 project = still only ~50 total calls/day. The multi-key strategy
  provides zero additional daily quota — only RPM load-balancing.
- **5 separate accounts**: Each key has an independent quota = ~250 total calls/day.

The investigation observed exactly 50 total calls exhausted across 5 keys, which strongly
suggests they are on **one project** — meaning the multi-key setup is providing RPM
load-balancing benefits only, not quota multiplication.

---

## Clarifications Needed Before Implementation

The following must be confirmed before touching any code:

**1. Exact per-key RPD quota**
Go to Google AI Studio (`aistudio.google.com`) → your API key → Quota page.
What is the exact daily request limit shown for `gemini-2.5-flash`? The investigation
inferred ~10 RPD/key from observed 429 behavior, but we need the exact number to set
`GEMINI_GENERATE_RPD` correctly.

**2. Single project vs. multiple accounts**
Are all 5 Gemini API keys registered on the same Google account/project, or are they
5 separate Google accounts? Check if they all show the same project ID in AI Studio.
This determines whether multi-key provides actual quota scaling or just RPM scaling.

**3. Paid tier timeline**
Is upgrading to a paid Gemini plan planned (near-term, long-term, or not at all)?
At 6 watchlist instruments with sentiment + explanations + forecasts competing for
quota, free tier is structurally insufficient regardless of configuration tuning.
The answer determines how conservatively the free-tier workarounds need to be built.

---

## Implementation Plan

### Phase 1 — Immediate Config Fix (no code changes)

**Prerequisite:** Answers to clarifications 1 and 2 above.

1. Set the correct values in `backend/.env`:

   ```env
   # Set to the exact per-key RPD shown in Google AI Studio
   GEMINI_GENERATE_RPD=<actual_per_key_rpd>

   # Tuned for actual quota: reserve enough for HIGH-priority explanation calls.
   # Example for free tier (50 total/day): 10 reserves 10 for explanations,
   # leaving 40 for sentiment/forecaster/other background work.
   GEMINI_HIGH_PRIORITY_RPD_RESERVE=<appropriate_value>
   ```

   **Reserve sizing guide:**
   | Scenario | Total RPD | Recommended Reserve |
   |---|---|---|
   | Free tier (10/key × 5) | 50 | 10–15 |
   | Free tier (50/key × 5) | 250 | 50 |
   | Paid Tier 1 | Effectively unlimited | 200 (original design) |

2. Restart backend and worker processes.

3. **After midnight PT circuit reset** (auto, via `_run_quota_reset_watcher`) OR after
   manually verifying quota has reset in AI Studio and running:
   ```bash
   docker exec cortex_merge_ai-ml-redis-1 redis-cli DEL \
     cortex:gemini:circuit:generate:jZfei_tA \
     cortex:gemini:circuit:generate:GWUf3hHg \
     cortex:gemini:circuit:generate:-NlXkftg \
     cortex:gemini:circuit:generate:R3N8QMBw \
     cortex:gemini:circuit:generate:VGUK4RlA
   ```
   **Do not run this before quota actually resets** — it will immediately hit another 429.

4. Requeue the stuck explanation via the on-demand bypass endpoint:
   ```
   POST /api/v1/ai/stream/explanation/request/{suggestion_id}?token=<jwt>
   suggestion_id: 11a45408-ac2f-4959-8b18-3c77e5c0b017
   ```

---

### Phase 2 — Code Fixes (3 targeted changes)

#### 2a. Fix the Config Validator (`backend/app/core/config.py`)

Change `GEMINI_GENERATE_RPD`'s minimum validator from `ge=100` to `ge=1`.

Free-tier values below 100 are legitimate. The current floor physically prevents operators
from setting the correct free-tier value and makes the root cause of this outage
irrecoverable via config alone.

```python
# Before
GEMINI_GENERATE_RPD: int = Field(1_500, ge=100, le=100_000, ...)

# After
GEMINI_GENERATE_RPD: int = Field(1_500, ge=1, le=100_000, ...)
```

#### 2b. Startup Misconfiguration Guard (`backend/app/ai/intelligence/request_manager.py`)

At `GeminiRequestManager.initialize()`, add a `CRITICAL` log warning if:
```
GEMINI_GENERATE_RPD × key_count < GEMINI_HIGH_PRIORITY_RPD_RESERVE
```

This exact misconfiguration is what caused the current outage. It should be detected
at boot time with an unmissable log entry, not discovered 4 days later when explanations
go stale with no clear error.

Optionally (stronger): also warn if:
```
GEMINI_GENERATE_RPD × key_count / GEMINI_HIGH_PRIORITY_RPD_RESERVE < 3
```
i.e., when the reserve consumes more than 33% of the total budget — a sign the values
are misconfigured relative to each other even if neither is individually wrong.

#### 2c. DLQ Auto-Requeue on Quota Reset (`explanation_worker.py` + `request_manager.py`)

**The problem:** The midnight PT quota reset clears circuit breakers but leaves quota-exhausted
DLQ entries permanently stranded. Every outage requires manual intervention.

**The fix:** After `_reset_all_open_circuits()` fires, the explanation worker should
automatically scan the DLQ for entries with `reason=gemini_quota_exhausted` from the
current trading day and move them back to `cortex:stream:explanation:jobs`.

**Design options:**

*Option A — Pub/Sub signal from manager to worker (preferred):*
- `request_manager._reset_all_open_circuits()` publishes to a Redis channel
  (e.g., `cortex:gemini:quota:reset`) after clearing circuits
- `explanation_worker` subscribes and on receipt, scans DLQ for `gemini_quota_exhausted`
  entries, re-publishes them to the jobs stream with fresh message IDs
- Clean separation — manager doesn't need to know about the worker

*Option B — Worker polls on its own schedule:*
- Worker checks DLQ for stale quota-exhausted entries at the start of each market session
- Simpler, no coupling, slightly less immediate

Either option eliminates the manual Redis intervention requirement. Production systems
at this standard must self-heal.

---

### Phase 3 — Observability (Prometheus + Grafana)

Two alert rules targeting the `gemini_rpd_budget_remaining` gauge that already exists
in `metrics.py`:

**Alert 1 — `GeminiRPDBudgetLow` (warning)**
```yaml
alert: GeminiRPDBudgetLow
expr: gemini_rpd_budget_remaining < gemini_high_priority_rpd_reserve * 2
for: 5m
severity: warning
annotations:
  summary: "Gemini daily budget below 2× the HIGH-priority reserve"
  description: "Background callers will be throttled within the next ~{{ $value }} calls."
```
This fires early — before the crisis — giving time to act.

**Alert 2 — `GeminiAllCircuitsOpen` (critical)**
```yaml
alert: GeminiAllCircuitsOpen
expr: sum(gemini_circuit_open{op="generate"}) == count(gemini_circuit_open{op="generate"})
for: 1m
severity: critical
annotations:
  summary: "All Gemini generate circuits are OPEN — explanations are dead"
  description: "Daily quota exhausted. Circuits reset at midnight Pacific Time."
```
This is the "explanations have stopped" signal that would have caught the current outage
on day 1 instead of day 4.

A Grafana panel showing `gemini_rpd_budget_remaining` as a daily burn-down chart, with
the reserve threshold marked as a horizontal line, completes the picture.

---

### Phase 4 — Paid Tier Upgrade (operator action, recommended)

With 6 watchlist instruments running sentiment analysis on every article, context
pre-warming, explanation generation, and news forecasting — the free tier is structurally
undersized regardless of how well the config is tuned.

**When billing is enabled:**
1. Generate new API keys (or keep existing keys — billing applies at the project level)
2. Set in `backend/.env`:
   ```env
   GEMINI_GENERATE_RPD=10000      # Tier 1 paid limit per key (confirm in AI Studio)
   GEMINI_GENERATE_RPM=1000       # Tier 1 paid limit (currently set to 30 — free tier)
   GEMINI_HIGH_PRIORITY_RPD_RESERVE=200  # Original designed value
   ```
3. The multi-key load balancer then provides genuine RPM scaling (not just quota arithmetic)
4. Budget guard becomes a cost-control mechanism rather than a scarcity guard

**Cost estimate at current usage (50 calls/day observed on free tier):**
Even at 10× scale (500 calls/day), with gemini-2.5-flash at $0.30/1M input + $2.50/1M output,
and assuming ~2K tokens/call average, daily cost ≈ $0.15/day. Entirely negligible.

---

## Summary

| Phase | Type | Effort | Blocks |
|---|---|---|---|
| 1 | Config + operator action | ~15 min | Immediate recovery of stuck explanation |
| 2a | Code (1-line fix) | ~5 min | Allows free-tier config to be set |
| 2b | Code (startup check) | ~30 min | Prevents recurrence going undetected |
| 2c | Code (DLQ self-heal) | ~2h | Eliminates all future manual recovery |
| 3 | Grafana/Prometheus | ~1h | Early warning before next outage |
| 4 | Operator (billing) | ~30 min | Structural capacity fix |

Phases 2a and 2b are the minimum code changes — small, low-risk, and immediately protective.
Phase 2c is the production-grade self-healing addition. Phase 3 closes the observability gap.
Phase 4 removes the constraint permanently.
