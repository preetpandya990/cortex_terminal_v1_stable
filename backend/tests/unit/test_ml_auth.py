"""
Unit tests for ML API JWT authentication

Tests Requirements: 18.1, 18.5, 8.3
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, AsyncMock, patch
from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.ml_auth_utils import (
    log_auth_failure,
    log_auth_success,
    get_user_prediction_count,
    check_user_rate_limit,
)
from app.models.ml_data import MLAuditLog, MLPrediction
from app.core.security import get_current_user_id, create_token_pair
from app.exceptions import InvalidTokenError


class TestJWTAuthentication:
    """Test JWT authentication for ML endpoints"""
    
    @pytest.mark.asyncio
    async def test_valid_token_returns_user_id(self):
        """Test that valid JWT token returns user ID"""
        # Create a valid token
        token_pair = create_token_pair("test_user_123")
        
        # Mock request with Authorization header
        mock_request = Mock(spec=Request)
        mock_request.cookies = {}
        
        # Mock credentials
        from fastapi.security import HTTPAuthorizationCredentials
        mock_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials=token_pair.access_token
        )
        
        # Mock Redis
        mock_redis = AsyncMock()
        mock_redis.get.return_value = None  # Token not revoked
        
        # Call get_current_user_id
        user_id = await get_current_user_id(
            request=mock_request,
            credentials=mock_credentials,
            redis=mock_redis
        )
        
        assert user_id == "test_user_123"
    
    @pytest.mark.asyncio
    async def test_invalid_token_raises_401(self):
        """Test that invalid JWT token raises 401 error"""
        # Mock request
        mock_request = Mock(spec=Request)
        mock_request.cookies = {}
        
        # Mock credentials with invalid token
        from fastapi.security import HTTPAuthorizationCredentials
        mock_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials="invalid_token_xyz"
        )
        
        # Mock Redis
        mock_redis = AsyncMock()
        
        # Should raise InvalidTokenError
        with pytest.raises(InvalidTokenError):
            await get_current_user_id(
                request=mock_request,
                credentials=mock_credentials,
                redis=mock_redis
            )
    
    @pytest.mark.asyncio
    async def test_expired_token_raises_401(self):
        """Test that expired JWT token raises 401 error"""
        # Create token with very short expiry
        with patch('app.core.security.settings') as mock_settings:
            mock_settings.ACCESS_TOKEN_EXPIRE_MINUTES = -1  # Already expired
            mock_settings.SECRET_KEY = "test_secret_key"
            mock_settings.ALGORITHM = "HS256"
            
            token_pair = create_token_pair("test_user_123")
        
        # Mock request
        mock_request = Mock(spec=Request)
        mock_request.cookies = {}
        
        # Mock credentials
        from fastapi.security import HTTPAuthorizationCredentials
        mock_credentials = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials=token_pair.access_token
        )
        
        # Mock Redis
        mock_redis = AsyncMock()
        
        # Should raise InvalidTokenError for expired token
        with pytest.raises(InvalidTokenError):
            await get_current_user_id(
                request=mock_request,
                credentials=mock_credentials,
                redis=mock_redis
            )
    
    @pytest.mark.asyncio
    async def test_no_token_raises_401(self):
        """Test that missing token raises 401 error"""
        # Mock request with no token
        mock_request = Mock(spec=Request)
        mock_request.cookies = {}
        
        # No credentials
        mock_credentials = None
        
        # Mock Redis
        mock_redis = AsyncMock()
        
        # Should raise InvalidTokenError
        with pytest.raises(InvalidTokenError):
            await get_current_user_id(
                request=mock_request,
                credentials=mock_credentials,
                redis=mock_redis
            )


class TestAuditLogging:
    """Test audit logging for authentication events"""
    
    @pytest.mark.asyncio
    async def test_log_auth_failure(self):
        """Test logging authentication failure"""
        # Mock database session
        mock_db = AsyncMock(spec=AsyncSession)
        
        # Mock request
        mock_request = Mock(spec=Request)
        mock_request.url.path = "/api/v1/ml/predict"
        mock_request.method = "POST"
        mock_request.query_params = {}
        mock_request.client.host = "192.168.1.1"
        mock_request.headers = {"user-agent": "test-agent"}
        
        # Call log_auth_failure
        await log_auth_failure(
            db=mock_db,
            request=mock_request,
            error_message="Invalid token",
            user_id=None
        )
        
        # Verify audit log was created
        mock_db.add.assert_called_once()
        audit_log = mock_db.add.call_args[0][0]
        
        assert isinstance(audit_log, MLAuditLog)
        assert audit_log.event_type == "auth_failure"
        assert audit_log.endpoint == "/api/v1/ml/predict"
        assert audit_log.ip_address == "192.168.1.1"
        assert audit_log.error_message == "Invalid token"
        assert audit_log.response_status == 401
        
        mock_db.commit.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_log_auth_success(self):
        """Test logging successful authentication"""
        # Mock database session
        mock_db = AsyncMock(spec=AsyncSession)
        
        # Mock request
        mock_request = Mock(spec=Request)
        mock_request.url.path = "/api/v1/ml/predict"
        mock_request.method = "POST"
        mock_request.query_params = {}
        mock_request.client.host = "192.168.1.1"
        mock_request.headers = {"user-agent": "test-agent"}
        
        # Call log_auth_success
        await log_auth_success(
            db=mock_db,
            request=mock_request,
            user_id="test_user_123",
            prediction_id=456
        )
        
        # Verify audit log was created
        mock_db.add.assert_called_once()
        audit_log = mock_db.add.call_args[0][0]
        
        assert isinstance(audit_log, MLAuditLog)
        assert audit_log.event_type == "prediction_request"
        assert audit_log.user_id == "test_user_123"
        assert audit_log.prediction_id == 456
        assert audit_log.response_status == 200
        
        mock_db.commit.assert_called_once()


class TestUserPredictionTracking:
    """Test per-user prediction tracking for rate limiting"""
    
    @pytest.mark.asyncio
    async def test_get_user_prediction_count(self):
        """Test getting user prediction count"""
        # Mock database session
        mock_db = AsyncMock(spec=AsyncSession)
        
        # Mock query result
        mock_result = AsyncMock()
        mock_result.scalar_one.return_value = 5
        mock_db.execute.return_value = mock_result
        
        # Call get_user_prediction_count
        count = await get_user_prediction_count(
            db=mock_db,
            user_id="test_user_123",
            time_window_minutes=60
        )
        
        assert count == 5
        mock_db.execute.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_check_user_rate_limit_allowed(self):
        """Test rate limit check when user is within limit"""
        # Mock database session
        mock_db = AsyncMock(spec=AsyncSession)
        
        # Mock query result - user has made 50 predictions
        mock_result = AsyncMock()
        mock_result.scalar_one.return_value = 50
        mock_db.execute.return_value = mock_result
        
        # Check rate limit (limit is 100)
        is_allowed, count = await check_user_rate_limit(
            db=mock_db,
            user_id="test_user_123",
            limit=100,
            time_window_minutes=60
        )
        
        assert is_allowed is True
        assert count == 50
    
    @pytest.mark.asyncio
    async def test_check_user_rate_limit_exceeded(self):
        """Test rate limit check when user exceeded limit"""
        # Mock database session
        mock_db = AsyncMock(spec=AsyncSession)
        
        # Mock query result - user has made 150 predictions
        mock_result = AsyncMock()
        mock_result.scalar_one.return_value = 150
        mock_db.execute.return_value = mock_result
        
        # Check rate limit (limit is 100)
        is_allowed, count = await check_user_rate_limit(
            db=mock_db,
            user_id="test_user_123",
            limit=100,
            time_window_minutes=60
        )
        
        assert is_allowed is False
        assert count == 150
    
    @pytest.mark.asyncio
    async def test_prediction_associated_with_user(self):
        """Test that predictions are associated with user_id"""
        # Create a prediction with user_id
        prediction = MLPrediction(
            model_id="model_v1",
            user_id="test_user_123",
            symbol="AAPL",
            prediction=1.5,
            timestamp=datetime.now(timezone.utc)
        )
        
        assert prediction.user_id == "test_user_123"
        assert prediction.symbol == "AAPL"


class TestMLEndpointAuthentication:
    """Test that ML endpoints require authentication"""
    
    @pytest.mark.asyncio
    async def test_ml_endpoints_require_authentication(self):
        """Test that ML endpoints have authentication dependency"""
        from app.api.v1.ml_predictions import router
        
        # Check that router has authentication dependency
        assert router.dependencies is not None
        assert len(router.dependencies) > 0
        
        # Verify get_current_user_id is in dependencies
        dep_funcs = [str(dep.dependency) for dep in router.dependencies]
        assert any('get_current_user_id' in func for func in dep_funcs)
    
    @pytest.mark.asyncio
    async def test_predict_endpoint_uses_user_id(self):
        """Test that predict endpoint uses user_id parameter"""
        from app.api.v1.ml_predictions import predict_single
        import inspect
        
        # Get function signature
        sig = inspect.signature(predict_single)
        
        # Verify user_id parameter exists
        assert 'user_id' in sig.parameters
        
        # Verify it uses get_current_user_id dependency
        param = sig.parameters['user_id']
        assert param.default is not inspect.Parameter.empty
