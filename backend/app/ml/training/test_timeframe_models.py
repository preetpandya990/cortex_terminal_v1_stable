"""
Cortex AI — Timeframe Models Test
===================================
Unit tests for timeframe-specific model training and feature computation.
"""
import numpy as np
import pandas as pd
import pytest

from app.ml.training.timeframe_config import (
    get_timeframe_config,
    get_indicator_period,
    get_sequence_length,
    get_training_config,
    get_all_timeframes,
)
from app.ml.features.timeframe_features import (
    TimeframeFeatureComputer,
    get_timeframe_feature_definitions,
)


class TestTimeframeConfig:
    """Test timeframe configuration."""
    
    def test_get_all_timeframes(self):
        """Test getting all supported timeframes."""
        timeframes = get_all_timeframes()
        
        assert len(timeframes) == 3
        assert "1d" in timeframes
        assert "1w" in timeframes
        assert "1M" in timeframes
    
    def test_get_timeframe_config(self):
        """Test getting timeframe configuration."""
        # Daily config
        daily_config = get_timeframe_config("1d")
        assert daily_config["name"] == "daily"
        assert daily_config["sequence_length"] == 60
        assert daily_config["indicator_periods"]["rsi"] == 14
        
        # Weekly config
        weekly_config = get_timeframe_config("1w")
        assert weekly_config["name"] == "weekly"
        assert weekly_config["sequence_length"] == 52
        assert weekly_config["indicator_periods"]["rsi"] == 70  # 14 * 5
        
        # Monthly config
        monthly_config = get_timeframe_config("1M")
        assert monthly_config["name"] == "monthly"
        assert monthly_config["sequence_length"] == 24
        assert monthly_config["indicator_periods"]["rsi"] == 294  # 14 * 21
    
    def test_get_indicator_period(self):
        """Test getting indicator periods for different timeframes."""
        # RSI periods
        assert get_indicator_period("1d", "rsi") == 14
        assert get_indicator_period("1w", "rsi") == 70
        assert get_indicator_period("1M", "rsi") == 294
        
        # MACD periods
        assert get_indicator_period("1d", "macd_fast") == 12
        assert get_indicator_period("1w", "macd_fast") == 60
        assert get_indicator_period("1M", "macd_fast") == 252
    
    def test_get_sequence_length(self):
        """Test getting sequence lengths."""
        assert get_sequence_length("1d") == 60
        assert get_sequence_length("1w") == 52
        assert get_sequence_length("1M") == 24
    
    def test_get_training_config(self):
        """Test getting training configuration."""
        daily_config = get_training_config("1d")
        
        assert "batch_size" in daily_config
        assert "epochs" in daily_config
        assert "learning_rate" in daily_config
        assert "early_stopping_patience" in daily_config
        assert "validation_split" in daily_config
        assert "test_split" in daily_config
    
    def test_invalid_timeframe(self):
        """Test error handling for invalid timeframe."""
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            get_timeframe_config("invalid")
    
    def test_invalid_indicator(self):
        """Test error handling for invalid indicator."""
        with pytest.raises(ValueError, match="Unsupported indicator"):
            get_indicator_period("1d", "invalid_indicator")


