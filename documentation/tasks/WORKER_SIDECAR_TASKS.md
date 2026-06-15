# Cortex Worker Sidecar — Implementation Task List

> Branch: `feat/gemini-ai-service`
> Authored: 2026-06-13
> Decisions: Circuit breaker → `aiobreaker` (standardize on existing). Ollama → remove entirely.

---

## Decisions Confirmed

- **Circuit breaker**: Standardize on `aiobreaker` (already used in `core/circuit_breaker.py`; fix the missing requirements.txt entry; use same library for `WorkerClient`)
- **Ollama**: Remove entirely from `docker-compose.yml`

---

## Session Progress

| Session | Date | Status |
|---|---|---|
| Session 1 | 2026-06-13 | ✅ COMPLETE — P-0, P-1, Tasks 1.1–4.3 + sl_tp_worker pool fix |
| Session 2 | 2026-06-13 | ✅ COMPLETE — Tasks 5.1–13.x |

---

## Critical Pre-work (Blockers — do these first)

### ✅ P-0 | Create `backend/Dockerfile`

DONE. Multi-stage (builder + runtime), `python:3.11-slim` (Debian Bookworm), non-root user `cortex:1001`, venv at `/venv`, PyTorch CPU-only pre-installed before requirements.txt to prevent CUDA 13 wheel, TA-Lib via `libta-lib-dev` (apt, not source build), spaCy model downloaded at build time, `curl` in runtime stage for healthcheck probes.

### ✅ P-1 | Fix `aiobreaker` missing from `backend/requirements.txt`

DONE. Added `aiobreaker>=1.3.0`.

---

## Phase 1 — Requirements

### ✅ Task 1.1 | `backend/requirements.txt`

DONE.
- Added `aiobreaker>=1.3.0` under "Rate Limiting & Resilience" section
- Added `uvloop>=0.21.0` under "Web Framework" section

---

## Phase 2 — Config & Security

### ✅ Task 2.1 | `backend/app/core/config.py`

DONE. Added under new `# ── Worker Sidecar ──` section after the existing Worker block:
```python
WORKER_BASE_URL: str = Field("http://worker:8001")
INTERNAL_API_SECRET: str = Field(..., min_length=64)
WORKER_HTTP_TIMEOUT: float = Field(3.0, ge=0.5, le=30.0)
```
Added `@field_validator("INTERNAL_API_SECRET")` → `internal_secret_must_not_be_placeholder` that rejects placeholders starting with `<` or `REPLACE` and a forbidden-set check.

### ✅ Task 2.2 | `backend/.env.example`

DONE. Added `WORKER_BASE_URL` and `INTERNAL_API_SECRET` entries with generation instructions.

---

## Phase 3 — Refactor `worker.py` → Pure Task Module

### ✅ Task 3.1 | `backend/app/worker.py`

DONE. (Note: worker.py was NOT deployed in docker-compose — risk was lower than the task file indicated.)

**Removed:**
- `shutdown_event = asyncio.Event()` global
- `signal_handler()` and signal registration
- `import signal`
- `async def main()` function
- `if __name__ == "__main__":` block

**Kept:** `worker_lifespan()` context manager — unchanged.

**Modified all 4 native loops** (`heartbeat_loop`, `cache_invalidation_loop`, `expiry_loop`, `correlation_loop`):
- Added `pause: PauseToken, trigger: TriggerToken, shutdown: asyncio.Event` params
- Replaced `while not shutdown_event.is_set():` → `while not shutdown.is_set():`
- Added `await pause.checkpoint()` as first statement of each loop iteration
- Replaced `asyncio.wait_for(shutdown_event.wait(), timeout=N) / except TimeoutError: continue` → `await trigger.wait_or_timeout(N)` (never raises; returns bool)
- `cache_invalidation_loop`: event-driven (async for), added `if shutdown.is_set(): break` + `await pause.checkpoint()` inside message handler

Added import: `from app.workers.supervisor import PauseToken, TriggerToken`

