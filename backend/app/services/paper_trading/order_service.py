"""
Paper Trading — Order Service
================================
Handles order placement, cancellation, and MARKET-order fill simulation.

Fill simulation model:
  MARKET orders  → immediate fill at current LTP (from Redis tick cache) ± slippage
  LIMIT / SL / SL-M → written to DB as OPEN; the P&L worker matches them when
                       the price condition is satisfied.

Redis tick cache key: `cai:ltp:{instrument_key}` → string float (set by pnl_worker
which subscribes to cai:market-feed:ltpc and writes the latest LTP per instrument).
If the key is absent (symbol not yet ticked, market closed, mock env), the order
falls back to the TradeSuggestion's entry_price with a slippage adjustment.

Slippage model:
  Fill price = LTP ± (LTP × slippage_bps / 10_000)
  BUY  → LTP + slippage  (adverse — you pay a little more)
  SELL → LTP − slippage  (adverse — you receive a little less)

T+1 settlement (CNC only):
  CNC BUY fills settle on T+1 (next NSE trading day). The service records
  settlement_date on the fill. A SELL order on a CNC position is rejected if
  ALL open shares were bought today (settlement_date > today). This mirrors the
  real NSE T+1 constraint where delivery shares aren't in the demat until T+1.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    ForbiddenError,
    InsufficientFundsError,
    InvalidOrderError,
    OrderNotFoundError,
    PortfolioNotFoundError,
    PositionLimitExceededError,
    SettlementPendingError,
)
from app.models.paper_trading import (
    PaperFill,
    PaperOrder,
    PaperPosition,
    Portfolio,
)
from app.models.trade_suggestions import TradeSuggestion
from app.schemas.paper_trading import (
    ClosePositionRequest,
    PlaceOrderRequest,
)
from app.services.market_calendar import nse_calendar
from app.services.paper_trading.charge_calculator import calculate_charges
from app.services.paper_trading.portfolio_service import (
    credit_cash,
    deduct_cash,
    get_active_portfolio,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

# Redis key for LTP cache (written by pnl_worker from market feed subscription)
_LTP_KEY_PREFIX = "cai:ltp:"

# Default slippage for liquid NSE large-caps (3 basis points)
_DEFAULT_SLIPPAGE_BPS = Decimal("3")

_ZERO = Decimal("0")


# ──────────────────────────────────────────────────────────────────────────────
# Trade context — unified carrier for signal-backed and manual orders
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class _TradeContext:
    """
    Carries the instrument and signal metadata needed throughout the order
    placement pipeline.  Built from either a TradeSuggestion (signal-backed)
    or directly from PlaceOrderRequest fields (manual trade).
    """
    suggestion_id: UUID | None
    symbol: str
    instrument_key: str
    entry_price: Decimal | None
    stop_loss: Decimal | None
    take_profit_1: Decimal | None
    take_profit_2: Decimal | None
    take_profit_3: Decimal | None


def _ctx_from_suggestion(suggestion: TradeSuggestion) -> _TradeContext:
    def _dec(v: object) -> Decimal | None:
        return Decimal(str(v)) if v is not None else None

    return _TradeContext(
        suggestion_id=suggestion.suggestion_id,
        symbol=suggestion.symbol,
        instrument_key=suggestion.instrument_key or suggestion.symbol,
        entry_price=_dec(suggestion.entry_price),
        stop_loss=_dec(suggestion.stop_loss),
        take_profit_1=_dec(suggestion.take_profit_1),
        take_profit_2=_dec(suggestion.take_profit_2),
        take_profit_3=_dec(suggestion.take_profit_3),
    )


def _ctx_from_payload(payload: PlaceOrderRequest) -> _TradeContext:
    return _TradeContext(
        suggestion_id=None,
        symbol=(payload.symbol or "").upper(),
        instrument_key=payload.instrument_key or "",
        entry_price=Decimal(str(payload.entry_price)) if payload.entry_price else None,
        stop_loss=None,
        take_profit_1=None,
        take_profit_2=None,
        take_profit_3=None,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public API — Place order
# ──────────────────────────────────────────────────────────────────────────────

async def place_order(
    session: AsyncSession,
    redis: Redis,
    user_id: int,
    payload: PlaceOrderRequest,
) -> tuple[PaperOrder, PaperFill | None]:
    """
    Place a paper order.  Supports two modes:

    Signal-backed: payload.suggestion_id is set — fetches TradeSuggestion and
                   derives instrument metadata from it.
    Manual:        payload.suggestion_id is None — uses symbol/instrument_key/
                   entry_price supplied directly in the payload.

    Returns
    -------
    (order, fill)
        fill is non-None only when the order was immediately filled (MARKET).

    Raises
    ------
    DataNotFoundError          If suggestion_id provided but suggestion not found.
    PositionLimitExceededError If max_open_positions would be breached.
    InsufficientFundsError     If cash is insufficient to cover a BUY order.
    SettlementPendingError     If a CNC SELL is attempted on same-day shares.
    InvalidOrderError          For other business-rule violations.
    """
    portfolio = await get_active_portfolio(session, user_id)

    if payload.suggestion_id is not None:
        suggestion = await _fetch_suggestion(session, payload.suggestion_id)
        ctx = _ctx_from_suggestion(suggestion)
    else:
        ctx = _ctx_from_payload(payload)

    if payload.transaction_type.value == "BUY":
        order, fill = await _place_buy_order(session, redis, portfolio, ctx, payload)
    else:
        order, fill = await _place_sell_order(session, redis, portfolio, ctx, payload)

    return order, fill


async def cancel_order(
    session: AsyncSession,
    order_id: UUID,
    user_id: int,
) -> PaperOrder:
    """
    Cancel a PENDING or OPEN paper order.

    Raises
    ------
    OrderNotFoundError  If the order does not exist or belongs to another user.
    ForbiddenError      If the order belongs to a different portfolio/user.
    InvalidOrderError   If the order is already terminal (COMPLETE/REJECTED/CANCELLED).
    """
    # Load order with portfolio to verify ownership
    stmt = (
        select(PaperOrder)
        .join(Portfolio, PaperOrder.portfolio_id == Portfolio.id)
        .where(PaperOrder.id == order_id)
    )
    result = await session.execute(stmt)
    order = result.scalar_one_or_none()

    if order is None:
        raise OrderNotFoundError(f"Order {order_id} not found.")

    # Ownership check via the portfolio's user_id
    portfolio_stmt = select(Portfolio).where(Portfolio.id == order.portfolio_id)
    portfolio = (await session.execute(portfolio_stmt)).scalar_one_or_none()
    if portfolio is None or portfolio.user_id != user_id:
        raise ForbiddenError("You do not have access to this order.")

    if order.is_terminal:
        raise InvalidOrderError(
            f"Order {order_id} is in terminal status '{order.status}' and cannot be cancelled."
        )

    order.status = "CANCELLED"
    order.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await session.refresh(order)

    logger.info("Order cancelled: id=%s user_id=%d", order_id, user_id)
    return order


# ──────────────────────────────────────────────────────────────────────────────
# Internal — BUY path
# ──────────────────────────────────────────────────────────────────────────────

async def _place_buy_order(
    session: AsyncSession,
    redis: Redis,
    portfolio: Portfolio,
    ctx: _TradeContext,
    payload: PlaceOrderRequest,
) -> tuple[PaperOrder, PaperFill | None]:
    """Validate and execute a BUY order."""
    # ── Position limit check ──────────────────────────────────────────────────
    open_count = await _count_open_positions(session, portfolio.id)
    if open_count >= portfolio.max_open_positions:
        raise PositionLimitExceededError(
            f"Max open positions ({portfolio.max_open_positions}) reached. "
            "Close an existing position before opening a new one."
        )

    # ── Funds pre-flight ──────────────────────────────────────────────────────
    ref_price = ctx.entry_price or _ZERO
    if ref_price <= _ZERO:
        ref_price = Decimal(str(payload.price)) if payload.price else _ZERO
    estimated_cost = ref_price * Decimal(payload.quantity)
    if estimated_cost > portfolio.current_cash:
        raise InsufficientFundsError(
            f"Estimated cost ₹{float(estimated_cost):,.2f} exceeds "
            f"available cash ₹{float(portfolio.current_cash):,.2f}."
        )

    order = _build_order(portfolio.id, ctx, payload)
    session.add(order)
    await session.flush()

    fill: PaperFill | None = None
    if payload.order_type.value == "MARKET":
        fill = await _execute_fill(session, redis, portfolio, order, ctx, payload.quantity)
        order.status = "COMPLETE"
    else:
        order.status = "OPEN"

    order.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await session.refresh(order)
    return order, fill


# ──────────────────────────────────────────────────────────────────────────────
# Internal — SELL path
# ──────────────────────────────────────────────────────────────────────────────

async def _place_sell_order(
    session: AsyncSession,
    redis: Redis,
    portfolio: Portfolio,
    ctx: _TradeContext,
    payload: PlaceOrderRequest,
) -> tuple[PaperOrder, PaperFill | None]:
    """
    Validate and execute a SELL order against an existing open position.

    For standalone ClosePositionRequest calls, use position_service.close_position
    directly.  This path handles SELL orders placed from the PlaceOrderRequest
    flow (e.g., a counter-trade on the same symbol).
    """
    # ── Open position check ───────────────────────────────────────────────────
    open_pos = await _find_open_position(session, portfolio.id, ctx.symbol)
    if open_pos is None:
        raise InvalidOrderError(
            f"No open position found for {ctx.symbol}. "
            "Cannot place a SELL order without an open position."
        )
    if payload.quantity > open_pos.quantity:
        raise InvalidOrderError(
            f"SELL quantity {payload.quantity} exceeds open position "
            f"quantity {open_pos.quantity} for {ctx.symbol}."
        )

    # ── T+1 settlement check (CNC only) ───────────────────────────────────────
    if payload.product_type.value == "CNC":
        await _assert_t1_settlement(session, portfolio.id, ctx.symbol)

    order = _build_order(portfolio.id, ctx, payload)
    session.add(order)
    await session.flush()

    fill: PaperFill | None = None
    if payload.order_type.value == "MARKET":
        fill = await _execute_fill(
            session, redis, portfolio, order, ctx, payload.quantity,
            close_position=open_pos,
        )
        order.status = "COMPLETE"
    else:
        order.status = "OPEN"

    order.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await session.refresh(order)
    return order, fill


# ──────────────────────────────────────────────────────────────────────────────
# Internal — Fill execution
# ──────────────────────────────────────────────────────────────────────────────

async def _execute_fill(
    session: AsyncSession,
    redis: Redis,
    portfolio: Portfolio,
    order: PaperOrder,
    ctx: _TradeContext,
    fill_quantity: int,
    close_position: PaperPosition | None = None,
) -> PaperFill:
    """
    Simulate a fill at the current LTP and write all side-effects atomically.

    Side-effects (within the caller's transaction):
      BUY:  PaperFill written, portfolio cash deducted, position opened/updated
      SELL: PaperFill written, portfolio cash credited, position closed/updated
    """
    # ── 1. Resolve fill price ─────────────────────────────────────────────────
    ltp = await _get_ltp(redis, order.instrument_key)
    fallback_used = ltp is None
    if fallback_used:
        # No tick: fall back to known entry price (suggestion or manual), then limit price.
        ltp = ctx.entry_price or _ZERO
        if ltp <= _ZERO:
            ltp = Decimal(str(order.price or "0"))
        logger.warning(
            "No LTP found for %s — using fallback price %.4f",
            order.instrument_key, float(ltp),
        )

    # Apply slippage (adverse fill — buy high, sell low)
    slippage_bps = _DEFAULT_SLIPPAGE_BPS
    slippage_amount = ltp * slippage_bps / Decimal("10000")
    if order.transaction_type == "BUY":
        fill_price = ltp + slippage_amount
    else:
        fill_price = ltp - slippage_amount

    fill_price = fill_price.quantize(Decimal("0.0001"))

    # ── 2. Compute statutory charges ─────────────────────────────────────────
    charges = calculate_charges(
        transaction_type=order.transaction_type,
        product_type=order.product_type,
        fill_price=fill_price,
        fill_quantity=fill_quantity,
    )

    # ── 3. Compute settlement date ────────────────────────────────────────────
    settlement_date = _compute_settlement_date(order.transaction_type, order.product_type)

    # ── 4. Write PaperFill ────────────────────────────────────────────────────
    fill = PaperFill(
        order_id=order.id,
        portfolio_id=portfolio.id,
        symbol=order.symbol,
        fill_quantity=fill_quantity,
        fill_price=fill_price,
        slippage_bps=slippage_bps,
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

    # ── 5. Cash movement ──────────────────────────────────────────────────────
    if order.transaction_type == "BUY":
        if charges.net_amount > portfolio.current_cash:
            raise InsufficientFundsError(
                f"Fill cost ₹{float(charges.net_amount):,.2f} exceeds "
                f"available cash ₹{float(portfolio.current_cash):,.2f}."
            )
        await deduct_cash(session, portfolio, charges.net_amount)
    else:
        await credit_cash(session, portfolio, charges.net_amount)

    # ── 6. Position update (deferred to position_service) ────────────────────
    # Import inline to avoid circular imports at module level.
    from app.services.paper_trading.position_service import (
        apply_buy_fill_to_position,
        apply_sell_fill_to_position,
    )
    if order.transaction_type == "BUY":
        await apply_buy_fill_to_position(
            session=session,
            portfolio=portfolio,
            order=order,
            fill=fill,
            ctx=ctx,
        )
    else:
        if close_position is None:
            close_position = await _find_open_position(
                session, portfolio.id, order.symbol
            )
        if close_position is not None:
            await apply_sell_fill_to_position(
                session=session,
                portfolio=portfolio,
                position=close_position,
                fill=fill,
                exit_reason="MANUAL",
            )

    logger.info(
        "Fill executed: order=%s symbol=%s tx=%s qty=%d price=%.4f "
        "charges=%.4f net=%.4f fallback=%s",
        order.id, order.symbol, order.transaction_type,
        fill_quantity, float(fill_price),
        float(charges.total_charges), float(charges.net_amount),
        fallback_used,
    )
    return fill


# ──────────────────────────────────────────────────────────────────────────────
# Internal — helpers
# ──────────────────────────────────────────────────────────────────────────────

def _build_order(
    portfolio_id: UUID,
    ctx: _TradeContext,
    payload: PlaceOrderRequest,
) -> PaperOrder:
    return PaperOrder(
        portfolio_id=portfolio_id,
        suggestion_id=ctx.suggestion_id,
        symbol=ctx.symbol,
        instrument_key=ctx.instrument_key,
        transaction_type=payload.transaction_type.value,
        product_type=payload.product_type.value,
        order_type=payload.order_type.value,
        validity=payload.validity.value,
        quantity=payload.quantity,
        price=Decimal(str(payload.price)) if payload.price is not None else None,
        trigger_price=Decimal(str(payload.trigger_price)) if payload.trigger_price is not None else None,
        status="PENDING",
    )


async def _fetch_suggestion(session: AsyncSession, suggestion_id: UUID) -> TradeSuggestion:
    from app.exceptions import DataNotFoundError
    stmt = select(TradeSuggestion).where(
        TradeSuggestion.suggestion_id == suggestion_id
    )
    result = await session.execute(stmt)
    suggestion = result.scalar_one_or_none()
    if suggestion is None:
        raise DataNotFoundError(
            f"TradeSuggestion {suggestion_id} not found. "
            "The suggestion may have expired or been removed."
        )
    return suggestion


async def _count_open_positions(session: AsyncSession, portfolio_id: UUID) -> int:
    from sqlalchemy import func
    stmt = (
        select(func.count(PaperPosition.id))
        .where(
            PaperPosition.portfolio_id == portfolio_id,
            PaperPosition.status == "OPEN",
        )
    )
    result = await session.execute(stmt)
    return result.scalar_one() or 0


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
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _assert_t1_settlement(
    session: AsyncSession,
    portfolio_id: UUID,
    symbol: str,
) -> None:
    """
    Raise SettlementPendingError if all shares in the open CNC position were
    purchased today (settlement_date > today means T+1 not yet settled).
    """
    today = date.today()

    # Find the open position for this symbol
    pos_stmt = select(PaperPosition).where(
        and_(
            PaperPosition.portfolio_id == portfolio_id,
            PaperPosition.symbol == symbol,
            PaperPosition.status == "OPEN",
        )
    )
    position = (await session.execute(pos_stmt)).scalar_one_or_none()
    if position is None:
        return  # No position — let the sell attempt fail elsewhere

    # Check if there are fills with settlement_date > today (unsettled)
    from sqlalchemy import func
    unsettled_stmt = (
        select(func.sum(PaperFill.fill_quantity))
        .join(PaperOrder, PaperFill.order_id == PaperOrder.id)
        .where(
            and_(
                PaperFill.portfolio_id == portfolio_id,
                PaperFill.symbol == symbol,
                PaperOrder.transaction_type == "BUY",
                PaperFill.settlement_date > today,
            )
        )
    )
    unsettled_qty = (await session.execute(unsettled_stmt)).scalar_one_or_none() or 0

    if unsettled_qty >= position.quantity:
        raise SettlementPendingError()


async def _get_ltp(redis: Redis, instrument_key: str) -> Decimal | None:
    """Read current LTP from the Redis tick cache. Returns None if absent."""
    try:
        raw = await redis.get(f"{_LTP_KEY_PREFIX}{instrument_key}")
        if raw is None:
            return None
        return Decimal(str(raw))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to read LTP from Redis for %s: %s", instrument_key, exc
        )
        return None


def _compute_settlement_date(transaction_type: str, product_type: str) -> date:
    """
    Return the settlement date for a fill.

    CNC BUY → T+1 (next NSE trading day)
    All others → today (intraday; no delivery restriction)
    """
    if transaction_type == "BUY" and product_type == "CNC":
        today = date.today()
        # Find the next NSE trading day after today
        candidate = today + timedelta(days=1)
        for _ in range(7):  # safety limit — handles weekend + holiday spans
            if nse_calendar.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        # Fallback: today + 1 if calendar lookup exhausted
        return today + timedelta(days=1)
    return date.today()


# ──────────────────────────────────────────────────────────────────────────────
# Fill executor for LIMIT/SL/SL-M orders (called by pnl_worker)
# ──────────────────────────────────────────────────────────────────────────────

async def execute_pending_order_fill(
    session: AsyncSession,
    redis: Redis,
    order: PaperOrder,
    fill_price: Decimal,
) -> PaperFill:
    """
    Execute a fill for a LIMIT/SL/SL-M order whose price condition was met.

    Called by the P&L recompute worker when it detects a price breach.
    The fill_price is supplied by the worker (the tick price at which the
    condition was breached).

    This is a lighter path than place_order because the order record and all
    upstream validations already passed at order placement time.
    """
    portfolio_stmt = select(Portfolio).where(Portfolio.id == order.portfolio_id)
    portfolio = (await session.execute(portfolio_stmt)).scalar_one_or_none()
    if portfolio is None:
        raise PortfolioNotFoundError(
            f"Portfolio {order.portfolio_id} not found while filling order {order.id}."
        )

    # Build a _TradeContext from the order record.  For LIMIT/SL fills the
    # suggestion may have already expired; we only need symbol/instrument_key
    # and fill price here — SL/TP targets were written to PaperPosition at
    # order placement time so they don't need to be re-derived.
    ctx = _TradeContext(
        suggestion_id=order.suggestion_id,
        symbol=order.symbol,
        instrument_key=order.instrument_key,
        entry_price=fill_price,  # the confirmed fill price is the reference
        stop_loss=None,
        take_profit_1=None,
        take_profit_2=None,
        take_profit_3=None,
    )

    # Temporarily override the fill price (bypass LTP lookup)
    charges = calculate_charges(
        transaction_type=order.transaction_type,
        product_type=order.product_type,
        fill_price=fill_price,
        fill_quantity=order.quantity,
    )
    settlement_date = _compute_settlement_date(order.transaction_type, order.product_type)

    fill = PaperFill(
        order_id=order.id,
        portfolio_id=portfolio.id,
        symbol=order.symbol,
        fill_quantity=order.quantity,
        fill_price=fill_price,
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

    if order.transaction_type == "BUY":
        if charges.net_amount > portfolio.current_cash:
            # Reject: insufficient funds at fill time (price moved against us)
            order.status = "REJECTED"
            order.rejection_reason = (
                f"Insufficient cash at fill time: "
                f"need ₹{float(charges.net_amount):,.2f}, "
                f"have ₹{float(portfolio.current_cash):,.2f}."
            )
            await session.flush()
            raise InsufficientFundsError(order.rejection_reason)
        await deduct_cash(session, portfolio, charges.net_amount)
    else:
        await credit_cash(session, portfolio, charges.net_amount)

    from app.services.paper_trading.position_service import (
        apply_buy_fill_to_position,
        apply_sell_fill_to_position,
    )
    if order.transaction_type == "BUY":
        await apply_buy_fill_to_position(
            session=session,
            portfolio=portfolio,
            order=order,
            fill=fill,
            ctx=ctx,
        )
    else:
        close_position = await _find_open_position(
            session, portfolio.id, order.symbol
        )
        if close_position is not None:
            await apply_sell_fill_to_position(
                session=session,
                portfolio=portfolio,
                position=close_position,
                fill=fill,
                exit_reason="MANUAL",
            )

    order.status = "COMPLETE"
    order.updated_at = datetime.now(timezone.utc)
    await session.flush()

    logger.info(
        "Pending order filled: order=%s symbol=%s tx=%s qty=%d price=%.4f",
        order.id, order.symbol, order.transaction_type,
        order.quantity, float(fill_price),
    )
    return fill
