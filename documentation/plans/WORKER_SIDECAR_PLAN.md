# Cortex Worker Sidecar — Implementation Plan

## Architecture After This Change

```
Docker Compose (cortex-network):
  cortex-api   :8000  ─── HTTP control plane ──▶  cortex-worker :8001 (internal only)
       │                  (httpx + circuit breaker)        │
       └─────────────────── Redis pub/sub (data flow) ─────┘
                            (unchanged, no new latency)
```

The worker becomes its own Docker Compose service running a FastAPI app with `--workers 1 --loop uvloop`. The main API gains an `httpx` client with a circuit breaker to call the worker's control plane. Redis pub/sub handles all data flow — unchanged.

---

## Scope

13 tasks total under supervision after this plan:
- 11 original worker tasks (from `worker.py`)
- `pnl_worker` and `sl_tp_worker` migrated from `main.py`

---

## New Files

| File | Purpose |
|---|---|
| `backend/app/worker_app.py` | FastAPI app for the worker sidecar — lifespan, TaskGroup, routes |
| `backend/app/workers/supervisor.py` | `PauseToken`, `TriggerToken`, `TaskState`, `supervised()` wrapper |
| `backend/app/workers/registry.py` | `TASK_DEFINITIONS` dict — all 13 tasks, bound at startup |
| `backend/app/api/worker_control.py` | Control-plane routes mounted on worker FastAPI app |
| `backend/app/core/worker_client.py` | `WorkerClient` — httpx + aiocircuitbreaker, fail-open |
| `backend/app/api/v1/admin_worker.py` | Admin proxy routes on main API → worker sidecar |

## Modified Files

| File | Change |
|---|---|
| `backend/app/worker.py` | Becomes pure task coroutines module — remove `main()`, orchestration moves to `worker_app.py`. Add `PauseToken` / `TriggerToken` safepoints to every loop |
| `backend/app/main.py` | Remove `pnl_worker_task`, `sl_tp_worker_task`. Add `WorkerClient` init. Update `/health/ready` to include worker status (fail-open) |
| `backend/app/core/config.py` | Add `WORKER_BASE_URL`, `INTERNAL_API_SECRET`, `WORKER_HTTP_TIMEOUT` |
| `backend/requirements.txt` | Add `uvloop>=0.21`, `aiocircuitbreaker>=1.0` |
| `docker-compose.yml` | Add `worker` service, update `api` env, remove `ollama` dependency from `api` (stale — Gemini branch) |
| `backend/.env.example` | Add `WORKER_BASE_URL`, `INTERNAL_API_SECRET` |

---

## Phase 1 — Docker Compose Worker Service

`docker-compose.yml` changes:
- Add `worker` service: same Dockerfile, `expose: ["8001"]` (never `ports:`), same `env_file: .env`, same `cortex-network`
- `depends_on: db (healthy), redis (healthy)` — no Ollama dependency (Gemini branch)
- Healthcheck: `curl http://localhost:8001/health` every 10s
- Command: `uvicorn app.worker_app:app --host 0.0.0.0 --port 8001 --workers 1 --loop uvloop`
- Add `WORKER_BASE_URL: http://worker:8001` to `api` service environment
- `ml_models` volume mounted on worker (same model files as API)
- `api` service: add `depends_on: worker (healthy)` so API only starts after worker is ready

> **Note:** `ollama` service can be removed or kept. The `api` service currently `depends_on: ollama` — this must be removed since we're on Gemini. The `ollama` service itself can stay as optional/commented.

---

## Phase 2 — Supervisor Infrastructure (`backend/app/workers/supervisor.py`)

**`PauseToken`** — `asyncio.Event`-based cooperative pause:
```python
class PauseToken:
    # Starts running; checkpoint() blocks only when paused
    async def checkpoint(self): await self._event.wait()
    def pause(self): self._event.clear()
    def resume(self): self._event.set()
```

**`TriggerToken`** — replaces `asyncio.wait_for(shutdown_event.wait(), timeout=N)` in every task's sleep:
```python
class TriggerToken:
    async def wait_or_timeout(self, secs: float):
        # Wakes immediately on trigger, or after secs — whichever first
```

**`TaskState`** dataclass: `status`, `last_run_at`, `crash_count`, `pause_token`, `trigger_token`, `task_handle`

