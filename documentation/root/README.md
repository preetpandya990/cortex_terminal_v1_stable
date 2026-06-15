# Cortex AI

Production-grade ML-driven trading signal platform for the Indian equity
markets (NSE).  FastAPI backend, Next.js frontend, TimescaleDB for OHLCV +
ML metadata, Redis for cache/pub-sub, optional Ollama for local LLM, weekly
ML challenger retrain.

This README is a **migration / fresh-install guide**: it focuses on what an
operator must do to stand the system up on a new box without missing
load-bearing steps.  Feature-level documentation lives in `documentation/`
and `ML_IMPLEMENTATION_PLAN.md`.

---

## Migration checklist (read this first)

Use this as the punch-list when moving to a new machine.  Each box maps
to a section below.

```
SYSTEM
[ ] Linux/WSL2 host; Python 3.11.15; Node 20+; Docker + Compose (Recommended)
[ ] NVIDIA GPU + driver if running ML training locally (TF GPU requires CUDA ≤12.x)

REPO
[ ] git clone <this repo>
[ ] git lfs install   # only if any large binaries are tracked via LFS (this repo: none today)

SECRETS / ENV  (none of these come from git — `.env*` are .gitignored)
[ ] cp .env.example .env                 → fill POSTGRES_PASSWORD, SECRET_KEY,
                                           UPSTOX_API_KEY/SECRET, …
[ ] cp backend/.env.example backend/.env → same values, plus any backend-only overrides

INFRA
[ ] docker compose up -d db redis prometheus           # OR install Postgres 16 + TimescaleDB +
                                                        Redis 7 natively
[ ] (optional) Ollama for local LLM features

PYTHON BACKEND
[ ] cd backend && python3.11 -m venv .venv && source .venv/bin/activate
[ ] pip install -r requirements.txt
[ ] (only if you will train ML locally) pip install -r requirements-ml-training.txt
[ ] alembic upgrade head                  → expect head = 0039 (or later)

FRONTEND
[ ] cd frontend && npm ci && npm run build

TESTS  ←──  `backend/tests/` is .gitignored.  See §Tests for why + how to obtain.
[ ] If transferring from another box: rsync backend/tests/  (else they're not here)
[ ] pytest backend/tests/unit/ml/             → expect ≥256 passed

ML ARTIFACTS  ←──  Not in git.  Either transfer or regenerate.
[ ] rsync backend/models/production/ from old box   (preferred — preserves 1.0.0)
[ ] OR run the orchestrator: backend/scripts/production_training_orchestrator.py --fresh
                                              (multi-hour; GPU recommended)
[ ] scripts/promote_model.py status           → verify models registered

SCHEDULED SERVICES  (systemd; Linux only)
[ ] sudo cp deploy/cortex-retrain.{service,timer} /etc/systemd/system/
[ ] sudo cp backend/cortex-worker.service       /etc/systemd/system/
[ ] sudo systemctl daemon-reload && sudo systemctl enable --now cortex-worker.service \
                                                                cortex-retrain.timer

SMOKE
[ ] curl http://localhost:8000/health         → 200 OK
[ ] open http://localhost:3000                → frontend renders, no console errors
[ ] backend/scripts/preflight_check.py        → all green
[ ] backend/scripts/reeval_production_model.py → exit 0 (MEETS_BAR) OR 2 (DEMOTE_RECOMMENDED)
                                                — either is "the system is wired correctly"
```

If any box fails, the corresponding section below explains how to fix it.

---

## Architecture at a glance

```
┌─────────────┐     ┌──────────────────────┐     ┌─────────────────────┐
│  Next.js    │ ←→  │  FastAPI (uvicorn)   │ ←→  │  TimescaleDB (PG16) │
│  frontend   │ HTTP│  app.main:app        │ AOPG│  OHLCV + ML meta    │
│  :3000      │  WS │  :8000               │     │  :5432 / host :5433 │
└─────────────┘     └──────────┬───────────┘     └─────────────────────┘
                               │
              ┌────────────────┼────────────────┐
              ↓                ↓                ↓
       ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
       │  Worker      │ │  Redis 7     │ │  Ollama      │
       │  app.worker  │ │  cache +     │ │  local LLM   │
       │  (systemd)   │ │  pub/sub     │ │  (optional)  │
       └──────────────┘ └──────────────┘ └──────────────┘

       ┌────────────────────────────────────────────────────────┐
       │  Off-band: weekly ML challenger retrain                │
       │  systemd: cortex-retrain.timer  →  scheduled_retrain.py │
       │   →  production_training_orchestrator.py --fresh        │
       │   →  challenger run dir; NO auto-promote (human gate)  │
       └────────────────────────────────────────────────────────┘
```

