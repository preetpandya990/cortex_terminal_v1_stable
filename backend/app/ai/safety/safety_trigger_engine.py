"""
Safety Trigger Engine - Automated Safety Monitoring

Monitors system metrics and triggers safety actions when thresholds are breached.
"""
import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.fusion.models import AISafetyTrigger
from app.ai.safety.kill_switch_manager import KillSwitchManager
from app.core.config import get_settings
from app.core.redis import PubSubClient, RedisChannels, get_pubsub_client

logger = logging.getLogger(__name__)
settings = get_settings()


class SafetyTriggerEngine:
    """Monitors safety conditions and triggers actions."""

    def __init__(self):
        self.kill_switch_manager = KillSwitchManager()

    async def check_safety_conditions(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        metrics: dict,
    ) -> list[AISafetyTrigger]:
        """Check safety conditions and trigger actions if needed."""
        triggers = []

        # Check signal frequency
        signal_rate = metrics.get("signal_rate", 0)
        if signal_rate > settings.SIGNAL_FREQUENCY_THRESHOLD:
            trigger = await self._create_trigger(
                db,
                pubsub,
                "high_signal_rate",
                f"Signal rate > {settings.SIGNAL_FREQUENCY_THRESHOLD}/min",
                Decimal(str(settings.SIGNAL_FREQUENCY_THRESHOLD)),
                Decimal(str(signal_rate)),
                "kill_switch_activated",
            )
            triggers.append(trigger)

        # Check volatility spike
        volatility = metrics.get("volatility", 0)
        threshold = settings.VOLATILITY_SPIKE_MULTIPLIER
        if volatility > threshold:
            trigger = await self._create_trigger(
                db,
                pubsub,
                "high_volatility",
                f"Volatility > {threshold}x normal",
                Decimal(str(threshold)),
                Decimal(str(volatility)),
                "paused_signals",
            )
            triggers.append(trigger)

        return triggers

    async def _create_trigger(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        trigger_type: str,
        condition: str,
        threshold: Decimal,
        actual: Decimal,
        action: str,
    ) -> AISafetyTrigger:
        """Create safety trigger and take action."""
        trigger = AISafetyTrigger(
            trigger_type=trigger_type,
            trigger_condition=condition,
            threshold_value=threshold,
            actual_value=actual,
            action_taken=action,
            triggered_at=datetime.utcnow(),
        )

        db.add(trigger)
        await db.commit()
        await db.refresh(trigger)

        # Publish to Redis
        await pubsub.publish_json(RedisChannels.SAFETY_TRIGGERS, {
            "trigger_id": trigger.id,
            "type": trigger_type,
            "action": action,
            "timestamp": trigger.triggered_at.isoformat(),
        })

        # Take action if needed
        if action == "kill_switch_activated":
            await self.kill_switch_manager.activate(
                db,
                pubsub,
                "global",
                None,
                "safety_trigger_engine",
                f"Safety trigger: {condition}",
            )

        logger.warning(f"Safety trigger: {trigger_type} - {action}")
        return trigger


async def safety_monitoring_loop(session_factory: async_sessionmaker) -> None:
    """
    Safety monitoring background loop.
    
    Monitors system safety conditions and activates kill switch if thresholds breached.
    Runs until cancelled via asyncio.CancelledError.
    
    Args:
        session_factory: SQLAlchemy async session factory
    """
    logger.info("Safety monitoring loop started")
    engine = SafetyTriggerEngine()
    pubsub = get_pubsub_client()
    
    try:
        while True:
            try:
                async with session_factory() as db:
                    # Gather safety metrics
                    # Production would query:
                    # - Recent P&L from trading signals
                    # - Signal generation rate (last 1 minute)
                    # - Market volatility (VIX equivalent)
                    # - System health metrics
                    
                    metrics = {
                        "signal_rate": 0,  # Placeholder
                        "volatility": 0,   # Placeholder
                        "loss_pct": 0,     # Placeholder
                    }
                    
                    # Check safety conditions
                    triggers = await engine.check_safety_conditions(db, pubsub, metrics)
                    
                    if triggers:
                        logger.warning(f"Safety check triggered {len(triggers)} actions")
                    else:
                        logger.debug("Safety check passed - all conditions normal")
            
            except Exception as e:
                logger.error(f"Safety monitoring error: {e}", exc_info=True)
            
            # Sleep between iterations
            logger.debug(f"Sleeping for {settings.SAFETY_CHECK_INTERVAL_SECONDS}s")
            await asyncio.sleep(settings.SAFETY_CHECK_INTERVAL_SECONDS)
    
    except asyncio.CancelledError:
        logger.info("Safety monitoring loop cancelled")
        raise
    finally:
        logger.info("Safety monitoring loop stopped")
