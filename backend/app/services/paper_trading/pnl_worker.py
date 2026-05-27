"""
Paper Trading — P&L Recompute Worker
======================================
Background task that bridges the Upstox market feed tick stream into the
paper trading subsystem.

Architecture
------------
Four concurrent asyncio tasks run within a supervised lifecycle:

  Tick Listener  (``_tick_listener``)
    Subscribes to ``cai:market-feed:ltpc`` via ``pubsub.listen()`` — a true
    blocking async generator backed by a real socket read.  No polling, no
    sleep loops.  For each tick it:
      1. Writes LTP to ``cai:ltp:{instrument_key}`` (5-min TTL).
      2. Resolves affected portfolio_ids through a 60 s Redis hash cache
         (falls back to DB on miss).
      3. Enqueues each portfolio_id into a bounded ``asyncio.Queue``.

  Recompute Loop  (``_recompute_loop``)
    Blocks on ``queue.get()`` — wakes the instant any portfolio is dirtied.
    Drains remaining queue items without blocking to deduplicate: a burst of
    ticks for N positions in the same portfolio produces exactly one DB
    round-trip.  For each unique portfolio it:
      a. Fetches all OPEN positions from DB.
      b. Reads LTPs from Redis, recomputes unrealized P&L.
      c. Checks SL / TP1 / TP2 / TP3 breach conditions.
      d. Auto-closes breached positions (calls position_service).
      e. Publishes a ``LivePnLUpdate`` frame to
         ``cai:paper:pnl:{portfolio_id}``.

  Post-Close Monitor Loop  (``_post_close_monitor_loop``)
    Subscribes independently to the market feed.  For each tick it checks
    whether any post-close counterfactual monitor is active for the instrument
    and, if so, evaluates it.  A 60 s sweep handles monitors that expire
    between ticks.

  Pending-Order Matching Engine  (``_pending_order_match_loop``)
    Subscribes to the market feed AND to ``cai:paper:pending_orders_updated``
    for cache invalidation.  Maintains an in-memory dict keyed by
    instrument_key mapping to lists of pending order slots.  For each tick:
      a. Evaluates ``_limit_condition_met()`` for every cached order.
      b. Fills matched orders via ``execute_pending_order_fill()`` under a
         ``SELECT FOR UPDATE SKIP LOCKED`` guard to prevent double-fill.
      c. Publishes ``order_filled`` / ``order_expired`` WS events to
         ``cai:paper:pnl:{portfolio_id}``.
    A 60 s sweep expires DAY orders past 15:30 IST (10:00 UTC).

This replaces the former 500 ms clock-gate.  Latency from a market tick to
a browser WS frame is now bounded only by the DB query (~5–20 ms) rather
than the artificial 500 ms recompute interval.

Concurrency notes
-----------------
- ``WorkerSessionLocal`` is a dedicated DB pool isolated from the API pool;
  auto-close operations never stall REST request handling.
- The queue is bounded (``_QUEUE_MAX_SIZE``) to prevent unbounded memory
  growth under heavy tick loads.  Overflow drops the oldest stale entry
  because the LTP cache always reflects the latest price.
- If any inner task exits for any reason, the supervisor cancels all others
  and restarts after a 5 s back-off.
- The matching engine uses ``SKIP LOCKED`` on PaperOrder rows so two worker
  processes (e.g. rolling deploy) never double-fill the same order.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from time import monotonic
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import and_, select

from app.core.database import WorkerSessionLocal
from app.core.redis import RedisChannels

logger = logging.getLogger(__name__)

# ── Redis key constants ────────────────────────────────────────────────────────
_LTP_KEY_PREFIX        = "cai:ltp:"
_LTP_TTL_SECONDS       = 300   # 5-min TTL; stale price auto-expires
_PNL_CHANNEL_PREFIX    = "cai:paper:pnl:"

# ── Symbol → portfolio mapping cache (HSET: instrument_key → JSON list[str])
_SYMBOL_PORTFOLIOS_KEY = "cai:paper:symbol_portfolios"
_SYMBOL_PORTFOLIOS_TTL = 60    # seconds before DB re-query

# ── Queue sizing ───────────────────────────────────────────────────────────────
_QUEUE_MAX_SIZE        = 256   # drops oldest on overflow; latest LTP wins

# ── Matching engine constants ──────────────────────────────────────────────────
# DAY orders expire at NSE market close: 15:30 IST = 10:00 UTC.
_NSE_CLOSE_UTC         = time(10, 0, 0)
# Sweep expired DAY orders every 60 s; also catches post-market no-tick periods.
_DAY_EXPIRY_SWEEP_S    = 60.0

_ZERO = Decimal("0")


# ── In-memory pending order slot ───────────────────────────────────────────────

@dataclass(slots=True)
class _PendingOrderSlot:
    """
    Lightweight in-memory representation of a pending LIMIT/SL/SL-M order.

    Holds the minimum fields needed for the hot-path condition check in
    ``_check_and_fill_pending_orders``.  The full ``PaperOrder`` ORM row is
    loaded from the DB inside the fill transaction — this slot only decides
    *whether* to attempt a fill.
    """
    order_id:         UUID
    portfolio_id:     UUID
    transaction_type: str   # "BUY" | "SELL"
    order_type:       str   # "LIMIT" | "SL" | "SL-M"
    validity:         str   # "DAY" | "IOC" (IOC never sits in cache, but guarded)
    price:            Decimal | None
    trigger_price:    Decimal | None


# ──────────────────────────────────────────────────────────────────────────────
# Worker entry-point (supervisor)
# ──────────────────────────────────────────────────────────────────────────────

async def run_pnl_worker(redis: Redis) -> None:
    """
    Supervisor coroutine — started with asyncio.create_task at app lifespan.

    Restarts the inner loop on any unhandled exception with 5 s back-off.
    """
    logger.info("PnL worker starting")
    while True:
        try:
            await _worker_loop(redis)
        except asyncio.CancelledError:
            logger.info("PnL worker cancelled — shutting down")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("PnL worker crashed — restarting in 5 s: %s", exc)
            await asyncio.sleep(5)


# ──────────────────────────────────────────────────────────────────────────────
# Inner loop — two cooperative tasks under a shared lifecycle
# ──────────────────────────────────────────────────────────────────────────────

async def _worker_loop(redis: Redis) -> None:
    """
    Spin up the listener, recompute, post-close monitor, and matching engine.
    Restarts all four if any one exits unexpectedly.
    """
    queue: asyncio.Queue[UUID] = asyncio.Queue(maxsize=_QUEUE_MAX_SIZE)

    pubsub = redis.pubsub()
    await pubsub.subscribe(RedisChannels.MARKET_FEED_LTPC)
    logger.info("PnL worker subscribed to %s", RedisChannels.MARKET_FEED_LTPC)

    listener  = asyncio.create_task(
        _tick_listener(redis, pubsub, queue), name="pnl-tick-listener"
    )
    recompute = asyncio.create_task(
        _recompute_loop(redis, queue), name="pnl-recompute-loop"
    )
    monitor = asyncio.create_task(
        _post_close_monitor_loop(redis), name="pnl-post-close-monitor"
    )
    matcher = asyncio.create_task(
        _pending_order_match_loop(redis), name="pnl-pending-order-matcher"
    )

    all_tasks = [listener, recompute, monitor, matcher]

    try:
        done, _ = await asyncio.wait(
            all_tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if not task.cancelled() and (exc := task.exception()):
                logger.error(
                    "PnL worker inner task %s raised: %s", task.get_name(), exc
                )
    finally:
        for t in all_tasks:
            t.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
        await pubsub.unsubscribe(RedisChannels.MARKET_FEED_LTPC)
        await pubsub.aclose()


async def _tick_listener(
    redis: Redis,
    pubsub: Any,
    queue: asyncio.Queue[UUID],
) -> None:
    """
    Forward market-feed ticks to the recompute queue.

    ``pubsub.listen()`` is a blocking async generator backed by a real socket
    read — zero CPU overhead while idle, wakes the instant a frame arrives.
    """
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue

        raw = message["data"]
        if isinstance(raw, bytes):
            raw = raw.decode()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        instrument_key = data.get("instrument_key")
        ltp            = data.get("ltp")
        if instrument_key is None or ltp is None:
            continue

        # Persist LTP for the recompute task (and any other consumers)
        await redis.set(
            f"{_LTP_KEY_PREFIX}{instrument_key}", str(ltp), ex=_LTP_TTL_SECONDS
        )

        # Resolve portfolios with an open position in this instrument
        portfolio_ids = await _get_portfolios_for_instrument(redis, instrument_key)
        for pid in portfolio_ids:
            try:
                queue.put_nowait(pid)
            except asyncio.QueueFull:
                # Queue saturated — drop the oldest stale entry to make room.
                # The LTP cache already reflects the latest price so correctness
                # is preserved; we never drop a portfolio, only deduplicate.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(pid)


async def _recompute_loop(redis: Redis, queue: asyncio.Queue[UUID]) -> None:
    """
    Coalescing recompute loop — event-driven, zero artificial delay.

    Blocks on the first queue item then drains all remaining without blocking,
    deduplicating into a set.  A burst of ticks for N positions in the same
    portfolio triggers exactly one DB round-trip.
    """
    while True:
        # Block until at least one portfolio needs recomputing
        first = await queue.get()
        dirty: set[UUID] = {first}

        # Non-blocking drain: coalesce any additional items queued concurrently
        try:
            while True:
                dirty.add(queue.get_nowait())
        except asyncio.QueueEmpty:
            pass

        # Recompute each unique dirty portfolio immediately
        for portfolio_id in dirty:
            try:
                await _recompute_portfolio_pnl(redis, portfolio_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "PnL recompute failed for portfolio %s: %s", portfolio_id, exc
                )


async def _recompute_portfolio_pnl(redis: Redis, portfolio_id: UUID) -> None:
    """
    Recompute unrealized P&L for all open positions in a portfolio,
    check SL/TP breach, and publish a LivePnLUpdate to the portfolio's
    WebSocket channel.
    """
    from app.models.paper_trading import PaperPosition, Portfolio

    async with WorkerSessionLocal() as session:
        # Fetch portfolio + open positions
        portfolio_stmt = select(Portfolio).where(Portfolio.id == portfolio_id)
        portfolio = (await session.execute(portfolio_stmt)).scalar_one_or_none()
        if portfolio is None or not portfolio.is_active:
            return

        pos_stmt = select(PaperPosition).where(
            and_(
                PaperPosition.portfolio_id == portfolio_id,
                PaperPosition.status == "OPEN",
            )
        )
        positions = list((await session.execute(pos_stmt)).scalars().all())
        if not positions:
            return

        # Recompute per-position P&L
        position_frames = []
        auto_close_queue: list[tuple[PaperPosition, str, Decimal]] = []

        total_unrealized = _ZERO

        for pos in positions:
            ltp_raw = await redis.get(f"{_LTP_KEY_PREFIX}{pos.instrument_key}")
            if ltp_raw is None:
                last_price = pos.last_price or pos.avg_cost_price
            else:
                last_price = Decimal(str(ltp_raw))

            unrealized = (last_price - pos.avg_cost_price) * Decimal(pos.quantity)
            pnl_pct = (
                float((last_price - pos.avg_cost_price) / pos.avg_cost_price * 100)
                if pos.avg_cost_price > _ZERO else 0.0
            )

            # Update DB cache (unrealized_pnl + last_price)
            pos.last_price = last_price
            pos.unrealized_pnl = unrealized.quantize(Decimal("0.0001"))
            pos.updated_at = datetime.now(timezone.utc)

            total_unrealized += unrealized

            # SL / TP breach check
            exit_reason, exit_price = _check_sl_tp_breach(pos, last_price)
            if exit_reason:
                auto_close_queue.append((pos, exit_reason, exit_price))

            position_frames.append({
                "position_id": str(pos.id),
                "symbol": pos.symbol,
                "quantity": pos.quantity,
                "side": pos.side,
                "avg_cost_price": float(pos.avg_cost_price),
                "last_price": float(last_price),
                "unrealized_pnl": float(unrealized),
                "pnl_pct": pnl_pct,
                "stop_loss": float(pos.stop_loss) if pos.stop_loss else None,
                "target_price_1": float(pos.target_price_1) if pos.target_price_1 else None,
            })

        await session.flush()

        # Handle SL/TP auto-closes
        for pos, reason, exit_p in auto_close_queue:
            try:
                await _auto_close_position(session, redis, portfolio, pos, reason, exit_p)
                positions.remove(pos)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Auto-close failed: portfolio=%s symbol=%s reason=%s: %s",
                    portfolio_id, pos.symbol, reason, exc,
                )

        await session.commit()

    # ── Compute total realized from cumulative outcomes ────────────────────────
    from app.models.paper_trading import PaperTradeOutcome
    from sqlalchemy import func

    async with WorkerSessionLocal() as session2:
        agg = await session2.execute(
            select(func.coalesce(func.sum(PaperTradeOutcome.net_pnl), _ZERO).label("total_net"))
            .where(PaperTradeOutcome.portfolio_id == portfolio_id)
        )
        total_realized_pnl = Decimal(str(agg.scalar_one() or _ZERO))

        portfolio2_stmt = select(Portfolio).where(Portfolio.id == portfolio_id)
        portfolio2 = (await session2.execute(portfolio2_stmt)).scalar_one_or_none()
        current_cash = portfolio2.current_cash if portfolio2 else _ZERO

    # ── Build and publish LivePnLUpdate ───────────────────────────────────────
    open_market_value = sum(
        Decimal(str(f["last_price"])) * f["quantity"]
        for f in position_frames
    )
    portfolio_value = current_cash + open_market_value
    initial_capital = portfolio.initial_capital if portfolio.initial_capital > _ZERO else Decimal("1")

    total_return_pct = float(
        (portfolio_value - initial_capital) / initial_capital * 100
    )

    payload = json.dumps({
        "portfolio_id": str(portfolio_id),
        "positions": position_frames,
        "total_unrealized_pnl": float(total_unrealized),
        "total_realized_pnl": float(total_realized_pnl),
        "current_cash": float(current_cash),
        "portfolio_value": float(portfolio_value),
        "total_return_pct": total_return_pct,
        "ts": datetime.now(timezone.utc).isoformat(),
    }, default=str)

    channel = f"{_PNL_CHANNEL_PREFIX}{portfolio_id}"
    await redis.publish(channel, payload)


def _check_sl_tp_breach(
    position: "PaperPosition",
    last_price: Decimal,
) -> tuple[str | None, Decimal]:
    """
    Check if last_price triggers a SL or TP close.

    Returns (exit_reason, exit_price) or (None, _ZERO) if no breach.

    Check order: SL first (risk management priority), then TP3 → TP2 → TP1.
    """
    is_long = position.side == "LONG"

    # Stop-loss
    if position.stop_loss is not None:
        sl = position.stop_loss
        if (is_long and last_price <= sl) or (not is_long and last_price >= sl):
            return "SL", last_price

    # Take-profit: highest target first (TP3 > TP2 > TP1)
    for tp_attr, reason in [
        ("target_price_3", "TP3"),
        ("target_price_2", "TP2"),
        ("target_price_1", "TP1"),
    ]:
        tp = getattr(position, tp_attr, None)
        if tp is None:
            continue
        if (is_long and last_price >= tp) or (not is_long and last_price <= tp):
            return reason, last_price

    return None, _ZERO


async def _auto_close_position(
    session,
    redis: Redis,
    portfolio,
    position,
    exit_reason: str,
    exit_price: Decimal,
) -> None:
    """
    Auto-close a position when SL or TP is breached.

    Delegates to position_service.apply_sell_fill_to_position after creating
    a synthetic fill at the breach price.
    """
    from app.models.paper_trading import PaperFill, PaperOrder
    from app.services.paper_trading.charge_calculator import calculate_charges
    from app.services.paper_trading.portfolio_service import credit_cash
    from app.services.paper_trading.position_service import apply_sell_fill_to_position
    from app.services.paper_trading.order_service import _compute_settlement_date

    # Infer product_type from most recent order for this position
    from sqlalchemy import desc
    opening_order_stmt = (
        select(PaperOrder)
        .join(PaperFill, PaperOrder.id == PaperFill.order_id)
        .where(
            and_(
                PaperFill.portfolio_id == position.portfolio_id,
                PaperFill.symbol == position.symbol,
                PaperOrder.transaction_type == "BUY",
            )
        )
        .order_by(desc(PaperOrder.placed_at))
        .limit(1)
    )
    opening_order = (await session.execute(opening_order_stmt)).scalar_one_or_none()
    product_type = opening_order.product_type if opening_order else "CNC"

    charges = calculate_charges(
        transaction_type="SELL",
        product_type=product_type,
        fill_price=exit_price,
        fill_quantity=position.quantity,
    )
    settlement_date = _compute_settlement_date("SELL", product_type)

    # Create synthetic SELL order
    sell_order = PaperOrder(
        portfolio_id=position.portfolio_id,
        suggestion_id=position.suggestion_id,
        symbol=position.symbol,
        instrument_key=position.instrument_key,
        transaction_type="SELL",
        product_type=product_type,
        order_type="MARKET",
        validity="DAY",
        quantity=position.quantity,
        status="PENDING",
    )
    session.add(sell_order)
    await session.flush()

    fill = PaperFill(
        order_id=sell_order.id,
        portfolio_id=position.portfolio_id,
        symbol=position.symbol,
        fill_quantity=position.quantity,
        fill_price=exit_price,
        slippage_bps=_ZERO,
        brokerage=_ZERO,
        stt=charges.stt,
        exchange_charges=charges.exchange_charges,
        sebi_charges=charges.sebi_charges,
        gst=charges.gst,
        stamp_duty=charges.stamp_duty,
        total_charges=charges.total_charges,
        net_amount=charges.net_amount,
        settlement_date=settlement_date,
        executed_at=datetime.now(timezone.utc),
    )
    session.add(fill)
    await session.flush()

    await credit_cash(session, portfolio, charges.net_amount)

    await apply_sell_fill_to_position(
        session=session,
        portfolio=portfolio,
        position=position,
        fill=fill,
        exit_reason=exit_reason,
    )

    sell_order.status = "COMPLETE"
    sell_order.updated_at = datetime.now(timezone.utc)
    await session.flush()

    logger.info(
        "Auto-close executed: portfolio=%s symbol=%s reason=%s price=%.4f qty=%d",
        portfolio.id, position.symbol, exit_reason,
        float(exit_price), fill.fill_quantity,
    )

    # Schedule ML feedback computation and post-close monitoring (fire-and-forget)
    from app.models.paper_trading import PaperTradeOutcome
    outcome_stmt = (
        select(PaperTradeOutcome)
        .where(PaperTradeOutcome.position_id == position.id)
    )
    outcome = (await session.execute(outcome_stmt)).scalar_one_or_none()
    if outcome:
        asyncio.create_task(
            _compute_feedback_deferred(
                outcome.id,
                outcome.symbol,
                outcome.portfolio_id,
                outcome.user_id,
            ),
            name=f"ml_feedback_{outcome.id}",
        )
        asyncio.create_task(
            _schedule_post_close_monitor_deferred(
                redis=redis,
                user_id=portfolio.user_id,
                outcome_id=outcome.id,
                position_id=position.id,
                portfolio_id=portfolio.id,
                instrument_key=position.instrument_key,
                symbol=position.symbol,
                direction=position.side,
                close_reason=exit_reason,
                close_price=exit_price,
                entry_price=position.avg_cost_price,
                stop_loss=position.stop_loss,
                target_price_1=position.target_price_1,
                target_price_2=position.target_price_2,
                target_price_3=position.target_price_3,
            ),
            name=f"pcm_schedule_{position.id}",
        )


async def _compute_feedback_deferred(
    outcome_id: UUID,
    symbol: str,
    portfolio_id: UUID,
    user_id: int,
) -> None:
    """
    Fire-and-forget: compute ML feedback with exponential-backoff retry.

    Delegates to compute_ml_feedback_with_retry() which handles:
      - 3 attempts with exp backoff + jitter
      - MLFeedbackError DB record on final failure
      - Redis cai:ml:feedback_errors alert on final failure
      - Prometheus instrumentation
    """
    from app.core.retry import compute_ml_feedback_with_retry

    await compute_ml_feedback_with_retry(
        outcome_id=outcome_id,
        symbol=symbol,
        portfolio_id=portfolio_id,
        user_id=user_id,
        session_factory=WorkerSessionLocal,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Symbol → portfolio index management
# ──────────────────────────────────────────────────────────────────────────────

async def _get_portfolios_for_instrument(
    redis: Redis,
    instrument_key: str,
) -> list[UUID]:
    """
    Return all portfolio_ids with an OPEN position in the given instrument.

    Uses a Redis HSET `cai:paper:symbol_portfolios` as a local cache.
    On cache miss, queries the DB and repopulates.  The cache has a 60-second
    TTL so it stays roughly fresh without a DB query on every tick.
    """
    raw = await redis.hget(_SYMBOL_PORTFOLIOS_KEY, instrument_key)
    if raw:
        try:
            return [UUID(pid) for pid in json.loads(raw)]
        except (json.JSONDecodeError, ValueError):
            pass

    # DB lookup
    portfolio_ids = await _db_portfolios_for_instrument(instrument_key)

    # Cache result (even empty list — avoids repeated DB hits for unknown instruments)
    await redis.hset(
        _SYMBOL_PORTFOLIOS_KEY,
        instrument_key,
        json.dumps([str(p) for p in portfolio_ids]),
    )
    # Set TTL on the whole hash once (idempotent)
    await redis.expire(_SYMBOL_PORTFOLIOS_KEY, _SYMBOL_PORTFOLIOS_TTL)

    return portfolio_ids


async def _db_portfolios_for_instrument(instrument_key: str) -> list[UUID]:
    """Query DB for portfolios with an open position in the given instrument."""
    from app.models.paper_trading import PaperPosition

    async with WorkerSessionLocal() as session:
        stmt = (
            select(PaperPosition.portfolio_id)
            .where(
                and_(
                    PaperPosition.instrument_key == instrument_key,
                    PaperPosition.status == "OPEN",
                )
            )
            .distinct()
        )
        result = await session.execute(stmt)
        return [row[0] for row in result.all()]


async def invalidate_symbol_cache(redis: Redis, instrument_key: str) -> None:
    """
    Remove a symbol's portfolio cache entry.

    Called by position_service after opening or closing a position so the
    next tick rebuild picks up the change.
    """
    await redis.hdel(_SYMBOL_PORTFOLIOS_KEY, instrument_key)


# ──────────────────────────────────────────────────────────────────────────────
# Post-close monitoring
# ──────────────────────────────────────────────────────────────────────────────

async def _schedule_post_close_monitor_deferred(
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
    Fire-and-forget wrapper: schedule a PostCloseMonitor after an auto-close.

    Runs as an asyncio task so it never delays the hot auto-close path.
    All exceptions are caught and logged — monitoring failures must never
    surface back to the P&L worker.
    """
    from app.services.paper_trading.post_close_monitor_service import (
        schedule_post_close_monitor,
    )

    try:
        await schedule_post_close_monitor(
            redis=redis,
            user_id=user_id,
            outcome_id=outcome_id,
            position_id=position_id,
            portfolio_id=portfolio_id,
            instrument_key=instrument_key,
            symbol=symbol,
            direction=direction,
            close_reason=close_reason,
            close_price=close_price,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target_price_1=target_price_1,
            target_price_2=target_price_2,
            target_price_3=target_price_3,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Post-close monitor scheduling failed: symbol=%s reason=%s: %s",
            symbol, close_reason, exc,
        )


