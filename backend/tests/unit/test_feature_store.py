"""Unit tests for FeatureStore."""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.feature_store import FeatureStore
from app.models.ml_data import MLFeature


@pytest.mark.asyncio
async def test_register_feature(db_session: AsyncSession, mock_cache: MagicMock):
    """Test feature registration."""
    store = FeatureStore(db_session, mock_cache, feature_version="1.0.0")
    
    def compute_rsi(df: pd.DataFrame) -> float:
        return 70.5
    
    store.register_feature("rsi_14", compute_rsi, "14-period RSI", "float")
    
    assert "rsi_14" in store._feature_registry
    assert store._feature_metadata["rsi_14"]["description"] == "14-period RSI"
    assert store._feature_metadata["rsi_14"]["version"] == "1.0.0"


@pytest.mark.asyncio
async def test_compute_features_with_known_data(db_session: AsyncSession, mock_cache: MagicMock):
    """Test feature computation with known OHLCV data."""
    store = FeatureStore(db_session, mock_cache)
    
    # Register simple features
    def compute_close(df: pd.DataFrame) -> float:
        return float(df["close"].iloc[-1])
    
    def compute_volume(df: pd.DataFrame) -> float:
        return float(df["volume"].iloc[-1])
    
    store.register_feature("last_close", compute_close)
    store.register_feature("last_volume", compute_volume)
    
    # Create known OHLCV data
    ohlcv = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=5, freq="D"),
        "open": [100.0, 101.0, 102.0, 103.0, 104.0],
        "high": [105.0, 106.0, 107.0, 108.0, 109.0],
        "low": [99.0, 100.0, 101.0, 102.0, 103.0],
        "close": [103.0, 104.0, 105.0, 106.0, 107.0],
        "volume": [1000, 1100, 1200, 1300, 1400],
    })
    
    features = await store.compute_features("BTCUSDT", "1d", ohlcv)
    
    assert features["last_close"] == 107.0
    assert features["last_volume"] == 1400.0


@pytest.mark.asyncio
async def test_compute_features_stores_in_db(db_session: AsyncSession, mock_cache: MagicMock):
    """Test that computed features are stored in database."""
    store = FeatureStore(db_session, mock_cache)
    
    def compute_test(df: pd.DataFrame) -> float:
        return 42.0
    
    store.register_feature("test_feature", compute_test)
    
    ohlcv = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=3, freq="D"),
        "open": [100, 101, 102],
        "high": [105, 106, 107],
        "low": [99, 100, 101],
        "close": [103, 104, 105],
        "volume": [1000, 1100, 1200],
    })
    
    await store.compute_features("BTCUSDT", "1d", ohlcv)
    
    # Query database for stored feature
    result = await db_session.execute(
        db_session.query(MLFeature).filter_by(
            symbol="BTCUSDT",
            feature_name="test_feature"
        )
    )
    feature_record = result.scalar_one_or_none()
    
    assert feature_record is not None
    assert feature_record.feature_value == 42.0


@pytest.mark.asyncio
async def test_cache_hit_scenario(db_session: AsyncSession, mock_cache: MagicMock):
    """Test cache hit returns cached features."""
    store = FeatureStore(db_session, mock_cache)
    
    # Mock cache hit
    cached_features = {"rsi_14": 70.5, "macd": 0.5}
    mock_cache.get.return_value = cached_features
    
    features = await store.get_features("BTCUSDT", "1h", use_cache=True)
    
    assert features == cached_features
    mock_cache.get.assert_called_once()


@pytest.mark.asyncio
async def test_cache_miss_scenario(db_session: AsyncSession, mock_cache: MagicMock):
    """Test cache miss computes and caches features."""
    store = FeatureStore(db_session, mock_cache)
    
    # Mock cache miss
    mock_cache.get.return_value = None
    
    def compute_test(df: pd.DataFrame) -> float:
        return 50.0
    
    store.register_feature("test_feature", compute_test)
    
    # Mock OHLCV data retrieval
    ohlcv = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=3, freq="H"),
        "open": [100, 101, 102],
        "high": [105, 106, 107],
        "low": [99, 100, 101],
        "close": [103, 104, 105],
        "volume": [1000, 1100, 1200],
    })
    
    with pytest.mock.patch.object(store, "_fetch_ohlcv_data", return_value=ohlcv):
        features = await store.get_features("BTCUSDT", "1h", use_cache=True)
    
    assert features["test_feature"] == 50.0
    mock_cache.set.assert_called_once()


