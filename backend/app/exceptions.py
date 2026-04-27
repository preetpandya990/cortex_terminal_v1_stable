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


class UpstoxRateLimitError(UpstoxAPIError):
    """
    Raised when Upstox / Cloudflare returns HTTP 429 (rate limited).

    This is a transient, self-correcting condition — back off and retry.
    It must NOT count toward the circuit breaker threshold, which is reserved
    for signals that the upstream service itself is unavailable.
    """
    default_message = "Upstox API rate limit exceeded — backing off"
    status_code = 429


class UpstoxInvalidInstrumentError(UpstoxAPIError):
    """
    Raised when Upstox rejects an instrument key as invalid (HTTP 400 UDAPI100011).

    This is a permanent, non-transient error — the instrument is delisted, renamed,
    or never existed in the historical candle API.  It must NOT count toward the
    circuit breaker threshold, which is reserved for signals that the API itself
    is degraded.
    """
    default_message = "Instrument key not recognised by Upstox API"
    status_code = 400


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