async def _post_close_monitor_loop(redis: Redis) -> None:
    """
    Third supervised task in _worker_loop.

    Subscribes to the same market-feed channel as the tick listener but
    operates completely independently.  For each tick it performs an O(1)
    Redis SISMEMBER check; only instruments with active monitors trigger a
    DB round-trip.

    A 60-second monotonic sweep catches monitors whose window elapsed during
    periods with no ticks (circuit breaker, post-market, etc.).
    """
    from app.services.paper_trading.post_close_monitor_service import (
        PCM_ACTIVE_INSTRUMENTS_KEY,
        evaluate_instrument_monitors,
        sweep_expired_monitors,
    )

    pubsub = redis.pubsub()
    await pubsub.subscribe(RedisChannels.MARKET_FEED_LTPC)
    logger.info("Post-close monitor loop started")

    # Startup sweep — recover any monitors that expired while the server was down
    try:
        await sweep_expired_monitors(redis)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Post-close monitor startup sweep failed: %s", exc)

    _SWEEP_INTERVAL_S = 60.0
    last_sweep_mono = monotonic()

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue

            raw = message["data"]
            if isinstance(raw, bytes):
                raw = raw.decode()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            instrument_key = data.get("instrument_key")
            ltp = data.get("ltp")
            if instrument_key is None or ltp is None:
                continue

            # Fast-path: only touch DB when this instrument has active monitors
            if await redis.sismember(PCM_ACTIVE_INSTRUMENTS_KEY, instrument_key):
                try:
                    await evaluate_instrument_monitors(
                        redis=redis,
                        instrument_key=instrument_key,
                        ltp=Decimal(str(ltp)),
                        now=datetime.now(timezone.utc),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Post-close monitor eval failed: instrument=%s: %s",
                        instrument_key, exc,
                    )

            # Periodic time-based sweep
            now_mono = monotonic()
            if now_mono - last_sweep_mono >= _SWEEP_INTERVAL_S:
                try:
                    await sweep_expired_monitors(redis)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Post-close monitor expiry sweep failed: %s", exc)
                last_sweep_mono = now_mono
    finally:
        await pubsub.unsubscribe(RedisChannels.MARKET_FEED_LTPC)
        await pubsub.aclose()


