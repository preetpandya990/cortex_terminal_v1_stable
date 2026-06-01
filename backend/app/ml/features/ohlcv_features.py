"""
OHLCV Feature Engineering Module

Computes 44 technical features from OHLCV data for ML model training.
Production-grade implementation with proper error handling and validation.

Author: Cortex AI Team
Date: 2026-04-11
"""

import pandas as pd
import numpy as np
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def compute_price_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute price-based features (8 features)
    
    Args:
        df: DataFrame with OHLCV data (must have 'close' column)
        
    Returns:
        DataFrame with price features added
    """
    result = df.copy()
    
    # Returns
    result['returns_1d'] = result['close'].pct_change()
    result['returns_5d'] = result['close'].pct_change(periods=5)
    result['returns_10d'] = result['close'].pct_change(periods=10)
    result['returns_20d'] = result['close'].pct_change(periods=20)
    
    # Log returns
    result['log_returns'] = np.log(result['close'] / result['close'].shift(1))
    
    # Volatility (20-day rolling std of returns)
    result['volatility_20d'] = result['returns_1d'].rolling(window=20).std()
    
    # Price momentum
    result['price_momentum_5d'] = result['close'] / result['close'].shift(5) - 1
    result['price_momentum_10d'] = result['close'] / result['close'].shift(10) - 1
    
    return result


def compute_ma_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute moving average features (13 features)

    Args:
        df: DataFrame with OHLCV data (must have 'close' column)

    Returns:
        DataFrame with MA features added
    """
    result = df.copy()

    # Simple Moving Averages
    result['sma_5'] = result['close'].rolling(window=5).mean()
    result['sma_10'] = result['close'].rolling(window=10).mean()
    result['sma_20'] = result['close'].rolling(window=20).mean()
    result['sma_50'] = result['close'].rolling(window=50).mean()
    result['sma_200'] = result['close'].rolling(window=200).mean()

    # Exponential Moving Averages
    result['ema_12'] = result['close'].ewm(span=12, adjust=False).mean()
    result['ema_26'] = result['close'].ewm(span=26, adjust=False).mean()
    result['ema_20'] = result['close'].ewm(span=20, adjust=False).mean()
    result['ema_50'] = result['close'].ewm(span=50, adjust=False).mean()

    # SMA Crossovers (normalized)
    result['sma_cross_20_50'] = (result['sma_20'] - result['sma_50']) / result['sma_50']
    result['sma_cross_50_200'] = (result['sma_50'] - result['sma_200']) / result['sma_200']

    # Price to SMA ratio
    result['price_to_sma_20'] = (result['close'] - result['sma_20']) / result['sma_20']

    return result


def compute_momentum_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute momentum indicators (6 features)
    
    Args:
        df: DataFrame with OHLCV data (must have 'close', 'high', 'low' columns)
        
    Returns:
        DataFrame with momentum features added
    """
    result = df.copy()
    
    # RSI (14-day)
    delta = result['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    result['rsi_14'] = 100 - (100 / (1 + rs))
    
    # MACD
    result['macd'] = result['ema_12'] - result['ema_26']
    result['macd_signal'] = result['macd'].ewm(span=9, adjust=False).mean()
    result['macd_hist'] = result['macd'] - result['macd_signal']
    
    # Stochastic Oscillator
    low_14 = result['low'].rolling(window=14).min()
    high_14 = result['high'].rolling(window=14).max()
    result['stoch_k'] = 100 * (result['close'] - low_14) / (high_14 - low_14)
    result['stoch_d'] = result['stoch_k'].rolling(window=3).mean()
    
    return result


def compute_volatility_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute volatility indicators (6 features)
    
    Args:
        df: DataFrame with OHLCV data (must have 'close', 'high', 'low' columns)
        
    Returns:
        DataFrame with volatility features added
    """
    result = df.copy()
    
    # Bollinger Bands
    sma_20 = result['close'].rolling(window=20).mean()
    std_20 = result['close'].rolling(window=20).std()
    result['bb_upper'] = sma_20 + (2 * std_20)
    result['bb_lower'] = sma_20 - (2 * std_20)
    result['bb_width'] = (result['bb_upper'] - result['bb_lower']) / sma_20
    result['bb_position'] = (result['close'] - result['bb_lower']) / (result['bb_upper'] - result['bb_lower'])
    
    # Average True Range (ATR)
    high_low = result['high'] - result['low']
    high_close = np.abs(result['high'] - result['close'].shift())
    low_close = np.abs(result['low'] - result['close'].shift())
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    result['atr_14'] = true_range.rolling(window=14).mean()
    result['atr_ratio'] = result['atr_14'] / result['close']
    
    return result


