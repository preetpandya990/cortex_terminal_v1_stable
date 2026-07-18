# Fix: Orphaned Explanations in Legacy-Mode Explanation Pipeline

**Date:** 2026-07-15
**Status:** Implemented, tested, UNCOMMITTED. Follows up on
`AI_EXPLANATION_PIPELINE_ORPHANED_SUGGESTION_INVESTIGATION.md` (2026-07-14).

## Problem (recap of the investigation)

In legacy mode (`EXPLANATION_ON_DEMAND=False`), the correlation engine
auto-publishes an explanation job to Kafka as a best-effort side effect after
committing a trade suggestion (`app/ai/correlation/engine.py`). That publish
is wrapped in a deliberately non-fatal `except Exception` — a broker hiccup
must never roll back a committed suggestion. But nothing ever noticed a
failed publish: `explanation_service.ensure_explanation()` blindly trusted
the engine's attempt succeeded and returned `"generating"` forever. Result:
an infinite loading skeleton in the UI, no failure state, no recovery path.

A partial index (`idx_trade_suggestions_explanation_pending`, migration
0042) was built years ago specifically to support a reconciliation query for
this — it was never wired to anything.

Trigger case: suggestion `153e0b1f-830f-47b6-9ca7-e72f52c4c196` (HESTERBIO),
created by a test script that never called `init_kafka()`. Confirmed via
live Kafka/Redis inspection: no job message, no Redis keys, consumer group
idle at zero lag.

## Diagnosis

This is the textbook **dual-write problem** (DB write + broker publish, no
atomicity) — a known, well-studied failure class in event-driven systems.
Industry-standard fix is the **transactional outbox pattern**. A full
CDC-based version (Debezium tailing the Postgres WAL) would be new
infrastructure disproportionate to a single non-fatal-publish edge case, and
inconsistent with how this codebase already solves this exact problem shape
twice: `ai_processing_safety_net` (periodic threshold sweep) and
`explanation_worker.py`'s DLQ quota-recovery routine (Redis `SET NX` dedup +
republish). This fix extends that existing convention rather than
introducing a new pattern — `trade_suggestions` rows already function as the
outbox (that's what migration 0042's index was for); the missing piece was
the relay that drains it.

## Design

**One unified in-flight lock.** `DEMAND_INFLIGHT_KEY` (previously
demand-mode-only) was generalized and renamed to `EXPLANATION_INFLIGHT_KEY`
— "an explanation-generation attempt for this suggestion_id is either
actively running or has just been claimed for republish."

- **Worker side:** refreshes the lock (plain `SET`, TTL 200s) at the start
  of every attempt, including retries — this is what lets reconciliation
  reliably distinguish "genuinely still retrying" from "orphaned" instead of
  relying on a wall-clock guess. (Worst-case legacy retry cycle is ~9 min
  across `MAX_ATTEMPTS=3` with 60s backoff — longer than the 300s staleness
  cutoff, so the lock, not the clock, is what prevents a race.)
- **Reconciliation side:** `SET NX` the same key before republishing. Lock
  already held → skip, don't duplicate-publish.

**Layer 1 — self-heal on read.** `ensure_explanation()`'s legacy branch: if
an active suggestion has no explanation and is older than
`EXPLANATION_RECONCILE_STALENESS_SECS` (300s default), it attempts the same
claim-and-publish sequence on-demand mode already uses (`trigger=
"reconciliation"` instead of `"demand"`). Instant recovery the moment a user
views the affected panel.

**Layer 2 — periodic reconciliation sweep.** New worker task
`ExplanationReconciliationSweep`, polling every
`EXPLANATION_RECONCILE_SWEEP_INTERVAL_SECS` (120s default). Queries
`idx_trade_suggestions_explanation_pending` for active, unexplained,
sufficiently-stale, above-consensus-threshold suggestions and republishes
each independently (one failure never blocks the rest of the batch). Only
runs when `not EXPLANATION_ON_DEMAND` — on-demand mode's "nobody viewed it
yet" laziness is intentional, not a bug. Guaranteed backstop for suggestions
nobody ever views.

