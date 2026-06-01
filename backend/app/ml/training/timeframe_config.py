"""
Cortex AI — Timeframe-Specific Configuration
==============================================
Configuration for timeframe-specific models including adjusted indicator periods,
sequence lengths, and feature definitions.

This module implements Requirements 4.2, 4.3, 4.5, and 12.2:
- Separate models for daily, weekly, monthly timeframes
- Timeframe-specific feature computation with adjusted indicator periods
- Training-serving consistency per timeframe
"""
from __future__ import annotations

from typing import Any

# ══════════════════════════════════════════════════════════════════════════════
# TIMEFRAME-SPECIFIC CONFIGURATIONS
# ══════════════════════════════════════════════════════════════════════════════

TIMEFRAME_CONFIGS: dict[str, dict[str, Any]] = {
    "1d": {
        "name": "daily",
        "description": "Daily timeframe model",
        "sequence_length": 60,  # 60 days (~3 months)
        "min_training_samples": 200,  # Minimum 200 days
        "validation_split": 0.15,
        "test_split": 0.15,
        "batch_size": 32,
        "epochs": 100,
        "early_stopping_patience": 10,
        "learning_rate": 0.001,
        
        # Indicator period adjustments (base periods)
        "indicator_periods": {
            "rsi": 14,
            "rsi_long": 21,
            "macd_fast": 12,
            "macd_slow": 26,
            "macd_signal": 9,
            "stochastic": 14,
            "stochastic_smooth": 3,
            "roc_short": 10,
            "roc_long": 20,
            "williams_r": 14,
            "cci": 20,
            "momentum": 10,
            "tsi_fast": 25,
            "tsi_slow": 13,
            "sma_short": 20,
            "sma_medium": 50,
            "sma_long": 200,
            "ema_fast": 12,
            "ema_medium": 26,
            "ema_slow": 50,
            "dema": 20,
            "tema": 20,
            "adx": 14,
            "aroon": 25,
            "atr": 14,
            "bb_period": 20,
            "bb_std": 2,
            "keltner_period": 20,
            "keltner_atr_mult": 2,
            "historical_volatility": 20,
            "volume_sma": 20,
            "vwap": 20,
            "mfi": 14,
        },
    },
    
    "1w": {
        "name": "weekly",
        "description": "Weekly timeframe model",
        "sequence_length": 52,  # 52 weeks (1 year)
        "min_training_samples": 104,  # Minimum 2 years
        "validation_split": 0.15,
        "test_split": 0.15,
        "batch_size": 32,
        "epochs": 100,
        "early_stopping_patience": 10,
        "learning_rate": 0.001,
        
        # Indicator periods scaled by 5 (5 trading days per week)
        "indicator_periods": {
            "rsi": 70,  # 14 * 5
            "rsi_long": 105,  # 21 * 5
            "macd_fast": 60,  # 12 * 5
            "macd_slow": 130,  # 26 * 5
            "macd_signal": 45,  # 9 * 5
            "stochastic": 70,  # 14 * 5
            "stochastic_smooth": 15,  # 3 * 5
            "roc_short": 50,  # 10 * 5
            "roc_long": 100,  # 20 * 5
            "williams_r": 70,  # 14 * 5
            "cci": 100,  # 20 * 5
            "momentum": 50,  # 10 * 5
            "tsi_fast": 125,  # 25 * 5
            "tsi_slow": 65,  # 13 * 5
            "sma_short": 100,  # 20 * 5
            "sma_medium": 250,  # 50 * 5
            "sma_long": 1000,  # 200 * 5
            "ema_fast": 60,  # 12 * 5
            "ema_medium": 130,  # 26 * 5
            "ema_slow": 250,  # 50 * 5
            "dema": 100,  # 20 * 5
            "tema": 100,  # 20 * 5
            "adx": 70,  # 14 * 5
            "aroon": 125,  # 25 * 5
            "atr": 70,  # 14 * 5
            "bb_period": 100,  # 20 * 5
            "bb_std": 2,
            "keltner_period": 100,  # 20 * 5
            "keltner_atr_mult": 2,
            "historical_volatility": 100,  # 20 * 5
            "volume_sma": 100,  # 20 * 5
            "vwap": 100,  # 20 * 5
            "mfi": 70,  # 14 * 5
        },
    },
    
    "1M": {
        "name": "monthly",
        "description": "Monthly timeframe model",
        "sequence_length": 24,  # 24 months (2 years)
        "min_training_samples": 48,  # Minimum 4 years
        "validation_split": 0.15,
        "test_split": 0.15,
        "batch_size": 16,  # Smaller batch size due to less data
        "epochs": 150,  # More epochs for monthly data
        "early_stopping_patience": 15,
        "learning_rate": 0.0005,  # Lower learning rate
        
        # Indicator periods scaled by 21 (21 trading days per month)
        "indicator_periods": {
            "rsi": 294,  # 14 * 21
            "rsi_long": 441,  # 21 * 21
            "macd_fast": 252,  # 12 * 21
            "macd_slow": 546,  # 26 * 21
            "macd_signal": 189,  # 9 * 21
            "stochastic": 294,  # 14 * 21
            "stochastic_smooth": 63,  # 3 * 21
            "roc_short": 210,  # 10 * 21
            "roc_long": 420,  # 20 * 21
            "williams_r": 294,  # 14 * 21
            "cci": 420,  # 20 * 21
            "momentum": 210,  # 10 * 21
            "tsi_fast": 525,  # 25 * 21
            "tsi_slow": 273,  # 13 * 21
            "sma_short": 420,  # 20 * 21
            "sma_medium": 1050,  # 50 * 21
            "sma_long": 4200,  # 200 * 21
            "ema_fast": 252,  # 12 * 21
            "ema_medium": 546,  # 26 * 21
            "ema_slow": 1050,  # 50 * 21
            "dema": 420,  # 20 * 21
            "tema": 420,  # 20 * 21
            "adx": 294,  # 14 * 21
            "aroon": 525,  # 25 * 21
            "atr": 294,  # 14 * 21
            "bb_period": 420,  # 20 * 21
            "bb_std": 2,
            "keltner_period": 420,  # 20 * 21
            "keltner_atr_mult": 2,
            "historical_volatility": 420,  # 20 * 21
            "volume_sma": 420,  # 20 * 21
            "vwap": 420,  # 20 * 21
            "mfi": 294,  # 14 * 21
        },
    },
}


