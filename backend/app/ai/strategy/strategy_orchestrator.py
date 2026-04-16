"""
Strategy Orchestrator - Strategy Selection & Management

Selects and manages trading strategies based on market regime.
"""
import logging
from datetime import datetime

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIActiveStrategy, AIRegimeDetection
from app.core.redis import PubSubClient, RedisChannels

logger = logging.getLogger(__name__)


class StrategyOrchestrator:
    """Orchestrates strategy selection and activation."""

    async def activate_strategy(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        strategy_id: str,
        strategy_name: str,
        regime_type: str,
        priority: int = 5,
    ) -> AIActiveStrategy:
        """Activate a trading strategy."""
        strategy = AIActiveStrategy(
            strategy_id=strategy_id,
            strategy_name=strategy_name,
            regime_type=regime_type,
            priority_level=priority,
            status="active",
            activated_at=datetime.utcnow(),
        )

        db.add(strategy)
        await db.commit()
        await db.refresh(strategy)

        await pubsub.publish_json(RedisChannels.MODELS_STATE_CHANGES, {
            "action": "strategy_activated",
            "strategy_id": strategy_id,
            "regime": regime_type,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(f"Activated strategy {strategy_id} for regime {regime_type}")
        return strategy

    async def deactivate_strategy(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        strategy_id: str,
    ) -> None:
        """Deactivate a trading strategy."""
        stmt = select(AIActiveStrategy).where(
            and_(
                AIActiveStrategy.strategy_id == strategy_id,
                AIActiveStrategy.status == "active",
            )
        )
        result = await db.execute(stmt)
        strategy = result.scalar_one_or_none()

        if strategy:
            strategy.status = "stopped"
            strategy.deactivated_at = datetime.utcnow()
            await db.commit()

            await pubsub.publish_json(RedisChannels.MODELS_STATE_CHANGES, {
                "action": "strategy_deactivated",
                "strategy_id": strategy_id,
                "timestamp": datetime.utcnow().isoformat(),
            })

            logger.info(f"Deactivated strategy {strategy_id}")

    async def select_strategy_for_regime(
        self,
        db: AsyncSession,
        regime_type: str,
    ) -> AIActiveStrategy | None:
        """Select best strategy for current regime."""
        stmt = (
            select(AIActiveStrategy)
            .where(
                and_(
                    AIActiveStrategy.regime_type == regime_type,
                    AIActiveStrategy.status == "active",
                )
            )
            .order_by(AIActiveStrategy.priority_level.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
