# PATH: backend/app/services/strategy_service.py
"""
Trading Strategies — CRUD & Subscription Service
==================================================
All database operations for Strategy, UserStrategySubscription,
StrategyActivation, StrategyTrade, and StrategyBacktestRun.

Public API:
  Strategy CRUD
    create_strategy(user_id, req, db)
    get_strategy(strategy_id, requesting_user_id, db)
    list_strategies(scope, status, page, page_size, db)
    list_user_strategies(user_id, page, page_size, db)
    update_strategy(strategy_id, req, user_id, db)
    delete_strategy(strategy_id, user_id, db)

  Subscriptions
    subscribe(user_id, strategy_id, req, db)
    unsubscribe(user_id, strategy_id, db)
    reorder_subscriptions(user_id, req, db)
    get_active_subscriptions(user_id, db)

  Activations
    list_activations(user_id, symbol, state, page, page_size, db)
    get_activation(activation_id, user_id, db)
    cancel_activation(activation_id, user_id, db)

  Trades
    list_trades(strategy_id, user_id, page, page_size, cursor, db)
    get_performance(strategy_id, db)

  Backtests
    trigger_backtest(user_id, strategy_id, req, db)
    list_backtests(strategy_id, user_id, db)
    get_backtest(run_id, user_id, db)
    cancel_backtest(run_id, user_id, db)
"""
from __future__ import annotations

import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.exceptions import (
    BacktestRunNotFoundError,
    ForbiddenError,
    InvalidStrategyStateError,
    StrategyActivationNotFoundError,
    StrategyConflictError,
    StrategyForbiddenError,
    StrategyNotFoundError,
)
from app.models.strategies import (
    Strategy,
    StrategyActivation,
    StrategyAuditLog,
    StrategyBacktestRun,
    StrategyTrade,
    UserStrategySubscription,
)
from app.schemas.strategies import (
    AdminStrategyItemResponse,
    AdminStrategiesListResponse,
    CreateStrategyRequest,
    PromoteStrategyRequest,
    ReorderSubscriptionsRequest,
    StrategyAuditLogListResponse,
    StrategyAuditLogResponse,
    StrategyPerformanceResponse,
    SubscribeStrategyRequest,
    TriggerBacktestRequest,
    UpdateStrategyRequest,
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Strategy CRUD
# ──────────────────────────────────────────────────────────────────────────────

async def create_strategy(
    user_id: int,
    req: CreateStrategyRequest,
    db: AsyncSession,
) -> Strategy:
    strategy = Strategy(
        created_by=user_id,
        name=req.name,
        description=req.description,
        tags=req.tags or [],
        scope="private",
        status="draft",
        version=1,
        definition=req.definition.model_dump(mode="json"),
    )
    db.add(strategy)
    await db.flush()
    await db.refresh(strategy)
    logger.info("Created strategy %s for user %d", strategy.id, user_id)
    return strategy


async def get_strategy(
    strategy_id: UUID,
    requesting_user_id: int,
    db: AsyncSession,
    *,
    require_owner: bool = False,
) -> Strategy:
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id)
    )
    strategy = result.scalar_one_or_none()
    if strategy is None:
        raise StrategyNotFoundError()
    # Private strategies are only visible to their creator
    if strategy.scope == "private" and strategy.created_by != requesting_user_id:
        raise StrategyNotFoundError()
    if require_owner and strategy.created_by != requesting_user_id:
        raise StrategyForbiddenError()
    return strategy


