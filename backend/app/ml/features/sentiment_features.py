"""
Sentiment Feature Engineering Module

Production-grade sentiment analysis feature extraction from AI-processed news events.
Implements proper database joins and comprehensive error handling.

Author: Cortex AI Team  
Date: 2026-04-13
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, text
from sqlalchemy.orm import selectinload
import logging

from app.ai.fusion.models import AIProcessedEvent, AINLPResult, AIEventClassification

logger = logging.getLogger(__name__)


class SentimentFeatureExtractor:
    """
    Production-grade sentiment feature extraction with comprehensive validation.
    
    Features extracted:
    1. sentiment_score_mean: Average sentiment score over lookback period
    2. sentiment_score_std: Standard deviation of sentiment scores
    3. sentiment_volatility: Rolling volatility of sentiment
    4. news_volume: Number of news events
    5. impact_weighted_sentiment: Sentiment weighted by impact scores
    """
    
    def __init__(self, lookback_days: int = 30):
        """
        Initialize sentiment feature extractor.
        
        Args:
            lookback_days: Number of days to look back for sentiment data
        """
        self.lookback_days = lookback_days
        self.feature_names = [
            'sentiment_score_mean',
            'sentiment_score_std', 
            'sentiment_volatility',
            'news_volume',
            'impact_weighted_sentiment'
        ]
    
    async def extract_features(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        db: AsyncSession
    ) -> pd.DataFrame:
        """
        Extract sentiment features for a symbol over date range.
        
        Args:
            symbol: Stock symbol (e.g., 'NSE_EQ|INE669E01016')
            start_date: Start date for feature extraction
            end_date: End date for feature extraction
            db: Database session
            
        Returns:
            DataFrame with sentiment features indexed by date
        """
        try:
            logger.info(f"Extracting sentiment features for {symbol}")
            
            # Fetch raw sentiment data
            sentiment_data = await self._fetch_sentiment_data(
                symbol, start_date, end_date, db
            )
            
            if sentiment_data.empty:
                logger.warning(f"No sentiment data found for {symbol}")
                return self._create_empty_features(start_date, end_date)
            
            # Generate features
            features = self._compute_features(sentiment_data, start_date, end_date)
            
            logger.info(f"✓ Extracted {len(features)} sentiment feature rows for {symbol}")
            return features
            
        except Exception as e:
            logger.error(f"Error extracting sentiment features for {symbol}: {e}")
            return self._create_empty_features(start_date, end_date)
    
    async def _fetch_sentiment_data(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        db: AsyncSession
    ) -> pd.DataFrame:
        """
        Fetch sentiment data with proper database joins.
        
        This implements the correct schema:
        ai_processed_events -> ai_nlp_results -> ai_event_classifications
        """
        try:
            # Extend date range for lookback calculation
            extended_start = start_date - timedelta(days=self.lookback_days)
            
            # Production-grade query with proper joins
            query = select(
                AIProcessedEvent.processed_timestamp,
                AINLPResult.sentiment_score,
                AINLPResult.sentiment_label,
                AINLPResult.confidence_score,
                AIEventClassification.impact_score,
                AIEventClassification.event_type,
                AIEventClassification.classification_confidence
            ).join(
                AINLPResult, AIProcessedEvent.id == AINLPResult.processed_event_id
            ).join(
                AIEventClassification, AINLPResult.id == AIEventClassification.nlp_result_id
            ).where(
                and_(
                    AIProcessedEvent.processed_timestamp >= extended_start,
                    AIProcessedEvent.processed_timestamp <= end_date,
                    AIEventClassification.affected_symbols.contains([symbol]),
                    AINLPResult.sentiment_score.isnot(None),
                    AIEventClassification.impact_score.isnot(None)
                )
            ).order_by(AIProcessedEvent.processed_timestamp)
            
            result = await db.execute(query)
            rows = result.fetchall()
            
            if not rows:
                logger.info(f"No sentiment events found for {symbol}")
                return pd.DataFrame()
            
            # Convert to DataFrame with proper data types
            df = pd.DataFrame([{
                'timestamp': row.processed_timestamp,
                'sentiment_score': float(row.sentiment_score) if row.sentiment_score else 0.0,
                'sentiment_label': row.sentiment_label or 'neutral',
                'confidence_score': float(row.confidence_score) if row.confidence_score else 0.0,
                'impact_score': float(row.impact_score) if row.impact_score else 0.0,
                'event_type': row.event_type or 'unknown',
                'classification_confidence': float(row.classification_confidence) if row.classification_confidence else 0.0
            } for row in rows])
            
            # Ensure timestamp is datetime
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df = df.sort_values('timestamp').reset_index(drop=True)
            
            logger.info(f"Fetched {len(df)} sentiment events for {symbol}")
            return df
            
        except Exception as e:
            logger.error(f"Database query failed for {symbol}: {e}")
            return pd.DataFrame()
    
    def _compute_features(
        self,
        sentiment_data: pd.DataFrame,
        start_date: datetime,
        end_date: datetime
    ) -> pd.DataFrame:
        """
        Compute rolling sentiment features with production-grade calculations.
        """
        # Create date range for features
        date_range = pd.date_range(start=start_date, end=end_date, freq='D')
        features = pd.DataFrame(index=date_range, columns=self.feature_names)
        
        for date in date_range:
            # Get sentiment data for lookback window
            window_start = date - timedelta(days=self.lookback_days)
            window_data = sentiment_data[
                (sentiment_data['timestamp'] >= window_start) &
                (sentiment_data['timestamp'] <= date)
            ]
            
            if len(window_data) == 0:
                # No data - use neutral values
                features.loc[date] = [0.0, 0.0, 0.0, 0.0, 0.0]
                continue
            
            # Feature 1: Mean sentiment score
            sentiment_mean = window_data['sentiment_score'].mean()
            
            # Feature 2: Sentiment standard deviation
            sentiment_std = window_data['sentiment_score'].std() if len(window_data) > 1 else 0.0
            
            # Feature 3: Sentiment volatility (rolling std of daily averages)
            daily_sentiment = window_data.groupby(window_data['timestamp'].dt.date)['sentiment_score'].mean()
            sentiment_volatility = daily_sentiment.std() if len(daily_sentiment) > 1 else 0.0
            
            # Feature 4: News volume (number of events)
            news_volume = len(window_data)
            
            # Feature 5: Impact-weighted sentiment
            if window_data['impact_score'].sum() > 0:
                impact_weighted = (
                    window_data['sentiment_score'] * window_data['impact_score']
                ).sum() / window_data['impact_score'].sum()
            else:
                impact_weighted = sentiment_mean
            
            features.loc[date] = [
                sentiment_mean,
                sentiment_std,
                sentiment_volatility,
                news_volume,
                impact_weighted
            ]
        
        # Fill any remaining NaN values
        features = features.fillna(0.0)
        
        # Ensure all values are finite
        features = features.replace([np.inf, -np.inf], 0.0)
        
        return features
    
    def _create_empty_features(
        self,
        start_date: datetime,
        end_date: datetime
    ) -> pd.DataFrame:
        """Create empty feature DataFrame with neutral values."""
        date_range = pd.date_range(start=start_date, end=end_date, freq='D')
        features = pd.DataFrame(
            index=date_range,
            columns=self.feature_names,
            data=0.0  # Neutral sentiment values
        )
        return features


# Convenience functions for backward compatibility
async def fetch_sentiment_data(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    db: AsyncSession
) -> pd.DataFrame:
    """
    Backward-compatible function for sentiment data fetching.
    
    Args:
        symbol: Stock symbol
        start_date: Start date
        end_date: End date
        db: Database session
        
    Returns:
        DataFrame with sentiment data
    """
    extractor = SentimentFeatureExtractor()
    return await extractor._fetch_sentiment_data(symbol, start_date, end_date, db)


async def compute_sentiment_features(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    db: AsyncSession,
    lookback_days: int = 30
) -> pd.DataFrame:
    """
    Production-grade sentiment feature computation.
    
    Args:
        symbol: Stock symbol
        start_date: Start date
        end_date: End date
        db: Database session
        lookback_days: Lookback period for rolling calculations
        
    Returns:
        DataFrame with 5 sentiment features
    """
    extractor = SentimentFeatureExtractor(lookback_days=lookback_days)
    return await extractor.extract_features(symbol, start_date, end_date, db)


# Backward compatibility functions
async def compute_all_sentiment_features(
    symbols: List[str],
    start_date: datetime,
    end_date: datetime,
    db: AsyncSession
) -> Dict[str, pd.DataFrame]:
    """
    Compute sentiment features for multiple symbols.
    
    Args:
        symbols: List of stock symbols
        start_date: Start date
        end_date: End date
        db: Database session
        
    Returns:
        Dictionary mapping symbol to sentiment features DataFrame
    """
    extractor = SentimentFeatureExtractor()
    results = {}
    
    for symbol in symbols:
        try:
            features = await extractor.extract_features(symbol, start_date, end_date, db)
            results[symbol] = features
            logger.info(f"✓ Computed sentiment features for {symbol}")
        except Exception as e:
            logger.error(f"Failed to compute sentiment features for {symbol}: {e}")
            results[symbol] = extractor._create_empty_features(start_date, end_date)
    
    return results


def get_sentiment_feature_names() -> List[str]:
    """
    Get list of sentiment feature names.
    
    Returns:
        List of feature names
    """
    return [
        'sentiment_score_mean',
        'sentiment_score_std', 
        'sentiment_volatility',
        'news_volume',
        'impact_weighted_sentiment'
    ]


def merge_sentiment_with_ohlcv(
    ohlcv_data: pd.DataFrame,
    sentiment_data: pd.DataFrame
) -> pd.DataFrame:
    """
    Merge sentiment features with OHLCV data.
    
    Args:
        ohlcv_data: OHLCV DataFrame with datetime index
        sentiment_data: Sentiment features DataFrame with datetime index
        
    Returns:
        Merged DataFrame
    """
    # Ensure both have datetime index
    if not isinstance(ohlcv_data.index, pd.DatetimeIndex):
        ohlcv_data.index = pd.to_datetime(ohlcv_data.index)
    if not isinstance(sentiment_data.index, pd.DatetimeIndex):
        sentiment_data.index = pd.to_datetime(sentiment_data.index)

    # Normalize timezone: strip tz from both sides so the join never raises
    # "Cannot join tz-naive with tz-aware DatetimeIndex".  We always work in
    # UTC-normalised dates at the daily granularity, so stripping tz info here
    # is safe — the daily bars carry no intraday time component.
    if ohlcv_data.index.tz is not None:
        ohlcv_data = ohlcv_data.copy()
        ohlcv_data.index = ohlcv_data.index.tz_localize(None)
    if not sentiment_data.empty and sentiment_data.index.tz is not None:
        sentiment_data = sentiment_data.copy()
        sentiment_data.index = sentiment_data.index.tz_localize(None)

    # Merge on index (date)
    merged = ohlcv_data.join(sentiment_data, how='left')
    
    # Fill missing sentiment values with neutral (0.0)
    sentiment_cols = get_sentiment_feature_names()
    for col in sentiment_cols:
        if col in merged.columns:
            merged[col] = merged[col].fillna(0.0)
    
    return merged
