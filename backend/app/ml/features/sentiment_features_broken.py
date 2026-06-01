"""
Sentiment Feature Engineering Module

Computes 5 sentiment features from RSS news events for ML model training.
Integrates with existing ai_processed_events table.

Author: Cortex AI Team
Date: 2026-04-11
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
import logging

from app.ai.fusion.models import AIProcessedEvent, AINLPResult, AIEventClassification

logger = logging.getLogger(__name__)


async def fetch_sentiment_data(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    db: AsyncSession
) -> pd.DataFrame:
    """
    Fetch sentiment data for a symbol from joined tables
    
    Args:
        symbol: Stock symbol (e.g., 'NSE_EQ|INE669E01016')
        start_date: Start date for data
        end_date: End date for data
        db: Database session
        
    Returns:
        DataFrame with sentiment data
    """
    try:
        # Query to join all sentiment-related tables
        stmt = select(
            AIProcessedEvent.processed_timestamp,
            AINLPResult.sentiment_score,
            AINLPResult.sentiment_label,
            AINLPResult.confidence_score,
            AIEventClassification.impact_score,
            AIEventClassification.event_type
        ).select_from(
            AIProcessedEvent
            .join(AINLPResult, AIProcessedEvent.id == AINLPResult.processed_event_id)
            .join(AIEventClassification, AINLPResult.id == AIEventClassification.nlp_result_id)
        ).where(
            and_(
                AIProcessedEvent.processed_timestamp >= start_date,
                AIProcessedEvent.processed_timestamp <= end_date,
                AIEventClassification.affected_symbols.contains([symbol])
            )
        ).order_by(AIProcessedEvent.processed_timestamp)
        
        result = await db.execute(stmt)
        rows = result.fetchall()
        
        if not rows:
            logger.info(f"No sentiment data found for {symbol}")
            return pd.DataFrame()
        
        # Convert to DataFrame
        df = pd.DataFrame([{
            'timestamp': row.processed_timestamp,
            'sentiment_score': float(row.sentiment_score) if row.sentiment_score else 0.0,
            'sentiment_label': row.sentiment_label or 'neutral',
            'confidence_score': float(row.confidence_score) if row.confidence_score else 0.0,
            'impact_score': float(row.impact_score) if row.impact_score else 0.0,
            'event_type': row.event_type or 'unknown'
        } for row in rows])
        
        logger.info(f"Fetched {len(df)} sentiment events for {symbol}")
        return df
        
    except Exception as e:
        logger.warning(f"Error fetching sentiment data for {symbol}: {e}")
        return pd.DataFrame()
    
    result = await db.execute(stmt)
    events = result.scalars().all()
    
    if not events:
        logger.warning(f"No sentiment data found for {symbol} between {start_date} and {end_date}")
        return pd.DataFrame(columns=['timestamp', 'sentiment_score', 'impact_score', 'event_count'])
    
    # Convert to DataFrame
    data = []
    for event in events:
        data.append({
            'timestamp': event.created_at,
            'sentiment_score': event.sentiment_score if event.sentiment_score else 0.0,
            'impact_score': event.impact_score if event.impact_score else 0.0,
            'event_type': event.event_type
        })
    
    df = pd.DataFrame(data)
    
    # Aggregate by day (multiple events per day)
    df['date'] = df['timestamp'].dt.date
    daily_sentiment = df.groupby('date').agg({
        'sentiment_score': 'mean',  # Average sentiment
        'impact_score': 'mean',     # Average impact
        'event_type': 'count'       # Number of events
    }).reset_index()
    
    daily_sentiment.rename(columns={
        'date': 'timestamp',
        'event_type': 'event_count'
    }, inplace=True)
    
    # Convert date back to datetime
    daily_sentiment['timestamp'] = pd.to_datetime(daily_sentiment['timestamp'])
    
    logger.info(f"Fetched {len(daily_sentiment)} days of sentiment data for {symbol}")
    
    return daily_sentiment


def compute_sentiment_features(sentiment_df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute 5 sentiment features from raw sentiment data
    
    Args:
        sentiment_df: DataFrame with columns: timestamp, sentiment_score, impact_score, event_count
        
    Returns:
        DataFrame with sentiment features added
    """
    if sentiment_df.empty:
        # Return empty DataFrame with expected columns
        return pd.DataFrame(columns=[
            'timestamp', 'sentiment_score', 'sentiment_momentum',
            'event_impact', 'news_volume', 'sentiment_volatility'
        ])
    
    result = sentiment_df.copy()
    
    # Feature 1: sentiment_score (already present, normalized to -1 to 1)
    # Ensure it's in range
    result['sentiment_score'] = result['sentiment_score'].clip(-1, 1)
    
    # Feature 2: sentiment_momentum (3-day change)
    result['sentiment_momentum'] = result['sentiment_score'].diff(periods=3)
    
    # Feature 3: event_impact (average impact score, 0 to 1)
    result['event_impact'] = result['impact_score'].clip(0, 1)
    
    # Feature 4: news_volume (number of events, log-scaled)
    result['news_volume'] = np.log1p(result['event_count'])  # log(1 + x) to handle 0
    
    # Feature 5: sentiment_volatility (5-day rolling std)
    result['sentiment_volatility'] = result['sentiment_score'].rolling(window=5).std()
    
    # Fill NaN with 0 (for early days with insufficient history)
    result['sentiment_momentum'].fillna(0, inplace=True)
    result['sentiment_volatility'].fillna(0, inplace=True)
    
    # Drop intermediate columns
    result.drop(columns=['impact_score', 'event_count'], inplace=True, errors='ignore')
    
    return result


