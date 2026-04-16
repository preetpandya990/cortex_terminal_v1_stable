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


class SignalPipeline:
    """End-to-end signal generation pipeline with resilience."""

    def __init__(self, max_retries: int = 3, retry_delay: float = 1.0):
        self.signal_assembler = SignalAssembler()
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.circuit_breakers = {
            "intelligence": CircuitBreaker(),
            "fusion": CircuitBreaker(),
            "distribution": CircuitBreaker(),
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

    async def distribute_signals(
        self,
        pubsub: PubSubClient,
        signals: list[AITradingSignal],
    ) -> None:
        """Distribute signals to Redis pub/sub channels."""
        for signal in signals:
            try:
                await self._execute_with_circuit_breaker(
                    "distribution",
                    pubsub.publish_json,
                    RedisChannels.signals_for_symbol(signal.symbol),
                    {
                        "signal_id": signal.id,
                        "symbol": signal.symbol,
                        "action": signal.action,
                        "confidence": float(signal.confidence_score),
                        "timestamp": signal.signal_timestamp.isoformat(),
                    },
                )
            except Exception as e:
                logger.error(f"Distribution failed for signal {signal.id}: {e}")

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

        # Get affected symbols (placeholder)
        symbols = ["RELIANCE", "TCS"]  # Will be extracted from classification

        # Fusion layer
        signals = await self._retry_with_backoff(
            self.process_fusion_layer,
            db,
            pubsub,
            symbols,
        )

        # Distribution layer
        await self._retry_with_backoff(
            self.distribute_signals,
            pubsub,
            signals,
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