Source-of-truth files:
- `docker-compose.yml` — local infra (TimescaleDB / Redis / Ollama / Prometheus / API / frontend)
- `backend/app/main.py` — FastAPI app entrypoint (`uvicorn app.main:app`)
- `backend/app/worker.py` — long-running background worker (`python -m app.worker`)
- `backend/alembic/versions/` — DB migrations (current head: `0039_limit_order_reserved_cash`)
- `deploy/` — systemd units for scheduled retrain (`cortex-retrain.{service,timer}` + `README.md`)
- `backend/cortex-worker.service` — systemd unit for the worker
- `graphify-out/GRAPH_REPORT.md` — auto-extracted cross-module knowledge graph (8000+ nodes)

---

## System requirements

| Need | Version / Notes |
|---|---|
| OS | Linux or WSL2 (Ubuntu-class).  Backend builds on macOS too but TF GPU + systemd are Linux-only. |
| Python | **3.11.15** (pinned — newer 3.11.x is OK; 3.12+ untested) |
| Node.js | 20+ (frontend uses Next.js 16 + React 19) |
| Docker | 24+ with Compose v2 (`docker compose`, not `docker-compose`) |
| RAM | 16 GB minimum (8 GB free for orchestrator).  Training peaks ~5 GB. |
| Disk | 30 GB free (Postgres data + ml artefacts + venv + node_modules) |
| NVIDIA GPU | Optional but **strongly recommended for training**.  TF 2.21 bundles CUDA 12.9 + cuDNN 9.  See §ML Environment Constraints. |

---

## Quick start — Docker (recommended)

This brings up Postgres + Redis + Ollama + Prometheus + the API + the
frontend in one command.  Easiest path for evaluation / staging.

```bash
git clone <repo-url> Cortex_Merge_AI-ML && cd Cortex_Merge_AI-ML

# 1. Secrets (root .env — Compose reads from here)
cp .env.example .env
$EDITOR .env                                # fill POSTGRES_PASSWORD, SECRET_KEY,
                                            # UPSTOX_API_KEY/SECRET, …

# 2. Generate the cryptographic key the .env requires
openssl rand -hex 32                                                # → SECRET_KEY

# 3. Up
docker compose up --build       # add -d to background

# 4. Verify
curl http://localhost:8000/health
open  http://localhost:3000
```

The API container runs `alembic upgrade head` before `uvicorn`, so the DB
schema is bootstrapped automatically on first up.

**Caveat**: Docker Compose runs everything in containers — no scheduled
retrain (the systemd timer is host-only), and ML artefacts live in the
`ml_models` Docker volume rather than `backend/models/production/`.  For
training workflows use the bare-metal setup below.

---

## Manual setup (bare metal — required for ML training)

### 1. Infrastructure

```bash
# TimescaleDB (Postgres 16) — easiest is via Docker so you can keep using
# the same container as Compose; the API container in Compose will not
# duplicate it because the bare-metal API connects to the same port.
docker run -d --name cortex-db --restart unless-stopped \
  -e POSTGRES_USER=cortex -e POSTGRES_PASSWORD=$YOUR_PG_PASSWORD \
  -e POSTGRES_DB=cortex_db -p 5433:5432 \
  timescale/timescaledb-ha:pg16

# Redis 7
docker run -d --name cortex-redis --restart unless-stopped \
  -p 6379:6379 redis:7-alpine \
  redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy allkeys-lru
```

Native installs (`apt install postgresql-16-timescaledb redis`) work too —
the URLs in `.env` are what matters.

### 2. Backend (Python 3.11.15)

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate

# Core runtime dependencies (FastAPI, SQLAlchemy, Redis, Upstox SDK,
# TensorFlow with bundled CUDA, XGBoost, ONNX, Treelite, …)
pip install -r requirements.txt

