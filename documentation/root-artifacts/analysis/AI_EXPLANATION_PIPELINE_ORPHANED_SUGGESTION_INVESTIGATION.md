# Investigation: Orphaned Explanations in the AI Explanation Pipeline

**Date:** 2026-07-14
**Trigger case:** `153e0b1f-830f-47b6-9ca7-e72f52c4c196` (HESTERBIO, BUY, consensus_score=87.05) —
used below as the concrete example that exposed the gap. The finding is about the
pipeline's failure-recovery behavior in general, not specific to this one suggestion.

## Direct cause — confirmed empirically

HESTERBIO's active suggestion was created by `backend/scripts/force_correlation_test.py`
— the standalone real-pipeline test harness used earlier in this session to verify the
ETF asset-class filter fix. That script **never calls `init_kafka()`**.

In `app/ai/correlation/engine.py` (~line 1081-1117), after committing a suggestion, the
engine tries to auto-publish an explanation job since `EXPLANATION_ON_DEMAND=False`
(confirmed default, not overridden in `.env`) and the consensus score (87.05) clears
`EXPLANATION_CONSENSUS_THRESHOLD` (75.0):

```python
from app.core.kafka import KafkaTopics, publish
...
await publish(KafkaTopics.EXPLANATION_JOBS, {...}, key=str(suggestion.suggestion_id))
```

`publish()` calls `get_kafka_producer()`, which raises `RuntimeError("Kafka not
initialized")` when the process-wide `_producer` singleton was never started — exactly
the case here. This is caught by a deliberately non-fatal `except Exception` (so a Kafka
hiccup never rolls back a committed suggestion) and just logged as a warning. The
suggestion was persisted; the explanation job never was.

### Verified with zero ambiguity

- Kafka topic `cortex.explanation.jobs` contains no message for this `suggestion_id`
  (checked live via `rpk topic consume`).
- No Redis key of any kind references this `suggestion_id` (no debounce lock, no
  idempotency marker — nothing was ever attempted downstream).
- The `cortex-explanation-workers` consumer group shows `TOTAL-LAG: 0` — the workers are
  healthy and idle, correctly waiting on a job that will never arrive.

## Deeper systemic gap — this isn't just a test-script artifact

Checked what happens when a user actually views HESTERBIO's panel (`ai_stream.py` →
`explanation_service.ensure_explanation()`). In legacy mode
(`EXPLANATION_ON_DEMAND=False`), that function's logic is:

```python
if not settings.EXPLANATION_ON_DEMAND:
    if consensus_score < threshold: return "weak_signal"
    return "generating"   # <-- trusts the engine already published; never verifies or re-publishes
```

Its own docstring states the assumption plainly: *"Legacy mode: the engine already
decided at creation time."* There is **no re-publish or verification path** in legacy
mode — if the engine's original publish attempt failed for any reason (Kafka blip,
broker restart, or this exact case), the suggestion is silently orphaned. The frontend
would show an indefinite "generating" skeleton forever, with no failure ever surfacing
to the user and no automatic recovery.

This isn't hypothetical: `alembic/versions/0042_trade_suggestion_llm_explanation.py`
created a partial index `idx_trade_suggestions_explanation_pending` explicitly commented
as targeting *"the explanation worker's pending-queue query"* for exactly this class of
instrument (active + `llm_summary IS NULL`):

```sql
CREATE INDEX idx_trade_suggestions_explanation_pending
ON trade_suggestions (created_at DESC)
WHERE llm_summary IS NULL
  AND status = 'active'
```

Grepping the entire codebase, **that index is never queried by anything** — no
reconciliation sweep or backfill job was ever built to use it. The schema anticipated
this failure mode; the recovery mechanism was never implemented.

## Bottom line

HESTERBIO itself is orphaned because of how it was created (a test script bypassing
Kafka init) — low real-world likelihood in normal operation since the live
backend/worker always initializes Kafka on startup. But the fact that a single
non-fatal publish failure can permanently strand a high-confidence suggestion with no
visible failure state and no automatic recovery is a genuine, previously-undetected gap
in the legacy explanation pipeline, evidenced by the unused index that was clearly meant
to fix exactly this.

## Not done (investigation only, per instruction)

No code changes were made. Candidate fixes for the systemic gap (not implemented):
1. A periodic reconciliation sweep using `idx_trade_suggestions_explanation_pending` to
   detect and re-publish orphaned active suggestions past some age threshold.
2. Have `ensure_explanation()` re-publish (with dedup) instead of blindly trusting the
   engine's original attempt succeeded, even in legacy mode.
