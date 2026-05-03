"""
Admin — User Management Routes
================================
CRUD operations for platform user accounts.  All endpoints require the
admin role, which is verified directly from the JWT claim (no extra DB
round-trip) via the AdminUserID dependency.
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.v1.auth import UserResponse, hash_password
from app.core.auth import AdminUserID
from app.core.redis import get_redis
from app.core.security import revoke_token_family
from app.models.user import RefreshToken, User

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Request / Response schemas ─────────────────────────────────────────────────

class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_-]+$")
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=100)
    full_name: str | None = Field(None, max_length=100)
    role: Literal["viewer", "trader", "admin"] = "viewer"


class UpdateUserRequest(BaseModel):
    role: Literal["viewer", "trader", "admin"] | None = None
    is_active: bool | None = None


class UserListResponse(BaseModel):
    users: list[UserResponse]
    total: int


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _active_admin_count(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count(User.id)).where(
            User.role == "admin", User.is_active.is_(True)
        )
    )
    return result.scalar_one()


async def _revoke_all_sessions(user: User, db: AsyncSession, redis: object) -> None:
    """Revoke every active refresh-token family for *user* in Redis.

    Prevents the user from obtaining new access tokens immediately.
    Any in-flight access tokens expire naturally within ACCESS_TOKEN_EXPIRE_MINUTES.
    """
    result = await db.execute(
        select(RefreshToken.token_family)
        .where(
            RefreshToken.user_id == user.id,
            RefreshToken.is_revoked.is_(False),
        )
        .distinct()
    )
    families = result.scalars().all()
    for family in families:
        await revoke_token_family(family, redis)  # type: ignore[arg-type]
    if families:
        logger.info(
            "Revoked %d session family(ies) for user_id=%s username=%s",
            len(families), user.id, user.username,
        )


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/users", response_model=UserListResponse)
async def list_users(
    admin_id: AdminUserID,
    db: AsyncSession = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=500),
    search: str | None = Query(None, max_length=100),
) -> UserListResponse:
    """List all users, with optional username / email substring search."""
    base = select(User)
    count_base = select(func.count(User.id))

    if search:
        pattern = f"%{search.strip()}%"
        condition = or_(User.username.ilike(pattern), User.email.ilike(pattern))
        base = base.where(condition)
        count_base = count_base.where(condition)

    total = (await db.execute(count_base)).scalar_one()
    users = (
        await db.execute(base.order_by(User.created_at.asc()).offset(skip).limit(limit))
    ).scalars().all()

    return UserListResponse(users=list(users), total=total)


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest,
    admin_id: AdminUserID,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Create a new user with an explicit role.  Admin-only."""
    if (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")

    if (
        await db.execute(select(User).where(User.email == body.email))
    ).scalar_one_or_none():
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        role=body.role,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info(
        "Admin user_id=%s created user %s (role=%s)", admin_id, user.username, user.role
    )
    return user


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    body: UpdateUserRequest,
    admin_id: AdminUserID,
    db: AsyncSession = Depends(get_db),
    redis: object = Depends(get_redis),
) -> User:
    """Update a user's role and/or active status.

    Safety guards:
    - You cannot change your own role or deactivate yourself.
    - You cannot demote the last active admin.
    Sessions are revoked immediately when a user is deactivated or downgraded.
    """
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    is_self = admin_id == str(user_id)

    if is_self:
        if body.role is not None and body.role != user.role:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "You cannot change your own role"
            )
        if body.is_active is False:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "You cannot deactivate your own account"
            )

    if body.role is not None and body.role != "admin" and user.role == "admin":
        if await _active_admin_count(db) <= 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Cannot demote the last active admin — promote another user first",
            )

    role_changed = body.role is not None and body.role != user.role
    deactivated = body.is_active is False and user.is_active is True

    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active

    await db.commit()
    await db.refresh(user)

    if role_changed or deactivated:
        await _revoke_all_sessions(user, db, redis)

    logger.info(
        "Admin user_id=%s updated user %s — role_changed=%s deactivated=%s",
        admin_id, user.username, role_changed, deactivated,
    )
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_user(
    user_id: int,
    admin_id: AdminUserID,
    db: AsyncSession = Depends(get_db),
    redis: object = Depends(get_redis),
) -> Response:
    """Permanently delete a user and all associated data (cascade).

    Safety guards:
    - You cannot delete your own account.
    - You cannot delete the last active admin.
    """
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if admin_id == str(user_id):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "You cannot delete your own account"
        )

    if user.role == "admin" and await _active_admin_count(db) <= 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot delete the last active admin — promote another user first",
        )

    await _revoke_all_sessions(user, db, redis)
    await db.delete(user)
    await db.commit()
    logger.info(
        "Admin user_id=%s permanently deleted user %s (id=%s)", admin_id, user.username, user_id
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