**`supervised()` wrapper** — Python 3.11 pattern:
```python
async def supervised(name, coro_factory, state: TaskState, shutdown: asyncio.Event):
    while not shutdown.is_set():
        try:
            await coro_factory()
            state.crash_count = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.crash_count += 1
            delay = min(60.0, 2 ** state.crash_count)
            logger.error(f"{name} crashed #{state.crash_count}: {exc}; retry in {delay:.1f}s")
            if state.crash_count >= 5:
                raise  # propagate → Docker restarts the whole worker process
            await asyncio.sleep(delay)
```

**`TaskGroup` usage in `worker_app.py` lifespan:**
```python
async with asyncio.TaskGroup() as tg:
    for name, factory in task_registry.items():
        tg.create_task(supervised(name, factory, state[name], shutdown_event))
```

If any task exceeds `max_failures`, the `ExceptionGroup` bubbles up, `worker_app.py` exits, and Docker `restart: unless-stopped` brings it back. This is intentional — a task crashing 5 times in a row means something fundamental is wrong.

---

## Phase 3 — Task Registry (`backend/app/workers/registry.py`)

Factories bound at startup with all dependencies injected via closure:

```python
def build_task_registry(session_factory, redis, ml_components, upstox) -> dict[str, Callable]:
    return {
        "rss_ingestion":        lambda: rss_ingestion_loop(session_factory),
        "event_processing":     lambda: event_processing_loop(session_factory, ml_components, redis._redis),
        "regime_detection":     lambda: regime_detection_loop(session_factory),
        "drift_detection":      lambda: drift_detection_loop(session_factory),
        "safety_monitoring":    lambda: safety_monitoring_loop(session_factory),
        "data_ingestion":       lambda: data_ingestion_loop(session_factory, upstox),
        "heartbeat":            lambda: heartbeat_loop(),
        "correlation_engine":   lambda: correlation_loop(session_factory, redis, ml_components, upstox),
        "suggestion_expiry":    lambda: expiry_loop(session_factory, redis),
        "cache_invalidation":   lambda: cache_invalidation_loop(redis),
        "fundamentals_refresh": lambda: fundamentals_scheduler.run(),
        "pnl_worker":           lambda: run_pnl_worker(redis._redis),      # migrated from main.py
        "sl_tp_worker":         lambda: run_sl_tp_worker(redis._redis),    # migrated from main.py
    }
```

---

## Phase 4 — Control-Plane API (`backend/app/api/worker_control.py`)

Mounted on the worker FastAPI app. All routes require `X-Internal-Token` header.

| Route | What it does |
|---|---|
| `GET /health` | Liveness — always fast, no external deps checked |
| `GET /tasks` | All 13 tasks: status, last_run_at, crash_count |
| `GET /tasks/{name}` | Single task detail |
| `POST /tasks/{name}/pause` | Sets `PauseToken` — task pauses at next safepoint |
| `POST /tasks/{name}/resume` | Clears `PauseToken` |
| `POST /tasks/{name}/trigger` | Fires `TriggerToken` — task wakes immediately from sleep |
| `POST /tasks/{name}/restart` | Cancels task handle; supervisor auto-restarts |
| `GET /metrics` | Prometheus scrape endpoint |

Per-task health in Redis:
- Each task writes `worker:task:{name}:heartbeat` (TTL 90s) after each successful cycle
- `/tasks` reads these + in-memory `TaskState`
- Prometheus gauges: `worker_task_last_cycle_timestamp{task}`, `worker_task_crash_count{task}`

---

## Phase 5 — CPU Isolation (Correlation Engine)

The correlation engine's scoring and aggregation over 2,551 instruments is CPU-bound and blocks the event loop during its 30s cycle, starving the control-plane HTTP server. Wrap only the CPU portion:

```python
# Before: sync scoring blocks the event loop
scores = compute_consensus_scores(anomalies, ml_signals)

# After: offloaded to thread pool (numpy releases GIL)
scores = await asyncio.to_thread(compute_consensus_scores, anomalies, ml_signals)
```

The async DB and Redis calls around it remain unchanged. This unblocks the `/health` and `/tasks` endpoints during a correlation cycle.

---

## Phase 6 — Worker Client on Main API (`backend/app/core/worker_client.py`)

```
httpx.AsyncClient
  → tenacity retry (2 attempts, 0.5s wait)
  → aiocircuitbreaker (5 failures → OPEN 30s)
  → fail-open: on CircuitBreakerError / RetryError / httpx.HTTPError → return None, never raise
```

- `base_url = settings.WORKER_BASE_URL` (from env: `http://worker:8001`)
- Timeout: `httpx.Timeout(connect=1.0, read=3.0)` — control calls must be fast
- Default header: `X-Internal-Token: {settings.INTERNAL_API_SECRET}`
- Singleton: initialized in `main.py` lifespan, stored at `app.state.worker_client`
- Shutdown: `await app.state.worker_client.aclose()`