# ──────────────────────────────────────────────────────────────────────────────
# Pending-Order Matching Engine
# ──────────────────────────────────────────────────────────────────────────────

async def _pending_order_match_loop(redis: Redis) -> None:
    """
    Fourth supervised task — the LIMIT/SL/SL-M order matching engine.

    Maintains an in-memory ``dict[str, list[_PendingOrderSlot]]`` keyed by
    instrument_key.  On each market-feed tick it evaluates the price condition
    for all cached orders for that instrument and attempts fills.

    Cache invalidation is driven by ``cai:paper:pending_orders_updated``
    messages published by the API layer whenever an order is placed, cancelled,
    or filled.  This keeps the cache accurate without a full DB scan per tick.

    A 60-second sweep expires any DAY orders past 15:30 IST (10:00 UTC).

    Concurrency safety
    ------------------
    ``_fill_pending_order()`` uses ``SELECT PaperOrder FOR UPDATE SKIP LOCKED``
    so that if two worker processes race, only one wins the lock; the other
    skips the already-locked row silently.
    """
    # In-memory cache: instrument_key → list of pending slots
    pending: dict[str, list[_PendingOrderSlot]] = {}
    last_expiry_sweep_mono = monotonic()

    # Two independent pub/sub subscriptions in one connection
    pubsub = redis.pubsub()
    await pubsub.subscribe(
        RedisChannels.MARKET_FEED_LTPC,
        RedisChannels.PAPER_PENDING_ORDERS_UPDATED,
    )
    logger.info("Pending-order matching engine started")

    # Warm the cache from DB at startup
    try:
        pending = await _rebuild_full_pending_cache()
        logger.info(
            "Pending-order cache warmed: %d instrument(s) with open orders",
            len(pending),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Pending-order cache warm failed — starting empty: %s", exc)

    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue

            raw = message["data"]
            if isinstance(raw, bytes):
                raw = raw.decode()

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            channel = message.get("channel", "")
            if isinstance(channel, bytes):
                channel = channel.decode()

            # ── Cache invalidation message ─────────────────────────────────────
            if channel == RedisChannels.PAPER_PENDING_ORDERS_UPDATED:
                instrument_key = data.get("instrument_key")
                try:
                    if instrument_key:
                        slots = await _rebuild_cache_for_instrument(instrument_key)
                        if slots:
                            pending[instrument_key] = slots
                        else:
                            pending.pop(instrument_key, None)
                    else:
                        # Full rebuild requested (e.g. admin recovery)
                        pending = await _rebuild_full_pending_cache()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Pending cache rebuild failed for instrument=%s: %s",
                        instrument_key, exc,
                    )
                continue

            # ── Market tick ───────────────────────────────────────────────────
            if channel != RedisChannels.MARKET_FEED_LTPC:
                continue

            instrument_key = data.get("instrument_key")
            ltp_raw = data.get("ltp")
            if instrument_key is None or ltp_raw is None:
                continue

            slots = pending.get(instrument_key)
            if not slots:
                continue  # no pending orders for this instrument — fast path

            ltp = Decimal(str(ltp_raw))
            filled_ids, expired_ids = await _check_and_fill_pending_orders(
                redis=redis,
                instrument_key=instrument_key,
                slots=slots,
                ltp=ltp,
            )

            # Prune filled/rejected/expired slots from the in-memory cache.
            # ``_rebuild_cache_for_instrument`` re-queries the DB; pruning
            # the matched IDs inline avoids a DB round-trip per tick.
            consumed = filled_ids | expired_ids
            if consumed:
                pending[instrument_key] = [
                    s for s in slots if s.order_id not in consumed
                ]
                if not pending[instrument_key]:
                    del pending[instrument_key]

            # ── DAY expiry sweep ──────────────────────────────────────────────
            now_mono = monotonic()
            if now_mono - last_expiry_sweep_mono >= _DAY_EXPIRY_SWEEP_S:
                try:
                    expired_count = await _expire_day_orders(redis, pending)
                    if expired_count:
                        logger.info(
                            "DAY order expiry sweep: %d order(s) expired", expired_count
                        )
                    # Rebuild cache after expiry to reflect DB state
                    if expired_count:
                        pending = await _rebuild_full_pending_cache()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("DAY order expiry sweep failed: %s", exc)
                last_expiry_sweep_mono = now_mono

    finally:
        await pubsub.unsubscribe(
            RedisChannels.MARKET_FEED_LTPC,
            RedisChannels.PAPER_PENDING_ORDERS_UPDATED,
        )
        await pubsub.aclose()