@pytest.mark.asyncio
async def test_feature_version_management(db_session: AsyncSession, mock_cache: MagicMock):
    """Test feature versioning."""
    store_v1 = FeatureStore(db_session, mock_cache, feature_version="1.0.0")
    store_v2 = FeatureStore(db_session, mock_cache, feature_version="2.0.0")
    
    def compute_v1(df: pd.DataFrame) -> float:
        return 1.0
    
    def compute_v2(df: pd.DataFrame) -> float:
        return 2.0
    
    store_v1.register_feature("test", compute_v1)
    store_v2.register_feature("test", compute_v2)
    
    assert store_v1._feature_metadata["test"]["version"] == "1.0.0"
    assert store_v2._feature_metadata["test"]["version"] == "2.0.0"


@pytest.mark.asyncio
async def test_invalid_ohlcv_data(db_session: AsyncSession, mock_cache: MagicMock):
    """Test validation of invalid OHLCV data."""
    store = FeatureStore(db_session, mock_cache)
    
    def compute_test(df: pd.DataFrame) -> float:
        return 1.0
    
    store.register_feature("test", compute_test)
    
    # Missing required columns
    invalid_ohlcv = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=3, freq="D"),
        "close": [100, 101, 102],
    })
    
    with pytest.raises(ValueError):
        await store.compute_features("BTCUSDT", "1d", invalid_ohlcv)


@pytest.mark.asyncio
async def test_unregistered_feature_error(db_session: AsyncSession, mock_cache: MagicMock):
    """Test error when requesting unregistered feature."""
    store = FeatureStore(db_session, mock_cache)
    
    ohlcv = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=3, freq="D"),
        "open": [100, 101, 102],
        "high": [105, 106, 107],
        "low": [99, 100, 101],
        "close": [103, 104, 105],
        "volume": [1000, 1100, 1200],
    })
    
    with pytest.raises(KeyError):
        await store.compute_features("BTCUSDT", "1d", ohlcv, feature_names=["nonexistent"])


@pytest.mark.asyncio
async def test_graceful_feature_computation_failure(db_session: AsyncSession, mock_cache: MagicMock):
    """Test graceful handling of feature computation errors."""
    store = FeatureStore(db_session, mock_cache)
    
    def compute_failing(df: pd.DataFrame) -> float:
        raise ValueError("Computation failed")
    
    def compute_working(df: pd.DataFrame) -> float:
        return 42.0
    
    store.register_feature("failing", compute_failing)
    store.register_feature("working", compute_working)
    
    ohlcv = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=3, freq="D"),
        "open": [100, 101, 102],
        "high": [105, 106, 107],
        "low": [99, 100, 101],
        "close": [103, 104, 105],
        "volume": [1000, 1100, 1200],
    })
    
    features = await store.compute_features("BTCUSDT", "1d", ohlcv)
    
    assert features["failing"] is None
    assert features["working"] == 42.0


@pytest.mark.asyncio
async def test_selective_feature_computation(db_session: AsyncSession, mock_cache: MagicMock):
    """Test computing only selected features."""
    store = FeatureStore(db_session, mock_cache)
    
    store.register_feature("feature1", lambda df: 1.0)
    store.register_feature("feature2", lambda df: 2.0)
    store.register_feature("feature3", lambda df: 3.0)
    
    ohlcv = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=3, freq="D"),
        "open": [100, 101, 102],
        "high": [105, 106, 107],
        "low": [99, 100, 101],
        "close": [103, 104, 105],
        "volume": [1000, 1100, 1200],
    })
    
    features = await store.compute_features("BTCUSDT", "1d", ohlcv, feature_names=["feature1", "feature3"])
    
    assert "feature1" in features
    assert "feature2" not in features
    assert "feature3" in features
