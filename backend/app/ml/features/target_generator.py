"""
Target Variable Generator Module

Creates binary target variables (UP/DOWN) for ML model training with ATR-normalized thresholds.
Implements industry-standard selective classification approach.

Production-grade implementation with comprehensive validation and analysis.

Author: Cortex AI Team
Date: 2026-04-18
"""

import pandas as pd
import numpy as np
from typing import Dict, Tuple, Optional, List
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
import logging

from app.models.upstox_data import UpstoxOHLCV

logger = logging.getLogger(__name__)


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Calculate Average True Range (ATR) for volatility normalization.
    
    Args:
        df: DataFrame with OHLCV data
        period: ATR period (default 14)
        
    Returns:
        Series with ATR values
    """
    high = df['high']
    low = df['low']
    close = df['close']
    
    # True Range components
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    
    # True Range = max of three components
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # ATR = EMA of True Range
    atr = tr.ewm(span=period, adjust=False).mean()
    
    return atr


def create_target_variable(
    df: pd.DataFrame,
    atr_multiplier: float = 0.5,
    horizon: int = 5,
    price_col: str = 'close',
    use_atr_normalization: bool = True,
) -> pd.Series:
    """
    Create binary target with a symmetric ATR-adaptive dead zone.

    Labelling logic:
        future_return  >  +threshold  →  1  (UP)
        future_return  <  -threshold  →  0  (DOWN)
        |future_return| ≤  threshold  →  NaN (dead zone — excluded from training)

    The symmetric dead zone is the critical difference from a naive one-sided
    threshold: by excluding near-zero moves from *both* sides we prevent the
    class imbalance that causes a model to learn "always predict DOWN".

    Target distribution with default params (horizon=5, multiplier=0.5):
        • Dead zone  ≈ 20–30 % of trading days (noise-filtered)
        • UP / DOWN  ≈ 50 / 50 on the retained samples (naturally balanced)

    HOLD is NOT a training label — it is applied at inference time via a
    confidence threshold on the UP probability (Selective Classification).

    Args:
        df:                    DataFrame with OHLCV columns.
        atr_multiplier:        ATR fraction used as half-width of dead zone.
                               0.5 × 14-day ATR gives a narrow but meaningful band.
        horizon:               Look-ahead in bars (5 for daily swing trading).
        price_col:             Close price column name.
        use_atr_normalization: Adapt threshold per-stock via ATR (recommended).

    Returns:
        pd.Series of float64: 1.0 (UP), 0.0 (DOWN), or NaN (dead zone / no data).
        Callers must call .dropna() before passing to a trainer.
    """
    if price_col not in df.columns:
        raise ValueError(f"Column '{price_col}' not found in DataFrame")

    future_price = df[price_col].shift(-horizon)
    current_price = df[price_col]
    future_return = (future_price - current_price) / current_price

    if use_atr_normalization:
        if 'high' not in df.columns or 'low' not in df.columns:
            logger.warning("High/Low columns missing — using fixed 0.5 % dead-zone threshold")
            threshold = 0.005
        else:
            atr = calculate_atr(df, period=14)
            # Express ATR as a fraction of price, clamp to avoid degenerate cases
            threshold = (atr_multiplier * atr / current_price).clip(lower=0.001).fillna(0.005)
    else:
        threshold = 0.005  # 0.5 % symmetric fixed band

    # Symmetric labelling — everything inside the band becomes NaN (dead zone)
    target = pd.Series(np.nan, index=df.index, dtype=np.float64)
    target[future_return > threshold] = 1.0   # UP
    target[future_return < -threshold] = 0.0  # DOWN
    # Rows where |return| ≤ threshold remain NaN (dead zone)

    # No future data available for the last `horizon` rows
    target.iloc[-horizon:] = np.nan

    # Diagnostics
    n_labeled = int((target == 1).sum() + (target == 0).sum())
    n_total = len(target) - horizon
    dead_pct = 100.0 * (1 - n_labeled / max(n_total, 1))
    up_pct = 100.0 * float((target == 1).sum()) / max(n_labeled, 1)
    avg_thr = float(threshold.mean()) if isinstance(threshold, pd.Series) else float(threshold)
    logger.info(
        "Target created — horizon=%dd  multiplier=%.2f  avg_threshold=%.3f%%  "
        "dead_zone=%.1f%%  UP=%.1f%%  DOWN=%.1f%%  labeled=%d",
        horizon, atr_multiplier, avg_thr * 100,
        dead_pct, up_pct, 100.0 - up_pct, n_labeled,
    )

    return target


def analyze_class_distribution(
    targets: pd.Series,
    symbol: Optional[str] = None
) -> Dict[str, float]:
    """
    Analyze class distribution in binary target variable.
    
    Args:
        targets: Series with binary target values (0=DOWN, 1=UP)
        symbol: Optional symbol name for logging
        
    Returns:
        Dict with class counts and percentages
    """
    # Remove NaN values
    valid_targets = targets.dropna()
    
    if len(valid_targets) == 0:
        logger.warning("No valid targets to analyze")
        return {}
    
    # Count each class (binary)
    up_count = (valid_targets == 1).sum()
    down_count = (valid_targets == 0).sum()
    total = len(valid_targets)
    
    distribution = {
        'total_samples': total,
        'up_count': int(up_count),
        'down_count': int(down_count),
        'up_pct': float(up_count / total * 100),
        'down_pct': float(down_count / total * 100)
    }
    
    symbol_str = f" for {symbol}" if symbol else ""
    logger.info(
        f"Binary class distribution{symbol_str}: "
        f"UP={distribution['up_pct']:.1f}%, "
        f"DOWN={distribution['down_pct']:.1f}%"
    )
    
    return distribution


def check_class_imbalance(
    distribution: Dict[str, float],
    imbalance_threshold: float = 0.65
) -> Tuple[bool, str]:
    """
    Check if binary class distribution is imbalanced.
    
    Note: Binary classification naturally produces ~50/50 balance with ATR thresholds.
    
    Args:
        distribution: Class distribution dict from analyze_class_distribution
        imbalance_threshold: Threshold for imbalance (default 65%)
        
    Returns:
        Tuple of (is_imbalanced, warning_message)
    """
    if not distribution:
        return False, ""
    
    max_pct = max(
        distribution['up_pct'],
        distribution['down_pct']
    ) / 100
    
    is_imbalanced = max_pct > imbalance_threshold
    
    if is_imbalanced:
        warning = (
            f"Class imbalance detected: max class = {max_pct*100:.1f}%. "
            f"Consider adjusting ATR multiplier or using class weights."
        )
        logger.warning(warning)
        return True, warning
    
    return False, ""


def add_target_to_features(
    features_df: pd.DataFrame,
    atr_multiplier: float = 0.5,
    horizon: int = 5,
    price_col: str = 'close',
    use_atr_normalization: bool = True,
) -> pd.DataFrame:
    """
    Add binary target and raw forward return to a features DataFrame.

    Args:
        features_df: DataFrame with features and OHLCV data
        atr_multiplier: ATR multiplier for threshold
        horizon: Prediction horizon in days
        price_col: Price column name
        use_atr_normalization: Use ATR-normalized thresholds

    Returns:
        DataFrame with 'target' (0=DOWN, 1=UP) and 'forward_return'
        (h-bar fractional price change) columns added.  Both columns share
        the same NaN mask so dropna(subset=['target']) removes the same rows
        from both.
    """
    result = features_df.copy()

    # Raw h-bar forward return — needed for financial metrics (Sharpe, drawdown, …)
    result['forward_return'] = (
        result[price_col].shift(-horizon) / result[price_col] - 1
    )

    # Binary target built from the same forward-return series
    target = create_target_variable(
        result, atr_multiplier, horizon, price_col, use_atr_normalization
    )
    result['target'] = target

    # Analyze distribution
    symbol = result['symbol'].iloc[0] if 'symbol' in result.columns else None
    distribution = analyze_class_distribution(target, symbol)

    # Check for imbalance
    check_class_imbalance(distribution)

    # Drop dead-zone / no-future-data rows — affects both target and forward_return
    initial_rows = len(result)
    result = result.dropna(subset=['target'])
    dropped_rows = initial_rows - len(result)

    if dropped_rows > 0:
        logger.info(f"Dropped {dropped_rows} rows with NaN target")

    return result


def create_targets_batch(
    features_dict: Dict[str, pd.DataFrame],
    atr_multiplier: float = 0.5,
    horizon: int = 5,
    price_col: str = 'close',
    use_atr_normalization: bool = True,
) -> Dict[str, pd.DataFrame]:
    """
    Create binary targets for multiple symbols in batch.
    
    Args:
        features_dict: Dict mapping symbol to features DataFrame
        atr_multiplier: ATR multiplier for threshold
        horizon: Prediction horizon in days
        price_col: Price column name
        use_atr_normalization: Use ATR-normalized thresholds
        
    Returns:
        Dict mapping symbol to features DataFrame with binary targets
    """
    logger.info(f"Creating binary targets for {len(features_dict)} symbols...")
    
    results = {}
    all_distributions = []
    
    for symbol, features_df in features_dict.items():
        try:
            # Add binary target
            df_with_target = add_target_to_features(
                features_df, atr_multiplier, horizon, price_col, use_atr_normalization
            )
            
            results[symbol] = df_with_target
            
            # Collect distribution
            if 'target' in df_with_target.columns:
                dist = analyze_class_distribution(df_with_target['target'], symbol)
                dist['symbol'] = symbol
                all_distributions.append(dist)
            
        except Exception as e:
            logger.error(f"Error creating target for {symbol}: {e}", exc_info=True)
            continue
    
    # Log aggregate statistics — filter out empty dicts (symbols with zero valid targets)
    valid_dists = [d for d in all_distributions if 'up_pct' in d]
    if valid_dists:
        avg_up = np.mean([d['up_pct'] for d in valid_dists])
        avg_down = np.mean([d['down_pct'] for d in valid_dists])
        
        logger.info(
            f"Average binary class distribution across {len(valid_dists)} symbols: "
            f"UP={avg_up:.1f}%, DOWN={avg_down:.1f}%"
        )
    
    return results


def get_class_weights(
    targets,  # Can be pd.Series or np.ndarray
    method: str = 'balanced'
) -> Dict[int, float]:
    """
    Calculate class weights for handling imbalance in binary classification.
    
    Args:
        targets: Series or array with binary target values (0=DOWN, 1=UP)
        method: Weighting method ('balanced' or 'inverse')
        
    Returns:
        Dict mapping class label to weight {0: weight_down, 1: weight_up}
    """
    # Convert to pandas Series if numpy array
    if isinstance(targets, np.ndarray):
        targets = pd.Series(targets)
    
    valid_targets = targets.dropna()
    
    if len(valid_targets) == 0:
        return {0: 1.0, 1: 1.0}
    
    # Count each class
    class_counts = valid_targets.value_counts()
    total = len(valid_targets)
    n_classes = 2  # Binary classification
    
    if method == 'balanced':
        # sklearn-style balanced weights: n_samples / (n_classes * n_samples_class)
        weights = {}
        for class_label in [0, 1]:
            if class_label in class_counts:
                weights[class_label] = total / (n_classes * class_counts[class_label])
            else:
                weights[class_label] = 1.0
    
    elif method == 'inverse':
        # Simple inverse frequency
        weights = {}
        for class_label in [0, 1]:
            if class_label in class_counts:
                weights[class_label] = 1.0 / (class_counts[class_label] / total)
            else:
                weights[class_label] = 1.0
    
    else:
        raise ValueError(f"Unknown weighting method: {method}")
    
    logger.info(f"Binary class weights ({method}): {weights}")
    
    return weights


def split_features_targets(
    df: pd.DataFrame,
    feature_cols: Optional[list] = None
) -> Tuple[pd.DataFrame, pd.Series]:
    """
    Split DataFrame into features (X) and targets (y)
    
    Args:
        df: DataFrame with features and target column
        feature_cols: List of feature columns (if None, auto-detect)
        
    Returns:
        Tuple of (X features DataFrame, y target Series)
    """
    if 'target' not in df.columns:
        raise ValueError("DataFrame must have 'target' column")
    
    # Auto-detect feature columns if not provided
    if feature_cols is None:
        exclude_cols = ['target', 'timestamp', 'symbol', 'open', 'high', 'low', 'close', 'volume']
        feature_cols = [col for col in df.columns if col not in exclude_cols]
    
    X = df[feature_cols].copy()
    y = df['target'].copy()
    
    logger.info(f"Split data: X shape={X.shape}, y shape={y.shape}")
    
    return X, y


def validate_targets(
    targets: pd.Series,
    expected_classes: list = [0, 1]
) -> Tuple[bool, list]:
    """
    Validate binary target variable.
    
    Args:
        targets: Series with binary target values (0=DOWN, 1=UP)
        expected_classes: List of expected class labels
        
    Returns:
        Tuple of (is_valid, list of error messages)
    """
    errors = []
    
    valid_targets = targets.dropna()
    
    if len(valid_targets) == 0:
        errors.append("All target values are NaN")
        return False, errors
    
    # Check for unexpected values
    unique_values = valid_targets.unique()
    unexpected = [v for v in unique_values if v not in expected_classes]
    if unexpected:
        errors.append(f"Unexpected target values: {unexpected}")
    
    # Check if all classes present
    missing_classes = [c for c in expected_classes if c not in unique_values]
    if missing_classes:
        errors.append(f"Missing classes: {missing_classes}")
    
    # Check for extreme imbalance (>80% one class)
    distribution = analyze_class_distribution(targets)
    if distribution:
        max_pct = max(distribution['up_pct'], distribution['down_pct'])
        if max_pct > 80:
            errors.append(f"Severe class imbalance: max class = {max_pct:.1f}%")
    
    is_valid = len(errors) == 0
    
    return is_valid, errors



async def create_targets_from_db(
    symbols: List[str],
    timeframe: str,
    db: AsyncSession,
    atr_multiplier: float = 0.5,
    horizon: int = 5,
    use_atr_normalization: bool = True,
) -> Dict[str, Dict]:
    """
    Create binary targets for symbols by fetching OHLCV from database.
    
    Args:
        symbols: List of instrument keys
        timeframe: Timeframe (e.g., '1D')
        atr_multiplier: ATR multiplier for threshold
        horizon: Prediction horizon in days
        db: Database session
        use_atr_normalization: Use ATR-normalized thresholds
        
    Returns:
        Dict mapping symbol to {'target': np.array, 'timestamps': list}
    """
    logger.info(f"Creating binary targets for {len(symbols)} symbols from database...")
    
    results = {}
    
    for symbol in symbols:
        try:
            # Fetch OHLCV data
            stmt = select(UpstoxOHLCV).where(
                and_(
                    UpstoxOHLCV.instrument_key == symbol,
                    UpstoxOHLCV.timeframe == timeframe
                )
            ).order_by(UpstoxOHLCV.timestamp)
            
            result = await db.execute(stmt)
            rows = result.scalars().all()
            
            if not rows:
                logger.warning(f"No OHLCV data found for {symbol}")
                continue
            
            # Convert to DataFrame
            df = pd.DataFrame([{
                'timestamp': row.timestamp,
                'open': float(row.open),
                'high': float(row.high),
                'low': float(row.low),
                'close': float(row.close),
                'volume': int(row.volume)
            } for row in rows])
            
            # Create binary target
            target_series = create_target_variable(
                df, atr_multiplier, horizon, 'close', use_atr_normalization
            )
            
            # Remove NaN values
            valid_mask = ~target_series.isna()
            target_array = target_series[valid_mask].values
            timestamps = df.loc[valid_mask, 'timestamp'].tolist()
            
            results[symbol] = {
                'target': target_array,
                'timestamps': timestamps
            }
            
            logger.info(f"Created {len(target_array)} binary targets for {symbol}")
            
        except Exception as e:
            logger.error(f"Error creating targets for {symbol}: {e}", exc_info=True)
            continue
    
    logger.info(f"Successfully created targets for {len(results)}/{len(symbols)} symbols")
    
    return results