async def _rebuild_full_pending_cache() -> dict[str, list[_PendingOrderSlot]]:
    """
    Query all OPEN LIMIT/SL/SL-M orders from the DB and build the cache.

    Called at startup and after a full-cache invalidation message.
    """
    from app.models.paper_trading import PaperOrder

    cache: dict[str, list[_PendingOrderSlot]] = {}

    async with WorkerSessionLocal() as session:
        stmt = (
            select(PaperOrder)
            .where(
                PaperOrder.status == "OPEN",
                PaperOrder.order_type.in_(["LIMIT", "SL", "SL-M"]),
            )
        )
        rows = list((await session.execute(stmt)).scalars().all())

    for order in rows:
        slot = _PendingOrderSlot(
            order_id=order.id,
            portfolio_id=order.portfolio_id,
            transaction_type=order.transaction_type,
            order_type=order.order_type,
            validity=order.validity,
            price=order.price,
            trigger_price=order.trigger_price,
        )
        cache.setdefault(order.instrument_key, []).append(slot)

    return cache


async def _rebuild_cache_for_instrument(
    instrument_key: str,
) -> list[_PendingOrderSlot]:
    """
    Re-query OPEN pending orders for a single instrument.

    Called on targeted invalidation messages (place/cancel/fill events).
    """
    from app.models.paper_trading import PaperOrder

    async with WorkerSessionLocal() as session:
        stmt = (
            select(PaperOrder)
            .where(
                PaperOrder.instrument_key == instrument_key,
                PaperOrder.status == "OPEN",
                PaperOrder.order_type.in_(["LIMIT", "SL", "SL-M"]),
            )
        )
        rows = list((await session.execute(stmt)).scalars().all())

    return [
        _PendingOrderSlot(
            order_id=order.id,
            portfolio_id=order.portfolio_id,
            transaction_type=order.transaction_type,
            order_type=order.order_type,
            validity=order.validity,
            price=order.price,
            trigger_price=order.trigger_price,
        )
        for order in rows
    ]


