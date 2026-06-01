"""
Request ID Middleware
=====================
Generates/propagates request IDs and correlation IDs for distributed tracing.
Extracts user_id from JWT token for contextual logging.
"""
import time
import uuid
import logging
from typing import Callable, Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.request_context import set_request_context, clear_request_context

logger = logging.getLogger(__name__)

# Header names
REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware to generate/propagate request IDs and correlation IDs.
    
    Features:
    - Generates UUID v4 for each request (or reuses from header)
    - Propagates X-Request-ID and X-Correlation-ID headers
    - Extracts user_id from JWT token (if authenticated)
    - Stores context in contextvars for async-safe access
    - Logs request completion with full context
    """
    
    def __init__(self, app: ASGIApp):
        super().__init__(app)
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and inject context."""
        # Generate or reuse request ID
        request_id = request.headers.get(REQUEST_ID_HEADER, str(uuid.uuid4()))
        
        # Generate or reuse correlation ID (for multi-service tracing)
        correlation_id = request.headers.get(CORRELATION_ID_HEADER, str(uuid.uuid4()))
        
        # Extract user_id from JWT token (if authenticated)
        user_id = await self._extract_user_id(request)
        
        # Set request context (async-safe via contextvars)
        set_request_context(
            request_id=request_id,
            correlation_id=correlation_id,
            user_id=user_id,
            endpoint=request.url.path,
            method=request.method,
        )
        
        # Time the request
        start_time = time.time()
        
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            # Log failed requests
            duration_ms = (time.time() - start_time) * 1000
            logger.error(
                "Request failed",
                extra={
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "user_id": user_id,
                    "endpoint": request.url.path,
                    "method": request.method,
                    "duration_ms": round(duration_ms, 2),
                    "error": str(e),
                },
                exc_info=True,
            )
            clear_request_context()
            raise
        
        # Calculate duration
        duration_ms = (time.time() - start_time) * 1000
        
        # Add headers to response
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        
        # Log request completion
        logger.info(
            "Request completed",
            extra={
                "request_id": request_id,
                "correlation_id": correlation_id,
                "user_id": user_id,
                "endpoint": request.url.path,
                "method": request.method,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 2),
            },
        )
        
        # Clear context after request
        clear_request_context()
        
        return response
    
    async def _extract_user_id(self, request: Request) -> Optional[str]:
        """
        Extract user_id from JWT token in Authorization header.
        
        Returns:
            User ID string or None if not authenticated
        """
        try:
            auth_header = request.headers.get("Authorization")
            if not auth_header or not auth_header.startswith("Bearer "):
                return None
            
            token = auth_header.split(" ")[1]
            
            # Decode JWT token (without verification, just for logging)
            # We trust the auth middleware to verify the token
            import jwt
            payload = jwt.decode(token, options={"verify_signature": False})
            
            # Extract user_id (adjust field name based on your JWT structure)
            user_id = payload.get("sub") or payload.get("user_id")
            return str(user_id) if user_id else None
            
        except Exception:
            # Silently fail - don't break requests due to logging issues
            return None
