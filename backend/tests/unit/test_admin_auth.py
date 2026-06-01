"""
Unit tests for admin role authentication dependency.
"""
import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.core.auth import require_admin_role
from app.core.security import create_access_token


def create_mock_request(path: str = "/admin", method: str = "GET") -> Request:
    """Create a properly mocked Request object."""
    return Request(scope={
        "type": "http",
        "path": path,
        "method": method,
        "headers": [],
        "query_string": b"",
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
    })


@pytest.mark.asyncio
async def test_require_admin_role_with_admin_token():
    """Test that admin role token grants access."""
    # Create admin token
    token = create_access_token("123", extra_claims={"role": "admin"})
    
    # Mock request and credentials
    request = create_mock_request()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    
    # Should succeed and return user ID
    user_id = await require_admin_role(request, credentials)
    assert user_id == "123"


@pytest.mark.asyncio
async def test_require_admin_role_with_viewer_token():
    """Test that viewer role token is rejected."""
    # Create viewer token
    token = create_access_token("456", extra_claims={"role": "viewer"})
    
    # Mock request and credentials
    request = create_mock_request()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    
    # Should raise 403 Forbidden
    with pytest.raises(HTTPException) as exc_info:
        await require_admin_role(request, credentials)
    
    assert exc_info.value.status_code == 403
    assert "Admin privileges required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_admin_role_with_trader_token():
    """Test that trader role token is rejected."""
    # Create trader token
    token = create_access_token("789", extra_claims={"role": "trader"})
    
    # Mock request and credentials
    request = create_mock_request()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    
    # Should raise 403 Forbidden
    with pytest.raises(HTTPException) as exc_info:
        await require_admin_role(request, credentials)
    
    assert exc_info.value.status_code == 403
    assert "Admin privileges required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_admin_role_without_token():
    """Test that missing token is rejected."""
    # Mock request without credentials
    request = create_mock_request()
    
    # Should raise 401 Unauthorized
    with pytest.raises(HTTPException) as exc_info:
        await require_admin_role(request, None)
    
    assert exc_info.value.status_code == 401
    assert "Authentication required" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_admin_role_with_invalid_token():
    """Test that invalid token is rejected."""
    # Mock request with invalid token
    request = create_mock_request()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid.token.here")
    
    # Should raise 401 Unauthorized
    with pytest.raises(HTTPException) as exc_info:
        await require_admin_role(request, credentials)
    
    assert exc_info.value.status_code == 401
    assert "Invalid or expired token" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_admin_role_with_missing_role_claim():
    """Test that token without role claim is rejected."""
    # Create token without role claim
    token = create_access_token("999")
    
    # Mock request and credentials
    request = create_mock_request()
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    
    # Should raise 403 Forbidden (role defaults to None)
    with pytest.raises(HTTPException) as exc_info:
        await require_admin_role(request, credentials)
    
    assert exc_info.value.status_code == 403
    assert "Admin privileges required" in exc_info.value.detail
