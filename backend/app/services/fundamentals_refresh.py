"""
Cortex AI — Fundamentals Refresh Scheduler
==========================================
Background scheduler that keeps company fundamentals data current by running
six purpose-built loops with different cadences, scopes, and endpoint targets.

Loop summary
------------
  daily_ratios     — key-ratios for all EQ instruments, 15:45 IST every trading day
  corp_actions     — corporate-actions for all EQ instruments, 18:30 IST every day
  financials       — income-statement + balance-sheet + cash-flow, triggered on
                     SEBI Regulation 33 reporting deadlines (quarterly)
  share_holdings   — share-holdings, triggered on SEBI Regulation 31 deadlines
  priority_refresh — full 9-endpoint refresh for Tier 1 (high-activity) instruments,
                     every 6 hours; covers SIGNAL_SCHEDULED_UNIVERSE + recently
                     fetched instruments
  nightly_universe — full 9-endpoint refresh for all EQ instruments at 01:00 IST;
                     skips instruments that still have a live Redis cache entry

Architecture notes
------------------
- All loops share the global _rate_lock in fundamentals_service so they
  serialise correctly against each other and against on-demand API traffic.
  No separate rate accounting is needed here.
- A per-instrument Redis NX lock (cai:fundamentals:refresh:{key}, TTL 90s)
  prevents two scheduler loops from refreshing the same instrument concurrently.
- Batch concurrency (_BATCH_CONCURRENCY = 5) controls how many instruments
  are in-flight simultaneously. At 1.0 req/s this only affects queue depth,
  not throughput, but it keeps memory bounded.
- Per-loop Redis epoch keys (cai:fundamentals:sched:{loop}:last_run) record
  the last wall-clock time a loop ran, enabling resume-without-repeat after
  a worker restart.
- SEBI deadline calendar is hardcoded; the four regulatory quarters are
  fixed each financial year and do not change.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Coroutine

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.redis import CacheService
from app.models.fundamentals import CompanyFundamentalsProfile
from app.models.upstox_data import InstrumentMaster
from app.services.fundamentals_service import (
    extract_isin,
    fetch_and_store_all,
    fetch_and_store_corporate_actions,
    fetch_and_store_financials,
    fetch_and_store_key_ratios,
    fetch_and_store_share_holdings,
    _redis_key,
)
from app.services.market_calendar import IST, nse_calendar

logger   = logging.getLogger(__name__)
settings = get_settings()

# ── Types ──────────────────────────────────────────────────────────────────────

# Signature shared by all targeted fetch helpers and the full-fetch wrapper.
FetchFn = Callable[
    [str, str, AsyncSession, CacheService],
    Coroutine[Any, Any, None],
]

# ── Constants ──────────────────────────────────────────────────────────────────

_BATCH_CONCURRENCY      = 5           # instruments in-flight concurrently
_PRIORITY_INTERVAL_SECS = 6 * 3600   # 6 h between Tier 1 priority refreshes
_REFRESH_LOCK_PREFIX    = "cai:fundamentals:refresh:"
_REFRESH_LOCK_TTL       = 90          # seconds; covers worst-case single-endpoint latency
_EPOCH_KEY_PREFIX       = "cai:fundamentals:sched:"
_EPOCH_TTL              = 7 * 86_400  # keep epoch records for 7 days

# SEBI Regulation 33: listed companies must publish quarterly standalone results
# by these dates each calendar year.
# Q1 (Apr–Jun) → 14 Aug | Q2 (Jul–Sep) → 14 Nov | Q3 (Oct–Dec) → 14 Feb | Q4 (Jan–Mar) → 30 May
_SEBI_FINANCIALS_DEADLINES: tuple[tuple[int, int], ...] = (
    (8, 14),
    (11, 14),
    (2, 14),
    (5, 30),
)

# SEBI Regulation 31: share-holding disclosures due by these dates.
# Q1 → 21 Jul | Q2 → 21 Oct | Q3 → 21 Jan | Q4 → 21 Apr
_SEBI_HOLDINGS_DEADLINES: tuple[tuple[int, int], ...] = (
    (7, 21),
    (10, 21),
    (1, 21),
    (4, 21),
)

# Sentinel: raised inside a fetch_fn to signal a deliberate skip (not an error).
# _run_batch catches this and increments `skip` rather than `error`.
class _SkipRefresh(Exception):
    pass


# ── Helper utilities ───────────────────────────────────────────────────────────

def _next_deadline(deadlines: tuple[tuple[int, int], ...], after: date) -> date:
    """Return the nearest future (month, day) deadline strictly after `after`."""
    year = after.year
    candidates: list[date] = []
    for m, d in deadlines:
        for y in (year, year + 1):
            try:
                candidates.append(date(y, m, d))
            except ValueError:
                pass
    return min(c for c in candidates if c > after)


async def _wait_until(target_utc: datetime, shutdown: asyncio.Event) -> bool:
    """
    Sleep until `target_utc` or until shutdown fires.
    Returns True if woke naturally (time to run), False if shutdown.
    """
    delay = (target_utc - datetime.now(timezone.utc)).total_seconds()
    if delay <= 0:
        return True
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=delay)
        return False
    except asyncio.TimeoutError:
        return True


async def _wait_until_ist_time(hhmm: str, shutdown: asyncio.Event) -> bool:
    """Sleep until the next HH:MM IST occurrence (today or tomorrow)."""
    now_ist = datetime.now(IST)
    hh, mm  = map(int, hhmm.split(":"))
    target  = now_ist.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now_ist:
        target += timedelta(days=1)
    return await _wait_until(target.astimezone(timezone.utc), shutdown)


def _epoch_key(loop_name: str) -> str:
    return f"{_EPOCH_KEY_PREFIX}{loop_name}:last_run"


async def _record_loop_run(loop_name: str, redis: CacheService) -> None:
    try:
        await redis.set(_epoch_key(loop_name), datetime.now(timezone.utc).isoformat(), ttl=_EPOCH_TTL)
    except Exception as exc:
        logger.warning("[%s] Failed to record run epoch: %s", loop_name, exc)


async def _last_loop_run(loop_name: str, redis: CacheService) -> datetime | None:
    try:
        raw = await redis.get(_epoch_key(loop_name))
        if raw:
            val = raw if isinstance(raw, str) else raw.decode()
            return datetime.fromisoformat(val)
    except Exception:
        pass
    return None


async def _try_acquire_refresh_lock(instrument_key: str, raw_redis: Any) -> bool:
    """SET NX EX lock to prevent concurrent refresh of the same instrument across loops."""
    lock_key = f"{_REFRESH_LOCK_PREFIX}{instrument_key}"
    try:
        return bool(await raw_redis.set(lock_key, "1", nx=True, ex=_REFRESH_LOCK_TTL))
    except Exception:
        return True  # Redis unavailable — proceed without lock (safe degradation)


async def _release_refresh_lock(instrument_key: str, raw_redis: Any) -> None:
    try:
        await raw_redis.delete(f"{_REFRESH_LOCK_PREFIX}{instrument_key}")
    except Exception:
        pass


async def _load_all_eq_keys(session_factory: async_sessionmaker) -> list[str]:
    """Return all NSE EQ instrument_keys, alphabetically ordered."""
    async with session_factory() as db:
        result = await db.execute(
            select(InstrumentMaster.instrument_key)
            .where(InstrumentMaster.instrument_type == "EQ")
            .order_by(InstrumentMaster.instrument_key)
        )
        return [row[0] for row in result.all()]


async def _load_tier1_keys(
    session_factory: async_sessionmaker,
    lookback_days: int,
) -> list[str]:
    """
    Tier 1 = SIGNAL_SCHEDULED_UNIVERSE instruments ∪ instruments fetched in
    the last `lookback_days` days (proxy for user-active instruments).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    async with session_factory() as db:
        result = await db.execute(
            select(CompanyFundamentalsProfile.instrument_key)
            .where(CompanyFundamentalsProfile.fetched_at >= cutoff)
        )
        recently_fetched: set[str] = {row[0] for row in result.all()}

        universe_symbols = set(settings.SIGNAL_SCHEDULED_UNIVERSE)
        if universe_symbols:
            result = await db.execute(
                select(InstrumentMaster.instrument_key).where(
                    InstrumentMaster.instrument_type == "EQ",
                    InstrumentMaster.trading_symbol.in_(universe_symbols),
                )
            )
            scheduled: set[str] = {row[0] for row in result.all()}
        else:
            scheduled = set()

    combined = sorted(recently_fetched | scheduled)
    logger.debug(
        "Tier 1: %d instruments (%d scheduled + %d recently fetched)",
        len(combined), len(scheduled), len(recently_fetched),
    )
    return combined


