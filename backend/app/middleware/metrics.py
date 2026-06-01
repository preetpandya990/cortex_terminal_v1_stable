"""
Metrics Middleware for Request Tracking
========================================
Automatically tracks all HTTP requests with Prometheus metrics.
"""
import time
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.metrics import (
    http_requests_total,
    http_request_duration_seconds,
    http_requests_in_progress,
)


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Middleware to collect Prometheus metrics for all HTTP requests.
    
    Tracks:
    - Request count by method, endpoint, status
    - Request duration histogram
    - Requests in progress gauge
    """
    
    def __init__(self, app: ASGIApp):
        super().__init__(app)
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and collect metrics."""
        # Extract endpoint path (remove query params)
        path = request.url.path
        method = request.method
        
        # Normalize path (replace IDs with placeholders)
        normalized_path = self._normalize_path(path)
        
        # Track in-progress requests
        http_requests_in_progress.labels(method=method, endpoint=normalized_path).inc()
        
        # Time the request
        start_time = time.time()
        
        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            # Track failed requests
            status_code = 500
            http_requests_total.labels(
                method=method,
                endpoint=normalized_path,
                status=status_code
            ).inc()
            http_requests_in_progress.labels(method=method, endpoint=normalized_path).dec()
            raise
        
        # Calculate duration
        duration = time.time() - start_time
        
        # Record metrics
        http_requests_total.labels(
            method=method,
            endpoint=normalized_path,
            status=status_code
        ).inc()
        
        http_request_duration_seconds.labels(
            method=method,
            endpoint=normalized_path
        ).observe(duration)
        
        http_requests_in_progress.labels(method=method, endpoint=normalized_path).dec()
        
        return response
    
    def _normalize_path(self, path: str) -> str:
        """
        Normalize path by replacing IDs with placeholders.
        
        Examples:
            /api/v1/ml/predict/123 -> /api/v1/ml/predict/{id}
            /api/v1/users/abc-def -> /api/v1/users/{id}
        """
        parts = path.split('/')
        normalized = []
        
        for part in parts:
            # Check if part looks like an ID (numeric or UUID-like)
            if part.isdigit() or (len(part) > 8 and '-' in part):
                normalized.append('{id}')
            else:
                normalized.append(part)
        
        return '/'.join(normalized)