def get_timeframe_config(timeframe: str) -> dict[str, Any]:
    """
    Get configuration for a specific timeframe.
    
    Args:
        timeframe: Timeframe identifier ("1d", "1w", "1M")
        
    Returns:
        Configuration dictionary for the timeframe
        
    Raises:
        ValueError: If timeframe is not supported
    """
    if timeframe not in TIMEFRAME_CONFIGS:
        raise ValueError(
            f"Unsupported timeframe: {timeframe}. "
            f"Supported: {list(TIMEFRAME_CONFIGS.keys())}"
        )
    
    return TIMEFRAME_CONFIGS[timeframe]


def get_indicator_period(timeframe: str, indicator: str) -> int:
    """
    Get the adjusted period for a specific indicator and timeframe.
    
    Args:
        timeframe: Timeframe identifier ("1d", "1w", "1M")
        indicator: Indicator name (e.g., "rsi", "macd_fast")
        
    Returns:
        Adjusted period for the indicator
        
    Raises:
        ValueError: If timeframe or indicator is not supported
    """
    config = get_timeframe_config(timeframe)
    
    if indicator not in config["indicator_periods"]:
        raise ValueError(
            f"Unsupported indicator: {indicator}. "
            f"Available: {list(config['indicator_periods'].keys())}"
        )
    
    return config["indicator_periods"][indicator]


def get_sequence_length(timeframe: str) -> int:
    """
    Get the sequence length for a specific timeframe.
    
    Args:
        timeframe: Timeframe identifier ("1d", "1w", "1M")
        
    Returns:
        Sequence length (number of time steps)
    """
    config = get_timeframe_config(timeframe)
    return config["sequence_length"]


def get_training_config(timeframe: str) -> dict[str, Any]:
    """
    Get training hyperparameters for a specific timeframe.
    
    Args:
        timeframe: Timeframe identifier ("1d", "1w", "1M")
        
    Returns:
        Dictionary with training configuration
    """
    config = get_timeframe_config(timeframe)
    
    return {
        "batch_size": config["batch_size"],
        "epochs": config["epochs"],
        "learning_rate": config["learning_rate"],
        "early_stopping_patience": config["early_stopping_patience"],
        "validation_split": config["validation_split"],
        "test_split": config["test_split"],
    }


def get_all_timeframes() -> list[str]:
    """
    Get list of all supported timeframes.
    
    Returns:
        List of timeframe identifiers
    """
    return list(TIMEFRAME_CONFIGS.keys())