def compute_volume_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute volume-based features (8 features)
    
    Args:
        df: DataFrame with OHLCV data (must have 'close', 'volume' columns)
        
    Returns:
        DataFrame with volume features added
    """
    result = df.copy()
    
    # Volume ratio
    volume_ma_20 = result['volume'].rolling(window=20).mean()
    result['volume_ratio'] = result['volume'] / volume_ma_20
    
    # Volume momentum
    result['volume_momentum_5d'] = result['volume'].pct_change(periods=5)
    result['volume_momentum_10d'] = result['volume'].pct_change(periods=10)
    
    # On-Balance Volume (OBV)
    obv = (np.sign(result['close'].diff()) * result['volume']).fillna(0).cumsum()
    result['obv'] = obv
    result['obv_ema'] = obv.ewm(span=20, adjust=False).mean()
    
    # VWAP (Volume Weighted Average Price)
    typical_price = (result['high'] + result['low'] + result['close']) / 3
    result['vwap'] = (typical_price * result['volume']).cumsum() / result['volume'].cumsum()
    result['vwap_deviation'] = (result['close'] - result['vwap']) / result['vwap']
    
    # Volume Price Trend
    result['volume_price_trend'] = (result['volume'] * result['close'].pct_change()).cumsum()
    
    return result


def compute_statistical_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute statistical features (4 features)
    
    Args:
        df: DataFrame with OHLCV data (must have 'close' column)
        
    Returns:
        DataFrame with statistical features added
    """
    result = df.copy()
    
    # Skewness and Kurtosis (20-day rolling)
    result['skewness_20d'] = result['close'].rolling(window=20).skew()
    result['kurtosis_20d'] = result['close'].rolling(window=20).kurt()
    
    # Autocorrelation
    result['autocorr_lag1'] = result['close'].rolling(window=20).apply(
        lambda x: x.autocorr(lag=1) if len(x) > 1 else 0, raw=False
    )
    result['autocorr_lag5'] = result['close'].rolling(window=20).apply(
        lambda x: x.autocorr(lag=5) if len(x) > 5 else 0, raw=False
    )
    
    return result


def compute_all_technical_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute all 44 technical features
    
    Args:
        df: DataFrame with OHLCV data (columns: open, high, low, close, volume, timestamp)
        
    Returns:
        DataFrame with all technical features added
        
    Raises:
        ValueError: If required columns are missing
    """
    # Validate input
    required_cols = ['open', 'high', 'low', 'close', 'volume']
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        raise ValueError(f"Missing required columns: {missing_cols}")
    
    if len(df) < 200:
        logger.warning(f"DataFrame has only {len(df)} rows. Some features may have NaN values.")
    
    # Compute features in order (some depend on previous)
    result = df.copy()
    
    logger.info("Computing price features...")
    result = compute_price_features(result)
    
    logger.info("Computing moving average features...")
    result = compute_ma_features(result)
    
    logger.info("Computing momentum features...")
    result = compute_momentum_features(result)
    
    logger.info("Computing volatility features...")
    result = compute_volatility_features(result)
    
    logger.info("Computing volume features...")
    result = compute_volume_features(result)
    
    logger.info("Computing statistical features...")
    result = compute_statistical_features(result)
    
    # Count features
    feature_cols = [col for col in result.columns if col not in required_cols + ['timestamp']]
    logger.info(f"Computed {len(feature_cols)} technical features")
    
    return result


def get_feature_names() -> list:
    """
    Get list of all 44 technical feature names

    Returns:
        List of feature column names
    """
    return [
        # Price features (8)
        'returns_1d', 'returns_5d', 'returns_10d', 'returns_20d',
        'log_returns', 'volatility_20d', 'price_momentum_5d', 'price_momentum_10d',

        # MA features (13)
        'sma_5', 'sma_10', 'sma_20', 'sma_50', 'sma_200',
        'ema_12', 'ema_26', 'ema_20', 'ema_50',
        'sma_cross_20_50', 'sma_cross_50_200', 'price_to_sma_20',

        # Momentum features (6)
        'rsi_14', 'macd', 'macd_signal', 'macd_hist', 'stoch_k', 'stoch_d',

        # Volatility features (6)
        'bb_upper', 'bb_lower', 'bb_width', 'bb_position', 'atr_14', 'atr_ratio',

        # Volume features (8)
        'volume_ratio', 'volume_momentum_5d', 'volume_momentum_10d',
        'obv', 'obv_ema', 'vwap', 'vwap_deviation', 'volume_price_trend',

        # Statistical features (4)
        'skewness_20d', 'kurtosis_20d', 'autocorr_lag1', 'autocorr_lag5'
    ]


def compute_cross_section_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute fully normalized, cross-sectionally comparable features.

    All outputs are scale-invariant so a single global model can train on
    2,500+ stocks simultaneously without price-level leakage between symbols.

    Built on top of compute_all_technical_features(); call this function
    instead of that one when training cross-sectional (multi-stock) models.

    Returns a DataFrame with exactly CROSS_SECTION_FEATURE_NAMES columns.
    NaN rows (insufficient warm-up data) are preserved — callers must dropna.
    """
    raw = compute_all_technical_features(df)
    close = raw["close"]
    result = pd.DataFrame(index=raw.index)

    # ── Price / return features — already scale-free ──────────────────────────
    for col in [
        "returns_1d", "returns_5d", "returns_10d", "returns_20d",
        "log_returns", "volatility_20d",
        "price_momentum_5d", "price_momentum_10d",
    ]:
        result[col] = raw[col]

    # ── Moving averages → price-relative deviations ───────────────────────────
    for n in [5, 10, 20, 50, 200]:
        sma = raw[f"sma_{n}"]
        result[f"price_to_sma_{n}"] = (close - sma) / sma
    result["sma_cross_20_50"] = raw["sma_cross_20_50"]
    result["sma_cross_50_200"] = raw["sma_cross_50_200"]
    result["price_to_ema_12"] = (close - raw["ema_12"]) / raw["ema_12"]
    result["price_to_ema_26"] = (close - raw["ema_26"]) / raw["ema_26"]
    result["price_to_ema_20"] = (close - raw["ema_20"]) / raw["ema_20"]
    result["price_to_ema_50"] = (close - raw["ema_50"]) / raw["ema_50"]
    result["ema_crossover_signal"] = (raw["ema_20"] > raw["ema_50"]).astype(float)

    # ── Momentum oscillators — already scale-free ─────────────────────────────
    result["rsi_14"] = raw["rsi_14"]
    result["macd_norm"] = raw["macd"] / close
    result["macd_signal_norm"] = raw["macd_signal"] / close
    result["macd_hist_norm"] = raw["macd_hist"] / close
    result["stoch_k"] = raw["stoch_k"]
    result["stoch_d"] = raw["stoch_d"]

    # ── Volatility — already scale-free ──────────────────────────────────────
    result["bb_width"] = raw["bb_width"]
    result["bb_position"] = raw["bb_position"]
    result["atr_ratio"] = raw["atr_ratio"]

    # ── Volume — convert cumulative/absolute to rate-of-change ───────────────
    result["volume_ratio"] = raw["volume_ratio"]
    result["volume_momentum_5d"] = raw["volume_momentum_5d"]
    result["volume_momentum_10d"] = raw["volume_momentum_10d"]
    result["obv_pct"] = (
        raw["obv"].pct_change().replace([np.inf, -np.inf], np.nan)
    )
    result["obv_ema_pct"] = (
        raw["obv_ema"].pct_change().replace([np.inf, -np.inf], np.nan)
    )
    result["vwap_deviation"] = raw["vwap_deviation"]
    result["vpt_pct"] = (
        raw["volume_price_trend"].pct_change().replace([np.inf, -np.inf], np.nan)
    )

    # ── Statistical features ──────────────────────────────────────────────────
    result["skewness_20d"] = raw["skewness_20d"]
    result["kurtosis_20d"] = raw["kurtosis_20d"]
    result["autocorr_lag1"] = raw["autocorr_lag1"]
    result["autocorr_lag5"] = raw["autocorr_lag5"]

    # ── Winsorise at ±5 × rolling std to suppress outliers ───────────────────
    for col in result.columns:
        s = result[col]
        lo, hi = s.quantile(0.001), s.quantile(0.999)
        result[col] = s.clip(lo, hi)

    return result[CROSS_SECTION_FEATURE_NAMES]


