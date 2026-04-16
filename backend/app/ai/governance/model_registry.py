"""
Model Registry - ML Model Governance

Manages ML model lifecycle: shadow → paper → live with governance gates.
"""
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIMLModel
from app.core.redis import PubSubClient, RedisChannels

logger = logging.getLogger(__name__)


class AIMLModel(Base):
    """AI ML Models table (from migration 0005)."""
    __tablename__ = "ai_ml_models"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    model_type: Mapped[str] = mapped_column(String(50), nullable=False)
    deployment_state: Mapped[str] = mapped_column(String(20), server_default="shadow", index=True)
    model_version: Mapped[str] = mapped_column(String(50), nullable=False)
    model_path: Mapped[str | None] = mapped_column(String(500))
    training_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accuracy: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    precision: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    recall: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    f1_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    governance_metadata: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class ModelRegistry:
    """Manages ML model governance and lifecycle."""

    async def register_model(
        self,
        db: AsyncSession,
        model_name: str,
        model_type: str,
        model_version: str,
        metrics: dict,
    ) -> AIMLModel:
        """Register a new model in shadow mode."""
        model = AIMLModel(
            model_name=model_name,
            model_type=model_type,
            deployment_state="shadow",
            model_version=model_version,
            training_date=datetime.utcnow(),
            accuracy=Decimal(str(metrics.get("accuracy", 0))),
            precision=Decimal(str(metrics.get("precision", 0))),
            recall=Decimal(str(metrics.get("recall", 0))),
            f1_score=Decimal(str(metrics.get("f1_score", 0))),
        )

        db.add(model)
        await db.commit()
        await db.refresh(model)

        logger.info(f"Registered model {model_name} v{model_version} in shadow mode")
        return model

    async def promote_model(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        model_name: str,
        target_state: str,
    ) -> AIMLModel:
        """Promote model to next deployment state."""
        stmt = select(AIMLModel).where(AIMLModel.model_name == model_name)
        result = await db.execute(stmt)
        model = result.scalar_one()

        # Governance gates
        if target_state == "paper" and model.deployment_state == "shadow":
            if model.accuracy < Decimal("0.85"):
                raise ValueError(f"Model accuracy {model.accuracy} below threshold 0.85")
        elif target_state == "live" and model.deployment_state == "paper":
            if model.accuracy < Decimal("0.90"):
                raise ValueError(f"Model accuracy {model.accuracy} below threshold 0.90")

        model.deployment_state = target_state
        model.updated_at = datetime.utcnow()
        await db.commit()

        await pubsub.publish_json(RedisChannels.MODELS_STATE_CHANGES, {
            "action": "model_promoted",
            "model_name": model_name,
            "state": target_state,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(f"Promoted model {model_name} to {target_state}")
        return model
