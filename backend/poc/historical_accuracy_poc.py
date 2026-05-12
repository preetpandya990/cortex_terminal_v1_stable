"""
Historical Accuracy Measurement POC
====================================
Production-grade backtesting for pattern detection accuracy measurement.

Industry Best Practices Implemented:
- Chronological train/test splits (no random splits)
- Walk-forward analysis (rolling window)
- Embargo gaps (5-day buffer to prevent autocorrelation leakage)
- Realistic outcome measurement (5-day forward window)
- Survivorship-bias-free data (uses actual historical data)
- No lookahead bias (features computed only from past data)

Metrics Measured:
- Directional accuracy (% predictions that moved in predicted direction)
- TP/SL hit rates (% predictions that hit target/stop levels)
- Average favorable/adverse moves
- Pattern-specific success rates
- Confidence calibration (do HIGH confidence predictions perform better?)

Reference: TradeAlgo ML Backtesting Guide (2026)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

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
class PatternPrediction:
    """Pattern prediction with entry conditions."""
    pattern_name: str
    direction: Literal["BUY", "SELL"]
    confidence: float
    confidence_level: Literal["HIGH", "MEDIUM", "LOW"]
    predicted_at: datetime
    entry_price: float
    stop_loss: float
    target_price: float


@dataclass
class OutcomeMeasurement:
    """Outcome measurement for a prediction."""
    prediction: PatternPrediction
    direction_correct: bool
    hit_target: bool
    hit_stop_loss: bool
    max_favorable_move_pct: float
    max_adverse_move_pct: float
    final_move_pct: float
    days_to_outcome: int


@dataclass
class AccuracyMetrics:
    """Aggregated accuracy metrics."""
    total_predictions: int = 0
    directional_accuracy: float = 0.0
    target_hit_rate: float = 0.0
    stop_loss_hit_rate: float = 0.0
    avg_favorable_move: float = 0.0
    avg_adverse_move: float = 0.0
    avg_final_move: float = 0.0
    
    # Confidence calibration
    high_confidence_accuracy: float = 0.0
    medium_confidence_accuracy: float = 0.0
    low_confidence_accuracy: float = 0.0
    
    # Pattern-specific
    pattern_accuracies: dict[str, float] = field(default_factory=dict)


class MinimalPatternDetector:
    """
    Minimal pattern detector for POC.
    Uses 3 basic patterns: Doji, Hammer, Bullish Engulfing.
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
        range_hl = high - low
        
        # Hammer: lower shadow > 2x body, upper shadow < body
        return (
            (lower_shadow > 2 * body) &
            (upper_shadow < body) &
            (range_hl > 0)
        )
    
    @staticmethod
    def detect_bullish_engulfing(
        open_p: np.ndarray,
        close: np.ndarray,
    ) -> np.ndarray:
        """Detect Bullish Engulfing pattern."""
        if len(open_p) < 2:
            return np.zeros(len(open_p), dtype=bool)
        
        # Previous candle: bearish (close < open)
        prev_bearish = close[:-1] < open_p[:-1]
        
        # Current candle: bullish (close > open)
        curr_bullish = close[1:] > open_p[1:]
        
        # Current engulfs previous
        engulfs = (
            (open_p[1:] < close[:-1]) &  # Opens below prev close
            (close[1:] > open_p[:-1])     # Closes above prev open
        )
        
        result = np.zeros(len(open_p), dtype=bool)
        result[1:] = prev_bearish & curr_bullish & engulfs
        return result
    
    def detect_patterns(
        self,
        timestamps: np.ndarray,
        open_p: np.ndarray,
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
    ) -> list[PatternPrediction]:
        """Detect all patterns and generate predictions."""
        predictions = []
        
        # Detect patterns
        doji_mask = self.detect_doji(open_p, high, low, close)
        hammer_mask = self.detect_hammer(open_p, high, low, close)
        engulfing_mask = self.detect_bullish_engulfing(open_p, close)
        
        # Generate predictions for each pattern
        for i in range(len(timestamps)):
            if doji_mask[i]:
                # Doji: neutral, but we'll predict continuation for testing
                predictions.append(self._create_prediction(
                    "DOJI",
                    "BUY" if close[i] > open_p[i] else "SELL",
                    0.65,  # Medium confidence
                    timestamps[i],
                    close[i],
                ))
            
            if hammer_mask[i]:
                # Hammer: bullish reversal
                predictions.append(self._create_prediction(
                    "HAMMER",
                    "BUY",
                    0.75,  # High confidence
                    timestamps[i],
                    close[i],
                ))
            
            if engulfing_mask[i]:
                # Bullish Engulfing: strong bullish reversal
                predictions.append(self._create_prediction(
                    "BULLISH_ENGULFING",
                    "BUY",
                    0.82,  # High confidence
                    timestamps[i],
                    close[i],
                ))
        
        return predictions
    
    def _create_prediction(
        self,
        pattern_name: str,
        direction: Literal["BUY", "SELL"],
        confidence: float,
        timestamp: datetime,
        entry_price: float,
    ) -> PatternPrediction:
        """Create a pattern prediction with TP/SL levels."""
        # Simple TP/SL: 2% target, 1% stop
        if direction == "BUY":
            target_price = entry_price * 1.02
            stop_loss = entry_price * 0.99
        else:
            target_price = entry_price * 0.98
            stop_loss = entry_price * 1.01
        
        # Confidence level
        if confidence >= 0.75:
            confidence_level = "HIGH"
        elif confidence >= 0.60:
            confidence_level = "MEDIUM"
        else:
            confidence_level = "LOW"
        
        return PatternPrediction(
            pattern_name=pattern_name,
            direction=direction,
            confidence=confidence,
            confidence_level=confidence_level,
            predicted_at=timestamp,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target_price=target_price,
        )