def _slot_condition_met(slot: _PendingOrderSlot, ltp: Decimal) -> bool:
    """
    Return True when ltp satisfies the slot's price condition.

    Mirrors ``order_service._limit_condition_met`` but operates on the
    lightweight in-memory slot instead of the full ORM object.

    Conditions:
      BUY  LIMIT  : ltp <= limit_price   (market fell to our bid)
      SELL LIMIT  : ltp >= limit_price   (market rose to our ask)
      BUY  SL/SL-M: ltp >= trigger_price (stop triggered — market ran up)
      SELL SL/SL-M: ltp <= trigger_price (stop triggered — market fell)
    """
    if slot.order_type == "LIMIT":
        ref = slot.price
        if ref is None:
            return False
        return ltp <= ref if slot.transaction_type == "BUY" else ltp >= ref

    # SL or SL-M
    ref = slot.trigger_price
    if ref is None:
        return False
    return ltp >= ref if slot.transaction_type == "BUY" else ltp <= ref


async def _check_and_fill_pending_orders(
    redis: Redis,
    instrument_key: str,
    slots: list[_PendingOrderSlot],
    ltp: Decimal,
) -> tuple[set[UUID], set[UUID]]:
    """
    Evaluate all cached slots for ``instrument_key`` against the current LTP.

    For each slot whose price condition is met, attempt a fill via
    ``_fill_pending_order``.  Returns two sets:
      - filled_ids: order_ids successfully filled (or rejected at fill time)
      - expired_ids: order_ids that should have expired (empty here; handled by sweep)

    Each fill is attempted in its own DB session so a single failure does not
    roll back other fills in the same tick.
    """
    filled_ids: set[UUID] = set()
    expired_ids: set[UUID] = set()

    for slot in slots:
        if not _slot_condition_met(slot, ltp):
            continue

        try:
            await _fill_pending_order(redis, slot, ltp)
            filled_ids.add(slot.order_id)
        except Exception as exc:  # noqa: BLE001
            # Log and record as filled (so we stop retrying) only if it's a
            # permanent rejection (position limit, insufficient funds).
            # Transient DB errors leave the slot in place for the next tick.
            exc_name = type(exc).__name__
            if exc_name in ("PositionLimitExceededError", "InsufficientFundsError"):
                logger.info(
                    "Pending order %s permanently rejected at fill: %s",
                    slot.order_id, exc,
                )
                filled_ids.add(slot.order_id)
            else:
                logger.warning(
                    "Pending order fill attempt failed (will retry): order=%s ltp=%.4f: %s",
                    slot.order_id, float(ltp), exc,
                )

    return filled_ids, expired_ids


