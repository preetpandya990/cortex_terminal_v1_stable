"""
Cortex AI — ML System Configuration
=====================================
ML-specific configuration including feature definitions, model architecture,
and training hyperparameters.

This module defines the technical specifications for the ML system that are
not environment-dependent (unlike app/core/config.py which uses env vars).
"""
from __future__ import annotations

from typing import Any

# ── Feature Definitions ────────────────────────────────────────────────────────
# Dictionary mapping feature names to their configuration
# Features are registered dynamically in the Feature Store
FEATURE_DEFINITIONS: dict[str, dict[str, Any]] = {
    # Momentum Indicators (15 features)
    "rsi_14": {"description": "14-period Relative Strength Index", "period": 14},
    "rsi_21": {"description": "21-period RSI", "period": 21},
    "macd_line": {"description": "MACD line (12-26)", "fast": 12, "slow": 26},
    "macd_signal": {"description": "MACD signal line (9-period EMA)", "period": 9},
    "macd_histogram": {"description": "MACD histogram", "derived": True},
    "stochastic_k": {"description": "Stochastic %K (14,3,3)", "period": 14},
    "stochastic_d": {"description": "Stochastic %D (3-period SMA of %K)", "period": 3},
    "roc_10": {"description": "10-period Rate of Change", "period": 10},
    "roc_20": {"description": "20-period Rate of Change", "period": 20},
    "williams_r": {"description": "Williams %R (14-period)", "period": 14},
    "cci_20": {"description": "Commodity Channel Index (20-period)", "period": 20},
    "momentum_10": {"description": "10-period Momentum", "period": 10},
    "tsi": {"description": "True Strength Index", "fast": 25, "slow": 13},
    "ultimate_oscillator": {"description": "Ultimate Oscillator (7,14,28)", "periods": [7, 14, 28]},
    "awesome_oscillator": {"description": "Awesome Oscillator (5,34)", "fast": 5, "slow": 34},
    
    # Trend Indicators (12 features)
    "sma_20": {"description": "20-period Simple Moving Average", "period": 20},
    "sma_50": {"description": "50-period SMA", "period": 50},
    "sma_200": {"description": "200-period SMA", "period": 200},
    "ema_12": {"description": "12-period Exponential Moving Average", "period": 12},
    "ema_26": {"description": "26-period EMA", "period": 26},
    "ema_20": {"description": "20-period EMA (aligns with MarketContext ema_20 signal)", "period": 20},
    "ema_50": {"description": "50-period EMA", "period": 50},
    "ema_crossover_signal": {"description": "Binary flag: 1.0 when EMA-20 > EMA-50 (bullish crossover), 0.0 otherwise", "derived": True},
    "dema_20": {"description": "20-period Double EMA", "period": 20},
    "tema_20": {"description": "20-period Triple EMA", "period": 20},
    "adx_14": {"description": "14-period Average Directional Index", "period": 14},
    "aroon_up": {"description": "Aroon Up (25-period)", "period": 25},
    
    # Volatility Indicators (8 features)
    "atr_14": {"description": "14-period Average True Range", "period": 14},
    "bb_upper": {"description": "Bollinger Band Upper (20,2)", "period": 20, "std": 2},
    "bb_middle": {"description": "Bollinger Band Middle (20-SMA)", "period": 20},
    "bb_lower": {"description": "Bollinger Band Lower (20,2)", "period": 20, "std": 2},
    "bb_width": {"description": "Bollinger Band Width", "derived": True},
    "keltner_upper": {"description": "Keltner Channel Upper", "period": 20, "atr_mult": 2},
    "keltner_lower": {"description": "Keltner Channel Lower", "period": 20, "atr_mult": 2},
    "historical_volatility": {"description": "20-period Historical Volatility", "period": 20},
    
    # Volume Indicators (5 features)
    "obv": {"description": "On-Balance Volume", "cumulative": True},
    "volume_sma_20": {"description": "20-period Volume SMA", "period": 20},
    "vwap": {"description": "Volume Weighted Average Price (20-period)", "period": 20},
    "mfi_14": {"description": "14-period Money Flow Index", "period": 14},
    "ad_line": {"description": "Accumulation/Distribution Line", "cumulative": True},
    
    # Market Structure (4 features)
    "support_level": {"description": "Nearest support level", "lookback": 50},
    "resistance_level": {"description": "Nearest resistance level", "lookback": 50},
    "pivot_point": {"description": "Standard pivot point", "derived": True},
    "fibonacci_382": {"description": "38.2% Fibonacci retracement", "lookback": 50},
}

# Total: 44 features (15 momentum + 12 trend + 8 volatility + 5 volume + 4 structure)


