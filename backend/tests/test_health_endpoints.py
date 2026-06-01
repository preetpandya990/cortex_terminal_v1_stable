"""
Tests for ML health check endpoints.

Requirements: 17.5, 19.4, 26.2
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.health import (
    check_database_health,
    check_redis_health,
    check_model_health,
)


@pytest.mark.asyncio
async def test_check_database_health_success():
    """Test database health check when database is healthy."""
    # Mock database session
    db = AsyncMock(spec=AsyncSession)
    
    # Mock execute to return a result with an active model
    mock_result = MagicMock()
    mock_model = MagicMock()
    mock_model.model_id = "test_model_v1"
    mock_model.model_version = "1.0.0"
    mock_result.scalar_one_or_none.return_value = mock_model
    db.execute.return_value = mock_result
    
    # Run check
    healthy, details = await check_database_health(db)
    
    # Assertions
    assert healthy is True
    assert details["connected"] is True
    assert details["active_model_available"] is True
    assert details["active_model_id"] == "test_model_v1"
    assert details["active_model_version"] == "1.0.0"


@pytest.mark.asyncio
async def test_check_database_health_no_model():
    """Test database health check when no active model exists."""
    # Mock database session
    db = AsyncMock(spec=AsyncSession)
    
    # Mock execute to return no model
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db.execute.return_value = mock_result
    
    # Run check
    healthy, details = await check_database_health(db)
    
    # Assertions
    assert healthy is True  # Database is connected
    assert details["connected"] is True
    assert details["active_model_available"] is False


@pytest.mark.asyncio
async def test_check_database_health_failure():
    """Test database health check when database is unavailable."""
    # Mock database session that raises exception
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = Exception("Connection failed")
    
    # Run check
    healthy, details = await check_database_health(db)
    
    # Assertions
    assert healthy is False
    assert details["connected"] is False
    assert "error" in details


@pytest.mark.asyncio
async def test_check_redis_health_success():
    """Test Redis health check when Redis is healthy."""
    # Mock cache service
    cache = MagicMock()
    cache._redis = AsyncMock()
    cache._redis.ping = AsyncMock()
    cache.get_cache_stats = AsyncMock(return_value={
        "total_keys": 100,
        "hit_rate": 0.85,
        "memory_used": "10M",
    })
    
    # Run check
    healthy, details = await check_redis_health(cache)
    
    # Assertions
    assert healthy is True
    assert details["connected"] is True
    assert details["total_keys"] == 100
    assert details["hit_rate"] == 0.85


@pytest.mark.asyncio
async def test_check_redis_health_failure():
    """Test Redis health check when Redis is unavailable."""
    # Mock cache service that raises exception
    cache = MagicMock()
    cache._redis = AsyncMock()
    cache._redis.ping.side_effect = Exception("Connection failed")
    
    # Run check
    healthy, details = await check_redis_health(cache)
    
    # Assertions
    assert healthy is False
    assert details["connected"] is False
    assert "error" in details


@pytest.mark.asyncio
async def test_check_model_health_success():
    """Test model health check when model is available."""
    # Mock database session
    db = AsyncMock(spec=AsyncSession)
    
    # Mock execute to return an active model
    mock_result = MagicMock()
    mock_model = MagicMock()
    mock_model.model_id = "test_model_v1"
    mock_model.model_version = "1.0.0"
    mock_model.model_name = "Test Model"
    mock_model.deployed_at = None
    mock_model.onnx_path = "/tmp/test_model.onnx"
    mock_result.scalar_one_or_none.return_value = mock_model
    db.execute.return_value = mock_result
    
    # Mock os.path.exists to return True
    with patch("os.path.exists", return_value=True):
        # Run check
        healthy, details = await check_model_health(db)
    
    # Assertions
    assert healthy is True
    assert details["model_loaded"] is True
    assert details["model_id"] == "test_model_v1"
    assert details["model_version"] == "1.0.0"
    assert details["model_file_exists"] is True


@pytest.mark.asyncio
async def test_check_model_health_no_model():
    """Test model health check when no active model exists."""
    # Mock database session
    db = AsyncMock(spec=AsyncSession)
    
    # Mock execute to return no model
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    db.execute.return_value = mock_result
    
    # Run check
    healthy, details = await check_model_health(db)
    
    # Assertions
    assert healthy is False
    assert details["model_loaded"] is False
    assert "error" in details


@pytest.mark.asyncio
async def test_check_model_health_file_not_found():
    """Test model health check when model file doesn't exist."""
    # Mock database session
    db = AsyncMock(spec=AsyncSession)
    
    # Mock execute to return an active model
    mock_result = MagicMock()
    mock_model = MagicMock()
    mock_model.model_id = "test_model_v1"
    mock_model.model_version = "1.0.0"
    mock_model.model_name = "Test Model"
    mock_model.deployed_at = None
    mock_model.onnx_path = "/tmp/missing_model.onnx"
    mock_result.scalar_one_or_none.return_value = mock_model
    db.execute.return_value = mock_result
    
    # Mock os.path.exists to return False
    with patch("os.path.exists", return_value=False):
        # Run check
        healthy, details = await check_model_health(db)
    
    # Assertions
    assert healthy is False
    assert details["model_loaded"] is True
    assert details["model_file_exists"] is False


@pytest.mark.asyncio
async def test_check_model_health_failure():
    """Test model health check when database query fails."""
    # Mock database session that raises exception
    db = AsyncMock(spec=AsyncSession)
    db.execute.side_effect = Exception("Query failed")
    
    # Run check
    healthy, details = await check_model_health(db)
    
    # Assertions
    assert healthy is False
    assert details["model_loaded"] is False
    assert "error" in details


# Integration tests would require a test client and database
# These are unit tests for the health check functions