# OPTIONAL: only needed if you will TRAIN models on this box.
# Most of these (numpy, torch, sklearn, pandas) are already pinned in
# requirements.txt to a tested combination — this file ensures the
# training-only extras (optuna trial extras, etc.) are also present.
# DO NOT install if you only need inference / API serving.
pip install -r requirements-ml-training.txt

# Backend .env (FastAPI + Alembic + the worker read from this file)
cp .env.example .env
$EDITOR .env

# Database schema
alembic upgrade head             # expect: head = 0039 (or later)
alembic current                  # confirm
```

### 3. Frontend (Node 20+)

```bash
cd ../frontend

# Create .env.local — there is no template; the only required key is the API URL.
# Bare-metal default below assumes you're running uvicorn on the same host.
cat > .env.local <<'EOF'
NEXT_PUBLIC_API_URL=http://localhost:8000
EOF

npm ci                            # exact lockfile install (not `npm install`)
npm run build                     # production build, or `npm run dev` for dev server
```

### 4. Run

```bash
# Terminal A — API
cd backend && source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

# Terminal B — background worker (RSS ingest, regime detection, PnL, …)
cd backend && source .venv/bin/activate
python -m app.worker

# Terminal C — frontend
cd frontend && npm run dev        # http://localhost:3000
```

---

## Environment variables

Two `.env` files exist on purpose:

| File | Used by | Notes |
|---|---|---|
| `.env` (root) | `docker compose` | Read by Compose at parse time; values interpolate into the API container's environment |
| `backend/.env` | bare-metal API, worker, scheduled retrain | Same vars as root but consumed directly by `pydantic-settings` and the systemd `EnvironmentFile` |

Both are `.gitignored`.  Keep them in sync (or symlink one to the other)
if you switch between Compose and bare-metal frequently.

### Required (will refuse to start without these)

| Var | How to generate / obtain |
|---|---|
| `POSTGRES_USER` `POSTGRES_PASSWORD` `POSTGRES_DB` | Choose; must match `DATABASE_URL` |
| `DATABASE_URL` | `postgresql+asyncpg://user:pass@host:port/db` (async driver) |
| `REDIS_URL` | `redis://localhost:6379/0` for a local Redis |
| `SECRET_KEY` | `openssl rand -hex 32` — JWT signing |

### Required for live trading features

| Var | Notes |
|---|---|
| `UPSTOX_API_KEY` `UPSTOX_API_SECRET` | Create app at https://developer.upstox.com/apps |
| `UPSTOX_REDIRECT_URI` | Must match Upstox app config |
| `UPSTOX_ACCESS_TOKEN` | Issued at OAuth callback — leave empty until first auth |

### Optional

`OLLAMA_BASE_URL`, `OPENAI_API_KEY`, `SENTRY_DSN`, `PROMETHEUS_ENABLED`,
`CORS_ALLOWED_ORIGINS` (never `*` — app refuses), per-route rate limits,
NSE holiday calendar — all defaulted in `.env.example`.

**Full reference**: `.env.example` at the repo root is the authoritative
list with inline documentation per variable.  Treat it as the spec.

---

## Database initialization

```bash
cd backend && source .venv/bin/activate

# Apply all migrations
alembic upgrade head

# Verify
alembic current                  # expect 0039 (head) or later
alembic heads
```

The migration set is **41 files** (`backend/alembic/versions/`) covering
the auth model, OHLCV ingestion, ML metadata, fundamentals (8 tables),
paper-trading, post-close monitoring, and the A8 `lineage` JSONB column.

**Data seeding is NOT a migration** — migrations only create schema.  Bars,
fundamentals, and the symbol universe are populated by ingestion services
that hit Upstox at runtime.  On a fresh box you will see empty tables until
the worker / scheduled jobs run.

---

## ML environment constraints — CRITICAL

The ML stack is **deliberately pinned** to a verified-working combination.
Drift here breaks GPU training or causes silent correctness regressions.
This is the **single most fragile area** in a migration.

### Locked package set (verified working)

