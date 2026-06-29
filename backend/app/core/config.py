"""
Cortex AI — Unified Configuration
==================================
All settings loaded from environment variables or .env file.
Fails fast if required secrets are missing.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    PostgresDsn,
    RedisDsn,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="",
    )

    # ── Application ────────────────────────────────────────────────────────────
    APP_NAME: str = "Cortex AI Trading Platform"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "production"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # ── Database ───────────────────────────────────────────────────────────────
    DATABASE_URL: PostgresDsn = Field(..., description="Async PostgreSQL URL (postgresql+asyncpg://...)")
    DB_POOL_SIZE: int = Field(20, ge=1, le=100)
    DB_MAX_OVERFLOW: int = Field(10, ge=0, le=100)
    DB_POOL_TIMEOUT: int = Field(30, ge=5, le=120)
    DB_POOL_RECYCLE: int = Field(1800, ge=300)

    # ── Redis ──────────────────────────────────────────────────────────────────
    REDIS_URL: RedisDsn = Field(..., description="Redis URL (redis://host:port/db)")
    REDIS_MAX_CONNECTIONS: int = Field(50, ge=10, le=500)

    # ── Security — JWT ─────────────────────────────────────────────────────────
    SECRET_KEY: str = Field(..., min_length=32, description="HMAC secret (openssl rand -hex 32)")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(30, ge=5, le=1440)
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(7, ge=1, le=30)

    # ── Security — CORS ────────────────────────────────────────────────────────
    CORS_ALLOWED_ORIGINS: list[AnyHttpUrl] = Field(..., description="Explicit allowlist of origins")
    CORS_ALLOW_CREDENTIALS: bool = True

    # ── Upstox ─────────────────────────────────────────────────────────────────
    UPSTOX_API_KEY: str = Field(..., min_length=8)
    UPSTOX_API_SECRET: str = Field(..., min_length=8)
    UPSTOX_REDIRECT_URI: AnyHttpUrl
    UPSTOX_ACCESS_TOKEN: str | None = None
    UPSTOX_BASE_URL: str = "https://api.upstox.com/v3/"
    UPSTOX_INSTRUMENTS_URL: AnyHttpUrl = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"

    # ── Instrument master sync ─────────────────────────────────────────────────
    # Upstox regenerates the BOD instrument file ~06:00 IST; we sync after that
    # and before the 09:15 open on trading days.
    INSTRUMENT_SYNC_HOUR_IST: int = Field(
        8, ge=0, le=23, description="Hour (IST) at which the daily instrument sync runs"
    )
    # Absolute floor for the sanity guard: a file with fewer in-scope instruments
    # than this is treated as truncated/corrupt and the sync is aborted.
    INSTRUMENT_SYNC_MIN_INSTRUMENTS: int = Field(
        1500, ge=1, description="Minimum plausible in-scope instrument count; below this the sync aborts"
    )
    # On startup, run an immediate catch-up sync if the last success is older
    # than this (or the table is empty) so a fresh deploy is usable at once.
    INSTRUMENT_SYNC_STALE_HOURS: int = Field(
        24, ge=1, le=168, description="Age past which a startup catch-up instrument sync is triggered"
    )

    # ── Worker ─────────────────────────────────────────────────────────────────
    ENABLE_BACKGROUND_TASKS: bool = True
    WORKER_SHUTDOWN_TIMEOUT: int = Field(30, ge=5, le=120)

    # ── Worker Sidecar ─────────────────────────────────────────────────────────
    # The worker sidecar runs as a separate Docker Compose service on port 8001
    # (internal only — never exposed to the host).  The main API calls it via
    # httpx + circuit breaker for control-plane operations (pause/resume/trigger).
    # All data flow continues through Redis pub/sub — these settings only affect
    # the HTTP control plane.
    WORKER_BASE_URL: str = Field(
        "http://worker:8001",
        description="Internal base URL of the worker sidecar service",
    )
    INTERNAL_API_SECRET: str = Field(
        ...,
        min_length=64,
        description=(
            "Shared secret for service-to-service auth (X-Internal-Token header). "
            "Must be 64+ hex characters. "
            "Generate: python -c \"import secrets; print(secrets.token_hex(32))\""
        ),
    )
    WORKER_HTTP_TIMEOUT: float = Field(
        3.0,
        ge=0.5,
        le=30.0,
        description="HTTP read timeout (seconds) for control-plane calls to the worker sidecar",
    )

    # ── RSS Ingestion ──────────────────────────────────────────────────────────
    RSS_MIN_POLL_SECONDS: int = Field(300, ge=60)
    RSS_MAX_POLL_SECONDS: int = Field(900, ge=300)
    RSS_CONCURRENCY: int = Field(5, ge=1, le=20)
    RSS_POLL_JITTER_SECONDS: int = Field(60, ge=0, le=300)

    # ── Intelligence Layer — NVIDIA NIM (primary LLM provider) ────────────────
    # Obtain API key from build.nvidia.com → API Keys → Generate.
    # Set NVIDIA_NIM_API_KEY in .env to activate NIM as the primary backend.
    # If unset, the system falls back to Ollama automatically (logged at startup).
    #
    # Model: verify current availability at build.nvidia.com/models before first run.
    # The model below is the recommended Qwen3 MoE variant as of 2026-06-03.
    NVIDIA_NIM_API_KEY: str | None = None
    NVIDIA_NIM_BASE_URL: str = "https://integrate.api.nvidia.com/v1"
    NVIDIA_NIM_MODEL: str = "qwen/qwen3.5-122b-a10b"
    NVIDIA_NIM_EMBED_MODEL: str = "nvidia/nv-embedqa-e5-v5"

    # ── Intelligence Layer — Google Gemini (active LLM + embedding provider) ──
    # Single provider for the entire AI layer: text generation, structured
    # output, the news forecaster, explanations, sentiment, classification,
    # fake-news detection, and RAG embeddings.  Obtain a key from Google AI
    # Studio (https://aistudio.google.com/apikey) and set GEMINI_API_KEY in .env.
    #
    # Multi-key load balancing (paid tier only): set GEMINI_API_KEYS to a JSON
    # array of additional keys, each from a separate GCP project with its own
    # independent quota pool.  The client round-robins across all keys; exhausted
    # keys are circuit-broken per-key and removed from rotation until manually
    # re-enabled via Redis (DEL cortex:gemini:circuit:<op>:<last8>).
    #
    # The NIM / Groq / Ollama settings above/below are legacy and no longer wired
    # into the client; they are retained only for reference and rollback.
    GEMINI_API_KEY: str | None = None
    GEMINI_API_KEYS: list[str] = Field(
        default_factory=list,
        description=(
            "Additional Gemini API keys for multi-key load balancing (paid tier only). "
            "Parsed as a JSON array: '[\"AIzaSy...key2...\", \"AIzaSy...key3...\"]'. "
            "Combined with GEMINI_API_KEY to form the key pool. "
            "Each key must belong to a separate GCP project for independent quota."
        ),
    )
    # gemini-2.5-flash: stable, low-latency, best price-performance for the
    # high-volume reliability-critical paths (the forecaster gates suggestions).
    # gemini-3.5-flash was capacity-constrained (503) at cutover; revisit when its
    # availability stabilizes.
    GEMINI_MODEL: str = "gemini-2.5-flash"
    # Latency control: "minimal" disables extended thinking for the fastest,
    # most predictable replies.  The client maps this per model generation —
    # thinking_level (Gemini 3.x) vs. thinking_budget=0 (Gemini 2.x) — see
    # llm_client._thinking_config.  Use "low"/"medium"/"high" for deeper reasoning.
    GEMINI_THINKING_LEVEL: str = "minimal"
    # Embeddings — gemini-embedding-001.  output_dimensionality pins the pgvector
    # column dimension (see ai_document_embeddings).  Vectors below 3072 are
    # L2-normalized by the embedder.  Recommended values: 768, 1536, 3072.
    # Default is 768 (halfvec storage, migration 0048).  Changing this requires
    # a new migration + full corpus re-embed via scripts/reembed_rag_corpus.py.
    GEMINI_EMBED_MODEL: str = "gemini-embedding-001"
    GEMINI_EMBED_DIM: int = Field(768, ge=128, le=3072)

    # ── Gemini Request Manager — quota budgets and queue tuning ───────────────
    # These values must match your API key's tier.  Upgrading tiers is a single
    # .env change — no code changes required.
    #
    # Paid Tier 1 (default):  generate 150 RPM / 1M TPM, embed 90 RPM
    # Paid Tier 2:            generate 500 RPM / 2M TPM
    # Free tier:              generate 10 RPM / 250K TPM, embed 90 RPM
    #
    # The burst_cap on the token buckets (min(rpm, 10)) is hard-coded in the
    # manager to prevent a quiet period from banking a full minute's budget and
    # then releasing it in a single burst — it is NOT configurable here.
    GEMINI_GENERATE_RPM: int = Field(
        150, ge=1, le=1500,
        description="Gemini generate requests/min budget — 10=free, 150=Tier1, 500=Tier2",
    )
    GEMINI_GENERATE_TPM: int = Field(
        1_000_000, ge=1, le=10_000_000,
        description="Gemini generate tokens/min budget — 250K=free, 1M=Tier1, 2M=Tier2",
    )
    GEMINI_EMBED_RPM: int = Field(
        90, ge=1, le=2000,
        description="Gemini embed requests/min budget — 90=free/Tier1, higher on Tier2",
    )
    GEMINI_MAX_QUEUE_DEPTH: int = Field(
        50, ge=10, le=500,
        description=(
            "Maximum permits waiting in the GeminiRequestManager priority queue. "
            "Callers beyond this limit receive GeminiRateLimitError immediately — "
            "backpressure prevents unbounded memory growth under sustained overload."
        ),
    )
    # Buffer (minutes) added to midnight Pacific Time before re-enabling exhausted keys.
    # Gemini's RPD counter resets at midnight PT but occasionally lags by up to
    # 15 minutes in practice (community-reported; Google does not publish an SLA).
    # At the reset point the watcher task automatically clears all open circuits
    # and restores every exhausted key to active rotation — no operator action or
    # app restart required.  Raise above 15 only if your keys are regularly
    # re-exhausted within the first few minutes of a new quota day.
    GEMINI_QUOTA_RESET_BUFFER_MINUTES: int = Field(
        15, ge=1, le=60,
        description=(
            "Minutes after midnight Pacific Time before auto-resetting exhausted "
            "Gemini key circuits.  Absorbs the observed 0–15 min reset lag. "
            "Default 15 min is the safe community-validated choice."
        ),
    )

    GEMINI_PERMIT_TIMEOUT: float = Field(
        60.0, ge=5.0, le=120.0,
        description=(
            "Maximum seconds a caller blocks waiting for a Gemini API permit. "
            "On timeout the caller receives GeminiRateLimitError and should degrade "
            "gracefully.  Must be less than LLM_REQUEST_TIMEOUT. "
            "60s gives the explanation worker (120s ceiling) room to absorb queue "
            "pressure without burning a retry attempt."
        ),
    )

    # ── Budget Guard ───────────────────────────────────────────────────────────
    # Soft daily-budget layer below the hard circuit breaker.  When estimated
    # remaining quota falls within the HIGH-priority reservation, MEDIUM / LOW /
    # BACKGROUND callers are throttled immediately so that explanations and
    # other HIGH+ calls always have headroom — even after a heavy background
    # sentiment / forecaster burst consumes the bulk of the daily allowance.
    #
    # GEMINI_GENERATE_RPD: set to the RPD limit on your Google AI Studio quota
    # page (per-key value; pool total = this × active key count):
    #   Free tier  (gemini-2.5-flash) : 1 500 RPD/key
    #   Tier 1                        : check your quota dashboard
    #
    # GEMINI_HIGH_PRIORITY_RPD_RESERVE: the minimum headroom kept for HIGH+
    # callers.  200 covers a worst-case trading day of 200 explanation calls;
    # raise it if you expect higher suggestion throughput.
    GEMINI_GENERATE_RPD: int = Field(
        1_500, ge=100, le=100_000,
        description=(
            "Daily Gemini generate quota per API key (requests per day). "
            "Pool total = this × key_count.  Used by the budget guard to "
            "estimate remaining headroom; does not affect the hard circuit "
            "breaker, which is still triggered by actual 429 responses.  Set "
            "to the value shown on your Google AI Studio quota dashboard."
        ),
    )
    GEMINI_HIGH_PRIORITY_RPD_RESERVE: int = Field(
        200, ge=10, le=5_000,
        description=(
            "Daily generate requests reserved exclusively for HIGH and CRITICAL "
            "priority callers (trade-suggestion explanations, instrument context). "
            "When estimated remaining budget falls below this threshold, MEDIUM / "
            "LOW / BACKGROUND callers receive GeminiBudgetThrottled and degrade "
            "gracefully.  HIGH+ callers are never throttled by the budget guard."
        ),
    )

    # ── Intelligence Layer — Gemini news forecaster (consensus-path) ──────────
    # The forecaster runs INSIDE the 5s consensus gather budget, so its own call
    # timeout must leave room for the ML/scanner work; on timeout/error it falls
    # back to the deterministic NLP event score (never blocks suggestion creation).
    # ~2.0–2.5s observed live on gemini-2.5-flash; 3.0s leaves headroom before
    # the forecast times out to the NLP fallback.
    GEMINI_FORECAST_TIMEOUT: float = Field(3.0, ge=0.5, le=4.5)
    GEMINI_FORECAST_MAX_TOKENS: int = Field(400, ge=128, le=2048)
    # Ceiling for the whole consensus signal-gather (ML → news forecast, sequential
    # because the forecaster reasons over the ML's indicators).  Lifted from the
    # legacy 5s so a legitimate ML+forecast is never rejected as a timeout.
    CONSENSUS_GATHER_TIMEOUT: float = Field(6.0, ge=3.0, le=15.0)

    # ── Explanation Confidence Gate ────────────────────────────────────────────
    # Minimum consensus_score (0–100) required before the correlation engine
    # enqueues an LLM explanation job.  consensus_score is a deterministic
    # composite: scanner×0.30 + AI×0.40 + ML×0.30.  Live-data max ≈ 89.7;
    # 75.0 gates the 60–74 band (~56% of current signals).  Signals below the
    # threshold surface a "weak signal" placeholder with a user-driven refresh
    # button that fires an on-demand explanation at Priority.HIGH.
    # Raise toward 80 via .env as model scores normalise over time.
    EXPLANATION_CONSENSUS_THRESHOLD: float = Field(
        75.0,
        ge=50.0,
        le=95.0,
        description=(
            "Minimum consensus_score for automatic LLM explanation generation. "
            "Signals below this threshold render a weak-signal placeholder in the "
            "AI panel with a user-driven refresh button that triggers an on-demand "
            "explanation at Priority.HIGH."
        ),
    )

    # ── Watchlist Context Scheduler ────────────────────────────────────────────
    # Pre-warms AI market context for all watchlist instruments on a fixed
    # intraday schedule.  Fires 4× per NSE trading day; only instruments whose
    # context expires within WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES are
    # re-enqueued, preventing redundant Gemini calls for recently generated context.
    WATCHLIST_SCHEDULER_RUN_TIMES_IST: list[str] = Field(
        default=["09:30", "11:00", "13:00", "14:30"],
        description=(
            "IST wall-clock times (HH:MM) at which the watchlist context scheduler "
            "runs.  Must be within NSE market hours.  Provide as a JSON array in "
            ".env: '[\"09:30\", \"11:00\", \"13:00\", \"14:30\"]'."
        ),
    )
    WATCHLIST_SCHEDULER_FRESHNESS_MARGIN_MINUTES: int = Field(
        80,
        ge=10,
        le=180,
        description=(
            "Re-enqueue an instrument's context only when it expires within this "
            "many minutes.  80 min aligns with the ~90-min gap between scheduled "
            "runs, ensuring every instrument is fresh at each firing without "
            "redundant Gemini calls for recently generated context."
        ),
    )
    WATCHLIST_SCHEDULER_BATCH_CAP: int = Field(
        200,
        ge=10,
        le=500,
        description=(
            "Maximum unique instruments enqueued per scheduler run.  Instruments "
            "beyond the cap are deferred to the next run.  200 comfortably fits "
            "within a single batch window at default Gemini Tier-1 quotas."
        ),
    )

    # Per-symbol forecast cache; busted when the news/event set changes.
    # 300s (5 min): at 100 symbols, 30s meant ~200 RPM from the forecaster alone.
    # 5-minute TTL cuts forecast calls by ~80% with negligible freshness impact.
    NEWS_FORECAST_CACHE_TTL: int = Field(300, ge=5, le=3600)
    # Circuit breaker: after N consecutive Gemini failures, short-circuit to the
    # NLP fallback for COOLDOWN seconds instead of paying the timeout every call.
    NEWS_FORECAST_BREAKER_THRESHOLD: int = Field(4, ge=1, le=20)
    NEWS_FORECAST_BREAKER_COOLDOWN: float = Field(60.0, ge=5.0, le=600.0)

    # ── Sentiment Batch Accumulator ────────────────────────────────────────────
    # The NLP engine accumulates incoming articles in a queue and fires one
    # Gemini call per batch instead of one call per article.  At batch size 15
    # this reduces steady-state sentiment calls by ~93%.
    #
    # SENTIMENT_BATCH_SIZE: articles per batched call.  Research on LLM batch
    #   classification reliability (arxiv:2506.04574, 2025) places the safe
    #   ceiling at 10–15 items.  15 is the production default — at the upper
    #   end of the validated range, cutting event-pipeline Gemini calls by ~47%
    #   vs the prior default of 8.  A lone article in the queue uses the
    #   single-item schema path (no batch prompt overhead) regardless.
    #
    # SENTIMENT_BATCH_WINDOW_SECS: maximum wait before flushing an incomplete
    #   batch.  60 s reduces flush frequency during low-traffic windows while
    #   remaining well within the 900 s service-level L2 cache window.  The SSE
    #   path bypasses the queue entirely via analyze_sentiment_batch().
    SENTIMENT_BATCH_SIZE: int = Field(
        15,
        ge=1,
        le=20,
        description=(
            "Maximum articles per batched Gemini sentiment call. "
            "Research (arxiv:2506.04574, 2025) validates up to 10–15 items; "
            "15 is the production default. A lone queued article bypasses the "
            "batch prompt path entirely."
        ),
    )
    SENTIMENT_BATCH_WINDOW_SECS: float = Field(
        60.0,
        ge=1.0,
        le=120.0,
        description=(
            "Maximum seconds the batch accumulator waits before flushing an "
            "incomplete batch. 60 s reduces flush frequency during low-traffic "
            "windows while remaining well inside the 900 s service L2 TTL. "
            "The SSE path bypasses the queue via analyze_sentiment_batch()."
        ),
    )

    # ── Live data freshness guard ─────────────────────────────────────────────
    # The daily OHLCV sync refreshes ~2,400 instruments incrementally, so for a
    # while some symbols lag the rest.  When a symbol's latest daily bar is behind
    # the global 1D data frontier by more than the tolerance, the ML ensemble and
    # the news forecaster ABSTAIN for that symbol (available=False) rather than
    # emit a signal on stale technicals.  Self-heals next cycle once the sync
    # reaches the symbol.  Frontier-relative, so it needs no trading calendar.
    ENABLE_STALENESS_GUARD: bool = True
    # A symbol is stale if its latest daily bar lags the frontier by MORE than this.
    STALENESS_MAX_LAG_DAYS: int = Field(1, ge=0, le=10)
    # Robust frontier = the newest day on which at least this many daily bars exist
    # (the "bulk" of the universe).  This is outlier-proof — a single instrument
    # synced ahead cannot drag the frontier forward and flag everyone else stale.
    STALENESS_FRONTIER_MIN_INSTRUMENTS: int = Field(500, ge=1)
    STALENESS_FRONTIER_CACHE_SECS: int = Field(60, ge=5, le=600)

    # ── Intelligence Layer — LLM Behaviour ────────────────────────────────────
    # 2 retries: reduces quota amplification by 33% on burst 429s while still
    # tolerating a single transient failure per request.
    LLM_MAX_RETRIES: int = Field(2, ge=1, le=5)
    LLM_REQUEST_TIMEOUT: float = Field(30.0, ge=5.0, le=120.0)

    # ── Intelligence Layer — RAG ───────────────────────────────────────────────
    RAG_TOP_K: int = Field(5, ge=1, le=20)
    RAG_WINDOW_HOURS: int = Field(24, ge=1, le=168)
    # 96 texts/call: 3× throughput vs. default 32, stays within gemini-embedding-001
    # input limits. Reduces embed API calls proportionally for the same corpus size.
    RAG_EMBED_BATCH_SIZE: int = Field(96, ge=1, le=128)
    # TTL for the RAG corpus cleanup job.  Embeddings older than this are deleted
    # daily.  Lower-bound matches the retriever's max window (168h = 7 days) so
    # that no live retrieval window ever extends past the cleanup boundary.
    RAG_EMBEDDING_TTL_DAYS: int = Field(7, ge=7, le=90)

    # ── RAG corpus backfill (paced, monitorable background job) ────────────────
    # Outbound embedding pace (texts/min).  Free-tier gemini-embedding-001 allows
    # ~100/min; with the empty-start bucket the worst-case 60s window is
    # RPM + sub_batch, so 80 + 10 = 90 stays safely under 100.  Raise
    # substantially on a billing-enabled key.
    RAG_BACKFILL_RPM: int = Field(80, ge=1, le=10000)
    # Texts per embed call / DB upsert; smaller = finer pacing + tighter error
    # isolation, larger = fewer DB commits.
    RAG_BACKFILL_SUB_BATCH: int = Field(10, ge=1, le=100)
    # Backfill lookback.  RAG retrieval only queries the last RAG_WINDOW_HOURS
    # (24h), so embedding deep history is wasted quota.  Default 3 days = the
    # retrieval window with 3x margin; widen explicitly only for special cases.
    RAG_BACKFILL_WINDOW_DAYS: int = Field(3, ge=1, le=36500)
    # How often the lifespan RAG refresh worker wakes to embed new news events.
    # Default 4h: ~130 new events per cycle at the observed 33 events/h ingest
    # rate → ~780 embed texts/day, safely under the free-tier 1000/day cap.
    # Lower to 1–2h on a billing-enabled key for near-real-time corpus freshness.
    RAG_REFRESH_INTERVAL_HOURS: int = Field(4, ge=1, le=168)

    # ── RAG Batch Embedding (Gemini Batch API — paid tier only) ───────────────
    # Enables 50% cost savings on corpus ingestion ($0.075 vs $0.15 per 1M
    # tokens) by routing backfill embedding through the Gemini async Batch API.
    # The Batch API returns results in 30–90 minutes (SLO: 24 h), so new
    # articles are not retrievable until the job completes.  Set to True only
    # on billing-enabled API keys; free-tier keys do not support Batch API.
    RAG_BATCH_EMBED_ENABLED: bool = Field(False)
    # Max texts submitted in one batch job.  Practical ceiling is 5 000 (avoids
    # 20 MB inline limit and 429s on large file uploads).  Corpus exceeding
    # this will be split into multiple sequential jobs automatically.
    RAG_BATCH_JOB_SIZE: int = Field(2000, ge=10, le=5000)
    # Polling cadence while waiting for a batch job to complete.  30 s is the
    # recommended minimum — polling more frequently does not speed up the job.
    RAG_BATCH_POLL_INTERVAL_SECS: int = Field(30, ge=10, le=300)
    # Hard timeout per batch job.  3 h is well inside the 48 h expiry window
    # but short enough to surface hung jobs promptly.
    RAG_BATCH_JOB_TIMEOUT_SECS: int = Field(10_800, ge=300, le=172_800)

    # ── Observability — Explanation-pipeline diagnostics log ──────────────────
    # When enabled, routes the explanation worker + LLM client + RAG loggers at
    # DEBUG into a dedicated rotating file, isolated from the multi-WebSocket
    # console firehose.  Console still shows INFO+ for these loggers; the file
    # additionally captures the full DEBUG trace (RAG retrieval + NIM embedding
    # timing, provider/stream events).  Off by default; purely additive.
    EXPLANATION_PIPELINE_LOG_ENABLED: bool = Field(
        False,
        description="Write explanation-pipeline DEBUG logs to a dedicated rotating file",
    )
    EXPLANATION_PIPELINE_LOG_PATH: str = Field(
        "logs/explanation_pipeline.log",
        description="Path for the explanation-pipeline diagnostics log file",
    )

    # ── Intelligence Layer — Groq (development / staging LLM provider) ───────────
    # Groq serves Qwen3-32B via custom LPU silicon: ~300ms TTFT, 60 RPM free tier,
    # OpenAI-compatible API.  Slot 2 in the provider chain — activated when
    # NVIDIA_NIM_API_KEY is unset (dev) or NIM is unreachable (failover).
    #
    # Obtain free API key (no credit card): console.groq.com/keys
    # Add GROQ_API_KEY to .env to activate.  Leave unset to skip Groq.
    #
    # Model: qwen/qwen3-32b — finance-capable, same Qwen3 family as NIM.
    # Ref: console.groq.com/docs/model/qwen/qwen3-32b
    GROQ_API_KEY: str | None = None
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_MODEL: str = "qwen/qwen3-32b"

    # ── Ollama (local fallback LLM provider — always last in chain) ───────────────
    # Requires Ollama running locally: ollama serve
    # Pull required models: ollama pull llama3.1:8b && ollama pull nomic-embed-text
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.1:8b"
    OLLAMA_TIMEOUT: int = Field(30, ge=5, le=120)
    OPENAI_API_KEY: str | None = None

    # ── Safety Layer ───────────────────────────────────────────────────────────
    LOSS_LIMIT_THRESHOLD: float = Field(0.05, ge=0.01, le=0.2)
    VOLATILITY_SPIKE_MULTIPLIER: float = Field(3.0, ge=1.5, le=10.0)
    SIGNAL_FREQUENCY_THRESHOLD: int = Field(100, ge=10, le=1000)

    # ── Worker Loops ───────────────────────────────────────────────────────────
    REGIME_IDLE_SLEEP_SECONDS: int = Field(
        60,
        ge=30,
        le=300,
        description="[Deprecated — no longer used by regime detection loop]",
    )
    REGIME_DETECTION_HOUR_IST: int = Field(
        16,
        ge=15,
        le=20,
        description=(
            "Hour of day (IST, 24h clock) at which the daily post-market "
            "regime detection run is triggered. Default 16 = 16:00 IST, "
            "30 min after NSE close (15:30). Must be ≥15 and ≤20."
        ),
    )
    DRIFT_CHECK_INTERVAL_SECONDS: int = Field(300, ge=60, le=3600)
    SAFETY_CHECK_INTERVAL_SECONDS: int = Field(30, ge=10, le=300)

    # ── Data Ingestion Worker ──────────────────────────────────────────────────
    DATA_INGESTION_ENABLED: bool = Field(True, description="Enable automated OHLCV data ingestion")
    DATA_INGESTION_CHECK_INTERVAL: int = Field(3600, ge=60, description="Maintenance gap-check interval (seconds)")
    DATA_INGESTION_REQUESTS_PER_MINUTE: int = Field(40, ge=10, le=490, description="API rate limit budget — 40/min (1 req every 1.5s) stays comfortably below Cloudflare's burst threshold on the historical-candle endpoint")
    DATA_INGESTION_CONCURRENCY: int = Field(1, ge=1, le=20, description="Parallel in-flight API requests — 1 = fully serial, eliminates all request bursting, safest for Cloudflare-guarded endpoints")
    DATA_INGESTION_BACKFILL_ENABLED: bool = Field(True, description="Run Phase 1 historical backfill on startup")
    DATA_INGESTION_MAX_RETRIES: int = Field(3, ge=1, le=10, description="Max retries per failed chunk before DLQ")
    DATA_INGESTION_CIRCUIT_BREAKER_THRESHOLD: int = Field(5, ge=3, le=20, description="Consecutive failures before circuit opens")
    DATA_INGESTION_CIRCUIT_BREAKER_TIMEOUT: int = Field(300, ge=60, le=3600, description="Seconds before circuit attempts recovery")
    BULK_INSERT_BATCH_SIZE: int = Field(1000, ge=100, le=10000, description="Database bulk insert batch size")

    # ── ML System ──────────────────────────────────────────────────────────────
    # Model storage: plaintext binary artifacts with SHA-256 integrity verification
    # at load time (see registry_loader._sha256_file). No encryption-at-rest is
    # applied; access control is enforced at the filesystem and infrastructure level.
    ML_MODEL_STORAGE_PATH: str = "backend/ml_models"
    ML_FEATURE_VERSION: str = "1.0.0"
    ML_EVALUATION_THRESHOLD: float = Field(0.85, ge=0.0, le=1.0)
    ML_LATENCY_TARGET_MS: int = Field(250, ge=50, le=1000)
    ML_BATCH_SIZE: int = Field(32, ge=1, le=128)
    ML_MAX_MODEL_VERSIONS: int = Field(10, ge=3, le=50)
    ML_DRIFT_THRESHOLD_SIGMA: float = Field(2.0, ge=1.0, le=5.0)
    ML_ACCURACY_ALERT_THRESHOLD: float = Field(0.83, ge=0.5, le=1.0)

    # ── Market Feed ────────────────────────────────────────────────────────────
    MARKET_FEED_THROTTLE_MS: int = Field(
        default=250,
        ge=100,
        le=5000,
        description="Per-instrument tick throttle (ms) before publishing to Redis",
    )

    # ── Rate Limiting ──────────────────────────────────────────────────────────
    RATE_LIMIT_SCANNER: str = "10/minute"
    RATE_LIMIT_SEARCH: str = "60/minute"
    RATE_LIMIT_MARKET_DATA: str = "120/minute"
    RATE_LIMIT_ML_PREDICTION: str = "100/minute"

    # ── Caching TTLs ───────────────────────────────────────────────────────────
    CACHE_TTL_SCANNER_OPEN:   int = 30    # seconds during market hours — prices update each tick
    CACHE_TTL_SCANNER_CLOSED: int = 900   # seconds off-hours — data is static between sessions
    CACHE_TTL_INSTRUMENTS: int = 300
    CACHE_TTL_LIVE_QUOTE: int = 1
    CACHE_TTL_MARKET_STATUS: int = 30
    ML_CACHE_TTL_DAILY: int = 300
    ML_CACHE_TTL_WEEKLY: int = 3600
    
    # API Response Caching
    CACHE_TTL_SUGGESTIONS_LIST: int = Field(30, ge=5, le=300, description="Cache TTL for suggestions list endpoint (seconds)")
    CACHE_TTL_SUGGESTIONS_DETAIL: int = Field(60, ge=5, le=300, description="Cache TTL for suggestions detail endpoint (seconds)")
    ENABLE_API_RESPONSE_CACHING: bool = Field(True, description="Enable API response caching")

    # ── Observability ──────────────────────────────────────────────────────────
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    SENTRY_DSN: str | None = None

    # ── NSE Market ─────────────────────────────────────────────────────────────
    NSE_HOLIDAY_API_URL: str = "https://www.nseindia.com/api/holiday-master?type=trading"
    NSE_HOLIDAY_CACHE_FILE: str = "backend/.cache/nse_holidays.json"
    NSE_MARKET_OPEN_IST: str = "09:15"
    NSE_MARKET_CLOSE_IST: str = "15:30"

    # ── Fundamentals Refresh Scheduler ─────────────────────────────────────────
    # Times are 24h HH:MM in IST.
    # DAILY_RATIOS: key-ratios endpoint runs post-market-close every trading day.
    # CORP_ACTIONS: corporate-actions runs each evening (no trading-day gate).
    # NIGHTLY_UNIVERSE: full 9-endpoint refresh of all EQ instruments at this time.
    # PRIORITY_LOOKBACK_DAYS: instruments with a signal in this many days form Tier 1.
    FUNDAMENTALS_DAILY_RATIOS_TIME_IST:    str = "15:45"
    FUNDAMENTALS_CORP_ACTIONS_TIME_IST:    str = "18:30"
    FUNDAMENTALS_NIGHTLY_UNIVERSE_TIME_IST: str = "01:00"
    FUNDAMENTALS_PRIORITY_LOOKBACK_DAYS:   int = Field(7, ge=1, le=30)

    # ── Signal Scheduler ───────────────────────────────────────────────────────
    # Nifty 50 + Nifty Next 50 — scheduled signal generation every 15 minutes
    # during NSE market hours.  Override via env var as a comma-separated string.
    SIGNAL_SCHEDULED_UNIVERSE: list[str] = [
        # ── Nifty 50 ──────────────────────────────────────────────────────────
        "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
        "HINDUNILVR", "SBIN", "BAJFINANCE", "BHARTIARTL", "KOTAKBANK",
        "ITC", "LT", "HCLTECH", "AXISBANK", "ASIANPAINT",
        "MARUTI", "ULTRACEMCO", "NESTLEIND", "TITAN", "WIPRO",
        "SUNPHARMA", "BAJAJFINSV", "ONGC", "NTPC", "POWERGRID",
        "TECHM", "TATAMOTORS", "JSWSTEEL", "TATASTEEL", "HINDALCO",
        "ADANIENT", "ADANIPORTS", "DRREDDY", "DIVISLAB", "CIPLA",
        "BAJAJ-AUTO", "HEROMOTOCO", "COALINDIA", "BPCL", "INDUSINDBK",
        "EICHERMOT", "GRASIM", "BRITANNIA", "APOLLOHOSP", "TATACONSUM",
        "PIDILITIND", "SHREECEM", "HDFCLIFE", "SBILIFE", "UPL",
        # ── Nifty Next 50 ─────────────────────────────────────────────────────
        "HAVELLS", "GODREJCP", "BERGEPAINT", "COLPAL", "MARICO",
        "LUPIN", "BIOCON", "TORNTPHARM", "DABUR", "VOLTAS",
        "MPHASIS", "COFORGE", "LTIM", "PERSISTENT", "TATAELXSI",
        "CANBK", "BANKBARODA", "PNB", "FEDERALBNK", "IDFCFIRSTB",
        "LICI", "NAUKRI", "ZOMATO", "IRCTC", "HAL",
        "BEL", "TRENT", "VEDL", "NMDC", "SAIL",
        "GAIL", "IOC", "HPCL", "ATGL", "TORNTPOWER",
        "SIEMENS", "ABB", "CHOLAFIN", "BAJAJHLDNG", "PIIND",
        "CONCOR", "OFSS", "MUTHOOTFIN", "ABCAPITAL", "MFSL",
        "ICICIGI", "POLICYBZR", "PAYTM", "NYKAA", "DELHIVERY",
    ]
    SIGNAL_SCHEDULER_INTERVAL_MINUTES: int = Field(15, ge=5, le=60)
    SIGNAL_SCHEDULER_FEATURE_CONCURRENCY: int = Field(20, ge=5, le=50)
    SIGNAL_SCHEDULER_ASSEMBLY_CONCURRENCY: int = Field(10, ge=2, le=20)
    SIGNAL_ON_DEMAND_CACHE_TTL: int = Field(900, ge=60, le=3600)  # 15 minutes

    # ── Symbol Validation ──────────────────────────────────────────────────────
    ENABLE_SYMBOL_VALIDATION: bool = Field(
        True,
        description=(
            "Validate symbols against instrument_master before signal assembly. "
            "Symbols not found as active NSE instruments (series EQ/BE/BZ/SM/ST) "
            "are rejected with a clear error. "
            "Disable only for emergency operations or integration testing."
        ),
    )

    # ── Computed Properties ────────────────────────────────────────────────────

    @property
    def gemini_api_key_pool(self) -> list[str]:
        """Ordered, deduplicated list of all configured Gemini API keys (primary first).

        Single-key deployments (only GEMINI_API_KEY set) return a 1-element list
        and behave identically to the pre-multi-key implementation.  Returns an
        empty list when no keys are configured — callers should degrade gracefully.
        """
        seen: set[str] = set()
        pool: list[str] = []
        for k in ([self.GEMINI_API_KEY] if self.GEMINI_API_KEY else []) + list(self.GEMINI_API_KEYS):
            if k and k not in seen:
                seen.add(k)
                pool.append(k)
        return pool

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def docs_url(self) -> str | None:
        return None if self.is_production else "/docs"

    @property
    def redoc_url(self) -> str | None:
        return None if self.is_production else "/redoc"

    @property
    def cors_origins_str(self) -> list[str]:
        return [str(origin).rstrip("/") for origin in self.CORS_ALLOWED_ORIGINS]

    # ── Validators ─────────────────────────────────────────────────────────────
    @field_validator("SECRET_KEY")
    @classmethod
    def secret_key_must_not_be_default(cls, v: str) -> str:
        forbidden = {"your-secret-key-change-in-production", "your-secret-key", "secret", "changeme"}
        if v.lower() in forbidden:
            raise ValueError("SECRET_KEY must not be a placeholder. Generate with: openssl rand -hex 32")
        return v

    @field_validator("INTERNAL_API_SECRET")
    @classmethod
    def internal_secret_must_not_be_placeholder(cls, v: str) -> str:
        forbidden = {"changeme", "secret", "your-internal-secret", "change-me", "placeholder"}
        if v.lower() in forbidden or v.startswith("<") or v.startswith("REPLACE"):
            raise ValueError(
                "INTERNAL_API_SECRET must not be a placeholder. "
                "Generate with: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v

    @model_validator(mode="after")
    def wildcard_cors_is_forbidden(self) -> "Settings":
        for origin in self.cors_origins_str:
            if origin == "*":
                raise ValueError("Wildcard CORS origin '*' is forbidden. Specify explicit origins.")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings singleton."""
    return Settings()
