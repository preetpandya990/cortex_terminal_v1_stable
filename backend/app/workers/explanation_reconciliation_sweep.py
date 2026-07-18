"""
Cortex AI — Explanation Reconciliation Sweep
=============================================
Periodic backstop for orphaned legacy-mode (EXPLANATION_ON_DEMAND=False)
explanation jobs. The correlation engine auto-publishes a job as a
best-effort side effect after committing a trade suggestion; that publish is
deliberately non-fatal (a broker hiccup must never roll back a committed
suggestion), so it can silently fail and leave the suggestion stuck with
``llm_summary IS NULL`` forever — an infinite "generating" skeleton in the
UI with no failure state and no automatic recovery.

``explanation_service.ensure_explanation()`` already self-heals this on
read (see its module docstring), but that only fires when a user actually
views the affected suggestion's panel. This sweep is the guaranteed
backstop for suggestions nobody views: every
``EXPLANATION_RECONCILE_SWEEP_INTERVAL_SECS`` it queries
``idx_trade_suggestions_explanation_pending`` (migration 0042) for active,
unexplained, sufficiently-stale suggestions and republishes each one,
sharing the exact same claim-and-publish path (and the same
``EXPLANATION_INFLIGHT_KEY`` race guard against a suggestion that's still
legitimately retrying) as the on-read self-heal branch.

Architecture
------------
Same __slots__/constructor/pause/trigger/shutdown contract as every other
native worker task (mirrors ``AIProcessingSafetyNet``), but a fixed-interval
loop rather than a once-daily IST-scheduled-time loop — this is a tight
correctness backstop, not a daily batch job.

Scope: only runs when ``not settings.EXPLANATION_ON_DEMAND``. On-demand
mode's "nobody viewed it yet, so no job was ever needed" laziness is
intentional design, not a bug — out of scope for this sweep.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.metrics import (
    explanation_reconciliation_pending_gauge,
    explanation_reconciliation_republish_total,
    explanation_reconciliation_sweep_duration_seconds,
    explanation_reconciliation_sweep_last_run_timestamp,
    explanation_reconciliation_sweep_runs_total,
)
from app.models.trade_suggestions import TradeSuggestion
from app.workers.supervisor import PauseToken, TriggerToken

logger = logging.getLogger(__name__)


class ExplanationReconciliationSweep:
    """
    Fixed-interval backstop that republishes orphaned legacy-mode
    explanation jobs.

    Registered as a supervised() task in the worker registry under the name
    "explanation_reconciliation_sweep". Responds to PauseToken (pause/resume)
    and TriggerToken (immediate run) from the control plane, same as every
    other native worker task.

    Instantiated once at registry build time; `run()` returns a fresh
    coroutine on each supervisor restart, so no mutable state persists
    across restarts.
    """

    __slots__ = (
        "_session_factory",
        "_redis",
        "_shutdown",
        "_pause",
        "_trigger",
        "_on_cycle",
    )

    def __init__(
        self,
        session_factory: async_sessionmaker,
        redis: Any,
        shutdown: asyncio.Event,
        pause: PauseToken,
        trigger: TriggerToken,
        on_cycle: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._shutdown = shutdown
        self._pause = pause
        self._trigger = trigger
        self._on_cycle = on_cycle

    async def run(self) -> None:
        """Main task loop. Runs until the shutdown event is set."""
        settings = get_settings()

        if settings.EXPLANATION_ON_DEMAND:
            logger.info(
                "explanation_reconciliation_sweep: EXPLANATION_ON_DEMAND=True "
                "— sweep is a legacy-mode-only backstop, parking without polling."
            )
            await self._shutdown.wait()
            return

        interval = settings.EXPLANATION_RECONCILE_SWEEP_INTERVAL_SECS
        logger.info(
            "explanation_reconciliation_sweep: started — interval=%ds staleness=%ds",
            interval, settings.EXPLANATION_RECONCILE_STALENESS_SECS,
        )

        while not self._shutdown.is_set():
            await self._pause.checkpoint()

            triggered = await self._trigger.wait_or_timeout(interval)
            if self._shutdown.is_set():
                break

            await self._run_sweep(triggered=triggered)

            if self._on_cycle is not None:
                self._on_cycle()

        logger.info("explanation_reconciliation_sweep: exiting cleanly (shutdown signalled)")

    async def _run_sweep(self, *, triggered: bool = False) -> None:
        """
        Query for orphaned suggestions and republish each one, independently
        of the others (one failure never blocks the rest of the batch).
        """
        # Lazy import: avoids a hard dependency on explanation_service (and,
        # transitively, the Kafka client) at worker module-import time.
        from app.ai.intelligence.explanation_service import claim_and_publish

        t0 = time.monotonic()
        settings = get_settings()
        run_kind = "triggered" if triggered else "scheduled"
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=settings.EXPLANATION_RECONCILE_STALENESS_SECS
        )

        republished = 0
        had_error = False

        try:
            async with self._session_factory() as db:
                stmt = (
                    select(TradeSuggestion)
                    .where(
                        TradeSuggestion.status == "active",
                        TradeSuggestion.llm_summary.is_(None),
                        TradeSuggestion.created_at < cutoff,
                        TradeSuggestion.consensus_score
                        >= settings.EXPLANATION_CONSENSUS_THRESHOLD,
                    )
                    .order_by(TradeSuggestion.created_at.asc())
                )
                candidates = (await db.execute(stmt)).scalars().all()

            explanation_reconciliation_pending_gauge.set(len(candidates))

            for suggestion in candidates:
                try:
                    result = await claim_and_publish(
                        self._redis, suggestion,
                        trigger="reconciliation", log_context="sweep",
                    )
                    if result.get("published"):
                        republished += 1
                        explanation_reconciliation_republish_total.labels(
                            trigger_source="sweep"
                        ).inc()
                except Exception as exc:
                    had_error = True
                    logger.error(
                        "explanation_reconciliation_sweep: republish failed for "
                        "suggestion %s: %s",
                        suggestion.suggestion_id, exc, exc_info=True,
                    )
        except Exception as exc:
            had_error = True
            logger.error(
                "explanation_reconciliation_sweep: %s sweep query failed: %s",
                run_kind, exc, exc_info=True,
            )

        duration = time.monotonic() - t0
        explanation_reconciliation_sweep_runs_total.labels(
            status="error" if had_error else "success"
        ).inc()
        explanation_reconciliation_sweep_duration_seconds.observe(duration)
        explanation_reconciliation_sweep_last_run_timestamp.set(time.time())

        if republished:
            logger.warning(
                "explanation_reconciliation_sweep: %s sweep complete — "
                "republished=%d duration_ms=%d",
                run_kind, republished, int(duration * 1000),
            )
        else:
            logger.debug(
                "explanation_reconciliation_sweep: %s sweep complete — "
                "nothing to republish duration_ms=%d",
                run_kind, int(duration * 1000),
            )
