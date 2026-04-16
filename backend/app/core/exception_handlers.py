# PATH: backend/app/core/exception_handlers.py
# ──────────────────────────────────────
"""
Cortex AI — Exception Handlers
================================
Centralised FastAPI exception handlers ensure:
  - Consistent JSON error shape across all error types
  - Zero internal detail exposure in production (stack traces, conn strings, etc.)
  - Correlation IDs in every error response for log correlation
  - Full exception context logged server-side for debugging
"""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from app.exceptions import (
    AuthError,
    CortexBaseError,
    DataNotFoundError,
    ForbiddenError,
    UpstoxAPIError,
)

logger = logging.getLogger(__name__)


def _error_response(
    request: Request,
    status_code: int,
    message: str,
    error_code: str | None = None,
) -> JSONResponse:
    correlation_id = str(uuid.uuid4())
    logger.error(
        "Request error [%s] %s %s → %d: %s",
        correlation_id,
        request.method,
        request.url.path,
        status_code,
        message,
    )
    body = {
        "error": {
            "message": message,
            "status_code": status_code,
            "correlation_id": correlation_id,
        }
    }
    if error_code:
        body["error"]["code"] = error_code
    return JSONResponse(status_code=status_code, content=body)


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the FastAPI app instance."""

    @app.exception_handler(CortexBaseError)
    async def cortex_error_handler(request: Request, exc: CortexBaseError) -> JSONResponse:
        logger.error(
            "Domain error [%s %s]: %s",
            request.method,
            request.url.path,
            exc.message,
            exc_info=exc,
        )
        return _error_response(request, exc.status_code, exc.message)

    @app.exception_handler(AuthError)
    async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
        return _error_response(
            request,
            exc.status_code,
            exc.message,
            "AUTHENTICATION_ERROR",
        )

    @app.exception_handler(DataNotFoundError)
    async def not_found_handler(request: Request, exc: DataNotFoundError) -> JSONResponse:
        return _error_response(request, 404, exc.message, "NOT_FOUND")

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Surface field-level errors to the client (safe — pydantic-generated)
        errors = [
            {"field": ".".join(str(l) for l in e["loc"]), "message": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": {"message": "Validation failed", "errors": errors}},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """
        Catch-all for any unhandled exception.
        Logs full traceback server-side, returns generic message to client.
        """
        correlation_id = str(uuid.uuid4())
        logger.exception(
            "Unhandled exception [%s] %s %s",
            correlation_id,
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "message": "An internal error occurred. "
                    "Please try again or contact support.",
                    "correlation_id": correlation_id,
                }
            },
        )