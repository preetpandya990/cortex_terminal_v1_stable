"""
Paper Trading & Audit — API Router
=====================================
All REST and WebSocket endpoints for the paper trading subsystem.

Routes (prefix: /api/v1/paper-trading)
---------------------------------------
  POST   /portfolios                   Create paper portfolio
  GET    /portfolios/me                Active portfolio + live summary
  PATCH  /portfolios/me                Update risk settings
  POST   /orders                       Place order from suggestion
  GET    /orders                       List orders (cursor-paginated)
  DELETE /orders/{order_id}            Cancel PENDING/OPEN order
  GET    /qty-suggestion               System-suggested quantity
  GET    /positions                    List positions
  GET    /positions/{position_id}      Position detail (fills + outcome)
  POST   /positions/{position_id}/close  Close position
  GET    /outcomes                     Audit log (admin/dev only)
  GET    /outcomes/stats               ML feedback stats (admin/dev only)
  GET    /pnl/snapshots                Historical daily P&L
  WS     /ws/pnl                       Real-time portfolio P&L stream

Authentication: all endpoints require a valid JWT (get_current_user_id).
RBAC: /outcomes/* endpoints additionally require role in ('admin', 'dev').
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Path,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_role
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.redis import get_redis, get_cache_service
from app.core.security import get_current_user_id
from app.models.paper_trading import PaperFill, PaperOrder, PaperPnlSnapshot, PaperTradeOutcome
from app.models.user import User
from app.schemas.paper_trading import (
    ClosePositionRequest,
    ConvertPositionRequest,
    CreatePortfolioRequest,
    ExitReason,
    OrdersListResponse,
    OutcomeStatsResponse,
    OutcomesListResponse,
    PaperFillResponse,
    PaperOrderResponse,
    PaperPositionDetailResponse,
    PaperPositionResponse,
    PaperPnlSnapshotResponse,
    PaperTradeOutcomeResponse,
    PlaceOrderRequest,
    PnlSnapshotsListResponse,
    PortfolioResponse,
    PortfolioSummaryResponse,
    PositionsListResponse,
    PriceTargetsResponse,
    QtySuggestionResponse,
    UpdatePortfolioSettingsRequest,
)
from app.services.paper_trading import portfolio_service, order_service, position_service, outcome_service
from app.services.paper_trading.pnl_worker import _PNL_CHANNEL_PREFIX

logger = logging.getLogger(__name__)

# ── PnL WebSocket constants ────────────────────────────────────────────────────
_PNL_WS_QUEUE_MAXSIZE = 128
_PNL_WS_AUTH_TIMEOUT  = 10.0   # seconds to wait for the in-band auth frame
_PNL_WS_HEARTBEAT_S   = 30.0
_PNL_WS_RECV_TIMEOUT  = 60.0   # client inactivity before the loop continues


# ── PnL WebSocket task helpers ────────────────────────────────────────────────

async def _pnl_listener(
    pubsub: Any,
    queue: asyncio.Queue[dict[str, Any]],
) -> None:
    """Forward Redis PnL pub/sub frames to the outbound queue."""
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            raw = message["data"]
            if isinstance(raw, bytes):
                raw = raw.decode()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # Drop oldest frame to make room for the fresh one
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(frame)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("PnL WS listener error")


async def _pnl_sender(
    websocket: WebSocket,
    queue: asyncio.Queue[dict[str, Any]],
) -> None:
    """Drain the outbound queue and write frames to the WebSocket client."""
    try:
        while True:
            frame = await queue.get()
            await websocket.send_json(frame)
    except asyncio.CancelledError:
        pass
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("PnL WS sender error")


async def _pnl_heartbeat(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Enqueue a server ping every 30 s so the client can detect stale connections."""
    try:
        while True:
            await asyncio.sleep(_PNL_WS_HEARTBEAT_S)
            try:
                queue.put_nowait({"type": "ping", "ts": datetime.now(timezone.utc).isoformat()})
            except asyncio.QueueFull:
                pass
    except asyncio.CancelledError:
        pass

