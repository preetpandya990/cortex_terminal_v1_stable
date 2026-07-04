"""
Cortex AI — Worker Sidecar Application
=======================================
Internal-only FastAPI service that hosts all 13 supervised background tasks
and exposes a control-plane HTTP API on port 8001.

The worker runs as a separate Docker Compose service on the cortex-network
internal bridge.  It is never exposed to the public internet — the main API
communicates with it via httpx over the internal Docker network.

Command:
    uvicorn app.worker_app:app \\
        --host 0.0.0.0 --port 8001 --workers 1 --loop uvloop --log-level info

Design:
    - ``--workers 1`` is mandatory: multiple Uvicorn workers would each spawn
      their own copy of all 15 task loops, causing duplicate processing.
    - The TaskGroup runs as a background asyncio.Task so that the lifespan
      ``yield`` (and therefore the control-plane HTTP routes) is available
      immediately after startup — not blocked by task execution.
    - Uvicorn owns SIGTERM/SIGINT handling.  On signal receipt uvicorn
      triggers lifespan teardown, which sets shutdown_event so all 13 tasks
      exit cooperatively before the process terminates.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware

from app.core.config import get_settings
from app.core.logging_config import setup_logging
from app.workers.registry import TASK_NAMES, build_task_registry
from app.workers.supervisor import create_task_states, supervised

settings = get_settings()

# ── Logging — identical call to main.py so both services share one config ─────
setup_logging(
    log_level=settings.LOG_LEVEL,
    environment=settings.ENVIRONMENT,
    explanation_log_enabled=settings.EXPLANATION_PIPELINE_LOG_ENABLED,
    explanation_log_path=settings.EXPLANATION_PIPELINE_LOG_PATH,
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Worker startup: initialise resources, build task registry, launch TaskGroup.
    Worker shutdown: signal tasks, await clean exit, dispose resources.
    """
    logger.info("Cortex worker sidecar starting [env=%s]", settings.ENVIRONMENT)

    # Prometheus metrics — same init call as main.py
    from app.core.metrics import init_metrics
    init_metrics(settings.APP_VERSION, settings.ENVIRONMENT)
    logger.info("Prometheus metrics initialized")

    # Cooperative shutdown event.  Observed by all 13 tasks; set in the
    # lifespan teardown block (after yield) which uvicorn triggers on
    # SIGTERM / SIGINT via its own signal handlers.  Do NOT register custom
    # signal handlers here — that would overwrite uvicorn's handlers and
    # prevent the server from initiating its own shutdown sequence.
    shutdown_event: asyncio.Event = asyncio.Event()

    # Resource initialisation: DB engine (dedicated worker pool), Redis, ML
    # models, and Upstox client.  worker_lifespan() owns cleanup in its finally.
    from app.worker import worker_lifespan
    async with worker_lifespan() as (session_factory, redis_client, ml_components, upstox_client):

        # Per-task state objects — stored at app.state for control-plane access
        task_states = create_task_states(list(TASK_NAMES))
        app.state.task_states   = task_states
        app.state.shutdown_event = shutdown_event

        # Publish each task's expected work-cycle interval once at startup —
        # these are static for the lifetime of the process, so there's no
        # need to re-set them on every /metrics scrape. Tasks with no fixed
        # cadence (value is None in the table) are left unpublished: PromQL
        # `on(task)` joins against this gauge then naturally exclude them
        # from ratio-based staleness alerting rather than requiring
        # special-case exclusion logic downstream.
        from app.core.metrics import worker_task_expected_interval_seconds
        from app.workers.registry import TASK_EXPECTED_INTERVAL_SECONDS

        for _task_name, _interval in TASK_EXPECTED_INTERVAL_SECONDS.items():
            if _interval is not None:
                worker_task_expected_interval_seconds.labels(task=_task_name).set(_interval)

        # Stored for worker_ai_processing.py's demand-driven dispatch routes,
        # which need direct access to the forecast/classification queues'
        # Redis client and DB session factory (the sentiment queue is
        # in-process and needs neither).
        app.state.session_factory = session_factory
        app.state.redis_client     = redis_client

        # Closure-based task factories — produce a fresh coroutine on each call
        # so that the supervisor can restart a crashed task without holding stale
        # DB sessions or closed connections from a previous coroutine.
        task_registry = build_task_registry(
            session_factory=session_factory,
            redis_client=redis_client,
            ml_components=ml_components,
            upstox_client=upstox_client,
            shutdown=shutdown_event,
            task_states=task_states,
        )
        app.state.task_registry = task_registry

        logger.info("Launching %d supervised background tasks", len(task_registry))

        # ── Background TaskGroup ──────────────────────────────────────────────
        # The TaskGroup runs inside a separate asyncio.Task so that the lifespan
        # yield is reached immediately.  HTTP routes on the control plane are
        # live before any task loop has completed its first cycle.

        async def _run_task_group() -> None:
            try:
                async with asyncio.TaskGroup() as tg:
                    for name, factory in task_registry.items():
                        task = tg.create_task(
                            supervised(name, factory, task_states[name], shutdown_event),
                            name=f"worker:{name}",
                        )
                        task_states[name].task_handle = task
                logger.info("All supervised tasks exited cleanly")
            except* Exception as eg:
                # One or more tasks exhausted max_failures — this is a fatal
                # worker condition.  Log each exception then re-raise so that
                # the background task exits with an error, triggering Docker's
                # ``restart: unless-stopped`` policy.
                for exc in eg.exceptions:
                    logger.critical(
                        "Task exceeded max failures — triggering container restart: %s",
                        exc,
                        exc_info=exc,
                    )
                raise

        task_group_bg = asyncio.create_task(
            _run_task_group(), name="worker:task_group"
        )
        app.state.task_group = task_group_bg

        app.state.started_at = datetime.now(timezone.utc)
        logger.info(
            "Worker sidecar ready — %d tasks running, control plane active on :8001",
            len(task_registry),
        )

        yield  # ← control-plane HTTP API is live from this point

        # ── Graceful shutdown ─────────────────────────────────────────────────
        logger.info(
            "Worker sidecar shutting down — signalling %d tasks", len(task_registry)
        )
        shutdown_event.set()

        try:
            await asyncio.wait_for(
                task_group_bg,
                timeout=float(settings.WORKER_SHUTDOWN_TIMEOUT),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Task group did not finish within %ds timeout — force-cancelling",
                settings.WORKER_SHUTDOWN_TIMEOUT,
            )
            task_group_bg.cancel()
            try:
                await task_group_bg
            except (asyncio.CancelledError, BaseException):
                pass
        except (asyncio.CancelledError, BaseException):
            # ExceptionGroup from a fatal task crash or external cancellation —
            # already logged inside _run_task_group; swallow here so cleanup runs.
            pass

        logger.info("Worker sidecar shutdown complete")


# ── Application factory ────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title="Cortex Worker Sidecar",
        version=settings.APP_VERSION,
        docs_url=None,   # internal service — no public API docs
        redoc_url=None,
        lifespan=lifespan,
    )

    # RequestID middleware: injects X-Request-ID / X-Correlation-ID for
    # structured log correlation between the API and worker control-plane calls.
    from app.middleware.request_id import RequestIDMiddleware
    app.add_middleware(RequestIDMiddleware)

    # GZip: Prometheus /metrics payloads can be large (all registered metrics).
    # minimum_size=1024 skips tiny responses like /health that don't benefit.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # Control-plane routes: /health, /metrics, /tasks, /tasks/{name}/*
    from app.api.worker_control import router as control_router
    app.include_router(control_router)

    # Demand-driven AI processing routes: /ai-processing/status, /ai-processing/*/dispatch
    from app.api.worker_ai_processing import router as ai_processing_router
    app.include_router(ai_processing_router)

    return app


app = create_app()
