"""
Unified Model Registry - ML Model Governance

Manages ML model lifecycle with encryption and governance gates.
State machine: shadow → paper → live
"""
import hashlib
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIMLModel
from app.core.config import get_settings
from app.core.redis import PubSubClient, RedisChannels

logger = logging.getLogger(__name__)


class UnifiedModelRegistry:
    """
    Unified model registry with encryption and governance.
    
    Features:
    - Fernet encryption for model artifacts
    - SHA256 checksum verification
    - State machine: shadow → paper → live
    - Evaluation gates for promotions
    - Redis pub/sub for state changes
    """

    def __init__(self, encryption_key: Optional[str] = None):
        """
        Initialize registry with encryption key.
        
        Args:
            encryption_key: Fernet key (base64). If None, uses settings.
        """
        settings = get_settings()
        key = encryption_key or settings.ML_MODEL_ENCRYPTION_KEY
        self.cipher = Fernet(key.encode()) if key else None

    def _encrypt_artifact(self, artifact_bytes: bytes) -> bytes:
        """Encrypt model artifact with Fernet."""
        if not self.cipher:
            raise ValueError("Encryption key not configured")
        return self.cipher.encrypt(artifact_bytes)

    def _decrypt_artifact(self, encrypted_bytes: bytes) -> bytes:
        """Decrypt model artifact with Fernet."""
        if not self.cipher:
            raise ValueError("Encryption key not configured")
        return self.cipher.decrypt(encrypted_bytes)

    def _compute_checksum(self, data: bytes) -> str:
        """Compute SHA256 checksum."""
        return hashlib.sha256(data).hexdigest()

    async def register_model(
        self,
        db: AsyncSession,
        model_name: str,
        model_type: str,
        model_version: str,
        timeframe: str,
        artifact_bytes: bytes,
        metrics: dict,
        metadata: Optional[dict] = None,
    ) -> AIMLModel:
        """
        Register a new model in shadow mode with encryption.
        
        Args:
            db: Database session
            model_name: Model identifier
            model_type: Model type (e.g., "lstm", "transformer")
            model_version: Version string
            timeframe: Trading timeframe (e.g., "1d", "1h")
            artifact_bytes: Model artifact (ONNX file bytes)
            metrics: Evaluation metrics (accuracy, precision, recall, f1)
            metadata: Additional metadata
            
        Returns:
            Registered AIMLModel
        """
        # Compute checksum before encryption
        checksum = self._compute_checksum(artifact_bytes)
        
        # Encrypt artifact
        encrypted_artifact = self._encrypt_artifact(artifact_bytes)
        
        # Create model record
        model = AIMLModel(
            model_name=model_name,
            model_type=model_type,
            deployment_state="shadow",
            model_version=model_version,
            timeframe=timeframe,
            artifact_encrypted=encrypted_artifact,
            artifact_sha256=checksum,
            training_date=datetime.utcnow(),
            accuracy=Decimal(str(metrics.get("accuracy", 0))),
            precision=Decimal(str(metrics.get("precision", 0))),
            recall=Decimal(str(metrics.get("recall", 0))),
            f1_score=Decimal(str(metrics.get("f1_score", 0))),
            governance_metadata=metadata or {},
        )

        db.add(model)
        await db.commit()
        await db.refresh(model)

        logger.info(
            f"Registered model {model_name} v{model_version} "
            f"[{timeframe}] in shadow mode (checksum: {checksum[:8]}...)"
        )
        return model

    async def promote_model(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        model_name: str,
        target_state: str,
        evaluation_results: Optional[dict] = None,
    ) -> AIMLModel:
        """
        Promote model to next deployment state with governance gates.
        
        State transitions:
        - shadow → paper: accuracy >= 0.85
        - paper → live: accuracy >= 0.90, precision >= 0.85, recall >= 0.80
        
        Args:
            db: Database session
            pubsub: Redis pub/sub client
            model_name: Model identifier
            target_state: Target state ("paper" or "live")
            evaluation_results: Additional evaluation results
            
        Returns:
            Promoted AIMLModel
            
        Raises:
            ValueError: If state transition invalid or gates not met
        """
        # Fetch model
        stmt = select(AIMLModel).where(AIMLModel.model_name == model_name)
        result = await db.execute(stmt)
        model = result.scalar_one()

        current_state = model.deployment_state

        # Validate state transition
        valid_transitions = {
            "shadow": ["paper"],
            "paper": ["live", "shadow"],  # Can demote to shadow
            "live": ["shadow"],  # Can demote to shadow
        }

        if target_state not in valid_transitions.get(current_state, []):
            raise ValueError(
                f"Invalid transition: {current_state} → {target_state}. "
                f"Valid: {valid_transitions.get(current_state, [])}"
            )

        # Evaluation gates
        if target_state == "paper" and current_state == "shadow":
            # Shadow → Paper: accuracy gate
            if model.accuracy < Decimal("0.85"):
                raise ValueError(
                    f"Shadow→Paper gate failed: accuracy {model.accuracy} < 0.85"
                )
            logger.info(f"Shadow→Paper gate passed: accuracy {model.accuracy}")

        elif target_state == "live" and current_state == "paper":
            # Paper → Live: full evaluation gates
            if model.accuracy < Decimal("0.90"):
                raise ValueError(
                    f"Paper→Live gate failed: accuracy {model.accuracy} < 0.90"
                )
            if model.precision < Decimal("0.85"):
                raise ValueError(
                    f"Paper→Live gate failed: precision {model.precision} < 0.85"
                )
            if model.recall < Decimal("0.80"):
                raise ValueError(
                    f"Paper→Live gate failed: recall {model.recall} < 0.80"
                )
            logger.info(
                f"Paper→Live gate passed: accuracy={model.accuracy}, "
                f"precision={model.precision}, recall={model.recall}"
            )

        # Update state
        model.deployment_state = target_state
        model.updated_at = datetime.utcnow()
        
        # Store evaluation results if provided
        if evaluation_results:
            model.governance_metadata = model.governance_metadata or {}
            model.governance_metadata["evaluation_results"] = evaluation_results
            model.governance_metadata["promoted_at"] = datetime.utcnow().isoformat()

        await db.commit()
        await db.refresh(model)

        # Publish state change event
        await pubsub.publish_json(RedisChannels.MODELS_STATE_CHANGES, {
            "action": "model_promoted",
            "model_name": model_name,
            "from_state": current_state,
            "to_state": target_state,
            "model_version": model.model_version,
            "timeframe": model.timeframe,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(f"Promoted model {model_name}: {current_state} → {target_state}")
        return model

    async def load_model(
        self,
        db: AsyncSession,
        model_name: str,
        verify_checksum: bool = True,
    ) -> tuple[bytes, AIMLModel]:
        """
        Load and decrypt model artifact with checksum verification.
        
        Args:
            db: Database session
            model_name: Model identifier
            verify_checksum: Whether to verify SHA256 checksum
            
        Returns:
            Tuple of (decrypted_artifact_bytes, model_record)
            
        Raises:
            ValueError: If checksum verification fails
        """
        # Fetch model
        stmt = select(AIMLModel).where(AIMLModel.model_name == model_name)
        result = await db.execute(stmt)
        model = result.scalar_one()

        # Decrypt artifact
        decrypted_artifact = self._decrypt_artifact(model.artifact_encrypted)

        # Verify checksum
        if verify_checksum:
            computed_checksum = self._compute_checksum(decrypted_artifact)
            if computed_checksum != model.artifact_sha256:
                raise ValueError(
                    f"Checksum mismatch for {model_name}: "
                    f"expected {model.artifact_sha256[:8]}..., "
                    f"got {computed_checksum[:8]}..."
                )
            logger.debug(f"Checksum verified for {model_name}")

        logger.info(
            f"Loaded model {model_name} v{model.model_version} "
            f"[{model.deployment_state}] ({len(decrypted_artifact)} bytes)"
        )
        return decrypted_artifact, model

    async def get_active_models(
        self,
        db: AsyncSession,
        state: str = "live",
        timeframe: Optional[str] = None,
    ) -> list[AIMLModel]:
        """
        Get all models in a specific state.
        
        Args:
            db: Database session
            state: Deployment state filter
            timeframe: Optional timeframe filter
            
        Returns:
            List of AIMLModel records
        """
        stmt = select(AIMLModel).where(AIMLModel.deployment_state == state)
        
        if timeframe:
            stmt = stmt.where(AIMLModel.timeframe == timeframe)
        
        result = await db.execute(stmt)
        models = result.scalars().all()
        
        logger.debug(f"Found {len(models)} models in {state} state")
        return list(models)

    async def demote_model(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        model_name: str,
        reason: str,
    ) -> AIMLModel:
        """
        Demote model to shadow state (e.g., due to drift).
        
        Args:
            db: Database session
            pubsub: Redis pub/sub client
            model_name: Model identifier
            reason: Reason for demotion
            
        Returns:
            Demoted AIMLModel
        """
        return await self.promote_model(
            db=db,
            pubsub=pubsub,
            model_name=model_name,
            target_state="shadow",
            evaluation_results={"demotion_reason": reason},
        )
