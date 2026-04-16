"""
Signal Assembler Service - Async Version

Assembles trading signals from multiple sources (events, ML predictions, technical indicators).
Implements exponential decay for event signals with 24-hour half-life.
"""
import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import numpy as np
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import (
    AITradingSignal,
    AIEventClassification,
    AIRegimeDetection,
    AIActiveStrategy,
)
from app.core.redis import PubSubClient, RedisChannels
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.ml.inference.feature_loader import FeatureLoader

logger = logging.getLogger(__name__)


class SignalAssembler:
    """Assembles trading signals from multiple sources with exponential decay."""

    def __init__(
        self,
        ensemble_predictor: Optional[EnsemblePredictor] = None,
        feature_loader: Optional[FeatureLoader] = None,
        event_decay_half_life_hours: float = 24.0,
        event_weight: float = 0.4,
        ml_weight: float = 0.4,
        technical_weight: float = 0.2,
    ):
        self.ensemble_predictor = ensemble_predictor
        self.feature_loader = feature_loader
        self.event_decay_half_life_hours = event_decay_half_life_hours
        self.event_weight = event_weight
        self.ml_weight = ml_weight
        self.technical_weight = technical_weight

        total_weight = event_weight + ml_weight + technical_weight
        if not math.isclose(total_weight, 1.0, rel_tol=1e-5):
            raise ValueError(f"Signal weights must sum to 1.0, got {total_weight}")

    def calculate_event_decay(self, age_hours: float, half_life_hours: Optional[float] = None) -> float:
        """Calculate exponential decay factor: 0.5 ^ (age_hours / half_life_hours)"""
        if age_hours < 0:
            raise ValueError(f"Event age cannot be negative: {age_hours}")
        half_life = half_life_hours or self.event_decay_half_life_hours
        return math.pow(0.5, age_hours / half_life)

    async def gather_event_signals(
        self,
        db: AsyncSession,
        symbol: str,
        lookback_hours: int = 48,
    ) -> dict[str, Any]:
        """Gather event signals with decay for a symbol."""
        cutoff_time = datetime.utcnow() - timedelta(hours=lookback_hours)

        stmt = (
            select(AIEventClassification)
            .where(
                and_(
                    AIEventClassification.affected_symbols.contains([symbol]),
                    AIEventClassification.created_at >= cutoff_time,
                )
            )
            .order_by(desc(AIEventClassification.created_at))
        )

        result = await db.execute(stmt)
        events = result.scalars().all()

        if not events:
            return {"score": 0.0, "confidence": 0.0, "event_count": 0}

        total_score = 0.0
        total_confidence = 0.0
        now = datetime.now(timezone.utc)

        for event in events:
            age_hours = (now - event.created_at).total_seconds() / 3600
            decay_factor = self.calculate_event_decay(age_hours, event.decay_half_life_hours)
            weighted_score = float(event.impact_score) * decay_factor
            total_score += weighted_score
            total_confidence += float(event.classification_confidence) * decay_factor

        avg_confidence = total_confidence / len(events) if events else 0.0

        return {
            "score": total_score,
            "confidence": avg_confidence,
            "event_count": len(events),
            "events": [{"id": e.id, "type": e.event_type, "impact": float(e.impact_score)} for e in events[:5]],
        }

    async def gather_ml_signals(
        self,
        db: AsyncSession,
        symbol: str,
        timeframe: str = "1d",
    ) -> dict[str, Any]:
        """
        Gather ML prediction signals for a symbol.
        
        Uses ensemble predictor with automatic feature loading.
        
        Args:
            db: Database session
            symbol: Stock symbol
            timeframe: Timeframe for prediction
            
        Returns:
            Dictionary with score, confidence, and prediction details
        """
        if not self.ensemble_predictor or not self.feature_loader:
            logger.debug(f"ML prediction disabled (predictor or loader not initialized)")
            return {"score": 0.0, "confidence": 0.0, "model": None}

        try:
            # Load features (3-tier: Redis → DB → Compute)
            tabular, sequence, current_price, volatility = await self.feature_loader.load_features(
                symbol=symbol,
                timeframe=timeframe,
            )

            # Run ensemble prediction
            prediction = await self.ensemble_predictor.predict(
                features_tabular=tabular,
                features_sequence=sequence,
                symbol=symbol,
                current_price=current_price,
                volatility=volatility,
                timeframe=timeframe,
                use_cache=True,
            )

            # Convert direction to score: BUY=+100, SELL=-100, HOLD=0
            direction_map = {"BUY": 100.0, "SELL": -100.0, "HOLD": 0.0}
            direction = prediction.get("direction_label", "HOLD")
            score = direction_map.get(direction, 0.0)

            return {
                "score": score,
                "confidence": prediction.get("confidence", 0.0),
                "model": prediction.get("metadata", {}).get("model_version"),
                "prediction": {
                    "direction": direction,
                    "entry_price": prediction.get("entry_price"),
                    "stop_loss": prediction.get("stop_loss"),
                    "targets": [
                        prediction.get("tp1"),
                        prediction.get("tp2"),
                        prediction.get("tp3"),
                    ],
                    "probabilities": prediction.get("probabilities"),
                },
            }

        except Exception as e:
            logger.error(f"ML prediction failed for {symbol}: {e}", exc_info=True)
            return {"score": 0.0, "confidence": 0.0, "model": None, "error": str(e)}

    async def gather_technical_signals(
        self,
        db: AsyncSession,
        symbol: str,
    ) -> dict[str, Any]:
        """Gather technical indicator signals for a symbol."""
        # Placeholder - will be integrated with technical analysis in Phase 5
        return {"score": 0.0, "confidence": 0.0, "indicators": {}}

    def fuse_signals(
        self,
        event_signals: dict[str, Any],
        ml_signals: dict[str, Any],
        technical_signals: dict[str, Any],
    ) -> dict[str, Any]:
        """Fuse signals from multiple sources with weighted average."""
        event_score = event_signals.get("score", 0.0)
        ml_score = ml_signals.get("score", 0.0)
        technical_score = technical_signals.get("score", 0.0)

        fused_score = (
            self.event_weight * event_score
            + self.ml_weight * ml_score
            + self.technical_weight * technical_score
        )

        event_conf = event_signals.get("confidence", 0.0)
        ml_conf = ml_signals.get("confidence", 0.0)
        technical_conf = technical_signals.get("confidence", 0.0)

        fused_confidence = (
            self.event_weight * event_conf
            + self.ml_weight * ml_conf
            + self.technical_weight * technical_conf
        )

        action = "HOLD"
        if fused_score > 50:
            action = "BUY"
        elif fused_score < -50:
            action = "SELL"

        return {
            "action": action,
            "confidence_score": fused_confidence,
            "fused_score": fused_score,
            "contributing_events": event_signals.get("events", []),
            "ml_predictions": ml_signals,
            "technical_indicators": technical_signals,
        }

    async def publish_signal_to_redis(
        self,
        pubsub: PubSubClient,
        symbol: str,
        signal_data: dict[str, Any],
    ) -> None:
        """Publish signal to Redis pub/sub channels."""
        await pubsub.publish_json(RedisChannels.signals_for_symbol(symbol), signal_data)
        await pubsub.publish_json(RedisChannels.SIGNALS_ALL, signal_data)

    async def assemble_signal(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        symbol: str,
        feature_vector: Optional[np.ndarray] = None,
        timeframe: str = "1d",
    ) -> AITradingSignal:
        """
        Assemble and persist a trading signal for a symbol.
        
        Args:
            db: Database session
            pubsub: Redis pub/sub client
            symbol: Stock symbol
            feature_vector: Pre-computed feature vector for ML prediction
            timeframe: Timeframe for prediction
            
        Returns:
            Persisted AITradingSignal
        """
        regime = await self.get_latest_regime(db)
        strategy_id = await self.get_active_strategy(db, regime) if regime else None

        event_signals = await self.gather_event_signals(db, symbol)
        ml_signals = await self.gather_ml_signals(db, symbol, timeframe)
        technical_signals = await self.gather_technical_signals(db, symbol)

        fused = self.fuse_signals(event_signals, ml_signals, technical_signals)

        signal = AITradingSignal(
            signal_timestamp=datetime.utcnow(),
            symbol=symbol,
            action=fused["action"],
            confidence_score=Decimal(str(fused["confidence_score"])),
            strategy_id=strategy_id or "default",
            regime_type=regime or "unknown",
            contributing_events=fused["contributing_events"],
            ml_predictions=fused["ml_predictions"],
            technical_indicators=fused["technical_indicators"],
            reasoning=f"Fused signal: {fused['fused_score']:.2f}",
        )

        db.add(signal)
        await db.commit()
        await db.refresh(signal)

        await self.publish_signal_to_redis(pubsub, symbol, {
            "signal_id": signal.id,
            "symbol": symbol,
            "action": signal.action,
            "confidence": float(signal.confidence_score),
            "timestamp": signal.signal_timestamp.isoformat(),
        })

        logger.info(f"Assembled signal for {symbol}: {signal.action} (confidence: {signal.confidence_score})")
        return signal

    async def get_latest_regime(self, db: AsyncSession) -> Optional[str]:
        """Get the latest detected market regime."""
        stmt = select(AIRegimeDetection).order_by(desc(AIRegimeDetection.detection_timestamp)).limit(1)
        result = await db.execute(stmt)
        regime = result.scalar_one_or_none()
        return regime.regime_type if regime else None

    async def get_active_strategy(self, db: AsyncSession, regime_type: str) -> Optional[str]:
        """Get active strategy for the current regime."""
        stmt = (
            select(AIActiveStrategy)
            .where(
                and_(
                    AIActiveStrategy.regime_type == regime_type,
                    AIActiveStrategy.status == "active",
                )
            )
            .order_by(desc(AIActiveStrategy.priority_level))
            .limit(1)
        )
        result = await db.execute(stmt)
        strategy = result.scalar_one_or_none()
        return strategy.strategy_id if strategy else None
