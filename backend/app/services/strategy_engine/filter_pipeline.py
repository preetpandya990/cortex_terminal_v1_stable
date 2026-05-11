"""
StrategyFilterPipeline — 7-gate evaluation pipeline.

Each gate is a fast, targeted check that can eliminate a signal early.
Gates run in order; the pipeline short-circuits on the first failure
(each gate logs its result to the audit trail regardless).

Gate order (index → name):
  0  symbol_gate      — allowed_symbols / blocked_symbols whitelist+blocklist
  1  direction_gate   — allowed_directions filter (BUY / SELL)
  2  regime_gate      — allowed_regimes filter
  3  session_gate     — allowed_sessions + trading-hours calendar guard
  4  confidence_gate  — guardrails.min_confidence_score threshold
  5  signal_cond_gate — definition.signal_condition recursive tree
  6  indicator_gate   — definition.indicator_condition recursive tree

Portfolio-risk gating (max_concurrent_activations) is deferred to Phase 3
when the StrategyActivationFSM is available to count in-flight activations.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategies import Strategy, StrategyActivation
from app.schemas.strategies import StrategyDefinition
from app.services.strategy_engine.market_context import MarketContext
from app.services.strategy_engine.rule_evaluator import EvalResult, evaluate_node

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")

# NSE session windows (IST)
_SESSION_WINDOWS: dict[str, tuple[time, time]] = {
    "pre_open": (time(9, 0),  time(9, 15)),
    "morning":  (time(9, 15), time(11, 30)),
    "midday":   (time(11, 30), time(13, 0)),
    "afternoon": (time(13, 0), time(15, 30)),
    "all":      (time(9, 0),  time(15, 30)),
}


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class GateResult:
    name: str
    passed: bool
    reason: str
    elapsed_ms: float


@dataclass
class PipelineResult:
    strategy_id: UUID
    strategy_name: str
    passed: bool
    blocked_at: str | None          # name of the first gate that failed
    gates: list[GateResult] = field(default_factory=list)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": str(self.strategy_id),
            "strategy_name": self.strategy_name,
            "passed": self.passed,
            "blocked_at": self.blocked_at,
            "evaluated_at": self.evaluated_at.isoformat(),
            "gates": [
                {
                    "name": g.name,
                    "passed": g.passed,
                    "reason": g.reason,
                    "elapsed_ms": round(g.elapsed_ms, 3),
                }
                for g in self.gates
            ],
        }


# ── Pipeline ──────────────────────────────────────────────────────────────────

class StrategyFilterPipeline:
    """
    Stateful 7-gate pipeline.  One instance is safe to share across coroutines
    — it holds no mutable per-evaluation state.
    """

    async def evaluate(
        self,
        strategy: Strategy,
        ctx: MarketContext,
        db: AsyncSession | None,
        user_id: int,
        *,
        evaluation_time: datetime | None = None,
    ) -> PipelineResult:
        """
        Run all gates against the given strategy and market context.

        Args:
            strategy:         ORM Strategy row (or mock with .id/.name/.definition).
            ctx:              Resolved MarketContext snapshot.
            db:               AsyncSession (unused in current 7-gate set; reserved
                              for future portfolio-risk gate).  Pass None in backtest.
            user_id:          Authenticated user ID (reserved for portfolio gate).
            evaluation_time:  Override the wall-clock used by session_gate.
                              Pass the historical signal timestamp when backtesting
                              so session filters apply to the signal's time, not now.
                              None (default) → datetime.now() used (live trading).

        Returns:
            PipelineResult with full gate audit trail.
        """
        definition = StrategyDefinition.model_validate(strategy.definition)
        gates: list[GateResult] = []
        blocked_at: str | None = None

        gate_fns = [
            ("symbol_gate",      lambda: self._symbol_gate(ctx, definition)),
            ("direction_gate",   lambda: self._direction_gate(ctx, definition)),
            ("regime_gate",      lambda: self._regime_gate(ctx, definition)),
            ("session_gate",     lambda: self._session_gate(ctx, definition, evaluation_time)),
            ("confidence_gate",  lambda: self._confidence_gate(ctx, definition)),
            ("signal_cond_gate", lambda: self._signal_condition_gate(ctx, definition)),
            ("indicator_gate",   lambda: self._indicator_gate(ctx, definition)),
        ]

        for name, fn in gate_fns:
            t0 = _time.monotonic()
            try:
                passed, reason = fn()
            except Exception as exc:
                logger.warning("Gate '%s' raised for strategy %s: %s", name, strategy.id, exc)
                passed, reason = False, f"gate error: {exc}"
            elapsed = (_time.monotonic() - t0) * 1000.0
            gates.append(GateResult(name=name, passed=passed, reason=reason, elapsed_ms=elapsed))

            if not passed and blocked_at is None:
                blocked_at = name
                # Short-circuit — still record the gate, then stop
                break

        overall_passed = blocked_at is None
        return PipelineResult(
            strategy_id=strategy.id,
            strategy_name=strategy.name,
            passed=overall_passed,
            blocked_at=blocked_at,
            gates=gates,
        )

    # ── Gate implementations ──────────────────────────────────────────────────

    @staticmethod
    def _symbol_gate(
        ctx: MarketContext,
        defn: StrategyDefinition,
    ) -> tuple[bool, str]:
        sg = defn.signal_gates
        symbol = ctx.symbol.upper()

        blocked = {s.upper() for s in sg.blocked_symbols}
        if symbol in blocked:
            return False, f"symbol '{symbol}' is in blocked_symbols"

        if sg.allowed_symbols:
            allowed = {s.upper() for s in sg.allowed_symbols}
            if symbol not in allowed:
                return False, f"symbol '{symbol}' not in allowed_symbols ({len(allowed)} entries)"

        return True, "symbol allowed"

    @staticmethod
    def _direction_gate(
        ctx: MarketContext,
        defn: StrategyDefinition,
    ) -> tuple[bool, str]:
        if ctx.action == "HOLD":
            return False, "signal action is HOLD — no directional trade"
        allowed = {d.upper() for d in defn.signal_gates.allowed_directions}
        if ctx.action.upper() not in allowed:
            return False, f"action '{ctx.action}' not in allowed_directions {sorted(allowed)}"
        return True, f"direction '{ctx.action}' allowed"

    @staticmethod
    def _regime_gate(
        ctx: MarketContext,
        defn: StrategyDefinition,
    ) -> tuple[bool, str]:
        allowed = defn.signal_gates.allowed_regimes
        if not allowed:
            return True, "all regimes allowed (no filter)"
        allowed_lower = {r.lower() for r in allowed}
        regime = (ctx.regime_type or "unknown").lower()
        if regime in allowed_lower:
            return True, f"regime '{regime}' is in allowed_regimes"
        return False, f"regime '{regime}' not in allowed_regimes {sorted(allowed_lower)}"

    @staticmethod
    def _session_gate(
        ctx: MarketContext,
        defn: StrategyDefinition,
        evaluation_time: datetime | None = None,
    ) -> tuple[bool, str]:
        allowed_sessions = defn.guardrails.allowed_sessions
        if not allowed_sessions:
            return True, "all sessions allowed (no filter)"

        ref_ts = (evaluation_time or datetime.now(timezone.utc)).astimezone(_IST)
        now_ist = ref_ts.time()
        current_sessions: list[str] = []
        for session_name, (start, end) in _SESSION_WINDOWS.items():
            if start <= now_ist < end:
                current_sessions.append(session_name)

        if not current_sessions:
            return False, "outside all NSE trading sessions"

        allowed_set = {s.lower() for s in allowed_sessions}
        matched = [s for s in current_sessions if s in allowed_set]
        if matched:
            return True, f"current session(s) {matched} match allowed_sessions"
        return False, f"current sessions {current_sessions} not in allowed_sessions {sorted(allowed_set)}"

    @staticmethod
    def _confidence_gate(
        ctx: MarketContext,
        defn: StrategyDefinition,
    ) -> tuple[bool, str]:
        threshold = defn.guardrails.min_confidence_score  # 0–100 scale
        actual_pct = ctx.confidence_score_pct
        if actual_pct >= threshold:
            return True, f"confidence {actual_pct:.1f}% >= threshold {threshold:.1f}%"
        return False, f"confidence {actual_pct:.1f}% < threshold {threshold:.1f}%"

    @staticmethod
    def _signal_condition_gate(
        ctx: MarketContext,
        defn: StrategyDefinition,
    ) -> tuple[bool, str]:
        cond = defn.signal_condition
        if cond is None:
            return True, "no signal_condition defined (pass-through)"
        result: EvalResult = evaluate_node(cond, ctx.as_dict())
        return result.passed, result.reason

    @staticmethod
    def _indicator_gate(
        ctx: MarketContext,
        defn: StrategyDefinition,
    ) -> tuple[bool, str]:
        cond = defn.indicator_condition
        if cond is None:
            return True, "no indicator_condition defined (pass-through)"
        result: EvalResult = evaluate_node(cond, ctx.as_dict())
        return result.passed, result.reason


# Module-level singleton — stateless, safe to share across all coroutines.
strategy_filter_pipeline = StrategyFilterPipeline()