| Package | Version | Why pinned |
|---|---|---|
| `numpy` | **1.26.4** (`<2.0`) | numpy 2.x breaks TensorFlow 2.21's `ml-dtypes` / ABI → silently disables GPU |
| `scikit-learn` | **1.4.0** | sklearn ≥1.5 cascades numpy + breaks the same TF chain |
| `pandas` | **3.0.2** | Verified compatible with the above |
| `torch` | **2.11.0+cpu** | CPU-only wheel — see §GPU below |
| `tensorflow[and-cuda]` | **2.21.0** | Bundles CUDA 12.9 + cuDNN 9 (installs `nvidia-*-cu12` wheels itself) |
| `onnx` | **1.21.0** | Sequence-model serving format |
| `xgboost` | (pinned in requirements.txt) | Treelite-compiled for inference |

### DO NOT install (will cascade-break TF GPU)

- `numpy>=2`
- `scikit-learn>=1.5`
- `skfolio` (cascades numpy → 2.x)
- Default-index `torch` (defaults to CUDA build that conflicts with bundled cu12)
- Anything via `pip install -U` without first dry-running

Always: `pip install --dry-run <pkg>` first.  If the dry-run shows numpy /
sklearn / torch / TF would be upgraded or replaced, **stop**.

### GPU notes (TensorFlow only)

- `tensorflow[and-cuda]==2.21.0` installs its own CUDA + cuDNN under the
  venv (`.venv/lib/python3.11/site-packages/nvidia_*_cu12/`).  You do
  **not** need a separate system CUDA install — just the NVIDIA driver.
- The driver's max CUDA version (`nvidia-smi` top right) must be ≥12.9.
  On WSL2 this is set by the *Windows host* driver, not the WSL kernel.
- `torch` is CPU-only because the box this was built on has a WSL CUDA
  ceiling of 12.2 (older driver) and a 4 GB GPU — see
  `ML_IMPLEMENTATION_TASKS.md` Workstream B for the empirical bench that
  pinned this decision.  If your target box has a 16+ GB GPU and a newer
  driver, you may be able to install a CUDA-matching torch wheel — but
  test it with the bench first (`backend/scripts/bench_tft_cpu.py`).

### Verify the stack post-install

```python
python -c "
import numpy, sklearn, torch, pandas, tensorflow as tf
print(f'numpy   : {numpy.__version__}     (must be 1.26.x)')
print(f'sklearn : {sklearn.__version__}   (must be 1.4.x)')
print(f'pandas  : {pandas.__version__}')
print(f'torch   : {torch.__version__}     (cuda={torch.cuda.is_available()})')
print(f'tf      : {tf.__version__}        gpu={len(tf.config.list_physical_devices(\"GPU\"))}')
"
```

Expected (CPU-only torch + TF GPU on):
```
numpy   : 1.26.4
sklearn : 1.4.0
pandas  : 3.0.2
torch   : 2.11.0+cpu       (cuda=False)
tf      : 2.21.0           gpu=1
```

If `gpu=0` and the box has an NVIDIA card, check the driver version with
`nvidia-smi`.  If `numpy` is 2.x, **roll back the venv** —
`pip install 'numpy<2.0' --force-reinstall` and re-test.

---

## Model artifacts — not in git

`backend/models/` is `.gitignored`.  This means **trained models do not
transfer with the git clone**.  Two options on a new box:

### Option A — Transfer (preferred, preserves the registry)

```bash
# From old box → new box  (uses ~2-5 GB)
rsync -avh --progress \
  preet@old:/home/preet/code/Cortex_Merge_AI-ML/backend/models/production/ \
  ./backend/models/production/
```

After transfer the `ml_model_metadata` rows in Postgres still reference
these paths (`model_path`, `onnx_path`).  Verify with:

```bash
cd backend && source .venv/bin/activate
python scripts/promote_model.py status
python scripts/reeval_production_model.py     # writes incident_reports/<ts>.json
```

`reeval_production_model.py` is **report-only** — it audits the live
1.0.0 ensemble against the post-A6 hard gates without mutating anything.
Exit 0 = MEETS_BAR, exit 2 = DEMOTE_RECOMMENDED (operator runs
`promote_model.py demote …` to act).  See `ML_IMPLEMENTATION_TASKS.md`
§C3 for context.

### Option B — Retrain from scratch (multi-hour)

```bash
cd backend && source .venv/bin/activate
python scripts/production_training_orchestrator.py --fresh
```

Requires the symbol universe + 10y of OHLCV bars + fundamentals already
ingested into Postgres (handled by `app.worker` + ingestion scripts).
Without bars, the orchestrator will fail at the A7 data-coverage gate.

