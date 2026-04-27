# PATH: backend/app/core/security.py
# ────────────────────────────
"""
Cortex AI — Security / JWT with Refresh Token Rotation
=======================================================
Production-grade JWT implementation with:
  - Short-lived access tokens (15-30 min)
  - Long-lived refresh tokens (7 days) with rotation
  - Token family tracking for reuse detection
  - Automatic revocation on suspicious activity
  - HTTPOnly cookie support
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError
from pydantic import BaseModel
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.redis import get_redis
from app.exceptions import AuthError, InvalidTokenError as CortexInvalidTokenError

logger = logging.getLogger(__name__)
settings = get_settings()

bearer_scheme = HTTPBearer(auto_error=False)  # Don't auto-error, we'll handle cookies too


# ── Token payload schema ───────────────────────────────────────────────────────
class TokenPayload(BaseModel):
    sub: str           # user identifier (e.g. user UUID or email)
    jti: str           # JWT ID — unique per token, used for revocation
    exp: int           # expiry timestamp
    iat: int           # issued-at timestamp
    type: str          # "access" | "refresh"
    family: str | None = None  # Token family ID for refresh token rotation
    role: str | None = None    # User role for RBAC


class TokenPair(BaseModel):
    """Access + Refresh token pair"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expires


# ── Token creation ─────────────────────────────────────────────────────────────
def create_token_pair(subject: str, family_id: str | None = None, role: str = "viewer") -> TokenPair:
    """
    Create a new access + refresh token pair.
    If family_id is provided, the refresh token belongs to that family (rotation).
    If None, a new family is created (initial login).
    """
    if family_id is None:
        family_id = str(uuid.uuid4())
    
    access_token = create_access_token(subject, extra_claims={"role": role})
    refresh_token = create_refresh_token(subject, family_id, role)
    
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )


def create_access_token(subject: str, extra_claims: dict | None = None) -> str:
    """Create a short-lived access JWT."""
    return _create_token(subject, "access", settings.ACCESS_TOKEN_EXPIRE_MINUTES, extra_claims=extra_claims)


def create_refresh_token(subject: str, family_id: str, role: str = "viewer") -> str:
    """Create a longer-lived refresh JWT with family tracking."""
    return _create_token(
        subject, 
        "refresh", 
        settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60,
        family_id=family_id
    )


def _create_token(
    subject: str,
    token_type: str,
    expire_minutes: int,
    family_id: str | None = None,
    extra_claims: dict | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=expire_minutes)
    payload = {
        "sub": subject,
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "type": token_type,
    }
    if family_id:
        payload["family"] = family_id
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# ── Token verification ─────────────────────────────────────────────────────────
def decode_token(token: str, expected_type: str = "access") -> TokenPayload:
    """
    Decode and validate a JWT.
    Raises CortexInvalidTokenError on any failure — never leaks internals.
    """
    try:
        raw = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
        )
        payload = TokenPayload(**raw)
        if payload.type != expected_type:
            raise CortexInvalidTokenError(
                f"Expected token type '{expected_type}', got '{payload.type}'"
            )
        return payload
    except ExpiredSignatureError:
        raise CortexInvalidTokenError("Token has expired")
    except (InvalidTokenError, Exception) as exc:
        logger.debug("Token decode failed: %s", type(exc).__name__)
        raise CortexInvalidTokenError()


# ── Refresh token rotation & reuse detection ───────────────────────────────────
async def rotate_refresh_token(
    refresh_token: str,
    redis: Redis
) -> TokenPair:
    """
    Validate refresh token and issue new token pair with rotation.
    
    Security features:
    - Detects token reuse (revokes entire family)
    - Tracks used tokens to prevent replay attacks
    - Rotates refresh token on every use
    """
    # Decode and validate refresh token
    payload = decode_token(refresh_token, expected_type="refresh")
    
    # Check if token was already used (reuse detection)
    used_key = f"refresh:used:{payload.jti}"
    was_used = await redis.get(used_key)
    
    if was_used:
        # Token reuse detected! Revoke entire family
        logger.warning(
            "Refresh token reuse detected! Revoking family=%s sub=%s",
            payload.family,
            payload.sub
        )
        if payload.family:
            await revoke_token_family(payload.family, redis)
        raise CortexInvalidTokenError("Token reuse detected - all tokens revoked")
    
    # Mark this token as used
    ttl = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
    await redis.setex(used_key, ttl, "1")
    
    # Check if token family is revoked
    if payload.family:
        revoked = await redis.get(f"refresh:family:revoked:{payload.family}")
        if revoked:
            raise CortexInvalidTokenError("Token family has been revoked")
    
    role = getattr(payload, "role", "viewer")
    return create_token_pair(payload.sub, family_id=payload.family, role=role)


async def revoke_token_family(family_id: str, redis: Redis) -> None:
    """Revoke all tokens in a family (used when reuse is detected)."""
    ttl = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
    await redis.setex(f"refresh:family:revoked:{family_id}", ttl, "1")
    logger.info("Revoked token family: %s", family_id)


async def revoke_refresh_token(jti: str, redis: Redis) -> None:
    """Revoke a specific refresh token by its JTI."""
    ttl = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600
    await redis.setex(f"refresh:revoked:{jti}", ttl, "1")


# ── Cookie helpers ─────────────────────────────────────────────────────────────
def set_refresh_token_cookie(response: Response, refresh_token: str) -> None:
    """Set refresh token in HTTPOnly, Secure, SameSite cookie."""
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.is_production,
        samesite="strict",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 3600,
        path="/api/v1/auth"  # Only send to auth endpoints
    )


def clear_refresh_token_cookie(response: Response) -> None:
    """Clear the refresh token cookie."""
    response.delete_cookie(
        key="refresh_token",
        path="/api/v1/auth"
    )


# ── FastAPI dependencies ───────────────────────────────────────────────────────
async def get_current_user_id(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    redis: Redis = Depends(get_redis)
) -> str:
    """
    Extract and validate the Bearer token, returning the subject (user ID).
    Supports both Authorization header and cookie-based tokens.
    Raise 401 on any failure — details are intentionally generic.
    """
    token = None
    
    # Try Authorization header first
    if credentials:
        token = credentials.credentials
    # Fallback to cookie (for browser requests)
    elif "access_token" in request.cookies:
        token = request.cookies.get("access_token")
    
    if not token:
        raise CortexInvalidTokenError("No authentication token provided")
    
    payload = decode_token(token)
    
    # Check if token is revoked
    revoked = await redis.get(f"access:revoked:{payload.jti}")
    if revoked:
        raise CortexInvalidTokenError("Token has been revoked")
    
    return payload.sub


def get_refresh_token_from_cookie(request: Request) -> str:
    """Extract refresh token from HTTPOnly cookie."""
    token = request.cookies.get("refresh_token")
    if not token:
        raise CortexInvalidTokenError("No refresh token provided")
    return token


CurrentUserID = Annotated[str, Depends(get_current_user_id)]