---

## Phase 7 — Admin Routes on Main API (`backend/app/api/v1/admin_worker.py`)

Thin proxy — receives request, calls `WorkerClient`, returns response. Protected by existing admin auth (not the internal token — that is service-to-service only).

Routes: `GET / POST /api/v1/admin/worker/tasks/*` — proxied to worker sidecar.

Registered in `main.py`:
```python
app.include_router(
    admin_worker.router,
    prefix=f"{settings.API_V1_PREFIX}/admin/worker",
    tags=["Admin — Worker Control"],
)
```

---

## Phase 8 — Main API Updates (`backend/app/main.py`)

**Remove:**
- `pnl_worker_task` creation + shutdown block
- `sl_tp_worker_task` creation + shutdown block

**Add:**
- `WorkerClient` init in lifespan startup
- Worker status in `/health/ready` (fail-open: if worker unreachable, readiness still passes but logs a warning — the API must not fail-ready because the worker is temporarily restarting)

---

## Phase 9 — Config & Security (`backend/app/core/config.py`)

New settings:
```python
WORKER_BASE_URL: str = Field("http://worker:8001")
INTERNAL_API_SECRET: str = Field(...)   # required, no default — must be set in .env
WORKER_HTTP_TIMEOUT: float = Field(3.0)
```

`.env.example` addition:
```bash
WORKER_BASE_URL=http://worker:8001
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
INTERNAL_API_SECRET=<32-byte-hex>
```

---

## Phase 10 — Requirements

`backend/requirements.txt` additions:
```
uvloop>=0.21.0
aiocircuitbreaker>=1.0.0
```

(`httpx` and `tenacity` are already present.)

---

## Prometheus Scrape Config Update

Add worker scrape job to `prometheus.yml`:
```yaml
- job_name: cortex_worker
  static_configs:
    - targets: ['worker:8001']
  metrics_path: /metrics
```

---

## What Does Not Change

- Redis pub/sub data-flow channels — completely untouched
- All 11 task coroutine implementations — only the orchestration layer changes
- ML model loading — worker still loads its own model independently
- `signal_scheduler`, `rag_corpus_refresh`, `instrument_sync_service`, `explanation_worker`, `cai_redis_listener`, `suggestions_redis_listener` — stay in `main.py` (API-coupled, not moved)

---

## Implementation Order

1. `supervisor.py` + `registry.py` — pure Python, no side effects, testable in isolation
2. `worker_app.py` + `worker_control.py` — new FastAPI app, bring up on port 8001
3. `docker-compose.yml` — add worker service, verify network isolation
4. `main.py` — remove `pnl_worker` / `sl_tp_worker`, add `WorkerClient`
5. `worker_client.py` + `admin_worker.py` — control-plane integration
6. CPU isolation — `asyncio.to_thread` on correlation engine
7. Per-task Redis heartbeats + Prometheus metrics
8. Config + security (`INTERNAL_API_SECRET`)

---

## Design Decisions & Rationale

| Decision | Choice | Reason |
|---|---|---|
| Worker Uvicorn workers | `--workers 1` always | Multiple workers = duplicated task loops. Control plane has negligible concurrency. |
| Worker port on host | `expose:` only — never `ports:` | Network isolation; worker is not a public service |
| Pause/resume primitive | `asyncio.Event` (safepoint pattern) | Canonical asyncio primitive; lighter than `asyncio.Condition` |
| Circuit breaker library | `aiocircuitbreaker` + `tenacity` | Complementary: tenacity retries transient failures, circuit breaker stops hammering a sustained outage |
| Fail-open policy | Return `None` on worker unreachable | API must not fail-ready because the worker is temporarily restarting |
| CPU-bound offload | `asyncio.to_thread()` | Numpy releases the GIL — ThreadPoolExecutor is sufficient; ProcessPoolExecutor would add pickling overhead without benefit |
| Service-to-service auth | Static shared secret in `X-Internal-Token` header | No JWT overhead needed for internal Docker network calls; network isolation is the primary defence |
| HTTP client | `httpx.AsyncClient` with explicit timeouts | Native async, connection pooling, granular timeout control (connect/read/write) |
| Event loop | `uvloop` | ~20–30% event loop throughput improvement at zero cost on Linux |
| Task max failures before propagating | 5 | Allows transient recovery; 5 consecutive crashes indicates a hard failure warranting a full process restart |

---

*Plan authored: 2026-06-13*
*Branch target: feat/gemini-ai-service (or a new branch cut from it)*
