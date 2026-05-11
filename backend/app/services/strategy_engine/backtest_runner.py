"""
BacktestRunner — Async task wrapper for BacktestEngine.

Responsibilities
----------------
1. Open an isolated DB session via WorkerSessionLocal.
2. Re-fetch the StrategyBacktestRun and Strategy rows inside that session.
3. Transition status: pending → running on start.
4. Relay incremental progress_pct updates to the DB row.
5. On success: write result_metrics, set status → completed.
6. On failure: write error_detail, set status → failed.
7. Honour cancellation: check status == 'cancelled' before every symbol.

This module is imported by the strategies API endpoint which fires it as
an asyncio.create_task() after committing the initial 'pending' run row.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.core.database import WorkerSessionLocal
from app.models.strategies import Strategy, StrategyBacktestRun
from app.services.strategy_engine.backtest_engine import BacktestEngine

logger = logging.getLogger(__name__)

# How often (in seconds) to flush progress updates to DB
_PROGRESS_FLUSH_INTERVAL_S: float = 2.0


async def run_backtest_task(
    run_id: UUID,
    ml_predictor: Any | None = None,
) -> None:
    """
    Top-level coroutine suitable for asyncio.create_task().

    Args:
        run_id:        UUID of the StrategyBacktestRun row to execute.
        ml_predictor:  Live EnsemblePredictor from app.state (None for
                       signal_replay-only runs).
    """
    async with WorkerSessionLocal() as db:
        try:
            await _execute(db, run_id, ml_predictor)
        except Exception:
            # Last-resort safety net — _execute handles its own errors but we
            # guard against session-level exceptions (e.g. connection loss).
            logger.exception("Unhandled exception in backtest task run_id=%s", run_id)
            await _mark_failed(db, run_id, "Internal runner error — see server logs")


async def _execute(
    db: Any,
    run_id: UUID,
    ml_predictor: Any | None,
) -> None:
    # ── Fetch run & strategy ─────────────────────────────────────────────────
    run_row = await db.get(StrategyBacktestRun, run_id)
    if run_row is None:
        logger.error("BacktestRunner: run_id=%s not found in DB — aborting", run_id)
        return

    if run_row.status == "cancelled":
        logger.info("BacktestRunner: run_id=%s already cancelled — skipping", run_id)
        return

    strategy_row = await db.get(Strategy, run_row.strategy_id)
    if strategy_row is None:
        await _mark_failed(db, run_id, f"Strategy {run_row.strategy_id} not found")
        return

    # ── Transition to running ────────────────────────────────────────────────
    run_row.status = "running"
    run_row.started_at = datetime.now(timezone.utc)
    run_row.progress_pct = 0
    await db.commit()

    logger.info(
        "BacktestRunner started: run_id=%s strategy=%s mode=%s",
        run_id, strategy_row.id, run_row.mode,
    )

    # ── Progress callback with rate-limited DB flush ─────────────────────────
    _last_flush: list[float] = [0.0]  # mutable container for closure

    async def report_progress(pct: int) -> None:
        # Re-check cancellation on every callback
        await db.refresh(run_row, ["status"])
        if run_row.status == "cancelled":
            raise asyncio.CancelledError("Backtest cancelled by user")

        import time
        now = time.monotonic()
        if now - _last_flush[0] >= _PROGRESS_FLUSH_INTERVAL_S:
            run_row.progress_pct = min(pct, 99)  # 100 reserved for completion
            await db.commit()
            _last_flush[0] = now

    # ── Run the engine ───────────────────────────────────────────────────────
    try:
        engine = BacktestEngine(ml_predictor=ml_predictor)
        result = await engine.run(
            run=run_row,
            strategy=strategy_row,
            db=db,
            progress_callback=report_progress,
        )
    except asyncio.CancelledError:
        logger.info("BacktestRunner: run_id=%s cancelled during execution", run_id)
        run_row.status = "cancelled"
        run_row.completed_at = datetime.now(timezone.utc)
        await db.commit()
        return
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("BacktestRunner: run_id=%s engine error: %s\n%s", run_id, exc, tb)
        await _mark_failed(db, run_id, f"{type(exc).__name__}: {exc}")
        return

    # ── Persist results ──────────────────────────────────────────────────────
    metrics_payload = result.build_result_metrics(
        mode=run_row.mode,
        start_dt=run_row.start_dt,
        end_dt=run_row.end_dt,
        symbols=list(run_row.symbols),
    )

    run_row.status = "completed"
    run_row.progress_pct = 100
    run_row.result_metrics = metrics_payload
    run_row.completed_at = datetime.now(timezone.utc)
    await db.commit()

    logger.info(
        "BacktestRunner completed: run_id=%s trades=%d",
        run_id, metrics_payload["metrics"]["total_trades"],
    )


async def _mark_failed(db: Any, run_id: UUID, error_detail: str) -> None:
    try:
        run_row = await db.get(StrategyBacktestRun, run_id)
        if run_row and run_row.status not in ("completed", "cancelled"):
            run_row.status = "failed"
            run_row.error_detail = error_detail[:2000]  # DB column cap
            run_row.completed_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception:
        logger.exception("Failed to mark run_id=%s as failed", run_id)
