"""
Feature Pipeline Module

Orchestrates feature computation, normalization, and sequence creation for ML training.
Handles batch processing for multiple symbols with production-grade error handling.

Author: Cortex AI Team
Date: 2026-04-12
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
import logging

from app.models.upstox_data import UpstoxOHLCV
from app.ml.features.ohlcv_features import compute_all_technical_features, get_feature_names
from app.ml.features.sentiment_features import compute_sentiment_features, merge_sentiment_with_ohlcv
from app.ml.features.sentiment_features import (
    compute_all_sentiment_features,
    get_sentiment_feature_names,
    merge_sentiment_with_ohlcv
)
from app.ml.features.fundamental_features import (
    compute_fundamental_features,
    get_fundamental_feature_names,
)

logger = logging.getLogger(__name__)


async def fetch_ohlcv_data(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    timeframe: str,
    db: AsyncSession
) -> pd.DataFrame:
    """
    Fetch OHLCV data from database
    
    Args:
        symbol: Stock symbol
        start_date: Start date
        end_date: End date
        timeframe: Timeframe (e.g., '1D')
        db: Database session
        
    Returns:
        DataFrame with OHLCV data
    """
    stmt = select(UpstoxOHLCV).where(
        and_(
            UpstoxOHLCV.instrument_key == symbol,
            UpstoxOHLCV.timeframe == timeframe,
            UpstoxOHLCV.timestamp >= start_date,
            UpstoxOHLCV.timestamp <= end_date
        )
    ).order_by(UpstoxOHLCV.timestamp)
    
    result = await db.execute(stmt)
    rows = result.scalars().all()
    
    if not rows:
        logger.warning(f"No OHLCV data found for {symbol} between {start_date} and {end_date}")
        return pd.DataFrame()
    
    # Convert to DataFrame
    data = [{
        'timestamp': row.timestamp,
        'open': float(row.open),
        'high': float(row.high),
        'low': float(row.low),
        'close': float(row.close),
        'volume': int(row.volume)
    } for row in rows]
    
    df = pd.DataFrame(data)
    logger.info(f"Fetched {len(df)} OHLCV rows for {symbol}")
    
    return df


async def compute_features_for_symbol(
    symbol: str,
    start_date: datetime,
    end_date: datetime,
    timeframe: str,
    db: AsyncSession,
    include_sentiment: bool = True,
    include_fundamentals: bool = True,
) -> pd.DataFrame:
    """
    Compute the full feature set for a single symbol.

    Feature groups (when all flags True):
      44  technical  — OHLCV-derived indicators
       5  sentiment  — FinBERT NLP scores
      20  fundamental — cross-sectional company-level ratios
      ──
      69  total

    Fundamental semantics
    ---------------------
    Fundamentals are computed once as of ``end_date`` and broadcast to every
    time step for that symbol.  They are company-level characteristics (updated
    quarterly), not daily signals, so a single point-in-time value per training
    window is correct and intentional.

    NaN handling contract
    ---------------------
    Non-fundamental columns: filled immediately (ffill → bfill → 0).
    Fundamental columns with None DB values: left as NaN so the caller
    (``compute_features_batch``) can apply cross-sectional median imputation
    across the full symbol universe — which is materially more accurate than
    filling with 0 for features like PE ratio, debt ratio, promoter holding, etc.

    Args:
        symbol:               Upstox instrument_key or NSE trading symbol.
        start_date:           Start of the OHLCV window.
        end_date:             End of the OHLCV window; used as ``as_of_date``
                              for point-in-time fundamental queries.
        timeframe:            Candle timeframe (e.g. '1D').
        db:                   Async SQLAlchemy session.
        include_sentiment:    Include the 5 FinBERT sentiment features.
        include_fundamentals: Include the 20 fundamental company-level features.

    Returns:
        DataFrame with all requested features.  Non-fundamental NaN values are
        filled.  Fundamental NaN values are intentionally left intact for
        batch-level median imputation by the caller.
    """
    logger.info("Computing features for %s  [%s → %s]", symbol, start_date.date(), end_date.date())

    ohlcv_df = await fetch_ohlcv_data(symbol, start_date, end_date, timeframe, db)
    if ohlcv_df.empty:
        logger.error("No OHLCV data for %s — cannot compute features", symbol)
        return pd.DataFrame()

    # ── 44 technical features ──────────────────────────────────────────────────
    features_df = compute_all_technical_features(ohlcv_df)

    # ── +5 sentiment features ──────────────────────────────────────────────────
    if include_sentiment:
        sentiment_df = await compute_sentiment_features(symbol, start_date, end_date, db)
        features_df = merge_sentiment_with_ohlcv(features_df, sentiment_df)

    # ── +20 fundamental features (cross-sectional; broadcast to all rows) ──────
    fund_col_set: frozenset[str] = frozenset()
    if include_fundamentals:
        fund_feats = await compute_fundamental_features(symbol, end_date.date(), db)
        fund_col_set = frozenset(get_fundamental_feature_names())
        for feat_name, feat_val in fund_feats.items():
            # None → NaN: preserved for cross-sectional median imputation in
            # compute_features_batch; filling with 0 here would corrupt features
            # like pe_ratio or debt_ratio where 0 carries real financial meaning.
            features_df[feat_name] = float(feat_val) if feat_val is not None else np.nan

    features_df["symbol"] = symbol

    # Fill NaN for non-fundamental columns only (tech + sentiment + metadata).
    # Fundamental NaN values are intentionally preserved for batch-level imputation.
    non_fund_cols = [c for c in features_df.columns if c not in fund_col_set]
    features_df[non_fund_cols] = features_df[non_fund_cols].ffill().bfill().fillna(0)

    logger.info("Computed %d feature rows for %s", len(features_df), symbol)
    return features_df


async def compute_features_batch(
    symbols: List[str],
    start_date: datetime,
    end_date: datetime,
    timeframe: str,
    db: AsyncSession,
    include_sentiment: bool = True,
    include_fundamentals: bool = True,
) -> Dict[str, pd.DataFrame]:
    """
    Compute features for multiple symbols in batch.

    Fundamental imputation
    ----------------------
    When ``include_fundamentals=True``, per-symbol fundamental values computed
    by ``compute_features_for_symbol`` may be NaN for newly listed companies or
    symbols with insufficient financial history.  After all per-symbol passes
    complete this function applies **cross-sectional median imputation**: for
    each fundamental feature, the median across all symbols that have a valid
    value is substituted for any remaining NaN.  If the entire universe lacks
    values for a feature (first-ever run with no fundamentals data), 0 is used
    as the last resort — but never as the first choice.

    This matches the contract documented in fundamental_features.py:
    "NaN values are handled by median imputation in the feature pipeline —
    never by filling with 0."

    Returns:
        Dict mapping symbol → feature DataFrame.  All DataFrames are guaranteed
        to have no NaN values on return.
    """
    logger.info("Computing features for %d symbols...", len(symbols))

    results: Dict[str, pd.DataFrame] = {}

    for i, symbol in enumerate(symbols, 1):
        try:
            logger.info("Processing %d/%d: %s", i, len(symbols), symbol)
            features_df = await compute_features_for_symbol(
                symbol, start_date, end_date, timeframe, db,
                include_sentiment=include_sentiment,
                include_fundamentals=include_fundamentals,
            )
            if not features_df.empty:
                results[symbol] = features_df
            else:
                logger.warning("No features computed for %s", symbol)
        except Exception as exc:
            logger.error("Error computing features for %s: %s", symbol, exc, exc_info=True)
            continue

    logger.info("Per-symbol computation complete: %d/%d symbols", len(results), len(symbols))

    # ── Cross-sectional median imputation for fundamental features ─────────────
    # Fundamental columns may be NaN for symbols with insufficient history.
    # We collect one representative value per symbol (all rows carry the same
    # broadcast value, so iloc[0] of a non-NaN series is sufficient), compute
    # the universe median per feature, and fill.  Symbols with no valid peers
    # for a given feature receive 0 as the final fallback.
    if include_fundamentals and results:
        fund_cols = get_fundamental_feature_names()

        medians: Dict[str, float] = {}
        for feat in fund_cols:
            vals: List[float] = []
            for df in results.values():
                if feat in df.columns:
                    non_null = df[feat].dropna()
                    if not non_null.empty:
                        vals.append(float(non_null.iloc[0]))
            medians[feat] = float(np.median(vals)) if vals else 0.0

        n_imputed = 0
        for symbol, df in results.items():
            symbol_had_nulls = False
            for feat in fund_cols:
                if feat in df.columns and df[feat].isna().any():
                    df[feat] = df[feat].fillna(medians[feat])
                    symbol_had_nulls = True
            if symbol_had_nulls:
                n_imputed += 1

        logger.info(
            "Fundamental median imputation: %d features  %d/%d symbols required imputation",
            len(fund_cols), n_imputed, len(results),
        )

    logger.info("✓ Batch feature computation complete: %d/%d symbols", len(results), len(symbols))
    return results


def normalize_features(
    df: pd.DataFrame,
    method: str = 'rolling',
    window: int = 60,
    feature_cols: Optional[List[str]] = None
) -> pd.DataFrame:
    """
    Production-grade feature normalization with comprehensive NaN handling.
    
    Args:
        df: DataFrame with features
        method: Normalization method ('rolling' or 'standard')
        window: Rolling window size (for rolling method)
        feature_cols: List of feature columns to normalize (if None, auto-detect)
        
    Returns:
        DataFrame with normalized features (guaranteed no NaN values)
    """
    if df.empty:
        logger.warning("Empty DataFrame provided for normalization")
        return df.copy()
    
    result = df.copy()
    
    # Auto-detect feature columns if not provided
    if feature_cols is None:
        exclude_cols = ['timestamp', 'symbol', 'open', 'high', 'low', 'close', 'volume']
        feature_cols = [col for col in df.columns if col not in exclude_cols]
    
    logger.info(f"Normalizing {len(feature_cols)} features using {method} method")
    
    # Validate input data
    for col in feature_cols:
        if col not in result.columns:
            logger.warning(f"Feature column {col} not found in DataFrame")
            continue
        
        # Check for all NaN column
        if result[col].isna().all():
            logger.warning(f"Column {col} is all NaN, filling with zeros")
            result[col] = 0.0
            continue
        
        # Fill any existing NaN values with forward fill, then backward fill, then zero
        result[col] = result[col].ffill().bfill().fillna(0.0)
    
    if method == 'rolling':
        # Rolling z-score normalization (prevents look-ahead bias)
        for col in feature_cols:
            if col not in result.columns:
                continue
                
            # Calculate rolling statistics with robust handling
            rolling_mean = result[col].rolling(window=window, min_periods=1).mean()
            rolling_std = result[col].rolling(window=window, min_periods=1).std()
            
            # Handle edge cases where std is 0 or NaN
            rolling_std = rolling_std.fillna(1.0)  # Fill NaN std with 1.0
            rolling_std = np.where(rolling_std < 1e-8, 1.0, rolling_std)  # Avoid division by zero
            
            # Normalize
            normalized = (result[col] - rolling_mean) / rolling_std
            
            # Handle any remaining NaN values
            normalized = normalized.fillna(0.0)
            
            result[col] = normalized
    
    elif method == 'standard':
        # Standard z-score normalization (use only for training data)
        for col in feature_cols:
            if col not in result.columns:
                continue
                
            mean = result[col].mean()
            std = result[col].std()
            
            # Handle edge cases
            if pd.isna(mean):
                mean = 0.0
            if pd.isna(std) or std < 1e-8:
                std = 1.0
            
            normalized = (result[col] - mean) / std
            normalized = normalized.fillna(0.0)
            
            result[col] = normalized
    
    else:
        raise ValueError(f"Unknown normalization method: {method}")
    
    # Clip extreme values to [-5, 5] range and ensure no NaN
    for col in feature_cols:
        if col in result.columns:
            result[col] = result[col].clip(-5, 5)
            result[col] = result[col].fillna(0.0)  # Final NaN safety check
            
            # Validate no infinite values
            result[col] = result[col].replace([np.inf, -np.inf], 0.0)
    
    # Final validation — filter to columns that actually exist before indexing.
    # Requesting feature_cols that are absent from the DataFrame raises a KeyError
    # in pandas; guarding here makes the function robust to any caller/schema drift.
    present_cols = [c for c in feature_cols if c in result.columns]

    nan_cols = result[present_cols].columns[result[present_cols].isna().any()].tolist()
    if nan_cols:
        logger.error("NaN values still present after normalization: %s", nan_cols)
        for col in nan_cols:
            result[col] = result[col].fillna(0.0)

    inf_cols = result[present_cols].columns[np.isinf(result[present_cols]).any()].tolist()
    if inf_cols:
        logger.error("Infinite values present after normalization: %s", inf_cols)
        for col in inf_cols:
            result[col] = result[col].replace([np.inf, -np.inf], 0.0)
    
    logger.info("✓ Normalization complete - all values finite and valid")
    
    return result


def create_sequences(
    df: pd.DataFrame,
    sequence_length: int = 60,
    feature_cols: Optional[List[str]] = None
) -> Tuple[np.ndarray, np.ndarray, pd.DatetimeIndex]:
    """
    Create sequences for LSTM/GRU input
    
    Args:
        df: DataFrame with normalized features
        sequence_length: Length of each sequence (e.g., 60 days)
        feature_cols: List of feature columns (if None, auto-detect)
        
    Returns:
        Tuple of (X sequences, timestamps, indices)
        - X: shape (samples, sequence_length, n_features)
        - timestamps: timestamps for each sequence
        - indices: original DataFrame indices
    """
    # Auto-detect feature columns if not provided
    if feature_cols is None:
        exclude_cols = ['timestamp', 'symbol', 'open', 'high', 'low', 'close', 'volume']
        feature_cols = [col for col in df.columns if col not in exclude_cols]
    
    logger.info(f"Creating sequences with length={sequence_length}, features={len(feature_cols)}")
    
    # Extract feature values
    feature_values = df[feature_cols].values
    timestamps = df['timestamp'].values
    
    # Create sequences
    X = []
    seq_timestamps = []
    seq_indices = []
    
    for i in range(len(feature_values) - sequence_length + 1):
        # Extract sequence
        sequence = feature_values[i:i + sequence_length]
        X.append(sequence)
        
        # Store timestamp of last day in sequence
        seq_timestamps.append(timestamps[i + sequence_length - 1])
        seq_indices.append(i + sequence_length - 1)
    
    X = np.array(X)
    seq_timestamps = pd.DatetimeIndex(seq_timestamps)
    
    logger.info(f"Created {len(X)} sequences with shape {X.shape}")
    
    return X, seq_timestamps, seq_indices


def get_all_feature_names(
    include_sentiment: bool = True,
    include_fundamentals: bool = True,
) -> List[str]:
    """
    Return the ordered feature name list used by the ML models.

    Counts (confirmed from source):
      44 technical (ohlcv_features.get_feature_names)
       5 sentiment (sentiment_features.get_sentiment_feature_names)
      20 fundamental (fundamental_features.get_fundamental_feature_names)
      ──
      69 total (when all flags are True)
    """
    features = get_feature_names()  # 44 technical features

    if include_sentiment:
        features.extend(get_sentiment_feature_names())  # +5 = 49

    if include_fundamentals:
        features.extend(get_fundamental_feature_names())  # +20 = 69

    return features


async def prepare_training_data(
    symbols: List[str],
    start_date: datetime,
    end_date: datetime,
    timeframe: str,
    db: AsyncSession,
    sequence_length: int = 60,
    include_sentiment: bool = True,
    include_fundamentals: bool = True,
    normalize: bool = True,
    precomputed_features: Optional[Dict[str, pd.DataFrame]] = None,
) -> Dict[str, Dict[str, np.ndarray]]:
    """
    Prepare complete training data for multiple symbols.

    Args:
        symbols:               List of stock symbols.
        start_date:            Start date.
        end_date:              End date.
        timeframe:             Timeframe (e.g., '1D').
        db:                    Database session.
        sequence_length:       Sequence length for GRU/LSTM input.
        include_sentiment:     Whether to include the 5 sentiment features.
        include_fundamentals:  Whether to include the 20 fundamental features.
        normalize:             Whether to apply rolling z-score normalisation.
        precomputed_features:  Optional dict[symbol → DataFrame] from a prior
                               compute_features_batch() call.  When provided the
                               DB fetch is skipped entirely (avoids the double-fetch
                               that costs ~30 min on 2,200 symbols).

    Returns:
        Dict mapping symbol → {'X', 'timestamps', 'feature_names', 'symbol',
        'n_samples', 'n_features'}.
    """
    logger.info("Preparing training data for %d symbols...", len(symbols))

    # Use caller-supplied features when available — saves ~30 min per run.
    if precomputed_features is not None:
        logger.info("Using precomputed features (skipping DB fetch)")
        features_dict = precomputed_features
    else:
        features_dict = await compute_features_batch(
            symbols, start_date, end_date, timeframe, db,
            include_sentiment=include_sentiment,
            include_fundamentals=include_fundamentals,
        )

    feature_names = get_all_feature_names(
        include_sentiment=include_sentiment,
        include_fundamentals=include_fundamentals,
    )
    
    # Process each symbol
    results = {}
    
    for symbol, features_df in features_dict.items():
        try:
            # Normalize features
            if normalize:
                features_df = normalize_features(features_df, method='rolling', window=60)
            
            # Create sequences
            X, timestamps, _ = create_sequences(
                features_df,
                sequence_length=sequence_length,
                feature_cols=feature_names
            )
            
            results[symbol] = {
                'X': X,
                'timestamps': timestamps,
                'feature_names': feature_names,
                'symbol': symbol,  # Add symbol key
                'n_samples': len(X),
                'n_features': len(feature_names)
            }
            
            logger.info(f"{symbol}: {len(X)} sequences, {len(feature_names)} features")
            
        except Exception as e:
            logger.error(f"Error preparing data for {symbol}: {e}", exc_info=True)
            continue
    
    logger.info(f"Training data prepared for {len(results)} symbols")
    
    return results