### Storage note

Model artefacts use **plaintext binary + SHA-256 integrity check** on every
load (`registry_loader._sha256_file`). There is no encryption-at-rest.
`ML_MODEL_ENCRYPTION_KEY` has been removed from the configuration — it is
not required and not accepted. If it appears in a legacy `.env` file, remove
it; the application will ignore unknown env vars but `.env.example` no
longer documents it.

---

## Tests — not in git

`backend/tests/` is **.gitignored** (each contributor maintains a local
test set that mirrors the modules they own).  On a fresh clone the
test directory is missing.

To pull tests when migrating between owned environments:

```bash
rsync -avh preet@old:/home/preet/code/Cortex_Merge_AI-ML/backend/tests/ \
            ./backend/tests/
```

After that, the canonical ML acceptance suite (~256 tests):

```bash
cd backend && source .venv/bin/activate
python -m pytest tests/unit/test_ml_*.py
# Expect: 256 passed in ~20s
```

Coverage breakdown (as of A1–A8 + C1 + C3, 2026-05-23):
- A1 fail-loud: 9 · A2 CPCV: 24 · A3 backtest+DSR: 36 · A4 calibration: 30
- A5 ensemble: 15 · A6 quality gate: 45 · A7 data integrity: 23
- A8 registry + demote: 36 · C1 scheduled retrain: 20 · C3 reeval: 18

---

## Background services

### Worker (always-on)

`backend/app/worker.py` runs market-feed ingestion, regime detection,
RSS news polling, PnL recomputation, etc.  Two ways to run:

```bash
# Foreground (dev)
cd backend && python -m app.worker

# systemd (production)
sudo cp backend/cortex-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cortex-worker.service
journalctl -u cortex-worker.service -f
```

The unit caps memory at 2 GB, CPU at 200%, and reads `backend/.env` via
`EnvironmentFile=`.

### Scheduled ML challenger retrain (weekly)

Off-market wrapper that runs `production_training_orchestrator.py
--fresh` on a fixed cadence.  **Never promotes** — produces a challenger
run dir for human review.

```bash
sudo cp deploy/cortex-retrain.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cortex-retrain.timer

# Verify
systemctl list-timers cortex-retrain.timer    # shows next fire time
```

Full operator manual at `deploy/README.md`.  Cadence + window knobs in
`backend/app/ml/config.py::SCHEDULED_RETRAIN`.

### One-shot scheduled retrain (no systemd)

```bash
cd backend && python scripts/scheduled_retrain.py --dry-run    # print plan
cd backend && python scripts/scheduled_retrain.py --once       # run now
cd backend && python scripts/scheduled_retrain.py --schedule   # APScheduler long-running
```

The `--once` mode is what the systemd `cortex-retrain.timer` invokes;
running it manually is identical.

---

## Verification (after migration)

Run these in order — each should succeed before moving to the next.

```bash
# 1. Stack pins intact
python -c "import numpy, sklearn, torch, tensorflow as tf; \
print(f'np={numpy.__version__} sk={sklearn.__version__} \
torch={torch.__version__} tf={tf.__version__} gpu={len(tf.config.list_physical_devices(\"GPU\"))}')"
# expect: np=1.26.4 sk=1.4.0 torch=2.11.0+cpu tf=2.21.0 gpu=1  (gpu=0 OK if no NVIDIA)

# 2. DB schema at head
cd backend && alembic current                                   # 0039 (head)

# 3. Redis reachable
redis-cli -u "$REDIS_URL" ping                                  # PONG

# 4. App preflight (imports, DB, Redis, model loader)
python scripts/preflight_check.py                               # all green

# 5. ML acceptance suite
python -m pytest tests/unit/test_ml_*.py -q                    # 256 passed

# 6. API smoke
uvicorn app.main:app --port 8000 &
curl -sf http://localhost:8000/health                           # 200 OK
kill %1

# 7. Live model audit (C3 report-only)
python scripts/reeval_production_model.py                       # exit 0 or 2
ls incident_reports/                                            # report file exists

# 8. Scheduled retrain wiring (no actual training)
python scripts/scheduled_retrain.py --dry-run                   # prints plan, exits 0

# 9. Frontend builds
cd ../frontend && npm ci && npm run build                       # exits 0
```