async def list_strategies(
    db: AsyncSession,
    *,
    scope: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Strategy], int]:
    """List publicly-scoped strategies (the global library)."""
    q = select(Strategy).where(Strategy.scope == "shared")
    if status:
        q = q.where(Strategy.status == status)
    if tags:
        for tag in tags:
            q = q.where(Strategy.tags.contains([tag]))
    count_q = select(func.count()).select_from(q.subquery())
    total_result = await db.execute(count_q)
    total = total_result.scalar_one()
    q = q.order_by(Strategy.total_subscribers.desc(), Strategy.created_at.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all(), total


async def list_user_strategies(
    user_id: int,
    db: AsyncSession,
    *,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Strategy], int]:
    q = select(Strategy).where(Strategy.created_by == user_id)
    if status:
        q = q.where(Strategy.status == status)
    count_q = select(func.count()).select_from(q.subquery())
    total_result = await db.execute(count_q)
    total = total_result.scalar_one()
    q = q.order_by(Strategy.updated_at.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all(), total


async def update_strategy(
    strategy_id: UUID,
    req: UpdateStrategyRequest,
    user_id: int,
    db: AsyncSession,
) -> Strategy:
    strategy = await get_strategy(strategy_id, user_id, db, require_owner=True)

    if req.name is not None:
        strategy.name = req.name
    if req.description is not None:
        strategy.description = req.description
    if req.tags is not None:
        strategy.tags = req.tags
    if req.status is not None:
        strategy.status = req.status.value
    if req.definition is not None:
        strategy.definition = req.definition.model_dump(mode="json")
        strategy.version += 1

    await db.flush()
    await db.refresh(strategy)
    logger.info("Updated strategy %s (v%d) by user %d", strategy.id, strategy.version, user_id)
    return strategy


async def delete_strategy(
    strategy_id: UUID,
    user_id: int,
    db: AsyncSession,
) -> None:
    strategy = await get_strategy(strategy_id, user_id, db, require_owner=True)
    strategy.status = "archived"
    await db.flush()
    logger.info("Archived strategy %s by user %d", strategy_id, user_id)


# ──────────────────────────────────────────────────────────────────────────────
# Subscriptions
# ──────────────────────────────────────────────────────────────────────────────

async def subscribe(
    user_id: int,
    strategy_id: UUID,
    req: SubscribeStrategyRequest,
    db: AsyncSession,
) -> UserStrategySubscription:
    # Ensure strategy exists and is accessible
    await get_strategy(strategy_id, user_id, db)

    # Check for duplicate active subscription
    dup_result = await db.execute(
        select(UserStrategySubscription).where(
            and_(
                UserStrategySubscription.user_id == user_id,
                UserStrategySubscription.strategy_id == strategy_id,
                UserStrategySubscription.is_active.is_(True),
            )
        )
    )
    if dup_result.scalar_one_or_none():
        raise StrategyConflictError(
            "You are already subscribed to this strategy. Unsubscribe first to change priority."
        )

    # Check for priority collision
    priority_result = await db.execute(
        select(UserStrategySubscription).where(
            and_(
                UserStrategySubscription.user_id == user_id,
                UserStrategySubscription.priority == req.priority,
                UserStrategySubscription.is_active.is_(True),
            )
        )
    )
    if priority_result.scalar_one_or_none():
        raise StrategyConflictError(
            f"Priority {req.priority} is already in use by another active subscription. "
            "Choose a different priority or reorder your subscriptions first."
        )

    sub = UserStrategySubscription(
        user_id=user_id,
        strategy_id=strategy_id,
        priority=req.priority,
        is_active=True,
        notes=req.notes,
    )
    db.add(sub)
    await db.flush()
    await db.refresh(sub)

    # Increment subscriber count on the strategy
    await db.execute(
        update(Strategy)
        .where(Strategy.id == strategy_id)
        .values(total_subscribers=Strategy.total_subscribers + 1)
    )
    await db.flush()

    logger.info("User %d subscribed to strategy %s at priority %d", user_id, strategy_id, req.priority)
    return sub


async def unsubscribe(
    user_id: int,
    strategy_id: UUID,
    db: AsyncSession,
) -> None:
    result = await db.execute(
        select(UserStrategySubscription).where(
            and_(
                UserStrategySubscription.user_id == user_id,
                UserStrategySubscription.strategy_id == strategy_id,
                UserStrategySubscription.is_active.is_(True),
            )
        )
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise StrategyConflictError("No active subscription to this strategy found.")

    from datetime import datetime, timezone
    sub.is_active = False
    sub.unsubscribed_at = datetime.now(timezone.utc)
    await db.flush()

    await db.execute(
        update(Strategy)
        .where(Strategy.id == strategy_id)
        .values(
            total_subscribers=func.greatest(Strategy.total_subscribers - 1, 0)
        )
    )
    await db.flush()
    logger.info("User %d unsubscribed from strategy %s", user_id, strategy_id)


async def set_subscription_active(
    user_id: int,
    strategy_id: UUID,
    is_active: bool,
    db: AsyncSession,
) -> UserStrategySubscription:
    """
    Pause (is_active=False) or resume (is_active=True) a subscription.
    Unlike unsubscribe(), this never changes total_subscribers or sets unsubscribed_at —
    the user remains subscribed; signals are simply skipped while paused.
    """
    result = await db.execute(
        select(UserStrategySubscription).where(
            and_(
                UserStrategySubscription.user_id == user_id,
                UserStrategySubscription.strategy_id == strategy_id,
                UserStrategySubscription.unsubscribed_at.is_(None),
            )
        )
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        raise StrategyConflictError("No subscription to this strategy found.")

    sub.is_active = is_active
    await db.flush()
    await db.refresh(sub)
    action = "resumed" if is_active else "paused"
    logger.info("User %d %s strategy subscription %s", user_id, action, strategy_id)
    return sub


async def reorder_subscriptions(
    user_id: int,
    req: ReorderSubscriptionsRequest,
    db: AsyncSession,
) -> list[UserStrategySubscription]:
    """
    Atomically reassign priorities 1…N in the order given by req.subscription_ids.
    All listed IDs must belong to the user and be currently active.
    """
    result = await db.execute(
        select(UserStrategySubscription).where(
            and_(
                UserStrategySubscription.user_id == user_id,
                UserStrategySubscription.is_active.is_(True),
            )
        )
    )
    active_subs = {sub.id: sub for sub in result.scalars().all()}

    if set(req.subscription_ids) != set(active_subs.keys()):
        raise StrategyConflictError(
            "subscription_ids must contain every active subscription ID exactly once."
        )

    for priority, sub_id in enumerate(req.subscription_ids, start=1):
        active_subs[sub_id].priority = priority

    await db.flush()
    return sorted(active_subs.values(), key=lambda s: s.priority)


async def get_active_subscriptions(
    user_id: int,
    db: AsyncSession,
) -> list[UserStrategySubscription]:
    result = await db.execute(
        select(UserStrategySubscription)
        .options(selectinload(UserStrategySubscription.strategy))
        .where(
            and_(
                UserStrategySubscription.user_id == user_id,
                UserStrategySubscription.is_active.is_(True),
            )
        )
        .order_by(UserStrategySubscription.priority.asc())
    )
    return result.scalars().all()


# ──────────────────────────────────────────────────────────────────────────────
# Activations
# ──────────────────────────────────────────────────────────────────────────────

async def list_activations(
    user_id: int,
    db: AsyncSession,
    *,
    symbol: str | None = None,
    state: str | None = None,
    strategy_id: UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[StrategyActivation], int]:
    q = select(StrategyActivation).where(StrategyActivation.user_id == user_id)
    if symbol:
        q = q.where(StrategyActivation.symbol == symbol.upper())
    if state:
        q = q.where(StrategyActivation.state == state)
    if strategy_id:
        q = q.where(StrategyActivation.strategy_id == strategy_id)
    count_q = select(func.count()).select_from(q.subquery())
    total_result = await db.execute(count_q)
    total = total_result.scalar_one()
    q = q.order_by(StrategyActivation.created_at.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all(), total


async def get_activation(
    activation_id: UUID,
    user_id: int,
    db: AsyncSession,
) -> StrategyActivation:
    result = await db.execute(
        select(StrategyActivation).where(StrategyActivation.id == activation_id)
    )
    activation = result.scalar_one_or_none()
    if activation is None:
        raise StrategyActivationNotFoundError()
    if activation.user_id != user_id:
        raise StrategyActivationNotFoundError()
    return activation


async def cancel_activation(
    activation_id: UUID,
    user_id: int,
    db: AsyncSession,
) -> StrategyActivation:
    from datetime import datetime, timezone
    from app.exceptions import InvalidStrategyStateError

    activation = await get_activation(activation_id, user_id, db)
    if activation.is_terminal:
        raise InvalidStrategyStateError(
            f"Activation is already in terminal state '{activation.state}' and cannot be cancelled."
        )

    prev_state = activation.state
    activation.state = "CANCELLED"
    activation.exit_reason = "MANUAL"
    activation.exit_at = datetime.now(timezone.utc)

    history = list(activation.state_history or [])
    history.append({"state": "CANCELLED", "at": activation.exit_at.isoformat(), "from": prev_state})
    activation.state_history = history

    await db.flush()
    await db.refresh(activation)
    logger.info("Activation %s cancelled by user %d", activation_id, user_id)
    return activation


async def exit_activation(
    activation_id: UUID,
    user_id: int,
    exit_price: float | None,
    db: AsyncSession,
) -> StrategyActivation:
    """
    Manually exit an IN_TRADE or PARTIAL_EXIT activation.

    Writes a StrategyTrade record and transitions the activation to EXITED.
    When no exit_price is provided the most recent LTP is used (best-effort).
    """
    from app.services.strategy_engine.activation_fsm import enter_exited
    from app.schemas.strategies import ActivationExitReason

    activation = await get_activation(activation_id, user_id, db)

    if activation.state not in ("IN_TRADE", "PARTIAL_EXIT"):
        raise InvalidStrategyStateError(
            f"Activation is in state '{activation.state}'; "
            "only IN_TRADE or PARTIAL_EXIT activations can be exited."
        )

    # Resolve exit price: provided > cached LTP > entry price fallback
    ep: float = exit_price or 0.0
    if ep == 0.0:
        from app.core.redis import get_redis
        try:
            redis = get_redis()
            key = f"cai:ltp:{activation.instrument_key or activation.symbol}"
            raw = await redis.get(key)
            if raw:
                ep = float(raw)
        except Exception:
            pass

    if ep == 0.0:
        # Last resort: use entry price from state_history
        for entry in reversed(activation.state_history):
            if entry.get("state") == "IN_TRADE":
                ep = float(entry.get("entry_price", 0.0))
                break

    if ep == 0.0:
        raise InvalidStrategyStateError("Cannot determine exit price. Provide exit_price explicitly.")

    activation, _ = await enter_exited(
        activation=activation,
        exit_price=ep,
        exit_reason=ActivationExitReason.MANUAL,
        db=db,
    )
    await db.flush()
    await db.refresh(activation)
    logger.info("Activation %s manually exited by user %d at %.2f", activation_id, user_id, ep)
    return activation


# ──────────────────────────────────────────────────────────────────────────────
# Trades
# ──────────────────────────────────────────────────────────────────────────────

async def list_trades(
    strategy_id: UUID,
    requesting_user_id: int,
    db: AsyncSession,
    *,
    user_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[StrategyTrade], int]:
    # Verify strategy is accessible
    await get_strategy(strategy_id, requesting_user_id, db)
    q = select(StrategyTrade).where(StrategyTrade.strategy_id == strategy_id)
    if user_id:
        q = q.where(StrategyTrade.user_id == user_id)
    count_q = select(func.count()).select_from(q.subquery())
    total_result = await db.execute(count_q)
    total = total_result.scalar_one()
    q = q.order_by(StrategyTrade.entry_at.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all(), total


async def get_performance(
    strategy_id: UUID,
    requesting_user_id: int,
    db: AsyncSession,
) -> StrategyPerformanceResponse:
    """
    Compute full performance metrics for a strategy from its trade history.

    Metrics:
      - Win rate, total P&L, avg hold duration, best/worst trade
      - Sharpe ratio  = mean(ret) / std(ret) * √252
      - Sortino ratio = mean(ret) / downside_dev(ret) * √252
        (downside dev uses squared deviations below zero, full-sample denominator)
      - Max drawdown  = largest peak-to-trough drop on the compounded equity curve
      - Calmar ratio  = CAGR / |max_drawdown_pct|

    All ratio computations require at least 2 trades; fewer returns None.
    """
    import math
    import numpy as np

    # Verify strategy is accessible
    strategy = await get_strategy(strategy_id, requesting_user_id, db)

    # Fetch all trades ordered by entry (chronological) for equity-curve math
    result = await db.execute(
        select(
            StrategyTrade.net_pnl,
            StrategyTrade.pnl_pct,
            StrategyTrade.hold_duration_seconds,
            StrategyTrade.entry_at,
            StrategyTrade.exit_at,
        )
        .where(StrategyTrade.strategy_id == strategy_id)
        .order_by(StrategyTrade.entry_at.asc())
    )
    rows = result.all()

    if not rows:
        return StrategyPerformanceResponse(
            strategy_id=strategy_id,
            total_trades=0,
            win_rate_pct=None,
            avg_daily_return_pct=None,
            sharpe_ratio=None,
            sortino_ratio=None,
            calmar_ratio=None,
            max_drawdown_pct=None,
            total_net_pnl=0.0,
            avg_hold_duration_seconds=None,
            best_trade_pnl=None,
            worst_trade_pnl=None,
        )

    net_pnls = np.array([float(r.net_pnl) for r in rows], dtype=np.float64)
    pnl_pcts = np.array([float(r.pnl_pct) for r in rows], dtype=np.float64)
    hold_secs = np.array([r.hold_duration_seconds for r in rows], dtype=np.float64)

    total_trades = len(rows)
    total_net_pnl = float(np.sum(net_pnls))
    win_rate = float(np.sum(net_pnls > 0) / total_trades * 100)
    avg_hold = float(np.mean(hold_secs))
    best_trade = float(np.max(net_pnls))
    worst_trade = float(np.min(net_pnls))

    # ── Compounded equity curve from per-trade % returns ─────────────────────
    # equity[i] = ∏(1 + pnl_pcts[0..i] / 100)  — starts at 1 (normalised NAV)
    returns_frac = pnl_pcts / 100.0
    equity = np.cumprod(1.0 + returns_frac)

    # Max drawdown from compounded equity curve
    peak = np.maximum.accumulate(equity)
    # Avoid division by zero when equity starts negative (rare, but guard)
    with np.errstate(invalid="ignore", divide="ignore"):
        dd = np.where(peak > 0, (peak - equity) / peak * 100.0, 0.0)
    max_dd = float(np.max(dd)) if len(dd) > 0 else None

    # ── Risk-adjusted ratios (require ≥ 2 trades) ─────────────────────────────
    sharpe = sortino = calmar = avg_daily_return_pct = None

    if total_trades >= 2:
        mean_ret = float(np.mean(pnl_pcts))
        std_ret = float(np.std(pnl_pcts, ddof=1))  # sample std
        avg_daily_return_pct = round(mean_ret, 4)

        # Sharpe — annualised assuming each trade ≈ 1 trading-day slot
        if std_ret > 1e-9:
            sharpe = round(float(mean_ret / std_ret * math.sqrt(252)), 4)

        # Sortino — downside deviation (semi-deviation below 0), full-sample N
        downside_sq = (np.minimum(pnl_pcts, 0.0)) ** 2
        downside_dev = float(np.sqrt(np.mean(downside_sq)))
        if downside_dev > 1e-9:
            sortino = round(float(mean_ret / downside_dev * math.sqrt(252)), 4)

        # Calmar — CAGR / |max_drawdown|
        first_entry = rows[0].entry_at
        last_exit = rows[-1].exit_at or rows[-1].entry_at
        if first_entry and last_exit and max_dd and max_dd > 1e-6:
            years = max(
                (last_exit - first_entry).total_seconds() / (365.25 * 86400),
                1.0 / 252,
            )
            # Geometric CAGR from compounded equity (NAV)
            cagr_pct = (equity[-1] ** (1.0 / years) - 1.0) * 100.0
            if cagr_pct > 0:
                calmar = round(float(cagr_pct / max_dd), 4)

    # ── Backfill denormalised columns on Strategy for fast list views ─────────
    if avg_daily_return_pct is not None and strategy.avg_daily_return_pct is None:
        strategy.avg_daily_return_pct = Decimal(str(avg_daily_return_pct))
    if strategy.win_rate_pct is None and total_trades > 0:
        strategy.win_rate_pct = Decimal(str(round(win_rate, 2)))
    if strategy.total_trades != total_trades:
        strategy.total_trades = total_trades
    await db.flush()

    return StrategyPerformanceResponse(
        strategy_id=strategy_id,
        total_trades=total_trades,
        win_rate_pct=round(win_rate, 2),
        avg_daily_return_pct=avg_daily_return_pct,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        max_drawdown_pct=round(max_dd, 2) if max_dd is not None else None,
        total_net_pnl=round(total_net_pnl, 2),
        avg_hold_duration_seconds=round(avg_hold, 0),
        best_trade_pnl=round(best_trade, 2),
        worst_trade_pnl=round(worst_trade, 2),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Backtests
# ──────────────────────────────────────────────────────────────────────────────

async def trigger_backtest(
    user_id: int,
    strategy_id: UUID,
    req: TriggerBacktestRequest,
    db: AsyncSession,
) -> StrategyBacktestRun:
    strategy = await get_strategy(strategy_id, user_id, db)

    run = StrategyBacktestRun(
        user_id=user_id,
        strategy_id=strategy_id,
        strategy_snapshot=strategy.definition,
        mode=req.mode.value,
        symbols=req.symbols,
        start_dt=req.start_dt,
        end_dt=req.end_dt,
        status="pending",
        progress_pct=0,
    )
    db.add(run)
    await db.flush()
    await db.refresh(run)
    logger.info(
        "Queued backtest run %s for strategy %s (mode=%s, symbols=%s)",
        run.id, strategy_id, req.mode.value, req.symbols,
    )
    return run


async def list_backtests(
    strategy_id: UUID,
    user_id: int,
    db: AsyncSession,
) -> list[StrategyBacktestRun]:
    await get_strategy(strategy_id, user_id, db)
    result = await db.execute(
        select(StrategyBacktestRun)
        .where(
            and_(
                StrategyBacktestRun.strategy_id == strategy_id,
                StrategyBacktestRun.user_id == user_id,
            )
        )
        .order_by(StrategyBacktestRun.created_at.desc())
    )
    return result.scalars().all()


async def get_backtest(
    run_id: UUID,
    user_id: int,
    db: AsyncSession,
) -> StrategyBacktestRun:
    result = await db.execute(
        select(StrategyBacktestRun).where(StrategyBacktestRun.id == run_id)
    )
    run = result.scalar_one_or_none()
    if run is None or run.user_id != user_id:
        raise BacktestRunNotFoundError()
    return run


async def cancel_backtest(
    run_id: UUID,
    user_id: int,
    db: AsyncSession,
) -> StrategyBacktestRun:
    run = await get_backtest(run_id, user_id, db)
    if run.is_terminal:
        from app.exceptions import InvalidStrategyStateError
        raise InvalidStrategyStateError(
            f"Backtest run is already in terminal state '{run.status}'."
        )
    run.status = "cancelled"
    await db.flush()
    await db.refresh(run)
    logger.info("Backtest run %s cancelled by user %d", run_id, user_id)
    return run


# ──────────────────────────────────────────────────────────────────────────────
# Admin — Strategy Governance
# ──────────────────────────────────────────────────────────────────────────────

async def admin_create_library_strategy(
    admin_user_id: int,
    req: CreateStrategyRequest,
    db: AsyncSession,
) -> Strategy:
    """
    Create a new strategy directly in the global library (scope=shared, status=active).

    Atomically creates the strategy and appends a 'promote' audit log entry so every
    governance change — including initial direct publication — is traceable.  This
    bypasses the private→promote workflow for cases where an admin is authoring a
    library strategy from scratch rather than curating a user submission.
    """
    strategy = Strategy(
        created_by=admin_user_id,
        name=req.name,
        description=req.description,
        tags=req.tags or [],
        scope="shared",
        status="active",
        version=1,
        definition=req.definition.model_dump(mode="json"),
    )
    db.add(strategy)
    await db.flush()  # materialise the UUID before the audit log FK

    audit = StrategyAuditLog(
        strategy_id=strategy.id,
        admin_user_id=admin_user_id,
        action="promote",
        reason="Created directly as a global library strategy by admin.",
        scope_before="private",
        scope_after="shared",
        strategy_name=strategy.name,
    )
    db.add(audit)
    await db.flush()
    await db.refresh(strategy)

    logger.info(
        "Admin %d created library strategy %s (%r) directly as shared/active",
        admin_user_id, strategy.id, strategy.name,
    )
    return strategy


async def admin_list_strategies(
    db: AsyncSession,
    *,
    scope: str | None = None,
    status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> tuple[list[AdminStrategyItemResponse], int]:
    """
    List all strategies across all users for the admin governance panel.

    Joins to the users table to resolve creator_username without N+1 queries.
    Supports optional scope/status filtering and name/description search.
    """
    from app.models.user import User

    # Build base query with creator join
    q = (
        select(Strategy, User.username.label("creator_username"))
        .outerjoin(User, User.id == Strategy.created_by)
    )

    if scope:
        q = q.where(Strategy.scope == scope)
    if status:
        q = q.where(Strategy.status == status)
    if search:
        term = f"%{search.strip()}%"
        q = q.where(
            or_(Strategy.name.ilike(term), Strategy.description.ilike(term))
        )

    # Count total before pagination
    count_q = select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one()

    q = (
        q.order_by(Strategy.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(q)).all()

    items: list[AdminStrategyItemResponse] = []
    for strategy, creator_username in rows:
        item = AdminStrategyItemResponse.model_validate(strategy)
        item.creator_username = creator_username
        items.append(item)

    return items, total


async def promote_strategy(
    strategy_id: UUID,
    admin_user_id: int,
    req: PromoteStrategyRequest,
    db: AsyncSession,
) -> Strategy:
    """
    Promote a strategy to the global library (scope: private → shared).

    Writes an audit log entry atomically.  Idempotent — promoting an already-
    shared strategy re-records the audit but does not raise an error.
    """
    strategy = await db.get(Strategy, strategy_id)
    if strategy is None:
        raise StrategyNotFoundError(f"Strategy {strategy_id} not found")

    if strategy.status == "archived":
        from app.exceptions import InvalidStrategyStateError
        raise InvalidStrategyStateError("Archived strategies cannot be promoted to the library.")

    scope_before = strategy.scope
    strategy.scope = "shared"

    audit = StrategyAuditLog(
        strategy_id=strategy_id,
        admin_user_id=admin_user_id,
        action="promote",
        reason=req.reason.strip(),
        scope_before=scope_before,
        scope_after="shared",
        strategy_name=strategy.name,
    )
    db.add(audit)
    await db.flush()
    await db.refresh(strategy)

    logger.info(
        "Strategy %s promoted to shared library by admin %d (was: %s)",
        strategy_id, admin_user_id, scope_before,
    )
    return strategy


async def demote_strategy(
    strategy_id: UUID,
    admin_user_id: int,
    req: PromoteStrategyRequest,
    db: AsyncSession,
) -> Strategy:
    """
    Demote a strategy from the global library (scope: shared → private).

    Writes an audit log entry atomically.  Idempotent — demoting an already-
    private strategy re-records the audit but does not raise an error.
    """
    strategy = await db.get(Strategy, strategy_id)
    if strategy is None:
        raise StrategyNotFoundError(f"Strategy {strategy_id} not found")

    scope_before = strategy.scope
    strategy.scope = "private"

    audit = StrategyAuditLog(
        strategy_id=strategy_id,
        admin_user_id=admin_user_id,
        action="demote",
        reason=req.reason.strip(),
        scope_before=scope_before,
        scope_after="private",
        strategy_name=strategy.name,
    )
    db.add(audit)
    await db.flush()
    await db.refresh(strategy)

    logger.info(
        "Strategy %s demoted from shared library by admin %d",
        strategy_id, admin_user_id,
    )
    return strategy


async def get_strategy_audit_log(
    strategy_id: UUID,
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[StrategyAuditLogResponse], int]:
    """Return paginated audit trail for a specific strategy."""
    from app.models.user import User

    q = (
        select(StrategyAuditLog, User.username.label("admin_username"))
        .outerjoin(User, User.id == StrategyAuditLog.admin_user_id)
        .where(StrategyAuditLog.strategy_id == strategy_id)
    )

    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()

    q = (
        q.order_by(StrategyAuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(q)).all()

    items: list[StrategyAuditLogResponse] = []
    for log_entry, admin_username in rows:
        item = StrategyAuditLogResponse.model_validate(log_entry)
        item.admin_username = admin_username
        items.append(item)

    return items, total