async def _run_batch(
    instruments: list[str],
    fetch_fn: FetchFn,
    loop_name: str,
    session_factory: async_sessionmaker,
    redis: CacheService,
    shutdown: asyncio.Event,
) -> tuple[int, int, int]:
    """
    Process `instruments` in batches of _BATCH_CONCURRENCY.
    Returns (ok_count, skip_count, error_count).

    The global rate lock inside fundamentals_service enforces 1.0 req/s across
    all concurrent coroutines; this semaphore only bounds in-flight count.
    """
    semaphore = asyncio.Semaphore(_BATCH_CONCURRENCY)
    raw_redis = redis._redis
    ok = skip = error = 0

    async def _one(instrument_key: str) -> str:
        if shutdown.is_set():
            return "skip"
        async with semaphore:
            try:
                isin = extract_isin(instrument_key)
            except ValueError:
                return "skip"

            if not await _try_acquire_refresh_lock(instrument_key, raw_redis):
                return "skip"  # another loop is currently refreshing this instrument

            try:
                async with session_factory() as db:
                    await fetch_fn(instrument_key, isin, db, redis)
                return "ok"
            except _SkipRefresh:
                return "skip"
            except Exception as exc:
                logger.error("[%s] Error refreshing %s: %s", loop_name, instrument_key, exc)
                return "error"
            finally:
                await _release_refresh_lock(instrument_key, raw_redis)

    for i in range(0, len(instruments), _BATCH_CONCURRENCY):
        if shutdown.is_set():
            break
        batch   = instruments[i: i + _BATCH_CONCURRENCY]
        results = await asyncio.gather(*(_one(k) for k in batch))
        for outcome in results:
            if outcome == "ok":
                ok += 1
            elif outcome == "skip":
                skip += 1
            else:
                error += 1

        if i > 0 and i % (20 * _BATCH_CONCURRENCY) == 0:
            logger.debug(
                "[%s] Progress %d/%d | ok=%d skip=%d err=%d",
                loop_name, i + len(batch), len(instruments), ok, skip, error,
            )

    return ok, skip, error


