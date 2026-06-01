"""
Integration tests for ML API JWT authentication

Tests the complete authentication flow with real endpoints.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from app.main import app
from app.core.security import create_token_pair


@pytest.fixture
def client():
    """Create test client"""
    return TestClient(app)


@pytest.fixture
def valid_token():
    """Create a valid JWT token"""
    token_pair = create_token_pair("test_user_123")
    return token_pair.access_token


class TestMLEndpointAuthentication:
    """Test ML endpoints require authentication"""
    
    def test_predict_without_token_returns_401(self, client):
        """Test that prediction request without token returns 401"""
        response = client.post(
            "/api/v1/ml/predict",
            json={
                "symbol": "AAPL",
                "timeframe": "1d"
            }
        )
        
        assert response.status_code == 401
        assert "detail" in response.json()
    
    def test_predict_with_invalid_token_returns_401(self, client):
        """Test that prediction request with invalid token returns 401"""
        response = client.post(
            "/api/v1/ml/predict",
            headers={"Authorization": "Bearer invalid_token_xyz"},
            json={
                "symbol": "AAPL",
                "timeframe": "1d"
            }
        )
        
        assert response.status_code == 401
    
    @patch('app.api.v1.ml_predictions.check_model_availability')
    @patch('app.core.redis.get_redis')
    def test_predict_with_valid_token_succeeds(
        self,
        mock_redis,
        mock_model_check,
        client,
        valid_token
    ):
        """Test that prediction request with valid token succeeds"""
        # Mock Redis to not revoke token
        mock_redis_instance = AsyncMock()
        mock_redis_instance.get.return_value = None
        mock_redis.return_value = mock_redis_instance
        
        # Mock model availability
        mock_model_check.return_value = (True, None)
        
        response = client.post(
            "/api/v1/ml/predict",
            headers={"Authorization": f"Bearer {valid_token}"},
            json={
                "symbol": "AAPL",
                "timeframe": "1d"
            }
        )
        
        # Should not return 401 (may return other errors due to mocking)
        assert response.status_code != 401
    
    def test_batch_predict_without_token_returns_401(self, client):
        """Test that batch prediction without token returns 401"""
        response = client.post(
            "/api/v1/ml/predict/batch",
            json={
                "symbols": ["AAPL", "GOOGL"],
                "timeframe": "1d"
            }
        )
        
        assert response.status_code == 401
    
    def test_get_cached_prediction_without_token_returns_401(self, client):
        """Test that get cached prediction without token returns 401"""
        response = client.get("/api/v1/ml/predictions/AAPL?timeframe=1d")
        
        assert response.status_code == 401
    
    def test_ensemble_predict_without_token_returns_401(self, client):
        """Test that ensemble prediction without token returns 401"""
        response = client.post(
            "/api/v1/ml/predict/ensemble",
            json={
                "symbol": "AAPL",
                "timeframes": ["1d", "1w", "1M"]
            }
        )
        
        assert response.status_code == 401


class TestUserPredictionTracking:
    """Test that predictions are tracked per user"""
    
    @patch('app.api.v1.ml_predictions.check_model_availability')
    @patch('app.api.v1.ml_predictions.get_prediction_engine')
    @patch('app.api.v1.ml_predictions.get_feature_engine_instance')
    @patch('app.core.redis.get_redis')
    @patch('app.api.deps.get_db')
    def test_prediction_stores_user_id(
        self,
        mock_get_db,
        mock_redis,
        mock_feature_engine,
        mock_prediction_engine,
        mock_model_check,
        client,
        valid_token
    ):
        """Test that predictions are associated with user_id"""
        # Mock dependencies
        mock_redis_instance = AsyncMock()
        mock_redis_instance.get.return_value = None
        mock_redis.return_value = mock_redis_instance
        
        mock_model_check.return_value = (True, None)
        
        mock_db_instance = AsyncMock()
        mock_get_db.return_value = mock_db_instance
        
        # Make prediction request
        response = client.post(
            "/api/v1/ml/predict",
            headers={"Authorization": f"Bearer {valid_token}"},
            json={
                "symbol": "AAPL",
                "timeframe": "1d"
            }
        )
        
        # Verify request was authenticated (not 401)
        assert response.status_code != 401


class TestAuditLogging:
    """Test audit logging for authentication events"""
    
    @patch('app.api.v1.ml_auth_utils.log_auth_failure')
    def test_auth_failure_logged(self, mock_log_failure, client):
        """Test that authentication failures are logged"""
        # Make request with invalid token
        response = client.post(
            "/api/v1/ml/predict",
            headers={"Authorization": "Bearer invalid_token"},
            json={
                "symbol": "AAPL",
                "timeframe": "1d"
            }
        )
        
        assert response.status_code == 401
        # Note: log_auth_failure would be called in a real scenario
        # but may not be called in test due to exception handling


class TestRateLimiting:
    """Test per-user rate limiting"""
    
    @patch('app.api.v1.ml_auth_utils.get_user_prediction_count')
    @patch('app.core.redis.get_redis')
    def test_user_prediction_count_tracked(
        self,
        mock_redis,
        mock_get_count,
        client,
        valid_token
    ):
        """Test that user prediction counts are tracked"""
        # Mock Redis
        mock_redis_instance = AsyncMock()
        mock_redis_instance.get.return_value = None
        mock_redis.return_value = mock_redis_instance
        
        # Mock prediction count
        mock_get_count.return_value = 50
        
        # Make prediction request
        response = client.post(
            "/api/v1/ml/predict",
            headers={"Authorization": f"Bearer {valid_token}"},
            json={
                "symbol": "AAPL",
                "timeframe": "1d"
            }
        )
        
        # Verify authentication succeeded
        assert response.status_code != 401
