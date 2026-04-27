"""
Request Context Storage
=======================
Thread-safe and async-safe storage for request-scoped data using contextvars.
"""
from contextvars import ContextVar
from typing import Optional

# Context variables for request-scoped data
_request_id: ContextVar[Optional[str]] = ContextVar('request_id', default=None)
_correlation_id: ContextVar[Optional[str]] = ContextVar('correlation_id', default=None)
_user_id: ContextVar[Optional[str]] = ContextVar('user_id', default=None)
_endpoint: ContextVar[Optional[str]] = ContextVar('endpoint', default=None)
_method: ContextVar[Optional[str]] = ContextVar('method', default=None)


def set_request_context(
    request_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    user_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    method: Optional[str] = None,
) -> None:
    """Set request context variables."""
    if request_id is not None:
        _request_id.set(request_id)
    if correlation_id is not None:
        _correlation_id.set(correlation_id)
    if user_id is not None:
        _user_id.set(user_id)
    if endpoint is not None:
        _endpoint.set(endpoint)
    if method is not None:
        _method.set(method)


def get_request_context() -> dict[str, Optional[str]]:
    """Get all request context variables as a dict."""
    return {
        'request_id': _request_id.get(),
        'correlation_id': _correlation_id.get(),
        'user_id': _user_id.get(),
        'endpoint': _endpoint.get(),
        'method': _method.get(),
    }


def get_request_id() -> Optional[str]:
    """Get current request ID."""
    return _request_id.get()


def get_correlation_id() -> Optional[str]:
    """Get current correlation ID."""
    return _correlation_id.get()


def get_user_id() -> Optional[str]:
    """Get current user ID."""
    return _user_id.get()


def clear_request_context() -> None:
    """Clear all request context variables."""
    _request_id.set(None)
    _correlation_id.set(None)
    _user_id.set(None)
    _endpoint.set(None)
    _method.set(None)
