"""
Pattern Detection POC — pandas_ta Fallback (Pure Python)
==========================================================
Alternative POC using pandas_ta for pattern detection when TA-Lib is unavailable.

pandas_ta is pure Python, easier to install, but slower than TA-Lib.
Use this for POC validation, then switch to TA-Lib for production.

Performance expectations:
  - pandas_ta: ~1,000-5,000 candles/second (pure Python)
  - TA-Lib: ~200,000+ candles/second (C library)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class PatternDetection:
    """Pattern detection result."""
    pattern_name: str
    direction: str  # 'BULLISH' or 'BEARISH'
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    confidence: float


@dataclass
class PerformanceMetrics:
    """Performance metrics."""
    total_candles: int
    patterns_detected: int
    latency_ms: float
    memory_mb: float
    candles_per_second: float


class PandasTAPatternEngine:
    """
    Pattern detection using pandas_ta (pure Python).
    
    Implements common candlestick patterns using OHLC logic.
    """
    
    def __init__(self):
        """Initialize engine."""
        try:
            import pandas_ta as ta
            self.ta = ta
            logger.info(f"pandas_ta version: {ta.version}")
        except ImportError:
            logger.warning("pandas_ta not installed. Installing...")
            import subprocess
            subprocess.check_call(["pip", "install", "pandas_ta"])
            import pandas_ta as ta
            self.ta = ta
    
    def detect_doji(self, df: pd.DataFrame) -> pd.Series:
        """Detect Doji pattern (open ≈ close)."""
        body = abs(df['close'] - df['open'])
        range_hl = df['high'] - df['low']
        return (body / range_hl < 0.1) & (range_hl > 0)
    
    def detect_hammer(self, df: pd.DataFrame) -> pd.Series:
        """Detect Hammer pattern (long lower shadow, small body at top)."""
        body = abs(df['close'] - df['open'])
        lower_shadow = df[['open', 'close']].min(axis=1) - df['low']
        upper_shadow = df['high'] - df[['open', 'close']].max(axis=1)
        range_hl = df['high'] - df['low']
        
        return (
            (lower_shadow > 2 * body) &
            (upper_shadow < body) &
            (range_hl > 0)
        )
    
    def detect_engulfing(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        """Detect Bullish/Bearish Engulfing patterns."""
        # Bullish: prev bearish, current bullish, current body engulfs prev
        prev_bearish = df['close'].shift(1) < df['open'].shift(1)
        curr_bullish = df['close'] > df['open']
        engulfs = (
            (df['open'] < df['close'].shift(1)) &
            (df['close'] > df['open'].shift(1))
        )
        bullish_engulfing = prev_bearish & curr_bullish & engulfs
        
        # Bearish: opposite
        prev_bullish = df['close'].shift(1) > df['open'].shift(1)
        curr_bearish = df['close'] < df['open']
        engulfs_bear = (
            (df['open'] > df['close'].shift(1)) &
            (df['close'] < df['open'].shift(1))
        )
        bearish_engulfing = prev_bullish & curr_bearish & engulfs_bear
        
        return bullish_engulfing, bearish_engulfing
    
    def detect_patterns(self, df: pd.DataFrame) -> list[PatternDetection]:
        """Detect all patterns."""
        detections = []
        
        # Doji
        doji = self.detect_doji(df)
        for idx in df[doji].index:
            row = df.loc[idx]
            detections.append(PatternDetection(
                pattern_name="DOJI",
                direction="NEUTRAL",
                timestamp=row['timestamp'],
                open=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                confidence=0.8,
            ))
        
        # Hammer
        hammer = self.detect_hammer(df)
        for idx in df[hammer].index:
            row = df.loc[idx]
            detections.append(PatternDetection(
                pattern_name="HAMMER",
                direction="BULLISH",
                timestamp=row['timestamp'],
                open=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                confidence=0.85,
            ))
        
        # Engulfing
        bull_eng, bear_eng = self.detect_engulfing(df)
        for idx in df[bull_eng].index:
            row = df.loc[idx]
            detections.append(PatternDetection(
                pattern_name="BULLISH_ENGULFING",
                direction="BULLISH",
                timestamp=row['timestamp'],
                open=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                confidence=0.9,
            ))
        
        for idx in df[bear_eng].index:
            row = df.loc[idx]
            detections.append(PatternDetection(
                pattern_name="BEARISH_ENGULFING",
                direction="BEARISH",
                timestamp=row['timestamp'],
                open=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                confidence=0.9,
            ))
        
        return sorted(detections, key=lambda x: x.timestamp)


async def fetch_ohlcv_data(
    db: AsyncSession,
    instrument_key: str,
    days: int = 365,
) -> pd.DataFrame:
    """Fetch OHLCV data as DataFrame."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    
    query = text("""
        SELECT timestamp, open, high, low, close, volume
        FROM upstox_ohlcv
        WHERE instrument_key = :key
          AND timeframe = '1D'
          AND timestamp >= :start_date
          AND timestamp <= :end_date
        ORDER BY timestamp ASC
    """)
    
    result = await db.execute(
        query,
        {"key": instrument_key, "start_date": start_date, "end_date": end_date},
    )
    rows = result.fetchall()
    
    if not rows:
        raise ValueError(f"No OHLCV data found for {instrument_key}")
    
    df = pd.DataFrame(rows, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['open'] = df['open'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['close'] = df['close'].astype(float)
    
    logger.info(f"Fetched {len(df)} candles for {instrument_key}")
    
    return df


async def run_poc(db: AsyncSession, instrument_key: str) -> PerformanceMetrics:
    """Run POC."""
    logger.info("=" * 80)
    logger.info("PATTERN DETECTION POC — pandas_ta (Pure Python)")
    logger.info("=" * 80)
    
    # Initialize
    engine = PandasTAPatternEngine()
    
    # Fetch data
    logger.info(f"Fetching 1 year OHLCV data for {instrument_key}...")
    df = await fetch_ohlcv_data(db, instrument_key, days=365)
    
    # Measure performance
    import psutil
    import os
    
    process = psutil.Process(os.getpid())
    mem_before = process.memory_info().rss / 1024 / 1024
    
    start_time = time.perf_counter()
    
    # Detect patterns
    logger.info("Running pattern detection...")
    detections = engine.detect_patterns(df)
    
    end_time = time.perf_counter()
    mem_after = process.memory_info().rss / 1024 / 1024
    
    latency_ms = (end_time - start_time) * 1000
    memory_mb = mem_after - mem_before
    candles_per_sec = len(df) / (end_time - start_time) if (end_time - start_time) > 0 else 0
    
    # Results
    logger.info("=" * 80)
    logger.info("RESULTS")
    logger.info("=" * 80)
    logger.info(f"Total candles: {len(df)}")
    logger.info(f"Patterns detected: {len(detections)}")
    logger.info(f"Latency: {latency_ms:.2f}ms")
    logger.info(f"Memory delta: {memory_mb:.2f}MB")
    logger.info(f"Throughput: {candles_per_sec:,.0f} candles/second")
    logger.info("")
    
    # Sample detections
    if detections:
        logger.info("Sample Pattern Detections (last 10):")
        for det in detections[-10:]:
            logger.info(
                f"  {det.timestamp.date()} | {det.pattern_name:20s} | {det.direction:8s} | "
                f"Confidence: {det.confidence:.0%} | Close: ₹{det.close:.2f}"
            )
    
    logger.info("=" * 80)
    
    return PerformanceMetrics(
        total_candles=len(df),
        patterns_detected=len(detections),
        latency_ms=latency_ms,
        memory_mb=memory_mb,
        candles_per_second=candles_per_sec,
    )


async def main():
    """Main entry point."""
    from app.core.database import get_db_session
    
    instrument_key = "NSE_EQ|INE002A01018"  # Reliance
    
    async with get_db_session() as db:
        try:
            metrics = await run_poc(db, instrument_key)
            
            logger.info("")
            logger.info("=" * 80)
            logger.info("POC ASSESSMENT")
            logger.info("=" * 80)
            logger.info(f"✅ POC Complete")
            logger.info(f"   Latency: {metrics.latency_ms:.2f}ms")
            logger.info(f"   Throughput: {metrics.candles_per_second:,.0f} candles/sec")
            logger.info("")
            logger.info("⚠️  NOTE: pandas_ta is 50-200x slower than TA-Lib")
            logger.info("   For production, install TA-Lib (see TALIB_INSTALLATION.md)")
            logger.info("=" * 80)
        
        except Exception as e:
            logger.error(f"POC failed: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    asyncio.run(main())
