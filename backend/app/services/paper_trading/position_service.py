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
from uuid import UUID

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
    suggestion: TradeSuggestion,
) -> PaperPosition:
    """
    Open a new position or add to an existing OPEN position (WAC).

    Called atomically from within order_service._execute_fill, inside the
    caller's transaction.

    Returns the (new or updated) PaperPosition.
    """
    existing = await _find_open_position(session, portfolio.id, order.symbol)

    if existing is None:
        position = _open_new_position(portfolio, order, fill, suggestion)
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
) -> PaperPosition:
    """
    Close a position (fully or partially) via the REST API.

    Creates a synthetic SELL order and fill at the current LTP, then
    delegates to apply_sell_fill_to_position.
    """
    from app.services.paper_trading.order_service import (
        _execute_fill,
        _get_ltp,
        _compute_settlement_date,
    )
    from app.services.paper_trading.charge_calculator import calculate_charges
    from app.services.paper_trading.portfolio_service import credit_cash

    position = await get_position(session, position_id, user_id)
    portfolio_stmt = select(Portfolio).where(Portfolio.id == position.portfolio_id)
    portfolio = (await session.execute(portfolio_stmt)).scalar_one_or_none()

    if not position.is_open:
        raise InvalidOrderError(
            f"Position {position_id} is already CLOSED."
        )
    if payload.quantity > position.quantity:
        raise InvalidOrderError(
            f"Close quantity {payload.quantity} exceeds open quantity {position.quantity}."
        )

    # ── T+1 check for CNC ────────────────────────────────────────────────────
    # Infer product_type from the opening order (simple approach: check fills)
    from app.services.paper_trading.order_service import _assert_t1_settlement
    from app.models.paper_trading import PaperOrder as PO
    opening_order_stmt = (
        select(PO)
        .join(PaperFill, PO.id == PaperFill.order_id)
        .where(
            and_(
                PaperFill.portfolio_id == position.portfolio_id,
                PaperFill.symbol == position.symbol,
                PO.transaction_type == "BUY",
            )
        )
        .order_by(PO.placed_at.desc())
        .limit(1)
    )
    opening_order = (await session.execute(opening_order_stmt)).scalar_one_or_none()
    if opening_order and opening_order.product_type == "CNC":
        await _assert_t1_settlement(session, position.portfolio_id, position.symbol)

    # ── Resolve exit price ────────────────────────────────────────────────────
    ltp = await _get_ltp(redis, position.instrument_key)
    if ltp is None:
        ltp = position.last_price or position.avg_cost_price
        logger.warning(
            "No LTP for %s at close — using cached price %.4f",
            position.instrument_key, float(ltp),
        )

    exit_price: Decimal
    if payload.order_type.value == "LIMIT" and payload.price:
        exit_price = Decimal(str(payload.price))
    else:
        # MARKET close: apply adverse slippage
        from app.services.paper_trading.order_service import _DEFAULT_SLIPPAGE_BPS
        slippage = ltp * _DEFAULT_SLIPPAGE_BPS / Decimal("10000")
        exit_price = (ltp - slippage).quantize(Decimal("0.0001"))

    product_type = opening_order.product_type if opening_order else "CNC"

    charges = calculate_charges(
        transaction_type="SELL",
        product_type=product_type,
        fill_price=exit_price,
        fill_quantity=payload.quantity,
    )
    settlement_date = _compute_settlement_date("SELL", product_type)

    # ── Create synthetic SELL order and fill ──────────────────────────────────
    sell_order = PaperOrder(
        portfolio_id=position.portfolio_id,
        suggestion_id=position.suggestion_id,
        symbol=position.symbol,
        instrument_key=position.instrument_key,
        transaction_type="SELL",
        product_type=product_type,
        order_type=payload.order_type.value,
        validity="DAY",
        quantity=payload.quantity,
        price=exit_price if payload.order_type.value == "LIMIT" else None,
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
        slippage_bps=_ZERO if payload.order_type.value == "LIMIT" else Decimal("3"),
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

    # Credit cash
    await credit_cash(session, portfolio, charges.net_amount)

    # Update position
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

    return position


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _open_new_position(
    portfolio: Portfolio,
    order: PaperOrder,
    fill: PaperFill,
    suggestion: TradeSuggestion,
) -> PaperPosition:
    return PaperPosition(
        portfolio_id=portfolio.id,
        suggestion_id=suggestion.suggestion_id if hasattr(suggestion, "suggestion_id") else None,
        symbol=order.symbol,
        instrument_key=order.instrument_key,
        quantity=fill.fill_quantity,
        avg_cost_price=fill.fill_price,
        last_price=fill.fill_price,
        unrealized_pnl=_ZERO,
        realized_pnl=_ZERO,
        total_charges=fill.total_charges,
        side="LONG" if order.transaction_type == "BUY" else "SHORT",
        target_price_1=getattr(suggestion, "take_profit_1", None),
        target_price_2=getattr(suggestion, "take_profit_2", None),
        target_price_3=getattr(suggestion, "take_profit_3", None),
        stop_loss=getattr(suggestion, "stop_loss", None),
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

    # Entry slippage: how far actual entry_price deviated from suggested
    entry_slippage_pct: Decimal | None = None
    if suggestion and suggestion.entry_price and suggestion.entry_price > _ZERO:
        entry_slippage_pct = (
            (entry_price - suggestion.entry_price) / suggestion.entry_price * Decimal("100")
        ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

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
        # Suggestion snapshot (None if no suggestion attached)
        suggested_entry_price=suggestion.entry_price if suggestion else None,
        suggested_stop_loss=suggestion.stop_loss if suggestion else None,
        suggested_tp1=suggestion.take_profit_1 if suggestion else None,
        suggested_tp2=suggestion.take_profit_2 if suggestion else None,
        suggested_tp3=suggestion.take_profit_3 if suggestion else None,
        suggestion_consensus_score=suggestion.consensus_score if suggestion else None,
        suggestion_confidence_level=suggestion.confidence_level if suggestion else None,
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