async def _fill_pending_order(
    redis: Redis,
    slot: _PendingOrderSlot,
    ltp: Decimal,
) -> None:
    """
    Attempt to fill a single pending order under a ``SELECT FOR UPDATE SKIP LOCKED``
    guard to prevent double-fill in multi-worker deployments.

    If the row is already locked by another worker, this call returns silently
    — the other worker will complete the fill.

    On success, publishes an ``order_filled`` WS event to the portfolio's
    ``cai:paper:pnl:{portfolio_id}`` channel so the browser updates instantly.
    """
    from app.models.paper_trading import PaperOrder
    from app.services.paper_trading.order_service import (
        _fill_price_for_order,
        execute_pending_order_fill,
    )

    async with WorkerSessionLocal() as session:
        # Acquire a SKIP LOCKED row lock — if another worker has this row
        # locked, ``scalar_one_or_none`` returns None and we bail out.
        stmt = (
            select(PaperOrder)
            .where(
                PaperOrder.id == slot.order_id,
                PaperOrder.status == "OPEN",
            )
            .with_for_update(skip_locked=True)
        )
        order = (await session.execute(stmt)).scalar_one_or_none()
        if order is None:
            # Already locked or filled by a concurrent worker — nothing to do.
            return

        fill_price = _fill_price_for_order(order, ltp)

        try:
            fill = await execute_pending_order_fill(
                session=session,
                redis=redis,
                order=order,
                fill_price=fill_price,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    # ── Publish order_filled WS event ─────────────────────────────────────────
    event = json.dumps({
        "type": "order_filled",
        "order_id": str(order.id),
        "portfolio_id": str(order.portfolio_id),
        "symbol": order.symbol,
        "transaction_type": order.transaction_type,
        "fill_price": float(fill_price),
        "quantity": order.quantity,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    channel = f"{_PNL_CHANNEL_PREFIX}{order.portfolio_id}"
    await redis.publish(channel, event)

    logger.info(
        "Pending order filled: order=%s symbol=%s tx=%s qty=%d price=%.4f",
        order.id, order.symbol, order.transaction_type,
        order.quantity, float(fill_price),
    )


async def _expire_day_orders(
    redis: Redis,
    pending: dict[str, list[_PendingOrderSlot]],
) -> int:
    """
    Expire all OPEN DAY orders past 15:30 IST (10:00 UTC).

    Iterates the in-memory cache to avoid a full table scan; only order_ids
    present in the cache are eligible.  DB update uses ``status='CANCELLED'``
    with a ``rejection_reason`` to preserve audit trail.

    Returns the number of orders expired.
    """
    from app.models.paper_trading import PaperOrder
    from app.services.paper_trading.portfolio_service import release_cash_reservation
    from app.services.paper_trading.order_service import _compute_reservation_amount

    now_utc = datetime.now(timezone.utc)
    if now_utc.time() < _NSE_CLOSE_UTC:
        return 0  # Market still open — nothing to expire

    # Collect all DAY order IDs from the cache
    day_order_ids: list[UUID] = [
        slot.order_id
        for slots in pending.values()
        for slot in slots
        if slot.validity == "DAY"
    ]
    if not day_order_ids:
        return 0

    expired_count = 0

    async with WorkerSessionLocal() as session:
        stmt = (
            select(PaperOrder)
            .where(
                PaperOrder.id.in_(day_order_ids),
                PaperOrder.status == "OPEN",
            )
            .with_for_update(skip_locked=True)
        )
        orders = list((await session.execute(stmt)).scalars().all())

        for order in orders:
            # Release cash reservation for BUY orders
            from app.models.paper_trading import Portfolio
            portfolio_stmt = select(Portfolio).where(Portfolio.id == order.portfolio_id)
            portfolio = (await session.execute(portfolio_stmt)).scalar_one_or_none()

            if portfolio is not None:
                reservation = _compute_reservation_amount(order)
                if reservation > _ZERO:
                    await release_cash_reservation(session, portfolio, reservation)

            order.status = "CANCELLED"
            order.rejection_reason = "DAY order expired at NSE market close (15:30 IST)."
            order.updated_at = now_utc
            expired_count += 1

            # Publish order_expired WS event
            event = json.dumps({
                "type": "order_expired",
                "order_id": str(order.id),
                "portfolio_id": str(order.portfolio_id),
                "symbol": order.symbol,
                "ts": now_utc.isoformat(),
            })
            await redis.publish(
                f"{_PNL_CHANNEL_PREFIX}{order.portfolio_id}", event
            )

        await session.commit()

    return expired_count
