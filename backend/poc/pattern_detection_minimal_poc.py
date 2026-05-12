"""
Pattern Detection POC — Minimal (NumPy Only)
=============================================
Validates data access and basic pattern detection logic.
Demonstrates production-grade code structure without TA-Lib dependency.

This POC proves:
1. OHLCV data access from database works
2. Pattern detection logic is sound
3. Performance is measurable
4. Code structure is production-ready

Next step: Install TA-Lib for 60+ patterns at 200,000+ candles/sec.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Load environment variables BEFORE importing app modules
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


@dataclass
class PatternDetection:
    """Pattern detection result."""
    pattern_name: str
    direction: str
    timestamp: datetime
    close: float
    confidence: float


@dataclass
class PerformanceMetrics:
    """Performance metrics."""
    total_candles: int
    patterns_detected: int
    latency_ms: float
    candles_per_second: float


class MinimalPatternEngine:
    """
    Minimal pattern detection engine using pure NumPy.
    
    Implements 3 basic patterns to validate approach:
    - Doji: open ≈ close (indecision)
    - Hammer: long lower shadow (bullish reversal)
    - Engulfing: current candle engulfs previous (reversal)
    """
    
    @staticmethod
    def detect_doji(
        open_p: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
    ) -> np.ndarray:
        """Detect Doji pattern (open ≈ close)."""
        body = np.abs(close - open_p)
        range_hl = high - low
        # Doji: body < 10% of range
        return (body / (range_hl + 1e-10) < 0.1) & (range_hl > 0)
    
    @staticmethod
    def detect_hammer(
        open_p: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
    ) -> np.ndarray:
        """Detect Hammer pattern (long lower shadow)."""
        body = np.abs(close - open_p)
        lower_shadow = np.minimum(open_p, close) - low
        upper_shadow = high - np.maximum(open_p, close)
        # Hammer: lower shadow > 2x body, upper shadow < body
        return (lower_shadow > 2 * body) & (upper_shadow < body)
    
    @staticmethod
    def detect_bullish_engulfing(
        open_p: np.ndarray,
        close: np.ndarray,
    ) -> np.ndarray:
        """Detect Bullish Engulfing pattern."""
        # Previous candle bearish
        prev_bearish = np.roll(close, 1) < np.roll(open_p, 1)
        # Current candle bullish
        curr_bullish = close > open_p
        # Current engulfs previous
        engulfs = (open_p < np.roll(close, 1)) & (close > np.roll(open_p, 1))
        # Combine conditions (skip first candle)
        result = prev_bearish & curr_bullish & engulfs
        result[0] = False  # Can't detect on first candle
        return result
    
    def detect_patterns(
        self,
        open_p: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        timestamps: list[datetime],
    ) -> list[PatternDetection]:
        """Detect all patterns."""
        detections = []
        
        # Doji
        doji_mask = self.detect_doji(open_p, high, low, close)
        for i in np.where(doji_mask)[0]:
            detections.append(PatternDetection(
                pattern_name="DOJI",
                direction="NEUTRAL",
                timestamp=timestamps[i],
                close=float(close[i]),
                confidence=0.8,
            ))
        
        # Hammer
        hammer_mask = self.detect_hammer(open_p, high, low, close)
        for i in np.where(hammer_mask)[0]:
            detections.append(PatternDetection(
                pattern_name="HAMMER",
                direction="BULLISH",
                timestamp=timestamps[i],
                close=float(close[i]),
                confidence=0.85,
            ))
        
        # Bullish Engulfing
        engulfing_mask = self.detect_bullish_engulfing(open_p, close)
        for i in np.where(engulfing_mask)[0]:
            detections.append(PatternDetection(
                pattern_name="BULLISH_ENGULFING",
                direction="BULLISH",
                timestamp=timestamps[i],
                close=float(close[i]),
                confidence=0.9,
            ))
        
        return sorted(detections, key=lambda x: x.timestamp)


async def fetch_ohlcv_data(
    db: AsyncSession,
    instrument_key: str,
    days: int = 365,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[datetime]]:
    """Fetch OHLCV data from database."""
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
    
    logger.info(f"✓ Fetched {len(rows)} candles for {instrument_key}")
    
    return open_prices, high_prices, low_prices, close_prices, timestamps


async def run_poc(db: AsyncSession, instrument_key: str) -> PerformanceMetrics:
    """Run pattern detection POC."""
    logger.info("=" * 80)
    logger.info("PATTERN DETECTION POC — Minimal (NumPy Only)")
    logger.info("=" * 80)
    logger.info("")
    
    # Initialize
    engine = MinimalPatternEngine()
    
    # Fetch data
    logger.info(f"Fetching 1 year OHLCV data for {instrument_key}...")
    open_p, high, low, close, timestamps = await fetch_ohlcv_data(db, instrument_key, days=365)
    logger.info("")
    
    # Detect patterns
    logger.info("Running pattern detection (3 patterns: Doji, Hammer, Bullish Engulfing)...")
    start_time = time.perf_counter()
    
    detections = engine.detect_patterns(open_p, high, low, close, timestamps)
    
    end_time = time.perf_counter()
    latency_ms = (end_time - start_time) * 1000
    candles_per_sec = len(timestamps) / (end_time - start_time) if (end_time - start_time) > 0 else 0
    
    logger.info(f"✓ Detection complete")
    logger.info("")
    
    # Results
    logger.info("=" * 80)
    logger.info("RESULTS")
    logger.info("=" * 80)
    logger.info(f"Total candles:        {len(timestamps):,}")
    logger.info(f"Patterns detected:    {len(detections)}")
    logger.info(f"Latency:              {latency_ms:.2f}ms")
    logger.info(f"Throughput:           {candles_per_sec:,.0f} candles/second")
    logger.info("")
    
    # Sample detections
    if detections:
        logger.info("Sample Pattern Detections (last 10):")
        logger.info("-" * 80)
        for det in detections[-10:]:
            logger.info(
                f"  {det.timestamp.date()} | {det.pattern_name:20s} | {det.direction:8s} | "
                f"Confidence: {det.confidence:.0%} | Close: ₹{det.close:.2f}"
            )
    else:
        logger.warning("⚠️  No patterns detected in the data")
    
    logger.info("=" * 80)
    logger.info("")
    
    return PerformanceMetrics(
        total_candles=len(timestamps),
        patterns_detected=len(detections),
        latency_ms=latency_ms,
        candles_per_second=candles_per_sec,
    )


async def main():
    """Main entry point."""
    # Test instrument: Reliance Industries (most liquid NSE stock)
    instrument_key = "NSE_EQ|INE002A01018"
    
    async with AsyncSessionLocal() as db:
        try:
            metrics = await run_poc(db, instrument_key)
            
            # Assessment
            logger.info("=" * 80)
            logger.info("POC ASSESSMENT")
            logger.info("=" * 80)
            
            passed = True
            
            if metrics.latency_ms < 100:
                logger.info("✅ PASS: Latency < 100ms")
            else:
                logger.warning(f"⚠️  WARN: Latency {metrics.latency_ms:.2f}ms (target: <100ms)")
            
            if metrics.patterns_detected > 0:
                logger.info("✅ PASS: Patterns detected")
            else:
                logger.warning("⚠️  WARN: No patterns detected")
            
            logger.info("")
            logger.info("📊 Current Implementation: 3 patterns (NumPy)")
            logger.info("🚀 With TA-Lib: 60+ patterns at 200,000+ candles/sec")
            logger.info("")
            logger.info("Next Steps:")
            logger.info("  1. Install TA-Lib (see poc/TALIB_INSTALLATION.md)")
            logger.info("  2. Run poc/pattern_detection_poc.py")
            logger.info("  3. Validate 60+ patterns with <10ms latency")
            logger.info("=" * 80)
        
        except Exception as e:
            logger.error(f"❌ POC failed: {e}", exc_info=True)
            raise


if __name__ == "__main__":
    asyncio.run(main())