# Canonical ordered list — never change order; append only.
CROSS_SECTION_FEATURE_NAMES: list[str] = [
    # Price / returns (8)
    "returns_1d", "returns_5d", "returns_10d", "returns_20d",
    "log_returns", "volatility_20d", "price_momentum_5d", "price_momentum_10d",
    # Moving averages — normalized (12)
    "price_to_sma_5", "price_to_sma_10", "price_to_sma_20",
    "price_to_sma_50", "price_to_sma_200",
    "sma_cross_20_50", "sma_cross_50_200",
    "price_to_ema_12", "price_to_ema_26",
    "price_to_ema_20", "price_to_ema_50", "ema_crossover_signal",
    # Momentum oscillators (6)
    "rsi_14", "macd_norm", "macd_signal_norm", "macd_hist_norm",
    "stoch_k", "stoch_d",
    # Volatility (3)
    "bb_width", "bb_position", "atr_ratio",
    # Volume (7)
    "volume_ratio", "volume_momentum_5d", "volume_momentum_10d",
    "obv_pct", "obv_ema_pct", "vwap_deviation", "vpt_pct",
    # Statistical (4)
    "skewness_20d", "kurtosis_20d", "autocorr_lag1", "autocorr_lag5",
]  # 40 features total


def validate_features(df: pd.DataFrame) -> tuple[bool, list[str]]:
    """
    Validate computed features
    
    Args:
        df: DataFrame with computed features
        
    Returns:
        Tuple of (is_valid, list of error messages)
    """
    errors = []
    
    # Check for required features
    expected_features = get_feature_names()
    missing_features = [f for f in expected_features if f not in df.columns]
    if missing_features:
        errors.append(f"Missing features: {missing_features}")
    
    # Check for infinite values
    for col in expected_features:
        if col in df.columns:
            if df[col].isin([np.inf, -np.inf]).any():
                errors.append(f"Feature '{col}' contains infinite values")
    
    # Check for all-NaN columns
    for col in expected_features:
        if col in df.columns:
            if df[col].isna().all():
                errors.append(f"Feature '{col}' is all NaN")
    
    is_valid = len(errors) == 0
    return is_valid, errors
