"""
ML API Authentication Utilities
================================
Helper functions for JWT authentication, audit logging, and rate limiting
for ML prediction endpoints.

Requirements: 18.1, 18.5, 8.3
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ml_data import MLAuditLog, MLPrediction

logger = logging.getLogger(__name__)


async def log_auth_failure(
    db: AsyncSession,
    request: Request,
    error_message: str,
    user_id: Optional[str] = None,
) -> None:
    """
    Log authentication failure to audit logs.
    
    Requirements: 18.5
    
    Args:
        db: Database session
        request: FastAPI request object
        error_message: Error message describing the failure
        user_id: User ID if available (may be None for invalid tokens)
    """
    try:
        audit_log = MLAuditLog(
            timestamp=datetime.now(timezone.utc),
            event_type="auth_failure",
            user_id=user_id,
            endpoint=str(request.url.path),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_data={
                "method": request.method,
                "query_params": dict(request.query_params),
            },
            response_status=401,
            error_message=error_message,
        )
        db.add(audit_log)
        await db.commit()
        logger.warning(
            f"Auth failure logged: endpoint={request.url.path}, "
            f"ip={request.client.host if request.client else 'unknown'}, "
            f"error={error_message}"
        )
    except Exception as exc:
        logger.error(f"Failed to log auth failure: {exc}", exc_info=True)
        # Don't raise - logging failure shouldn't break the request


async def log_auth_success(
    db: AsyncSession,
    request: Request,
    user_id: str,
    prediction_id: Optional[int] = None,
) -> None:
    """
    Log successful authentication and prediction request.
    
    Requirements: 18.5, 8.3
    
    Args:
        db: Database session
        request: FastAPI request object
        user_id: Authenticated user ID
        prediction_id: ID of the prediction if created
    """
    try:
        audit_log = MLAuditLog(
            timestamp=datetime.now(timezone.utc),
            event_type="prediction_request",
            user_id=user_id,
            endpoint=str(request.url.path),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_data={
                "method": request.method,
                "query_params": dict(request.query_params),
            },
            response_status=200,
            prediction_id=prediction_id,
        )
        db.add(audit_log)
        await db.commit()
    except Exception as exc:
        logger.error(f"Failed to log auth success: {exc}", exc_info=True)


async def get_user_prediction_count(
    db: AsyncSession,
    user_id: str,
    time_window_minutes: int = 60,
) -> int:
    """
    Get the number of predictions made by a user in the specified time window.
    
    Used for per-user rate limiting.
    
    Requirements: 8.3
    
    Args:
        db: Database session
        user_id: User ID to check
        time_window_minutes: Time window in minutes (default: 60)
        
    Returns:
        Number of predictions made by the user in the time window
    """
    try:
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=time_window_minutes)
        
        stmt = (
            select(func.count(MLPrediction.id))
            .where(
                MLPrediction.user_id == user_id,
                MLPrediction.timestamp >= cutoff_time,
            )
        )
        
        result = await db.execute(stmt)
        count = result.scalar_one()
        
        return count
    except Exception as exc:
        logger.error(f"Failed to get user prediction count: {exc}", exc_info=True)
        return 0  # Return 0 on error to avoid blocking legitimate requests


async def check_user_rate_limit(
    db: AsyncSession,
    user_id: str,
    limit: int = 100,
    time_window_minutes: int = 60,
) -> tuple[bool, int]:
    """
    Check if user has exceeded their rate limit.
    
    Requirements: 8.3
    
    Args:
        db: Database session
        user_id: User ID to check
        limit: Maximum number of predictions allowed
        time_window_minutes: Time window in minutes
        
    Returns:
        Tuple of (is_allowed, current_count)
    """
    count = await get_user_prediction_count(db, user_id, time_window_minutes)
    is_allowed = count < limit
    
    if not is_allowed:
        logger.warning(
            f"User {user_id} exceeded rate limit: {count}/{limit} "
            f"predictions in {time_window_minutes} minutes"
        )
    
    return is_allowed, count
