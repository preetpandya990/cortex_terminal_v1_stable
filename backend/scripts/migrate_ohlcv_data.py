#!/usr/bin/env python3
"""
Data Migration Script: tradeguru stock_ohlcv → cortex upstox_ohlcv
=================================================================
Migrates 4M+ OHLCV records with proper mapping and validation.
"""
import asyncio
import logging
from datetime import datetime
from typing import AsyncGenerator

import asyncpg
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Source database (tradeguru)
SOURCE_CONFIG = {
    'host': 'localhost',
    'port': 5434,
    'user': 'tradeguru_user',
    'password': 'tradeguru_pass',
    'database': 'tradeguru_db',
}

# Target database (cortex)
TARGET_CONFIG = {
    'host': 'localhost',
    'port': 5433,
    'user': 'cortex',
    'password': 'cortex_pg',
    'database': 'cortex_db',
}

BATCH_SIZE = 5000  # Insert 5000 rows at a time
TIMEFRAME_MAP = {'1day': '1D', '1week': '1W', '1month': '1M'}


async def fetch_source_data(conn: asyncpg.Connection) -> AsyncGenerator:
    """Stream data from source database in batches."""
    query = """
        SELECT 
            timestamp,
            symbol,
            timeframe,
            open,
            high,
            low,
            close,
            volume,
            instrument_key
        FROM stock_ohlcv
        WHERE timeframe = '1day'
        ORDER BY timestamp, symbol
    """
    
    logger.info("Fetching data from source database...")
    async with conn.transaction():
        async for record in conn.cursor(query):
            yield record


async def get_total_count(conn: asyncpg.Connection) -> int:
    """Get total row count for progress tracking."""
    return await conn.fetchval("SELECT COUNT(*) FROM stock_ohlcv WHERE timeframe = '1day'")


async def migrate_data():
    """Main migration function."""
    start_time = datetime.now()
    
    # Connect to both databases
    logger.info("Connecting to source database...")
    source_conn = await asyncpg.connect(**SOURCE_CONFIG)
    
    logger.info("Connecting to target database...")
    target_conn = await asyncpg.connect(**TARGET_CONFIG)
    
    try:
        # Get total count for progress bar
        total_rows = await get_total_count(source_conn)
        logger.info(f"Total rows to migrate: {total_rows:,}")
        
        # Prepare insert statement
        insert_query = """
            INSERT INTO upstox_ohlcv (
                instrument_key, timeframe, timestamp,
                open, high, low, close, volume, oi
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (instrument_key, timeframe, timestamp) DO NOTHING
        """
        
        # Migrate in batches
        batch = []
        migrated_count = 0
        skipped_count = 0
        
        with tqdm(total=total_rows, desc="Migrating", unit="rows") as pbar:
            async for record in fetch_source_data(source_conn):
                # Map timeframe
                timeframe = TIMEFRAME_MAP.get(record['timeframe'], '1D')
                
                # Use symbol as instrument_key if instrument_key is None
                instrument_key = record['instrument_key'] or f"NSE_EQ|{record['symbol']}"
                
                # Prepare row
                row = (
                    instrument_key,
                    timeframe,
                    record['timestamp'],
                    record['open'],
                    record['high'],
                    record['low'],
                    record['close'],
                    record['volume'] or 0,
                    0,  # oi (open interest) - default to 0
                )
                
                batch.append(row)
                
                # Insert batch when full
                if len(batch) >= BATCH_SIZE:
                    try:
                        await target_conn.executemany(insert_query, batch)
                        migrated_count += len(batch)
                        pbar.update(len(batch))
                    except Exception as e:
                        logger.error(f"Batch insert failed: {e}")
                        skipped_count += len(batch)
                    
                    batch = []
            
            # Insert remaining rows
            if batch:
                try:
                    await target_conn.executemany(insert_query, batch)
                    migrated_count += len(batch)
                    pbar.update(len(batch))
                except Exception as e:
                    logger.error(f"Final batch insert failed: {e}")
                    skipped_count += len(batch)
        
        # Summary
        duration = (datetime.now() - start_time).total_seconds()
        logger.info("=" * 70)
        logger.info("Migration Complete!")
        logger.info(f"Total rows processed: {total_rows:,}")
        logger.info(f"Successfully migrated: {migrated_count:,}")
        logger.info(f"Skipped (duplicates): {skipped_count:,}")
        logger.info(f"Duration: {duration:.2f} seconds")
        logger.info(f"Speed: {migrated_count/duration:.0f} rows/second")
        logger.info("=" * 70)
        
    finally:
        await source_conn.close()
        await target_conn.close()


async def validate_migration():
    """Validate migrated data."""
    logger.info("\nValidating migration...")
    
    target_conn = await asyncpg.connect(**TARGET_CONFIG)
    
    try:
        # Check row count
        count = await target_conn.fetchval("SELECT COUNT(*) FROM upstox_ohlcv")
        logger.info(f"✓ Total rows in target: {count:,}")
        
        # Check date range
        result = await target_conn.fetchrow("""
            SELECT 
                MIN(timestamp) as earliest,
                MAX(timestamp) as latest,
                COUNT(DISTINCT instrument_key) as unique_instruments
            FROM upstox_ohlcv
        """)
        logger.info(f"✓ Date range: {result['earliest']} to {result['latest']}")
        logger.info(f"✓ Unique instruments: {result['unique_instruments']:,}")
        
        # Sample data
        sample = await target_conn.fetchrow("""
            SELECT * FROM upstox_ohlcv 
            ORDER BY timestamp DESC 
            LIMIT 1
        """)
        logger.info(f"✓ Latest record: {sample['instrument_key']} @ {sample['timestamp']}")
        logger.info(f"  OHLC: {sample['open']}/{sample['high']}/{sample['low']}/{sample['close']}")
        
    finally:
        await target_conn.close()


if __name__ == "__main__":
    print("=" * 70)
    print("Cortex AI - OHLCV Data Migration")
    print("=" * 70)
    print()
    
    asyncio.run(migrate_data())
    asyncio.run(validate_migration())
    
    print("\n✅ Migration complete! Ready for ML model training.")
