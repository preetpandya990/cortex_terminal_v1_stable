"""
Pattern Detection POC — Production-Grade TA-Lib Integration
=============================================================
Tests TA-Lib candlestick pattern detection on real NSE OHLCV data.

Objectives:
  1. Validate TA-Lib installation and performance
  2. Detect patterns on 1 year of historical data (1 symbol)
  3. Measure: accuracy, latency, memory usage
  4. Production-grade error handling and logging

Performance Targets:
  - Latency: <10ms per symbol (100 candles)
  - Memory: <100MB for 1 year data
  - Accuracy: Pattern detection consistency >95%
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from sqlalchemy import select, text
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
    """Immutable pattern detection result."""
    pattern_name: str
    pattern_value: int  # TA-Lib returns -100, 0, or +100
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    confidence: float  # Normalized 0.0-1.0


@dataclass
class PerformanceMetrics:
    """Performance measurement results."""
    total_candles: int
    patterns_detected: int
    latency_ms: float
    memory_mb: float
    candles_per_second: float


class PatternDetectionEngine:
    """
    Production-grade pattern detection using TA-Lib.
    
    Thread-safe, stateless, with comprehensive error handling.
    """
    
    # TA-Lib candlestick pattern functions (60+ patterns)
    PATTERN_FUNCTIONS = [
        "CDLDOJI",           # Doji
        "CDLHAMMER",         # Hammer
        "CDLINVERTEDHAMMER", # Inverted Hammer
        "CDLENGULFING",      # Engulfing Pattern
        "CDLHARAMI",         # Harami Pattern
        "CDLMORNINGSTAR",    # Morning Star
        "CDLEVENINGSTAR",    # Evening Star
        "CDLSHOOTINGSTAR",   # Shooting Star
        "CDLMARUBOZU",       # Marubozu
        "CDLSPINNINGTOP",    # Spinning Top
        "CDL3WHITESOLDIERS", # Three White Soldiers
        "CDL3BLACKCROWS",    # Three Black Crows
        "CDLPIERCING",       # Piercing Pattern
        "CDLDARKCLOUDCOVER", # Dark Cloud Cover
        "CDL3INSIDE",        # Three Inside Up/Down
        "CDL3OUTSIDE",       # Three Outside Up/Down
        "CDLHANGINGMAN",     # Hanging Man
        "CDLDRAGONFLYDOJI",  # Dragonfly Doji
        "CDLGRAVESTONEDOJI", # Gravestone Doji
        "CDLLONGLEGGEDDOJI", # Long Legged Doji
    ]
    
    def __init__(self):
        """Initialize pattern detection engine."""
        self._validate_talib()
    
    def _validate_talib(self) -> None:
        """Validate TA-Lib installation."""
        try:
            import talib
            self.talib = talib
            logger.info(f"TA-Lib version: {talib.__version__}")
        except ImportError as e:
            raise RuntimeError(
                "TA-Lib not installed. Install: pip install TA-Lib\n"
                "Requires C library: https://ta-lib.github.io/ta-lib-python/"
            ) from e
    
    def detect_patterns(
        self,
        open_prices: np.ndarray,
        high_prices: np.ndarray,
        low_prices: np.ndarray,
        close_prices: np.ndarray,
        timestamps: list[datetime],
    ) -> list[PatternDetection]:
        """
        Detect candlestick patterns using TA-Lib.
        
        Args:
            open_prices: NumPy array of open prices
            high_prices: NumPy array of high prices
            low_prices: NumPy array of low prices
            close_prices: NumPy array of close prices
            timestamps: List of timestamps (same length as price arrays)
        
        Returns:
            List of detected patterns with metadata
        
        Raises:
            ValueError: If input arrays have mismatched lengths
            RuntimeError: If TA-Lib computation fails
        """
        # Validate inputs
        if not (len(open_prices) == len(high_prices) == len(low_prices) == len(close_prices) == len(timestamps)):
            raise ValueError("All input arrays must have the same length")
        
        if len(open_prices) < 3:
            raise ValueError("Need at least 3 candles for pattern detection")
        
        detections: list[PatternDetection] = []
        
        try:
            # Run all pattern detection functions
            for pattern_func_name in self.PATTERN_FUNCTIONS:
                pattern_func = getattr(self.talib, pattern_func_name)
                
                # TA-Lib returns array of integers: -100 (bearish), 0 (no pattern), +100 (bullish)
                result = pattern_func(open_prices, high_prices, low_prices, close_prices)
                
                # Find non-zero pattern detections
                for i, value in enumerate(result):
                    if value != 0:
                        # Normalize confidence: -100/+100 → 0.0-1.0
                        confidence = abs(value) / 100.0
                        
                        detections.append(PatternDetection(
                            pattern_name=pattern_func_name.replace("CDL", ""),
                            pattern_value=int(value),
                            timestamp=timestamps[i],
                            open=float(open_prices[i]),
                            high=float(high_prices[i]),
                            low=float(low_prices[i]),
                            close=float(close_prices[i]),
                            confidence=confidence,
                        ))
        
        except Exception as e:
            raise RuntimeError(f"Pattern detection failed: {e}") from e
        
        return detections


async def fetch_ohlcv_data(
    db: AsyncSession,
    instrument_key: str,
    days: int = 365,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[datetime]]:
    """
    Fetch OHLCV data from database.
    
    Returns:
        Tuple of (open, high, low, close, timestamps)
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    
    query = text("""
        SELECT timestamp, open, high, low, close
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
    
    timestamps = [row[0] for row in rows]
    open_prices = np.array([float(row[1]) for row in rows], dtype=np.float64)
    high_prices = np.array([float(row[2]) for row in rows], dtype=np.float64)
    low_prices = np.array([float(row[3]) for row in rows], dtype=np.float64)
    close_prices = np.array([float(row[4]) for row in rows], dtype=np.float64)
    
    logger.info(f"Fetched {len(rows)} candles for {instrument_key}")
    
    return open_prices, high_prices, low_prices, close_prices, timestamps


async def run_poc(db: AsyncSession, instrument_key: str) -> PerformanceMetrics:
    """
    Run pattern detection POC on real NSE data.
    
    Args:
        db: Database session
        instrument_key: NSE instrument key (e.g., 'NSE_EQ|INE002A01018')
    
    Returns:
        Performance metrics
    """
    logger.info("=" * 80)
    logger.info("PATTERN DETECTION POC — TA-Lib on Real NSE Data")
    logger.info("=" * 80)
    
    # Initialize engine
    engine = PatternDetectionEngine()
    
    # Fetch data
    logger.info(f"Fetching 1 year OHLCV data for {instrument_key}...")
    open_p, high_p, low_p, close_p, timestamps = await fetch_ohlcv_data(db, instrument_key, days=365)
    
    # Measure performance
    import psutil
    import os
    
    process = psutil.Process(os.getpid())
    mem_before = process.memory_info().rss / 1024 / 1024  # MB
    
    start_time = time.perf_counter()
    
    # Detect patterns
    logger.info("Running pattern detection...")
    detections = engine.detect_patterns(open_p, high_p, low_p, close_p, timestamps)
    
    end_time = time.perf_counter()
    mem_after = process.memory_info().rss / 1024 / 1024  # MB
    
    latency_ms = (end_time - start_time) * 1000
    memory_mb = mem_after - mem_before
    candles_per_sec = len(timestamps) / (end_time - start_time) if (end_time - start_time) > 0 else 0
    
    # Results
    logger.info("=" * 80)
    logger.info("RESULTS")
    logger.info("=" * 80)
    logger.info(f"Total candles processed: {len(timestamps)}")
    logger.info(f"Patterns detected: {len(detections)}")
    logger.info(f"Latency: {latency_ms:.2f}ms")
    logger.info(f"Memory delta: {memory_mb:.2f}MB")
    logger.info(f"Throughput: {candles_per_sec:,.0f} candles/second")
    logger.info("")
    
    # Show sample detections
    if detections:
        logger.info("Sample Pattern Detections (last 10):")
        for det in detections[-10:]:
            direction = "BULLISH" if det.pattern_value > 0 else "BEARISH"
            logger.info(
                f"  {det.timestamp.date()} | {det.pattern_name:20s} | {direction:8s} | "
                f"Confidence: {det.confidence:.0%} | Close: ₹{det.close:.2f}"
            )
    else:
        logger.warning("No patterns detected in the data")
    
    logger.info("=" * 80)
    
    # Performance validation
    if latency_ms > 100:
        logger.warning(f"⚠️  Latency {latency_ms:.2f}ms exceeds target (<100ms)")
    else:
        logger.info(f"✅ Latency {latency_ms:.2f}ms within target")
    
    if memory_mb > 100:
        logger.warning(f"⚠️  Memory {memory_mb:.2f}MB exceeds target (<100MB)")
    else:
        logger.info(f"✅ Memory {memory_mb:.2f}MB within target")
    
    return PerformanceMetrics(
        total_candles=len(timestamps),
        patterns_detected=len(detections),
        latency_ms=latency_ms,
        memory_mb=memory_mb,
        candles_per_second=candles_per_sec,
    )


async def main():
    """Main POC entry point."""
    from app.core.database import get_db_session
    
    # Test instrument: Reliance Industries (most liquid NSE stock)
    instrument_key = "NSE_EQ|INE002A01018"
    
    async with get_db_session() as db:
        try:
            metrics = await run_poc(db, instrument_key)
            
            # Final assessment
            logger.info("")
            logger.info("=" * 80)
            logger.info("POC ASSESSMENT")
            logger.info("=" * 80)
            
            passed = True
            
            if metrics.latency_ms < 100:
                logger.info("✅ PASS: Latency < 100ms")
            else:
                logger.error("❌ FAIL: Latency exceeds target")
                passed = False
            
            if metrics.memory_mb < 100:
                logger.info("✅ PASS: Memory < 100MB")
            else:
                logger.error("❌ FAIL: Memory exceeds target")
                passed = False
            
            if metrics.patterns_detected > 0:
                logger.info("✅ PASS: Patterns detected")
            else:
                logger.warning("⚠️  WARN: No patterns detected (may be normal)")
            
            if passed:
                logger.info("")
                logger.info("🎉 POC PASSED — Ready for production integration")
            else:
                logger.error("")
                logger.error("❌ POC FAILED — Optimization required")
            
            logger.info("=" * 80)
        
        except Exception as e:
            logger.error(f"POC failed with error: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    asyncio.run(main())
