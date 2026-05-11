"""
SL/TP Monitoring Worker — Strategy Activation Position Guard.

Polls all IN_TRADE and PARTIAL_EXIT activations every 500ms, reads the
current LTP from Redis, and triggers FSM transitions when SL or TP levels
are breached.

Architecture mirrors the paper-trading PnL worker:
  • Long-running asyncio task, started in app lifespan.
  • Each tick runs one bulk DB query → per-activation Redis LTP read → FSM.
  • Commits after each batch; failures are logged and skipped.

State machine actions triggered here:
  IN_TRADE     + SL hit         → EXITED  (exit_reason=SL)
  IN_TRADE     + TP1 hit        → PARTIAL_EXIT (exit_reason=TP1) if TP2/TP3 exist
  IN_TRADE     + TP1/only hit   → EXITED  (exit_reason=TP1)
  PARTIAL_EXIT + TP2 hit        → PARTIAL_EXIT (exit_reason=TP2) if TP3 exists
  PARTIAL_EXIT + final TP hit   → EXITED  (exit_reason=FULL_TP / TP3)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategies import StrategyActivation
from app.services.strategy_engine.activation_fsm import (
    enter_exited,
    get_in_trade_prices,
    transition,
)
from app.schemas.strategies import ActivationExitReason, ActivationState

logger = logging.getLogger(__name__)

_TICK_INTERVAL_S: float = 0.5          # 500 ms polling cadence
_LTP_KEY_PREFIX: str = "cai:ltp:"
_MAX_BATCH_SIZE: int = 200             # cap per tick to bound latency


async def run_sl_tp_worker(redis: Redis) -> None:
    """
    Entry point — start the SL/TP monitoring loop.

    Called from app lifespan; cancelled on shutdown.
    """
    logger.info("SL/TP worker starting")
    try:
        await _worker_loop(redis)
    except asyncio.CancelledError:
        logger.info("SL/TP worker cancelled cleanly")
    except Exception as exc:
        logger.error("SL/TP worker crashed: %s", exc, exc_info=True)
        raise


async def _worker_loop(redis: Redis) -> None:
    while True:
        start = asyncio.get_event_loop().time()
        try:
            await _process_tick(redis)
        except Exception as exc:
            logger.warning("SL/TP worker tick error: %s", exc)
        elapsed = asyncio.get_event_loop().time() - start
        await asyncio.sleep(max(0.0, _TICK_INTERVAL_S - elapsed))


async def _process_tick(redis: Redis) -> None:
    """Load active activations and check SL/TP for each."""
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        activations = await _load_active_activations(db)
        if not activations:
            return

        changed: list[StrategyActivation] = []
        for activation in activations:
            try:
                mutated = await _check_activation(activation, redis, db)
                if mutated:
                    changed.append(activation)
            except Exception as exc:
                logger.warning(
                    "SL/TP check failed for activation %s: %s", activation.id, exc
                )

        if changed:
            await db.commit()
            logger.debug("SL/TP worker committed %d activation(s)", len(changed))


async def _load_active_activations(db: AsyncSession) -> list[StrategyActivation]:
    stmt = (
        select(StrategyActivation)
        .where(
            StrategyActivation.state.in_(
                (ActivationState.IN_TRADE, ActivationState.PARTIAL_EXIT)
            )
        )
        .limit(_MAX_BATCH_SIZE)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _check_activation(
    activation: StrategyActivation,
    redis: Redis,
    db: AsyncSession,
) -> bool:
    """
    Check SL/TP breach for a single activation.

    Returns True if any FSM transition was triggered (session needs commit).
    """
    ltp = await _read_ltp(activation, redis)
    if ltp is None:
        return False

    prices = get_in_trade_prices(activation)
    sl_price = prices.get("sl")
    tp1 = prices.get("tp1")
    tp2 = prices.get("tp2")
    tp3 = prices.get("tp3")

    direction = _get_direction(activation)

    # ── SL check (highest priority) ───────────────────────────────────────────
    if sl_price is not None and _sl_breached(ltp, sl_price, direction):
        logger.info(
            "SL hit — activation=%s symbol=%s ltp=%.2f sl=%.2f",
            activation.id, activation.symbol, ltp, sl_price,
        )
        await enter_exited(
            activation=activation,
            exit_price=ltp,
            exit_reason=ActivationExitReason.SL,
            db=db,
            redis=None,   # position closure handled separately
        )
        return True

    # ── TP checks ─────────────────────────────────────────────────────────────
    current_state = activation.state

    if current_state == ActivationState.IN_TRADE and tp1 is not None:
        if _tp_reached(ltp, tp1, direction):
            if tp2 is not None:
                # Partial exit — more TP levels remain
                logger.info(
                    "TP1 hit (partial) — activation=%s symbol=%s ltp=%.2f tp1=%.2f",
                    activation.id, activation.symbol, ltp, tp1,
                )
                transition(
                    activation,
                    ActivationState.PARTIAL_EXIT,
                    extra={"reason": ActivationExitReason.TP1, "ltp": ltp, "tp1_price": tp1},
                )
                return True
            else:
                # Only TP level — full exit
                logger.info(
                    "TP1 hit (full) — activation=%s symbol=%s ltp=%.2f tp1=%.2f",
                    activation.id, activation.symbol, ltp, tp1,
                )
                await enter_exited(
                    activation=activation,
                    exit_price=ltp,
                    exit_reason=ActivationExitReason.TP1,
                    db=db,
                    redis=None,
                )
                return True

    if current_state == ActivationState.PARTIAL_EXIT:
        # Check TP2
        if tp2 is not None and _tp_reached(ltp, tp2, direction):
            if tp3 is not None:
                logger.info(
                    "TP2 hit (partial) — activation=%s symbol=%s",
                    activation.id, activation.symbol,
                )
                transition(
                    activation,
                    ActivationState.PARTIAL_EXIT,
                    extra={"reason": ActivationExitReason.TP2, "ltp": ltp, "tp2_price": tp2},
                )
                return True
            else:
                logger.info(
                    "TP2 hit (full) — activation=%s symbol=%s",
                    activation.id, activation.symbol,
                )
                await enter_exited(
                    activation=activation,
                    exit_price=ltp,
                    exit_reason=ActivationExitReason.FULL_TP,
                    db=db,
                    redis=None,
                )
                return True

        # Check TP3 (final)
        if tp3 is not None and _tp_reached(ltp, tp3, direction):
            logger.info(
                "TP3 hit (full) — activation=%s symbol=%s",
                activation.id, activation.symbol,
            )
            await enter_exited(
                activation=activation,
                exit_price=ltp,
                exit_reason=ActivationExitReason.TP3,
                db=db,
                redis=None,
            )
            return True

    return False


async def _read_ltp(activation: StrategyActivation, redis: Redis) -> float | None:
    key = _LTP_KEY_PREFIX + (activation.instrument_key or activation.symbol)
    try:
        raw = await redis.get(key)
        return float(raw) if raw is not None else None
    except Exception:
        return None


def _get_direction(activation: StrategyActivation) -> str:
    """Extract direction from IN_TRADE state_history entry."""
    for entry in reversed(activation.state_history):
        if entry.get("state") == ActivationState.IN_TRADE:
            return entry.get("direction", "LONG")
    return "LONG"


def _sl_breached(ltp: float, sl_price: float, direction: str) -> bool:
    if direction == "LONG":
        return ltp <= sl_price
    return ltp >= sl_price  # SHORT


def _tp_reached(ltp: float, tp_price: float, direction: str) -> bool:
    if direction == "LONG":
        return ltp >= tp_price
    return ltp <= tp_price  # SHORT
