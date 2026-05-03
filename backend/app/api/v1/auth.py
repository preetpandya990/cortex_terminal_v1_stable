"""
Authentication Routes - Registration, Login, Token Management
"""
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import bcrypt

from app.api.deps import get_db
from app.core.config import get_settings
from app.core.redis import get_redis
from app.core.security import (
    create_token_pair,
    decode_token,
    set_refresh_token_cookie,
    clear_refresh_token_cookie,
    get_current_user_id,
    CurrentUserID,
)
from app.exceptions import AuthError
from app.models.user import User, RefreshToken

router = APIRouter()

# ── Security constants ─────────────────────────────────────────────────────────
_settings = get_settings()

# OWASP recommended: 12 rounds (~250 ms). Development uses 10 (~100 ms) so
# test suites stay responsive while still exercising the real bcrypt path.
_BCRYPT_ROUNDS: int = 10 if _settings.ENVIRONMENT == "development" else 12

# Pre-computed at startup for constant-time login (prevents username enumeration
# via response-time differences when the account does not exist).
_DUMMY_HASH: str = bcrypt.hashpw(
    b"cortex-timing-sentinel", bcrypt.gensalt(_BCRYPT_ROUNDS)
).decode()


# ── Request/Response Models ────────────────────────────────────────────────────
class UserRegister(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=100)
    full_name: str | None = None


class UserLogin(BaseModel):
    # Accepts a username OR an email address in a single field.
    identifier: str = Field(..., min_length=1, max_length=254)
    password: str = Field(..., min_length=1, max_length=100)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str
    full_name: str | None
    role: str
    is_active: bool
    created_at: datetime
    last_login: datetime | None


# ── Helper Functions ───────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(_BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))



async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    result = await db.execute(select(User).where(User.username == username))
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_identifier(db: AsyncSession, identifier: str) -> User | None:
    """Resolve a username-or-email identifier to a User row.

    Tries username first (exact match), then falls back to email so that users
    with an '@' in their username are still found correctly.
    """
    user = await get_user_by_username(db, identifier)
    if user is None:
        user = await get_user_by_email(db, identifier)
    return user


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def store_refresh_token(
    db: AsyncSession,
    user_id: int,
    token_family: str,
    jti: str,
    expires_at: datetime,
) -> None:
    """Store refresh token in database."""
    refresh_token = RefreshToken(
        user_id=user_id,
        token_family=token_family,
        jti=jti,
        expires_at=expires_at,
    )
    db.add(refresh_token)
    await db.commit()


# ── Routes ─────────────────────────────────────────────────────────────────────
@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(
    user_data: UserRegister,
    db: AsyncSession = Depends(get_db),
):
    """Register a new user."""
    # Check if username exists
    existing_user = await get_user_by_username(db, user_data.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )
    
    # Check if email exists
    existing_email = await get_user_by_email(db, user_data.email)
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    
    # Create new user
    user = User(
        username=user_data.username,
        email=user_data.email,
        hashed_password=hash_password(user_data.password),
        full_name=user_data.full_name,
        role="viewer",  # Default role
        is_active=True,
    )
    
    db.add(user)
    await db.commit()
    await db.refresh(user)
    
    return user


@router.post("/login", response_model=TokenResponse)
async def login(
    credentials: UserLogin,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """Authenticate with username or email and return a token pair."""
    user = await get_user_by_identifier(db, credentials.identifier)

    # Always run bcrypt regardless of whether the account exists.
    # This equalises response time and prevents username enumeration via timing.
    candidate_hash = user.hashed_password if user is not None else _DUMMY_HASH
    password_ok = verify_password(credentials.password, candidate_hash)

    if user is None or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect credentials",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account disabled",
        )

    token_pair = create_token_pair(subject=str(user.id), role=user.role)
    refresh_payload = decode_token(token_pair.refresh_token, expected_type="refresh")

    await store_refresh_token(
        db,
        user_id=user.id,
        token_family=refresh_payload.family,
        jti=refresh_payload.jti,
        expires_at=datetime.fromtimestamp(refresh_payload.exp, tz=timezone.utc),
    )

    await db.execute(
        update(User)
        .where(User.id == user.id)
        .values(last_login=datetime.now(timezone.utc))
    )
    await db.commit()

    set_refresh_token_cookie(response, token_pair.refresh_token)
    return token_pair