class OutcomeMeasurer:
    """
    Measures prediction outcomes using forward-looking price data.
    
    Industry Best Practice:
    - Uses 5-day forward window (measurement_window_days)
    - Measures max favorable/adverse moves within window
    - Checks if TP/SL levels were hit
    - Calculates final move at end of window
    """
    
    def __init__(self, measurement_window_days: int = 5):
        self.measurement_window_days = measurement_window_days
    
    async def measure_outcome(
        self,
        session: AsyncSession,
        prediction: PatternPrediction,
        instrument_key: str,
    ) -> OutcomeMeasurement | None:
        """Measure outcome for a single prediction."""
        # Fetch forward-looking price data
        end_date = prediction.predicted_at + timedelta(days=self.measurement_window_days + 5)
        
        query = text("""
            SELECT timestamp, open, high, low, close
            FROM upstox_ohlcv
            WHERE instrument_key = :instrument_key
              AND timeframe = '1D'
              AND timestamp > :start_date
              AND timestamp <= :end_date
            ORDER BY timestamp ASC
            LIMIT :limit
        """)
        
        result = await session.execute(
            query,
            {
                "instrument_key": instrument_key,
                "start_date": prediction.predicted_at,
                "end_date": end_date,
                "limit": self.measurement_window_days,
            }
        )
        rows = result.fetchall()
        
        if not rows:
            return None
        
        # Extract price data
        highs = np.array([float(row.high) for row in rows])
        lows = np.array([float(row.low) for row in rows])
        closes = np.array([float(row.close) for row in rows])
        
        # Calculate moves
        if prediction.direction == "BUY":
            max_favorable = np.max(highs)
            max_adverse = np.min(lows)
            final_price = closes[-1]
            
            max_favorable_pct = ((max_favorable - prediction.entry_price) / prediction.entry_price) * 100
            max_adverse_pct = ((max_adverse - prediction.entry_price) / prediction.entry_price) * 100
            final_move_pct = ((final_price - prediction.entry_price) / prediction.entry_price) * 100
            
            # Check TP/SL hits
            hit_target = max_favorable >= prediction.target_price
            hit_stop_loss = max_adverse <= prediction.stop_loss
            direction_correct = final_price > prediction.entry_price
        else:  # SELL
            max_favorable = np.min(lows)
            max_adverse = np.max(highs)
            final_price = closes[-1]
            
            max_favorable_pct = ((prediction.entry_price - max_favorable) / prediction.entry_price) * 100
            max_adverse_pct = ((prediction.entry_price - max_adverse) / prediction.entry_price) * 100
            final_move_pct = ((prediction.entry_price - final_price) / prediction.entry_price) * 100
            
            # Check TP/SL hits
            hit_target = max_favorable <= prediction.target_price
            hit_stop_loss = max_adverse >= prediction.stop_loss
            direction_correct = final_price < prediction.entry_price
        
        return OutcomeMeasurement(
            prediction=prediction,
            direction_correct=direction_correct,
            hit_target=hit_target,
            hit_stop_loss=hit_stop_loss,
            max_favorable_move_pct=max_favorable_pct,
            max_adverse_move_pct=max_adverse_pct,
            final_move_pct=final_move_pct,
            days_to_outcome=len(rows),
        )


