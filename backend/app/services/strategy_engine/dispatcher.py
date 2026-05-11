"""
StrategyDispatcher — Per-user signal fanout with enforcement-mode routing.

For every AITradingSignal that clears the assembler, this component:

  1. Loads all active user subscriptions for the signal's symbol (or global).
  2. Groups subscriptions by user in ascending priority order.
  3. For each user, evaluates their subscribed strategies through the
     StrategyFilterPipeline.
  4. Applies the user's enforcement mode:

     hard_gate     — the signal is suppressed for this user unless at least
                     one strategy passes.  This gate must NOT block signal
                     delivery to other (soft_filter / portfolio_opt_in) users.
     soft_filter   — the signal is always delivered; matched strategies are
                     attached as context so the UI can show which rules fired.
     portfolio_opt_in — only portfolios that have opted in to a matched strategy
                     receive the signal (wired in Phase 3 with portfolio scoping).

  5. Returns a per-user list of DispatchResult for callers to consume
     (e.g. WebSocket broadcast gating, activation creation in Phase 3).

Design notes
------------
• The dispatcher is fired as a fire-and-forget asyncio.create_task() from
  SignalAssembler.assemble_signal() so it never blocks the critical path.
• It creates its own DB session (via AsyncSessionLocal) to be completely
  decoupled from the assembler's session lifecycle.
• All exceptions are caught and logged — a dispatcher failure must never
  propagate to the signal assembly path.
• Results are published to Redis (`cai:strategy:dispatch:{signal_id}`) with a
  short TTL so downstream consumers (Phase 3 activation worker) can pick them up.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategies import Strategy, StrategyActivation, UserStrategySubscription
from app.models.user import User
from app.services.strategy_engine.filter_pipeline import PipelineResult, strategy_filter_pipeline
from app.services.strategy_engine.market_context import MarketContext, resolve_market_context

logger = logging.getLogger(__name__)

# Redis TTL for dispatch result cache (used by SL/TP worker)
_DISPATCH_RESULT_TTL_S: int = 300  # 5 minutes


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class UserDispatchResult:
    """Outcome for a single user after running all their subscribed strategies."""
    user_id: int
    enforcement_mode: str
    signal_allowed: bool               # False only for hard_gate with no match
    matched_strategy_ids: list[UUID]
    pipeline_results: list[PipelineResult] = field(default_factory=list)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "enforcement_mode": self.enforcement_mode,
            "signal_allowed": self.signal_allowed,
            "matched_strategy_ids": [str(sid) for sid in self.matched_strategy_ids],
            "evaluated_at": self.evaluated_at.isoformat(),
            "pipeline_results": [r.to_dict() for r in self.pipeline_results],
        }


@dataclass
class DispatchSummary:
    """Aggregate summary across all users, stored in Redis for Phase 3."""
    signal_id: int
    symbol: str
    total_users_evaluated: int
    total_users_matched: int
    total_strategies_matched: int
    user_results: list[UserDispatchResult]
    dispatched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "symbol": self.symbol,
            "total_users_evaluated": self.total_users_evaluated,
            "total_users_matched": self.total_users_matched,
            "total_strategies_matched": self.total_strategies_matched,
            "dispatched_at": self.dispatched_at.isoformat(),
            "user_results": [r.to_dict() for r in self.user_results],
        }


# ── Dispatcher ────────────────────────────────────────────────────────────────

class StrategyDispatcher:
    """
    Evaluates a signal against all subscribed strategies for all users.
    Thread-safe — holds no mutable state; safe to share across coroutines.
    """

    async def dispatch(
        self,
        *,
        signal_id: int,
        symbol: str,
        action: str,
        confidence_score: float,
        regime_type: str,
        time_horizon: str,
        technical_indicators: dict[str, Any],
        ml_predictions: dict[str, Any],
        instrument_key: str | None = None,
        db: AsyncSession,
        redis: Redis | None = None,
    ) -> DispatchSummary:
        """
        Run the full per-user strategy evaluation for a signal.

        Args:
            signal_id:            ID of the persisted AITradingSignal.
            symbol:               NSE trading symbol.
            action:               "BUY" | "SELL" | "HOLD"
            confidence_score:     Fused confidence 0.0–1.0.
            regime_type:          Market regime string.
            time_horizon:         "intraday" | "swing" | "positional"
            technical_indicators: JSONB from AITradingSignal.
            ml_predictions:       JSONB from AITradingSignal.
            instrument_key:       Upstox key for Redis LTP lookup (optional).
            db:                   Database session.
            redis:                Redis client (optional; LTP skipped if None).

        Returns:
            DispatchSummary with per-user pipeline results.
        """
        ctx = await resolve_market_context(
            symbol=symbol,
            action=action,
            confidence_score=confidence_score,
            regime_type=regime_type,
            time_horizon=time_horizon,
            technical_indicators=technical_indicators,
            ml_predictions=ml_predictions,
            instrument_key=instrument_key,
            redis=redis,
        )

        # Load all active subscriptions with their strategy and user preferences.
        # Single JOIN query — avoids N+1 on user/strategy lookups.
        rows = await self._load_subscriptions(symbol, db)

        # Group by user_id, preserving priority order (query is already ordered).
        grouped: dict[int, list[tuple[UserStrategySubscription, Strategy, str]]] = {}
        for sub, strategy, enforcement_mode in rows:
            grouped.setdefault(sub.user_id, []).append((sub, strategy, enforcement_mode))

        user_results: list[UserDispatchResult] = []

        # Evaluate strategies for each user concurrently.
        tasks = [
            self._evaluate_user(
                user_id=uid,
                subs=subs_list,
                ctx=ctx,
                signal_id=signal_id,
                db=db,
            )
            for uid, subs_list in grouped.items()
        ]
        if tasks:
            user_results = list(await asyncio.gather(*tasks, return_exceptions=False))

        matched_users = sum(1 for r in user_results if r.signal_allowed)
        total_matched_strategies = sum(
            len(r.matched_strategy_ids) for r in user_results
        )

        summary = DispatchSummary(
            signal_id=signal_id,
            symbol=symbol,
            total_users_evaluated=len(user_results),
            total_users_matched=matched_users,
            total_strategies_matched=total_matched_strategies,
            user_results=user_results,
        )

        # Persist dispatch summary to Redis for Phase 3 activation worker.
        if redis is not None:
            await self._cache_summary(signal_id, summary, redis)

        logger.info(
            "Strategy dispatch — signal_id=%d symbol=%s users=%d matched=%d strategies=%d",
            signal_id,
            symbol,
            len(user_results),
            matched_users,
            total_matched_strategies,
        )

        return summary

    async def dispatch_background(
        self,
        signal_id: int,
        symbol: str,
        action: str,
        confidence_score: float,
        regime_type: str,
        time_horizon: str,
        technical_indicators: dict[str, Any],
        ml_predictions: dict[str, Any],
        instrument_key: str | None = None,
        redis: Redis | None = None,
    ) -> None:
        """
        Self-contained background variant that creates its own DB session.

        Called via asyncio.create_task() from SignalAssembler so the assembler's
        session lifecycle is completely decoupled.  All exceptions are caught.
        """
        from app.core.database import AsyncSessionLocal

        try:
            async with AsyncSessionLocal() as db:
                await self.dispatch(
                    signal_id=signal_id,
                    symbol=symbol,
                    action=action,
                    confidence_score=confidence_score,
                    regime_type=regime_type,
                    time_horizon=time_horizon,
                    technical_indicators=technical_indicators,
                    ml_predictions=ml_predictions,
                    instrument_key=instrument_key,
                    db=db,
                    redis=redis,
                )
                await db.commit()
        except Exception as exc:
            logger.error(
                "Strategy dispatch background task failed — signal_id=%d symbol=%s: %s",
                signal_id,
                symbol,
                exc,
                exc_info=True,
            )

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    async def _load_subscriptions(
        symbol: str,
        db: AsyncSession,
    ) -> list[tuple[UserStrategySubscription, Strategy, str]]:
        """
        Load all active subscriptions that could match this symbol.

        Returns rows of (subscription, strategy, enforcement_mode) ordered by
        (user_id, priority ASC) so per-user evaluation preserves priority order.
        """
        from app.models.strategies import UserPreferences

        stmt = (
            select(
                UserStrategySubscription,
                Strategy,
                UserPreferences.enforcement_mode,
            )
            .join(Strategy, Strategy.id == UserStrategySubscription.strategy_id)
            .outerjoin(
                UserPreferences,
                UserPreferences.user_id == UserStrategySubscription.user_id,
            )
            .where(
                and_(
                    UserStrategySubscription.is_active == True,  # noqa: E712
                    Strategy.status == "active",
                )
            )
            .order_by(
                UserStrategySubscription.user_id,
                UserStrategySubscription.priority.asc(),
            )
        )

        result = await db.execute(stmt)
        rows = result.all()

        return [
            (
                sub,
                strategy,
                # Fall back to "soft_filter" when preferences row is absent
                enforcement_mode if enforcement_mode else "soft_filter",
            )
            for sub, strategy, enforcement_mode in rows
        ]

    @staticmethod
    async def _resolve_instrument_key(symbol: str, db: AsyncSession) -> str | None:
        """Resolve NSE trading_symbol → instrument_key from instrument_master."""
        from sqlalchemy import text as sa_text
        try:
            result = await db.execute(
                sa_text(
                    "SELECT instrument_key FROM instrument_master "
                    "WHERE trading_symbol = :sym AND exchange = 'NSE' "
                    "AND (instrument_type = 'EQ' OR instrument_type IS NULL) LIMIT 1"
                ),
                {"sym": symbol},
            )
            row = result.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    async def _evaluate_user(
        self,
        user_id: int,
        subs: list[tuple[UserStrategySubscription, Strategy, str]],
        ctx: MarketContext,
        db: AsyncSession,
        signal_id: int | None = None,
    ) -> UserDispatchResult:
        """
        Evaluate all subscribed strategies for a single user and apply
        enforcement mode logic.
        """
        enforcement_mode = subs[0][2] if subs else "soft_filter"
        pipeline_results: list[PipelineResult] = []
        matched_ids: list[UUID] = []

        for sub, strategy, _ in subs:
            try:
                result = await strategy_filter_pipeline.evaluate(
                    strategy=strategy,
                    ctx=ctx,
                    db=db,
                    user_id=user_id,
                )
                pipeline_results.append(result)
                if result.passed:
                    matched_ids.append(strategy.id)
                    await self._maybe_create_activation(
                        user_id=user_id,
                        strategy=strategy,
                        ctx=ctx,
                        signal_id=signal_id,
                        db=db,
                    )
            except Exception as exc:
                logger.warning(
                    "Pipeline evaluation failed — user=%d strategy=%s: %s",
                    user_id,
                    strategy.id,
                    exc,
                )

        # Enforcement mode routing
        if enforcement_mode == "hard_gate":
            signal_allowed = len(matched_ids) > 0
        else:
            signal_allowed = True

        return UserDispatchResult(
            user_id=user_id,
            enforcement_mode=enforcement_mode,
            signal_allowed=signal_allowed,
            matched_strategy_ids=matched_ids,
            pipeline_results=pipeline_results,
        )

    async def _maybe_create_activation(
        self,
        *,
        user_id: int,
        strategy: Strategy,
        ctx: MarketContext,
        signal_id: int | None,
        db: AsyncSession,
    ) -> None:
        """
        Create a WATCHING activation for a passed pipeline result, then
        immediately advance to IN_TRADE if auto_execute=True.

        Idempotent: skips creation when an active (non-terminal) activation
        already exists for this user+strategy+symbol combination.
        """
        from app.services.strategy_engine.activation_fsm import (
            create_activation,
            enter_in_trade,
        )

        try:
            # Guard: one active activation per user+strategy+symbol
            existing_stmt = (
                select(StrategyActivation.id)
                .where(
                    and_(
                        StrategyActivation.user_id == user_id,
                        StrategyActivation.strategy_id == strategy.id,
                        StrategyActivation.symbol == ctx.symbol,
                        StrategyActivation.state.not_in(("EXITED", "CANCELLED")),
                    )
                )
                .limit(1)
            )
            existing = (await db.execute(existing_stmt)).scalar_one_or_none()
            if existing is not None:
                return

            instrument_key = await self._resolve_instrument_key(ctx.symbol, db)

            activation = await create_activation(
                user_id=user_id,
                strategy_id=strategy.id,
                signal_id=signal_id,
                symbol=ctx.symbol,
                instrument_key=instrument_key,
                strategy_snapshot=strategy.definition,
                auto_execute=False,  # Phase 3: auto-execute wired in next iteration
                db=db,
            )

            # If the user configured auto_execute and the signal has a price, enter trade
            defn_auto = strategy.definition.get("auto_execute", False) if isinstance(strategy.definition, dict) else False
            entry_p = ctx.entry_price or ctx.ltp
            if defn_auto and entry_p is not None:
                activation.auto_execute = True
                # Redis not available at this point in dispatch; skip auto place for now
                # The SL/TP worker will pick up WATCHING activations and advance them

            await db.flush()

            logger.info(
                "Activation created — user=%d strategy=%s symbol=%s state=WATCHING",
                user_id,
                strategy.id,
                ctx.symbol,
            )
        except Exception as exc:
            logger.warning(
                "Failed to create activation — user=%d strategy=%s: %s",
                user_id,
                strategy.id,
                exc,
            )

    @staticmethod
    async def _cache_summary(
        signal_id: int,
        summary: DispatchSummary,
        redis: Redis,
    ) -> None:
        try:
            key = f"cai:strategy:dispatch:{signal_id}"
            payload = json.dumps(summary.to_dict(), default=str)
            await redis.setex(key, _DISPATCH_RESULT_TTL_S, payload)
        except Exception as exc:
            logger.debug("Failed to cache dispatch summary for signal_id=%d: %s", signal_id, exc)


# Module-level singleton
strategy_dispatcher = StrategyDispatcher()
