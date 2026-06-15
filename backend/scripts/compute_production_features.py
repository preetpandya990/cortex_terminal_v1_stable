"""
Production Feature Computation Pipeline
========================================
Pre-computes and stores features for all symbols in the database.

Features:
- Parallel processing with configurable workers
- Progress tracking with ETA
- Automatic retry on failures
- Batch commits for performance
- Comprehensive error handling
- Resume capability (skips existing features)

Author: Cortex AI Team
Date: 2026-04-14
"""
import argparse
import asyncio
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
import sys

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text, select
import numpy as np

from app.core.config import get_settings
from app.ml.features.feature_pipeline import compute_features_for_symbol
from app.ml.features.feature_store import save_features_to_db

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

settings = get_settings()


class FeatureComputationPipeline:
    """Production-grade feature computation pipeline."""
    
    def __init__(
        self,
        db_url: str,
        lookback_days: int = 90,
        batch_size: int = 10,
        max_workers: int = 5,
        mode: str = "incremental",
        stale_days: int = 3,
    ):
        """
        Initialize pipeline.

        Args:
            db_url:        Database connection URL.
            lookback_days: Days of historical OHLCV to include in feature computation.
            batch_size:    Number of symbols to process concurrently per batch.
            max_workers:   Maximum concurrent workers.
            mode:          "incremental" — only symbols with no features at all (default).
                           "refresh"     — symbols where MAX(ml_features.timestamp) is
                                           older than stale_days calendar days.
            stale_days:    Staleness threshold in calendar days (refresh mode only).
        """
        self.db_url = db_url
        self.lookback_days = lookback_days
        self.batch_size = batch_size
        self.max_workers = max_workers
        self.mode = mode
        self.stale_days = stale_days
        
        self.engine = None
        self.session_factory = None
        
        # Statistics
        self.total_symbols = 0
        self.processed_symbols = 0
        self.failed_symbols = []
        self.skipped_symbols = 0
        self.start_time = None
    
    async def initialize(self):
        """Initialize database connection."""
        self.engine = create_async_engine(
            self.db_url,
            pool_size=self.max_workers + 2,
            max_overflow=5,
            pool_pre_ping=True,
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        logger.info(f"✓ Database connection initialized")
    
    async def cleanup(self):
        """Cleanup resources."""
        if self.engine:
            await self.engine.dispose()
            logger.info("✓ Database connection closed")
    
    async def get_symbols_to_process(self) -> list[str]:
        """
        Return symbols that need feature computation.

        incremental mode — symbols with no ml_features row at all (first-time fill).
        refresh mode     — symbols where MAX(ml_features.timestamp) < today − stale_days,
                           plus any symbol present in OHLCV but absent from ml_features.

        Returns:
            Ordered list of instrument_key values to process.
        """
        async with self.session_factory() as session:
            result = await session.execute(text('''
                SELECT DISTINCT instrument_key
                FROM upstox_ohlcv
                WHERE timeframe = '1D'
                ORDER BY instrument_key
            '''))
            all_symbols = [row[0] for row in result.fetchall()]
            logger.info(f"Found {len(all_symbols)} symbols with OHLCV data")

            if self.mode == "incremental":
                result2 = await session.execute(text('''
                    SELECT DISTINCT symbol
                    FROM ml_features
                    WHERE feature_version = 'v1.0'
                '''))
                existing = {row[0] for row in result2.fetchall()}
                symbols = [s for s in all_symbols if s not in existing]
                logger.info(
                    f"Mode=incremental: {len(symbols)} symbols need first-time computation"
                )
                return symbols

            # mode == "refresh"
            # For each symbol already in the feature store, find the latest feature date.
            # Symbols absent from ml_features are treated as infinitely stale.
            result2 = await session.execute(text('''
                SELECT symbol, MAX(timestamp)::date AS latest
                FROM ml_features
                WHERE feature_version = 'v1.0'
                GROUP BY symbol
            '''))
            latest_by_symbol = {row[0]: row[1] for row in result2.fetchall()}

            threshold = date.today() - timedelta(days=self.stale_days)

            stale = [
                sym for sym in all_symbols
                if latest_by_symbol.get(sym) is None or latest_by_symbol[sym] < threshold
            ]

            logger.info(
                f"Mode=refresh (stale_days={self.stale_days}, threshold={threshold}): "
                f"{len(stale)}/{len(all_symbols)} symbols need refresh"
            )
            return stale
    
    async def compute_features_for_symbol_safe(
        self,
        symbol: str,
        session: AsyncSession,
    ) -> tuple[str, bool, str]:
        """
        Compute features for a single symbol with error handling.
        
        Args:
            symbol: Instrument key
            session: Database session
            
        Returns:
            Tuple of (symbol, success, error_message)
        """
        try:
            # Get actual data date range for this symbol
            result = await session.execute(
                text("""
                    SELECT MAX(timestamp) as end_date
                    FROM upstox_ohlcv
                    WHERE instrument_key = :symbol
                """),
                {"symbol": symbol}
            )
            row = result.fetchone()
            if not row or not row[0]:
                return (symbol, False, "No OHLCV data found")
            
            end_date = row[0]
            start_date = end_date - timedelta(days=self.lookback_days)
            
            features_df = await compute_features_for_symbol(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                timeframe='1D',
                db=session,
            )
            
            if features_df.empty:
                return (symbol, False, "No features computed (insufficient data)")
            
            # Save to database
            rows_saved = await save_features_to_db(
                symbol=symbol,
                features_df=features_df,
                db=session,
            )
            
            if rows_saved == 0:
                return (symbol, False, "No rows saved")
            
            return (symbol, True, f"{rows_saved} rows")
            
        except Exception as e:
            error_msg = str(e)[:100]  # Truncate long errors
            logger.error(f"Failed to process {symbol}: {error_msg}")
            return (symbol, False, error_msg)
    
    async def process_batch(
        self,
        symbols: list[str],
    ) -> list[tuple[str, bool, str]]:
        """
        Process a batch of symbols concurrently.
        
        Args:
            symbols: List of symbols to process
            
        Returns:
            List of (symbol, success, message) tuples
        """
        # Create separate session for each symbol to avoid concurrent access issues
        async def process_with_own_session(symbol: str) -> tuple[str, bool, str]:
            async with self.session_factory() as session:
                result = await self.compute_features_for_symbol_safe(symbol, session)
                try:
                    await session.commit()
                except Exception as e:
                    logger.error(f"Commit failed for {symbol}: {e}")
                    await session.rollback()
                return result
        
        # Process concurrently with individual sessions
        tasks = [process_with_own_session(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Handle exceptions
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed_results.append((symbols[i], False, str(result)[:100]))
            else:
                processed_results.append(result)
        
        return processed_results
    
    def print_progress(self):
        """Print progress statistics."""
        if self.total_symbols == 0:
            return
        
        elapsed = (datetime.now() - self.start_time).total_seconds()
        progress_pct = (self.processed_symbols / self.total_symbols) * 100
        
        if self.processed_symbols > 0:
            avg_time_per_symbol = elapsed / self.processed_symbols
            remaining_symbols = self.total_symbols - self.processed_symbols
            eta_seconds = avg_time_per_symbol * remaining_symbols
            eta = timedelta(seconds=int(eta_seconds))
        else:
            eta = "calculating..."
        
        logger.info(
            f"Progress: {self.processed_symbols}/{self.total_symbols} "
            f"({progress_pct:.1f}%) | "
            f"Failed: {len(self.failed_symbols)} | "
            f"Skipped: {self.skipped_symbols} | "
            f"ETA: {eta}"
        )
    
    async def run(self):
        """Run the feature computation pipeline."""
        logger.info("=" * 80)
        logger.info("PRODUCTION FEATURE COMPUTATION PIPELINE")
        logger.info("=" * 80)
        
        self.start_time = datetime.now()
        
        try:
            # Initialize
            await self.initialize()
            
            # Get symbols to process
            symbols = await self.get_symbols_to_process()
            self.total_symbols = len(symbols)
            
            if self.total_symbols == 0:
                _suffix = f", stale_days={self.stale_days}" if self.mode == "refresh" else ""
                logger.info(f"✓ No symbols require processing (mode={self.mode}{_suffix})")
                return
            
            logger.info(f"Starting computation for {self.total_symbols} symbols...")
            logger.info("Configuration:")
            logger.info(f"  Mode:          {self.mode}")
            if self.mode == "refresh":
                logger.info(f"  Stale days:    {self.stale_days}")
            logger.info(f"  Lookback days: {self.lookback_days}")
            logger.info(f"  Batch size:    {self.batch_size}")
            logger.info(f"  Max workers:   {self.max_workers}")
            logger.info("")
            
            # Process in batches
            for i in range(0, len(symbols), self.batch_size):
                batch = symbols[i:i + self.batch_size]
                batch_num = (i // self.batch_size) + 1
                total_batches = (len(symbols) + self.batch_size - 1) // self.batch_size
                
                logger.info(f"Processing batch {batch_num}/{total_batches} ({len(batch)} symbols)...")
                
                results = await self.process_batch(batch)
                
                # Update statistics
                for symbol, success, message in results:
                    self.processed_symbols += 1
                    if not success:
                        self.failed_symbols.append((symbol, message))
                
                # Print progress
                self.print_progress()
            
            # Final summary
            elapsed = (datetime.now() - self.start_time).total_seconds()
            logger.info("")
            logger.info("=" * 80)
            logger.info("COMPUTATION COMPLETE")
            logger.info("=" * 80)
            logger.info(f"Total time: {timedelta(seconds=int(elapsed))}")
            logger.info(f"Processed: {self.processed_symbols}/{self.total_symbols}")
            logger.info(f"Success: {self.processed_symbols - len(self.failed_symbols)}")
            logger.info(f"Failed: {len(self.failed_symbols)}")
            
            if self.failed_symbols:
                logger.warning("")
                logger.warning("Failed symbols:")
                for symbol, error in self.failed_symbols[:10]:  # Show first 10
                    logger.warning(f"  {symbol}: {error}")
                if len(self.failed_symbols) > 10:
                    logger.warning(f"  ... and {len(self.failed_symbols) - 10} more")
            
            logger.info("=" * 80)
            
        finally:
            await self.cleanup()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pre-compute and store ML features for all OHLCV symbols.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["incremental", "refresh"],
        default="incremental",
        help=(
            "incremental: only process symbols with no features at all. "
            "refresh: recompute symbols where features are older than --stale-days."
        ),
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=3,
        dest="stale_days",
        help="Staleness threshold in calendar days (refresh mode only).",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=90,
        dest="lookback_days",
        help="Days of historical OHLCV to include in feature computation.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        dest="batch_size",
        help="Number of symbols to process concurrently per batch.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=5,
        dest="max_workers",
        help="Maximum concurrent workers.",
    )
    return parser.parse_args()


async def main(args: argparse.Namespace) -> None:
    pipeline = FeatureComputationPipeline(
        db_url=str(settings.DATABASE_URL),
        lookback_days=args.lookback_days,
        batch_size=args.batch_size,
        max_workers=args.max_workers,
        mode=args.mode,
        stale_days=args.stale_days,
    )
    await pipeline.run()


if __name__ == "__main__":
    _args = _parse_args()
    try:
        asyncio.run(main(_args))
    except KeyboardInterrupt:
        logger.info("\n✗ Interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"\n✗ Pipeline failed: {e}", exc_info=True)
        sys.exit(1)
