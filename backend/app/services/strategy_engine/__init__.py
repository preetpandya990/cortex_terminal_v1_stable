"""
Strategy Rule Engine
=====================
Evaluates user-defined trading strategies against assembled AI/ML signals.

Components
----------
market_context   — Resolves an AITradingSignal + Redis LTP into a flat,
                   evaluable MarketContext dataclass.
rule_evaluator   — Pure recursive evaluation of ConditionNode trees against
                   a MarketContext dict.  No I/O — deterministic and testable.
filter_pipeline  — Stateful 7-gate pipeline that applies a StrategyDefinition
                   to a MarketContext, producing a full GateResult audit trail.
dispatcher       — Per-user fanout: loads subscriptions, runs the pipeline for
                   every active strategy, routes results by enforcement mode.
"""
from app.services.strategy_engine.dispatcher import StrategyDispatcher, strategy_dispatcher
from app.services.strategy_engine.backtest_engine import BacktestEngine, backtest_engine
from app.services.strategy_engine.backtest_runner import run_backtest_task

__all__ = [
    "StrategyDispatcher",
    "strategy_dispatcher",
    "BacktestEngine",
    "backtest_engine",
    "run_backtest_task",
]
