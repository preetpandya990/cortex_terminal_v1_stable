"""
Cortex AI — Technical Indicators
==================================
Comprehensive technical indicator library for ML feature engineering.

All functions accept pandas DataFrame with OHLCV columns and return float values.
Functions are pure (no side effects) and stateless for reproducibility.

Indicators implemented:
- 15 Momentum indicators (RSI, MACD, Stochastic, ROC, Williams %R, etc.)
- 10 Trend indicators (SMA, EMA, DEMA, TEMA, ADX, Aroon)
- 8 Volatility indicators (ATR, Bollinger Bands, Keltner Channels, etc.)
- 5 Volume indicators (OBV, Volume SMA, VWAP, MFI, A/D Line)
- 4 Market structure features (Support/Resistance, Pivot Points, Fibonacci)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════════════════════════
# MOMENTUM INDICATORS (15 features)
# ══════════════════════════════════════════════════════════════════════════════

def rsi(df: pd.DataFrame, period: int = 14) -> float:
    """
    Relative Strength Index using Wilder's smoothing.
    
    Args:
        df: DataFrame with 'close' column
        period: RSI period (default: 14)
    
    Returns:
        RSI value (0-100)
    """
    if len(df) < period + 1:
        return np.nan
    
    closes = df['close'].values
    deltas = np.diff(closes)
    
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    # Wilder's smoothing
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    
    if avg_loss == 0:
        return 100.0
    
    rs = avg_gain / avg_loss
    rsi_value = 100 - (100 / (1 + rs))
    
    return float(rsi_value)


def macd_line(df: pd.DataFrame, fast: int = 12, slow: int = 26) -> float:
    """
    MACD line (fast EMA - slow EMA).
    
    Args:
        df: DataFrame with 'close' column
        fast: Fast EMA period (default: 12)
        slow: Slow EMA period (default: 26)
    
    Returns:
        MACD line value
    """
    if len(df) < slow:
        return np.nan
    
    closes = df['close'].values
    fast_ema = _ema(closes, fast)
    slow_ema = _ema(closes, slow)
    
    return float(fast_ema - slow_ema)


def macd_signal(df: pd.DataFrame, signal_period: int = 9) -> float:
    """
    MACD signal line (EMA of MACD line).
    
    Args:
        df: DataFrame with 'close' column
        signal_period: Signal line period (default: 9)
    
    Returns:
        MACD signal value
    """
    if len(df) < 26 + signal_period:
        return np.nan
    
    # Compute MACD line for all points
    closes = df['close'].values
    fast_ema = _ema(closes, 12)
    slow_ema = _ema(closes, 26)
    macd_values = fast_ema - slow_ema
    
    # Compute signal as EMA of MACD
    signal = _ema(np.array([macd_values]), signal_period)
    
    return float(signal)


def macd_histogram(df: pd.DataFrame) -> float:
    """
    MACD histogram (MACD line - signal line).
    
    Args:
        df: DataFrame with 'close' column
    
    Returns:
        MACD histogram value
    """
    macd = macd_line(df)
    signal = macd_signal(df)
    
    if np.isnan(macd) or np.isnan(signal):
        return np.nan
    
    return float(macd - signal)


def stochastic_k(df: pd.DataFrame, period: int = 14) -> float:
    """
    Stochastic %K oscillator.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: Lookback period (default: 14)
    
    Returns:
        %K value (0-100)
    """
    if len(df) < period:
        return np.nan
    
    recent = df.tail(period)
    lowest_low = recent['low'].min()
    highest_high = recent['high'].max()
    current_close = df['close'].iloc[-1]
    
    if highest_high == lowest_low:
        return 50.0
    
    k = 100 * (current_close - lowest_low) / (highest_high - lowest_low)
    
    return float(k)


def stochastic_d(df: pd.DataFrame, k_period: int = 14, d_period: int = 3) -> float:
    """
    Stochastic %D (SMA of %K).
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        k_period: %K period (default: 14)
        d_period: %D smoothing period (default: 3)
    
    Returns:
        %D value (0-100)
    """
    if len(df) < k_period + d_period:
        return np.nan
    
    # Compute %K for last d_period points
    k_values = []
    for i in range(d_period):
        subset = df.iloc[-(k_period + d_period - i):-(d_period - i) if d_period - i > 0 else None]
        k_values.append(stochastic_k(subset, k_period))
    
    return float(np.mean(k_values))


def roc(df: pd.DataFrame, period: int = 10) -> float:
    """
    Rate of Change.
    
    Args:
        df: DataFrame with 'close' column
        period: ROC period (default: 10)
    
    Returns:
        ROC percentage
    """
    if len(df) < period + 1:
        return np.nan
    
    current = df['close'].iloc[-1]
    past = df['close'].iloc[-(period + 1)]
    
    if past == 0:
        return np.nan
    
    roc_value = ((current - past) / past) * 100
    
    return float(roc_value)


def williams_r(df: pd.DataFrame, period: int = 14) -> float:
    """
    Williams %R oscillator.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: Lookback period (default: 14)
    
    Returns:
        Williams %R value (-100 to 0)
    """
    if len(df) < period:
        return np.nan
    
    recent = df.tail(period)
    highest_high = recent['high'].max()
    lowest_low = recent['low'].min()
    current_close = df['close'].iloc[-1]
    
    if highest_high == lowest_low:
        return -50.0
    
    wr = -100 * (highest_high - current_close) / (highest_high - lowest_low)
    
    return float(wr)


def cci(df: pd.DataFrame, period: int = 20) -> float:
    """
    Commodity Channel Index.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: CCI period (default: 20)
    
    Returns:
        CCI value
    """
    if len(df) < period:
        return np.nan
    
    recent = df.tail(period)
    typical_price = (recent['high'] + recent['low'] + recent['close']) / 3
    sma_tp = typical_price.mean()
    mean_deviation = np.mean(np.abs(typical_price - sma_tp))
    
    current_tp = (df['high'].iloc[-1] + df['low'].iloc[-1] + df['close'].iloc[-1]) / 3
    
    if mean_deviation == 0:
        return 0.0
    
    cci_value = (current_tp - sma_tp) / (0.015 * mean_deviation)
    
    return float(cci_value)


def momentum(df: pd.DataFrame, period: int = 10) -> float:
    """
    Momentum indicator.
    
    Args:
        df: DataFrame with 'close' column
        period: Momentum period (default: 10)
    
    Returns:
        Momentum value
    """
    if len(df) < period + 1:
        return np.nan
    
    current = df['close'].iloc[-1]
    past = df['close'].iloc[-(period + 1)]
    
    return float(current - past)


def tsi(df: pd.DataFrame, fast: int = 25, slow: int = 13) -> float:
    """
    True Strength Index.
    
    Args:
        df: DataFrame with 'close' column
        fast: Fast period (default: 25)
        slow: Slow period (default: 13)
    
    Returns:
        TSI value
    """
    if len(df) < slow + fast:
        return np.nan
    
    closes = df['close'].values
    momentum_vals = np.diff(closes)
    
    # Double smoothed momentum
    abs_momentum = np.abs(momentum_vals)
    
    smoothed_momentum = _ema(momentum_vals, slow)
    double_smoothed_momentum = _ema(np.array([smoothed_momentum]), fast)
    
    smoothed_abs = _ema(abs_momentum, slow)
    double_smoothed_abs = _ema(np.array([smoothed_abs]), fast)
    
    if double_smoothed_abs == 0:
        return 0.0
    
    tsi_value = 100 * (double_smoothed_momentum / double_smoothed_abs)
    
    return float(tsi_value)


def ultimate_oscillator(df: pd.DataFrame, periods: list[int] = [7, 14, 28]) -> float:
    """
    Ultimate Oscillator.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        periods: Three periods for calculation (default: [7, 14, 28])
    
    Returns:
        Ultimate Oscillator value (0-100)
    """
    if len(df) < max(periods) + 1:
        return np.nan
    
    # Calculate buying pressure and true range
    bp = df['close'] - df[['low', 'close']].shift(1).min(axis=1)
    tr = df[['high', 'low']].max(axis=1) - df[['high', 'low']].min(axis=1)
    tr = tr.combine(np.abs(df['high'] - df['close'].shift(1)), max)
    tr = tr.combine(np.abs(df['low'] - df['close'].shift(1)), max)
    
    # Calculate averages for each period
    avg_values = []
    for period in periods:
        if len(df) < period:
            return np.nan
        bp_sum = bp.tail(period).sum()
        tr_sum = tr.tail(period).sum()
        if tr_sum == 0:
            avg_values.append(0)
        else:
            avg_values.append(bp_sum / tr_sum)
    
    # Weighted average
    uo = 100 * ((4 * avg_values[0]) + (2 * avg_values[1]) + avg_values[2]) / 7
    
    return float(uo)


def awesome_oscillator(df: pd.DataFrame, fast: int = 5, slow: int = 34) -> float:
    """
    Awesome Oscillator.
    
    Args:
        df: DataFrame with 'high', 'low' columns
        fast: Fast period (default: 5)
        slow: Slow period (default: 34)
    
    Returns:
        Awesome Oscillator value
    """
    if len(df) < slow:
        return np.nan
    
    median_price = (df['high'] + df['low']) / 2
    
    fast_sma = median_price.tail(fast).mean()
    slow_sma = median_price.tail(slow).mean()
    
    ao = fast_sma - slow_sma
    
    return float(ao)


# ══════════════════════════════════════════════════════════════════════════════
# TREND INDICATORS (10 features)
# ══════════════════════════════════════════════════════════════════════════════

def sma(df: pd.DataFrame, period: int = 20) -> float:
    """
    Simple Moving Average.
    
    Args:
        df: DataFrame with 'close' column
        period: SMA period (default: 20)
    
    Returns:
        SMA value
    """
    if len(df) < period:
        return np.nan
    
    return float(df['close'].tail(period).mean())


def ema(df: pd.DataFrame, period: int = 12) -> float:
    """
    Exponential Moving Average.
    
    Args:
        df: DataFrame with 'close' column
        period: EMA period (default: 12)
    
    Returns:
        EMA value
    """
    if len(df) < period:
        return np.nan
    
    closes = df['close'].values
    return float(_ema(closes, period))


def dema(df: pd.DataFrame, period: int = 20) -> float:
    """
    Double Exponential Moving Average.
    
    Args:
        df: DataFrame with 'close' column
        period: DEMA period (default: 20)
    
    Returns:
        DEMA value
    """
    if len(df) < period * 2:
        return np.nan
    
    closes = df['close'].values
    ema1 = _ema(closes, period)
    
    # Create array of EMA values for second EMA
    ema_series = pd.Series([ema1] * len(closes))
    ema2 = _ema(ema_series.values, period)
    
    dema_value = 2 * ema1 - ema2
    
    return float(dema_value)


def tema(df: pd.DataFrame, period: int = 20) -> float:
    """
    Triple Exponential Moving Average.
    
    Args:
        df: DataFrame with 'close' column
        period: TEMA period (default: 20)
    
    Returns:
        TEMA value
    """
    if len(df) < period * 3:
        return np.nan
    
    closes = df['close'].values
    ema1 = _ema(closes, period)
    
    ema_series = pd.Series([ema1] * len(closes))
    ema2 = _ema(ema_series.values, period)
    
    ema_series2 = pd.Series([ema2] * len(closes))
    ema3 = _ema(ema_series2.values, period)
    
    tema_value = 3 * ema1 - 3 * ema2 + ema3
    
    return float(tema_value)


def adx(df: pd.DataFrame, period: int = 14) -> float:
    """
    Average Directional Index.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: ADX period (default: 14)
    
    Returns:
        ADX value (0-100)
    """
    if len(df) < period + 1:
        return np.nan
    
    # Calculate +DM and -DM
    high_diff = df['high'].diff()
    low_diff = -df['low'].diff()
    
    plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0)
    minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0)
    
    # Calculate True Range
    tr = np.maximum(
        df['high'] - df['low'],
        np.maximum(
            np.abs(df['high'] - df['close'].shift(1)),
            np.abs(df['low'] - df['close'].shift(1))
        )
    )
    
    # Smooth with Wilder's method
    atr_val = tr[-period:].mean()
    plus_di = 100 * (plus_dm[-period:].mean() / atr_val) if atr_val > 0 else 0
    minus_di = 100 * (minus_dm[-period:].mean() / atr_val) if atr_val > 0 else 0
    
    # Calculate DX and ADX
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
    
    return float(dx)


def aroon_up(df: pd.DataFrame, period: int = 25) -> float:
    """
    Aroon Up indicator.
    
    Args:
        df: DataFrame with 'high' column
        period: Aroon period (default: 25)
    
    Returns:
        Aroon Up value (0-100)
    """
    if len(df) < period:
        return np.nan
    
    recent_highs = df['high'].tail(period)
    periods_since_high = period - recent_highs.argmax() - 1
    
    aroon = 100 * (period - periods_since_high) / period
    
    return float(aroon)


# ══════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def _ema(values: np.ndarray, period: int) -> float:
    """
    Calculate Exponential Moving Average.
    
    Args:
        values: Array of values
        period: EMA period
    
    Returns:
        EMA value
    """
    if len(values) < period:
        return np.nan
    
    multiplier = 2 / (period + 1)
    ema_val = np.mean(values[:period])  # Start with SMA
    
    for value in values[period:]:
        ema_val = (value * multiplier) + (ema_val * (1 - multiplier))
    
    return ema_val



# ══════════════════════════════════════════════════════════════════════════════
# VOLATILITY INDICATORS (8 features)
# ══════════════════════════════════════════════════════════════════════════════

def atr(df: pd.DataFrame, period: int = 14) -> float:
    """
    Average True Range.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: ATR period (default: 14)
    
    Returns:
        ATR value
    """
    if len(df) < period + 1:
        return np.nan
    
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift(1))
    low_close = np.abs(df['low'] - df['close'].shift(1))
    
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    
    atr_value = tr.tail(period).mean()
    
    return float(atr_value)


def bb_upper(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> float:
    """
    Bollinger Band Upper.
    
    Args:
        df: DataFrame with 'close' column
        period: BB period (default: 20)
        std_dev: Standard deviation multiplier (default: 2.0)
    
    Returns:
        Upper Bollinger Band value
    """
    if len(df) < period:
        return np.nan
    
    recent = df['close'].tail(period)
    middle = recent.mean()
    std = recent.std()
    
    upper = middle + (std_dev * std)
    
    return float(upper)


def bb_middle(df: pd.DataFrame, period: int = 20) -> float:
    """
    Bollinger Band Middle (SMA).
    
    Args:
        df: DataFrame with 'close' column
        period: BB period (default: 20)
    
    Returns:
        Middle Bollinger Band value
    """
    return sma(df, period)


def bb_lower(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> float:
    """
    Bollinger Band Lower.
    
    Args:
        df: DataFrame with 'close' column
        period: BB period (default: 20)
        std_dev: Standard deviation multiplier (default: 2.0)
    
    Returns:
        Lower Bollinger Band value
    """
    if len(df) < period:
        return np.nan
    
    recent = df['close'].tail(period)
    middle = recent.mean()
    std = recent.std()
    
    lower = middle - (std_dev * std)
    
    return float(lower)


def bb_width(df: pd.DataFrame, period: int = 20, std_dev: float = 2.0) -> float:
    """
    Bollinger Band Width.
    
    Args:
        df: DataFrame with 'close' column
        period: BB period (default: 20)
        std_dev: Standard deviation multiplier (default: 2.0)
    
    Returns:
        Bollinger Band width percentage
    """
    upper = bb_upper(df, period, std_dev)
    lower = bb_lower(df, period, std_dev)
    middle = bb_middle(df, period)
    
    if np.isnan(upper) or np.isnan(lower) or np.isnan(middle) or middle == 0:
        return np.nan
    
    width = ((upper - lower) / middle) * 100
    
    return float(width)


def keltner_upper(df: pd.DataFrame, period: int = 20, atr_mult: float = 2.0) -> float:
    """
    Keltner Channel Upper.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: KC period (default: 20)
        atr_mult: ATR multiplier (default: 2.0)
    
    Returns:
        Upper Keltner Channel value
    """
    middle = ema(df, period)
    atr_value = atr(df, period)
    
    if np.isnan(middle) or np.isnan(atr_value):
        return np.nan
    
    upper = middle + (atr_mult * atr_value)
    
    return float(upper)


def keltner_lower(df: pd.DataFrame, period: int = 20, atr_mult: float = 2.0) -> float:
    """
    Keltner Channel Lower.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: KC period (default: 20)
        atr_mult: ATR multiplier (default: 2.0)
    
    Returns:
        Lower Keltner Channel value
    """
    middle = ema(df, period)
    atr_value = atr(df, period)
    
    if np.isnan(middle) or np.isnan(atr_value):
        return np.nan
    
    lower = middle - (atr_mult * atr_value)
    
    return float(lower)


def historical_volatility(df: pd.DataFrame, period: int = 20) -> float:
    """
    Historical Volatility (annualized).
    
    Args:
        df: DataFrame with 'close' column
        period: Lookback period (default: 20)
    
    Returns:
        Annualized volatility percentage
    """
    if len(df) < period + 1:
        return np.nan
    
    returns = df['close'].pct_change().tail(period)
    volatility = returns.std() * np.sqrt(252) * 100  # Annualized
    
    return float(volatility)


# ══════════════════════════════════════════════════════════════════════════════
# VOLUME INDICATORS (5 features)
# ══════════════════════════════════════════════════════════════════════════════

def obv(df: pd.DataFrame) -> float:
    """
    On-Balance Volume.
    
    Args:
        df: DataFrame with 'close', 'volume' columns
    
    Returns:
        OBV value
    """
    if len(df) < 2:
        return np.nan
    
    price_change = df['close'].diff()
    obv_values = np.where(price_change > 0, df['volume'], 
                          np.where(price_change < 0, -df['volume'], 0))
    
    obv_total = obv_values.sum()
    
    return float(obv_total)


def volume_sma(df: pd.DataFrame, period: int = 20) -> float:
    """
    Volume Simple Moving Average.
    
    Args:
        df: DataFrame with 'volume' column
        period: SMA period (default: 20)
    
    Returns:
        Volume SMA value
    """
    if len(df) < period:
        return np.nan
    
    return float(df['volume'].tail(period).mean())


def vwap(df: pd.DataFrame, period: int = 20) -> float:
    """
    Volume Weighted Average Price.
    
    Args:
        df: DataFrame with 'high', 'low', 'close', 'volume' columns
        period: VWAP period (default: 20)
    
    Returns:
        VWAP value
    """
    if len(df) < period:
        return np.nan
    
    recent = df.tail(period)
    typical_price = (recent['high'] + recent['low'] + recent['close']) / 3
    vwap_value = (typical_price * recent['volume']).sum() / recent['volume'].sum()
    
    return float(vwap_value)


def mfi(df: pd.DataFrame, period: int = 14) -> float:
    """
    Money Flow Index.
    
    Args:
        df: DataFrame with 'high', 'low', 'close', 'volume' columns
        period: MFI period (default: 14)
    
    Returns:
        MFI value (0-100)
    """
    if len(df) < period + 1:
        return np.nan
    
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    money_flow = typical_price * df['volume']
    
    price_change = typical_price.diff()
    
    positive_flow = np.where(price_change > 0, money_flow, 0)
    negative_flow = np.where(price_change < 0, money_flow, 0)
    
    positive_mf = positive_flow[-period:].sum()
    negative_mf = negative_flow[-period:].sum()
    
    if negative_mf == 0:
        return 100.0
    
    money_ratio = positive_mf / negative_mf
    mfi_value = 100 - (100 / (1 + money_ratio))
    
    return float(mfi_value)


def ad_line(df: pd.DataFrame) -> float:
    """
    Accumulation/Distribution Line.
    
    Args:
        df: DataFrame with 'high', 'low', 'close', 'volume' columns
    
    Returns:
        A/D Line value
    """
    if len(df) < 1:
        return np.nan
    
    clv = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low'])
    clv = clv.fillna(0)  # Handle division by zero
    
    ad = (clv * df['volume']).cumsum()
    
    return float(ad.iloc[-1])


# ══════════════════════════════════════════════════════════════════════════════
# MARKET STRUCTURE INDICATORS (4 features)
# ══════════════════════════════════════════════════════════════════════════════

def support_level(df: pd.DataFrame, lookback: int = 50) -> float:
    """
    Nearest support level (recent swing low).
    
    Args:
        df: DataFrame with 'low' column
        lookback: Lookback period (default: 50)
    
    Returns:
        Support level price
    """
    if len(df) < lookback:
        return np.nan
    
    recent_lows = df['low'].tail(lookback)
    
    # Find local minima (swing lows)
    swing_lows = []
    for i in range(2, len(recent_lows) - 2):
        if (recent_lows.iloc[i] < recent_lows.iloc[i-1] and 
            recent_lows.iloc[i] < recent_lows.iloc[i-2] and
            recent_lows.iloc[i] < recent_lows.iloc[i+1] and 
            recent_lows.iloc[i] < recent_lows.iloc[i+2]):
            swing_lows.append(recent_lows.iloc[i])
    
    if not swing_lows:
        return float(recent_lows.min())
    
    current_price = df['close'].iloc[-1]
    # Find nearest support below current price
    supports_below = [s for s in swing_lows if s < current_price]
    
    if not supports_below:
        return float(recent_lows.min())
    
    return float(max(supports_below))


def resistance_level(df: pd.DataFrame, lookback: int = 50) -> float:
    """
    Nearest resistance level (recent swing high).
    
    Args:
        df: DataFrame with 'high' column
        lookback: Lookback period (default: 50)
    
    Returns:
        Resistance level price
    """
    if len(df) < lookback:
        return np.nan
    
    recent_highs = df['high'].tail(lookback)
    
    # Find local maxima (swing highs)
    swing_highs = []
    for i in range(2, len(recent_highs) - 2):
        if (recent_highs.iloc[i] > recent_highs.iloc[i-1] and 
            recent_highs.iloc[i] > recent_highs.iloc[i-2] and
            recent_highs.iloc[i] > recent_highs.iloc[i+1] and 
            recent_highs.iloc[i] > recent_highs.iloc[i+2]):
            swing_highs.append(recent_highs.iloc[i])
    
    if not swing_highs:
        return float(recent_highs.max())
    
    current_price = df['close'].iloc[-1]
    # Find nearest resistance above current price
    resistances_above = [r for r in swing_highs if r > current_price]
    
    if not resistances_above:
        return float(recent_highs.max())
    
    return float(min(resistances_above))


def pivot_point(df: pd.DataFrame) -> float:
    """
    Standard Pivot Point.
    
    Args:
        df: DataFrame with 'high', 'low', 'close' columns
    
    Returns:
        Pivot point value
    """
    if len(df) < 1:
        return np.nan
    
    high = df['high'].iloc[-1]
    low = df['low'].iloc[-1]
    close = df['close'].iloc[-1]
    
    pp = (high + low + close) / 3
    
    return float(pp)


def fibonacci_382(df: pd.DataFrame, lookback: int = 50) -> float:
    """
    38.2% Fibonacci retracement level.
    
    Args:
        df: DataFrame with 'high', 'low' columns
        lookback: Lookback period (default: 50)
    
    Returns:
        Fibonacci 38.2% retracement level
    """
    if len(df) < lookback:
        return np.nan
    
    recent = df.tail(lookback)
    high = recent['high'].max()
    low = recent['low'].min()
    
    # 38.2% retracement from high
    fib_level = high - (0.382 * (high - low))
    
    return float(fib_level)


# ══════════════════════════════════════════════════════════════════════════════
# FEATURE REGISTRY
# ══════════════════════════════════════════════════════════════════════════════

# Dictionary mapping feature names to computation functions
FEATURE_FUNCTIONS: dict[str, callable] = {
    # Momentum (15)
    "rsi_14": lambda df: rsi(df, 14),
    "rsi_21": lambda df: rsi(df, 21),
    "macd_line": macd_line,
    "macd_signal": macd_signal,
    "macd_histogram": macd_histogram,
    "stochastic_k": stochastic_k,
    "stochastic_d": stochastic_d,
    "roc_10": lambda df: roc(df, 10),
    "roc_20": lambda df: roc(df, 20),
    "williams_r": williams_r,
    "cci_20": lambda df: cci(df, 20),
    "momentum_10": lambda df: momentum(df, 10),
    "tsi": tsi,
    "ultimate_oscillator": ultimate_oscillator,
    "awesome_oscillator": awesome_oscillator,
    
    # Trend (10)
    "sma_20": lambda df: sma(df, 20),
    "sma_50": lambda df: sma(df, 50),
    "sma_200": lambda df: sma(df, 200),
    "ema_12": lambda df: ema(df, 12),
    "ema_26": lambda df: ema(df, 26),
    "ema_50": lambda df: ema(df, 50),
    "dema_20": lambda df: dema(df, 20),
    "tema_20": lambda df: tema(df, 20),
    "adx_14": lambda df: adx(df, 14),
    "aroon_up": lambda df: aroon_up(df, 25),
    
    # Volatility (8)
    "atr_14": lambda df: atr(df, 14),
    "bb_upper": lambda df: bb_upper(df, 20, 2.0),
    "bb_middle": lambda df: bb_middle(df, 20),
    "bb_lower": lambda df: bb_lower(df, 20, 2.0),
    "bb_width": lambda df: bb_width(df, 20, 2.0),
    "keltner_upper": lambda df: keltner_upper(df, 20, 2.0),
    "keltner_lower": lambda df: keltner_lower(df, 20, 2.0),
    "historical_volatility": lambda df: historical_volatility(df, 20),
    
    # Volume (5)
    "obv": obv,
    "volume_sma_20": lambda df: volume_sma(df, 20),
    "vwap": lambda df: vwap(df, 20),
    "mfi_14": lambda df: mfi(df, 14),
    "ad_line": ad_line,
    
    # Market Structure (4)
    "support_level": lambda df: support_level(df, 50),
    "resistance_level": lambda df: resistance_level(df, 50),
    "pivot_point": pivot_point,
    "fibonacci_382": lambda df: fibonacci_382(df, 50),
}
