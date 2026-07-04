"""
Cortex AI — AI Processing Safety Net
=====================================
Daily fallback sweep for the 3 demand-driven Tier-2 Gemini dispatch paths
(sentiment batching, event classification, news-forecast batching). When any
of the 3 ``*_AUTO_*`` flags in config.py is False, its queue only drains on
an explicit admin dispatch from the Worker Control Panel — this scheduler is
the safety net that ensures a queue never grows unbounded if no admin
dispatches manually.

Architecture
------------
Mirrors ``WatchlistContextScheduler`` exactly: pure asyncio while-True loop
with cooperative sleep via TriggerToken, same __slots__/constructor shape,
same pause/trigger/shutdown contract as every other native worker task. The
pure scheduling helpers (``_parse_run_times``, ``_seconds_until_next_run``)
are imported — not duplicated — from ``watchlist_context_scheduler``.

Unlike the watchlist scheduler, this task has **no market-hours guard**:
Gemini quota resets at midnight and dispatch decisions must be independent
of NSE trading hours (an overnight backlog should clear before market open,
not during it).

Per-category isolation
-----------------------
Each scheduled or triggered wake calls ``_run_batch()``, which independently
checks each of the 3 pending counts against its configured threshold and
dispatches only the categories over threshold. One category's failure
(exception, quota exhaustion) is caught and logged without preventing the
other two from being checked and dispatched — the same per-category
isolation as the admin "Dispatch All" action.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.metrics import (
    ai_processing_dispatch_total,
    ai_processing_safety_net_duration_seconds,
    ai_processing_safety_net_last_run_timestamp,
    ai_processing_safety_net_runs_total,
)
from app.workers.supervisor import PauseToken, TriggerToken
from app.workers.watchlist_context_scheduler import (
    _now_ist,
    _parse_run_times,
    _seconds_until_next_run,
)

logger = logging.getLogger(__name__)

# Polling cadence when recalculating the next scheduled run time — same
# cadence as the watchlist scheduler for consistent control-plane latency.
_SCHEDULE_POLL_SECS: float = 30.0


class AIProcessingSafetyNet:
    """
    Daily fallback dispatcher for the 3 demand-driven AI processing queues.

    Registered as a supervised() task in the worker registry under the name
    "ai_processing_safety_net". Responds to PauseToken (pause/resume) and
    TriggerToken (immediate run) from the control plane — the existing
    generic ``POST /tasks/{name}/trigger`` endpoint gives the admin a
    "force safety net now" action with no new endpoint code.

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
        """
        Main task loop. Runs until the shutdown event is set.

        Each iteration:
          1. Checks the pause gate (blocks if paused by the control plane).
          2. Calculates seconds until the next scheduled IST run time.
          3. Sleeps via TriggerToken.wait_or_timeout() — wakes early on trigger.
          4. Delegates to _run_batch() — no market-hours guard (see module docstring).
        """
        settings = get_settings()
        run_times = _parse_run_times([settings.AI_SAFETY_NET_RUN_TIME_IST])

        if not run_times:
            logger.error(
                "ai_processing_safety_net: no valid run time configured — task "
                "exiting. Fix AI_SAFETY_NET_RUN_TIME_IST in .env and restart the worker."
            )
            return

        logger.info(
            "ai_processing_safety_net: started — schedule=%s IST",
            settings.AI_SAFETY_NET_RUN_TIME_IST,
        )

        while not self._shutdown.is_set():
            await self._pause.checkpoint()

            now_ist = _now_ist()
            secs = _seconds_until_next_run(now_ist, run_times)

            logger.debug(
                "ai_processing_safety_net: next sweep in %.0fs (at ~%s IST)",
                secs,
                now_ist.strftime("%H:%M"),
            )

            triggered = await self._trigger.wait_or_timeout(max(secs, _SCHEDULE_POLL_SECS))

            if self._shutdown.is_set():
                break

            await self._run_batch(triggered=triggered)

            if self._on_cycle is not None:
                self._on_cycle()

        logger.info("ai_processing_safety_net: exiting cleanly (shutdown signalled)")

    async def _run_batch(self, *, triggered: bool = False) -> None:
        """
        Check each of the 3 pending counts against its threshold and dispatch
        only the categories over threshold, independently. One category's
        failure never blocks the other two.
        """
        t0 = time.monotonic()
        settings = get_settings()
        run_kind = "triggered" if triggered else "scheduled"

        logger.info("ai_processing_safety_net: %s sweep starting", run_kind)

        checked = 0
        dispatched_categories: list[str] = []
        had_error = False

        for category, check in (
            ("sentiment", self._check_sentiment),
            ("classification", self._check_classification),
            ("forecast", self._check_forecast),
        ):
            checked += 1
            try:
                dispatched = await check(settings)
                if dispatched:
                    dispatched_categories.append(category)
            except Exception as exc:
                had_error = True
                ai_processing_dispatch_total.labels(
                    category=category, trigger_source="scheduled", outcome="error"
                ).inc()
                logger.error(
                    "ai_processing_safety_net: %s dispatch failed — %s",
                    category, exc, exc_info=True,
                )

        duration = time.monotonic() - t0
        ai_processing_safety_net_runs_total.labels(
            status="error" if had_error else "success"
        ).inc()
        ai_processing_safety_net_duration_seconds.observe(duration)
        ai_processing_safety_net_last_run_timestamp.set(time.time())

        logger.info(
            "ai_processing_safety_net: %s sweep complete — checked=%d dispatched=%s "
            "duration_ms=%d",
            run_kind, checked, dispatched_categories or "none", int(duration * 1000),
        )

    async def _check_sentiment(self, settings: Any) -> bool:
        """Dispatch the sentiment queue if it's over threshold. Returns True if dispatched."""
        from app.ai.intelligence.nlp_engine import NLPEngine

        pending = await NLPEngine.pending_sentiment_count()
        if pending < settings.AI_SAFETY_NET_SENTIMENT_THRESHOLD:
            return False

        logger.info(
            "ai_processing_safety_net: sentiment queue depth %d >= threshold %d — dispatching",
            pending, settings.AI_SAFETY_NET_SENTIMENT_THRESHOLD,
        )
        result = await NLPEngine.flush_pending_sentiment()
        ai_processing_dispatch_total.labels(
            category="sentiment", trigger_source="scheduled", outcome="success"
        ).inc()
        logger.info("ai_processing_safety_net: sentiment dispatch result=%s", result)
        return True

    async def _check_forecast(self, settings: Any) -> bool:
        """Dispatch the forecast queue if it's over threshold. Returns True if dispatched."""
        from app.ai.fusion.forecast_batch_worker import (
            flush_pending_forecasts,
            pending_forecast_count,
        )

        pending = await pending_forecast_count()
        if pending < settings.AI_SAFETY_NET_FORECAST_THRESHOLD:
            return False

        logger.info(
            "ai_processing_safety_net: forecast queue depth %d >= threshold %d — dispatching",
            pending, settings.AI_SAFETY_NET_FORECAST_THRESHOLD,
        )
        result = await flush_pending_forecasts(self._redis, self._session_factory)
        ai_processing_dispatch_total.labels(
            category="forecast", trigger_source="scheduled", outcome="success"
        ).inc()
        logger.info("ai_processing_safety_net: forecast dispatch result=%s", result)
        return True

    async def _check_classification(self, settings: Any) -> bool:
        """Dispatch the classification queue if it's over threshold. Returns True if dispatched."""
        from app.ai.intelligence.event_classifier import EventClassifier

        classifier = EventClassifier(use_llm=True)
        pending = await classifier.pending_classification_count()
        if pending < settings.AI_SAFETY_NET_EVENTS_THRESHOLD:
            return False

        logger.info(
            "ai_processing_safety_net: classification queue depth %d >= threshold %d — "
            "dispatching",
            pending, settings.AI_SAFETY_NET_EVENTS_THRESHOLD,
        )
        result = await classifier.flush_pending_classifications(
            self._session_factory
        )
        ai_processing_dispatch_total.labels(
            category="classification", trigger_source="scheduled", outcome="success"
        ).inc()
        logger.info("ai_processing_safety_net: classification dispatch result=%s", result)
        return True
