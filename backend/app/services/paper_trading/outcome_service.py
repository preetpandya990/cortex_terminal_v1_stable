"""
Paper Trading — Outcome Service
=================================
Computes ML feedback fields on PaperTradeOutcome records after position close.

ML feedback fields (written async, may be NULL briefly after close):
  - ml_direction_correct : True if the signal direction proved correct
                           (price moved in predicted direction from entry)
  - hit_tp1/tp2/tp3      : True if price reached each take-profit level
                           between entry and exit
  - hit_sl               : True if price touched the stop-loss before exit
  - market_regime_at_entry: Fetched from ai_regime_detections at entry time

The feedback logic intentionally uses the position's exit_price as a proxy for
the final post-exit state (we don't store the full candle path post-exit, only
the exit tick).  This is conservative — the `hit_tp` fields reflect whether the
exit price reached the target, NOT whether price briefly touched it intrabar.

For the direction check, "correct" is defined as:
  BUY signal  → exit_price > entry_price (even after charges)
  SELL signal → exit_price < entry_price

The hit_sl field is set when exit_reason is 'SL' (the P&L worker triggered a
stop-loss close), OR when exit_price ≤ stop_loss for a LONG position.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_trading import PaperTradeOutcome
from app.models.trade_suggestions import TradeSuggestion

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

async def compute_ml_feedback(
    session: AsyncSession,
    outcome_id: UUID,
) -> PaperTradeOutcome:
    """
    Compute and persist ML feedback fields for a single outcome record.

    This function is designed to be called from a background task
    (e.g. a FastAPI BackgroundTask or Celery task) shortly after the
    outcome record is written.

    Raises
    ------
    ValueError  If the outcome record is not found.
    """
    stmt = select(PaperTradeOutcome).where(PaperTradeOutcome.id == outcome_id)
    outcome = (await session.execute(stmt)).scalar_one_or_none()
    if outcome is None:
        raise ValueError(f"PaperTradeOutcome {outcome_id} not found.")

    _populate_ml_fields(outcome)

    # Fetch market regime at entry time from ai_regime_detections
    regime = await _get_regime_at_entry(session, outcome)
    if regime:
        outcome.market_regime_at_entry = regime

    await session.flush()
    await session.refresh(outcome)

    logger.info(
        "ML feedback computed: outcome=%s symbol=%s direction_correct=%s "
        "hit_tp1=%s hit_sl=%s",
        outcome.id, outcome.symbol,
        outcome.ml_direction_correct, outcome.hit_tp1, outcome.hit_sl,
    )
    return outcome


async def compute_ml_feedback_batch(
    session: AsyncSession,
    outcome_ids: list[UUID],
) -> int:
    """
    Batch-compute ML feedback for multiple outcomes.

    Returns the count of successfully updated outcomes.
    Continues on individual errors (logs and skips).
    """
    updated = 0
    for oid in outcome_ids:
        try:
            await compute_ml_feedback(session, oid)
            updated += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to compute ML feedback for outcome %s: %s", oid, exc)
    return updated


async def get_outcome_stats(
    session: AsyncSession,
    portfolio_id: UUID | None = None,
    user_id: int | None = None,
) -> dict:
    """
    Compute aggregated outcome statistics for the dashboard.

    When portfolio_id is supplied, scopes to that portfolio.
    When user_id is supplied, scopes to all portfolios owned by that user.
    When neither is supplied (admin/dev), returns platform-wide stats.
    """
    from datetime import timezone
    from sqlalchemy import and_, func

    stmt = select(PaperTradeOutcome)
    if portfolio_id is not None:
        stmt = stmt.where(PaperTradeOutcome.portfolio_id == portfolio_id)
    elif user_id is not None:
        stmt = stmt.where(PaperTradeOutcome.user_id == user_id)

    result = await session.execute(stmt)
    outcomes: list[PaperTradeOutcome] = list(result.scalars().all())

    if not outcomes:
        return _empty_stats()

    total = len(outcomes)
    winners = [o for o in outcomes if o.net_pnl > _ZERO]
    losers = [o for o in outcomes if o.net_pnl <= _ZERO]

    total_gross = sum(o.gross_pnl for o in outcomes)
    total_charges = sum(o.total_charges for o in outcomes)
    total_net = sum(o.net_pnl for o in outcomes)
    avg_net = total_net / total
    avg_pnl_pct = sum(o.pnl_pct for o in outcomes) / total

    avg_hold_hours = (
        sum(o.hold_duration_seconds for o in outcomes) / total / 3600.0
    )

    # ML accuracy (exclude NULL)
    with_ml = [o for o in outcomes if o.ml_direction_correct is not None]
    ml_accuracy: float | None = (
        sum(1 for o in with_ml if o.ml_direction_correct) / len(with_ml)
        if with_ml else None
    )

    # Target / stop hit rates
    tp1_rate = sum(1 for o in outcomes if o.hit_tp1) / total
    tp2_rate = sum(1 for o in outcomes if o.hit_tp2) / total
    tp3_rate = sum(1 for o in outcomes if o.hit_tp3) / total
    sl_rate = sum(1 for o in outcomes if o.hit_sl) / total

    # Exit reason distribution
    by_exit_reason: dict[str, int] = {}
    for o in outcomes:
        by_exit_reason[o.exit_reason] = by_exit_reason.get(o.exit_reason, 0) + 1

    # Segmented breakdowns
    by_confidence = _segment_stats(outcomes, lambda o: o.suggestion_confidence_level)
    by_regime = _segment_stats(outcomes, lambda o: o.market_regime_at_entry)
    by_direction = _segment_stats(outcomes, lambda o: o.signal_direction)

    from_date = min(o.created_at for o in outcomes)
    to_date = max(o.created_at for o in outcomes)

    return {
        "total_trades": total,
        "total_winners": len(winners),
        "total_losers": len(losers),
        "win_rate": round(len(winners) / total * 100, 2) if total else None,
        "total_gross_pnl": float(total_gross),
        "total_charges": float(total_charges),
        "total_net_pnl": float(total_net),
        "avg_net_pnl_per_trade": float(avg_net),
        "avg_pnl_pct": float(avg_pnl_pct),
        "avg_hold_duration_hours": avg_hold_hours,
        "ml_direction_accuracy": ml_accuracy,
        "tp1_hit_rate": tp1_rate,
        "tp2_hit_rate": tp2_rate,
        "tp3_hit_rate": tp3_rate,
        "sl_hit_rate": sl_rate,
        "by_exit_reason": by_exit_reason,
        "by_confidence_level": by_confidence,
        "by_market_regime": by_regime,
        "by_signal_direction": by_direction,
        "from_date": from_date,
        "to_date": to_date,
        "generated_at": datetime.now(timezone.utc),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Internal
# ──────────────────────────────────────────────────────────────────────────────

def _populate_ml_fields(outcome: PaperTradeOutcome) -> None:
    """
    Compute binary ML feedback fields from outcome data.

    All computations are based on exit_price vs targets — conservative
    (we don't extrapolate post-exit candle data).
    """
    is_long = outcome.signal_direction == "BUY"
    entry = Decimal(str(outcome.entry_price))
    exit_p = Decimal(str(outcome.exit_price))

    # Direction correctness
    if is_long:
        outcome.ml_direction_correct = exit_p > entry
    else:
        outcome.ml_direction_correct = exit_p < entry

    # Stop-loss hit: triggered by SL exit_reason, or price crossed stop
    if outcome.exit_reason == "SL":
        outcome.hit_sl = True
    elif outcome.suggested_stop_loss is not None:
        sl = Decimal(str(outcome.suggested_stop_loss))
        if is_long:
            outcome.hit_sl = exit_p <= sl
        else:
            outcome.hit_sl = exit_p >= sl
    else:
        outcome.hit_sl = False

    # TP hits: check if exit price reached each target (conservative — exit price only)
    if outcome.suggested_tp1 is not None:
        tp1 = Decimal(str(outcome.suggested_tp1))
        outcome.hit_tp1 = (exit_p >= tp1) if is_long else (exit_p <= tp1)

    if outcome.suggested_tp2 is not None:
        tp2 = Decimal(str(outcome.suggested_tp2))
        outcome.hit_tp2 = (exit_p >= tp2) if is_long else (exit_p <= tp2)

    if outcome.suggested_tp3 is not None:
        tp3 = Decimal(str(outcome.suggested_tp3))
        outcome.hit_tp3 = (exit_p >= tp3) if is_long else (exit_p <= tp3)

    # TP exit_reason overrides (if user exited at TP, mark that level as hit)
    if outcome.exit_reason == "TP1":
        outcome.hit_tp1 = True
    elif outcome.exit_reason == "TP2":
        outcome.hit_tp1 = True
        outcome.hit_tp2 = True
    elif outcome.exit_reason == "TP3":
        outcome.hit_tp1 = True
        outcome.hit_tp2 = True
        outcome.hit_tp3 = True


async def _get_regime_at_entry(
    session: AsyncSession,
    outcome: PaperTradeOutcome,
) -> str | None:
    """
    Fetch the market regime that was active at the time the position was opened.

    Queries ai_regime_detections for the most recent detection for this symbol
    that was valid at or before the outcome's position entry time.

    Returns None gracefully if the table doesn't exist or has no data.
    """
    try:
        from sqlalchemy import text
        # Position opened_at is reconstructed from hold_duration and created_at
        from datetime import timedelta
        entry_time = outcome.created_at - timedelta(seconds=outcome.hold_duration_seconds)

        stmt = text(
            """
            SELECT regime
            FROM ai_regime_detections
            WHERE symbol = :symbol
              AND detected_at <= :entry_time
            ORDER BY detected_at DESC
            LIMIT 1
            """
        )
        result = await session.execute(
            stmt, {"symbol": outcome.symbol, "entry_time": entry_time}
        )
        row = result.fetchone()
        return row[0] if row else None
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "Could not fetch market regime for %s at %s: %s",
            outcome.symbol, outcome.created_at, exc,
        )
        return None


def _segment_stats(
    outcomes: list[PaperTradeOutcome],
    key_fn,
) -> dict[str, dict]:
    """Build a dict of per-segment outcome stats from a list of outcomes."""
    groups: dict[str, list[PaperTradeOutcome]] = {}
    for o in outcomes:
        k = key_fn(o)
        if k is None:
            k = "unknown"
        groups.setdefault(k, []).append(o)

    result = {}
    for seg, seg_outcomes in groups.items():
        n = len(seg_outcomes)
        winners = [o for o in seg_outcomes if o.net_pnl > _ZERO]
        with_ml = [o for o in seg_outcomes if o.ml_direction_correct is not None]
        result[seg] = {
            "count": n,
            "win_rate": round(len(winners) / n * 100, 2) if n else None,
            "avg_net_pnl": float(sum(o.net_pnl for o in seg_outcomes) / n) if n else None,
            "avg_pnl_pct": float(sum(o.pnl_pct for o in seg_outcomes) / n) if n else None,
            "ml_direction_accuracy": (
                sum(1 for o in with_ml if o.ml_direction_correct) / len(with_ml)
                if with_ml else None
            ),
        }
    return result


def _empty_stats() -> dict:
    from datetime import timezone
    return {
        "total_trades": 0,
        "total_winners": 0,
        "total_losers": 0,
        "win_rate": None,
        "total_gross_pnl": 0.0,
        "total_charges": 0.0,
        "total_net_pnl": 0.0,
        "avg_net_pnl_per_trade": None,
        "avg_pnl_pct": None,
        "avg_hold_duration_hours": None,
        "ml_direction_accuracy": None,
        "tp1_hit_rate": None,
        "tp2_hit_rate": None,
        "tp3_hit_rate": None,
        "sl_hit_rate": None,
        "by_exit_reason": {},
        "by_confidence_level": {},
        "by_market_regime": {},
        "by_signal_direction": {},
        "from_date": None,
        "to_date": None,
        "generated_at": datetime.now(timezone.utc),
    }