async def compute_all_sentiment_features(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    db: AsyncSession
) -> pd.DataFrame:
    """
    Fetch and compute all 5 sentiment features for a symbol
    
    Args:
        symbol: Stock symbol
        start_date: Start date for data
        end_date: End date for data
        db: Database session
        
    Returns:
        DataFrame with sentiment features
    """
    # Fetch raw sentiment data
    sentiment_df = await fetch_sentiment_data(symbol, start_date, end_date, db)
    
    if sentiment_df.empty:
        logger.warning(f"No sentiment data for {symbol}, returning zeros")
        # Return DataFrame with zeros for all features
        date_range = pd.date_range(start=start_date, end=end_date, freq='D')
        return pd.DataFrame({
            'timestamp': date_range,
            'sentiment_score': 0.0,
            'sentiment_momentum': 0.0,
            'event_impact': 0.0,
            'news_volume': 0.0,
            'sentiment_volatility': 0.0
        })
    
    # Compute features
    features_df = compute_sentiment_features(sentiment_df)
    
    logger.info(f"Computed {len(features_df)} days of sentiment features for {symbol}")
    
    return features_df


def get_sentiment_feature_names() -> list:
    """
    Get list of all 5 sentiment feature names
    
    Returns:
        List of sentiment feature column names
    """
    return [
        'sentiment_score',
        'sentiment_momentum',
        'event_impact',
        'news_volume',
        'sentiment_volatility'
    ]


def merge_sentiment_with_ohlcv(
    ohlcv_df: pd.DataFrame,
    sentiment_df: pd.DataFrame
) -> pd.DataFrame:
    """
    Merge sentiment features with OHLCV data
    
    Args:
        ohlcv_df: DataFrame with OHLCV data and technical features
        sentiment_df: DataFrame with sentiment features
        
    Returns:
        Merged DataFrame with all features
    """
    # Ensure both have timestamp as datetime
    ohlcv_df['timestamp'] = pd.to_datetime(ohlcv_df['timestamp'])
    sentiment_df['timestamp'] = pd.to_datetime(sentiment_df['timestamp'])
    
    # Merge on date (left join to keep all OHLCV rows)
    ohlcv_df['date'] = ohlcv_df['timestamp'].dt.date
    sentiment_df['date'] = sentiment_df['timestamp'].dt.date
    
    merged = ohlcv_df.merge(
        sentiment_df.drop(columns=['timestamp']),
        on='date',
        how='left'
    )
    
    # Fill missing sentiment values with 0 (days with no news)
    sentiment_cols = get_sentiment_feature_names()
    for col in sentiment_cols:
        if col in merged.columns:
            merged[col].fillna(0, inplace=True)
    
    # Drop temporary date column
    merged.drop(columns=['date'], inplace=True)
    
    logger.info(f"Merged sentiment features. Final shape: {merged.shape}")
    
    return merged
