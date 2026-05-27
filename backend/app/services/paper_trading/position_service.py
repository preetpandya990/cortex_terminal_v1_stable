"""
Paper Trading — Position Service
==================================
Manages paper position lifecycle: opening, WAC recalculation on partial adds,
partial/full close, and writing the PaperTradeOutcome audit record on full close.

WAC (Weighted Average Cost) — SEBI mandate for NSE equity delivery:
    new_avg = (old_qty × old_avg + fill_qty × fill_price) / (old_qty + fill_qty)

Position constraints:
  - At most one OPEN position per (portfolio_id, symbol) is enforced by the DB
    partial unique index `uq_paper_positions_portfolio_symbol_open`.
  - The service does NOT re-validate this invariant in Python; the DB constraint
    is the authoritative gate and will raise IntegrityError on violation.
  - `quantity >= 0` DB CHECK ensures no negative qty slips through.

T+1 Settlement enforcement is in order_service._assert_t1_settlement.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from app.services.paper_trading.order_service import _TradeContext

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    ForbiddenError,
    InvalidOrderError,
    PositionNotFoundError,
)
from app.models.paper_trading import (
    PaperFill,
    PaperOrder,
    PaperPosition,
    PaperTradeOutcome,
    Portfolio,
)
from app.models.trade_suggestions import TradeSuggestion
from app.schemas.paper_trading import (
    ClosePositionRequest,
    PositionsListResponse,
)

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_DP4 = Decimal("0.0001")
_DP8 = Decimal("0.00000001")


# ──────────────────────────────────────────────────────────────────────────────
# Public API — used by order_service
# ──────────────────────────────────────────────────────────────────────────────

async def apply_buy_fill_to_position(
    session: AsyncSession,
    portfolio: Portfolio,
    order: PaperOrder,
    fill: PaperFill,
    ctx: "_TradeContext",  # type: ignore[name-defined]  # forward ref from order_service
) -> PaperPosition:
    """
    Open a new position or add to an existing OPEN position (WAC).

    Called atomically from within order_service._execute_fill, inside the
    caller's transaction.

    Returns the (new or updated) PaperPosition.
    """
    existing = await _find_open_position(session, portfolio.id, order.symbol)

    if existing is None:
        position = _open_new_position(portfolio, order, fill, ctx)
        session.add(position)
    else:
        # Add-to-position: recalculate WAC
        _update_wac(existing, fill)
        position = existing

    await session.flush()
    await session.refresh(position)
    logger.info(
        "Position updated (BUY fill): portfolio=%s symbol=%s "
        "new_qty=%d avg_cost=%.4f",
        portfolio.id, order.symbol,
        position.quantity, float(position.avg_cost_price),
    )
    return position


async def apply_sell_fill_to_position(
    session: AsyncSession,
    portfolio: Portfolio,
    position: PaperPosition,
    fill: PaperFill,
    exit_reason: str,
) -> PaperPosition:
    """
    Reduce or fully close an open position from a SELL fill.

    For partial closes, realized P&L is accumulated on the position and
    avg_cost_price is unchanged (SEBI-mandated WAC preserves cost basis).

    For full closes, the position is marked CLOSED and a PaperTradeOutcome
    audit record is written.

    Returns the updated PaperPosition.
    """
    sell_qty = fill.fill_quantity
    if sell_qty > position.quantity:
        raise InvalidOrderError(
            f"Sell quantity {sell_qty} exceeds position quantity {position.quantity} "
            f"for {position.symbol}."
        )

    # Realized P&L on this sell: (exit_price - avg_cost) × qty − charges
    # charges are already deducted from net_amount; here we track gross P&L
    gross_pnl = (fill.fill_price - position.avg_cost_price) * Decimal(sell_qty)
    gross_pnl = gross_pnl.quantize(_DP4, rounding=ROUND_HALF_UP)

    position.realized_pnl += gross_pnl
    position.total_charges += fill.total_charges
    position.quantity -= sell_qty
    position.updated_at = datetime.now(timezone.utc)

    if position.quantity == 0:
        # Full close
        position.status = "CLOSED"
        position.closed_at = datetime.now(timezone.utc)
        await session.flush()

        # Write audit outcome
        await _write_outcome(session, portfolio, position, fill, exit_reason)
    else:
        await session.flush()

    await session.refresh(position)
    logger.info(
        "Position updated (SELL fill): portfolio=%s symbol=%s "
        "remaining_qty=%d realized_pnl=%.4f status=%s",
        portfolio.id, position.symbol,
        position.quantity, float(position.realized_pnl), position.status,
    )
    return position


# ──────────────────────────────────────────────────────────────────────────────
# Public API — REST endpoints
# ──────────────────────────────────────────────────────────────────────────────

async def get_position(
    session: AsyncSession,
    position_id: UUID,
    user_id: int,
) -> PaperPosition:
    """
    Fetch a position by ID, asserting ownership via portfolio.

    Raises
    ------
    PositionNotFoundError
    ForbiddenError
    """
    stmt = (
        select(PaperPosition)
        .join(Portfolio, PaperPosition.portfolio_id == Portfolio.id)
        .where(PaperPosition.id == position_id)
    )
    result = await session.execute(stmt)
    position = result.scalar_one_or_none()
    if position is None:
        raise PositionNotFoundError(f"Position {position_id} not found.")

    portfolio_stmt = select(Portfolio).where(Portfolio.id == position.portfolio_id)
    portfolio = (await session.execute(portfolio_stmt)).scalar_one_or_none()
    if portfolio is None or portfolio.user_id != user_id:
        raise ForbiddenError("You do not have access to this position.")

    return position


async def list_positions(
    session: AsyncSession,
    user_id: int,
    status_filter: str | None = None,
) -> PositionsListResponse:
    """
    List all positions for the user's active portfolio.

    Returns a full (unpaginated) list — portfolio open positions are bounded
    by max_open_positions (≤ 100), so memory is never a concern.
    """
    from app.services.paper_trading.portfolio_service import get_active_portfolio
    from app.schemas.paper_trading import PaperPositionResponse

    portfolio = await get_active_portfolio(session, user_id)

    stmt = select(PaperPosition).where(
        PaperPosition.portfolio_id == portfolio.id
    )
    if status_filter:
        stmt = stmt.where(PaperPosition.status == status_filter.upper())
    stmt = stmt.order_by(PaperPosition.opened_at.desc())

    result = await session.execute(stmt)
    positions = list(result.scalars().all())

    open_positions = [p for p in positions if p.status == "OPEN"]
    closed_positions = [p for p in positions if p.status == "CLOSED"]

    total_unrealized = sum(
        (p.unrealized_pnl or _ZERO) for p in open_positions
    )

    return PositionsListResponse(
        positions=[PaperPositionResponse.model_validate(p) for p in positions],
        open_count=len(open_positions),
        closed_count=len(closed_positions),
        total_unrealized_pnl=float(total_unrealized),
    )


async def close_position(
    session: AsyncSession,
    redis,
    position_id: UUID,
    user_id: int,
    payload: ClosePositionRequest,
) -> tuple[PaperPosition, "PaperOrder | None", bool]:
    """
    Close a position (fully or partially) via the REST API.

    MARKET close: fills immediately at LTP − 3 bps slippage.  Returns
    (updated_position, None, False).

    LIMIT close: queues a pending SELL order at the requested price.  The
    position is NOT updated until the pnl_worker matches the order against a
    live tick.  Returns (unchanged_position, pending_sell_order, True).

    Returns
    -------
    (position, pending_order, queued)
        queued=True  → LIMIT order was queued; position is still OPEN.
        queued=False → MARKET fill completed; position status reflects close.
    """
    from app.services.paper_trading.order_service import (
        _assert_t1_settlement,
        _compute_settlement_date,
        _DEFAULT_SLIPPAGE_BPS,
        _get_ltp,
    )
    from app.services.paper_trading.charge_calculator import calculate_charges
    from app.services.paper_trading.portfolio_service import credit_cash

    position = await get_position(session, position_id, user_id)
    portfolio_stmt = select(Portfolio).where(Portfolio.id == position.portfolio_id)
    portfolio = (await session.execute(portfolio_stmt)).scalar_one_or_none()

    if not position.is_open:
        raise InvalidOrderError(f"Position {position_id} is already CLOSED.")
    if payload.quantity > position.quantity:
        raise InvalidOrderError(
            f"Close quantity {payload.quantity} exceeds open quantity {position.quantity}."
        )

    # ── T+1 settlement check (CNC only) ──────────────────────────────────────
    if position.product_type == "CNC":
        await _assert_t1_settlement(session, position.portfolio_id, position.symbol)

    product_type: str = position.product_type

    # ── LIMIT close — queue a pending SELL order ──────────────────────────────
    if payload.order_type.value == "LIMIT":
        if not payload.price:
            raise InvalidOrderError("LIMIT close requires a price.")
        limit_price = Decimal(str(payload.price))
        pending_sell = PaperOrder(
            portfolio_id=position.portfolio_id,
            suggestion_id=position.suggestion_id,
            symbol=position.symbol,
            instrument_key=position.instrument_key,
            transaction_type="SELL",
            product_type=product_type,
            order_type="LIMIT",
            validity="DAY",
            quantity=payload.quantity,
            price=limit_price,
            status="OPEN",
        )
        session.add(pending_sell)
        await session.flush()
        await session.refresh(pending_sell)
        logger.info(
            "LIMIT close queued: position=%s symbol=%s qty=%d price=%.4f order=%s",
            position_id, position.symbol, payload.quantity,
            float(limit_price), pending_sell.id,
        )
        return position, pending_sell, True

    # ── MARKET close — fill immediately ──────────────────────────────────────
    ltp = await _get_ltp(redis, position.instrument_key)
    if ltp is None:
        ltp = position.last_price or position.avg_cost_price
        logger.warning(
            "No LTP for %s at close — using cached price %.4f",
            position.instrument_key, float(ltp),
        )

    slippage = ltp * _DEFAULT_SLIPPAGE_BPS / Decimal("10000")
    exit_price = (ltp - slippage).quantize(Decimal("0.0001"))

    charges = calculate_charges(
        transaction_type="SELL",
        product_type=product_type,
        fill_price=exit_price,
        fill_quantity=payload.quantity,
    )
    settlement_date = _compute_settlement_date("SELL", product_type)

    sell_order = PaperOrder(
        portfolio_id=position.portfolio_id,
        suggestion_id=position.suggestion_id,
        symbol=position.symbol,
        instrument_key=position.instrument_key,
        transaction_type="SELL",
        product_type=product_type,
        order_type="MARKET",
        validity="DAY",
        quantity=payload.quantity,
        status="PENDING",
    )
    session.add(sell_order)
    await session.flush()

    fill = PaperFill(
        order_id=sell_order.id,
        portfolio_id=position.portfolio_id,
        symbol=position.symbol,
        fill_quantity=payload.quantity,
        fill_price=exit_price,
        slippage_bps=_DEFAULT_SLIPPAGE_BPS,
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

    position = await apply_sell_fill_to_position(
        session=session,
        portfolio=portfolio,
        position=position,
        fill=fill,
        exit_reason=payload.exit_reason.value,
    )

    sell_order.status = "COMPLETE"
    sell_order.updated_at = datetime.now(timezone.utc)
    await session.flush()

    return position, None, False


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _open_new_position(
    portfolio: Portfolio,
    order: PaperOrder,
    fill: PaperFill,
    ctx: "_TradeContext",  # type: ignore[name-defined]
) -> PaperPosition:
    return PaperPosition(
        portfolio_id=portfolio.id,
        suggestion_id=ctx.suggestion_id,
        symbol=order.symbol,
        instrument_key=order.instrument_key,
        quantity=fill.fill_quantity,
        avg_cost_price=fill.fill_price,
        last_price=fill.fill_price,
        unrealized_pnl=_ZERO,
        realized_pnl=_ZERO,
        total_charges=fill.total_charges,
        side="LONG" if order.transaction_type == "BUY" else "SHORT",
        target_price_1=ctx.take_profit_1,
        target_price_2=ctx.take_profit_2,
        target_price_3=ctx.take_profit_3,
        stop_loss=ctx.stop_loss,
        status="OPEN",
        opened_at=datetime.now(timezone.utc),
    )


def _update_wac(position: PaperPosition, fill: PaperFill) -> None:
    """Recalculate WAC and update position quantity + charges."""
    old_qty = Decimal(position.quantity)
    old_avg = position.avg_cost_price
    fill_qty = Decimal(fill.fill_quantity)
    fill_price = fill.fill_price

    new_qty = old_qty + fill_qty
    new_avg = ((old_qty * old_avg) + (fill_qty * fill_price)) / new_qty
    new_avg = new_avg.quantize(_DP4, rounding=ROUND_HALF_UP)

    position.quantity = int(new_qty)
    position.avg_cost_price = new_avg
    position.total_charges += fill.total_charges
    position.updated_at = datetime.now(timezone.utc)


async def _find_open_position(
    session: AsyncSession,
    portfolio_id: UUID,
    symbol: str,
) -> PaperPosition | None:
    stmt = select(PaperPosition).where(
        and_(
            PaperPosition.portfolio_id == portfolio_id,
            PaperPosition.symbol == symbol,
            PaperPosition.status == "OPEN",
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _write_outcome(
    session: AsyncSession,
    portfolio: Portfolio,
    position: PaperPosition,
    exit_fill: PaperFill,
    exit_reason: str,
) -> PaperTradeOutcome:
    """
    Write the immutable PaperTradeOutcome audit record after full close.

    This record is the source of truth for ML feedback.  All suggestion
    snapshot values are copied from the position (which was populated at
    open time from the TradeSuggestion) so the audit record is self-contained
    even after the suggestion expires.
    """
    entry_price = position.avg_cost_price
    exit_price = exit_fill.fill_price
    qty = exit_fill.fill_quantity
    opened_at = position.opened_at
    closed_at = position.closed_at or datetime.now(timezone.utc)

    gross_pnl = (exit_price - entry_price) * Decimal(qty)
    gross_pnl = gross_pnl.quantize(_DP4, rounding=ROUND_HALF_UP)
    total_charges = position.total_charges
    net_pnl = (gross_pnl - total_charges).quantize(_DP4, rounding=ROUND_HALF_UP)

    cost_basis = entry_price * Decimal(qty)
    pnl_pct = (
        (net_pnl / cost_basis * Decimal("100")).quantize(_DP4, rounding=ROUND_HALF_UP)
        if cost_basis > _ZERO
        else _ZERO
    )

    # Seconds between open and close
    hold_duration_seconds = max(
        0,
        int((closed_at - opened_at.replace(tzinfo=timezone.utc)
             if opened_at.tzinfo is None else closed_at - opened_at).total_seconds()),
    )

    # Fetch source suggestion for snapshot values
    suggestion: TradeSuggestion | None = None
    if position.suggestion_id is not None:
        stmt = select(TradeSuggestion).where(
            TradeSuggestion.suggestion_id == position.suggestion_id
        )
        suggestion = (await session.execute(stmt)).scalar_one_or_none()

    # For manually-opened positions with no linked TradeSuggestion, look for a
    # matching AITradingSignal so we can capture confidence level and price targets.
    from app.services.paper_trading.outcome_service import (
        _derive_confidence_level,
        _find_matching_ai_signal,
    )

    ai_signal = None
    if suggestion is None:
        signal_direction = "BUY" if position.side == "LONG" else "SELL"
        ai_signal = await _find_matching_ai_signal(
            session,
            symbol=position.symbol,
            direction=signal_direction,
            opened_at=opened_at,
            closed_at=closed_at,
        )
        if ai_signal:
            logger.info(
                "Manual trade %s/%s matched to AITradingSignal id=%s "
                "(confidence=%.2f generated=%s)",
                portfolio.id, position.symbol,
                ai_signal.id, float(ai_signal.confidence_score),
                ai_signal.signal_timestamp.isoformat(),
            )

    # Entry slippage against the best available reference price
    entry_slippage_pct: Decimal | None = None
    ref_entry = (suggestion.entry_price if suggestion else None) or (
        ai_signal.target_price if ai_signal else None
    )
    if ref_entry and ref_entry > _ZERO:
        entry_slippage_pct = (
            (entry_price - ref_entry) / ref_entry * Decimal("100")
        ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # Price target resolution — priority: suggestion → position snapshot → ai_signal
    _sl = (suggestion.stop_loss if suggestion else None) or position.stop_loss or (
        ai_signal.stop_loss if ai_signal else None
    )
    _tp1 = (suggestion.take_profit_1 if suggestion else None) or position.target_price_1 or (
        ai_signal.target_price if ai_signal else None
    )
    _tp2 = (suggestion.take_profit_2 if suggestion else None) or position.target_price_2
    _tp3 = (suggestion.take_profit_3 if suggestion else None) or position.target_price_3

    # Confidence resolution — suggestion takes priority; fall back to ai_signal
    _confidence_level = suggestion.confidence_level if suggestion else (
        _derive_confidence_level(ai_signal.confidence_score) if ai_signal else None
    )
    _consensus_score = suggestion.consensus_score if suggestion else (
        (ai_signal.confidence_score * Decimal("100")).quantize(Decimal("0.01")) if ai_signal else None
    )

    outcome = PaperTradeOutcome(
        portfolio_id=portfolio.id,
        position_id=position.id,
        suggestion_id=position.suggestion_id,
        user_id=portfolio.user_id,
        symbol=position.symbol,
        signal_direction="BUY" if position.side == "LONG" else "SELL",
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=qty,
        gross_pnl=gross_pnl,
        total_charges=total_charges,
        net_pnl=net_pnl,
        pnl_pct=pnl_pct,
        hold_duration_seconds=hold_duration_seconds,
        exit_reason=exit_reason,
        suggested_entry_price=suggestion.entry_price if suggestion else None,
        suggested_stop_loss=_sl,
        suggested_tp1=_tp1,
        suggested_tp2=_tp2,
        suggested_tp3=_tp3,
        suggestion_consensus_score=_consensus_score,
        suggestion_confidence_level=_confidence_level,
        entry_slippage_pct=entry_slippage_pct,
        # ML feedback fields — populated async by outcome_service
        ml_direction_correct=None,
        hit_tp1=False,
        hit_tp2=False,
        hit_tp3=False,
        hit_sl=False,
        market_regime_at_entry=None,
        created_at=datetime.now(timezone.utc),
    )
    session.add(outcome)
    await session.flush()

    logger.info(
        "Outcome written: portfolio=%s symbol=%s net_pnl=%.4f pnl_pct=%.2f%% "
        "exit_reason=%s hold=%ds",
        portfolio.id, position.symbol,
        float(net_pnl), float(pnl_pct), exit_reason, hold_duration_seconds,
    )
    return outcome
