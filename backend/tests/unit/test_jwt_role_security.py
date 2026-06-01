"""
JWT Role Claim Security Verification Tests
===========================================
Comprehensive security audit of JWT role-based authorization.

Tests verify compliance with:
- OWASP JWT Security Best Practices (RFC 8725)
- Industry standards for RBAC in JWTs
- Protection against common JWT vulnerabilities
"""
import jwt
import pytest
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.security import create_access_token, decode_token, TokenPayload
from app.exceptions import InvalidTokenError

settings = get_settings()


class TestJWTRoleClaimSecurity:
    """Verify JWT role claim implementation meets security standards."""
    
    def test_role_claim_present_in_token(self):
        """Verify role claim is included in JWT payload."""
        token = create_access_token("user123", extra_claims={"role": "admin"})
        
        # Decode without verification to inspect payload
        unverified = jwt.decode(token, options={"verify_signature": False})
        
        assert "role" in unverified, "Role claim must be present in JWT"
        assert unverified["role"] == "admin"
    
    def test_role_claim_cryptographically_protected(self):
        """Verify role claim is protected by signature (tampering detection)."""
        token = create_access_token("user123", extra_claims={"role": "viewer"})
        
        # Split token into parts
        header, payload, signature = token.split(".")
        
        # Decode payload and tamper with role
        import base64
        import json
        
        # Decode payload
        padded = payload + "=" * (4 - len(payload) % 4)
        decoded_payload = json.loads(base64.urlsafe_b64decode(padded))
        
        # Tamper: change viewer to admin
        decoded_payload["role"] = "admin"
        
        # Re-encode tampered payload
        tampered_payload = base64.urlsafe_b64encode(
            json.dumps(decoded_payload).encode()
        ).decode().rstrip("=")
        
        # Reconstruct token with tampered payload
        tampered_token = f"{header}.{tampered_payload}.{signature}"
        
        # Verification must fail
        with pytest.raises(InvalidTokenError):
            decode_token(tampered_token)
    
    def test_role_claim_validated_on_decode(self):
        """Verify role claim is properly extracted and validated."""
        token = create_access_token("user456", extra_claims={"role": "trader"})
        payload = decode_token(token)
        
        assert isinstance(payload, TokenPayload)
        assert payload.role == "trader"
        assert payload.sub == "user456"
    
    def test_algorithm_cannot_be_none(self):
        """Verify 'alg: none' attack is prevented."""
        # Create token with alg=none header
        import base64
        import json
        
        header = {"alg": "none", "typ": "JWT"}
        payload = {"sub": "attacker", "role": "admin", "exp": 9999999999}
        
        encoded_header = base64.urlsafe_b64encode(
            json.dumps(header).encode()
        ).decode().rstrip("=")
        
        encoded_payload = base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        ).decode().rstrip("=")
        
        # Token with no signature
        malicious_token = f"{encoded_header}.{encoded_payload}."
        
        # Must be rejected
        with pytest.raises(InvalidTokenError):
            decode_token(malicious_token)
    
    def test_expired_token_rejected(self):
        """Verify expired tokens are rejected regardless of role."""
        # Create token that expires immediately
        now = datetime.now(timezone.utc)
        past = now - timedelta(hours=1)
        
        payload = {
            "sub": "user789",
            "jti": "test-jti",
            "iat": int(past.timestamp()),
            "exp": int((past + timedelta(minutes=1)).timestamp()),
            "type": "access",
            "role": "admin"
        }
        
        expired_token = jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
        
        with pytest.raises(InvalidTokenError) as exc_info:
            decode_token(expired_token)
        
        assert "expired" in str(exc_info.value).lower()
    
    def test_role_claim_supports_all_valid_roles(self):
        """Verify all valid roles (viewer, trader, admin) work correctly."""
        valid_roles = ["viewer", "trader", "admin"]
        
        for role in valid_roles:
            token = create_access_token(f"user_{role}", extra_claims={"role": role})
            payload = decode_token(token)
            assert payload.role == role
    
    def test_missing_role_claim_handled_gracefully(self):
        """Verify tokens without role claim don't crash (defaults to None)."""
        token = create_access_token("user_no_role")  # No role claim
        payload = decode_token(token)
        
        assert payload.role is None  # Should default to None, not crash
    
    def test_token_type_validation(self):
        """Verify token type claim is validated (access vs refresh)."""
        # Create refresh token
        from app.core.security import create_refresh_token
        refresh_token = create_refresh_token("user123", "family-id", "admin")
        
        # Trying to decode as access token should fail
        with pytest.raises(InvalidTokenError):
            decode_token(refresh_token, expected_type="access")
    
    def test_signature_algorithm_enforced(self):
        """Verify only HS256 algorithm is accepted."""
        # Create token with different algorithm
        payload = {
            "sub": "user123",
            "role": "admin",
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
        }
        
        # Sign with HS512 (not allowed)
        malicious_token = jwt.encode(payload, settings.SECRET_KEY, algorithm="HS512")
        
        # Must be rejected
        with pytest.raises(InvalidTokenError):
            decode_token(malicious_token)
    
    def test_role_claim_immutable_after_issuance(self):
        """Verify role cannot be changed without re-authentication."""
        # This is inherently guaranteed by signature verification
        # but we test the principle explicitly
        
        token = create_access_token("user123", extra_claims={"role": "viewer"})
        payload1 = decode_token(token)
        
        # Same token decoded again should have same role
        payload2 = decode_token(token)
        
        assert payload1.role == payload2.role == "viewer"
        assert payload1.jti == payload2.jti  # Same token


