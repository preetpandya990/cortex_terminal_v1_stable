"""
StrategyActivationFSM — Lifecycle management for strategy activations.

State machine:
    WATCHING → IN_TRADE   (direct; IN_SETUP is a future refinement)
    IN_TRADE → PARTIAL_EXIT
    IN_TRADE → EXITED
    PARTIAL_EXIT → EXITED
    Any non-terminal → CANCELLED

Each transition appends an entry to `state_history`:
    {"state": "IN_TRADE", "at": "2026-05-06T09:16:00Z",
     "entry_price": 2885.0, "sl_price": 2820.0,
     "tp1_price": 2950.0, "tp2_price": 3020.0, "tp3_price": 3100.0}

auto_execute flow (on WATCHING → IN_TRADE):
    1. Resolve qty from PositionSizingConfig + live portfolio cash
    2. Place MARKET paper order via PaperOrderService
    3. Write activation.entry_order_id, entry_at
    4. Persist IN_TRADE state_history entry with computed SL/TP prices
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import InvalidStrategyStateError
from app.models.strategies import StrategyActivation, StrategyTrade
from app.schemas.strategies import (
    ActivationExitReason,
    ActivationState,
    PositionSizingConfig,
    SizingMode,
    StopLossConfig,
    StopLossType,
    StrategyDefinition,
    TakeProfitConfig,
)

logger = logging.getLogger(__name__)

# ── Valid state transitions ───────────────────────────────────────────────────

_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    ActivationState.WATCHING:      {ActivationState.IN_TRADE, ActivationState.CANCELLED},
    ActivationState.IN_SETUP:      {ActivationState.IN_TRADE, ActivationState.CANCELLED},
    ActivationState.IN_TRADE:      {ActivationState.PARTIAL_EXIT, ActivationState.EXITED, ActivationState.CANCELLED},
    ActivationState.PARTIAL_EXIT:  {ActivationState.EXITED, ActivationState.CANCELLED},
    ActivationState.EXITED:        set(),
    ActivationState.CANCELLED:     set(),
}


# ── Activation creation ───────────────────────────────────────────────────────

async def create_activation(
    *,
    user_id: int,
    strategy_id: UUID,
    signal_id: int | None,
    symbol: str,
    instrument_key: str | None,
    strategy_snapshot: dict[str, Any],
    auto_execute: bool,
    db: AsyncSession,
) -> StrategyActivation:
    """
    Persist a new WATCHING activation.

    Does NOT flush or commit — the caller is responsible for db.commit().
    """
    now = datetime.now(timezone.utc)
    activation = StrategyActivation(
        user_id=user_id,
        strategy_id=strategy_id,
        signal_id=signal_id,
        symbol=symbol,
        instrument_key=instrument_key,
        strategy_snapshot=strategy_snapshot,
        auto_execute=auto_execute,
        state=ActivationState.WATCHING,
        state_history=[{"state": ActivationState.WATCHING, "at": now.isoformat()}],
    )
    db.add(activation)
    await db.flush()  # populate activation.id without committing
    return activation


# ── FSM transition ────────────────────────────────────────────────────────────

def transition(
    activation: StrategyActivation,
    new_state: str,
    extra: dict[str, Any] | None = None,
) -> StrategyActivation:
    """
    Advance the activation FSM to `new_state`.

    Appends a transition entry to `state_history` and updates `activation.state`.
    Does NOT flush/commit — caller must do that.

    Args:
        activation: The ORM row to mutate.
        new_state:  Target state string (from ActivationState enum).
        extra:      Optional dict merged into the history entry (e.g. prices).

    Raises:
        InvalidStrategyStateError: When the transition is not permitted.
    """
    allowed = _ALLOWED_TRANSITIONS.get(activation.state, set())
    if new_state not in allowed:
        raise InvalidStrategyStateError(
            f"Cannot transition activation {activation.id} "
            f"from {activation.state} to {new_state}. "
            f"Allowed: {sorted(allowed) or 'none (terminal)'}"
        )

    now = datetime.now(timezone.utc)
    entry: dict[str, Any] = {"state": new_state, "at": now.isoformat()}
    if extra:
        entry.update(extra)

    # SQLAlchemy does not track mutations inside a JSONB list; reassign.
    activation.state_history = list(activation.state_history) + [entry]
    activation.state = new_state
    return activation


# ── IN_TRADE entry with optional auto-execute ─────────────────────────────────

async def enter_in_trade(
    *,
    activation: StrategyActivation,
    entry_price: float,
    db: AsyncSession,
    redis: Redis,
) -> StrategyActivation:
    """
    Transition to IN_TRADE, compute effective SL/TP prices, and (if
    auto_execute=True) place a MARKET paper order.

    The computed absolute SL/TP price levels are stored in the IN_TRADE
    state_history entry so the SL/TP worker never needs to re-derive them.

    Args:
        activation:  WATCHING or IN_SETUP activation (mutated in-place).
        entry_price: Confirmed fill price (from signal or LTP).
        db:          Active AsyncSession.
        redis:       Redis client for PaperOrderService.

    Returns:
        The same activation after state mutation (not yet committed).
    """
    defn = StrategyDefinition.model_validate(activation.strategy_snapshot)
    direction = "LONG"  # Phase 3: BUY signals only; SHORT in Phase 4

    sl_price = _compute_sl_price(entry_price, defn.stop_loss, direction)
    tp_prices = _compute_tp_prices(entry_price, defn.take_profit, direction)

    extra = {
        "entry_price": entry_price,
        "direction": direction,
    }
    if sl_price is not None:
        extra["sl_price"] = sl_price
    for i, tp in enumerate(tp_prices, start=1):
        extra[f"tp{i}_price"] = tp

    if activation.auto_execute:
        order, fill = await _place_entry_order(activation, entry_price, defn, db, redis)
        activation.entry_order_id = order.id
        activation.entry_at = datetime.now(timezone.utc)
        extra["entry_order_id"] = str(order.id)
        if fill:
            extra["fill_price"] = float(fill.fill_price)
            extra["quantity"] = fill.quantity

    transition(activation, ActivationState.IN_TRADE, extra=extra)
    return activation


# ── EXITED — write StrategyTrade record ───────────────────────────────────────

async def enter_exited(
    *,
    activation: StrategyActivation,
    exit_price: float,
    exit_reason: str,
    db: AsyncSession,
    redis: Redis | None = None,
) -> tuple[StrategyActivation, StrategyTrade | None]:
    """
    Transition to EXITED, close any open paper position (auto_execute only),
    and write a StrategyTrade immutable audit record.

    Returns (activation, trade).  `trade` is None when no paper position existed.
    """
    now = datetime.now(timezone.utc)

    in_trade_entry = _get_state_entry(activation, ActivationState.IN_TRADE)
    entry_price = float(in_trade_entry.get("entry_price", exit_price)) if in_trade_entry else exit_price
    entry_at = _parse_dt(in_trade_entry.get("at")) if in_trade_entry else now
    direction = in_trade_entry.get("direction", "LONG") if in_trade_entry else "LONG"
    quantity = in_trade_entry.get("quantity", 1) if in_trade_entry else 1

    hold_secs = max(0, int((now - entry_at).total_seconds()))

    ep = Decimal(str(round(entry_price, 4)))
    xp = Decimal(str(round(exit_price, 4)))
    qty = int(quantity)

    gross_pnl = (xp - ep) * qty if direction == "LONG" else (ep - xp) * qty
    net_pnl = gross_pnl  # Charge computation lives in PaperOrderService; keep simple here

    defn = StrategyDefinition.model_validate(activation.strategy_snapshot)
    pnl_pct = ((xp - ep) / ep * 100) if direction == "LONG" else ((ep - xp) / ep * 100)

    trade: StrategyTrade | None = None
    if qty > 0 and ep > 0 and xp > 0:
        trade = StrategyTrade(
            strategy_id=activation.strategy_id,
            user_id=activation.user_id,
            activation_id=activation.id,
            symbol=activation.symbol,
            direction=direction,
            entry_price=ep,
            exit_price=xp,
            quantity=qty,
            gross_pnl=gross_pnl,
            total_charges=Decimal("0"),
            net_pnl=net_pnl,
            pnl_pct=pnl_pct.quantize(Decimal("0.0001")),
            exit_reason=exit_reason,
            strategy_version=1,  # denormalized; fetch from Strategy if needed
            hold_duration_seconds=hold_secs,
            entry_at=entry_at,
            exit_at=now,
        )
        db.add(trade)

    if activation.auto_execute and activation.entry_order_id and redis is not None:
        await _close_paper_position(activation, db, redis)

    transition(
        activation,
        ActivationState.EXITED,
        extra={"exit_price": exit_price, "reason": exit_reason},
    )
    activation.exit_reason = exit_reason
    activation.exit_at = now

    return activation, trade


# ── SL/TP price computation (pure) ───────────────────────────────────────────

def compute_sl_tp_prices(
    entry_price: float,
    snapshot: dict[str, Any],
    direction: str = "LONG",
) -> dict[str, float | None]:
    """
    Derive absolute SL/TP price levels from a strategy_snapshot dict.
    Returns {"sl": ..., "tp1": ..., "tp2": ..., "tp3": ...}.
    """
    defn = StrategyDefinition.model_validate(snapshot)
    sl = _compute_sl_price(entry_price, defn.stop_loss, direction)
    tps = _compute_tp_prices(entry_price, defn.take_profit, direction)
    return {
        "sl":  sl,
        "tp1": tps[0] if len(tps) > 0 else None,
        "tp2": tps[1] if len(tps) > 1 else None,
        "tp3": tps[2] if len(tps) > 2 else None,
    }


def get_in_trade_prices(activation: StrategyActivation) -> dict[str, float | None]:
    """Extract the SL/TP price levels stored in the IN_TRADE state_history entry."""
    entry = _get_state_entry(activation, ActivationState.IN_TRADE)
    if not entry:
        return {"sl": None, "tp1": None, "tp2": None, "tp3": None}
    return {
        "sl":  entry.get("sl_price"),
        "tp1": entry.get("tp1_price"),
        "tp2": entry.get("tp2_price"),
        "tp3": entry.get("tp3_price"),
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _compute_sl_price(
    entry: float,
    sl_cfg: StopLossConfig | None,
    direction: str,
) -> float | None:
    if sl_cfg is None:
        return None
    if sl_cfg.type == StopLossType.fixed_pct:
        mult = (1.0 - sl_cfg.value / 100.0) if direction == "LONG" else (1.0 + sl_cfg.value / 100.0)
        return round(entry * mult, 2)
    if sl_cfg.type == StopLossType.price_level:
        return round(float(sl_cfg.value), 2)
    # atr_multiple: needs live ATR — not available at activation time; deferred
    return None


def _compute_tp_prices(
    entry: float,
    tp_cfg: TakeProfitConfig | None,
    direction: str,
) -> list[float]:
    if tp_cfg is None or not tp_cfg.levels:
        return []
    prices: list[float] = []
    for level in tp_cfg.levels:
        mult = (1.0 + level.pct / 100.0) if direction == "LONG" else (1.0 - level.pct / 100.0)
        prices.append(round(entry * mult, 2))
    return prices


def _get_state_entry(
    activation: StrategyActivation,
    state: str,
) -> dict[str, Any] | None:
    for entry in reversed(activation.state_history):
        if entry.get("state") == state:
            return entry
    return None


def _parse_dt(iso: str | None) -> datetime:
    if not iso:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


async def _place_entry_order(
    activation: StrategyActivation,
    entry_price: float,
    defn: StrategyDefinition,
    db: AsyncSession,
    redis: Redis,
) -> tuple[Any, Any]:
    """Place a MARKET BUY order for the activation's symbol."""
    from app.schemas.paper_trading import (
        OrderType, ProductType, TransactionType,
    )
    from app.services.paper_trading.order_service import place_order
    from app.schemas.paper_trading import PlaceOrderRequest

    qty = _compute_quantity(entry_price, defn.position_sizing, db, activation.user_id)

    # Resolve instrument_key: use stored one or fall back to symbol
    instrument_key = activation.instrument_key or activation.symbol

    product = ProductType.MIS if defn.guardrails.allowed_sessions == ["morning", "midday", "afternoon"] else ProductType.CNC

    payload = PlaceOrderRequest(
        symbol=activation.symbol,
        instrument_key=instrument_key,
        entry_price=round(entry_price, 2),
        transaction_type=TransactionType.BUY,
        product_type=product,
        order_type=OrderType.MARKET,
        quantity=max(1, qty),
    )

    return await place_order(db, redis, activation.user_id, payload)


