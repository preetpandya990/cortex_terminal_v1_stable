"""
Cortex AI — ML Rate Limiter
=============================
Hybrid rate limiting for ML endpoints using both IP address and user_id.

Strategy:
- Primary limit: Per-user rate limit (prevents abuse by authenticated users)
- Secondary limit: Per-IP rate limit (prevents unauthenticated abuse)
- Uses Redis for distributed rate limiting
- Logs violations to audit logs for security monitoring

Requirements: 18.2, 32.1, 32.2
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from fastapi import Request, HTTPException, status, Depends
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)
settings = get_settings()


class MLRateLimiter:
    """
    Hybrid rate limiter for ML endpoints (IP + user_id).
    
    Implements sliding window rate limiting with Redis backend.
    """
    
    def __init__(self, redis: Redis):
        self.redis = redis
    
    async def check_rate_limit(
        self,
        user_id: str,
        ip_address: str,
        endpoint: str,
        limit: str,  # Format: "10/minute" or "5/minute"
    ) -> tuple[bool, dict]:
        """
        Check if request is within rate limits.
        
        Args:
            user_id: Authenticated user ID
            ip_address: Client IP address
            endpoint: API endpoint being accessed
            limit: Rate limit string (e.g., "10/minute")
        
        Returns:
            Tuple of (is_allowed, metadata)
            metadata contains: remaining, reset_time, limit_type
        
        Raises:
            HTTPException: 429 if rate limit exceeded
        """
        # Parse limit string
        count, period = limit.split("/")
        count = int(count)
        window_seconds = self._parse_period(period)
        
        # Check user-based limit (primary)
        user_key = f"ml:ratelimit:user:{user_id}:{endpoint}"
        user_allowed, user_remaining = await self._check_limit(
            user_key, count, window_seconds
        )
        
        # Check IP-based limit (secondary, more lenient)
        ip_limit = count * 2  # IP limit is 2x user limit
        ip_key = f"ml:ratelimit:ip:{ip_address}:{endpoint}"
        ip_allowed, ip_remaining = await self._check_limit(
            ip_key, ip_limit, window_seconds
        )
        
        # Both must pass
        if not user_allowed:
            await self._log_violation(user_id, ip_address, endpoint, "user", count, window_seconds)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "rate_limit_exceeded",
                    "message": f"Rate limit exceeded: {limit} per user",
                    "limit": count,
                    "window": period,
                    "retry_after": window_seconds,
                },
                headers={"Retry-After": str(window_seconds)},
            )
        
        if not ip_allowed:
            await self._log_violation(user_id, ip_address, endpoint, "ip", ip_limit, window_seconds)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "rate_limit_exceeded",
                    "message": f"Rate limit exceeded: {ip_limit}/{period} per IP",
                    "limit": ip_limit,
                    "window": period,
                    "retry_after": window_seconds,
                },
                headers={"Retry-After": str(window_seconds)},
            )
        
        return True, {
            "remaining": min(user_remaining, ip_remaining),
            "reset_time": window_seconds,
            "limit_type": "hybrid",
        }
    
    async def _check_limit(
        self,
        key: str,
        limit: int,
        window_seconds: int,
    ) -> tuple[bool, int]:
        """
        Check rate limit using sliding window algorithm.
        
        Returns:
            Tuple of (is_allowed, remaining_requests)
        """
        now = datetime.now(timezone.utc).timestamp()
        window_start = now - window_seconds
        
        # Remove old entries outside window
        await self.redis.zremrangebyscore(key, 0, window_start)
        
        # Count requests in current window
        current_count = await self.redis.zcard(key)
        
        if current_count >= limit:
            return False, 0
        
        # Add current request
        await self.redis.zadd(key, {str(now): now})
        
        # Set expiry on key
        await self.redis.expire(key, window_seconds)
        
        remaining = limit - current_count - 1
        return True, remaining
    
    def _parse_period(self, period: str) -> int:
        """Convert period string to seconds."""
        if period == "second":
            return 1
        elif period == "minute":
            return 60
        elif period == "hour":
            return 3600
        elif period == "day":
            return 86400
        else:
            raise ValueError(f"Invalid period: {period}")
    
    async def _log_violation(
        self,
        user_id: str,
        ip_address: str,
        endpoint: str,
        limit_type: str,
        limit: int,
        window: int,
    ) -> None:
        """Log rate limit violation to audit logs."""
        logger.warning(
            "Rate limit exceeded: user=%s ip=%s endpoint=%s type=%s limit=%d/%ds",
            user_id,
            ip_address,
            endpoint,
            limit_type,
            limit,
            window,
        )
        
        # Store violation in Redis for monitoring
        violation_key = f"ml:ratelimit:violations:{user_id}"
        await self.redis.incr(violation_key)
        await self.redis.expire(violation_key, 3600)  # 1 hour TTL


# Dependency for FastAPI endpoints
async def get_ml_rate_limiter(redis: Redis = Depends(get_redis)) -> MLRateLimiter:
    """Factory function for ML rate limiter."""
    return MLRateLimiter(redis)


def rate_limit(limit: str):
    """
    Decorator for ML endpoints to apply hybrid rate limiting.
    
    Usage:
        @router.post("/predict")
        @rate_limit("10/minute")
        async def predict(request: Request, user_id: str = Depends(get_current_user_id)):
            ...
    
    Args:
        limit: Rate limit string (e.g., "10/minute", "5/minute")
    """
    async def dependency(
        request: Request,
        user_id: str,
        limiter: MLRateLimiter = None,
    ):
        if limiter is None:
            limiter = await get_ml_rate_limiter()
        
        # Get client IP
        ip_address = request.client.host if request.client else "unknown"
        
        # Check rate limit
        await limiter.check_rate_limit(
            user_id=user_id,
            ip_address=ip_address,
            endpoint=request.url.path,
            limit=limit,
        )
    
    return dependency
