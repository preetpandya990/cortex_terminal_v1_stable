# PATH: backend/app/api/v1/strategies.py
"""
Trading Strategies — API Router
=================================
All REST endpoints for strategy CRUD, subscriptions, activations,
trades, performance, and backtesting.

Routes (prefix: /api/v1/strategies)
--------------------------------------
  Strategy Library
    POST   /                             Create strategy (trader+)
    GET    /                             List global library (all authenticated)
    GET    /mine                         List own strategies
    GET    /{id}                         Get strategy detail
    PUT    /{id}                         Update strategy (owner only)
    DELETE /{id}                         Archive strategy (owner only)
    POST   /{id}/duplicate               Clone strategy

  Subscriptions
    GET    /subscriptions                Active subscriptions (ordered by priority)
    POST   /{id}/subscribe               Subscribe to a strategy
    DELETE /{id}/subscribe               Unsubscribe
    PUT    /subscriptions/reorder        Reorder subscription priorities

  Activations
    GET    /activations                  List activations (filterable)
    GET    /activations/{id}             Get single activation
    POST   /activations/{id}/cancel      Cancel an activation
    POST   /activations/{id}/exit        Manually exit an activation

  Trades & Performance
    GET    /{id}/trades                  Trade history for a strategy
    GET    /{id}/performance             Aggregated performance metrics

  Backtesting
    POST   /{id}/backtests               Queue backtest run
    GET    /{id}/backtests               List backtest runs
    GET    /{id}/backtests/{run_id}       Get single backtest run
    DELETE /{id}/backtests/{run_id}       Cancel pending/running backtest

Authentication: all endpoints require a valid JWT.
Rate limits are applied per-endpoint (see decorators).

IMPORTANT — route ordering: all fixed-path routes (/mine, /subscriptions,
/subscriptions/reorder, /activations, /activations/{id}/...) MUST be registered
before the /{strategy_id} wildcard routes, otherwise FastAPI matches the wildcard
first, tries to parse the literal segment as a UUID, and returns 422.
"""
# NOTE: do NOT add `from __future__ import annotations` here — FastAPI needs
# runtime-resolved type annotations for its dependency injection system.

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import require_role
from app.core.limiter import limiter
from app.core.security import CurrentUserID
from app.schemas.strategies import (
    ActivationsListResponse,
    BacktestRunsListResponse,
    CreateStrategyRequest,
    ManualExitRequest,
    PatchSubscriptionRequest,
    ReorderSubscriptionsRequest,
    StrategyActivationResponse,
    StrategyBacktestRunResponse,
    StrategyPerformanceResponse,
    StrategyResponse,
    StrategiesListResponse,
    StrategySummaryResponse,
    StrategyTradeResponse,
    SubscribeStrategyRequest,
    SubscriptionsListResponse,
    TradesListResponse,
    TriggerBacktestRequest,
    UpdateStrategyRequest,
    UserStrategySubscriptionResponse,
)
from app.services import strategy_service

logger = logging.getLogger(__name__)

router = APIRouter()


# ──────────────────────────────────────────────────────────────────────────────
# Strategy Library — fixed paths (must precede /{strategy_id} wildcard)
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/",
    response_model=StrategyResponse,
    status_code=201,
    summary="Create a new trading strategy",
)
@limiter.limit("10/minute")
async def create_strategy(
    request: Request,
    body: CreateStrategyRequest,
    user_id: CurrentUserID,
    _: None = Depends(require_role("trader")),
    db: AsyncSession = Depends(get_db),
) -> StrategyResponse:
    strategy = await strategy_service.create_strategy(int(user_id), body, db)
    await db.commit()
    return StrategyResponse.model_validate(strategy)


