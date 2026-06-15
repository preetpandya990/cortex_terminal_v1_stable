"""
Cortex AI — Worker Sidecar HTTP Client
=======================================
Fail-open async HTTP client for service-to-service calls from the main API
to the worker sidecar's control-plane (port 8001, internal Docker network).

Fail-open contract:
    ALL public methods return None on failure — they never raise.  The main
    API must never fail readiness checks or block user traffic because the
    worker is temporarily restarting.  Callers check for None and degrade
    gracefully (e.g. return 503 with ``{"degraded": true}``).

Resilience stack (innermost → outermost):
    1. httpx.AsyncClient   — connection-pooled, granular timeouts
    2. tenacity retry      — 2 attempts, 0.5 s wait on transient httpx errors
                             (TCP reset, connection refused during restart)
    3. aiobreaker          — 5 consecutive failures → OPEN for 30 s
                             (prevents hammering a sustained worker outage)

Usage:
    # Initialise once in main.py lifespan
    worker_client = WorkerClient()
    app.state.worker_client = worker_client

    # Teardown
    await app.state.worker_client.aclose()
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import httpx
from aiobreaker import CircuitBreaker, CircuitBreakerError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class WorkerClient:
    """
    Async HTTP client for the worker sidecar control-plane.

    Thread-safety: this class is not thread-safe; it is designed to be used
    exclusively in the async context of the main API's event loop.
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.WORKER_BASE_URL,
            # Granular timeout: fast connect (worker is on the same Docker
            # network), configurable read (control-plane calls must be fast).
            timeout=httpx.Timeout(
                connect=1.0,
                read=settings.WORKER_HTTP_TIMEOUT,
                write=1.0,
                pool=1.0,
            ),
            # Pre-set auth header on every request — eliminates per-call overhead
            # and ensures no request is accidentally sent without it.
            headers={"X-Internal-Token": settings.INTERNAL_API_SECRET},
        )
        # Consistent with core/circuit_breaker.py — reuses the same aiobreaker
        # library already present in the codebase.
        self._breaker = CircuitBreaker(
            fail_max=5,
            timeout_duration=timedelta(seconds=30),
            name="worker_sidecar",
        )

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """
        Execute a control-plane request through the retry + circuit-breaker stack.

        Returns:
            Parsed JSON dict on success, None on any failure (fail-open).
        """
        # Inner function captures method/path/kwargs in a closure so the
        # retry decorator can call it without arguments.
        async def _do_request() -> httpx.Response:
            return await self._client.request(method, path, **kwargs)

        # tenacity wraps _do_request: 2 attempts total, 0.5 s fixed wait,
        # retries on transient httpx.HTTPError (network errors, not 4xx/5xx).
        # reraise=True: re-raises the last exception rather than wrapping it in
        # RetryError, so the circuit breaker sees the original httpx.HTTPError.
        _do_request_with_retry = retry(
            retry=retry_if_exception_type(httpx.HTTPError),
            stop=stop_after_attempt(2),
            wait=wait_fixed(0.5),
            reraise=True,
        )(_do_request)

        try:
            response = await self._breaker.call_async(_do_request_with_retry)
        except CircuitBreakerError:
            logger.warning(
                "WorkerClient: circuit OPEN — skipping %s %s (worker likely restarting)",
                method, path,
            )
            return None
        except httpx.HTTPError as exc:
            logger.warning(
                "WorkerClient request failed (fail-open): %s %s — %s", method, path, exc
            )
            return None
        except Exception as exc:
            logger.warning(
                "WorkerClient unexpected error (fail-open): %s %s — %s", method, path, exc
            )
            return None

        try:
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "WorkerClient: HTTP %d from %s %s",
                exc.response.status_code, method, path,
            )
            return None
        except Exception as exc:
            logger.warning(
                "WorkerClient response parse error (fail-open): %s %s — %s",
                method, path, exc,
            )
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    async def get_health(self) -> dict[str, Any] | None:
        """Check worker liveness. Returns None if unreachable."""
        return await self._request("GET", "/health")

    async def get_tasks(self) -> dict[str, Any] | None:
        """Return all task states. Returns None if unreachable."""
        return await self._request("GET", "/tasks")

    async def get_task(self, name: str) -> dict[str, Any] | None:
        """Return state for a single task. Returns None if unreachable or task not found."""
        return await self._request("GET", f"/tasks/{name}")

    async def pause_task(self, name: str) -> dict[str, Any] | None:
        """Pause a task at its next cooperative checkpoint."""
        return await self._request("POST", f"/tasks/{name}/pause")

    async def resume_task(self, name: str) -> dict[str, Any] | None:
        """Resume a paused task."""
        return await self._request("POST", f"/tasks/{name}/resume")

    async def trigger_task(self, name: str) -> dict[str, Any] | None:
        """Wake a task immediately from its sleep interval."""
        return await self._request("POST", f"/tasks/{name}/trigger")

    async def restart_task(self, name: str) -> dict[str, Any] | None:
        """Cancel and restart a task via the supervisor."""
        return await self._request("POST", f"/tasks/{name}/restart")

    async def aclose(self) -> None:
        """Close the underlying httpx connection pool."""
        await self._client.aclose()