# ── Full-fetch wrappers used by the scheduler loops ───────────────────────────

async def _full_fetch(
    instrument_key: str,
    isin: str,
    db: AsyncSession,
    redis: CacheService,
) -> None:
    """Fetch all 9 endpoints and bust the Redis cache."""
    await fetch_and_store_all(instrument_key, isin, db)
    try:
        await redis.delete(_redis_key(instrument_key))
    except Exception:
        pass


async def _full_fetch_if_stale(
    instrument_key: str,
    isin: str,
    db: AsyncSession,
    redis: CacheService,
) -> None:
    """Full 9-endpoint fetch, but raises _SkipRefresh if Redis cache is still live."""
    try:
        if await redis._redis.exists(_redis_key(instrument_key)):
            raise _SkipRefresh()
    except _SkipRefresh:
        raise
    except Exception:
        pass  # Redis unavailable — proceed with refresh

    await fetch_and_store_all(instrument_key, isin, db)
    try:
        await redis.delete(_redis_key(instrument_key))
    except Exception:
        pass


# ── Scheduler class ────────────────────────────────────────────────────────────

class FundamentalsRefreshScheduler:
    """
    Manages six background refresh loops for company fundamentals data.

    Usage in worker.py:
        scheduler = FundamentalsRefreshScheduler(session_factory, redis_client, shutdown_event)
        task = asyncio.create_task(scheduler.run(), name="fundamentals_refresh")
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        redis: CacheService,
        shutdown: asyncio.Event,
    ) -> None:
        self._sf       = session_factory
        self._redis    = redis
        self._shutdown = shutdown

    async def run(self) -> None:
        """Launch all sub-loops; cancel them if this coroutine is cancelled."""
        tasks = [
            asyncio.create_task(self._loop_daily_ratios(),     name="fund_daily_ratios"),
            asyncio.create_task(self._loop_corp_actions(),     name="fund_corp_actions"),
            asyncio.create_task(self._loop_financials(),       name="fund_financials"),
            asyncio.create_task(self._loop_share_holdings(),   name="fund_holdings"),
            asyncio.create_task(self._loop_priority_refresh(), name="fund_priority"),
            asyncio.create_task(self._nightly_universe_loop(), name="fund_nightly"),
        ]
        logger.info("FundamentalsRefreshScheduler started (%d loops)", len(tasks))
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            logger.info("FundamentalsRefreshScheduler stopped")

    # ── Loop helpers ───────────────────────────────────────────────────────────

    async def _run(
        self,
        loop_name: str,
        instruments: list[str],
        fetch_fn: FetchFn,
    ) -> None:
        ok, skip, err = await _run_batch(
            instruments, fetch_fn, loop_name, self._sf, self._redis, self._shutdown
        )
        logger.info(
            "[%s] Complete — total=%d ok=%d skip=%d err=%d",
            loop_name, len(instruments), ok, skip, err,
        )
        await _record_loop_run(loop_name, self._redis)

    async def _already_ran_today(self, loop_name: str) -> bool:
        last = await _last_loop_run(loop_name, self._redis)
        if not last:
            return False
        return last.date() == datetime.now(timezone.utc).date()

    async def _ran_within(self, loop_name: str, seconds: float) -> bool:
        last = await _last_loop_run(loop_name, self._redis)
        if not last:
            return False
        return (datetime.now(timezone.utc) - last).total_seconds() < seconds

    # ── Loop: key-ratios (15:45 IST, trading days only) ───────────────────────

    async def _loop_daily_ratios(self) -> None:
        loop = "daily_ratios"
        t    = settings.FUNDAMENTALS_DAILY_RATIOS_TIME_IST
        logger.info("[%s] Loop started (runs at %s IST on trading days)", loop, t)

        while not self._shutdown.is_set():
            if not await _wait_until_ist_time(t, self._shutdown):
                break

            today = datetime.now(IST).date()
            if not nse_calendar.is_trading_day(today):
                logger.debug("[%s] Not a trading day (%s) — skipping", loop, today)
                continue
            if await self._already_ran_today(loop):
                logger.info("[%s] Already ran today — skipping", loop)
                continue

            logger.info("[%s] Refreshing key-ratios for all EQ instruments", loop)
            instruments = await _load_all_eq_keys(self._sf)
            await self._run(loop, instruments, fetch_and_store_key_ratios)

    # ── Loop: corporate-actions (18:30 IST, every day) ────────────────────────

    async def _loop_corp_actions(self) -> None:
        loop = "corp_actions"
        t    = settings.FUNDAMENTALS_CORP_ACTIONS_TIME_IST
        logger.info("[%s] Loop started (runs at %s IST daily)", loop, t)

        while not self._shutdown.is_set():
            if not await _wait_until_ist_time(t, self._shutdown):
                break
            if await self._already_ran_today(loop):
                logger.info("[%s] Already ran today — skipping", loop)
                continue

            logger.info("[%s] Refreshing corporate-actions for all EQ instruments", loop)
            instruments = await _load_all_eq_keys(self._sf)
            await self._run(loop, instruments, fetch_and_store_corporate_actions)

    # ── Loop: financials (SEBI Reg 33 deadlines, 06:00 IST) ──────────────────

    async def _loop_financials(self) -> None:
        loop = "financials"
        logger.info("[%s] Loop started (SEBI Reg 33 quarterly deadlines)", loop)

        while not self._shutdown.is_set():
            today    = datetime.now(IST).date()
            deadline = _next_deadline(_SEBI_FINANCIALS_DEADLINES, today)
            target   = datetime(deadline.year, deadline.month, deadline.day, 6, 0, tzinfo=IST)

            logger.info("[%s] Next financials refresh on %s at 06:00 IST", loop, deadline)
            if not await _wait_until(target.astimezone(timezone.utc), self._shutdown):
                break

            if await self._ran_within(loop, 12 * 3600):
                logger.info("[%s] Already ran within 12h — skipping", loop)
                continue

            logger.info("[%s] Refreshing financials for all EQ instruments", loop)
            instruments = await _load_all_eq_keys(self._sf)
            await self._run(loop, instruments, fetch_and_store_financials)

    # ── Loop: share-holdings (SEBI Reg 31 deadlines, 09:00 IST) ─────────────

    async def _loop_share_holdings(self) -> None:
        loop = "share_holdings"
        logger.info("[%s] Loop started (SEBI Reg 31 quarterly deadlines)", loop)

        while not self._shutdown.is_set():
            today    = datetime.now(IST).date()
            deadline = _next_deadline(_SEBI_HOLDINGS_DEADLINES, today)
            target   = datetime(deadline.year, deadline.month, deadline.day, 9, 0, tzinfo=IST)

            logger.info("[%s] Next holdings refresh on %s at 09:00 IST", loop, deadline)
            if not await _wait_until(target.astimezone(timezone.utc), self._shutdown):
                break

            if await self._ran_within(loop, 12 * 3600):
                logger.info("[%s] Already ran within 12h — skipping", loop)
                continue

            logger.info("[%s] Refreshing share-holdings for all EQ instruments", loop)
            instruments = await _load_all_eq_keys(self._sf)
            await self._run(loop, instruments, fetch_and_store_share_holdings)

    # ── Loop: Tier 1 priority (every 6 h, all 9 endpoints) ───────────────────

    async def _loop_priority_refresh(self) -> None:
        loop = "priority"
        logger.info("[%s] Loop started (Tier 1 full refresh every 6 h)", loop)

        while not self._shutdown.is_set():
            last = await _last_loop_run(loop, self._redis)
            now  = datetime.now(timezone.utc)

            if last:
                remaining = _PRIORITY_INTERVAL_SECS - (now - last).total_seconds()
                if remaining > 0:
                    if not await _wait_until(now + timedelta(seconds=remaining), self._shutdown):
                        break

            logger.info("[%s] Refreshing Tier 1 instruments (full 9-endpoint)", loop)
            instruments = await _load_tier1_keys(self._sf, settings.FUNDAMENTALS_PRIORITY_LOOKBACK_DAYS)
            if not instruments:
                logger.info("[%s] No Tier 1 instruments found — sleeping 1 h", loop)
                await _wait_until(datetime.now(timezone.utc) + timedelta(hours=1), self._shutdown)
                continue

            await self._run(loop, instruments, _full_fetch)

    # ── Loop: nightly universe (01:00 IST, all EQ, skips cached) ─────────────

    async def _nightly_universe_loop(self) -> None:
        loop = "nightly_universe"
        t    = settings.FUNDAMENTALS_NIGHTLY_UNIVERSE_TIME_IST
        logger.info("[%s] Loop started (full universe at %s IST, skips live-cached)", loop, t)

        while not self._shutdown.is_set():
            if not await _wait_until_ist_time(t, self._shutdown):
                break

            if await self._ran_within(loop, 20 * 3600):
                logger.info("[%s] Already ran within 20h — skipping", loop)
                continue

            logger.info("[%s] Starting nightly full-universe refresh", loop)
            instruments = await _load_all_eq_keys(self._sf)
            await self._run(loop, instruments, _full_fetch_if_stale)
