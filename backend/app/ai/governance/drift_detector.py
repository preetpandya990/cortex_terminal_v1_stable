"""
Drift Detector - ML Model Drift Monitoring

AI-driven governance layer that monitors ML predictions and manages model lifecycle.
Uses ML system predictions to detect drift and trigger governance actions.
"""
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIDriftReport, AIMLModel
from app.models.ml_data import MLPrediction, MLModelMetadata
from app.core.config import get_settings
from app.core.redis import PubSubClient, RedisChannels

logger = logging.getLogger(__name__)
settings = get_settings()


class DriftDetector:
    """
    AI-driven drift detector that monitors ML model performance.
    
    Bridges ML prediction system with AI governance:
    - Monitors MLPrediction data for accuracy degradation
    - Creates AIDriftReport for governance tracking
    - Triggers AIMLModel state transitions (live → paper → shadow)
    - Publishes alerts via Redis for real-time monitoring
    """

    async def check_drift(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        model_id: int,
        lookback_hours: int = 24,
    ) -> AIDriftReport:
        """
        Check for model drift using recent ML predictions.
        
        Args:
            db: Database session
            pubsub: Redis pub/sub client for alerts
            model_id: AIMLModel ID to check
            lookback_hours: Hours of prediction history to analyze
            
        Returns:
            AIDriftReport with drift detection results
        """
        # Get AI model (with explicit refresh to ensure we see committed data)
        await db.commit()  # Ensure any pending transactions are committed
        stmt = select(AIMLModel).where(AIMLModel.id == model_id)
        result = await db.execute(stmt)
        ai_model = result.scalar_one_or_none()
        
        if not ai_model:
            logger.error(f"AI Model {model_id} not found in database")
            raise ValueError(f"AI Model {model_id} not found")

        # Find corresponding ML model by name mapping
        stmt = select(MLModelMetadata).where(
            MLModelMetadata.model_name == ai_model.model_name
        )
        result = await db.execute(stmt)
        ml_model = result.scalar_one_or_none()
        
        if not ml_model:
            # No ML predictions to analyze - use simulated metrics
            logger.warning(f"No ML model found for {ai_model.model_name}, using baseline check")
            return await self._baseline_drift_check(db, pubsub, ai_model)

        # Get recent ML predictions (timestamp column is WITHOUT TIME ZONE)
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=lookback_hours)
        stmt = select(MLPrediction).where(
            MLPrediction.model_id == ml_model.model_id,
            MLPrediction.timestamp >= cutoff
        ).order_by(MLPrediction.timestamp.desc()).limit(1000)
        
        result = await db.execute(stmt)
        recent_predictions = result.scalars().all()

        # Calculate drift metrics
        drift_score = Decimal("0.0")
        accuracy_drop = Decimal("0.0")
        distribution_metrics = {}
        
        if len(recent_predictions) >= 10:
            # Extract prediction values (Float column)
            predictions = [float(p.prediction) for p in recent_predictions]
            
            import statistics
            current_mean = statistics.mean(predictions)
            current_std = statistics.stdev(predictions) if len(predictions) > 1 else 0
            
            # Get baseline from ML model training stats
            baseline_stats = ml_model.training_prediction_stats or {}
            baseline_mean = baseline_stats.get('mean', current_mean)
            baseline_std = baseline_stats.get('std', 1.0)
            
            # Calculate drift score using z-score approach
            if baseline_std > 0:
                z_score = abs(current_mean - baseline_mean) / baseline_std
                drift_score = Decimal(str(min(z_score, 10.0)))  # Cap at 10
            
            # For models with accuracy baseline, calculate accuracy drop
            if ai_model.accuracy:
                # Estimate current accuracy from prediction confidence
                # In production, this would use actual outcomes
                estimated_accuracy = Decimal(str(max(0.5, 1.0 - (float(drift_score) * 0.05))))
                accuracy_drop = ai_model.accuracy - estimated_accuracy
            
            distribution_metrics = {
                "sample_size": len(recent_predictions),
                "current_mean": current_mean,
                "current_std": current_std,
                "baseline_mean": baseline_mean,
                "baseline_std": baseline_std,
                "z_score": float(drift_score),
                "lookback_hours": lookback_hours,
            }
        else:
            distribution_metrics = {
                "sample_size": len(recent_predictions),
                "error": "Insufficient data for drift detection (minimum 10 predictions required)"
            }

        # Determine if drift detected
        drift_detected = drift_score > Decimal(str(settings.ML_DRIFT_THRESHOLD_SIGMA))
        action_taken = None

        if drift_detected:
            action_taken = await self._handle_drift_action(db, ai_model)
            logger.warning(
                f"Drift detected for model {ai_model.model_name} (ID: {model_id}): "
                f"score={drift_score}, accuracy_drop={accuracy_drop}"
            )

        # Create AI drift report for governance tracking
        report = AIDriftReport(
            model_id=model_id,
            report_timestamp=datetime.now(timezone.utc),
            drift_detected=drift_detected,
            drift_score=drift_score,
            accuracy_drop=accuracy_drop,
            distribution_metrics=distribution_metrics,
            action_taken=action_taken,
        )

        db.add(report)
        await db.commit()
        await db.refresh(report)

        # Publish alert if drift detected
        if drift_detected:
            await pubsub.publish_json(RedisChannels.MODELS_DRIFT_ALERTS, {
                "model_id": model_id,
                "model_name": ai_model.model_name,
                "drift_score": float(drift_score),
                "accuracy_drop": float(accuracy_drop) if accuracy_drop else None,
                "action": action_taken,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "report_id": report.id,
            })
            logger.warning(f"Drift alert published for model {ai_model.model_name}")
        else:
            logger.info(f"No drift detected for model {ai_model.model_name}")

        return report
    
    async def _baseline_drift_check(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        ai_model: AIMLModel,
    ) -> AIDriftReport:
        """
        Baseline drift check when no ML predictions available.
        Uses model metadata and simulated metrics for testing.
        """
        # For testing: simulate drift based on model age
        model_age_hours = (datetime.now(timezone.utc) - ai_model.created_at).total_seconds() / 3600
        
        # Simulate drift score (for testing only)
        drift_score = Decimal("0.5")  # Low baseline
        accuracy_drop = Decimal("0.0")
        
        distribution_metrics = {
            "sample_size": 0,
            "note": "No ML predictions available - baseline check only",
            "model_age_hours": model_age_hours,
        }
        
        drift_detected = False
        action_taken = None
        
        report = AIDriftReport(
            model_id=ai_model.id,
            report_timestamp=datetime.now(timezone.utc),
            drift_detected=drift_detected,
            drift_score=drift_score,
            accuracy_drop=accuracy_drop,
            distribution_metrics=distribution_metrics,
            action_taken=action_taken,
        )
        
        db.add(report)
        await db.commit()
        await db.refresh(report)
        
        return report
    
    async def _handle_drift_action(self, db: AsyncSession, model: AIMLModel) -> str:
        """
        Handle AI governance action when drift detected.
        
        Automatic model lifecycle management:
        - live → paper (reduced risk)
        - paper → shadow (monitoring only)
        - shadow → retired (decommissioned)
        """
        original_state = model.deployment_state
        
        if model.deployment_state == "live":
            model.deployment_state = "paper"
            action = "demoted_to_paper"
        elif model.deployment_state == "paper":
            model.deployment_state = "shadow"
            action = "demoted_to_shadow"
        elif model.deployment_state == "shadow":
            model.deployment_state = "retired"
            action = "retired"
        else:
            action = "no_action"
        
        if action != "no_action":
            await db.commit()
            logger.warning(
                f"AI Governance: Model {model.model_name} transitioned {original_state} → {model.deployment_state}"
            )
        
        return action