---

## Known gotchas

- **`backend/tests/` and `backend/.env` are `.gitignored`**.  Migrating
  via `git clone` alone is not enough — see `rsync` snippets above.
- **Two `.env` files** (root + backend).  Docker reads root; bare metal
  reads backend.  Keep them in sync; mismatch silently uses the wrong DB.
- **TensorFlow GPU is fragile**.  Any `pip install` of `numpy` /
  `scikit-learn` / `skfolio` / `torch` without `--dry-run` first can
  cascade-break TF.  See §ML Environment Constraints.
- **`filterwarnings = error`** in `backend/pytest.ini`: any uncaught
  warning fails the test.  When adding new code, run the test suite
  before merging.
- **GRU training caps VRAM at 1.6 GB** by default (`gru_trainer.py`
  `_GPU_VRAM_LIMIT_MB`) so it co-exists with other GPU users on a 4 GB
  card.  Raise this on bigger GPUs.
- **The `cu13` orphan in the venv** (verified-present on the dev box this
  README was authored on) is a known artefact of an earlier abandoned
  torch+CUDA install.  Naive `pip uninstall nvidia-*-cu13` will break
  TF GPU — purge requires `tensorflow[and-cuda]==2.21.0` force-reinstall
  *after* removing the cu13 stack.  Tracked as A0.1b in
  `ML_IMPLEMENTATION_TASKS.md`; not a fresh-box concern.
- **Alembic migrations are forward-only in practice**.  Down-revisions
  are written but rarely tested past the most recent few.  Snapshot
  Postgres before any downgrade attempt.
- **`scripts/promote_model.py` has no `demote` for arbitrary state**;
  the new `demote` subcommand transitions production → staging only
  (the C3 flow).  See `deploy/README.md` and `ML_IMPLEMENTATION_TASKS.md`
  §A8 / §C3 for the full lifecycle.
- **CORS rejects `*`**.  `CORS_ALLOWED_ORIGINS` must be an explicit
  comma-separated list or the app refuses to start.

---

## Common operations cheat-sheet

```bash
# DB schema
cd backend && alembic upgrade head                          # apply migrations
cd backend && alembic current                               # what's applied
cd backend && alembic history --indicate-current            # full history

# Model lifecycle
cd backend && python scripts/promote_model.py status                       # current state
cd backend && python scripts/promote_model.py staging    --version <v>     # dev → staging
cd backend && python scripts/promote_model.py production --version <v>     # staging → prod
cd backend && python scripts/promote_model.py rollback   --model-name xgboost
cd backend && python scripts/promote_model.py demote     --version <v> --reason '…'

# ML audit
cd backend && python scripts/reeval_production_model.py                    # C3 report
cd backend && python scripts/preflight_check.py                            # full env check

# Training
cd backend && python scripts/production_training_orchestrator.py --fresh   # full retrain
cd backend && python scripts/scheduled_retrain.py --once                   # via wrapper

# Services
sudo systemctl status  cortex-worker.service cortex-retrain.timer
sudo systemctl restart cortex-worker.service
journalctl -u cortex-worker.service -f
journalctl -u cortex-retrain.service --since "1 week ago"

# Knowledge graph (architecture queries)
graphify update .                                           # refresh after code changes
graphify query "<question>"                                 # natural-language query
cat graphify-out/GRAPH_REPORT.md                            # god-node + community report
```

---

## Where to read next

| Want to understand … | Read |
|---|---|
| The ML correctness work (A0–A8) and what's next | `ML_IMPLEMENTATION_TASKS.md` |
| Architectural decisions + RC-1..RC-7 root causes | `ML_AUDIT_REPORT.md` + `ML_REMEDIATION_PLAN.md` |
| Master plan for the ML overhaul | `ML_IMPLEMENTATION_PLAN.md` |
| Fundamentals data pipeline | `FUNDAMENTALS_IMPLEMENTATION_PLAN.md` + `FUNDAMENTALS_RESEARCH.md` |
| systemd timer + scheduled retrain | `deploy/README.md` |
| Cross-module relationships | `graphify-out/GRAPH_REPORT.md` (8000+ node graph) |
| Per-area docs (auth, paper trading, fundamentals, …) | `documentation/` |