class TestTimeframeFeatureComputer:
    """Test timeframe-specific feature computation."""
    
    @pytest.fixture
    def sample_ohlcv_data(self):
        """Create sample OHLCV data for testing."""
        np.random.seed(42)
        n_samples = 100
        
        df = pd.DataFrame({
            "timestamp": pd.date_range("2023-01-01", periods=n_samples, freq="D"),
            "open": np.random.randn(n_samples) * 10 + 1000,
            "high": np.random.randn(n_samples) * 10 + 1010,
            "low": np.random.randn(n_samples) * 10 + 990,
            "close": np.random.randn(n_samples) * 10 + 1000,
            "volume": np.random.randint(1000000, 10000000, n_samples),
        })
        
        return df
    
    def test_feature_computer_initialization(self):
        """Test feature computer initialization."""
        # Daily
        daily_computer = TimeframeFeatureComputer("1d")
        assert daily_computer.timeframe == "1d"
        assert daily_computer.config["name"] == "daily"
        
        # Weekly
        weekly_computer = TimeframeFeatureComputer("1w")
        assert weekly_computer.timeframe == "1w"
        assert weekly_computer.config["name"] == "weekly"
    
    def test_compute_all_features(self, sample_ohlcv_data):
        """Test computing all features."""
        computer = TimeframeFeatureComputer("1d")
        features_df = computer.compute_all_features(sample_ohlcv_data)
        
        # Check that features were computed
        assert len(features_df) == len(sample_ohlcv_data)
        assert len(features_df.columns) > 0
        
        # Check for expected feature columns
        expected_features = [
            "rsi_14", "rsi_21",
            "macd_line", "macd_signal", "macd_histogram",
            "sma_20", "sma_50", "sma_200",
            "atr_14", "bb_upper", "bb_middle", "bb_lower",
            "obv", "volume_sma_20",
        ]
        
        for feature in expected_features:
            assert feature in features_df.columns, f"Missing feature: {feature}"
    
    def test_rsi_computation(self, sample_ohlcv_data):
        """Test RSI computation."""
        computer = TimeframeFeatureComputer("1d")
        
        close = sample_ohlcv_data["close"]
        rsi = computer._compute_rsi(close, period=14)
        
        # RSI should be between 0 and 100
        assert rsi.min() >= 0
        assert rsi.max() <= 100
    
    def test_macd_computation(self, sample_ohlcv_data):
        """Test MACD computation."""
        computer = TimeframeFeatureComputer("1d")
        
        close = sample_ohlcv_data["close"]
        macd_line, macd_signal, macd_hist = computer._compute_macd(
            close, fast=12, slow=26, signal=9
        )
        
        # Check that MACD components are computed
        assert len(macd_line) == len(close)
        assert len(macd_signal) == len(close)
        assert len(macd_hist) == len(close)
        
        # MACD histogram should be difference of line and signal
        np.testing.assert_array_almost_equal(
            macd_hist.dropna(),
            (macd_line - macd_signal).dropna(),
            decimal=5,
        )
    
    def test_bollinger_bands_computation(self, sample_ohlcv_data):
        """Test Bollinger Bands computation."""
        computer = TimeframeFeatureComputer("1d")
        
        close = sample_ohlcv_data["close"]
        upper, middle, lower = computer._compute_bollinger_bands(
            close, period=20, std_dev=2
        )
        
        # Upper should be above middle, middle above lower
        assert (upper.dropna() >= middle.dropna()).all()
        assert (middle.dropna() >= lower.dropna()).all()


class TestFeatureDefinitions:
    """Test feature definitions."""
    
    def test_get_timeframe_feature_definitions(self):
        """Test getting feature definitions for timeframes."""
        # Daily definitions
        daily_defs = get_timeframe_feature_definitions("1d")
        assert "rsi_14" in daily_defs
        assert daily_defs["rsi_14"]["period"] == 14
        assert daily_defs["rsi_14"]["category"] == "momentum"
        
        # Weekly definitions
        weekly_defs = get_timeframe_feature_definitions("1w")
        assert "rsi_14" in weekly_defs
        assert weekly_defs["rsi_14"]["period"] == 70  # Scaled by 5
        
        # Monthly definitions
        monthly_defs = get_timeframe_feature_definitions("1M")
        assert "rsi_14" in monthly_defs
        assert monthly_defs["rsi_14"]["period"] == 294  # Scaled by 21


class TestPeriodScaling:
    """Test that indicator periods are correctly scaled across timeframes."""
    
    def test_weekly_scaling(self):
        """Test that weekly periods are scaled by 5."""
        daily_rsi = get_indicator_period("1d", "rsi")
        weekly_rsi = get_indicator_period("1w", "rsi")
        
        assert weekly_rsi == daily_rsi * 5
    
    def test_monthly_scaling(self):
        """Test that monthly periods are scaled by 21."""
        daily_rsi = get_indicator_period("1d", "rsi")
        monthly_rsi = get_indicator_period("1M", "rsi")
        
        assert monthly_rsi == daily_rsi * 21
    
    def test_all_indicators_scaled(self):
        """Test that all indicators are properly scaled."""
        indicators = [
            "rsi", "rsi_long", "macd_fast", "macd_slow", "macd_signal",
            "stochastic", "roc_short", "roc_long", "williams_r", "cci",
            "momentum", "sma_short", "sma_medium", "sma_long",
            "ema_fast", "ema_medium", "ema_slow", "atr", "bb_period",
        ]
        
        for indicator in indicators:
            daily_period = get_indicator_period("1d", indicator)
            weekly_period = get_indicator_period("1w", indicator)
            monthly_period = get_indicator_period("1M", indicator)
            
            # Weekly should be 5x daily
            assert weekly_period == daily_period * 5, \
                f"{indicator}: weekly period not scaled correctly"
            
            # Monthly should be 21x daily
            assert monthly_period == daily_period * 21, \
                f"{indicator}: monthly period not scaled correctly"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