@router.get(
    "/",
    response_model=StrategiesListResponse,
    summary="List the global strategy library",
)
@limiter.limit("60/minute")
async def list_strategies(
    request: Request,
    user_id: CurrentUserID,
    status: str | None = Query(default=None, description="Filter by status"),
    tags: list[str] | None = Query(default=None, description="Filter by tags"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> StrategiesListResponse:
    items, total = await strategy_service.list_strategies(
        db, status=status, tags=tags, page=page, page_size=page_size
    )
    return StrategiesListResponse(
        items=[StrategySummaryResponse.model_validate(s) for s in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/mine",
    response_model=StrategiesListResponse,
    summary="List the authenticated user's own strategies",
)
@limiter.limit("60/minute")
async def list_my_strategies(
    request: Request,
    user_id: CurrentUserID,
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> StrategiesListResponse:
    items, total = await strategy_service.list_user_strategies(
        int(user_id), db, status=status, page=page, page_size=page_size
    )
    return StrategiesListResponse(
        items=[StrategySummaryResponse.model_validate(s) for s in items],
        total=total,
        page=page,
        page_size=page_size,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Subscriptions — fixed paths (must precede /{strategy_id} wildcard)
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/subscriptions",
    response_model=SubscriptionsListResponse,
    summary="List active strategy subscriptions ordered by priority",
)
@limiter.limit("60/minute")
async def get_subscriptions(
    request: Request,
    user_id: CurrentUserID,
    db: AsyncSession = Depends(get_db),
) -> SubscriptionsListResponse:
    subs = await strategy_service.get_active_subscriptions(int(user_id), db)
    return SubscriptionsListResponse(
        items=[UserStrategySubscriptionResponse.model_validate(s) for s in subs],
        total=len(subs),
    )


@router.put(
    "/subscriptions/reorder",
    response_model=SubscriptionsListResponse,
    summary="Atomically reorder subscription priorities",
)
@limiter.limit("20/minute")
async def reorder_subscriptions(
    request: Request,
    body: ReorderSubscriptionsRequest,
    user_id: CurrentUserID,
    db: AsyncSession = Depends(get_db),
) -> SubscriptionsListResponse:
    subs = await strategy_service.reorder_subscriptions(int(user_id), body, db)
    await db.commit()
    return SubscriptionsListResponse(
        items=[UserStrategySubscriptionResponse.model_validate(s) for s in subs],
        total=len(subs),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Activations — fixed paths (must precede /{strategy_id} wildcard)
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/activations",
    response_model=ActivationsListResponse,
    summary="List strategy activations",
)
@limiter.limit("60/minute")
async def list_activations(
    request: Request,
    user_id: CurrentUserID,
    symbol: str | None = Query(default=None, description="Filter by symbol"),
    state: str | None = Query(default=None, description="Filter by activation state"),
    strategy_id: UUID | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> ActivationsListResponse:
    items, total = await strategy_service.list_activations(
        int(user_id), db,
        symbol=symbol, state=state, strategy_id=strategy_id,
        page=page, page_size=page_size,
    )
    return ActivationsListResponse(
        items=[StrategyActivationResponse.model_validate(a) for a in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/activations/{activation_id}",
    response_model=StrategyActivationResponse,
    summary="Get a single activation",
)
@limiter.limit("120/minute")
async def get_activation(
    request: Request,
    user_id: CurrentUserID,
    activation_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyActivationResponse:
    activation = await strategy_service.get_activation(activation_id, int(user_id), db)
    return StrategyActivationResponse.model_validate(activation)


@router.post(
    "/activations/{activation_id}/cancel",
    response_model=StrategyActivationResponse,
    summary="Cancel an active strategy activation",
)
@limiter.limit("30/minute")
async def cancel_activation(
    request: Request,
    user_id: CurrentUserID,
    activation_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyActivationResponse:
    activation = await strategy_service.cancel_activation(activation_id, int(user_id), db)
    await db.commit()
    return StrategyActivationResponse.model_validate(activation)


@router.post(
    "/activations/{activation_id}/exit",
    response_model=StrategyActivationResponse,
    summary="Manually exit an IN_TRADE or PARTIAL_EXIT activation",
)
@limiter.limit("30/minute")
async def exit_activation(
    request: Request,
    body: ManualExitRequest,
    user_id: CurrentUserID,
    activation_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyActivationResponse:
    activation = await strategy_service.exit_activation(
        activation_id, int(user_id), body.exit_price, db
    )
    await db.commit()
    return StrategyActivationResponse.model_validate(activation)


# ──────────────────────────────────────────────────────────────────────────────
# Strategy Library — wildcard /{strategy_id} routes (must come last)
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/{strategy_id}",
    response_model=StrategyResponse,
    summary="Get strategy detail",
)
@limiter.limit("120/minute")
async def get_strategy(
    request: Request,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyResponse:
    strategy = await strategy_service.get_strategy(strategy_id, int(user_id), db)
    return StrategyResponse.model_validate(strategy)


@router.put(
    "/{strategy_id}",
    response_model=StrategyResponse,
    summary="Update a strategy (owner only)",
)
@limiter.limit("30/minute")
async def update_strategy(
    request: Request,
    body: UpdateStrategyRequest,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyResponse:
    strategy = await strategy_service.update_strategy(strategy_id, body, int(user_id), db)
    await db.commit()
    return StrategyResponse.model_validate(strategy)


@router.delete(
    "/{strategy_id}",
    status_code=204,
    summary="Archive a strategy (owner only)",
)
@limiter.limit("10/minute")
async def delete_strategy(
    request: Request,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    await strategy_service.delete_strategy(strategy_id, int(user_id), db)
    await db.commit()


@router.post(
    "/{strategy_id}/duplicate",
    response_model=StrategyResponse,
    status_code=201,
    summary="Clone a strategy into a new private draft",
)
@limiter.limit("10/minute")
async def duplicate_strategy(
    request: Request,
    user_id: CurrentUserID,
    _: None = Depends(require_role("trader")),
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyResponse:
    source = await strategy_service.get_strategy(strategy_id, int(user_id), db)
    from app.schemas.strategies import CreateStrategyRequest, StrategyDefinition
    clone_req = CreateStrategyRequest(
        name=f"{source.name} (copy)",
        description=source.description,
        tags=source.tags or [],
        definition=StrategyDefinition.model_validate(source.definition),
    )
    clone = await strategy_service.create_strategy(int(user_id), clone_req, db)
    await db.commit()
    return StrategyResponse.model_validate(clone)


@router.post(
    "/{strategy_id}/subscribe",
    response_model=UserStrategySubscriptionResponse,
    status_code=201,
    summary="Subscribe to a strategy",
)
@limiter.limit("20/minute")
async def subscribe(
    request: Request,
    body: SubscribeStrategyRequest,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> UserStrategySubscriptionResponse:
    sub = await strategy_service.subscribe(int(user_id), strategy_id, body, db)
    await db.commit()
    return UserStrategySubscriptionResponse.model_validate(sub)


@router.patch(
    "/{strategy_id}/subscribe",
    response_model=UserStrategySubscriptionResponse,
    summary="Pause or resume a strategy subscription",
)
@limiter.limit("30/minute")
async def patch_subscription(
    request: Request,
    body: PatchSubscriptionRequest,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> UserStrategySubscriptionResponse:
    sub = await strategy_service.set_subscription_active(
        int(user_id), strategy_id, body.is_active, db
    )
    await db.commit()
    return UserStrategySubscriptionResponse.model_validate(sub)


@router.delete(
    "/{strategy_id}/subscribe",
    status_code=204,
    summary="Unsubscribe from a strategy",
)
@limiter.limit("20/minute")
async def unsubscribe(
    request: Request,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    await strategy_service.unsubscribe(int(user_id), strategy_id, db)
    await db.commit()


# ──────────────────────────────────────────────────────────────────────────────
# Trades & Performance
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/{strategy_id}/trades",
    response_model=TradesListResponse,
    summary="Trade history for a strategy",
)
@limiter.limit("60/minute")
async def list_trades(
    request: Request,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> TradesListResponse:
    items, total = await strategy_service.list_trades(
        strategy_id, int(user_id), db, page=page, page_size=page_size
    )
    return TradesListResponse(
        items=[StrategyTradeResponse.model_validate(t) for t in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/{strategy_id}/performance",
    response_model=StrategyPerformanceResponse,
    summary="Aggregated performance metrics",
)
@limiter.limit("30/minute")
async def get_performance(
    request: Request,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyPerformanceResponse:
    return await strategy_service.get_performance(strategy_id, int(user_id), db)


# ──────────────────────────────────────────────────────────────────────────────
# Backtesting
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/{strategy_id}/backtests",
    response_model=StrategyBacktestRunResponse,
    status_code=202,
    summary="Queue an async backtest run",
)
@limiter.limit("5/minute")
async def trigger_backtest(
    request: Request,
    body: TriggerBacktestRequest,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyBacktestRunResponse:
    from app.services.strategy_engine.backtest_runner import run_backtest_task

    run = await strategy_service.trigger_backtest(int(user_id), strategy_id, body, db)
    await db.commit()

    ml_predictor = getattr(request.app.state, "ml_predictor", None)
    asyncio.create_task(
        run_backtest_task(run_id=run.id, ml_predictor=ml_predictor),
        name=f"backtest:{run.id}",
    )

    return StrategyBacktestRunResponse.model_validate(run)


@router.get(
    "/{strategy_id}/backtests",
    response_model=BacktestRunsListResponse,
    summary="List backtest runs for a strategy",
)
@limiter.limit("60/minute")
async def list_backtests(
    request: Request,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> BacktestRunsListResponse:
    runs = await strategy_service.list_backtests(strategy_id, int(user_id), db)
    return BacktestRunsListResponse(
        items=[StrategyBacktestRunResponse.model_validate(r) for r in runs],
        total=len(runs),
    )


@router.get(
    "/{strategy_id}/backtests/{run_id}",
    response_model=StrategyBacktestRunResponse,
    summary="Get a single backtest run",
)
@limiter.limit("120/minute")
async def get_backtest(
    request: Request,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    run_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyBacktestRunResponse:
    run = await strategy_service.get_backtest(run_id, int(user_id), db)
    return StrategyBacktestRunResponse.model_validate(run)


@router.delete(
    "/{strategy_id}/backtests/{run_id}",
    status_code=204,
    summary="Cancel a pending or running backtest",
)
@limiter.limit("10/minute")
async def cancel_backtest(
    request: Request,
    user_id: CurrentUserID,
    strategy_id: UUID = Path(...),
    run_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> None:
    await strategy_service.cancel_backtest(run_id, int(user_id), db)
    await db.commit()
