"""
Feature Store Module

Handles storage and retrieval of computed features from TimescaleDB and Redis.
Provides offline (historical) and online (real-time) feature access.

Author: Cortex AI Team
Date: 2026-04-12
"""

import pandas as pd
import numpy as np
import json
from datetime import datetime
from typing import List, Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, delete
from redis.asyncio import Redis
import logging

logger = logging.getLogger(__name__)

# Feature version for tracking changes
FEATURE_VERSION = "v1.0"


async def save_features_to_db(
    symbol: str,
    features_df: pd.DataFrame,
    db: AsyncSession,
    feature_version: str = FEATURE_VERSION
) -> int:
    """
    Save computed features to TimescaleDB
    
    Args:
        symbol: Stock symbol
        features_df: DataFrame with features (must have 'timestamp' column)
        db: Database session
        feature_version: Feature version string
        
    Returns:
        Number of rows saved
    """
    if features_df.empty:
        logger.warning(f"No features to save for {symbol}")
        return 0
    
    # Get feature columns (exclude metadata)
    exclude_cols = ['timestamp', 'symbol', 'open', 'high', 'low', 'close', 'volume']
    feature_cols = [col for col in features_df.columns if col not in exclude_cols]
    
    logger.info(f"Saving {len(features_df)} feature rows for {symbol} to database...")
    
    # Prepare bulk insert data
    rows_to_insert = []
    for _, row in features_df.iterrows():
        # Extract feature vector as dict
        feature_vector = {col: float(row[col]) if pd.notna(row[col]) else None 
                         for col in feature_cols}
        
        rows_to_insert.append({
            'symbol': symbol,
            'timestamp': row['timestamp'],
            'feature_vector': json.dumps(feature_vector),
            'feature_version': feature_version
        })
    
    # Bulk insert using raw SQL for performance
    if rows_to_insert:
        # Build INSERT statement with ON CONFLICT
        from sqlalchemy import text
        await db.execute(
            text("""
            INSERT INTO ml_features (symbol, timestamp, feature_vector, feature_version)
            VALUES (:symbol, :timestamp, :feature_vector, :feature_version)
            ON CONFLICT (symbol, timestamp, feature_version) DO UPDATE
            SET feature_vector = EXCLUDED.feature_vector
            """),
            rows_to_insert
        )
        await db.commit()
    
    logger.info(f"Saved {len(rows_to_insert)} feature rows for {symbol}")
    
    return len(rows_to_insert)


async def load_features_from_db(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    db: AsyncSession,
    feature_version: str = FEATURE_VERSION
) -> pd.DataFrame:
    """
    Load features from TimescaleDB
    
    Args:
        symbol: Stock symbol
        start_date: Start date
        end_date: End date
        db: Database session
        feature_version: Feature version string
        
    Returns:
        DataFrame with features
    """
    logger.info(f"Loading features for {symbol} from {start_date} to {end_date}")
    
    # Query features
    from sqlalchemy import text
    result = await db.execute(
        text("""
        SELECT timestamp, feature_vector
        FROM ml_features
        WHERE symbol = :symbol
          AND timestamp >= :start_date
          AND timestamp <= :end_date
          AND feature_version = :feature_version
        ORDER BY timestamp
        """),
        {
            'symbol': symbol,
            'start_date': start_date,
            'end_date': end_date,
            'feature_version': feature_version
        }
    )
    
    rows = result.fetchall()
    
    if not rows:
        logger.warning(f"No features found for {symbol}")
        return pd.DataFrame()
    
    # Convert to DataFrame
    data = []
    for row in rows:
        # feature_vector is already a dict (JSONB type), no need to parse
        feature_dict = row.feature_vector if isinstance(row.feature_vector, dict) else json.loads(row.feature_vector)
        feature_dict['timestamp'] = row.timestamp
        feature_dict['symbol'] = symbol
        data.append(feature_dict)
    
    df = pd.DataFrame(data)
    
    logger.info(f"Loaded {len(df)} feature rows for {symbol}")
    
    return df


async def cache_features_to_redis(
    symbol: str,
    features: np.ndarray,
    feature_names: List[str],
    timestamp: datetime,
    redis: Redis,
    ttl: int = 86400  # 24 hours
) -> None:
    """
    Cache latest features to Redis for online serving
    
    Args:
        symbol: Stock symbol
        features: Feature array (1D array of feature values)
        feature_names: List of feature names
        timestamp: Timestamp of features
        redis: Redis client
        ttl: Time to live in seconds (default 24 hours)
    """
    # Create feature dict
    feature_dict = {
        'timestamp': timestamp.isoformat(),
        'features': features.tolist(),
        'feature_names': feature_names,
        'version': FEATURE_VERSION
    }
    
    # Store in Redis
    key = f"ml:features:{symbol}"
    await redis.setex(
        key,
        ttl,
        json.dumps(feature_dict)
    )
    
    logger.debug(f"Cached features for {symbol} to Redis (TTL: {ttl}s)")