Updated module docstring to describe its new role as a pure task-coroutine module.

---

## Phase 4 — Supervisor Infrastructure

### ✅ Task 4.1 | Create `backend/app/workers/__init__.py`

DONE. Package init with brief docstring.

### ✅ Task 4.2 | Create `backend/app/workers/supervisor.py`

DONE. Full implementation:

- **`PauseToken`** — `asyncio.Event`-backed gate, starts SET (running). `checkpoint()` / `pause()` / `resume()` / `is_paused` property.
- **`TriggerToken`** — `asyncio.Event`-backed early wakeup. `wait_or_timeout(secs)` uses `asyncio.wait_for(event.wait(), timeout=secs)` with auto-reset on trigger. Returns `True` (triggered) or `False` (timeout). `fire()` sets event.
- **`TaskStatus`** — `Literal["starting", "running", "paused", "crashed", "stopped"]`
- **`TaskState`** — `@dataclass` with `name`, `status`, `last_run_at`, `crash_count`, `pause_token`, `trigger_token`, `task_handle`. All tokens created via `field(default_factory=...)`.
- **`create_task_states(names)`** — dict comprehension; one fresh `TaskState` per name.
- **`supervised(name, coro_factory, state, shutdown, *, max_failures=5)`** — restart loop with exponential back-off (`min(60.0, 2.0 ** crash_count)`). Respects shutdown during back-off sleep. Propagates after `max_failures` consecutive crashes. `CancelledError` always re-raised.

### ✅ Task 4.3 | Create `backend/app/workers/registry.py`

DONE. `build_task_registry()` returns all 13 task factories. All imports are lazy (inside the function) to keep module-level import time fast and avoid circular deps. `FundamentalsRefreshScheduler` instantiated once inside the builder with the `shutdown` event. Registry/TASK_NAMES integrity check raises `RuntimeError` on mismatch at build time. `TASK_NAMES` tuple exported for use by `worker_app.py`.

