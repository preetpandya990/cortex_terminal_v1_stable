"""
Cortex AI — Security Unit Tests
=================================
Tests JWT token creation, decoding, expiry, and type enforcement.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from app.core.security import create_access_token, create_refresh_token, decode_token
from app.exceptions import InvalidTokenError as CortexInvalidTokenError


class TestJWTTokens:
    def test_access_token_decodes_correctly(self) -> None:
        token = create_access_token("user-123")
        payload = decode_token(token, expected_type="access")
        assert payload.sub == "user-123"
        assert payload.type == "access"
        assert payload.jti  # must have a unique ID

    def test_refresh_token_decodes_correctly(self) -> None:
        token = create_refresh_token("user-456", "family-1")
        payload = decode_token(token, expected_type="refresh")
        assert payload.sub == "user-456"
        assert payload.type == "refresh"

    def test_access_token_rejected_as_refresh(self) -> None:
        token = create_access_token("user-789")
        with pytest.raises(CortexInvalidTokenError):
            decode_token(token, expected_type="refresh")

    def test_refresh_token_rejected_as_access(self) -> None:
        token = create_refresh_token("user-789", "family-2")
        with pytest.raises(CortexInvalidTokenError):
            decode_token(token, expected_type="access")

    def test_tampered_token_raises(self) -> None:
        token = create_access_token("user-000")
        # Tamper with the signature
        parts = token.split(".")
        tampered = f"{parts[0]}.{parts[1]}.invalidsignature"
        with pytest.raises(CortexInvalidTokenError):
            decode_token(tampered)

    def test_expired_token_raises(self) -> None:
        """Token with past expiry must raise, not return payload."""
        import jwt
        from app.core.config import get_settings

        settings = get_settings()
        past_payload = {
            "sub": "user-exp",
            "jti": "test-jti",
            "iat": int(time.time()) - 3600,
            "exp": int(time.time()) - 1800,  # expired 30 min ago
            "type": "access",
        }
        expired_token = jwt.encode(
            past_payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM
        )
        with pytest.raises(CortexInvalidTokenError):
            decode_token(expired_token)

    def test_empty_string_raises(self) -> None:
        with pytest.raises(CortexInvalidTokenError):
            decode_token("")

    def test_garbage_input_raises(self) -> None:
        with pytest.raises(CortexInvalidTokenError):
            decode_token("not.a.jwt")

    def test_each_token_has_unique_jti(self) -> None:
        """Two tokens for the same user must have different JTIs."""
        t1 = create_access_token("user-same")
        t2 = create_access_token("user-same")
        p1 = decode_token(t1)
        p2 = decode_token(t2)
        assert p1.jti != p2.jti


class TestConfigValidation:
    def test_wildcard_cors_raises(self) -> None:
        """Wildcard CORS must be rejected at settings parse time."""
        from pydantic import ValidationError
        from app.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
                REDIS_URL="redis://localhost:6379/0",
                SECRET_KEY="a" * 32,
                CORS_ALLOWED_ORIGINS=["*"],
                UPSTOX_API_KEY="key12345",
                UPSTOX_API_SECRET="secret12345",
                UPSTOX_REDIRECT_URI="http://localhost/callback",
            )

    def test_default_secret_key_raises(self) -> None:
        """Placeholder SECRET_KEY must be rejected."""
        from pydantic import ValidationError
        from app.core.config import Settings

        with pytest.raises(ValidationError):
            Settings(
                DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
                REDIS_URL="redis://localhost:6379/0",
                SECRET_KEY="your-secret-key-change-in-production",
                CORS_ALLOWED_ORIGINS=["http://localhost:3000"],
                UPSTOX_API_KEY="key12345",
                UPSTOX_API_SECRET="secret12345",
                UPSTOX_REDIRECT_URI="http://localhost/callback",
            )