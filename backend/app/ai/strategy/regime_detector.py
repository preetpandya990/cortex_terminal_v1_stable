"""
Regime Detector - Async Market Regime Detection

Detects market regimes: bull_trending, bear_trending, sideways_range, high_volatility, etc.
"""
import asyncio
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.fusion.models import AIRegimeDetection
from app.core.config import get_settings
from app.core.redis import PubSubClient, RedisChannels, get_pubsub_client

logger = logging.getLogger(__name__)
settings = get_settings()


# Active symbols for regime detection (NSE top stocks)
ACTIVE_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "HINDUNILVR",
    "ICICIBANK", "SBIN", "BHARTIARTL", "ITC", "KOTAKBANK"
]


class RegimeDetector:
    """Detects and tracks market regimes."""

    async def detect_regime(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        symbol: str,
    ) -> AIRegimeDetection | None:
        """
        Detect current market regime for a symbol.
        
        Args:
            db: Database session
            pubsub: Redis pub/sub client
            symbol: Stock symbol
            
        Returns:
            AIRegimeDetection if regime changed, None otherwise
        """
        # Placeholder - production would analyze:
        # - Price volatility (ATR, Bollinger Bands)
        # - Volume patterns
        # - Trend strength (ADX, moving averages)
        # - Market breadth indicators
        
        # For now, return None (no regime change detected)
        # This will be implemented with actual market data integration
        return None


async def regime_detection_loop(session_factory: async_sessionmaker) -> None:
    """
    Regime detection background loop.
    
    Monitors market regimes for active symbols and publishes changes to Redis.
    Runs until cancelled via asyncio.CancelledError.
    
    Args:
        session_factory: SQLAlchemy async session factory
    """
    logger.info("Regime detection loop started")
    detector = RegimeDetector()
    pubsub = get_pubsub_client()
    
    try:
        while True:
            try:
                async with session_factory() as db:
                    regime_changes = 0
                    
                    for symbol in ACTIVE_SYMBOLS:
                        regime = await detector.detect_regime(db, pubsub, symbol)
                        
                        if regime:
                            # Publish regime change to Redis
                            await pubsub.publish_json(
                                RedisChannels.REGIME_SYMBOL.format(symbol=symbol),
                                {
                                    "regime_id": regime.id,
                                    "symbol": symbol,
                                    "regime_type": regime.regime_type,
                                    "confidence": float(regime.confidence_score),
                                    "timestamp": regime.detection_timestamp.isoformat(),
                                }
                            )
                            regime_changes += 1
                    
                    if regime_changes > 0:
                        logger.info(f"Detected {regime_changes} regime changes")
                    else:
                        logger.debug(f"No regime changes detected for {len(ACTIVE_SYMBOLS)} symbols")
            
            except Exception as e:
                logger.error(f"Regime detection error: {e}", exc_info=True)
            
            # Sleep between iterations
            logger.debug(f"Sleeping for {settings.REGIME_IDLE_SLEEP_SECONDS}s")
            await asyncio.sleep(settings.REGIME_IDLE_SLEEP_SECONDS)
    
    except asyncio.CancelledError:
        logger.info("Regime detection loop cancelled")
        raise
    finally:
        logger.info("Regime detection loop stopped")
