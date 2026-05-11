# PATH: backend/app/services/user_preferences_service.py
"""
User Preferences & Profile Service
=====================================
Database operations for UserPreferences and User profile updates.

Public API:
  get_or_create_preferences(user_id, db)   → UserPreferences (upserts defaults)
  update_preferences(user_id, req, db)     → UserPreferences
  get_user_profile(user_id, db)            → User
  update_user_profile(user_id, req, db)    → User
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password, verify_password
from app.exceptions import (
    DataNotFoundError,
    DuplicateResourceError,
    IncorrectPasswordError,
)
from app.models.strategies import UserPreferences
from app.models.user import User
from app.schemas.strategies import UpdateUserPreferencesRequest, UpdateUserProfileRequest

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# UserPreferences
# ──────────────────────────────────────────────────────────────────────────────

async def get_or_create_preferences(
    user_id: int,
    db: AsyncSession,
) -> UserPreferences:
    """
    Return the user's preferences row, creating one with defaults if absent.
    Safe to call on every authenticated request — the INSERT is skipped on
    conflict (via the unique constraint on user_id).
    """
    result = await db.execute(
        select(UserPreferences).where(UserPreferences.user_id == user_id)
    )
    prefs = result.scalar_one_or_none()
    if prefs is not None:
        return prefs

    prefs = UserPreferences(user_id=user_id)
    db.add(prefs)
    await db.flush()
    await db.refresh(prefs)
    logger.info("Created default preferences for user %d", user_id)
    return prefs


async def update_preferences(
    user_id: int,
    req: UpdateUserPreferencesRequest,
    db: AsyncSession,
) -> UserPreferences:
    prefs = await get_or_create_preferences(user_id, db)

    if req.enforcement_mode is not None:
        prefs.enforcement_mode = req.enforcement_mode.value
    if req.hard_gate_notify is not None:
        prefs.hard_gate_notify = req.hard_gate_notify
    if req.default_position_sizing_mode is not None:
        prefs.default_position_sizing_mode = req.default_position_sizing_mode.value
    if req.default_risk_per_trade_pct is not None:
        from decimal import Decimal
        prefs.default_risk_per_trade_pct = Decimal(str(req.default_risk_per_trade_pct))
    if req.max_concurrent_activations is not None:
        prefs.max_concurrent_activations = req.max_concurrent_activations

    await db.flush()
    await db.refresh(prefs)
    logger.info("Updated preferences for user %d", user_id)
    return prefs


# ──────────────────────────────────────────────────────────────────────────────
# User Profile
# ──────────────────────────────────────────────────────────────────────────────

async def get_user_profile(user_id: int, db: AsyncSession) -> User:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise DataNotFoundError("User not found")
    return user


async def update_user_profile(
    user_id: int,
    req: UpdateUserProfileRequest,
    db: AsyncSession,
) -> User:
    user = await get_user_profile(user_id, db)

    if req.full_name is not None:
        user.full_name = req.full_name.strip()

    if req.email is not None and req.email != user.email:
        # Ensure the new email is not already taken
        dup = await db.execute(
            select(User).where(User.email == req.email, User.id != user_id)
        )
        if dup.scalar_one_or_none():
            raise DuplicateResourceError("An account with this email address already exists.")
        user.email = req.email.lower().strip()

    if req.new_password:
        # current_password is validated as required by the schema when new_password present
        if not verify_password(req.current_password, user.hashed_password):  # type: ignore[arg-type]
            raise IncorrectPasswordError()
        user.hashed_password = hash_password(req.new_password)
        logger.info("Password changed for user %d", user_id)

    await db.flush()
    await db.refresh(user)
    return user
