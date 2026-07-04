"""
Cortex AI — Watchlist Context Scheduler
========================================
Pre-warms AI market context for all instruments held in any user's watchlist,
running 4× per NSE trading day at configurable fixed IST wall-clock times.

Architecture
------------
Pure asyncio while-True loop with cooperative sleep via TriggerToken —
the same pattern used by FundamentalsRefreshScheduler and every other
native worker in this codebase.  The scheduler reads all distinct
instrument_keys from watchlist_items, skips instruments whose context is
still fresh, and XADDs the remainder to cortex:stream:context:jobs with
force="1" so the context_worker regenerates even if unexpired context exists.

Scheduling
----------
Run times are configured via WATCHLIST_SCHEDULER_RUN_TIMES_IST (HH:MM list).
Between runs the task sleeps via TriggerToken.wait_or_timeout(), so the
control-plane can fire an immediate batch at any time via the admin trigger
endpoint without a code change or restart.

Market hours guard
------------------
Scheduled runs only fire during NSE market hours (NSE_MARKET_OPEN_IST to
NSE_MARKET_CLOSE_IST).  Admin-triggered runs (triggered=True) bypass this
guard so operators can force a refresh at any time for diagnostic purposes.

Freshness margin
----------------
An instrument's context is re-enqueued only if it expires within
WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES minutes.  This prevents
redundant Gemini calls for instruments that were recently refreshed by
the SSE pipeline (Stage 3 in ai_stream.py).

Safety
------
- WATCHLIST_SCHEDULER_BATCH_CAP hard-limits instruments per run; surplus
  deferred to the next scheduled slot.
- shutdown event is checked before every sleep boundary.
- All exceptions are caught; a failed batch does not crash the task.
- Prometheus metrics surface run count, enqueued count, duration, and
  last-run timestamp for Grafana alerting.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.metrics import (
    watchlist_scheduler_duration_seconds,
    watchlist_scheduler_instruments_queued_total,
    watchlist_scheduler_last_run_timestamp,
    watchlist_scheduler_runs_total,
)
from app.core.kafka import KafkaTopics, publish as kafka_publish
from app.workers.supervisor import PauseToken, TriggerToken

logger = logging.getLogger(__name__)

# ── Module-level constants ─────────────────────────────────────────────────────

# Fixed IST UTC offset (UTC+5:30).
_IST_TZ = timezone(timedelta(hours=5, minutes=30))

# Polling cadence when recalculating the next scheduled run time.
_SCHEDULE_POLL_SECS: float = 30.0


# ── Pure helpers ───────────────────────────────────────────────────────────────

def _now_ist() -> datetime:
    """Current wall-clock time as a timezone-aware datetime in IST."""
    return datetime.now(timezone.utc).astimezone(_IST_TZ)


def _parse_run_times(times_ist: list[str]) -> list[tuple[int, int]]:
    """
    Parse a list of "HH:MM" strings into sorted (hour, minute) tuples.
    Malformed entries are logged and silently dropped.
    """
    parsed: list[tuple[int, int]] = []
    for t in times_ist:
        try:
            h_str, m_str = t.split(":")
            parsed.append((int(h_str), int(m_str)))
        except (ValueError, AttributeError):
            logger.warning(
                "watchlist_scheduler: ignoring malformed run time %r "
                "(expected HH:MM format)",
                t,
            )
    return sorted(parsed)


def _is_market_hours(now_ist: datetime) -> bool:
    """Return True when *now_ist* falls within NSE market hours (inclusive)."""
    settings   = get_settings()
    open_h,  open_m  = map(int, settings.NSE_MARKET_OPEN_IST.split(":"))
    close_h, close_m = map(int, settings.NSE_MARKET_CLOSE_IST.split(":"))
    now_min   = now_ist.hour * 60 + now_ist.minute
    open_min  = open_h  * 60 + open_m
    close_min = close_h * 60 + close_m
    return open_min <= now_min <= close_min


def _seconds_until_next_run(
    now_ist: datetime,
    run_times: list[tuple[int, int]],
) -> float:
    """
    Return seconds from *now_ist* until the next scheduled run time.

    If all of today's run times have passed, wraps to the first slot tomorrow.
    Always returns a positive value (>= 0.0).
    """
    today      = now_ist.date()
    candidates: list[datetime] = []

    for h, m in run_times:
        candidate = datetime(
            today.year, today.month, today.day,
            h, m, 0,
            tzinfo=_IST_TZ,
        )
        if candidate > now_ist:
            candidates.append(candidate)

    if not candidates:
        # All today's slots are past — schedule for the first slot tomorrow.
        tomorrow = today + timedelta(days=1)
        h, m = run_times[0]
        candidates.append(
            datetime(tomorrow.year, tomorrow.month, tomorrow.day, h, m, 0, tzinfo=_IST_TZ)
        )

    return max(0.0, (min(candidates) - now_ist).total_seconds())


# ── Scheduler class ────────────────────────────────────────────────────────────

class WatchlistContextScheduler:
    """
    Pre-warm watchlist instrument contexts on a fixed intraday schedule.

    Registered as a supervised() task in the worker registry under the name
    "watchlist_scheduler".  Responds to PauseToken (pause/resume) and
    TriggerToken (immediate run) from the control plane — the same contract
    as all native worker tasks (heartbeat, suggestion_expiry, etc.).

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
        self._redis            = redis
        self._shutdown         = shutdown
        self._pause            = pause
        self._trigger          = trigger
        self._on_cycle         = on_cycle

    async def run(self) -> None:
        """
        Main task loop.  Runs until the shutdown event is set.

        Each iteration:
          1. Checks the pause gate (blocks if paused by the control plane).
          2. Calculates seconds until the next scheduled IST run time.
          3. Sleeps via TriggerToken.wait_or_timeout() — wakes early on trigger.
          4. Applies the market-hours guard (scheduled runs only).
          5. Delegates to _run_batch().
        """
        settings  = get_settings()
        run_times = _parse_run_times(settings.WATCHLIST_SCHEDULER_RUN_TIMES_IST)

        if not run_times:
            logger.error(
                "watchlist_scheduler: no valid run times configured — task exiting. "
                "Fix WATCHLIST_SCHEDULER_RUN_TIMES_IST in .env and restart the worker."
            )
            return

        logger.info(
            "watchlist_scheduler: started — schedule=%s IST",
            settings.WATCHLIST_SCHEDULER_RUN_TIMES_IST,
        )

        while not self._shutdown.is_set():
            await self._pause.checkpoint()

            now_ist = _now_ist()
            secs    = _seconds_until_next_run(now_ist, run_times)

            logger.debug(
                "watchlist_scheduler: next run in %.0fs "
                "(at ~%s IST)",
                secs,
                (now_ist + timedelta(seconds=secs)).strftime("%H:%M"),
            )

            # Sleep until the next slot, waking early if triggered by the control plane.
            # Guard against zero-second sleeps (e.g. scheduler fires exactly on the slot).
            triggered = await self._trigger.wait_or_timeout(max(secs, _SCHEDULE_POLL_SECS))

            if self._shutdown.is_set():
                break

            # Market hours guard: skip scheduled runs outside market hours.
            # Admin-triggered runs (triggered=True) bypass this check.
            if not triggered and not _is_market_hours(_now_ist()):
                logger.debug(
                    "watchlist_scheduler: outside market hours — skipping scheduled run"
                )
                continue

            await self._run_batch(triggered=triggered)

            if self._on_cycle is not None:
                self._on_cycle()

        logger.info("watchlist_scheduler: exiting cleanly (shutdown signalled)")

    async def _run_batch(self, *, triggered: bool = False) -> None:
        """
        Query all watchlist instruments, identify stale ones, and enqueue
        context regeneration jobs on cortex:stream:context:jobs.

        Args:
            triggered: True when the run was triggered by the control plane
                       rather than the scheduled timer.  Used for logging only.
        """
        t0       = time.monotonic()
        settings = get_settings()
        margin   = timedelta(minutes=settings.WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES)
        cap      = settings.WATCHLIST_SCHEDULER_BATCH_CAP
        now_utc  = datetime.now(timezone.utc)
        run_kind = "triggered" if triggered else "scheduled"

        logger.info(
            "watchlist_scheduler: %s batch starting at %s IST",
            run_kind,
            _now_ist().strftime("%H:%M"),
        )

        try:
            # ── 1. Fetch all distinct watchlist instrument keys ───────────────
            async with self._session_factory() as db:
                rows     = (await db.execute(
                    text("SELECT DISTINCT instrument_key FROM watchlist_items")
                )).all()
            all_keys: list[str] = [r[0] for r in rows if r[0]]

            if not all_keys:
                logger.info(
                    "watchlist_scheduler: no watchlist instruments found — skipping batch"
                )
                watchlist_scheduler_runs_total.labels(status="skipped_empty").inc()
                watchlist_scheduler_last_run_timestamp.set(now_utc.timestamp())
                return

            # ── 2. Identify instruments whose context is still fresh ──────────
            from app.ai.fusion.models import AIInstrumentContext  # lazy — avoids circular dep

            fresh_cutoff = now_utc + margin
            async with self._session_factory() as db:
                fresh_result = await db.execute(
                    select(AIInstrumentContext.instrument_key).where(
                        AIInstrumentContext.expires_at > fresh_cutoff
                    )
                )
            fresh_keys: set[str] = {r[0] for r in fresh_result.all()}

            stale_keys = [k for k in all_keys if k not in fresh_keys]

            if not stale_keys:
                logger.info(
                    "watchlist_scheduler: all %d instruments are fresh "
                    "(within %d-min margin) — no jobs enqueued",
                    len(all_keys),
                    settings.WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES,
                )
                watchlist_scheduler_runs_total.labels(status="success").inc()
                watchlist_scheduler_last_run_timestamp.set(now_utc.timestamp())
                return

            to_enqueue = stale_keys[:cap]
            if len(stale_keys) > cap:
                logger.warning(
                    "watchlist_scheduler: %d stale instruments exceeds batch cap %d — "
                    "enqueuing first %d; remainder deferred to the next scheduled run",
                    len(stale_keys), cap, cap,
                )

            # ── 3. Enqueue stale instruments on the context topic ─────────────
            enqueued = 0
            for instrument_key in to_enqueue:
                symbol = (
                    instrument_key.split("|")[-1]
                    if "|" in instrument_key
                    else instrument_key
                )
                try:
                    await kafka_publish(
                        KafkaTopics.CONTEXT_JOBS,
                        {
                            "instrument_key":  instrument_key,
                            "symbol":          symbol,
                            "prediction_data": "",
                            "lock_key":        "",
                            "lock_token":      "",
                            "force":           "1",
                            "source":          "watchlist_scheduler",
                        },
                        key=instrument_key,
                    )
                    enqueued += 1
                except Exception as exc:
                    logger.warning(
                        "watchlist_scheduler: publish failed for instrument=%s: %s",
                        instrument_key,
                        exc,
                    )

            # ── 4. Metrics + summary log ──────────────────────────────────────
            duration = time.monotonic() - t0
            watchlist_scheduler_runs_total.labels(status="success").inc()
            watchlist_scheduler_instruments_queued_total.inc(enqueued)
            watchlist_scheduler_duration_seconds.observe(duration)
            watchlist_scheduler_last_run_timestamp.set(now_utc.timestamp())

            logger.info(
                "watchlist_scheduler: %s batch complete — "
                "total=%d fresh=%d stale=%d enqueued=%d skipped=%d duration_ms=%d",
                run_kind,
                len(all_keys),
                len(fresh_keys),
                len(stale_keys),
                enqueued,
                len(stale_keys) - enqueued,
                int(duration * 1000),
            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            watchlist_scheduler_runs_total.labels(status="error").inc()
            logger.error(
                "watchlist_scheduler: batch run failed: %s",
                exc,
                exc_info=True,
            )