def _compute_quantity(
    entry_price: float,
    sizing: PositionSizingConfig,
    db: AsyncSession,
    user_id: int,
) -> int:
    """
    Compute order quantity from PositionSizingConfig.
    For async portfolio cash lookup in risk_pct/kelly modes, falls back to
    a safe minimum of 1 — the proper implementation requires an await
    which we skip here to keep the sync call graph clean.
    Phase 3 auto-execute uses the simplest viable sizing.
    """
    if sizing.mode == SizingMode.fixed_qty and sizing.fixed_qty:
        return sizing.fixed_qty
    # risk_pct / kelly modes need live portfolio cash — default 1 for safety
    # Phase 4 will wire the async portfolio lookup properly
    return 1


async def _close_paper_position(
    activation: StrategyActivation,
    db: AsyncSession,
    redis: Redis,
) -> None:
    """Close the paper position linked to the activation (best-effort)."""
    from sqlalchemy import select as sa_select
    from app.models.paper_trading import PaperPosition
    from app.services.paper_trading.position_service import close_position
    from app.schemas.paper_trading import ClosePositionRequest, OrderType

    try:
        stmt = sa_select(PaperPosition).where(
            PaperPosition.instrument_key == (activation.instrument_key or activation.symbol),
            PaperPosition.user_id == activation.user_id,
            PaperPosition.state == "OPEN",
        )
        result = await db.execute(stmt)
        position = result.scalar_one_or_none()
        if position is None:
            return

        payload = ClosePositionRequest(
            quantity=position.quantity,
            order_type=OrderType.MARKET,
        )
        await close_position(db, redis, position.id, activation.user_id, payload)
    except Exception as exc:
        logger.warning(
            "Failed to close paper position for activation %s: %s",
            activation.id, exc,
        )