# ── Model Architecture Configuration ───────────────────────────────────────────
MODEL_ARCHITECTURE: dict[str, Any] = {
    "type": "lstm_transformer_hybrid",
    "description": "LSTM for temporal patterns + Transformer for attention mechanism",
    
    # LSTM configuration
    "lstm": {
        "num_layers": 2,
        "hidden_size": 128,
        "dropout": 0.2,
        "bidirectional": True,
    },
    
    # Transformer configuration
    "transformer": {
        "num_heads": 8,
        "num_layers": 2,
        "d_model": 128,
        "d_ff": 512,
        "dropout": 0.1,
    },
    
    # Multi-output head configuration
    "output_heads": {
        "direction": {
            "type": "classification",
            "num_classes": 3,  # BUY, SELL, HOLD
            "activation": "softmax",
        },
        "confidence": {
            "type": "regression",
            "activation": "sigmoid",
            "range": [0.0, 1.0],
        },
        "entry_price": {
            "type": "regression",
            "activation": "linear",
        },
        "tp1": {
            "type": "regression",
            "activation": "linear",
        },
        "tp2": {
            "type": "regression",
            "activation": "linear",
        },
        "tp3": {
            "type": "regression",
            "activation": "linear",
        },
        "stop_loss": {
            "type": "regression",
            "activation": "linear",
        },
        "volatility": {
            "type": "regression",
            "activation": "relu",  # Ensure positive
        },
    },
    
    # Input configuration
    "input": {
        "sequence_length": 60,  # 60 time steps (e.g., 60 days for daily data)
        "num_features": 40,  # Number of features per time step (CROSS_SECTION_FEATURE_NAMES)
    },
}


# ── Training Configuration ─────────────────────────────────────────────────────
TRAINING_CONFIG: dict[str, Any] = {
    # Data splitting
    "train_split": 0.7,  # 70% training
    "val_split": 0.15,   # 15% validation
    "test_split": 0.15,  # 15% testing
    
    # Training hyperparameters
    "batch_size": 32,
    "epochs": 100,
    "learning_rate": 0.001,
    "weight_decay": 0.0001,
    "early_stopping_patience": 10,
    
    # Optimizer configuration
    "optimizer": {
        "type": "adam",
        "betas": [0.9, 0.999],
        "eps": 1e-8,
    },
    
    # Learning rate scheduler
    "lr_scheduler": {
        "type": "reduce_on_plateau",
        "factor": 0.5,
        "patience": 5,
        "min_lr": 1e-6,
    },
    
    # Loss function weights (multi-output)
    "loss_weights": {
        "direction": 2.0,      # Highest priority
        "confidence": 1.0,
        "entry_price": 1.5,
        "tp1": 1.0,
        "tp2": 1.0,
        "tp3": 1.0,
        "stop_loss": 1.5,      # Important for risk management
        "volatility": 0.5,
    },
    
    # Data augmentation
    "augmentation": {
        "enabled": True,
        "noise_std": 0.01,  # Add Gaussian noise to features
        "time_shift": 3,    # Random time shift ±3 steps
    },
    
    # Class balancing (for direction classification)
    "class_weights": {
        "BUY": 1.0,
        "SELL": 1.0,
        "HOLD": 0.5,  # Reduce weight for HOLD class
    },
}


# ── Hyperparameter Search Space ───────────────────────────────────────────────
HYPERPARAMETER_SEARCH_SPACE: dict[str, Any] = {
    "lstm_hidden_size": [64, 128, 256],
    "lstm_num_layers": [1, 2, 3],
    "transformer_num_heads": [4, 8, 16],
    "transformer_num_layers": [1, 2, 3],
    "learning_rate": [0.0001, 0.001, 0.01],
    "batch_size": [16, 32, 64],
    "dropout": [0.1, 0.2, 0.3],
}


# ── Evaluation Metrics ─────────────────────────────────────────────────────────
EVALUATION_METRICS: list[str] = [
    # Classification metrics (direction)
    "accuracy",
    "precision",
    "recall",
    "f1_score",
    "confusion_matrix",
    
    # Regression metrics (prices)
    "mae",  # Mean Absolute Error
    "mse",  # Mean Squared Error
    "rmse", # Root Mean Squared Error
    "r2_score",
    
    # Custom trading metrics
    "directional_accuracy",  # % of correct BUY/SELL/HOLD predictions
    "profit_factor",         # Gross profit / Gross loss
    "sharpe_ratio",          # Risk-adjusted returns
    "max_drawdown",          # Maximum peak-to-trough decline
]


