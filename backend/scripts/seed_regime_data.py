"""
Seed script to populate regime detection data for development
"""
import asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.ai.fusion.models import AIRegimeDetection
import random

DATABASE_URL = "postgresql+asyncpg://cortex_user:cortex_pass@localhost:5432/cortex_db"

async def seed_regime_data():
    engine = create_async_engine(DATABASE_URL, echo=True)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    regime_types = [
        "bull_trending",
        "bear_trending", 
        "sideways_range",
        "high_volatility",
        "low_liquidity",
        "news_driven"
    ]
    
    symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK"]
    
    async with async_session() as session:
        # Create regime detections for the last 7 days
        now = datetime.now(timezone.utc)
        
        for days_ago in range(7):
            for hour in range(0, 24, 4):  # Every 4 hours
                timestamp = now - timedelta(days=days_ago, hours=hour)
                
                for symbol in symbols:
                    regime_type = random.choice(regime_types)
                    confidence = random.uniform(0.7, 0.95)
                    
                    regime = AIRegimeDetection(
                        symbol=symbol,
                        regime_type=regime_type,
                        confidence_score=confidence,
                        detection_timestamp=timestamp,
                        indicators={
                            "adx": random.uniform(20, 50),
                            "atr": random.uniform(0.5, 3.0),
                            "bollinger_width": random.uniform(0.02, 0.08),
                            "rsi": random.uniform(30, 70),
                        },
                        metadata={
                            "model_version": "1.0.0",
                            "features_used": ["price", "volume", "volatility"]
                        }
                    )
                    session.add(regime)
        
        await session.commit()
        print(f"✅ Created {len(symbols) * 7 * 6} regime detection records")
    
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(seed_regime_data())
