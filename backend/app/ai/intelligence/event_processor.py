"""
Event Processing Loop - Raw Events → NLP → Classification → Signals

Processes raw events through the complete AI pipeline:
1. Fetch unprocessed raw events
2. Run NLP analysis (entity extraction, sentiment)
3. Classify event type and impact
4. Generate trading signals
5. Publish to Redis channels
"""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.fusion.models import (
    AIRawEvent,
    AIProcessedEvent,
    AINLPResult,
    AIEventClassification,
    AITradingSignal,
)
from app.ai.intelligence.nlp_engine import NLPEngine
from app.ai.intelligence.event_classifier import EventClassifier
from app.ai.fusion.signal_assembler import SignalAssembler
from app.core.config import get_settings
from app.core.redis import get_cache_service, PubSubClient, RedisChannels, get_pubsub_client

logger = logging.getLogger(__name__)
settings = get_settings()


class EventProcessor:
    """Processes raw events through NLP → Classification → Signal generation."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        batch_size: int = 10,
        ensemble_predictor=None,
        feature_loader=None,
    ):
        self.session_factory = session_factory
        self.batch_size = batch_size
        self.nlp_engine = NLPEngine()
        self.event_classifier = EventClassifier(use_llm=True)
        self.signal_assembler = SignalAssembler(
            ensemble_predictor=ensemble_predictor,
            feature_loader=feature_loader,
        )
        self.pubsub = get_pubsub_client()

    async def process_raw_event(
        self,
        db: AsyncSession,
        event_data: dict,
    ) -> Optional[AITradingSignal]:
        """
        Process a single raw event through the complete pipeline.
        
        Args:
            db: Database session
            event_data: Dict with 'id', 'content', 'event' keys
            
        Returns:
            AITradingSignal if signal was generated, None otherwise
        """
        event_id = event_data["id"]
        event_content = event_data["content"]
        
        try:
            # Step 1: Create processed event record
            processed_event = AIProcessedEvent(
                raw_event_id=event_id,
                processed_timestamp=datetime.now(timezone.utc),
                processing_status="completed",
            )
            db.add(processed_event)
            await db.flush()  # Get processed_event.id
            
            # Step 2: NLP analysis
            nlp_result = await self.nlp_engine.process_event(
                db=db,
                processed_event_id=processed_event.id,
                content=event_content,
            )
            
            # Step 3: Event classification
            classification = await self.event_classifier.classify(
                db=db,
                nlp_result_id=nlp_result.id,
                content=event_content,
                entities=nlp_result.named_entities or {},
            )
            
            # Step 4: Generate signals for affected symbols
            signals = []
            if classification.affected_symbols:
                for symbol in classification.affected_symbols:
                    try:
                        signal = await self.signal_assembler.assemble_signal(
                            db=db,
                            pubsub=self.pubsub,
                            symbol=symbol,
                            timeframe="1h",  # Default timeframe
                        )
                        
                        if signal:
                            signals.append(signal)
                            
                            # Publish signal to Redis
                            import json
                            await self.pubsub.publish(
                                RedisChannels.SIGNALS_ALL,
                                json.dumps({
                                    "symbol": symbol,
                                    "signal_id": signal.id,
                                    "action": signal.action,
                                    "confidence": float(signal.confidence_score),
                                    "timestamp": signal.signal_timestamp.isoformat(),
                                })
                            )
                            
                            logger.info(
                                f"Generated signal for {symbol}: {signal.action}, "
                                f"confidence={signal.confidence_score:.2f}"
                            )
                    except Exception as e:
                        logger.error(f"Failed to generate signal for {symbol}: {e}", exc_info=True)
            
            await db.commit()
            
            return signals[0] if signals else None
            
        except Exception as e:
            logger.error(f"Failed to process raw event {event_id}: {e}", exc_info=True)
            await db.rollback()
            
            # Update processed event status to failed
            try:
                stmt = (
                    update(AIProcessedEvent)
                    .where(AIProcessedEvent.raw_event_id == event_id)
                    .values(processing_status="failed", error_message=str(e))
                )
                await db.execute(stmt)
                await db.commit()
            except Exception as update_error:
                logger.error(f"Failed to update error status for event {event_id}: {update_error}")
            
            return None

    async def process_batch(self, db: AsyncSession) -> int:
        """
        Process a batch of unprocessed raw events.
        
        Returns:
            Number of events processed
        """
        # Find unprocessed raw events with eager loading to avoid lazy load issues
        stmt = (
            select(AIRawEvent)
            .outerjoin(AIProcessedEvent, AIRawEvent.id == AIProcessedEvent.raw_event_id)
            .where(AIProcessedEvent.id.is_(None))
            .order_by(AIRawEvent.event_timestamp.desc())
            .limit(self.batch_size)
        )
        
        result = await db.execute(stmt)
        raw_events = result.scalars().all()
        
        if not raw_events:
            return 0
        
        logger.info(f"Processing {len(raw_events)} unprocessed events")
        
        # Eagerly extract all data before processing to avoid lazy loading
        events_data = [
            {
                "id": event.id,
                "content": event.raw_content,
                "event": event
            }
            for event in raw_events
        ]
        
        processed_count = 0
        for event_data in events_data:
            signal = await self.process_raw_event(db, event_data)
            if signal:
                processed_count += 1
        
        return processed_count


async def event_processing_loop(
    session_factory: async_sessionmaker,
    ensemble_predictor=None,
    feature_loader=None,
) -> None:
    """
    Event processing background loop.
    
    Continuously processes raw events through NLP → Classification → Signals.
    Runs until cancelled via asyncio.CancelledError.
    
    Args:
        session_factory: SQLAlchemy async session factory
        ensemble_predictor: ML ensemble predictor (optional)
        feature_loader: Feature loader service (optional)
    """
    logger.info("Event processing loop started")
    
    try:
        logger.info("Creating EventProcessor instance...")
        processor = EventProcessor(
            session_factory,
            batch_size=10,
            ensemble_predictor=ensemble_predictor,
            feature_loader=feature_loader,
        )
        logger.info("EventProcessor created successfully")
        if ensemble_predictor:
            logger.info("✓ ML predictions enabled")
        else:
            logger.info("⚠ ML predictions disabled (no model)")
    except Exception as e:
        logger.error(f"Failed to create EventProcessor: {e}", exc_info=True)
        return
    
    # Processing interval (30 seconds default)
    processing_interval = 30
    logger.info(f"Event processing interval: {processing_interval}s")
    
    try:
        while True:
            try:
                logger.info("Event processing cycle starting...")
                async with session_factory() as db:
                    processed_count = await processor.process_batch(db)
                    
                    if processed_count > 0:
                        logger.info(f"Event processing cycle complete: {processed_count} events processed")
                    else:
                        logger.info("No unprocessed events found")
            
            except Exception as e:
                logger.error(f"Event processing error: {e}", exc_info=True)
            
            # Sleep before next cycle
            await asyncio.sleep(processing_interval)
    
    except asyncio.CancelledError:
        logger.info("Event processing loop cancelled")
        raise
    finally:
        logger.info("Event processing loop stopped")
