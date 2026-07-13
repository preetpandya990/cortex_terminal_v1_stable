# ── Cortex AI — Backend Dockerfile ───────────────────────────────────────────
#
# Multi-stage build:
#   builder  — compiles all Python packages (includes build toolchain + TA-Lib
#              headers).  Nothing from this stage enters production.
#   runtime  — slim image with only the installed venv and runtime system libs.
#
# Base:  python:3.11.15-slim-bookworm.
# GPU:   tensorflow[and-cuda] and onnxruntime-gpu bundle their own CUDA/cuDNN
#        wheels — no CUDA base image required.  At run time, pass --gpus all to
#        the Docker daemon so the container inherits the host NVIDIA driver.
#
# Non-root user: cortex (uid/gid 1001) — no shell, no home directory.
# Default command: API (port 8000).  docker-compose.yml overrides this per
# service — the worker sidecar starts on port 8001 with a different CMD.
# ─────────────────────────────────────────────────────────────────────────────

# ──────────────────────────── Stage 1 · builder ───────────────────────────────
FROM python:3.11.15-slim-bookworm AS builder

# Build toolchain for any Python packages that still need native compilation.
# TA-Lib is installed from the modern wheel-backed Python distribution pinned in
# requirements.txt; it no longer depends on Debian libta-lib packages here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
    && rm -rf /var/lib/apt/lists/*

# Isolate all Python packages inside a virtualenv so the runtime stage can
# copy the entire /venv directory cleanly.
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

# Pin pip + setuptools to prevent wheel ABI mismatches on ML packages.
RUN pip install --no-cache-dir --upgrade pip==24.3.1 setuptools==75.8.0 wheel==0.45.1

# ── PyTorch CPU-only wheel ────────────────────────────────────────────────────
# MUST be installed before requirements.txt.
# PyPI's default torch 2.x wheel is now cu130 (CUDA 13), which conflicts with
# our tensorflow[and-cuda] CUDA 12 stack and pulls 5+ GB of redundant NVIDIA
# libs.  Install the CPU build from the official whl index first so pip's
# resolver sees it as already satisfied (same version, different build tag)
# when processing requirements.txt.
RUN pip install --no-cache-dir \
        torch==2.11.0 \
        --index-url https://download.pytorch.org/whl/cpu

# ── Main dependency install ───────────────────────────────────────────────────
# Runs after PyTorch is pinned so the resolver never selects the GPU wheel.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ──────────────────────────── Stage 2 · runtime ───────────────────────────────
FROM python:3.11.15-slim-bookworm AS runtime

# Runtime system dependencies only:
#   curl        — used by Docker Compose healthcheck probes (curl -f /health).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — principle of least privilege.
# The container process never needs to write to the filesystem beyond /app.
RUN groupadd --gid 1001 cortex \
    && useradd \
        --uid 1001 \
        --gid cortex \
        --shell /usr/sbin/nologin \
        --no-create-home \
        cortex

# Copy the fully-populated virtualenv from the builder stage.
# All Python packages (including compiled C extensions) are present here.
COPY --from=builder /venv /venv

# Application source: copy the backend directory into /app.
# Docker build context is ./backend (set in docker-compose.yml).
WORKDIR /app
COPY --chown=cortex:cortex . .

# Writable runtime paths for logs, model artifacts, and local cache.
# COPY --chown updates copied content ownership, but the /app directory itself
# is still root-owned because WORKDIR created it earlier in the image build.
# Ensure the non-root runtime user can create any missing subdirectories.
RUN mkdir -p /app/logs /app/models /app/.cache \
    && chown -R cortex:cortex /app

# ── Runtime environment ───────────────────────────────────────────────────────
ENV PATH="/venv/bin:$PATH" \
    # /app is the Python root — "from app.xxx import yyy" resolves correctly.
    PYTHONPATH="/app" \
    # Suppress .pyc file generation — not needed in immutable containers.
    PYTHONDONTWRITEBYTECODE=1 \
    # Force stdout/stderr to be unbuffered so structured logs reach the
    # Docker log driver immediately (critical for real-time observability).
    PYTHONUNBUFFERED=1 \
    # Silence pip version-check noise in container stdout.
    PIP_DISABLE_PIP_VERSION_CHECK=1

USER cortex

# Expose the API port.  The worker sidecar uses 8001 — overridden in
# docker-compose.yml via the `command:` key, not an EXPOSE change.
EXPOSE 8000

# Default command: run the API.
# docker-compose.yml overrides this for the worker service (port 8001).
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--loop", "uvloop", \
     "--log-level", "info"]
