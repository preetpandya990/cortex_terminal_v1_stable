"""
Signal Generation Pipeline - Async Orchestration

Orchestrates: Ingestion → Intelligence → Fusion → Distribution
Implements circuit breakers and retry logic for resilience.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import (
    AIRawEvent,
    AIProcessedEvent,
    AINLPResult,
    AIEventClassification,
    AITradingSignal,
    AIKillSwitch,
)
from app.ai.fusion.signal_assembler import SignalAssembler
from app.ai.safety.kill_switch_manager import KillSwitchManager
from app.core.redis import PubSubClient, RedisChannels

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Circuit breaker for fault tolerance."""

    def __init__(self, failure_threshold: int = 5, timeout_seconds: int = 60):
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.failure_count = 0
        self.last_failure_time: datetime | None = None
        self.state = "closed"  # closed, open, half_open

    def record_success(self):
        if self.state == "half_open":
            self.state = "closed"
            self.failure_count = 0
            logger.info("Circuit breaker closed")

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = datetime.utcnow()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning(f"Circuit breaker opened (failures: {self.failure_count})")

    def can_attempt(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open" and self.last_failure_time:
            elapsed = (datetime.utcnow() - self.last_failure_time).total_seconds()
            if elapsed >= self.timeout_seconds:
                self.state = "half_open"
                logger.info("Circuit breaker half-open")
                return True
            return False
        return True  # half_open


_HIGH_IMPACT_THRESHOLD = 0.70  # impact_score ≥ this → event-driven on-demand trigger


class SignalPipeline:
    """
    End-to-end signal generation pipeline with resilience.

    Optional scheduler injection enables the event-driven on-demand trigger:
    for high-impact events (classification.impact_score ≥ 0.70), affected symbols
    are immediately queued via scheduler.generate_on_demand() as a fire-and-forget
    task alongside the synchronous fusion layer.  This ensures stale signals are
    never served to users monitoring high-impact news even if the scheduled cycle
    has not yet run.
    """

    def __init__(
        self,
        max_retries: int = 3,
        retry_delay: float = 1.0,
        scheduler=None,  # Optional[SignalScheduler] — avoids circular import
    ):
        self.signal_assembler = SignalAssembler()
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._scheduler = scheduler
        self.circuit_breakers = {
            "intelligence": CircuitBreaker(),
            "fusion": CircuitBreaker(),
        }

    async def _execute_with_circuit_breaker(
        self,
        component: str,
        operation,
        *args,
        **kwargs,
    ) -> Any:
        """Execute operation with circuit breaker protection."""
        breaker = self.circuit_breakers.get(component)
        if not breaker or not breaker.can_attempt():
            raise RuntimeError(f"Circuit breaker open for {component}")

        try:
            result = await operation(*args, **kwargs)
            breaker.record_success()
            return result
        except Exception as e:
            breaker.record_failure()
            raise e

    async def _retry_with_backoff(self, operation, *args, **kwargs) -> Any:
        """Retry operation with exponential backoff."""
        for attempt in range(self.max_retries):
            try:
                return await operation(*args, **kwargs)
            except Exception as e:
                if attempt == self.max_retries - 1:
                    raise e
                delay = self.retry_delay * (2 ** attempt)
                logger.warning(f"Retry {attempt + 1}/{self.max_retries} after {delay}s: {e}")
                await asyncio.sleep(delay)

    async def process_intelligence_layer(
        self,
        db: AsyncSession,
        raw_event_id: int,
    ) -> dict[str, Any]:
        """Process intelligence layer: NLP → Classification."""
        # Placeholder - will be implemented with NLP services in task 4.3
        return {
            "nlp_result_id": None,
            "classification_id": None,
            "sentiment": "neutral",
            "event_type": "unknown",
        }

    async def process_fusion_layer(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        symbols: list[str],
    ) -> list[AITradingSignal]:
        """Process fusion layer: assemble signals for symbols."""
        signals = []
        for symbol in symbols:
            try:
                signal = await self._execute_with_circuit_breaker(
                    "fusion",
                    self.signal_assembler.assemble_signal,
                    db,
                    pubsub,
                    symbol,
                )
                signals.append(signal)
            except Exception as e:
                logger.error(f"Fusion failed for {symbol}: {e}")
        return signals


    async def process_event_end_to_end(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        redis: Redis,
        raw_event_id: int,
    ) -> dict[str, Any]:
        """Process single event through entire pipeline."""
        # Check kill switch (fast path: Redis cache)
        kill_switch_manager = KillSwitchManager(redis)
        if await kill_switch_manager.is_active(db, switch_type="global"):
            logger.warning("Global kill switch active - skipping event")
            return {"status": "skipped", "reason": "kill_switch_active"}

        # Intelligence layer
        intelligence_result = await self._retry_with_backoff(
            self.process_intelligence_layer,
            db,
            raw_event_id,
        )

        # Resolve affected symbols and event impact score from the classification chain.
        symbols, impact_score = await self._resolve_affected_symbols(db, raw_event_id)
        if not symbols:
            logger.info("No affected symbols found for raw_event_id=%d — skipping fusion", raw_event_id)
            return {"status": "skipped", "reason": "no_affected_symbols", "raw_event_id": raw_event_id}

        # Event-driven on-demand trigger: for high-impact events, immediately queue
        # signal generation for each affected symbol via the scheduler's on-demand
        # path.  This is fire-and-forget — it never blocks the pipeline.
        # The scheduler's generate_on_demand() checks the on-demand Redis cache;
        # assemble_signal() primes that cache after every assembly, so if the fusion
        # layer below has already generated a fresh signal, the task is a no-op.
        if impact_score >= _HIGH_IMPACT_THRESHOLD and self._scheduler is not None:
            for sym in symbols:
                asyncio.create_task(
                    self._scheduler.generate_on_demand(sym),
                    name=f"event_trigger_{sym}_{raw_event_id}",
                )
            logger.info(
                "Event-driven trigger fired for %d symbol(s) (impact=%.2f, raw_event_id=%d)",
                len(symbols),
                impact_score,
                raw_event_id,
            )

        # Fusion layer — assemble_signal inside publishes to SIGNALS_ALL
        # and the per-symbol channel; no separate distribution step needed.
        signals = await self._retry_with_backoff(
            self.process_fusion_layer,
            db,
            pubsub,
            symbols,
        )

        return {
            "status": "completed",
            "raw_event_id": raw_event_id,
            "signals_generated": len(signals),
            "symbols": symbols,
        }

    async def process_batch_events(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        event_ids: list[int],
        concurrency: int = 5,
    ) -> dict[str, Any]:
        """Process batch of events with concurrency control."""
        semaphore = asyncio.Semaphore(concurrency)

        async def process_with_semaphore(event_id: int):
            async with semaphore:
                return await self.process_event_end_to_end(db, pubsub, event_id)

        results = await asyncio.gather(
            *[process_with_semaphore(eid) for eid in event_ids],
            return_exceptions=True,
        )

        success_count = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "completed")
        error_count = sum(1 for r in results if isinstance(r, Exception))

        return {
            "total": len(event_ids),
            "success": success_count,
            "errors": error_count,
            "results": [r for r in results if not isinstance(r, Exception)],
        }

    async def _resolve_affected_symbols(
        self,
        db: AsyncSession,
        raw_event_id: int,
    ) -> tuple[list[str], float]:
        """
        Resolve affected symbols and event impact score by traversing the
        classification chain: raw_event → processed_event → nlp_result → classification.

        Returns:
            (symbols, impact_score) — symbols is deduplicated and order-preserving;
            impact_score is 0.0 when the chain is incomplete or classification missing.
        """
        processed = (
            await db.execute(
                select(AIProcessedEvent).where(AIProcessedEvent.raw_event_id == raw_event_id)
            )
        ).scalar_one_or_none()

        if not processed:
            return [], 0.0

        nlp = (
            await db.execute(
                select(AINLPResult).where(AINLPResult.processed_event_id == processed.id)
            )
        ).scalar_one_or_none()

        if not nlp:
            return [], 0.0

        classification = (
            await db.execute(
                select(AIEventClassification).where(
                    AIEventClassification.nlp_result_id == nlp.id
                )
            )
        ).scalar_one_or_none()

        if not classification or not classification.affected_symbols:
            return [], 0.0

        symbols = list(dict.fromkeys(classification.affected_symbols))  # deduplicate, preserve order
        impact_score = float(classification.impact_score)
        return symbols, impact_score

    def get_circuit_breaker_status(self) -> dict[str, dict[str, Any]]:
        """Get status of all circuit breakers."""
        return {
            name: {
                "state": breaker.state,
                "failure_count": breaker.failure_count,
                "last_failure": breaker.last_failure_time.isoformat() if breaker.last_failure_time else None,
            }
            for name, breaker in self.circuit_breakers.items()
        }