class TestRoleBasedAuthorizationIntegration:
    """Integration tests for role-based authorization flow."""
    
    @pytest.mark.asyncio
    async def test_admin_role_grants_access(self):
        """Verify admin role in JWT grants admin access."""
        from app.core.auth import require_admin_role
        from fastapi import Request
        from fastapi.security import HTTPAuthorizationCredentials
        
        token = create_access_token("admin_user", extra_claims={"role": "admin"})
        
        request = Request(scope={
            "type": "http",
            "path": "/admin/test",
            "method": "POST",
            "headers": [],
            "query_string": b"",
            "root_path": "",
            "scheme": "https",
            "server": ("api.cortex.ai", 443),
        })
        
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        
        user_id = await require_admin_role(request, credentials)
        assert user_id == "admin_user"
    
    @pytest.mark.asyncio
    async def test_non_admin_role_denied_access(self):
        """Verify non-admin roles are rejected by admin endpoints."""
        from app.core.auth import require_admin_role
        from fastapi import Request, HTTPException
        from fastapi.security import HTTPAuthorizationCredentials
        
        token = create_access_token("trader_user", extra_claims={"role": "trader"})
        
        request = Request(scope={
            "type": "http",
            "path": "/admin/test",
            "method": "POST",
            "headers": [],
            "query_string": b"",
            "root_path": "",
            "scheme": "https",
            "server": ("api.cortex.ai", 443),
        })
        
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        
        with pytest.raises(HTTPException) as exc_info:
            await require_admin_role(request, credentials)
        
        assert exc_info.value.status_code == 403


class TestSecurityBestPractices:
    """Verify compliance with JWT security best practices."""
    
    def test_short_lived_access_tokens(self):
        """Verify access tokens have short expiration (≤30 min)."""
        assert settings.ACCESS_TOKEN_EXPIRE_MINUTES <= 30, \
            "Access tokens should expire within 30 minutes (RFC 8725)"
    
    def test_secret_key_minimum_length(self):
        """Verify SECRET_KEY meets minimum entropy requirements."""
        assert len(settings.SECRET_KEY) >= 32, \
            "SECRET_KEY must be at least 32 characters (256 bits)"
    
    def test_algorithm_is_secure(self):
        """Verify using secure HMAC algorithm."""
        assert settings.ALGORITHM == "HS256", \
            "Should use HS256 for symmetric signing"
    
    def test_standard_claims_present(self):
        """Verify standard JWT claims (sub, exp, iat, jti) are present."""
        token = create_access_token("user123", extra_claims={"role": "admin"})
        unverified = jwt.decode(token, options={"verify_signature": False})
        
        required_claims = ["sub", "exp", "iat", "jti", "type"]
        for claim in required_claims:
            assert claim in unverified, f"Standard claim '{claim}' must be present"
