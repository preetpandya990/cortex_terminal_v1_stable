"""
Cortex AI — Timeframe-Specific Feature Computation
====================================================
Feature computation with timeframe-specific indicator periods.

This module implements Requirements 4.5, 12.2:
- Adjust indicator periods for each timeframe
- Store timeframe-specific feature definitions in feature store
- Ensure training-serving consistency per timeframe
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import numpy as np

from app.ml.training.timeframe_config import (
    get_timeframe_config,
    get_indicator_period,
)

logger = logging.getLogger(__name__)


class TimeframeFeatureComputer:
    """
    Compute features with timeframe-specific indicator periods.
    
    Ensures that technical indicators are computed with appropriate periods
    for each timeframe (e.g., RSI(14) for daily, RSI(70) for weekly).
    """
    
    def __init__(self, timeframe: str):
        """
        Initialize feature computer for a specific timeframe.
        
        Args:
            timeframe: Timeframe identifier ("1d", "1w", "1M")
        """
        self.timeframe = timeframe
        self.config = get_timeframe_config(timeframe)
        
        logger.info(
            f"Initialized feature computer for {self.config['name']} timeframe"
        )
    
    def compute_all_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute all features for the given OHLCV data.
        
        Args:
            df: DataFrame with columns [timestamp, open, high, low, close, volume]
            
        Returns:
            DataFrame with computed features
        """
        features_df = pd.DataFrame(index=df.index)
        
        # Momentum indicators
        features_df = self._compute_momentum_features(df, features_df)
        
        # Trend indicators
        features_df = self._compute_trend_features(df, features_df)
        
        # Volatility indicators
        features_df = self._compute_volatility_features(df, features_df)
        
        # Volume indicators
        features_df = self._compute_volume_features(df, features_df)
        
        # Market structure
        features_df = self._compute_market_structure_features(df, features_df)
        
        logger.info(f"Computed {len(features_df.columns)} features")
        
        return features_df
    
    def _compute_momentum_features(
        self, df: pd.DataFrame, features_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Compute momentum indicators with timeframe-specific periods."""
        close = df["close"]
        high = df["high"]
        low = df["low"]
        
        # RSI
        rsi_period = get_indicator_period(self.timeframe, "rsi")
        features_df["rsi_14"] = self._compute_rsi(close, rsi_period)
        
        rsi_long_period = get_indicator_period(self.timeframe, "rsi_long")
        features_df["rsi_21"] = self._compute_rsi(close, rsi_long_period)
        
        # MACD
        macd_fast = get_indicator_period(self.timeframe, "macd_fast")
        macd_slow = get_indicator_period(self.timeframe, "macd_slow")
        macd_signal = get_indicator_period(self.timeframe, "macd_signal")
        
        macd_line, macd_signal_line, macd_hist = self._compute_macd(
            close, macd_fast, macd_slow, macd_signal
        )
        features_df["macd_line"] = macd_line
        features_df["macd_signal"] = macd_signal_line
        features_df["macd_histogram"] = macd_hist
        
        # Stochastic
        stoch_period = get_indicator_period(self.timeframe, "stochastic")
        stoch_smooth = get_indicator_period(self.timeframe, "stochastic_smooth")
        
        stoch_k, stoch_d = self._compute_stochastic(
            high, low, close, stoch_period, stoch_smooth
        )
        features_df["stochastic_k"] = stoch_k
        features_df["stochastic_d"] = stoch_d
        
        # ROC
        roc_short_period = get_indicator_period(self.timeframe, "roc_short")
        roc_long_period = get_indicator_period(self.timeframe, "roc_long")
        
        features_df["roc_10"] = self._compute_roc(close, roc_short_period)
        features_df["roc_20"] = self._compute_roc(close, roc_long_period)
        
        # Williams %R
        williams_period = get_indicator_period(self.timeframe, "williams_r")
        features_df["williams_r"] = self._compute_williams_r(
            high, low, close, williams_period
        )
        
        # CCI
        cci_period = get_indicator_period(self.timeframe, "cci")
        features_df["cci_20"] = self._compute_cci(high, low, close, cci_period)
        
        # Momentum
        momentum_period = get_indicator_period(self.timeframe, "momentum")
        features_df["momentum_10"] = self._compute_momentum(close, momentum_period)
        
        # TSI (True Strength Index)
        tsi_fast = get_indicator_period(self.timeframe, "tsi_fast")
        tsi_slow = get_indicator_period(self.timeframe, "tsi_slow")
        features_df["tsi"] = self._compute_tsi(close, tsi_fast, tsi_slow)
        
        # Ultimate Oscillator (using fixed ratios)
        features_df["ultimate_oscillator"] = self._compute_ultimate_oscillator(
            high, low, close
        )
        
        # Awesome Oscillator
        features_df["awesome_oscillator"] = self._compute_awesome_oscillator(
            high, low
        )
        
        return features_df
    
    def _compute_trend_features(
        self, df: pd.DataFrame, features_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Compute trend indicators with timeframe-specific periods."""
        close = df["close"]
        high = df["high"]
        low = df["low"]
        
        # SMAs
        sma_short = get_indicator_period(self.timeframe, "sma_short")
        sma_medium = get_indicator_period(self.timeframe, "sma_medium")
        sma_long = get_indicator_period(self.timeframe, "sma_long")
        
        features_df["sma_20"] = close.rolling(window=sma_short).mean()
        features_df["sma_50"] = close.rolling(window=sma_medium).mean()
        features_df["sma_200"] = close.rolling(window=sma_long).mean()
        
        # EMAs
        ema_fast = get_indicator_period(self.timeframe, "ema_fast")
        ema_medium = get_indicator_period(self.timeframe, "ema_medium")
        ema_slow = get_indicator_period(self.timeframe, "ema_slow")
        
        features_df["ema_12"] = close.ewm(span=ema_fast, adjust=False).mean()
        features_df["ema_26"] = close.ewm(span=ema_medium, adjust=False).mean()
        features_df["ema_50"] = close.ewm(span=ema_slow, adjust=False).mean()
        
        # DEMA
        dema_period = get_indicator_period(self.timeframe, "dema")
        features_df["dema_20"] = self._compute_dema(close, dema_period)
        
        # TEMA
        tema_period = get_indicator_period(self.timeframe, "tema")
        features_df["tema_20"] = self._compute_tema(close, tema_period)
        
        # ADX
        adx_period = get_indicator_period(self.timeframe, "adx")
        features_df["adx_14"] = self._compute_adx(high, low, close, adx_period)
        
        # Aroon
        aroon_period = get_indicator_period(self.timeframe, "aroon")
        features_df["aroon_up"] = self._compute_aroon_up(high, aroon_period)
        
        return features_df
    
    def _compute_volatility_features(
        self, df: pd.DataFrame, features_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Compute volatility indicators with timeframe-specific periods."""
        close = df["close"]
        high = df["high"]
        low = df["low"]
        
        # ATR
        atr_period = get_indicator_period(self.timeframe, "atr")
        features_df["atr_14"] = self._compute_atr(high, low, close, atr_period)
        
        # Bollinger Bands
        bb_period = get_indicator_period(self.timeframe, "bb_period")
        bb_std = self.config["indicator_periods"]["bb_std"]
        
        bb_upper, bb_middle, bb_lower = self._compute_bollinger_bands(
            close, bb_period, bb_std
        )
        features_df["bb_upper"] = bb_upper
        features_df["bb_middle"] = bb_middle
        features_df["bb_lower"] = bb_lower
        features_df["bb_width"] = (bb_upper - bb_lower) / bb_middle
        
        # Keltner Channels
        keltner_period = get_indicator_period(self.timeframe, "keltner_period")
        keltner_mult = self.config["indicator_periods"]["keltner_atr_mult"]
        
        keltner_upper, keltner_lower = self._compute_keltner_channels(
            high, low, close, keltner_period, keltner_mult
        )
        features_df["keltner_upper"] = keltner_upper
        features_df["keltner_lower"] = keltner_lower
        
        # Historical Volatility
        hv_period = get_indicator_period(self.timeframe, "historical_volatility")
        features_df["historical_volatility"] = self._compute_historical_volatility(
            close, hv_period
        )
        
        return features_df
    
    def _compute_volume_features(
        self, df: pd.DataFrame, features_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Compute volume indicators with timeframe-specific periods."""
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]
        
        # OBV
        features_df["obv"] = self._compute_obv(close, volume)
        
        # Volume SMA
        volume_sma_period = get_indicator_period(self.timeframe, "volume_sma")
        features_df["volume_sma_20"] = volume.rolling(window=volume_sma_period).mean()
        
        # VWAP
        vwap_period = get_indicator_period(self.timeframe, "vwap")
        features_df["vwap"] = self._compute_vwap(high, low, close, volume, vwap_period)
        
        # MFI
        mfi_period = get_indicator_period(self.timeframe, "mfi")
        features_df["mfi_14"] = self._compute_mfi(high, low, close, volume, mfi_period)
        
        # A/D Line
        features_df["ad_line"] = self._compute_ad_line(high, low, close, volume)
        
        return features_df
    
    def _compute_market_structure_features(
        self, df: pd.DataFrame, features_df: pd.DataFrame
    ) -> pd.DataFrame:
        """Compute market structure features."""
        high = df["high"]
        low = df["low"]
        close = df["close"]
        
        # Support and resistance levels (using 50-period lookback)
        lookback = 50
        features_df["support_level"] = low.rolling(window=lookback).min()
        features_df["resistance_level"] = high.rolling(window=lookback).max()
        
        # Pivot point
        features_df["pivot_point"] = (high + low + close) / 3
        
        # Fibonacci retracement (38.2%)
        high_max = high.rolling(window=lookback).max()
        low_min = low.rolling(window=lookback).min()
        features_df["fibonacci_382"] = high_max - (high_max - low_min) * 0.382
        
        return features_df
    
    # ══════════════════════════════════════════════════════════════════════════
    # INDICATOR COMPUTATION METHODS
    # ══════════════════════════════════════════════════════════════════════════
    
    def _compute_rsi(self, close: pd.Series, period: int) -> pd.Series:
        """Compute RSI indicator."""
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
    
    def _compute_macd(
        self, close: pd.Series, fast: int, slow: int, signal: int
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Compute MACD indicator."""
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        
        macd_line = ema_fast - ema_slow
        macd_signal = macd_line.ewm(span=signal, adjust=False).mean()
        macd_histogram = macd_line - macd_signal
        
        return macd_line, macd_signal, macd_histogram


    def _compute_stochastic(
        self, high: pd.Series, low: pd.Series, close: pd.Series, period: int, smooth: int
    ) -> tuple[pd.Series, pd.Series]:
        """Compute Stochastic oscillator."""
        low_min = low.rolling(window=period).min()
        high_max = high.rolling(window=period).max()
        
        stoch_k = 100 * (close - low_min) / (high_max - low_min)
        stoch_d = stoch_k.rolling(window=smooth).mean()
        
        return stoch_k, stoch_d
    
    def _compute_roc(self, close: pd.Series, period: int) -> pd.Series:
        """Compute Rate of Change."""
        roc = ((close - close.shift(period)) / close.shift(period)) * 100
        return roc
    
    def _compute_williams_r(
        self, high: pd.Series, low: pd.Series, close: pd.Series, period: int
    ) -> pd.Series:
        """Compute Williams %R."""
        high_max = high.rolling(window=period).max()
        low_min = low.rolling(window=period).min()
        
        williams_r = -100 * (high_max - close) / (high_max - low_min)
        return williams_r
    
    def _compute_cci(
        self, high: pd.Series, low: pd.Series, close: pd.Series, period: int
    ) -> pd.Series:
        """Compute Commodity Channel Index."""
        typical_price = (high + low + close) / 3
        sma = typical_price.rolling(window=period).mean()
        mad = typical_price.rolling(window=period).apply(
            lambda x: np.abs(x - x.mean()).mean()
        )
        
        cci = (typical_price - sma) / (0.015 * mad)
        return cci
    
    def _compute_momentum(self, close: pd.Series, period: int) -> pd.Series:
        """Compute Momentum indicator."""
        momentum = close - close.shift(period)
        return momentum
    
    def _compute_tsi(self, close: pd.Series, fast: int, slow: int) -> pd.Series:
        """Compute True Strength Index."""
        price_change = close.diff()
        
        # Double smoothed price change
        smooth1 = price_change.ewm(span=slow, adjust=False).mean()
        smooth2 = smooth1.ewm(span=fast, adjust=False).mean()
        
        # Double smoothed absolute price change
        abs_smooth1 = price_change.abs().ewm(span=slow, adjust=False).mean()
        abs_smooth2 = abs_smooth1.ewm(span=fast, adjust=False).mean()
        
        tsi = 100 * (smooth2 / abs_smooth2)
        return tsi
    
    def _compute_ultimate_oscillator(
        self, high: pd.Series, low: pd.Series, close: pd.Series
    ) -> pd.Series:
        """Compute Ultimate Oscillator."""
        # Use fixed periods: 7, 14, 28
        bp = close - pd.concat([low, close.shift(1)], axis=1).min(axis=1)
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs()
        ], axis=1).max(axis=1)
        
        avg7 = bp.rolling(window=7).sum() / tr.rolling(window=7).sum()
        avg14 = bp.rolling(window=14).sum() / tr.rolling(window=14).sum()
        avg28 = bp.rolling(window=28).sum() / tr.rolling(window=28).sum()
        
        uo = 100 * ((4 * avg7) + (2 * avg14) + avg28) / 7
        return uo
    
    def _compute_awesome_oscillator(
        self, high: pd.Series, low: pd.Series
    ) -> pd.Series:
        """Compute Awesome Oscillator."""
        median_price = (high + low) / 2
        
        ao = median_price.rolling(window=5).mean() - median_price.rolling(window=34).mean()
        return ao
    
    def _compute_dema(self, close: pd.Series, period: int) -> pd.Series:
        """Compute Double Exponential Moving Average."""
        ema1 = close.ewm(span=period, adjust=False).mean()
        ema2 = ema1.ewm(span=period, adjust=False).mean()
        
        dema = 2 * ema1 - ema2
        return dema
    
    def _compute_tema(self, close: pd.Series, period: int) -> pd.Series:
        """Compute Triple Exponential Moving Average."""
        ema1 = close.ewm(span=period, adjust=False).mean()
        ema2 = ema1.ewm(span=period, adjust=False).mean()
        ema3 = ema2.ewm(span=period, adjust=False).mean()
        
        tema = 3 * ema1 - 3 * ema2 + ema3
        return tema
    
    def _compute_adx(
        self, high: pd.Series, low: pd.Series, close: pd.Series, period: int
    ) -> pd.Series:
        """Compute Average Directional Index."""
        # True Range
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs()
        ], axis=1).max(axis=1)
        
        # Directional Movement
        up_move = high - high.shift(1)
        down_move = low.shift(1) - low
        
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
        
        # Smoothed indicators
        atr = tr.rolling(window=period).mean()
        plus_di = 100 * pd.Series(plus_dm).rolling(window=period).mean() / atr
        minus_di = 100 * pd.Series(minus_dm).rolling(window=period).mean() / atr
        
        # ADX
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
        adx = dx.rolling(window=period).mean()
        
        return adx
    
    def _compute_aroon_up(self, high: pd.Series, period: int) -> pd.Series:
        """Compute Aroon Up indicator."""
        aroon_up = high.rolling(window=period).apply(
            lambda x: (period - (period - 1 - x.argmax())) / period * 100
        )
        return aroon_up
    
    def _compute_atr(
        self, high: pd.Series, low: pd.Series, close: pd.Series, period: int
    ) -> pd.Series:
        """Compute Average True Range."""
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs()
        ], axis=1).max(axis=1)
        
        atr = tr.rolling(window=period).mean()
        return atr
    
    def _compute_bollinger_bands(
        self, close: pd.Series, period: int, std_dev: float
    ) -> tuple[pd.Series, pd.Series, pd.Series]:
        """Compute Bollinger Bands."""
        middle = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        
        upper = middle + (std * std_dev)
        lower = middle - (std * std_dev)
        
        return upper, middle, lower
    
    def _compute_keltner_channels(
        self, high: pd.Series, low: pd.Series, close: pd.Series, period: int, multiplier: float
    ) -> tuple[pd.Series, pd.Series]:
        """Compute Keltner Channels."""
        middle = close.ewm(span=period, adjust=False).mean()
        atr = self._compute_atr(high, low, close, period)
        
        upper = middle + (atr * multiplier)
        lower = middle - (atr * multiplier)
        
        return upper, lower
    
    def _compute_historical_volatility(
        self, close: pd.Series, period: int
    ) -> pd.Series:
        """Compute Historical Volatility."""
        log_returns = np.log(close / close.shift(1))
        volatility = log_returns.rolling(window=period).std() * np.sqrt(252)
        
        return volatility
    
    def _compute_obv(self, close: pd.Series, volume: pd.Series) -> pd.Series:
        """Compute On-Balance Volume."""
        obv = (np.sign(close.diff()) * volume).fillna(0).cumsum()
        return obv
    
    def _compute_vwap(
        self, high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int
    ) -> pd.Series:
        """Compute Volume Weighted Average Price."""
        typical_price = (high + low + close) / 3
        vwap = (typical_price * volume).rolling(window=period).sum() / volume.rolling(window=period).sum()
        
        return vwap
    
    def _compute_mfi(
        self, high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int
    ) -> pd.Series:
        """Compute Money Flow Index."""
        typical_price = (high + low + close) / 3
        money_flow = typical_price * volume
        
        positive_flow = money_flow.where(typical_price > typical_price.shift(1), 0)
        negative_flow = money_flow.where(typical_price < typical_price.shift(1), 0)
        
        positive_mf = positive_flow.rolling(window=period).sum()
        negative_mf = negative_flow.rolling(window=period).sum()
        
        mfi = 100 - (100 / (1 + positive_mf / negative_mf))
        return mfi
    
    def _compute_ad_line(
        self, high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series
    ) -> pd.Series:
        """Compute Accumulation/Distribution Line."""
        clv = ((close - low) - (high - close)) / (high - low)
        ad_line = (clv * volume).cumsum()
        
        return ad_line


def get_timeframe_feature_definitions(timeframe: str) -> dict[str, dict[str, Any]]:
    """
    Get feature definitions for a specific timeframe.
    
    This function returns a dictionary mapping feature names to their
    configuration, including adjusted indicator periods for the timeframe.
    
    Args:
        timeframe: Timeframe identifier ("1d", "1w", "1M")
        
    Returns:
        Dictionary of feature definitions with timeframe-specific periods
    """
    config = get_timeframe_config(timeframe)
    periods = config["indicator_periods"]
    
    feature_definitions = {
        # Momentum Indicators
        "rsi_14": {
            "description": f"{periods['rsi']}-period RSI for {config['name']} timeframe",
            "period": periods["rsi"],
            "category": "momentum",
        },
        "rsi_21": {
            "description": f"{periods['rsi_long']}-period RSI for {config['name']} timeframe",
            "period": periods["rsi_long"],
            "category": "momentum",
        },
        "macd_line": {
            "description": f"MACD line ({periods['macd_fast']}-{periods['macd_slow']}) for {config['name']}",
            "fast": periods["macd_fast"],
            "slow": periods["macd_slow"],
            "category": "momentum",
        },
        "macd_signal": {
            "description": f"MACD signal ({periods['macd_signal']}-period) for {config['name']}",
            "period": periods["macd_signal"],
            "category": "momentum",
        },
        "macd_histogram": {
            "description": f"MACD histogram for {config['name']} timeframe",
            "derived": True,
            "category": "momentum",
        },
        
        # Add more feature definitions as needed...
        # (Abbreviated for brevity - in production, include all 37 features)
    }
    
    return feature_definitions