**sl_tp_worker pool fix** (noted in Open Design Notes #1): Instead of injecting a session factory parameter, `sl_tp_worker._process_tick` was updated to import `WorkerSessionLocal` instead of `AsyncSessionLocal` — simpler and equally correct since `WorkerSessionLocal` is the dedicated background-task pool already defined in `database.py`.

---

## Phase 5 — Worker FastAPI App

### ✅ Task 5.1 | Create `backend/app/worker_app.py`

Full FastAPI app for the worker sidecar:
- `docs_url=None, redoc_url=None` — internal service, no docs exposure
- `lifespan()` context manager:
  1. Setup logging (same `setup_logging()` call as `main.py`)
  2. Init Prometheus metrics (`init_metrics()`)
  3. Register `SIGTERM`/`SIGINT` handlers → set `shutdown_event`
  4. Enter `worker_lifespan()` to get `(session_factory, redis_client, ml_components, upstox_client)`
  5. Create `task_states = create_task_states(list(TASK_NAMES))`
  6. Create `task_registry = build_task_registry(...)`
  7. Store both at `app.state.task_states`, `app.state.task_registry`, `app.state.shutdown_event`
  8. Start all 13 tasks via `asyncio.TaskGroup` inside a background `asyncio.Task` (so lifespan `yield` is not blocked):
     ```python
     async with asyncio.TaskGroup() as tg:
         for name, factory in task_registry.items():
             task = tg.create_task(supervised(name, factory, task_states[name], shutdown_event))
             task_states[name].task_handle = task
     ```
  9. On shutdown: `shutdown_event.set()`, await task group completion, exit `worker_lifespan()`
- Include `worker_control.router`
- Add `GZipMiddleware` for metrics endpoint compression
- Add `RequestIDMiddleware` for structured logging correlation

---

## Phase 6 — Control-Plane API

### ✅ Task 6.1 | Create `backend/app/api/worker_control.py`

Routes mounted directly on the worker FastAPI app:

| Route | Auth | Action |
|---|---|---|
| `GET /health` | none | Liveness — always 200 if process alive |
| `GET /tasks` | internal token | Return all 13 `TaskState` dicts + Redis heartbeat TTLs |
| `GET /tasks/{name}` | internal token | Single task detail |
| `POST /tasks/{name}/pause` | internal token | `state.pause_token.pause()` |
| `POST /tasks/{name}/resume` | internal token | `state.pause_token.resume()` |
| `POST /tasks/{name}/trigger` | internal token | `state.trigger_token.fire()` |
| `POST /tasks/{name}/restart` | internal token | `task_handle.cancel()` — supervised() auto-restarts |
| `GET /metrics` | none | Prometheus scrape |

**Auth dependency**: Extract `X-Internal-Token` header, compare with `secrets.compare_digest(token, settings.INTERNAL_API_SECRET)` — constant-time to prevent timing attacks. Return `403` on mismatch.

**Per-task Redis heartbeats**: Each task writes `worker:task:{name}:heartbeat` (TTL 90s) after each successful cycle. The `GET /tasks` route reads these TTLs to report `last_heartbeat_at`.

**Prometheus metrics to register:**
- `worker_task_last_cycle_seconds{task}` (Gauge — Unix timestamp of last cycle)
- `worker_task_crash_count{task}` (Gauge — current crash_count from TaskState)
- `worker_task_status{task}` (Gauge — 0=running, 1=paused, 2=crashed, 3=stopped)

---

## Phase 7 — CPU Isolation (Correlation Engine)

### ✅ Task 7.1 | `backend/app/worker.py` — `correlation_loop()`

The `engine.on_scanner_anomaly()` and scoring steps inside `EventCorrelationEngine` iterate over 2,551 instruments. The numpy/pandas scoring work releases the GIL and is safe for a thread pool.

Inspect `backend/app/ai/correlation/engine.py` — identify the synchronous scoring step (likely inside `on_scanner_anomaly` or a scoring subroutine). Wrap with:
```python
result = await asyncio.to_thread(synchronous_scoring_fn, *args)
```
The surrounding async DB and Redis calls remain in the event loop. This unblocks `/health` and control-plane routes during the 30s correlation cycle.

---

## Phase 8 — Worker Client on Main API

### ✅ Task 8.1 | Create `backend/app/core/worker_client.py`

```python
class WorkerClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.WORKER_BASE_URL,
            timeout=httpx.Timeout(connect=1.0, read=settings.WORKER_HTTP_TIMEOUT),
            headers={"X-Internal-Token": settings.INTERNAL_API_SECRET},
        )
        self._breaker = CircuitBreaker(
            fail_max=5,
            reset_timeout=timedelta(seconds=30),
        )  # aiobreaker.CircuitBreaker — consistent with core/circuit_breaker.py

    async def _request(self, method: str, path: str, **kwargs) -> dict | None:
        try:
            response = await self._breaker.call_async(
                self._client.request, method, path, **kwargs
            )
            response.raise_for_status()
            return response.json()
        except (CircuitBreakerError, httpx.HTTPError, Exception):
            logger.warning("WorkerClient request failed (fail-open): %s %s", method, path)
            return None

    async def aclose(self) -> None:
        await self._client.aclose()
```

Retry logic: Use `tenacity.retry` with `stop=stop_after_attempt(2)`, `wait=wait_fixed(0.5)`, `retry=retry_if_exception_type(httpx.HTTPError)` wrapping `_client.request` **before** the circuit breaker sees it. Two transient failures → circuit breaker counts as one failure.

**All methods return `None` on failure — never raise.** This is the fail-open contract.

---

## Phase 9 — Admin Proxy Routes

### ✅ Task 9.1 | Create `backend/app/api/v1/admin_worker.py`

Thin proxy. Does not validate or transform worker responses. Protected by existing `require_admin` dependency (same as `admin_training.py`, `admin_strategies.py`).

```
GET  /api/v1/admin/worker/tasks        → WorkerClient.get_tasks()
GET  /api/v1/admin/worker/tasks/{name} → WorkerClient.get_task(name)
POST /api/v1/admin/worker/tasks/{name}/pause
POST /api/v1/admin/worker/tasks/{name}/resume
POST /api/v1/admin/worker/tasks/{name}/trigger
POST /api/v1/admin/worker/tasks/{name}/restart
GET  /api/v1/admin/worker/health       → WorkerClient.get_health()
```

If `WorkerClient` returns `None` → return `JSONResponse({"detail": "worker_unavailable", "degraded": true}, status_code=503)`.

---

## Phase 10 — Main API Updates

### ✅ Task 10.1 | `backend/app/main.py`

**Remove (6 blocks total):**
- `run_pnl_worker` import + task creation + `app.state.pnl_worker_task` assignment + shutdown cancel block
- `run_sl_tp_worker` import + task creation + `app.state.sl_tp_worker_task` assignment + shutdown cancel block

**Add:**
```python
# Startup
from app.core.worker_client import WorkerClient
worker_client = WorkerClient()
app.state.worker_client = worker_client

# Shutdown
await app.state.worker_client.aclose()
```

Update `/health/ready` to include worker status (fail-open):
```python
worker_healthy, worker_details = await check_worker(app.state.worker_client, timeout=1.0)
result.add_check("worker", worker_healthy, worker_details, critical=False)
# critical=False → API stays ready even if worker is temporarily restarting
```
Add `check_worker()` to `core/health_checks.py`.

### ✅ Task 10.2 | Register `admin_worker` router in `main.py`

```python
from app.api.v1 import admin_worker
app.include_router(
    admin_worker.router,
    prefix=f"{settings.API_V1_PREFIX}/admin/worker",
    tags=["Admin — Worker Control"],
)
```

---

## Phase 11 — Docker Compose Restructure

### ✅ Task 11.1 | `docker-compose.yml`

**Remove:**
- Entire `ollama` service block
- `ollama_data` from volumes section
- `ollama` from `api.depends_on`
- `OLLAMA_BASE_URL` from `api.environment`

**Add `worker` service:**
```yaml
worker:
  build:
    context: ./backend
    dockerfile: Dockerfile
  container_name: cortex-worker
  restart: unless-stopped
  env_file:
    - .env
  environment:
    DATABASE_URL: "postgresql+asyncpg://${POSTGRES_USER:-cortex}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB:-cortex_db}"
    REDIS_URL: "redis://redis:6379/0"
  expose:
    - "8001"           # internal only — never ports:
  volumes:
    - ml_models:/app/models
    - ./.cache:/app/.cache
  depends_on:
    db:
      condition: service_healthy
    redis:
      condition: service_healthy
  healthcheck:
    test: ["CMD", "curl", "-f", "http://localhost:8001/health"]
    interval: 10s
    timeout: 5s
    retries: 3
    start_period: 60s   # ML model loading takes time
  command: >
    uvicorn app.worker_app:app
      --host 0.0.0.0
      --port 8001
      --workers 1
      --loop uvloop
      --log-level info
  networks:
    - cortex-network
```

**Update `api` service:**
```yaml
depends_on:
  db:
    condition: service_healthy
  redis:
    condition: service_healthy
  worker:
    condition: service_healthy
environment:
  WORKER_BASE_URL: "http://worker:8001"
  # OLLAMA_BASE_URL removed
```

---

## Phase 12 — Prometheus

### ✅ Task 12.1 | `prometheus.yml`

```yaml
- job_name: cortex_worker
  static_configs:
    - targets: ['worker:8001']
  metrics_path: /metrics
  scrape_interval: 10s
```

---

## Phase 13 — Tests

### ✅ Task 13.1 | `backend/tests/workers/test_supervisor.py`

- `test_pause_token_blocks_and_resumes()`
- `test_trigger_token_wakes_before_timeout()`
- `test_trigger_token_falls_back_to_timeout()`
- `test_supervised_retries_with_backoff()` — crash N<5 times, verify state.crash_count and delay
- `test_supervised_propagates_at_max_failures()` — crash 5 times, verify exception propagates
- `test_supervised_resets_crash_count_on_success()` — crash → recover → crash_count reset to 0

### ✅ Task 13.2 | `backend/tests/workers/test_registry.py`

- `test_registry_builds_all_13_tasks()`
- `test_registry_factories_are_callable()`
- `test_pnl_worker_in_registry()`
- `test_sl_tp_worker_in_registry()`

### ✅ Task 13.3 | `backend/tests/api/test_worker_control.py`

- `test_health_no_auth_required()` — `GET /health` → 200 without token
- `test_tasks_requires_auth()` — `GET /tasks` without token → 403
- `test_tasks_with_valid_auth()` — `GET /tasks` with token → 200, all 13 names present
- `test_pause_resume_task()` — pause → state is "paused", resume → state is "running"

### ✅ Task 13.4 | `backend/tests/core/test_worker_client.py`

- `test_returns_none_on_http_error()` — `respx` mock raises `httpx.HTTPError` → returns `None`
- `test_returns_none_when_circuit_open()` — mock `CircuitBreakerError` → returns `None`
- `test_successful_request_returns_json()` — mock 200 → returns dict

---

## Dependency-Safe Execution Order

```
✅ P-0            → backend/Dockerfile
✅ P-1 + Task 1.1 → requirements.txt                   (aiobreaker + uvloop)
✅ Task 2.1+2.2   → config.py + .env.example
✅ Task 3.1       → worker.py refactor                  (pure task-coroutine module)
✅ Task 4.1       → workers/__init__.py
✅ Task 4.2       → workers/supervisor.py
✅ Task 4.3       → workers/registry.py
✅ Task 5.1       → worker_app.py                       (depends on registry.py + supervisor.py)
✅ Task 6.1       → api/worker_control.py               (depends on worker_app.py for state)
✅ Task 7.1       → CPU isolation in correlation_loop
✅ Task 8.1       → core/worker_client.py               (depends on config.py)
✅ Task 9.1       → api/v1/admin_worker.py              (depends on worker_client.py)
✅ Task 10.1+10.2 → main.py                             (depends on worker_client.py + admin_worker.py)
✅ Task 11.1      → docker-compose.yml                  (depends on worker_app.py + Dockerfile)
✅ Task 12.1      → prometheus.yml
✅ Task 13.x      → tests                               (run after each phase, not all at end)
```

---

## Open Design Notes

1. ~~**`sl_tp_worker.py` requires a modification**: It currently imports `AsyncSessionLocal` directly inside `_process_tick`.~~ **RESOLVED** — Changed import to `WorkerSessionLocal` directly (simpler than the originally-planned session factory parameter approach; `WorkerSessionLocal` is the correct dedicated background-task pool already in `database.py`).

2. **`pnl_worker` fire-and-forget tasks**: The P&L worker spawns `asyncio.create_task()` for ML feedback and post-close monitoring inside `_auto_close_position`. These tasks will run in the worker's event loop after migration — correct behavior, no change needed.

3. **ML model loading** in the worker starts cold. `start_period: 60s` in the worker healthcheck accounts for this. The API's `depends_on: worker: service_healthy` means the API only starts after the worker has loaded models.

4. **`INTERNAL_API_SECRET`** is a new required env var with no default. Both the API and worker will fail to start without it. Update `.env` on all environments before deploying.

5. **Pause/trigger for imported loops** (rss_ingestion, event_processing, regime_detection, drift_detection, safety_monitoring, data_ingestion): Phase 1 handles them via `CancelledError`. Adding full safepoints to those 6 external coroutines is a separate Phase 2 initiative.

6. **`worker.py` was NOT deployed** in the original docker-compose.yml — only `main.py` ran (with `pnl_worker` and `sl_tp_worker` tasks directly in its lifespan). The Task 3.1 refactor was therefore lower risk than originally assessed.