async def get_cached_features(
    symbol: str,
    redis: Redis
) -> Optional[Dict[str, Any]]:
    """
    Get cached features from Redis
    
    Args:
        symbol: Stock symbol
        redis: Redis client
        
    Returns:
        Dict with keys: timestamp, features, feature_names, version
        None if not found or expired
    """
    key = f"ml:features:{symbol}"
    cached = await redis.get(key)
    
    if not cached:
        logger.debug(f"No cached features for {symbol}")
        return None
    
    feature_dict = json.loads(cached)
    
    # Convert features back to numpy array
    feature_dict['features'] = np.array(feature_dict['features'])
    feature_dict['timestamp'] = datetime.fromisoformat(feature_dict['timestamp'])
    
    logger.debug(f"Retrieved cached features for {symbol}")
    
    return feature_dict


async def delete_features(
    symbol: str,
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    db: AsyncSession,
    feature_version: str = FEATURE_VERSION
) -> int:
    """
    Delete features from database
    
    Args:
        symbol: Stock symbol
        start_date: Start date (optional)
        end_date: End date (optional)
        db: Database session
        feature_version: Feature version string
        
    Returns:
        Number of rows deleted
    """
    conditions = [
        "symbol = :symbol",
        "feature_version = :feature_version"
    ]
    params = {
        'symbol': symbol,
        'feature_version': feature_version
    }
    
    if start_date:
        conditions.append("timestamp >= :start_date")
        params['start_date'] = start_date
    
    if end_date:
        conditions.append("timestamp <= :end_date")
        params['end_date'] = end_date
    
    where_clause = " AND ".join(conditions)

    from sqlalchemy import text

    result = await db.execute(
        text(f"DELETE FROM ml_features WHERE {where_clause}"),
        params
    )
    await db.commit()
    
    deleted_count = result.rowcount
    logger.info(f"Deleted {deleted_count} feature rows for {symbol}")
    
    return deleted_count


async def get_feature_stats(
    symbol: str,
    db: AsyncSession,
    feature_version: str = FEATURE_VERSION
) -> Dict[str, Any]:
    """
    Get statistics about stored features
    
    Args:
        symbol: Stock symbol
        db: Database session
        feature_version: Feature version string
        
    Returns:
        Dict with stats: count, date_range, version
    """
    result = await db.execute(
        """
        SELECT 
            COUNT(*) as count,
            MIN(timestamp) as min_date,
            MAX(timestamp) as max_date
        FROM ml_features
        WHERE symbol = :symbol
          AND feature_version = :feature_version
        """,
        {
            'symbol': symbol,
            'feature_version': feature_version
        }
    )
    
    row = result.fetchone()
    
    stats = {
        'symbol': symbol,
        'count': row.count if row else 0,
        'min_date': row.min_date if row else None,
        'max_date': row.max_date if row else None,
        'version': feature_version
    }
    
    return stats


async def batch_save_features(
    features_dict: Dict[str, pd.DataFrame],
    db: AsyncSession,
    feature_version: str = FEATURE_VERSION
) -> Dict[str, int]:
    """
    Save features for multiple symbols in batch
    
    Args:
        features_dict: Dict mapping symbol to features DataFrame
        db: Database session
        feature_version: Feature version string
        
    Returns:
        Dict mapping symbol to number of rows saved
    """
    logger.info(f"Batch saving features for {len(features_dict)} symbols...")
    
    results = {}
    
    for symbol, features_df in features_dict.items():
        try:
            count = await save_features_to_db(symbol, features_df, db, feature_version)
            results[symbol] = count
        except Exception as e:
            logger.error(f"Error saving features for {symbol}: {e}", exc_info=True)
            results[symbol] = 0
    
    total_saved = sum(results.values())
    logger.info(f"Batch save complete: {total_saved} total rows saved")
    
    return results


async def batch_cache_latest_features(
    features_dict: Dict[str, pd.DataFrame],
    feature_names: List[str],
    redis: Redis,
    ttl: int = 86400
) -> int:
    """
    Cache latest features for multiple symbols to Redis
    
    Args:
        features_dict: Dict mapping symbol to features DataFrame
        feature_names: List of feature names
        redis: Redis client
        ttl: Time to live in seconds
        
    Returns:
        Number of symbols cached
    """
    logger.info(f"Batch caching latest features for {len(features_dict)} symbols...")
    
    cached_count = 0
    
    for symbol, features_df in features_dict.items():
        try:
            if features_df.empty:
                continue
            
            # Get latest row
            latest_row = features_df.iloc[-1]
            timestamp = latest_row['timestamp']
            
            # Extract feature values
            feature_values = np.array([latest_row[col] for col in feature_names])
            
            # Cache to Redis
            await cache_features_to_redis(
                symbol, feature_values, feature_names, timestamp, redis, ttl
            )
            
            cached_count += 1
            
        except Exception as e:
            logger.error(f"Error caching features for {symbol}: {e}", exc_info=True)
            continue
    
    logger.info(f"Cached features for {cached_count} symbols")
    
    return cached_count
