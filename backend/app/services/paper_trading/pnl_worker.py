"""
Paper Trading — P&L Recompute Worker
======================================
Background task that bridges the Upstox market feed tick stream into the
paper trading subsystem:

  1. Subscribes to `cai:market-feed:ltpc` Redis pub/sub (same channel that
     MarketFeedService publishes to after protobuf decoding + throttling).

  2. Writes each LTP into `cai:ltp:{instrument_key}` (simple string key,
     TTL 5 min so stale prices auto-expire).

  3. Maintains a `cai:paper:dirty_portfolios` Redis set: when an LTP arrives
     for an instrument that has an open paper position, the portfolio_id is
     added to the dirty set.

  4. Every 500 ms, drains the dirty set and for each dirty portfolio:
       a. Fetches all OPEN positions from DB
       b. Recomputes unrealized P&L using cached LTPs
       c. Checks SL and TP1/TP2/TP3 breach conditions
       d. Auto-closes breached positions (calls position_service)
       e. Publishes a LivePnLUpdate frame to `cai:paper:pnl:{portfolio_id}`

Architecture notes:
  - The worker runs as a single asyncio task, started at app startup.
  - It uses the WorkerSessionLocal (dedicated DB connection pool, isolated
    from the API pool) so long DB operations do not starve API requests.
  - Each 500 ms tick is a short burst of I/O; the task sleeps between ticks.
  - If the worker loop throws an unhandled exception it logs and restarts
    after a 5-second back-off to avoid log spam.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import and_, select

from app.core.database import WorkerSessionLocal
from app.core.redis import RedisChannels

logger = logging.getLogger(__name__)

# ── Redis key constants ────────────────────────────────────────────────────────
_LTP_KEY_PREFIX = "cai:ltp:"
_LTP_TTL_SECONDS = 300          # 5-minute TTL — stale if no tick for 5 min
_DIRTY_SET_KEY = "cai:paper:dirty_portfolios"
_PNL_CHANNEL_PREFIX = "cai:paper:pnl:"

# ── Symbol → portfolio mapping key (HSET: symbol → JSON list of portfolio_ids)
_SYMBOL_PORTFOLIOS_KEY = "cai:paper:symbol_portfolios"
_SYMBOL_PORTFOLIOS_TTL = 60     # Rebuild every 60 s

# Tick interval
_RECOMPUTE_INTERVAL_MS = 500
_ZERO = Decimal("0")


# ──────────────────────────────────────────────────────────────────────────────
# Worker entry-point
# ──────────────────────────────────────────────────────────────────────────────

async def run_pnl_worker(redis: Redis) -> None:
    """
    Main worker coroutine — run with asyncio.create_task at app startup.

    Runs forever; catches and logs exceptions with 5-second back-off.
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
# Internal — main loop
# ──────────────────────────────────────────────────────────────────────────────

async def _worker_loop(redis: Redis) -> None:
    """Subscribe to market feed and spin the 500 ms recompute loop."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(RedisChannels.MARKET_FEED_LTPC)
    logger.info("PnL worker subscribed to %s", RedisChannels.MARKET_FEED_LTPC)

    last_recompute = 0.0

    try:
        while True:
            # ── Drain all pending tick messages ───────────────────────────────
            # get_message(timeout=0) is non-blocking; we drain the backlog.
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.0
                )
                if message is None:
                    break
                await _handle_tick_message(redis, message)

            # ── 500 ms recompute gate ─────────────────────────────────────────
            import time
            now = time.monotonic()
            if now - last_recompute >= (_RECOMPUTE_INTERVAL_MS / 1000.0):
                await _recompute_dirty_portfolios(redis)
                last_recompute = now

            # Yield to event loop for a brief moment before next drain cycle
            await asyncio.sleep(0.05)

    finally:
        await pubsub.unsubscribe(RedisChannels.MARKET_FEED_LTPC)
        await pubsub.aclose()


async def _handle_tick_message(redis: Redis, message: dict) -> None:
    """
    Process a single LTPC tick message from the market feed.

    Side-effects:
      - Writes LTP to `cai:ltp:{instrument_key}` (TTL 5 min)
      - Adds portfolio_ids with open positions in this instrument to dirty set
    """
    try:
        data = json.loads(message["data"])
    except (json.JSONDecodeError, KeyError):
        return

    instrument_key = data.get("instrument_key")
    ltp = data.get("ltp")
    if instrument_key is None or ltp is None:
        return

    # Write LTP cache
    await redis.set(f"{_LTP_KEY_PREFIX}{instrument_key}", str(ltp), ex=_LTP_TTL_SECONDS)

    # Mark portfolios dirty
    portfolio_ids = await _get_portfolios_for_instrument(redis, instrument_key)
    if portfolio_ids:
        await redis.sadd(_DIRTY_SET_KEY, *[str(p) for p in portfolio_ids])


async def _recompute_dirty_portfolios(redis: Redis) -> None:
    """
    Drain the dirty set and recompute P&L for each portfolio.

    Uses SMEMBERS + DEL (atomic enough for this use case — missed portfolio_ids
    will be re-added on the next tick, so no correctness issue from races).
    """
    portfolio_ids_raw = await redis.smembers(_DIRTY_SET_KEY)
    if not portfolio_ids_raw:
        return

    # Clear the dirty set immediately so new ticks can re-add
    await redis.delete(_DIRTY_SET_KEY)

    for raw in portfolio_ids_raw:
        portfolio_id_str = raw if isinstance(raw, str) else raw.decode()
        try:
            portfolio_id = UUID(portfolio_id_str)
            await _recompute_portfolio_pnl(redis, portfolio_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "PnL recompute failed for portfolio %s: %s", portfolio_id_str, exc
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
    # Simple: re-read from the outcome aggregate (cheap, single query)
    from app.services.paper_trading.outcome_service import _ZERO as Z
    from app.models.paper_trading import PaperTradeOutcome
    from sqlalchemy import func

    async with WorkerSessionLocal() as session2:
        agg = await session2.execute(
            select(func.coalesce(func.sum(PaperTradeOutcome.net_pnl), Z).label("total_net"))
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

    # Schedule ML feedback computation
    from app.services.paper_trading.outcome_service import compute_ml_feedback
    from app.models.paper_trading import PaperTradeOutcome
    outcome_stmt = (
        select(PaperTradeOutcome)
        .where(PaperTradeOutcome.position_id == position.id)
    )
    outcome = (await session.execute(outcome_stmt)).scalar_one_or_none()
    if outcome:
        asyncio.create_task(
            _compute_feedback_deferred(outcome.id),
            name=f"ml_feedback_{outcome.id}",
        )


async def _compute_feedback_deferred(outcome_id: UUID) -> None:
    """Fire-and-forget: compute ML feedback in a separate DB session."""
    try:
        async with WorkerSessionLocal() as session:
            from app.services.paper_trading.outcome_service import compute_ml_feedback
            await compute_ml_feedback(session, outcome_id)
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Deferred ML feedback failed for outcome %s: %s", outcome_id, exc)


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
