"""
Cortex AI — ML Feedback Retry Infrastructure
=============================================
Production-grade async retry for compute_ml_feedback() with:

  - 3 attempts, exponential backoff (1s → 2s) + uniform jitter (±20%)
  - Per-attempt Prometheus instrumentation
  - Durable error record (ml_feedback_errors table) on final failure
  - Redis pub/sub alert on final failure (cai:ml:feedback_errors)
  - Structured logging at every stage

Retry configuration rationale:
  - 3 attempts cover transient DB connection drops and lock timeouts
  - Backoff cap at 30s prevents indefinite blocking in the event loop
  - Jitter prevents thundering-herd from batch position closes
  - Total maximum wall-clock time before giving up: ~33s — acceptable for
    a non-blocking background task
  - Uses tenacity (already a project dependency) for battle-tested retry logic

Call sites:
  - paper_trading.py  → FastAPI BackgroundTask after manual position close
  - pnl_worker.py     → asyncio.create_task after auto-close (SL/TP hit)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker
from tenacity import (
    AsyncRetrying,
    RetryError,
    before_sleep_log,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS: int = 3
_BASE_WAIT_S: float = 1.0   # first retry delay (seconds)
_MAX_WAIT_S: float = 30.0   # backoff ceiling
_JITTER_S: float = 0.5      # ±uniform jitter added on top of exp delay


async def compute_ml_feedback_with_retry(
    *,
    outcome_id: UUID,
    symbol: str,
    portfolio_id: UUID,
    user_id: int,
    session_factory: async_sessionmaker,
) -> None:
    """
    Compute ML feedback for a closed position with exponential-backoff retry.

    Each attempt opens a fresh DB session so a prior session's error state
    cannot poison subsequent attempts.

    On final failure:
      1. Writes an MLFeedbackError record (durable audit trail)
      2. Publishes to Redis cai:ml:feedback_errors (real-time ops alert)
      3. Increments ml_feedback_computations_total{status="failure"}

    Parameters
    ----------
    outcome_id      : UUID of the PaperTradeOutcome to compute feedback for
    symbol          : Trading symbol (denormalized — for logging and error record)
    portfolio_id    : Portfolio UUID (denormalized — for error record)
    user_id         : Owning user ID (denormalized — for error record)
    session_factory : async_sessionmaker to create fresh sessions per attempt
    """
    from app.core.metrics import (
        ml_feedback_computation_duration_seconds,
        ml_feedback_computations_total,
        ml_feedback_retry_attempts_total,
    )
    from app.services.paper_trading.outcome_service import compute_ml_feedback

    attempt_num = 0
    wall_start = time.monotonic()
    first_attempt_at = datetime.now(timezone.utc)

    async def _single_attempt() -> None:
        nonlocal attempt_num
        attempt_num += 1
        ml_feedback_retry_attempts_total.labels(attempt=str(attempt_num)).inc()
        logger.debug(
            "ML feedback attempt %d/%d: outcome=%s symbol=%s",
            attempt_num, _MAX_ATTEMPTS, outcome_id, symbol,
        )
        async with session_factory() as session:
            await compute_ml_feedback(session, outcome_id)
            await session.commit()

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=wait_exponential_jitter(
                initial=_BASE_WAIT_S,
                max=_MAX_WAIT_S,
                jitter=_JITTER_S,
            ),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=False,
        ):
            with attempt:
                await _single_attempt()

        duration = time.monotonic() - wall_start
        ml_feedback_computation_duration_seconds.observe(duration)
        ml_feedback_computations_total.labels(status="success").inc()
        logger.info(
            "ML feedback computed: outcome=%s symbol=%s attempts=%d duration=%.3fs",
            outcome_id, symbol, attempt_num, duration,
        )

    except RetryError as exc:
        last_attempt_at = datetime.now(timezone.utc)
        final_error = exc.last_attempt.exception()
        error_message = str(final_error) if final_error else "Unknown error"
        error_type = type(final_error).__qualname__ if final_error else "Unknown"

        ml_feedback_computations_total.labels(status="failure").inc()
        logger.error(
            "ML feedback FAILED after %d attempts: outcome=%s symbol=%s "
            "error_type=%s error=%s",
            attempt_num, outcome_id, symbol, error_type, error_message,
        )

        error_id = await _record_feedback_error(
            outcome_id=outcome_id,
            symbol=symbol,
            portfolio_id=portfolio_id,
            user_id=user_id,
            error_message=error_message,
            error_type=error_type,
            attempt_count=attempt_num,
            first_attempt_at=first_attempt_at,
            last_attempt_at=last_attempt_at,
            session_factory=session_factory,
        )

        await _publish_feedback_error_alert(
            outcome_id=outcome_id,
            symbol=symbol,
            portfolio_id=portfolio_id,
            user_id=user_id,
            error_message=error_message,
            error_type=error_type,
            attempt_count=attempt_num,
            first_attempt_at=first_attempt_at,
            last_attempt_at=last_attempt_at,
            ml_feedback_error_id=error_id,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _record_feedback_error(
    *,
    outcome_id: UUID,
    symbol: str,
    portfolio_id: UUID,
    user_id: int,
    error_message: str,
    error_type: str,
    attempt_count: int,
    first_attempt_at: datetime,
    last_attempt_at: datetime,
    session_factory: async_sessionmaker,
) -> UUID | None:
    """
    Write an MLFeedbackError record.  Returns the new record's UUID or None
    if the write itself fails (logged; never raises — error recording must
    not disrupt the calling flow).
    """
    from app.models.ml_feedback import MLFeedbackError

    try:
        record = MLFeedbackError(
            outcome_id=outcome_id,
            symbol=symbol,
            portfolio_id=portfolio_id,
            user_id=user_id,
            error_message=error_message,
            error_type=error_type,
            attempt_count=attempt_count,
            first_attempt_at=first_attempt_at,
            last_attempt_at=last_attempt_at,
        )
        async with session_factory() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
        logger.info(
            "MLFeedbackError recorded: id=%s outcome=%s symbol=%s",
            record.id, outcome_id, symbol,
        )
        return record.id
    except Exception as write_exc:  # noqa: BLE001
        logger.error(
            "Failed to write MLFeedbackError for outcome=%s symbol=%s: %s",
            outcome_id, symbol, write_exc,
        )
        return None


async def _publish_feedback_error_alert(
    *,
    outcome_id: UUID,
    symbol: str,
    portfolio_id: UUID,
    user_id: int,
    error_message: str,
    error_type: str,
    attempt_count: int,
    first_attempt_at: datetime,
    last_attempt_at: datetime,
    ml_feedback_error_id: UUID | None,
) -> None:
    """
    Publish a structured error payload to cai:ml:feedback_errors.
    Never raises — pub/sub failure is logged and swallowed so it cannot
    block or mask the primary failure already being handled.
    """
    from app.core.redis import RedisChannels, get_pubsub_client

    try:
        pubsub = get_pubsub_client()
        await pubsub.publish_json(
            RedisChannels.ML_FEEDBACK_ERRORS,
            {
                "outcome_id": str(outcome_id),
                "symbol": symbol,
                "portfolio_id": str(portfolio_id),
                "user_id": user_id,
                "error_message": error_message,
                "error_type": error_type,
                "attempt_count": attempt_count,
                "first_attempt_at": first_attempt_at.isoformat(),
                "last_attempt_at": last_attempt_at.isoformat(),
                "ml_feedback_error_id": str(ml_feedback_error_id) if ml_feedback_error_id else None,
            },
        )
        logger.debug(
            "Published ML feedback error alert: outcome=%s symbol=%s",
            outcome_id, symbol,
        )
    except Exception as pub_exc:  # noqa: BLE001
        logger.warning(
            "Failed to publish ML feedback error alert for outcome=%s: %s",
            outcome_id, pub_exc,
        )