# ── ONNX Export Configuration ──────────────────────────────────────────────────
ONNX_EXPORT_CONFIG: dict[str, Any] = {
    "opset_version": 14,  # ONNX opset version
    "dynamic_axes": {
        "input": {0: "batch_size"},
        "output": {0: "batch_size"},
    },
    "optimization": {
        "quantization": "int8",  # INT8 quantization for CPU inference
        "graph_optimization_level": "all",
    },
}


# ── Timeframe Configuration ────────────────────────────────────────────────────
TIMEFRAME_CONFIG: dict[str, dict[str, Any]] = {
    "1d": {
        "description": "Daily timeframe",
        "sequence_length": 60,  # 60 days
        "min_data_points": 200,  # Minimum 200 days for training
        "cache_ttl": 300,  # 5 minutes
        "weight": 2.0,  # Ensemble weight
    },
    "1w": {
        "description": "Weekly timeframe",
        "sequence_length": 52,  # 52 weeks (1 year)
        "min_data_points": 104,  # Minimum 2 years
        "cache_ttl": 3600,  # 1 hour
        "weight": 3.0,  # Higher weight for longer timeframe
    },
    # Future: Intraday timeframes
    "1h": {
        "description": "1-hour timeframe (future)",
        "sequence_length": 120,  # 120 hours (5 days)
        "min_data_points": 500,
        "cache_ttl": 60,  # 1 minute
        "weight": 1.0,
        "enabled": False,  # Not yet available
    },
    "15m": {
        "description": "15-minute timeframe (future)",
        "sequence_length": 240,  # 240 periods (60 hours)
        "min_data_points": 1000,
        "cache_ttl": 30,  # 30 seconds
        "weight": 0.5,
        "enabled": False,  # Not yet available
    },
}


# ── Feature Engineering Configuration ──────────────────────────────────────────
FEATURE_ENGINEERING_CONFIG: dict[str, Any] = {
    # Missing data handling
    "missing_data": {
        "strategy": "forward_fill",
        "max_consecutive_missing": 3,
        "drop_threshold": 0.05,  # Drop stocks with >5% missing data
    },
    
    # Outlier detection
    "outliers": {
        "method": "iqr",  # Interquartile range
        "threshold": 3.0,  # 3x IQR
        "action": "clip",  # Clip to threshold (don't remove)
    },
    
    # Feature scaling
    "scaling": {
        "method": "robust",  # Robust to outliers
        "feature_range": [-1, 1],
    },
    
    # Feature selection
    "feature_selection": {
        "enabled": True,
        "method": "mutual_information",
        "top_k": 38,  # Select top 38 features from 40
    },
}


# ── Scheduled Retrain (C1) ────────────────────────────────────────────────────
# Cadence + window knobs for the local scheduled-retrain wrapper
# (`backend/scripts/scheduled_retrain.py`) and the systemd timer unit
# (`deploy/cortex-retrain.timer`).  Both entrypoints read this dict so the
# cron schedule, window policy, and on-disk paths have a single source of
# truth.  NO promotion automation lives here — the wrapper produces a
# challenger run dir only; an operator promotes via `promote_model.py`.
SCHEDULED_RETRAIN: dict[str, Any] = {
    # APScheduler cron expression (mirrored by the systemd OnCalendar line).
    # Saturday 20:00 Asia/Kolkata = well after Friday market close on NSE.
    "cron_day_of_week": "sat",
    "cron_hour":        20,
    "cron_timezone":    "Asia/Kolkata",

    # Retrain window policy.
    #   "expanding" — every retrain uses ALL historical bars (default).
    #   "rolling"   — sliding window of `lookback_years` years ending today.
    # Currently advisory: the orchestrator's TrainingConfig.lookback_years
    # already governs window size; this dict surfaces the choice for the
    # scheduled wrapper to log and (in C2) record in the Promotion Report.
    "window_mode":    "expanding",
    "lookback_years": 10,

    # Concurrency guard — prevents two retrains running simultaneously
    # (e.g. systemd-timer fires while a previous --once is still going).
    # Path is relative to the backend/ directory.
    "lock_file": ".scheduled_retrain.lock",

    # Per-run stdout/stderr capture from the orchestrator subprocess.
    # systemd journal also captures the wrapper's own logs.
    "log_dir": "logs/scheduled_retrain",
}


def get_enabled_timeframes() -> list[str]:
    """Get list of currently enabled timeframes."""
    return [tf for tf, config in TIMEFRAME_CONFIG.items() if config.get("enabled", True)]


def get_feature_count() -> int:
    """Get total number of defined features."""
    return len(FEATURE_DEFINITIONS)


def get_model_input_shape() -> tuple[int, int]:
    """Get model input shape (sequence_length, num_features)."""
    return (
        MODEL_ARCHITECTURE["input"]["sequence_length"],
        MODEL_ARCHITECTURE["input"]["num_features"],
    )
