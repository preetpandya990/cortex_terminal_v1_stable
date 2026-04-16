# PATH: backend/app/exceptions.py
# ─────────────────────────
"""
Cortex AI — Exception Hierarchy
================================
Typed exceptions eliminate bare `except Exception` and ensure consistent
HTTP error shapes across the entire API surface via FastAPI exception handlers.
"""
from __future__ import annotations


class CortexBaseError(Exception):
    """Root exception for all domain errors."""

    default_message: str = "An unexpected error occurred"
    status_code: int = 500

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message
        super().__init__(self.message)


# ── Authentication / Authorisation ─────────────────────────────────────────────
class AuthError(CortexBaseError):
    default_message = "Authentication failed"
    status_code = 401


class ForbiddenError(CortexBaseError):
    default_message = "You do not have permission to perform this action"
    status_code = 403


class InvalidTokenError(AuthError):
    default_message = "Token is invalid or expired"


class OAuthStateError(AuthError):
    default_message = "OAuth state parameter mismatch — possible CSRF attempt"


# ── Data / Domain ───────────────────────────────────────────────────────────────
class DataNotFoundError(CortexBaseError):
    default_message = "The requested resource was not found"
    status_code = 404


class DuplicateResourceError(CortexBaseError):
    default_message = "A resource with this identifier already exists"
    status_code = 409


# ── Upstream / External ─────────────────────────────────────────────────────────
class UpstoxAPIError(CortexBaseError):
    """Raised when the Upstox upstream API returns a non-2xx response."""

    default_message = "Upstream market data API error"
    status_code = 502

    def __init__(self, message: str | None = None, upstream_status: int | None = None) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status


class UpstoxConnectionError(UpstoxAPIError):
    default_message = "Failed to connect to market data feed"
    status_code = 503


class MarketDataUnavailableError(CortexBaseError):
    default_message = "Market data is temporarily unavailable"
    status_code = 503


# ── Validation ─────────────────────────────────────────────────────────────────
class ValidationError(CortexBaseError):
    default_message = "Request validation failed"
    status_code = 422


# ── Infrastructure ─────────────────────────────────────────────────────────────
class CacheError(CortexBaseError):
    default_message = "Cache operation failed"
    status_code = 500


class DatabaseError(CortexBaseError):
    default_message = "Database operation failed"
    status_code = 500