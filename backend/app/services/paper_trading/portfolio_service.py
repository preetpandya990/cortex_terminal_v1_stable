"""
Paper Trading — Portfolio Service
===================================
Handles all portfolio lifecycle operations: creation, retrieval, settings
updates, deactivation, and live summary enrichment.

Design rules:
  - All monetary arithmetic uses Decimal — never float.
  - ORM objects are returned; schema conversion is the router's responsibility.
  - The partial unique index `uq_portfolios_user_active` (WHERE is_active=true)
    is the authoritative uniqueness gate. The service catches IntegrityError
    and maps it to DuplicatePortfolioError so the router never sees raw DB errors.
  - `get_portfolio_summary` executes three SQL aggregates (positions, outcomes,
    positions market-value) instead of loading all rows into Python, keeping
    the summary O(1) in memory regardless of portfolio size.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    DuplicatePortfolioError,
    ForbiddenError,
    PortfolioNotFoundError,
)
from app.models.paper_trading import PaperPosition, PaperTradeOutcome, Portfolio
from app.schemas.paper_trading import (
    CreatePortfolioRequest,
    PortfolioSummaryResponse,
    UpdatePortfolioSettingsRequest,
)

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")


# ──────────────────────────────────────────────────────────────────────────────
# Create
# ──────────────────────────────────────────────────────────────────────────────

async def create_portfolio(
    session: AsyncSession,
    user_id: int,
    payload: CreatePortfolioRequest,
) -> Portfolio:
    """
    Create a new PAPER portfolio for the user.

    Raises
    ------
    DuplicatePortfolioError
        If the user already has an active portfolio (partial unique index
        uq_portfolios_user_active is violated).
    """
    capital = Decimal(str(payload.initial_capital))
    portfolio = Portfolio(
        user_id=user_id,
        portfolio_type="PAPER",
        name=payload.name,
        initial_capital=capital,
        current_cash=capital,
        risk_per_trade_pct=Decimal(str(payload.risk_per_trade_pct)),
        max_open_positions=payload.max_open_positions,
        is_active=True,
    )
    session.add(portfolio)

    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        # The partial unique index fires when a second active portfolio is created.
        # Any other IntegrityError (FK violation, CHECK, etc.) should still propagate.
        constraint = "uq_portfolios_user_active"
        if constraint in str(exc.orig):
            raise DuplicatePortfolioError() from exc
        raise

    await session.refresh(portfolio)
    logger.info(
        "Portfolio created: id=%s user_id=%d capital=%s",
        portfolio.id, user_id, capital,
    )
    return portfolio


# ──────────────────────────────────────────────────────────────────────────────
# Read — single
# ──────────────────────────────────────────────────────────────────────────────

async def get_portfolio(
    session: AsyncSession,
    portfolio_id: UUID,
    user_id: int,
) -> Portfolio:
    """
    Fetch a portfolio by ID and assert ownership.

    Raises
    ------
    PortfolioNotFoundError
        If the portfolio does not exist.
    ForbiddenError
        If the portfolio belongs to a different user.
    """
    stmt = select(Portfolio).where(Portfolio.id == portfolio_id)
    result = await session.execute(stmt)
    portfolio = result.scalar_one_or_none()

    if portfolio is None:
        raise PortfolioNotFoundError(
            f"Portfolio {portfolio_id} not found."
        )
    if portfolio.user_id != user_id:
        raise ForbiddenError(
            "You do not have access to this portfolio."
        )
    return portfolio


async def get_active_portfolio(
    session: AsyncSession,
    user_id: int,
) -> Portfolio:
    """
    Return the user's current active portfolio.

    Raises
    ------
    PortfolioNotFoundError
        If the user has no active portfolio.
    """
    stmt = (
        select(Portfolio)
        .where(Portfolio.user_id == user_id, Portfolio.is_active.is_(True))
    )
    result = await session.execute(stmt)
    portfolio = result.scalar_one_or_none()

    if portfolio is None:
        raise PortfolioNotFoundError(
            "No active portfolio found. Create one to start paper trading."
        )
    return portfolio


# ──────────────────────────────────────────────────────────────────────────────
# Read — enriched summary
# ──────────────────────────────────────────────────────────────────────────────

async def get_portfolio_summary(
    session: AsyncSession,
    user_id: int,
) -> PortfolioSummaryResponse:
    """
    Enriched portfolio summary for GET /portfolios/me.

    Executes three SQL aggregate queries to compute live stats without
    loading position or outcome rows into Python memory:

      1. Open-position aggregate: count, sum(unrealized_pnl),
         sum(quantity * last_price) for portfolio value
      2. Outcome aggregate: count, sum(net_pnl), count(winners)
      3. Derives portfolio_value, total_return_pct, win_rate

    Falls back gracefully when last_price is NULL (position not yet ticked).
    """
    portfolio = await get_active_portfolio(session, user_id)
    portfolio_id = portfolio.id

    # ── Open-position aggregate ────────────────────────────────────────────────
    pos_stmt = (
        select(
            func.count(PaperPosition.id).label("open_count"),
            func.coalesce(
                func.sum(PaperPosition.unrealized_pnl), _ZERO
            ).label("total_unrealized_pnl"),
            # Market value: quantity × last_price (falls back to cost if no tick)
            func.coalesce(
                func.sum(
                    PaperPosition.quantity
                    * func.coalesce(PaperPosition.last_price, PaperPosition.avg_cost_price)
                ),
                _ZERO,
            ).label("open_market_value"),
        )
        .where(
            PaperPosition.portfolio_id == portfolio_id,
            PaperPosition.status == "OPEN",
        )
    )
    pos_row = (await session.execute(pos_stmt)).one()

    # ── Outcome aggregate ──────────────────────────────────────────────────────
    out_stmt = (
        select(
            func.count(PaperTradeOutcome.id).label("total_trades"),
            func.coalesce(func.sum(PaperTradeOutcome.net_pnl), _ZERO).label("total_net_pnl"),
            func.count(PaperTradeOutcome.id)
            .filter(PaperTradeOutcome.net_pnl > _ZERO)
            .label("total_winners"),
        )
        .where(PaperTradeOutcome.portfolio_id == portfolio_id)
    )
    out_row = (await session.execute(out_stmt)).one()

    # ── Derived metrics ────────────────────────────────────────────────────────
    open_count: int = pos_row.open_count or 0
    total_unrealized_pnl = Decimal(str(pos_row.total_unrealized_pnl or _ZERO))
    open_market_value = Decimal(str(pos_row.open_market_value or _ZERO))

    total_trades: int = out_row.total_trades or 0
    total_net_pnl = Decimal(str(out_row.total_net_pnl or _ZERO))
    total_winners: int = out_row.total_winners or 0

    portfolio_value = portfolio.current_cash + open_market_value

    total_return_pct = (
        (portfolio_value - portfolio.initial_capital)
        / portfolio.initial_capital
        * Decimal("100")
        if portfolio.initial_capital > _ZERO
        else _ZERO
    )

    win_rate: float | None = None
    if total_trades > 0:
        win_rate = round(float(total_winners / total_trades * 100), 2)

    return PortfolioSummaryResponse(
        # Core portfolio fields
        id=portfolio.id,
        user_id=portfolio.user_id,
        portfolio_type=portfolio.portfolio_type,
        name=portfolio.name,
        initial_capital=portfolio.initial_capital,
        current_cash=portfolio.current_cash,
        currency=portfolio.currency,
        risk_per_trade_pct=portfolio.risk_per_trade_pct,
        max_open_positions=portfolio.max_open_positions,
        is_active=portfolio.is_active,
        created_at=portfolio.created_at,
        updated_at=portfolio.updated_at,
        # Live aggregate stats
        open_position_count=open_count,
        total_unrealized_pnl=float(total_unrealized_pnl),
        total_realized_pnl=float(total_net_pnl),
        portfolio_value=float(portfolio_value),
        total_return_pct=float(total_return_pct),
        total_trades=total_trades,
        win_rate=win_rate,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Update
# ──────────────────────────────────────────────────────────────────────────────

async def update_portfolio_settings(
    session: AsyncSession,
    portfolio_id: UUID,
    user_id: int,
    payload: UpdatePortfolioSettingsRequest,
) -> Portfolio:
    """
    Update mutable portfolio risk settings.

    Only `risk_per_trade_pct` and `max_open_positions` are mutable after
    creation.  `initial_capital` and `name` are intentionally excluded.

    Raises
    ------
    PortfolioNotFoundError / ForbiddenError
        Via get_portfolio.
    """
    portfolio = await get_portfolio(session, portfolio_id, user_id)

    if payload.risk_per_trade_pct is not None:
        portfolio.risk_per_trade_pct = Decimal(str(payload.risk_per_trade_pct))
    if payload.max_open_positions is not None:
        portfolio.max_open_positions = payload.max_open_positions

    portfolio.updated_at = datetime.now(timezone.utc)

    await session.flush()
    await session.refresh(portfolio)
    logger.info(
        "Portfolio settings updated: id=%s user_id=%d risk_pct=%s max_pos=%d",
        portfolio.id, user_id,
        portfolio.risk_per_trade_pct, portfolio.max_open_positions,
    )
    return portfolio


# ──────────────────────────────────────────────────────────────────────────────
# Deactivate (soft-delete)
# ──────────────────────────────────────────────────────────────────────────────

async def deactivate_portfolio(
    session: AsyncSession,
    portfolio_id: UUID,
    user_id: int,
) -> Portfolio:
    """
    Soft-delete a portfolio by setting is_active = False.

    Open positions are NOT force-closed — the caller must close them first
    if desired.  The deactivation only unblocks the unique constraint so the
    user can create a new active portfolio.

    Raises
    ------
    PortfolioNotFoundError / ForbiddenError
        Via get_portfolio.
    """
    portfolio = await get_portfolio(session, portfolio_id, user_id)

    if not portfolio.is_active:
        # Idempotent — already inactive, nothing to do.
        return portfolio

    portfolio.is_active = False
    portfolio.updated_at = datetime.now(timezone.utc)

    await session.flush()
    await session.refresh(portfolio)
    logger.info(
        "Portfolio deactivated: id=%s user_id=%d",
        portfolio.id, user_id,
    )
    return portfolio


# ──────────────────────────────────────────────────────────────────────────────
# Cash management helpers  (used internally by order_service)
# ──────────────────────────────────────────────────────────────────────────────

async def deduct_cash(
    session: AsyncSession,
    portfolio: Portfolio,
    amount: Decimal,
) -> None:
    """
    Reduce portfolio cash by `amount` (BUY fill net_amount deduction).

    The caller is responsible for verifying sufficient funds before calling
    this.  This function does NOT re-check — it trusts the service layer's
    pre-flight validation to avoid a double-read.

    The DB CHECK constraint `ck_portfolios_cash_non_negative` provides a
    final safety net at the database level.
    """
    portfolio.current_cash -= amount
    portfolio.updated_at = datetime.now(timezone.utc)
    await session.flush()


async def credit_cash(
    session: AsyncSession,
    portfolio: Portfolio,
    amount: Decimal,
) -> None:
    """
    Increase portfolio cash by `amount` (SELL fill net_amount credit).
    """
    portfolio.current_cash += amount
    portfolio.updated_at = datetime.now(timezone.utc)
    await session.flush()
