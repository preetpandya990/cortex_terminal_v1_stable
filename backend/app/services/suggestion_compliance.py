"""
SuggestionComplianceService
============================
Evaluates every new TradeSuggestion against all subscribed users' active
strategies and writes per-(suggestion, user, strategy) compliance rows to
user_suggestion_compliance.

Called inline by EventCorrelationEngine._compute_consensus() immediately
after a suggestion is committed.  The caller is responsible for issuing the
final db.commit() to flush compliance rows.

Design decisions:
- One DB round-trip fetches all (user, strategy) pairs via JOIN — avoids N+1.
- Pure-Python pipeline evaluation — no additional DB access per strategy.
- ON CONFLICT DO NOTHING via INSERT ... ON CONFLICT — worker re-runs are safe.
- Failures are caught and logged; they never prevent the suggestion from being
  returned or published to Redis.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategies import Strategy, UserStrategySubscription
from app.models.trade_suggestions import TradeSuggestion, UserSuggestionCompliance
from app.services.strategy_engine.filter_pipeline import strategy_filter_pipeline
from app.services.strategy_engine.market_context import MarketContext

logger = logging.getLogger(__name__)


# ── Context builder ───────────────────────────────────────────────────────────

def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return None if (f != f) else f  # reject NaN
    except (TypeError, ValueError):
        return None


def build_market_context_from_suggestion(suggestion: TradeSuggestion) -> MarketContext:
    """
    Derive a MarketContext snapshot from a TradeSuggestion for strategy
    pipeline evaluation.

    Field mapping:
        action           ← signal_direction  ("BUY" | "SELL")
        confidence_score ← consensus_score / 100.0
        regime_type      ← suggestion.regime_type (set at generation time)
        time_horizon     ← suggestion.time_horizon (set at generation time)
        rsi_14           ← scanner_signal.indicators.rsi / rsi_14
        ema_20/50        ← scanner_signal.indicators.ema_20/50
        ema_crossover    ← scanner_signal.indicators.ema_crossover
        ml_*             ← ml_signal.prediction.*
        entry/sl/tp*     ← TradeSuggestion trade columns (Decimal → float)
    """
    scanner: dict[str, Any] = suggestion.scanner_signal or {}
    ml: dict[str, Any] = suggestion.ml_signal or {}

    action = suggestion.signal_direction          # "BUY" | "SELL"
    consensus_pct = float(suggestion.consensus_score)
    confidence_score = consensus_pct / 100.0

    # Technical indicators — stored as a nested dict inside scanner_signal
    raw_indicators: dict[str, Any] = (
        scanner.get("indicators")
        or scanner.get("technical")
        or {}
    )
    rsi_14 = _safe_float(
        raw_indicators.get("rsi_14")
        or raw_indicators.get("rsi")
        or scanner.get("rsi")
    )
    ema_20 = _safe_float(raw_indicators.get("ema_20"))
    ema_50 = _safe_float(raw_indicators.get("ema_50"))
    ema_crossover: str | None = raw_indicators.get("ema_crossover") or None

    # ML prediction fields
    prediction: dict[str, Any] = ml.get("prediction") or {}
    ml_direction: str | None = prediction.get("direction") or ml.get("direction") or None
    ml_confidence = _safe_float(ml.get("confidence"))
    ml_targets: list = prediction.get("targets") or []

    entry_price = _safe_float(
        prediction.get("entry_price") or suggestion.entry_price
    )
    stop_loss = _safe_float(
        prediction.get("stop_loss") or suggestion.stop_loss
    )
    take_profit_1 = _safe_float(ml_targets[0] if len(ml_targets) > 0 else suggestion.take_profit_1)
    take_profit_2 = _safe_float(ml_targets[1] if len(ml_targets) > 1 else suggestion.take_profit_2)
    take_profit_3 = _safe_float(ml_targets[2] if len(ml_targets) > 2 else suggestion.take_profit_3)

    return MarketContext(
        symbol=suggestion.symbol,
        action=action,
        confidence_score=confidence_score,
        confidence_score_pct=round(confidence_score * 100, 2),
        regime_type=suggestion.regime_type or "unknown",
        time_horizon=suggestion.time_horizon or "intraday",
        rsi_14=rsi_14,
        ema_20=ema_20,
        ema_50=ema_50,
        ema_crossover=ema_crossover,
        ml_direction=ml_direction,
        ml_confidence=ml_confidence,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        take_profit_3=take_profit_3,
        ltp=None,
        ltp_age_seconds=None,
        ltp_stale=False,
    )


# ── Service ───────────────────────────────────────────────────────────────────

class SuggestionComplianceService:
    """
    Evaluates a TradeSuggestion against every subscribed user's active
    strategies and persists compliance rows.

    One instance is safe to share across the entire worker process — it holds
    no mutable per-evaluation state.
    """

    async def evaluate_and_persist(
        self,
        db: AsyncSession,
        suggestion: TradeSuggestion,
    ) -> int:
        """
        Run all active strategies for all subscribed users against the
        suggestion and bulk-INSERT compliance rows.

        Args:
            db:         Open AsyncSession; caller commits after this returns.
            suggestion: The freshly committed TradeSuggestion to evaluate.

        Returns:
            Number of compliance rows inserted (0 if no subscribed users).
        """
        # Single JOIN query: all (user, strategy) pairs in priority order.
        # Filters: subscription is_active=True, strategy status='active'.
        stmt = (
            select(
                UserStrategySubscription.user_id,
                UserStrategySubscription.priority,
                Strategy,
            )
            .join(Strategy, Strategy.id == UserStrategySubscription.strategy_id)
            .where(
                UserStrategySubscription.is_active.is_(True),
                Strategy.status == "active",
            )
            .order_by(
                UserStrategySubscription.user_id,
                UserStrategySubscription.priority,
            )
        )
        result = await db.execute(stmt)
        rows = result.all()

        if not rows:
            return 0

        ctx = build_market_context_from_suggestion(suggestion)
        now = datetime.now(timezone.utc)

        compliance_values: list[dict[str, Any]] = []
        for user_id, priority, strategy in rows:
            try:
                pipeline_result = await strategy_filter_pipeline.evaluate(
                    strategy=strategy,
                    ctx=ctx,
                    db=None,       # portfolio-risk gate deferred
                    user_id=user_id,
                )
                compliance_values.append({
                    "suggestion_id": suggestion.suggestion_id,
                    "user_id": user_id,
                    "strategy_id": strategy.id,
                    "passed": pipeline_result.passed,
                    "pipeline_result": pipeline_result.to_dict(),
                    "evaluated_at": now,
                })
            except Exception as exc:
                logger.warning(
                    "Strategy pipeline evaluation failed — skipping "
                    "(suggestion=%s, user=%s, strategy=%s): %s",
                    suggestion.suggestion_id, user_id, strategy.id, exc,
                )

        if not compliance_values:
            return 0

        # Bulk INSERT with ON CONFLICT DO NOTHING — idempotent; worker
        # re-runs cannot create duplicate compliance rows.
        await db.execute(
            insert(UserSuggestionCompliance)
            .values(compliance_values)
            .on_conflict_do_nothing(
                index_elements=["suggestion_id", "user_id", "strategy_id"]
            )
        )

        logger.debug(
            "Wrote %d compliance rows for suggestion %s",
            len(compliance_values),
            suggestion.suggestion_id,
        )
        return len(compliance_values)


    async def backfill_for_user(
        self,
        db: AsyncSession,
        user_id: int,
        strategy_id: UUID,
    ) -> int:
        """
        Evaluate all active TradeSuggestions for a newly-subscribed (user, strategy)
        pair and write compliance rows.  Called immediately after subscribe so the
        user sees badges on pre-existing suggestions without waiting for a new one.

        ON CONFLICT DO NOTHING keeps this idempotent — safe to call repeatedly.
        """
        # Load the strategy
        strategy_result = await db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                Strategy.status == "active",
            )
        )
        strategy = strategy_result.scalar_one_or_none()
        if not strategy:
            return 0

        # All currently active suggestions
        suggestions_result = await db.execute(
            select(TradeSuggestion).where(TradeSuggestion.status == "active")
        )
        suggestions = suggestions_result.scalars().all()
        if not suggestions:
            return 0

        now = datetime.now(timezone.utc)
        compliance_values: list[dict[str, Any]] = []

        for suggestion in suggestions:
            try:
                ctx = build_market_context_from_suggestion(suggestion)
                pipeline_result = await strategy_filter_pipeline.evaluate(
                    strategy=strategy,
                    ctx=ctx,
                    db=None,
                    user_id=user_id,
                )
                compliance_values.append({
                    "suggestion_id": suggestion.suggestion_id,
                    "user_id": user_id,
                    "strategy_id": strategy_id,
                    "passed": pipeline_result.passed,
                    "pipeline_result": pipeline_result.to_dict(),
                    "evaluated_at": now,
                })
            except Exception as exc:
                logger.warning(
                    "Backfill compliance evaluation failed (user=%s, strategy=%s, suggestion=%s): %s",
                    user_id, strategy_id, suggestion.suggestion_id, exc,
                )

        if not compliance_values:
            return 0

        await db.execute(
            insert(UserSuggestionCompliance)
            .values(compliance_values)
            .on_conflict_do_nothing(
                index_elements=["suggestion_id", "user_id", "strategy_id"]
            )
        )

        logger.info(
            "Backfilled %d compliance rows for user=%s strategy=%s",
            len(compliance_values), user_id, strategy_id,
        )
        return len(compliance_values)


# Module-level singleton — stateless, safe to share across all coroutines.
suggestion_compliance_service = SuggestionComplianceService()
