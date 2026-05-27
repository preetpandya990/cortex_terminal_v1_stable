"""
Paper Trading — Post-Close Monitor Service
==========================================
Counterfactual price tracking for trades auto-closed by SL or TP.

After a position is closed by the SL/TP breach logic the owner may want to
know what the stock did next:
  • Did a SL-closed position recover back above the stop-loss or entry price?
  • Did a TP1-closed position subsequently reach TP2 or TP3?

This service handles the full lifecycle:

  schedule_post_close_monitor()
    Called (fire-and-forget) from the pnl_worker immediately after an
    auto-close.  Reads the user's account-wide monitoring preference, computes
    the monitoring window, writes a PostCloseMonitor DB record, and registers
    the instrument in the Redis active-instrument SET so the monitor loop can
    dispatch ticks with a single O(1) Redis SISMEMBER check.

  evaluate_instrument_monitors()
    Called on each price tick for instruments with active monitors.
    Applies the tick to every active monitor, records peak prices, target hits,
    and SL-recovery flags.  Completes monitors when all remaining targets are
    hit or the window elapses during a tick.

  sweep_expired_monitors()
    Called periodically (every ~60 s) by the monitor loop.  Finds monitors
    whose monitor_until has passed regardless of tick activity and marks them
    COMPLETED, cleaning up Redis state.

  get_monitor_config() / update_monitor_config()
    REST-layer helpers for reading and writing the UserPreferences monitoring
    fields with a 5-minute Redis cache.

Redis key layout
----------------
  cai:paper:pcm:active_instruments   SET  of instrument_keys with active monitors
  cai:paper:pcm:monitors:{key}       SET  of monitor UUIDs for an instrument
  cai:paper:user_monitor_cfg:{uid}   JSON user-config cache  (TTL = 5 min)

Market session
--------------
  NSE regular session: 09:15–15:30 IST.  For 'end_of_session' mode, monitoring
  ends at 15:30 IST on the trade's close day.  If the close happens at or after
  15:30 IST the window collapses to zero and no monitor is created.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import and_, select

from app.core.database import WorkerSessionLocal

logger = logging.getLogger(__name__)

# ── Redis key constants ────────────────────────────────────────────────────────
PCM_ACTIVE_INSTRUMENTS_KEY = "cai:paper:pcm:active_instruments"
_PCM_MONITORS_PREFIX       = "cai:paper:pcm:monitors:"
_PCM_USER_CFG_PREFIX       = "cai:paper:user_monitor_cfg:"
_PCM_USER_CFG_TTL          = 300   # 5-min cache for user config

# ── IST / NSE session constants ───────────────────────────────────────────────
_IST = timezone(timedelta(hours=5, minutes=30))
_NSE_SESSION_END_HOUR   = 15
_NSE_SESSION_END_MINUTE = 30

_ZERO = Decimal("0")


# ──────────────────────────────────────────────────────────────────────────────
# User config — REST helpers
# ──────────────────────────────────────────────────────────────────────────────

async def get_monitor_config(session, user_id: int):
    """
    Return the UserPreferences row for user_id, inserting a default row if
    none exists.  The caller owns the session transaction.
    """
    from app.models.strategies import UserPreferences

    stmt = select(UserPreferences).where(UserPreferences.user_id == user_id)
    prefs = (await session.execute(stmt)).scalar_one_or_none()
    if prefs is None:
        prefs = UserPreferences(user_id=user_id)
        session.add(prefs)
        await session.flush()
    return prefs


async def update_monitor_config(
    session,
    redis: Redis,
    user_id: int,
    mode: str,
    duration_hours: float | None,
):
    """
    Upsert the monitoring config on UserPreferences and invalidate the cache.
    The caller owns the session transaction.
    """
    prefs = await get_monitor_config(session, user_id)
    prefs.post_close_monitor_mode = mode
    prefs.post_close_monitor_duration_hours = (
        Decimal(str(duration_hours)) if duration_hours is not None else None
    )
    await session.flush()

    # Invalidate Redis cache so the next worker call re-reads from DB
    await redis.delete(f"{_PCM_USER_CFG_PREFIX}{user_id}")

    return prefs


# ──────────────────────────────────────────────────────────────────────────────
# Scheduling — called from pnl_worker after auto-close
# ──────────────────────────────────────────────────────────────────────────────

async def schedule_post_close_monitor(
    redis: Redis,
    user_id: int,
    outcome_id: UUID,
    position_id: UUID,
    portfolio_id: UUID,
    instrument_key: str,
    symbol: str,
    direction: str,
    close_reason: str,
    close_price: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal | None,
    target_price_1: Decimal | None,
    target_price_2: Decimal | None,
    target_price_3: Decimal | None,
) -> None:
    """
    Create a PostCloseMonitor if the user has monitoring enabled.

    Resolves the user's config from Redis cache (or DB on miss), computes the
    monitoring window, determines which targets remain to be watched, writes
    the DB record, and registers the instrument in Redis for fast tick dispatch.

    Silently exits when:
      • monitoring mode is 'disabled'
      • mode is 'end_of_session' and the trade closed after market hours
      • close_reason is 'TP3' (all targets already hit — nothing to watch)
    """
    from app.models.paper_trading import PostCloseMonitor

    # TP3-closed positions have no remaining targets to track
    if close_reason == "TP3":
        return

    mode, duration_hours = await _get_cached_user_config(redis, user_id)

    if mode == "disabled":
        return

    close_time = datetime.now(timezone.utc)
    monitor_until = _compute_monitor_until(close_time, mode, duration_hours)

    if monitor_until is None:
        return

    # Determine which targets were NOT yet hit at close time
    remaining_tp1: Decimal | None = None
    remaining_tp2: Decimal | None = None
    remaining_tp3: Decimal | None = None

    if close_reason == "SL":
        remaining_tp1 = target_price_1
        remaining_tp2 = target_price_2
        remaining_tp3 = target_price_3
    elif close_reason == "TP1":
        remaining_tp2 = target_price_2
        remaining_tp3 = target_price_3
    elif close_reason == "TP2":
        remaining_tp3 = target_price_3
    # TP3 already handled above

    async with WorkerSessionLocal() as session:
        async with session.begin():
            monitor = PostCloseMonitor(
                outcome_id=outcome_id,
                position_id=position_id,
                portfolio_id=portfolio_id,
                user_id=user_id,
                symbol=symbol,
                instrument_key=instrument_key,
                direction=direction,
                close_reason=close_reason,
                close_price=close_price,
                entry_price=entry_price,
                stop_loss=stop_loss,
                remaining_tp1=remaining_tp1,
                remaining_tp2=remaining_tp2,
                remaining_tp3=remaining_tp3,
                monitor_until=monitor_until,
                status="ACTIVE",
                peak_favorable_price=close_price,
                peak_adverse_price=close_price,
                recovered_above_sl=False,
                recovered_above_entry=False,
                created_at=close_time,
                updated_at=close_time,
            )
            session.add(monitor)
            await session.flush()
            monitor_id = str(monitor.id)

    # Register in Redis for O(1) tick dispatch
    await redis.sadd(PCM_ACTIVE_INSTRUMENTS_KEY, instrument_key)
    await redis.sadd(f"{_PCM_MONITORS_PREFIX}{instrument_key}", monitor_id)

    logger.info(
        "Post-close monitor created: symbol=%s close_reason=%s "
        "direction=%s monitor_until=%s",
        symbol, close_reason, direction, monitor_until.isoformat(),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Tick evaluation — called from pnl_worker on every relevant price tick
# ──────────────────────────────────────────────────────────────────────────────

async def evaluate_instrument_monitors(
    redis: Redis,
    instrument_key: str,
    ltp: Decimal,
    now: datetime,
) -> None:
    """
    Apply a price tick to all active PostCloseMonitors for the instrument.

    For each monitor: updates peak prices, records target hits, checks SL
    recovery.  Completes the monitor when the window elapses or (for TP-closed
    positions) when all remaining targets have been hit.
    """
    from app.models.paper_trading import PostCloseMonitor

    raw_ids = await redis.smembers(f"{_PCM_MONITORS_PREFIX}{instrument_key}")
    if not raw_ids:
        await redis.srem(PCM_ACTIVE_INSTRUMENTS_KEY, instrument_key)
        return

    monitor_ids = [UUID(mid) for mid in raw_ids]

    async with WorkerSessionLocal() as session:
        stmt = (
            select(PostCloseMonitor)
            .where(
                and_(
                    PostCloseMonitor.id.in_(monitor_ids),
                    PostCloseMonitor.status == "ACTIVE",
                )
            )
        )
        monitors = list((await session.execute(stmt)).scalars().all())

        completed_ids: list[str] = []

        for monitor in monitors:
            _apply_tick(monitor, ltp, now)

            # Complete if window elapsed during this tick
            if now >= monitor.monitor_until:
                _mark_completed(monitor, now)
                completed_ids.append(str(monitor.id))
            # For TP-closed monitors: complete early when all remaining targets hit
            elif monitor.close_reason != "SL" and monitor.all_targets_resolved:
                _mark_completed(monitor, now)
                completed_ids.append(str(monitor.id))

        await session.commit()

    if completed_ids:
        await _remove_from_redis(redis, instrument_key, completed_ids)


# ──────────────────────────────────────────────────────────────────────────────
# Expiry sweep — periodic, time-driven cleanup
# ──────────────────────────────────────────────────────────────────────────────

async def sweep_expired_monitors(redis: Redis) -> None:
    """
    Mark ACTIVE monitors whose monitor_until has passed as COMPLETED.

    Called on worker startup and every ~60 s thereafter.  Handles the case
    where no ticks arrived for an instrument during its monitoring window
    (e.g. circuit breaker, weekend, or post-market trade).
    """
    from app.models.paper_trading import PostCloseMonitor

    now = datetime.now(timezone.utc)

    async with WorkerSessionLocal() as session:
        stmt = (
            select(PostCloseMonitor)
            .where(
                and_(
                    PostCloseMonitor.status == "ACTIVE",
                    PostCloseMonitor.monitor_until <= now,
                )
            )
        )
        expired = list((await session.execute(stmt)).scalars().all())

        for monitor in expired:
            _mark_completed(monitor, now)

        await session.commit()

    if not expired:
        return

    # Bulk Redis cleanup
    by_instrument: dict[str, list[str]] = {}
    for monitor in expired:
        by_instrument.setdefault(monitor.instrument_key, []).append(str(monitor.id))

    for instrument_key, ids in by_instrument.items():
        await _remove_from_redis(redis, instrument_key, ids)

    logger.info("Post-close monitor expiry sweep: completed %d record(s)", len(expired))


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _compute_monitor_until(
    close_time: datetime,
    mode: str,
    duration_hours: Decimal | None,
) -> datetime | None:
    """
    Compute the UTC datetime at which monitoring ends.

    Returns None when monitoring should not be created (disabled mode, or
    end_of_session when the trade closes at/after market close).
    """
    if mode == "fixed_duration":
        if not duration_hours or duration_hours <= _ZERO:
            return None
        return close_time + timedelta(hours=float(duration_hours))

    if mode == "end_of_session":
        close_ist = close_time.astimezone(_IST)
        session_end_ist = close_ist.replace(
            hour=_NSE_SESSION_END_HOUR,
            minute=_NSE_SESSION_END_MINUTE,
            second=0,
            microsecond=0,
        )
        if close_ist >= session_end_ist:
            # Trade closed at or after market close — no window available
            return None
        return session_end_ist.astimezone(timezone.utc)

    return None


def _apply_tick(monitor: "PostCloseMonitor", ltp: Decimal, now: datetime) -> None:  # type: ignore[name-defined]
    """
    Mutate monitor in-place based on the current LTP.

    Updates peak prices, records first-hit timestamps for each remaining
    target, and sets recovery flags for SL-closed positions.
    """
    is_long = monitor.direction == "LONG"

    # ── Peak prices ────────────────────────────────────────────────────────────
    if is_long:
        if monitor.peak_favorable_price is None or ltp > monitor.peak_favorable_price:
            monitor.peak_favorable_price = ltp
        if monitor.peak_adverse_price is None or ltp < monitor.peak_adverse_price:
            monitor.peak_adverse_price = ltp
    else:
        if monitor.peak_favorable_price is None or ltp < monitor.peak_favorable_price:
            monitor.peak_favorable_price = ltp
        if monitor.peak_adverse_price is None or ltp > monitor.peak_adverse_price:
            monitor.peak_adverse_price = ltp

    # ── SL recovery (SL-closed positions only) ─────────────────────────────────
    if monitor.close_reason == "SL" and monitor.stop_loss is not None:
        if not monitor.recovered_above_sl:
            sl = monitor.stop_loss
            if (is_long and ltp > sl) or (not is_long and ltp < sl):
                monitor.recovered_above_sl = True
                monitor.sl_recovery_at = now

        if not monitor.recovered_above_entry:
            entry = monitor.entry_price
            if (is_long and ltp >= entry) or (not is_long and ltp <= entry):
                monitor.recovered_above_entry = True

    # ── Remaining target hits ──────────────────────────────────────────────────
    if monitor.remaining_tp1 is not None and monitor.tp1_hit_at is None:
        tp = monitor.remaining_tp1
        if (is_long and ltp >= tp) or (not is_long and ltp <= tp):
            monitor.tp1_hit_at = now

    if monitor.remaining_tp2 is not None and monitor.tp2_hit_at is None:
        tp = monitor.remaining_tp2
        if (is_long and ltp >= tp) or (not is_long and ltp <= tp):
            monitor.tp2_hit_at = now

    if monitor.remaining_tp3 is not None and monitor.tp3_hit_at is None:
        tp = monitor.remaining_tp3
        if (is_long and ltp >= tp) or (not is_long and ltp <= tp):
            monitor.tp3_hit_at = now

    monitor.updated_at = now


def _mark_completed(monitor: "PostCloseMonitor", now: datetime) -> None:  # type: ignore[name-defined]
    monitor.status = "COMPLETED"
    monitor.completed_at = now
    monitor.updated_at = now


async def _remove_from_redis(
    redis: Redis,
    instrument_key: str,
    monitor_ids: list[str],
) -> None:
    """Remove monitor IDs from the per-instrument SET; drop instrument if empty."""
    if monitor_ids:
        await redis.srem(f"{_PCM_MONITORS_PREFIX}{instrument_key}", *monitor_ids)

    remaining = await redis.scard(f"{_PCM_MONITORS_PREFIX}{instrument_key}")
    if remaining == 0:
        await redis.srem(PCM_ACTIVE_INSTRUMENTS_KEY, instrument_key)
        await redis.delete(f"{_PCM_MONITORS_PREFIX}{instrument_key}")


async def _get_cached_user_config(
    redis: Redis,
    user_id: int,
) -> tuple[str, Decimal | None]:
    """
    Return (mode, duration_hours) from the Redis cache, falling back to DB.

    A 5-minute TTL is used so config changes propagate within one cache window.
    """
    from app.models.strategies import UserPreferences

    cache_key = f"{_PCM_USER_CFG_PREFIX}{user_id}"
    raw = await redis.get(cache_key)
    if raw:
        try:
            data: dict[str, Any] = json.loads(raw)
            dh_raw = data.get("duration_hours")
            return (
                data["mode"],
                Decimal(str(dh_raw)) if dh_raw is not None else None,
            )
        except (json.JSONDecodeError, KeyError, Exception):
            pass

    # DB fallback
    async with WorkerSessionLocal() as session:
        stmt = select(UserPreferences).where(UserPreferences.user_id == user_id)
        prefs = (await session.execute(stmt)).scalar_one_or_none()

    if prefs is None:
        mode: str = "disabled"
        duration_hours: Decimal | None = None
    else:
        mode = prefs.post_close_monitor_mode
        duration_hours = prefs.post_close_monitor_duration_hours

    # Cache with TTL
    await redis.set(
        cache_key,
        json.dumps({
            "mode": mode,
            "duration_hours": str(duration_hours) if duration_hours is not None else None,
        }),
        ex=_PCM_USER_CFG_TTL,
    )

    return mode, duration_hours
