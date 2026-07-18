"""
Tests for ExplanationReconciliationSweep — the periodic backstop that
republishes orphaned legacy-mode explanation jobs.

Covers:
  - On-demand mode: the sweep parks without polling (out of scope).
  - The sweep query surfaces candidates and republishes each independently.
  - claim_and_publish's "published" flag (not "status") drives the republish
    count/metric — a suggestion whose lock is already held (still
    legitimately retrying) must NOT be double-counted as republished.
  - One candidate's exception never blocks the rest of the batch.
  - _run_sweep() records success/error metrics and never raises itself.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.workers.explanation_reconciliation_sweep import ExplanationReconciliationSweep
from app.workers.supervisor import PauseToken, TriggerToken


def _suggestion(consensus_score: str = "82.0") -> MagicMock:
    s = MagicMock()
    s.suggestion_id = uuid4()
    s.id = 1
    s.instrument_key = "NSE_EQ|INE002A01018"
    s.symbol = "RELIANCE"
    s.consensus_score = Decimal(consensus_score)
    return s


def _session_factory(candidates: list) -> MagicMock:
    """A session_factory whose `async with factory() as db: db.execute(...)`
    returns `candidates` via `.scalars().all()`, matching the real
    AsyncSession.execute(select(...)) shape."""
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = candidates
    db.execute = AsyncMock(return_value=execute_result)

    @asynccontextmanager
    async def _ctx():
        yield db

    factory = MagicMock(side_effect=_ctx)
    return factory


@pytest.fixture
def mock_settings():
    return MagicMock(
        EXPLANATION_ON_DEMAND=False,
        EXPLANATION_RECONCILE_STALENESS_SECS=300,
        EXPLANATION_RECONCILE_SWEEP_INTERVAL_SECS=120,
        EXPLANATION_CONSENSUS_THRESHOLD=75.0,
    )


def _sweep(candidates: list) -> ExplanationReconciliationSweep:
    return ExplanationReconciliationSweep(
        session_factory=_session_factory(candidates),
        redis=AsyncMock(),
        shutdown=asyncio.Event(),
        pause=PauseToken(),
        trigger=TriggerToken(),
    )


# ── Scope guard ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_parks_without_polling_in_on_demand_mode():
    settings = MagicMock(EXPLANATION_ON_DEMAND=True)
    shutdown = asyncio.Event()
    sweep = ExplanationReconciliationSweep(
        session_factory=MagicMock(),
        redis=AsyncMock(),
        shutdown=shutdown,
        pause=PauseToken(),
        trigger=TriggerToken(),
    )
    with patch(
        "app.workers.explanation_reconciliation_sweep.get_settings",
        return_value=settings,
    ):
        task = asyncio.create_task(sweep.run())
        await asyncio.sleep(0.05)
        assert not task.done()  # parked on shutdown.wait(), not polling
        shutdown.set()
        await asyncio.wait_for(task, timeout=1.0)


# ── Republish accounting ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_republishes_orphaned_candidate(mock_settings):
    suggestion = _suggestion()
    sweep = _sweep([suggestion])

    with (
        patch(
            "app.workers.explanation_reconciliation_sweep.get_settings",
            return_value=mock_settings,
        ),
        patch(
            "app.ai.intelligence.explanation_service.claim_and_publish",
            AsyncMock(return_value={"status": "generating", "published": True}),
        ) as mock_claim,
    ):
        await sweep._run_sweep(triggered=True)

    mock_claim.assert_awaited_once()
    args, kwargs = mock_claim.await_args
    assert kwargs["trigger"] == "reconciliation"


@pytest.mark.asyncio
async def test_lock_already_held_is_not_counted_as_republished(mock_settings):
    """The candidate is genuinely still retrying (worker holds the lock) —
    claim_and_publish returns published=False and must not be counted."""
    suggestion = _suggestion()
    sweep = _sweep([suggestion])

    from app.core.metrics import explanation_reconciliation_republish_total
    before = explanation_reconciliation_republish_total.labels(
        trigger_source="sweep"
    )._value.get()

    with (
        patch(
            "app.workers.explanation_reconciliation_sweep.get_settings",
            return_value=mock_settings,
        ),
        patch(
            "app.ai.intelligence.explanation_service.claim_and_publish",
            AsyncMock(return_value={"status": "generating", "published": False}),
        ),
    ):
        await sweep._run_sweep(triggered=True)

    after = explanation_reconciliation_republish_total.labels(
        trigger_source="sweep"
    )._value.get()
    assert after == before  # unchanged — nothing was actually republished


@pytest.mark.asyncio
async def test_one_candidate_failure_does_not_block_the_rest(mock_settings):
    s1, s2 = _suggestion(), _suggestion()
    sweep = _sweep([s1, s2])

    async def _side_effect(redis, suggestion, *, trigger, log_context):
        if suggestion is s1:
            raise RuntimeError("kafka down")
        return {"status": "generating", "published": True}

    with (
        patch(
            "app.workers.explanation_reconciliation_sweep.get_settings",
            return_value=mock_settings,
        ),
        patch(
            "app.ai.intelligence.explanation_service.claim_and_publish",
            AsyncMock(side_effect=_side_effect),
        ) as mock_claim,
    ):
        await sweep._run_sweep(triggered=True)  # must not raise

    assert mock_claim.await_count == 2


@pytest.mark.asyncio
async def test_no_candidates_is_a_success_run_with_no_republish(mock_settings):
    sweep = _sweep([])

    from app.core.metrics import explanation_reconciliation_sweep_runs_total
    before = explanation_reconciliation_sweep_runs_total.labels(status="success")._value.get()

    with (
        patch(
            "app.workers.explanation_reconciliation_sweep.get_settings",
            return_value=mock_settings,
        ),
        patch(
            "app.ai.intelligence.explanation_service.claim_and_publish",
            AsyncMock(),
        ) as mock_claim,
    ):
        await sweep._run_sweep(triggered=True)

    after = explanation_reconciliation_sweep_runs_total.labels(status="success")._value.get()
    assert after == before + 1
    mock_claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_failure_records_error_metric_and_never_raises(mock_settings):
    from app.core.metrics import explanation_reconciliation_sweep_runs_total

    broken_factory = MagicMock(side_effect=RuntimeError("db pool exhausted"))
    sweep = ExplanationReconciliationSweep(
        session_factory=broken_factory,
        redis=AsyncMock(),
        shutdown=asyncio.Event(),
        pause=PauseToken(),
        trigger=TriggerToken(),
    )

    before = explanation_reconciliation_sweep_runs_total.labels(status="error")._value.get()

    with patch(
        "app.workers.explanation_reconciliation_sweep.get_settings",
        return_value=mock_settings,
    ):
        await sweep._run_sweep(triggered=True)  # must not raise

    after = explanation_reconciliation_sweep_runs_total.labels(status="error")._value.get()
    assert after == before + 1
