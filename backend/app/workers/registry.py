"""
Cortex AI — Worker Task Registry
=================================

Single source of truth for all 18 background tasks that run inside the worker
sidecar.  Each entry is a zero-argument factory (a closure) so that:

  - The supervisor can call factory() to obtain a *fresh* coroutine on restart
    without holding stale database sessions or closed connections.
  - All dependencies are injected at build time via the session_factory, redis,
    ml, and upstox arguments — the factories themselves are pure callables.

Task inventory
──────────────
  Native (6)  — receive PauseToken + TriggerToken so the control plane can
                pause, resume, and trigger them remotely:
    heartbeat              Writes worker:heartbeat to Redis every 30s (liveness).
    cache_invalidation     Pub/sub listener; invalidates suggestion list caches.
    suggestion_expiry      Batch-marks expired TradeSuggestions every 60s.
    correlation_engine     Bidirectional scanner→AI + news→AI consensus engine.
    fundamentals_refresh   Six-sub-loop fundamentals scheduler (see below).
    watchlist_scheduler    Pre-warms AI context for watchlist instruments 4×/day.
    ai_processing_safety_net  Daily fallback dispatch for the 3 demand-driven
                              Tier-2 Gemini queues (sentiment/forecast/classification).

  Imported (10) — respond to CancelledError only:
    rss_ingestion       RSS news feed ingestion.
    event_processing    ML event classification pipeline.
    regime_detection    Post-market regime labelling.
    drift_detection     Model drift monitoring.
    safety_monitoring   Portfolio risk safety checks.
    data_ingestion      OHLCV historical / live gap-fill.
    feature_refresh     Daily ML feature store refresh at 16:00 IST.
    rag_cleanup         Daily TTL eviction of stale RAG embeddings (>7 days).
    pnl_worker          Paper trading P&L recompute (migrated from main.py).
    sl_tp_worker        Strategy SL/TP FSM monitor (migrated from main.py).

  fundamentals_refresh control-plane semantics:
    pause   — all six sub-loops block at their next checkpoint() call.
    trigger — all six sub-loops wake from their current sleep; each evaluates
              its own guard conditions and runs only if its schedule is due.
    restart — CancelledError cancels all sub-tasks; supervisor restarts.

Usage:
    from app.workers.registry import build_task_registry, TASK_NAMES

    states   = create_task_states(list(TASK_NAMES))
    registry = build_task_registry(session_factory, redis, ml, upstox,
                                   shutdown_event, states)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Coroutine

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services.upstox_client import UpstoxClient
from app.workers.supervisor import TaskState

logger = logging.getLogger(__name__)

# Canonical ordered list of task names.  The order here is cosmetic; the
# supervisor starts all tasks concurrently inside a TaskGroup.
TASK_NAMES: tuple[str, ...] = (
    # ── Native loops (pause/trigger-aware) ───────────────────────────────────
    "heartbeat",
    "cache_invalidation",
    "suggestion_expiry",
    "correlation_engine",
    # ── Imported loops (CancelledError-only shutdown) ─────────────────────────
    "rss_ingestion",
    "event_processing",
    "regime_detection",
    "drift_detection",
    "safety_monitoring",
    "data_ingestion",
    "fundamentals_refresh",
    "feature_refresh",
    "rag_cleanup",
    # ── Migrated from main.py ────────────────────────────────────────────────
    "pnl_worker",
    "sl_tp_worker",
    # ── Watchlist pre-warmer (pause/trigger-aware) ────────────────────────────
    "watchlist_scheduler",
    # ── Async batch news forecaster ───────────────────────────────────────────
    # Drains cortex:forecast:batch:queue; fires one Gemini call per batch of
    # symbols (≤NEWS_FORECAST_BATCH_SIZE) rather than one call per signal-assembly
    # hot path.  Results are written to the same 5-min forecast cache keys.
    "forecast_batch",
    # ── Demand-driven AI processing safety net (pause/trigger-aware) ─────────
    # Daily 09:00 IST fallback sweep — dispatches sentiment/forecast/
    # classification queues independently when any crosses its threshold.
    "ai_processing_safety_net",
)


def build_task_registry(
    session_factory: async_sessionmaker,
    redis_client,
    ml_components: dict,
    upstox_client: UpstoxClient,
    shutdown: asyncio.Event,
    task_states: dict[str, TaskState],
) -> dict[str, Callable[[], Coroutine]]:
    """
    Build and return the full task registry.

    Each value is a zero-argument callable that returns a fresh coroutine.
    The supervisor calls the factory on initial start and on every restart
    after a crash, so closures must not capture mutable state that becomes
    invalid after an exception (e.g. open DB sessions).

    Args:
        session_factory:  Worker's async session factory (WorkerSessionLocal).
        redis_client:     Initialized CacheService instance.
        ml_components:    Dict of loaded ML predictor + metadata.
        upstox_client:    Authenticated Upstox REST/WS client.
        shutdown:         Shared shutdown asyncio.Event.
        task_states:      Per-task state objects (provides pause/trigger tokens).

    Returns:
        Mapping of task name → coro factory, preserving TASK_NAMES order.
    """
    # Lazy imports keep module-level import time fast and avoid circular deps.
    from app.ai.ingestion.rss_fetcher import rss_ingestion_loop
    from app.workers.rag_cleanup import rag_cleanup_loop
    from app.ai.intelligence.event_processor import event_processing_loop
    from app.ai.safety.safety_trigger_engine import safety_monitoring_loop
    from app.ai.strategy.regime_detector import regime_detection_loop
    from app.ml.monitoring.drift_scheduler import drift_detection_loop
    from app.services.data_ingestion_worker import data_ingestion_loop
    from app.services.fundamentals_refresh import FundamentalsRefreshScheduler
    from app.services.paper_trading.pnl_worker import run_pnl_worker
    from app.services.strategy_engine.sl_tp_worker import run_sl_tp_worker
    from app.worker import (
        cache_invalidation_loop,
        correlation_loop,
        expiry_loop,
        feature_refresh_loop,
        heartbeat_loop,
    )

    def _state(name: str) -> TaskState:
        return task_states[name]

    # FundamentalsRefreshScheduler is instantiated once; the factory calls
    # .run() to get a fresh coroutine on each supervisor restart so stale
    # sessions / connections are never reused.  PauseToken and TriggerToken
    # are wired from TaskState so the control plane can pause, resume, and
    # trigger all six sub-loops via the standard /tasks/{name}/* endpoints.
    fundamentals_scheduler = FundamentalsRefreshScheduler(
        session_factory=session_factory,
        redis=redis_client,
        shutdown=shutdown,
        pause=_state("fundamentals_refresh").pause_token,
        trigger=_state("fundamentals_refresh").trigger_token,
    )

    from app.workers.watchlist_context_scheduler import WatchlistContextScheduler
    from app.ai.fusion.forecast_batch_worker import forecast_batch_loop
    watchlist_scheduler_instance = WatchlistContextScheduler(
        session_factory=session_factory,
        redis=redis_client._redis,
        shutdown=shutdown,
        pause=_state("watchlist_scheduler").pause_token,
        trigger=_state("watchlist_scheduler").trigger_token,
    )

    from app.workers.ai_processing_safety_net import AIProcessingSafetyNet
    ai_processing_safety_net_instance = AIProcessingSafetyNet(
        session_factory=session_factory,
        redis=redis_client._redis,
        shutdown=shutdown,
        pause=_state("ai_processing_safety_net").pause_token,
        trigger=_state("ai_processing_safety_net").trigger_token,
    )

    registry: dict[str, Callable[[], Coroutine]] = {
        # ── Native loops — pause/trigger tokens injected from TaskState ──────
        "heartbeat": lambda: heartbeat_loop(
            pause=_state("heartbeat").pause_token,
            trigger=_state("heartbeat").trigger_token,
            shutdown=shutdown,
        ),
        "cache_invalidation": lambda: cache_invalidation_loop(
            redis_client=redis_client,
            pause=_state("cache_invalidation").pause_token,
            trigger=_state("cache_invalidation").trigger_token,
            shutdown=shutdown,
        ),
        "suggestion_expiry": lambda: expiry_loop(
            session_factory=session_factory,
            redis_client=redis_client,
            pause=_state("suggestion_expiry").pause_token,
            trigger=_state("suggestion_expiry").trigger_token,
            shutdown=shutdown,
        ),
        "correlation_engine": lambda: correlation_loop(
            session_factory=session_factory,
            redis_client=redis_client,
            ml_components=ml_components,
            upstox_client=upstox_client,
            pause=_state("correlation_engine").pause_token,
            trigger=_state("correlation_engine").trigger_token,
            shutdown=shutdown,
        ),
        # ── Fundamentals — pause/trigger-aware via FundamentalsRefreshScheduler
        "fundamentals_refresh": lambda: fundamentals_scheduler.run(),
        # ── Imported loops — respond to CancelledError from the supervisor ────
        "rss_ingestion": lambda: rss_ingestion_loop(session_factory),
        "event_processing": lambda: event_processing_loop(
            session_factory,
            ml_components=ml_components,
            redis=redis_client._redis,
        ),
        "regime_detection": lambda: regime_detection_loop(session_factory),
        "drift_detection": lambda: drift_detection_loop(session_factory),
        "safety_monitoring": lambda: safety_monitoring_loop(session_factory, redis_client._redis),
        "data_ingestion": lambda: data_ingestion_loop(session_factory, upstox_client),
        "feature_refresh": lambda: feature_refresh_loop(shutdown=shutdown),
        "rag_cleanup": lambda: rag_cleanup_loop(
            session_factory=session_factory,
            redis=redis_client,
            shutdown=shutdown,
        ),
        # ── Migrated from main.py ─────────────────────────────────────────────
        # pnl_worker uses WorkerSessionLocal internally — correct pool.
        # sl_tp_worker was updated to use WorkerSessionLocal (was AsyncSessionLocal).
        "pnl_worker": lambda: run_pnl_worker(redis_client._redis),
        "sl_tp_worker": lambda: run_sl_tp_worker(redis_client._redis),
        # ── Watchlist pre-warmer — pause/trigger-aware via WatchlistContextScheduler
        "watchlist_scheduler": lambda: watchlist_scheduler_instance.run(),
        # ── Async batch news forecaster ───────────────────────────────────────
        "forecast_batch": lambda: forecast_batch_loop(
            redis=redis_client._redis,
            session_factory=session_factory,
            shutdown=shutdown,
        ),
        # ── Demand-driven AI processing safety net — pause/trigger-aware ──────
        "ai_processing_safety_net": lambda: ai_processing_safety_net_instance.run(),
    }

    if set(registry.keys()) != set(TASK_NAMES):
        missing = set(TASK_NAMES) - set(registry.keys())
        extra = set(registry.keys()) - set(TASK_NAMES)
        raise RuntimeError(
            f"Registry/TASK_NAMES mismatch — missing={missing} extra={extra}"
        )

    logger.info(
        "Task registry built: %d tasks — %s",
        len(registry),
        ", ".join(registry.keys()),
    )
    return registry