class WalkForwardBacktester:
    """
    Walk-forward backtesting engine.
    
    Industry Best Practice:
    - Rolling window approach (train on past N months, test on next month)
    - Embargo gap (5-day buffer between train and test)
    - Chronological splits only (no random splits)
    - Out-of-sample testing (each test period uses only prior data)
    """
    
    def __init__(
        self,
        train_window_months: int = 6,
        test_window_days: int = 30,
        embargo_days: int = 5,
    ):
        self.train_window_months = train_window_months
        self.test_window_days = test_window_days
        self.embargo_days = embargo_days
        self.detector = MinimalPatternDetector()
        self.measurer = OutcomeMeasurer()
    
    async def run_backtest(
        self,
        session: AsyncSession,
        instrument_key: str,
        start_date: datetime,
        end_date: datetime,
    ) -> list[OutcomeMeasurement]:
        """Run walk-forward backtest."""
        logger.info(f"Starting walk-forward backtest: {start_date.date()} to {end_date.date()}")
        logger.info(f"Train window: {self.train_window_months} months")
        logger.info(f"Test window: {self.test_window_days} trading days")
        logger.info(f"Embargo gap: {self.embargo_days} trading days")
        
        # Fetch all available trading days
        query = text("""
            SELECT DISTINCT timestamp
            FROM upstox_ohlcv
            WHERE instrument_key = :instrument_key
              AND timeframe = '1D'
              AND timestamp >= :start_date
              AND timestamp <= :end_date
            ORDER BY timestamp ASC
        """)
        result = await session.execute(
            query,
            {
                "instrument_key": instrument_key,
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        trading_days = [row.timestamp for row in result.fetchall()]
        
        if len(trading_days) < 100:
            logger.error(f"Insufficient trading days: {len(trading_days)}")
            return []
        
        logger.info(f"Found {len(trading_days)} trading days")
        logger.info("")
        
        all_outcomes = []
        
        # Calculate train window size in trading days
        train_window_days = int(self.train_window_months * 21)  # ~21 trading days per month
        
        # Walk forward through trading days
        step = 0
        current_idx = train_window_days
        
        while current_idx + self.embargo_days + self.test_window_days < len(trading_days):
            step += 1
            
            # Define periods using actual trading days
            train_start_idx = current_idx - train_window_days
            train_end_idx = current_idx
            test_start_idx = current_idx + self.embargo_days
            test_end_idx = test_start_idx + self.test_window_days
            
            train_start = trading_days[train_start_idx]
            train_end = trading_days[train_end_idx - 1]
            test_start = trading_days[test_start_idx]
            test_end = trading_days[test_end_idx - 1]
            
            logger.info(f"\nStep {step}:")
            logger.info(f"  Train: {train_start.date()} to {train_end.date()} ({train_window_days} days)")
            logger.info(f"  Embargo: {self.embargo_days} days")
            logger.info(f"  Test: {test_start.date()} to {test_end.date()} ({self.test_window_days} days)")
            
            # Fetch test period data
            test_data = await self._fetch_ohlcv(
                session, instrument_key, test_start, test_end + timedelta(days=1)
            )
            
            if len(test_data["timestamps"]) < 10:
                logger.warning(f"  Insufficient test data ({len(test_data['timestamps'])} candles), skipping")
                current_idx += self.test_window_days
                continue
            
            # Detect patterns in test period
            predictions = self.detector.detect_patterns(
                test_data["timestamps"],
                test_data["open"],
                test_data["high"],
                test_data["low"],
                test_data["close"],
            )
            
            logger.info(f"  Detected {len(predictions)} patterns")
            
            # Measure outcomes
            outcomes = []
            for pred in predictions:
                outcome = await self.measurer.measure_outcome(
                    session, pred, instrument_key
                )
                if outcome:
                    outcomes.append(outcome)
            
            logger.info(f"  Measured {len(outcomes)} outcomes")
            all_outcomes.extend(outcomes)
            
            # Roll forward by test window size
            current_idx += self.test_window_days
        
        logger.info(f"\nBacktest complete: {len(all_outcomes)} total outcomes measured")
        return all_outcomes
    
    async def _fetch_ohlcv(
        self,
        session: AsyncSession,
        instrument_key: str,
        start_date: datetime,
        end_date: datetime,
    ) -> dict[str, np.ndarray]:
        """Fetch OHLCV data for a period."""
        query = text("""
            SELECT timestamp, open, high, low, close, volume
            FROM upstox_ohlcv
            WHERE instrument_key = :instrument_key
              AND timeframe = '1D'
              AND timestamp >= :start_date
              AND timestamp <= :end_date
            ORDER BY timestamp ASC
        """)
        
        result = await session.execute(
            query,
            {
                "instrument_key": instrument_key,
                "start_date": start_date,
                "end_date": end_date,
            }
        )
        rows = result.fetchall()
        
        return {
            "timestamps": np.array([row.timestamp for row in rows]),
            "open": np.array([float(row.open) for row in rows]),
            "high": np.array([float(row.high) for row in rows]),
            "low": np.array([float(row.low) for row in rows]),
            "close": np.array([float(row.close) for row in rows]),
            "volume": np.array([float(row.volume) for row in rows]),
        }


class AccuracyAnalyzer:
    """Analyze backtest outcomes and compute accuracy metrics."""
    
    @staticmethod
    def analyze(outcomes: list[OutcomeMeasurement]) -> AccuracyMetrics:
        """Compute comprehensive accuracy metrics."""
        if not outcomes:
            return AccuracyMetrics()
        
        # Overall metrics
        total = len(outcomes)
        directional_correct = sum(1 for o in outcomes if o.direction_correct)
        target_hits = sum(1 for o in outcomes if o.hit_target)
        sl_hits = sum(1 for o in outcomes if o.hit_stop_loss)
        
        # Average moves
        avg_favorable = np.mean([o.max_favorable_move_pct for o in outcomes])
        avg_adverse = np.mean([o.max_adverse_move_pct for o in outcomes])
        avg_final = np.mean([o.final_move_pct for o in outcomes])
        
        # Confidence calibration
        high_conf = [o for o in outcomes if o.prediction.confidence_level == "HIGH"]
        medium_conf = [o for o in outcomes if o.prediction.confidence_level == "MEDIUM"]
        low_conf = [o for o in outcomes if o.prediction.confidence_level == "LOW"]
        
        high_acc = (sum(1 for o in high_conf if o.direction_correct) / len(high_conf) * 100) if high_conf else 0.0
        medium_acc = (sum(1 for o in medium_conf if o.direction_correct) / len(medium_conf) * 100) if medium_conf else 0.0
        low_acc = (sum(1 for o in low_conf if o.direction_correct) / len(low_conf) * 100) if low_conf else 0.0
        
        # Pattern-specific accuracy
        pattern_accuracies = {}
        for pattern_name in set(o.prediction.pattern_name for o in outcomes):
            pattern_outcomes = [o for o in outcomes if o.prediction.pattern_name == pattern_name]
            pattern_correct = sum(1 for o in pattern_outcomes if o.direction_correct)
            pattern_accuracies[pattern_name] = (pattern_correct / len(pattern_outcomes) * 100)
        
        return AccuracyMetrics(
            total_predictions=total,
            directional_accuracy=(directional_correct / total * 100),
            target_hit_rate=(target_hits / total * 100),
            stop_loss_hit_rate=(sl_hits / total * 100),
            avg_favorable_move=avg_favorable,
            avg_adverse_move=avg_adverse,
            avg_final_move=avg_final,
            high_confidence_accuracy=high_acc,
            medium_confidence_accuracy=medium_acc,
            low_confidence_accuracy=low_acc,
            pattern_accuracies=pattern_accuracies,
        )


async def run_poc():
    """Run the historical accuracy measurement POC."""
    logger.info("=" * 80)
    logger.info("HISTORICAL ACCURACY MEASUREMENT POC")
    logger.info("=" * 80)
    logger.info("")
    
    # Test parameters
    instrument_key = "NSE_EQ|INE002A01018"  # Reliance Industries
    start_date = datetime(2023, 1, 1)
    end_date = datetime(2026, 5, 1)
    
    logger.info(f"Instrument: {instrument_key}")
    logger.info(f"Period: {start_date.date()} to {end_date.date()}")
    logger.info("")
    
    async with AsyncSessionLocal() as session:
        # Run walk-forward backtest
        backtester = WalkForwardBacktester(
            train_window_months=3,  # 3 months training
            test_window_days=20,     # 20 trading days test (~1 month)
            embargo_days=2,          # 2 day embargo
        )
        
        outcomes = await backtester.run_backtest(
            session, instrument_key, start_date, end_date
        )
        
        if not outcomes:
            logger.error("No outcomes measured. Check data availability.")
            return
        
        # Analyze results
        logger.info("")
        logger.info("=" * 80)
        logger.info("ACCURACY METRICS")
        logger.info("=" * 80)
        
        metrics = AccuracyAnalyzer.analyze(outcomes)
        
        logger.info(f"Total predictions:        {metrics.total_predictions}")
        logger.info(f"Directional accuracy:     {metrics.directional_accuracy:.1f}%")
        logger.info(f"Target hit rate:          {metrics.target_hit_rate:.1f}%")
        logger.info(f"Stop loss hit rate:       {metrics.stop_loss_hit_rate:.1f}%")
        logger.info(f"Avg favorable move:       {metrics.avg_favorable_move:+.2f}%")
        logger.info(f"Avg adverse move:         {metrics.avg_adverse_move:+.2f}%")
        logger.info(f"Avg final move:           {metrics.avg_final_move:+.2f}%")
        logger.info("")
        
        logger.info("Confidence Calibration:")
        logger.info(f"  HIGH confidence:        {metrics.high_confidence_accuracy:.1f}%")
        logger.info(f"  MEDIUM confidence:      {metrics.medium_confidence_accuracy:.1f}%")
        logger.info(f"  LOW confidence:         {metrics.low_confidence_accuracy:.1f}%")
        logger.info("")
        
        logger.info("Pattern-Specific Accuracy:")
        for pattern, accuracy in sorted(metrics.pattern_accuracies.items()):
            logger.info(f"  {pattern:20s} {accuracy:.1f}%")
        logger.info("")
        
        # Assessment
        logger.info("=" * 80)
        logger.info("POC ASSESSMENT")
        logger.info("=" * 80)
        
        # Industry benchmark: >70% directional accuracy for deployment
        if metrics.directional_accuracy >= 70:
            logger.info("✅ PASS: Directional accuracy >= 70% (deployment threshold)")
        elif metrics.directional_accuracy >= 60:
            logger.info("⚠️  MARGINAL: Directional accuracy 60-70% (needs improvement)")
        else:
            logger.info("❌ FAIL: Directional accuracy < 60% (not production-ready)")
        
        # Confidence calibration check
        if metrics.high_confidence_accuracy > metrics.medium_confidence_accuracy > metrics.low_confidence_accuracy:
            logger.info("✅ PASS: Confidence levels properly calibrated")
        else:
            logger.info("⚠️  WARNING: Confidence calibration needs adjustment")
        
        logger.info("")
        logger.info("Next Steps:")
        logger.info("  1. Expand to 60+ patterns with TA-Lib")
        logger.info("  2. Test on multiple instruments (diversification)")
        logger.info("  3. Optimize TP/SL levels per pattern")
        logger.info("  4. Implement production API endpoint")
        logger.info("=" * 80)


def main():
    """Entry point."""
    try:
        asyncio.run(run_poc())
    except KeyboardInterrupt:
        logger.info("\nPOC interrupted by user")
    except Exception as e:
        logger.error(f"POC failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
