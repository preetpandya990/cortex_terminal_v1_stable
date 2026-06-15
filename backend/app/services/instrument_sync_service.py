"""
Cortex AI — Instrument Master Sync Service.
===========================================
In-process scheduler that keeps ``instrument_master`` reconciled against
Upstox's daily begin-of-day (BOD) instrument file.

Lifecycle mirrors ``SignalScheduler`` / ``MarketFeedService`` (``start`` /
``stop`` driving a single named ``asyncio.Task``), so it slots into the FastAPI
lifespan exactly like the other long-lived services.

Cadence
-------
Fires once per day at ``INSTRUMENT_SYNC_HOUR_IST`` (default 08:00 IST) on NSE
trading days — after Upstox's ~06:00 file refresh, before the 09:15 open.  Most
fires are cheap: the fetcher issues a conditional GET and the CDN answers 304,
so the run ends without touching the database.

On ``start`` it also schedules a one-off **catch-up** sync (fire-and-forget, so
a slow/unreachable Upstox can never block app startup) when the table is empty
or its data is older than ``INSTRUMENT_SYNC_STALE_HOURS`` — making a fresh clone
or deploy immediately usable.

Single-runner guarantee
-----------------------
The actual sync runs only while holding a PostgreSQL **session-level advisory
lock**.  A session-level (not transaction-level) lock is the correct primitive
here because one sync spans several transactions (snapshot → upsert+reconcile);
a transaction-scoped lock would not cover the whole operation.  The lock is held
on a dedicated connection for the run's duration and released in ``finally``;
PostgreSQL additionally drops it automatically if the backend connection dies,
so a crashed process can never strand the lock.  With a single worker this is
defence-in-depth; under horizontal scaling it is load-bearing — only one
replica ever performs the sync, the rest no-op instantly.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.exceptions import SyncSanityError
from app.models.upstox_data import InstrumentMaster
from app.services.data_ingestion import InstrumentSyncResult, sync_instrument_master
from app.services.instrument_fetch import fetch_instruments, persist_validators

logger = logging.getLogger(__name__)
settings = get_settings()

_IST = ZoneInfo("Asia/Kolkata")

# Stable 64-bit advisory-lock key derived from a namespace string, so the value
# is deterministic across processes/deploys and collision-resistant against
# other advisory-lock users in the same database.
_LOCK_NAMESPACE = "cortex:instrument_master_sync"
_LOCK_KEY = int.from_bytes(
    hashlib.blake2b(_LOCK_NAMESPACE.encode(), digest_size=8).digest(),
    "big",
    signed=True,
)

# Re-evaluate the sleep against the wall clock at least this often, so a system
# suspend/resume (common on dev hosts) cannot make us oversleep the daily slot.
_MAX_SLEEP_SECONDS = 3600.0


async def _try_advisory_lock(session: AsyncSession, key: int) -> bool:
    """Attempt to take the session-level advisory lock; return True on success."""
    return bool((await session.execute(select(func.pg_try_advisory_lock(key)))).scalar())


async def _advisory_unlock(session: AsyncSession, key: int) -> None:
    """Release the session-level advisory lock (no-op if not held)."""
    await session.execute(select(func.pg_advisory_unlock(key)))


class InstrumentSyncService:
    """Market-calendar-aware daily reconciler for ``instrument_master``.

    Args:
        db_factory: ``async_sessionmaker`` callable; each operation gets its own
            session.
        redis: ``redis.asyncio.Redis`` singleton, used for conditional-GET
            validators.
    """

    def __init__(self, db_factory, redis) -> None:
        self._db_factory = db_factory
        self._redis = redis
        self._task: asyncio.Task | None = None
        self._catch_up_task: asyncio.Task | None = None
        self._running = False

    # ── Lifecycle ───────────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop(), name="instrument_sync")
        # Fire-and-forget so a slow/unreachable upstream never blocks startup.
        self._catch_up_task = asyncio.create_task(
            self._startup_catch_up(), name="instrument_sync_catch_up"
        )
        logger.info(
            "InstrumentSyncService started — daily at %02d:00 IST on trading days",
            settings.INSTRUMENT_SYNC_HOUR_IST,
        )

    async def stop(self) -> None:
        self._running = False
        for task in (self._task, self._catch_up_task):
            if task is None:
                continue
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info("InstrumentSyncService stopped")

    # ── Scheduler loop ──────────────────────────────────────────────────────────
    async def _scheduler_loop(self) -> None:
        from app.services.market_calendar import nse_calendar

        try:
            while self._running:
                await self._sleep_until_next_run()
                if not self._running:
                    break

                await nse_calendar.refresh_if_needed()
                today_ist = datetime.now(_IST).date()
                if not nse_calendar.is_trading_day(today_ist):
                    logger.info("InstrumentSyncService: %s is not a trading day; skipping", today_ist)
                    continue

                # _run_once is fully self-contained (catches + records its own
                # failures) so a bad day never kills the loop.
                await self._run_once()
        except asyncio.CancelledError:
            logger.info("InstrumentSyncService loop cancelled")
        except Exception:  # last-resort guard — the loop must never die silently
            logger.error("InstrumentSyncService loop crashed", exc_info=True)

    def _next_run_at(self) -> datetime:
        """Absolute IST datetime of the next ``HOUR:00`` slot strictly in the future."""
        now_ist = datetime.now(_IST)
        target = now_ist.replace(
            hour=settings.INSTRUMENT_SYNC_HOUR_IST, minute=0, second=0, microsecond=0
        )
        if target <= now_ist:
            target += timedelta(days=1)
        return target

    async def _sleep_until_next_run(self) -> None:
        """Sleep until the next run slot, in capped chunks resilient to clock jumps."""
        target = self._next_run_at()
        while self._running:
            remaining = (target - datetime.now(_IST)).total_seconds()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, _MAX_SLEEP_SECONDS))

    # ── Startup catch-up ─────────────────────────────────────────────────────────
    async def _startup_catch_up(self) -> None:
        try:
            stale = await self._is_data_stale()
            if not stale:
                logger.info("InstrumentSyncService: instrument data is fresh; no catch-up needed")
                return
            logger.info("InstrumentSyncService: instrument data stale/empty — running catch-up sync")
            await self._run_once()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("InstrumentSyncService catch-up failed", exc_info=True)

    async def _is_data_stale(self) -> bool:
        """True when the table is empty or its newest watermark is too old."""
        async with self._db_factory() as session:
            last_seen = (
                await session.execute(select(func.max(InstrumentMaster.last_seen_at)))
            ).scalar()
        if last_seen is None:
            return True
        age_hours = (datetime.now(last_seen.tzinfo) - last_seen).total_seconds() / 3600.0
        return age_hours > settings.INSTRUMENT_SYNC_STALE_HOURS

    # ── Sync execution ────────────────────────────────────────────────────────────
    async def _run_once(self, *, force: bool = False) -> InstrumentSyncResult | None:
        """Run one sync under the advisory lock. Never raises; records its own metrics.

        Returns the ``InstrumentSyncResult`` on a sync that changed the table,
        or ``None`` when skipped (lock held elsewhere / 304 / handled failure).
        """
        async with self._db_factory() as lock_session:
            if not await _try_advisory_lock(lock_session, _LOCK_KEY):
                logger.debug("InstrumentSyncService: advisory lock held elsewhere; skipping run")
                return None
            try:
                return await self._do_sync(force=force)
            finally:
                await _advisory_unlock(lock_session, _LOCK_KEY)

    async def _do_sync(self, *, force: bool) -> InstrumentSyncResult | None:
        started = time.perf_counter()
        result_label = "error"
        try:
            fetched = await fetch_instruments(self._redis, force=force)
            if fetched.not_modified:
                result_label = "skipped_304"
                logger.info("InstrumentSyncService: file unchanged (304); nothing to do")
                return None

            async with self._db_factory() as session:
                result = await sync_instrument_master(session, fetched.instruments)

            # Persist validators only after a fully successful sync, so a file
            # that failed the sanity guard is never cached behind a future 304.
            await persist_validators(self._redis, fetched.etag, fetched.last_modified)

            metrics.instrument_active_count.set(result.active_after)
            metrics.instrument_delisted_count.set(result.total_after - result.active_after)
            metrics.instrument_sync_last_success_timestamp.set(time.time())
            result_label = "success"
            return result
        except SyncSanityError:
            result_label = "aborted_sanity"
            logger.error("InstrumentSyncService: sync aborted by sanity guard", exc_info=True)
            return None
        except Exception:
            result_label = "error"
            logger.error("InstrumentSyncService: sync failed", exc_info=True)
            return None
        finally:
            metrics.instrument_sync_total.labels(result=result_label).inc()
            metrics.instrument_sync_duration_seconds.observe(time.perf_counter() - started)