router = APIRouter(dependencies=[Depends(get_current_user_id)])
ws_router = APIRouter()


def _user_id(user: User) -> int:
    return int(user.id)


# ──────────────────────────────────────────────────────────────────────────────
# Portfolio
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/portfolios",
    response_model=PortfolioResponse,
    status_code=201,
    summary="Create paper trading portfolio",
)
@limiter.limit("5/minute")
async def create_portfolio(
    request: Request,
    payload: CreatePortfolioRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PortfolioResponse:
    """
    Create a new PAPER portfolio for the authenticated user.

    One active portfolio is permitted per user. Attempting to create a second
    raises 409 Conflict.
    """
    portfolio = await portfolio_service.create_portfolio(
        session=session,
        user_id=_user_id(current_user),
        payload=payload,
    )
    await session.commit()
    return PortfolioResponse.model_validate(portfolio)


@router.get(
    "/portfolios/me",
    response_model=PortfolioSummaryResponse,
    summary="Get active portfolio with live stats",
)
@limiter.limit("60/minute")
async def get_portfolio_summary(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PortfolioSummaryResponse:
    """
    Return the authenticated user's active portfolio enriched with live
    aggregate stats: open position count, total unrealized P&L, portfolio
    value, total return %, and win rate.
    """
    return await portfolio_service.get_portfolio_summary(
        session=session,
        user_id=_user_id(current_user),
    )


@router.patch(
    "/portfolios/me",
    response_model=PortfolioResponse,
    summary="Update portfolio risk settings",
)
@limiter.limit("30/minute")
async def update_portfolio_settings(
    request: Request,
    payload: UpdatePortfolioSettingsRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PortfolioResponse:
    """
    Update `risk_per_trade_pct` and/or `max_open_positions` for the active
    portfolio. `initial_capital` and `name` are immutable.
    """
    active = await portfolio_service.get_active_portfolio(session, _user_id(current_user))
    portfolio = await portfolio_service.update_portfolio_settings(
        session=session,
        portfolio_id=active.id,
        user_id=_user_id(current_user),
        payload=payload,
    )
    await session.commit()
    return PortfolioResponse.model_validate(portfolio)


# ──────────────────────────────────────────────────────────────────────────────
# Quantity Suggestion
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/qty-suggestion",
    response_model=QtySuggestionResponse,
    summary="Get system-suggested quantity for a suggestion",
)
@limiter.limit("60/minute")
async def get_qty_suggestion(
    request: Request,
    suggestion_id: UUID = Query(..., description="TradeSuggestion UUID"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> QtySuggestionResponse:
    """
    Compute the system-recommended share quantity for a paper order derived
    from a TradeSuggestion, using the portfolio's risk settings.

    Returns the full rationale breakdown (capital_at_risk, stop_distance,
    risk_pct_of_cash) so the UI can display an explanation panel.
    """
    from app.models.trade_suggestions import TradeSuggestion
    from app.services.paper_trading.qty_suggester import suggest_quantity

    # Fetch suggestion
    stmt = select(TradeSuggestion).where(
        TradeSuggestion.suggestion_id == suggestion_id
    )
    suggestion = (await session.execute(stmt)).scalar_one_or_none()
    if suggestion is None:
        from app.exceptions import DataNotFoundError
        raise DataNotFoundError(f"TradeSuggestion {suggestion_id} not found.")

    portfolio = await portfolio_service.get_active_portfolio(session, _user_id(current_user))

    entry_price = suggestion.entry_price
    if entry_price is None:
        from app.exceptions import ValidationError
        raise ValidationError("This suggestion has no entry_price defined.")

    return suggest_quantity(
        current_cash=portfolio.current_cash,
        risk_per_trade_pct=portfolio.risk_per_trade_pct,
        entry_price=Decimal(str(entry_price)),
        stop_loss=Decimal(str(suggestion.stop_loss)) if suggestion.stop_loss else None,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Price Targets (ML-assisted SL/TP for manual trades)
# ──────────────────────────────────────────────────────────────────────────────

_PRICE_TARGETS_CACHE_KEY = "cai:pt:vol:{symbol}"
_PRICE_TARGETS_CACHE_TTL = 14_400  # 4 hours — volatility is a daily-scale measure


@router.get(
    "/price-targets",
    response_model=PriceTargetsResponse,
    summary="Compute ML-ensemble SL/TP levels for a manual trade",
)
@limiter.limit("60/minute")
async def get_price_targets(
    request: Request,
    symbol: str = Query(..., min_length=1, max_length=20, description="NSE trading symbol"),
    direction: str = Query(..., pattern="^(BUY|SELL)$", description="Trade direction"),
    entry_price: float = Query(..., gt=0, description="Intended entry price"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    cache: Any = Depends(get_cache_service),
) -> PriceTargetsResponse:
    """
    Compute stop-loss and take-profit levels for a manual paper trade.

    Primary path — ML ensemble (XGBoost + GRU, source: "ml_ensemble"):
      Loads 49-feature vectors via FeatureLoader, runs the live ensemble to
      obtain a calibrated annualised volatility, and applies the standard
      vol-based SL/TP formula.  EnsemblePredictor maintains its own 5-minute
      Redis cache so repeated opens of the same symbol are effectively free.

    Fallback path — OHLCV historical volatility (source: "historical_volatility"):
      Used when the ML predictor is unavailable or feature data is absent.
      Computes vol from 63 daily log-returns and caches the intermediate
      context per symbol for 4 hours.

    Returns HTTP 404 when neither path can produce meaningful price targets
    (insufficient OHLCV data or no instrument record for the symbol).
    """
    from decimal import Decimal as D
    from app.exceptions import DataNotFoundError
    from app.core.redis import get_redis
    from app.services.paper_trading.price_target_service import (
        PriceTargets,
        compute_price_targets_ml,
        fetch_volatility_context,
        apply_formula,
        volatility_context_to_dict,
        volatility_context_from_dict,
    )

    symbol_upper = symbol.strip().upper()
    entry_dec    = D(str(entry_price))
    targets: PriceTargets | None = None

    # ── Primary path: ML ensemble ─────────────────────────────────────────────
    ml_predictor = getattr(request.app.state, "ml_predictor", None)
    ml_ensemble  = getattr(request.app.state, "ml_ensemble",  None)

    if ml_predictor is not None and ml_ensemble is not None:
        try:
            targets = await compute_price_targets_ml(
                session=session,
                redis=get_redis(),
                predictor=ml_predictor,
                ensemble=ml_ensemble,
                symbol=symbol_upper,
                direction=direction,
                entry_price=entry_dec,
            )
        except Exception as exc:
            logger.warning(
                "ML price target computation failed for %s — falling back to OHLCV: %s",
                symbol_upper, exc,
            )

    # ── Fallback: OHLCV historical volatility ─────────────────────────────────
    if targets is None:
        cache_key = _PRICE_TARGETS_CACHE_KEY.format(symbol=symbol_upper)
        cached = await cache.get(cache_key)
        if cached is not None:
            ctx = volatility_context_from_dict(cached)
        else:
            ctx = await fetch_volatility_context(session, symbol_upper)
            if ctx is None:
                raise DataNotFoundError(
                    f"Insufficient OHLCV data for '{symbol_upper}'. "
                    "Price targets cannot be computed."
                )
            await cache.set(cache_key, volatility_context_to_dict(ctx), ttl=_PRICE_TARGETS_CACHE_TTL)

        targets = apply_formula(
            symbol=symbol_upper,
            direction=direction,
            entry_price=entry_dec,
            ctx=ctx,
            source="historical_volatility",
        )

    return PriceTargetsResponse(
        symbol=targets.symbol,
        direction=targets.direction,
        entry_price=float(targets.entry_price),
        stop_loss=float(targets.stop_loss),
        take_profit_1=float(targets.take_profit_1),
        take_profit_2=float(targets.take_profit_2),
        take_profit_3=float(targets.take_profit_3),
        volatility_annualized=targets.volatility_annualized,
        regime_type=targets.regime_type,
        candles_used=targets.candles_used,
        source=targets.source,
        computed_at=targets.computed_at,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Orders
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/orders",
    response_model=PaperOrderResponse,
    status_code=201,
    summary="Place a paper order from a suggestion",
)
@limiter.limit("30/minute")
async def place_order(
    request: Request,
    payload: PlaceOrderRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> PaperOrderResponse:
    """
    Place a paper order derived from a TradeSuggestion.

    MARKET orders are filled immediately at the current LTP (± 3 bps slippage).
    LIMIT / SL / SL-M orders are queued as OPEN; the P&L worker fills them
    when the price condition is met.

    On successful fill, ML feedback computation is scheduled as a background task.
    """
    paper_order, fill = await order_service.place_order(
        session=session,
        redis=redis,
        user_id=_user_id(current_user),
        payload=payload,
    )
    await session.commit()

    # Schedule ML feedback computation if a fill + outcome was written
    if fill is not None:
        outcome = await _latest_outcome_for_portfolio(
            session, paper_order.portfolio_id
        )
        if outcome:
            background_tasks.add_task(
                _compute_ml_feedback_bg,
                outcome.id,
                outcome.symbol,
                outcome.portfolio_id,
                outcome.user_id,
            )

    return PaperOrderResponse.model_validate(paper_order)


@router.get(
    "/orders",
    response_model=OrdersListResponse,
    summary="List paper orders (cursor-paginated)",
)
@limiter.limit("60/minute")
async def list_orders(
    request: Request,
    status: str | None = Query(None, description="Filter by order status"),
    symbol: str | None = Query(None, max_length=50, description="Filter by symbol"),
    cursor: str | None = Query(None, description="Opaque cursor for next page"),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> OrdersListResponse:
    """
    List paper orders for the authenticated user's portfolio, most recent first.
    Supports cursor-based pagination for stable, O(1) page traversal.
    """
    portfolio = await portfolio_service.get_active_portfolio(
        session, _user_id(current_user)
    )

    stmt = (
        select(PaperOrder)
        .where(PaperOrder.portfolio_id == portfolio.id)
    )
    if status:
        stmt = stmt.where(PaperOrder.status == status.upper())
    if symbol:
        stmt = stmt.where(PaperOrder.symbol == symbol.upper())

    # Apply cursor pagination on placed_at + id
    if cursor:
        import base64
        try:
            raw = base64.urlsafe_b64decode(cursor.encode()).decode()
            ts_str, id_str = raw.rsplit(":", 1)
            cursor_ts = datetime.fromisoformat(ts_str)
            cursor_id = UUID(id_str)
            stmt = stmt.where(
                (PaperOrder.placed_at < cursor_ts)
                | (
                    (PaperOrder.placed_at == cursor_ts)
                    & (PaperOrder.id < cursor_id)
                )
            )
        except Exception:
            pass  # Invalid cursor — treat as first page

    stmt = stmt.order_by(PaperOrder.placed_at.desc(), PaperOrder.id.desc())
    stmt = stmt.limit(limit + 1)

    result = await session.execute(stmt)
    rows = list(result.scalars().all())
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        import base64
        raw = f"{last.placed_at.isoformat()}:{last.id}"
        next_cursor = base64.urlsafe_b64encode(raw.encode()).decode()

    return OrdersListResponse(
        orders=[PaperOrderResponse.model_validate(r) for r in rows],
        total=None,
        next_cursor=next_cursor,
        has_more=has_more,
        limit=limit,
    )


@router.delete(
    "/orders/{order_id}",
    response_model=PaperOrderResponse,
    summary="Cancel a PENDING or OPEN order",
)
@limiter.limit("30/minute")
async def cancel_order(
    request: Request,
    order_id: UUID = Path(..., description="Order UUID"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PaperOrderResponse:
    """Cancel a PENDING or OPEN paper order. Terminal orders cannot be cancelled."""
    order = await order_service.cancel_order(
        session=session,
        order_id=order_id,
        user_id=_user_id(current_user),
    )
    await session.commit()
    return PaperOrderResponse.model_validate(order)


# ──────────────────────────────────────────────────────────────────────────────
# Positions
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/positions",
    response_model=PositionsListResponse,
    summary="List positions for active portfolio",
)
@limiter.limit("60/minute")
async def list_positions(
    request: Request,
    status: str | None = Query(None, description="Filter by status: OPEN or CLOSED"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PositionsListResponse:
    """List all positions (open and/or closed) for the user's active portfolio."""
    return await position_service.list_positions(
        session=session,
        user_id=_user_id(current_user),
        status_filter=status,
    )


@router.get(
    "/positions/{position_id}",
    response_model=PaperPositionDetailResponse,
    summary="Get position detail with fills and outcome",
)
@limiter.limit("60/minute")
async def get_position_detail(
    request: Request,
    position_id: UUID = Path(..., description="Position UUID"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PaperPositionDetailResponse:
    """
    Return full position detail including the fill history (WAC attribution)
    and the audit outcome record for closed positions.
    """
    position = await position_service.get_position(
        session=session,
        position_id=position_id,
        user_id=_user_id(current_user),
    )

    # Load fills for this position (via portfolio + symbol + side)
    fills_stmt = (
        select(PaperFill)
        .join(PaperOrder, PaperFill.order_id == PaperOrder.id)
        .where(
            PaperFill.portfolio_id == position.portfolio_id,
            PaperFill.symbol == position.symbol,
        )
        .order_by(PaperFill.executed_at.asc())
    )
    fills_raw = list((await session.execute(fills_stmt)).scalars().all())

    # Load outcome if closed
    outcome_raw = None
    if not position.is_open:
        outcome_stmt = select(PaperTradeOutcome).where(
            PaperTradeOutcome.position_id == position_id
        )
        outcome_raw = (await session.execute(outcome_stmt)).scalar_one_or_none()

    return PaperPositionDetailResponse(
        position=PaperPositionResponse.model_validate(position),
        fills=[PaperFillResponse.model_validate(f) for f in fills_raw],
        outcome=PaperTradeOutcomeResponse.model_validate(outcome_raw) if outcome_raw else None,
        fill_count=len(fills_raw),
    )


@router.post(
    "/positions/{position_id}/close",
    response_model=PaperPositionResponse,
    summary="Close a position (full or partial)",
)
@limiter.limit("30/minute")
async def close_position(
    request: Request,
    position_id: UUID = Path(..., description="Position UUID"),
    payload: ClosePositionRequest = ...,
    background_tasks: BackgroundTasks = ...,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> PaperPositionResponse:
    """
    Close a paper position fully or partially.

    MARKET close: fills at current LTP − 3 bps slippage (adverse).
    LIMIT close: fills at the specified price when reached.

    On full close, an audit outcome record is written and ML feedback
    is computed asynchronously.
    """
    position = await position_service.close_position(
        session=session,
        redis=redis,
        position_id=position_id,
        user_id=_user_id(current_user),
        payload=payload,
    )
    await session.commit()

    # Schedule ML feedback if position is now CLOSED
    if not position.is_open:
        outcome = await _latest_outcome_for_portfolio(
            session, position.portfolio_id
        )
        if outcome:
            background_tasks.add_task(
                _compute_ml_feedback_bg,
                outcome.id,
                outcome.symbol,
                outcome.portfolio_id,
                outcome.user_id,
            )

    return PaperPositionResponse.model_validate(position)


@router.post(
    "/positions/{position_id}/convert",
    response_model=PaperPositionResponse,
    summary="Convert position product type (CNC↔MIS)",
)
@limiter.limit("30/minute")
async def convert_position(
    request: Request,
    position_id: UUID = Path(..., description="Position UUID"),
    payload: ConvertPositionRequest = ...,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PaperPositionResponse:
    """
    Convert an open position's product type between CNC and MIS.

    Phase 1 constraints (422 if violated):
      - Position must be OPEN.
      - Same trading day only (position.opened_at IST date == today IST).
      - Full-position conversion; partial conversion is not yet supported.
      - NRML is not supported.

    The authoritative `product_type` on the position is updated immediately.
    All subsequent close operations will use the new product type for charge
    calculation and T+1 settlement enforcement.

    An immutable audit record is written to `paper_position_conversions`.
    """
    from app.services.paper_trading import conversion_service

    position = await conversion_service.convert_position(
        session=session,
        position_id=position_id,
        user_id=_user_id(current_user),
        payload=payload,
    )
    await session.commit()
    return PaperPositionResponse.model_validate(position)


# ──────────────────────────────────────────────────────────────────────────────
# Outcomes — admin/dev only
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/outcomes",
    response_model=OutcomesListResponse,
    summary="Audit log — all trade outcomes (admin/dev)",
    dependencies=[Depends(require_role("admin"))],
)
@limiter.limit("30/minute")
async def list_outcomes(
    request: Request,
    symbol: str | None = Query(None, max_length=50),
    user_id_filter: int | None = Query(None, alias="user_id"),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> OutcomesListResponse:
    """
    Audit log of all closed paper trade outcomes, most recent first.
    Scoped to all users (admin/dev can see platform-wide data).
    """
    stmt = select(PaperTradeOutcome)
    if symbol:
        stmt = stmt.where(PaperTradeOutcome.symbol == symbol.upper())
    if user_id_filter:
        stmt = stmt.where(PaperTradeOutcome.user_id == user_id_filter)

    if cursor:
        import base64
        try:
            raw = base64.urlsafe_b64decode(cursor.encode()).decode()
            ts_str, id_str = raw.rsplit(":", 1)
            cursor_ts = datetime.fromisoformat(ts_str)
            cursor_id = UUID(id_str)
            stmt = stmt.where(
                (PaperTradeOutcome.created_at < cursor_ts)
                | (
                    (PaperTradeOutcome.created_at == cursor_ts)
                    & (PaperTradeOutcome.id < cursor_id)
                )
            )
        except Exception:
            pass

    stmt = stmt.order_by(PaperTradeOutcome.created_at.desc(), PaperTradeOutcome.id.desc())
    stmt = stmt.limit(limit + 1)

    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        import base64
        raw = f"{last.created_at.isoformat()}:{last.id}"
        next_cursor = base64.urlsafe_b64encode(raw.encode()).decode()

    return OutcomesListResponse(
        outcomes=[PaperTradeOutcomeResponse.model_validate(r) for r in rows],
        total=None,
        next_cursor=next_cursor,
        has_more=has_more,
        limit=limit,
    )


@router.get(
    "/outcomes/stats",
    response_model=OutcomeStatsResponse,
    summary="Aggregated ML feedback stats (admin/dev)",
    dependencies=[Depends(require_role("admin"))],
)
@limiter.limit("10/minute")
async def get_outcome_stats(
    request: Request,
    portfolio_id: UUID | None = Query(None, description="Scope to a specific portfolio"),
    filter_user_id: int | None = Query(None, alias="user_id", description="Scope to a specific user"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> OutcomeStatsResponse:
    """
    Aggregated ML feedback statistics for the Admin/Dev dashboard.

    Returns platform-wide stats by default. Optionally scoped by portfolio_id
    or user_id query parameters.
    """
    stats = await outcome_service.get_outcome_stats(
        session=session,
        portfolio_id=portfolio_id,
        user_id=filter_user_id,
    )

    # Map raw dict → OutcomeStatsResponse
    from app.schemas.paper_trading import OutcomeStatsBreakdown

    def _breakdown(d: dict) -> dict[str, OutcomeStatsBreakdown]:
        return {
            k: OutcomeStatsBreakdown(**v)
            for k, v in d.items()
        }

    return OutcomeStatsResponse(
        total_trades=stats["total_trades"],
        total_winners=stats["total_winners"],
        total_losers=stats["total_losers"],
        win_rate=stats["win_rate"],
        total_gross_pnl=stats["total_gross_pnl"],
        total_charges=stats["total_charges"],
        total_net_pnl=stats["total_net_pnl"],
        avg_net_pnl_per_trade=stats["avg_net_pnl_per_trade"],
        avg_pnl_pct=stats["avg_pnl_pct"],
        avg_hold_duration_hours=stats["avg_hold_duration_hours"],
        ml_direction_accuracy=stats["ml_direction_accuracy"],
        tp1_hit_rate=stats["tp1_hit_rate"],
        tp2_hit_rate=stats["tp2_hit_rate"],
        tp3_hit_rate=stats["tp3_hit_rate"],
        sl_hit_rate=stats["sl_hit_rate"],
        by_exit_reason=stats["by_exit_reason"],
        by_confidence_level=_breakdown(stats["by_confidence_level"]),
        by_market_regime=_breakdown(stats["by_market_regime"]),
        by_signal_direction=_breakdown(stats["by_signal_direction"]),
        from_date=stats["from_date"],
        to_date=stats["to_date"],
        generated_at=stats["generated_at"],
    )


# ──────────────────────────────────────────────────────────────────────────────
# P&L Snapshots
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/pnl/snapshots",
    response_model=PnlSnapshotsListResponse,
    summary="Historical daily P&L snapshots",
)
@limiter.limit("30/minute")
async def list_pnl_snapshots(
    request: Request,
    from_date: str | None = Query(None, description="Start date YYYY-MM-DD"),
    to_date: str | None = Query(None, description="End date YYYY-MM-DD"),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PnlSnapshotsListResponse:
    """
    Historical daily P&L snapshots for the authenticated user's active portfolio.
    Ordered by snapshot_date descending (most recent first).
    """
    from datetime import date

    portfolio = await portfolio_service.get_active_portfolio(
        session, _user_id(current_user)
    )

    stmt = (
        select(PaperPnlSnapshot)
        .where(PaperPnlSnapshot.portfolio_id == portfolio.id)
        .order_by(PaperPnlSnapshot.snapshot_date.desc())
    )

    parsed_from: date | None = None
    parsed_to: date | None = None

    if from_date:
        try:
            parsed_from = date.fromisoformat(from_date)
            stmt = stmt.where(PaperPnlSnapshot.snapshot_date >= parsed_from)
        except ValueError:
            pass

    if to_date:
        try:
            parsed_to = date.fromisoformat(to_date)
            stmt = stmt.where(PaperPnlSnapshot.snapshot_date <= parsed_to)
        except ValueError:
            pass

    rows = list((await session.execute(stmt)).scalars().all())

    return PnlSnapshotsListResponse(
        snapshots=[PaperPnlSnapshotResponse.model_validate(r) for r in rows],
        count=len(rows),
        from_date=parsed_from,
        to_date=parsed_to,
    )


# ──────────────────────────────────────────────────────────────────────────────
# WebSocket — real-time P&L stream
# ──────────────────────────────────────────────────────────────────────────────

@ws_router.websocket("/paper-trading/ws/pnl")
async def pnl_websocket(
    websocket: WebSocket,
    redis: Redis = Depends(get_redis),
    session: AsyncSession = Depends(get_db),
) -> None:
    """
    Real-time portfolio P&L stream.

    Authentication (in-band — token never appears in URLs or server logs):
      Client must send ``{ type: "auth", token: "<jwt>" }`` as the very first frame
      within 10 seconds.  On success the server responds with
      ``{ type: "connected", portfolio_id: "..." }`` and begins streaming P&L frames.

    Token rotation (zero-downtime):
      Send ``{ type: "reauth", token: "<new_jwt>" }`` at any time.  The server
      validates the new token (must belong to the same user) and responds with
      ``{ type: "reauthed" }`` or ``{ type: "error", code: "REAUTH_FAILED" }``.

    Close codes:
      4001 — auth failed (bad/expired token or auth timeout)
      4004 — no active portfolio for this user

    Frame format: LivePnLUpdate JSON (see schemas/paper_trading.py).
    """
    from app.core.security import decode_token

    await websocket.accept()

    # ── In-band auth: wait for first frame ────────────────────────────────────
    uid: int = 0
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=_PNL_WS_AUTH_TIMEOUT)
        frame = json.loads(raw)
        if frame.get("type") != "auth" or not frame.get("token"):
            raise ValueError("expected {type: 'auth', token: '...'}")
        payload_data = decode_token(frame["token"])
        uid = int(payload_data.get("sub", 0))
        if not uid:
            raise ValueError("token missing sub claim")
    except asyncio.TimeoutError:
        logger.warning("PnL WS auth timeout")
        await websocket.send_json({"type": "error", "code": "AUTH_TIMEOUT"})
        await websocket.close(code=4001, reason="Auth timeout")
        return
    except Exception as exc:
        logger.warning("PnL WS auth failed: %s", exc)
        await websocket.send_json({"type": "error", "code": "AUTH_FAILED"})
        await websocket.close(code=4001, reason="Invalid token")
        return

    try:
        portfolio = await portfolio_service.get_active_portfolio(session, uid)
    except Exception:
        await websocket.send_json({"type": "error", "code": "NO_PORTFOLIO"})
        await websocket.close(code=4004, reason="No active portfolio")
        return

    await websocket.send_json({"type": "connected", "portfolio_id": str(portfolio.id)})
    portfolio_id = portfolio.id
    channel = f"{_PNL_CHANNEL_PREFIX}{portfolio_id}"

    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    logger.info("PnL WS connected: channel=%s uid=%d", channel, uid)

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_PNL_WS_QUEUE_MAXSIZE)
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(_pnl_listener(pubsub, queue),   name="pnl-listener"),
        asyncio.create_task(_pnl_sender(websocket, queue),  name="pnl-sender"),
        asyncio.create_task(_pnl_heartbeat(queue),          name="pnl-heartbeat"),
    ]

    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=_PNL_WS_RECV_TIMEOUT
                )
            except asyncio.TimeoutError:
                # No client message for 60 s — loop again; heartbeat keeps link alive
                continue

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "ping":
                await queue.put({"type": "pong", "ts": datetime.now(timezone.utc).isoformat()})

            elif msg_type == "reauth":
                # Proactive in-band token rotation — validate that it belongs to the same user.
                try:
                    new_payload = decode_token(msg.get("token", ""))
                    new_uid = int(new_payload.get("sub", 0))
                    if new_uid != uid:
                        raise ValueError("reauth token belongs to a different user")
                    await queue.put({"type": "reauthed"})
                except Exception as exc:
                    logger.warning("PnL WS reauth failed for uid=%d: %s", uid, exc)
                    await queue.put({"type": "error", "code": "REAUTH_FAILED"})

    except WebSocketDisconnect:
        logger.info("PnL WS disconnected: channel=%s uid=%d", channel, uid)
    except Exception:
        logger.exception("PnL WS unexpected error: channel=%s uid=%d", channel, uid)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        logger.info("PnL WS cleaned up: channel=%s uid=%d", channel, uid)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _latest_outcome_for_portfolio(
    session: AsyncSession,
    portfolio_id: UUID,
) -> PaperTradeOutcome | None:
    """Return the most recently created outcome for a portfolio (for background tasks)."""
    stmt = (
        select(PaperTradeOutcome)
        .where(PaperTradeOutcome.portfolio_id == portfolio_id)
        .order_by(PaperTradeOutcome.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _compute_ml_feedback_bg(
    outcome_id: UUID,
    symbol: str,
    portfolio_id: UUID,
    user_id: int,
) -> None:
    """
    Background task: compute ML feedback with exponential-backoff retry.

    Delegates to compute_ml_feedback_with_retry() which handles:
      - 3 attempts with exp backoff + jitter
      - MLFeedbackError DB record on final failure
      - Redis cai:ml:feedback_errors alert on final failure
      - Prometheus instrumentation
    """
    from app.core.database import AsyncSessionLocal
    from app.core.retry import compute_ml_feedback_with_retry

    await compute_ml_feedback_with_retry(
        outcome_id=outcome_id,
        symbol=symbol,
        portfolio_id=portfolio_id,
        user_id=user_id,
        session_factory=AsyncSessionLocal,
    )
