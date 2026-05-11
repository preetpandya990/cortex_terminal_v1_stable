# PATH: backend/app/api/v1/users.py
"""
User Profile & Preferences — API Router
=========================================
Endpoints for the authenticated user's profile and trading preferences.

Routes (prefix: /api/v1/users)
---------------------------------
  GET    /me/profile        Current user profile fields
  PUT    /me/profile        Update name, email, password
  GET    /me/preferences    Current strategy enforcement preferences
  PUT    /me/preferences    Update preferences
"""
# NOTE: do NOT add `from __future__ import annotations` here — FastAPI needs
# runtime-resolved type annotations for its dependency injection system.

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.limiter import limiter
from app.core.security import CurrentUserID
from app.schemas.strategies import (
    UpdateUserPreferencesRequest,
    UpdateUserProfileRequest,
    UserPreferencesResponse,
    UserProfileResponse,
)
from app.services.user_preferences_service import (
    get_or_create_preferences,
    get_user_profile,
    update_preferences,
    update_user_profile,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/me/profile",
    response_model=UserProfileResponse,
    summary="Get the authenticated user's profile",
)
@limiter.limit("60/minute")
async def get_profile(
    request: Request,
    user_id: CurrentUserID,
    db: AsyncSession = Depends(get_db),
) -> UserProfileResponse:
    user = await get_user_profile(int(user_id), db)
    return UserProfileResponse.model_validate(user)


@router.put(
    "/me/profile",
    response_model=UserProfileResponse,
    summary="Update profile fields (name, email, password)",
)
@limiter.limit("10/minute")
async def update_profile(
    request: Request,
    body: UpdateUserProfileRequest,
    user_id: CurrentUserID,
    db: AsyncSession = Depends(get_db),
) -> UserProfileResponse:
    user = await update_user_profile(int(user_id), body, db)
    await db.commit()
    return UserProfileResponse.model_validate(user)


@router.get(
    "/me/preferences",
    response_model=UserPreferencesResponse,
    summary="Get the authenticated user's strategy enforcement preferences",
)
@limiter.limit("60/minute")
async def get_preferences(
    request: Request,
    user_id: CurrentUserID,
    db: AsyncSession = Depends(get_db),
) -> UserPreferencesResponse:
    prefs = await get_or_create_preferences(int(user_id), db)
    return UserPreferencesResponse.model_validate(prefs)


@router.put(
    "/me/preferences",
    response_model=UserPreferencesResponse,
    summary="Update strategy enforcement preferences",
)
@limiter.limit("20/minute")
async def update_preferences_endpoint(
    request: Request,
    body: UpdateUserPreferencesRequest,
    user_id: CurrentUserID,
    db: AsyncSession = Depends(get_db),
) -> UserPreferencesResponse:
    prefs = await update_preferences(int(user_id), body, db)
    await db.commit()
    return UserPreferencesResponse.model_validate(prefs)