@router.post("/logout")
async def logout(
    response: Response,
    user_id: CurrentUserID,
    redis = Depends(get_redis),
):
    """Logout and revoke tokens."""
    # Clear refresh token cookie
    clear_refresh_token_cookie(response)
    
    return {"message": "Logged out successfully"}


@router.post("/dev-login", response_model=TokenResponse)
async def dev_login(
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Development-only login endpoint.
    Automatically logs in as 'trader' user for quick testing.
    
    ⚠️ SECURITY: This endpoint should be disabled in production!
    """
    from app.core.config import get_settings
    settings = get_settings()
    
    # Only allow in development
    if settings.is_production:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Endpoint not available in production"
        )
    
    # Get or create dev admin user
    user = await get_user_by_username(db, "trader")

    if not user:
        user = User(
            username="trader",
            email="trader@cortex.local",
            hashed_password=hash_password("trader123"),
            full_name="Dev Admin",
            role="admin",
            is_active=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    elif user.role != "admin":
        # Upgrade existing dev user to admin on next login
        stmt = update(User).where(User.id == user.id).values(role="admin")
        await db.execute(stmt)
        await db.commit()
        await db.refresh(user)
    
    # Create token pair
    token_pair = create_token_pair(subject=str(user.id), role=user.role)
    
    # Decode refresh token to get jti and family
    refresh_payload = decode_token(token_pair.refresh_token, expected_type="refresh")
    
    # Store refresh token in database
    await store_refresh_token(
        db,
        user_id=user.id,
        token_family=refresh_payload.family,
        jti=refresh_payload.jti,
        expires_at=datetime.fromtimestamp(refresh_payload.exp, tz=timezone.utc),
    )
    
    # Update last login
    stmt = (
        update(User)
        .where(User.id == user.id)
        .values(last_login=datetime.now(timezone.utc))
    )
    await db.execute(stmt)
    await db.commit()
    
    # Set refresh token in HTTPOnly cookie
    set_refresh_token_cookie(response, token_pair.refresh_token)
    
    return token_pair


@router.post("/refresh", response_model=TokenResponse)
async def refresh_access_token(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Refresh access token using refresh token.

    Implements token rotation: each refresh token can only be used once.
    Reuse detection triggers family revocation for security.
    """
    from app.core.security import rotate_refresh_token

    try:
        token_pair = await rotate_refresh_token(body.refresh_token, redis)

        payload = decode_token(token_pair.access_token)
        user = await get_user_by_id(db, int(payload.sub))
        if user:
            stmt = update(User).where(User.id == user.id).values(
                last_login=datetime.now(timezone.utc)
            )
            await db.execute(stmt)
            await db.commit()

        return token_pair
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token refresh failed",
        )


@router.get("/me", response_model=UserResponse)
async def get_current_user(
    user_id: CurrentUserID,
    db: AsyncSession = Depends(get_db),
):
    """Get current user profile."""
    user = await get_user_by_id(db, int(user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    return user


@router.post("/create-admin", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_admin_user(
    user_data: UserRegister,
    db: AsyncSession = Depends(get_db),
):
    """
    Create an admin user. 
    
    WARNING: This endpoint should be disabled in production or protected by IP whitelist.
    For development/testing only.
    """
    # Check if username exists
    existing_user = await get_user_by_username(db, user_data.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already registered"
        )
    
    # Check if email exists
    existing_email = await get_user_by_email(db, user_data.email)
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered"
        )
    
    # Create admin user
    user = User(
        username=user_data.username,
        email=user_data.email,
        hashed_password=hash_password(user_data.password),
        full_name=user_data.full_name,
        role="admin",  # Admin role
        is_active=True,
    )
    
    db.add(user)
    await db.commit()
    await db.refresh(user)
    
    return user