`engine.py` was intentionally left untouched — its non-fatal try/except
around the auto-publish is correct as-is; the fix belongs entirely in the
recovery layer.

## Files changed

| File | Change |
|---|---|
| `backend/app/core/config.py` | 2 new settings: `EXPLANATION_RECONCILE_STALENESS_SECS` (300), `EXPLANATION_RECONCILE_SWEEP_INTERVAL_SECS` (120) |
| `backend/app/core/metrics.py` | 5 new metrics: `explanation_reconciliation_republish_total{trigger_source}`, `..._sweep_runs_total{status}`, `..._sweep_duration_seconds`, `..._sweep_last_run_timestamp`, `..._pending` |
| `backend/app/ai/intelligence/explanation_service.py` | Renamed `DEMAND_INFLIGHT_KEY`→`EXPLANATION_INFLIGHT_KEY`; extracted shared `claim_and_publish()` helper (returns `"published": bool` distinct from `"status"`); added legacy self-heal branch |
| `backend/app/ai/intelligence/explanation_worker.py` | Refreshes `EXPLANATION_INFLIGHT_KEY` on every attempt start; cleanup is now mode-aware — demand jobs release every attempt (unchanged), legacy/reconciliation jobs only release on true success or DLQ, never mid-retry-backoff |
| `backend/app/workers/explanation_reconciliation_sweep.py` | **New file** — the sweep task class |
| `backend/app/workers/registry.py` | Task registration (`TASK_NAMES`, `TASK_EXPECTED_INTERVAL_SECONDS`, instantiation, lambda wiring) — 21st task |
| `backend/tests/ai/intelligence/test_explanation_service.py` | +5 test cases for the self-heal branch |
| `backend/tests/workers/test_explanation_reconciliation_sweep.py` | **New file** — 7 test cases |
| `backend/tests/workers/test_registry.py` | Fixed 2 stale hardcoded task counts (19→21; was already stale before this change) |

## Bug caught mid-implementation

`claim_and_publish()` originally returned the same `status="generating"`
whether it actually published a job or found the lock already held (someone
else handling it). Left as-is, the sweep's republish counter/metric would
have silently over-counted "skipped, already in flight" as "republished."
Fixed by adding an explicit `"published"` boolean to the return contract,
independent of `"status"`.

## Verification performed

- 42/42 relevant unit tests pass (`test_explanation_service.py`,
  `test_explanation_reconciliation_sweep.py`, `test_registry.py`,
  `test_ai_processing_safety_net.py`).
- Direct import/settings sanity check confirms no circular imports and
  correct config defaults load.
- Confirmed the 2 failing `test_supervisor.py` tests pre-exist on unmodified
  `main` (verified via `git stash`) — unrelated timing-based flakiness, not
  caused by this work.
- `graphify update .` run clean (13,446 nodes, 49,413 edges).

## Not done / follow-ups not included

- Nothing committed or deployed — matches how other recent work this
  session has been left (see memory: multiple UNCOMMITTED+UNDEPLOYED items).
- No new Grafana panel or alert rule was added for the 5 new metrics. The
  new task automatically gets freshness-ratio staleness alerting for free
  via the existing generic `on(task)` PromQL join (registry.py's
  `TASK_EXPECTED_INTERVAL_SECONDS` convention), so baseline coverage exists.
  A dedicated "reconciliation firing unusually often → broker health
  problem" panel would be a reasonable follow-up if wanted, but was left out
  as scope creep beyond fixing the bug.
- Live end-to-end verification (reproduce the original orphan, confirm the
  sweep/self-heal actually resolve it against a running stack) was not
  performed — the plan's verification section describes how to do this but
  it requires the live backend/worker/Kafka/Redis stack running.
