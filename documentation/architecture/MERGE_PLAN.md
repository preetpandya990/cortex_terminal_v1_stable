**CORTEX AI**

**UNIFIED PLATFORM MERGE PLAN**

*ML Engine + AI Intelligence Layer → Production-Grade Merger*

  ----------------------- -----------------------------------------------
  **Document Version**    1.0 --- Final

  **Classification**      Engineering Internal

  **Date**                April 2026

  **Scope**               Full-stack merge: FastAPI backend, Next.js
                          frontend, DB, infra

  **Codebase A**          Cortex AI --- Full ML Implemented (File 1)

  **Codebase B**          Cortex AI --- AI as Microservice (File 2)

  **Output**              Cortex AI Unified --- new merged codebase
  ----------------------- -----------------------------------------------

**1. Executive Summary**

This document is the definitive, file-by-file technical plan for merging
the Cortex AI ML codebase (File 1) and the Cortex AI Intelligence
Microservice (File 2) into a single, world-class production platform. It
covers every architectural decision, every conflict resolution, every
migration step, and every line-of-code implication across the full
stack.

The merged system will be a single FastAPI application --- not two
services --- with a dedicated async worker process launched and torn
down alongside the API server. The AI intelligence layer (NLP, regime
detection, signal fusion, safety) is not a microservice calling the ML
layer over HTTP. It is co-located in the same Python process, sharing
the same database engine, Redis client, and configuration singleton,
with clean internal module boundaries.

**Architecture summary**

  ------------------- ------------------------ --------------------------
  **Concern**         **Decision**             **Detail**

  Identity & Auth     ML\'s JWT +              AI\'s RBAC (Viewer /
                      refresh-token-rotation   Trader / Admin) layered on
                      system                   top of JWT claims

  Database            Unified async SQLAlchemy Single TimescaleDB engine
                      2.0 (asyncpg)            --- all sync sessions in
                                               AI services rewritten

  Background work     Separate async worker    Spawned by API lifespan,
                      process                  torn down on shutdown via
                                               SIGTERM → SIGKILL
                                               escalation

  LLM inference       Ollama (Llama 3.1 8B,    Primary for event
                      local GPU)               classification + fake-news
                                               layer 4; no cloud provider

  ML governance       AI governance layer owns shadow → paper → live
                      model lifecycle          progression; ML\'s
                                               evaluation gates enforce
                                               quality gates before
                                               promotion

  TimescaleDB         All time-series tables   Unified migration chain;
                      as hypertables           compression + retention
                                               policies on all applicable
                                               tables
  ------------------- ------------------------ --------------------------

**2. Architectural Decisions & Rationale**

All six architectural questions are answered below. Each decision is
final and drives the file-by-file work in Section 4.

**2.1 Authentication: JWT Identity + RBAC Permissions**

The merged system uses ML\'s JWT implementation as the sole identity
layer and layers AI\'s RBAC permission model directly on top. The JWT
payload is extended to carry the user\'s role claim. No separate API key
store is used in production.

  -------------------- ------------- ----------------------------------------
  **Component**        **Action**    **Detail**

  Token creation       Unchanged     create_token_pair() still issues
                       from ML       access + refresh tokens; role is
                                     embedded in JWT payload as claim
                                     \'role\'

  Token validation     Unchanged     HTTPBearer + HTTPOnly cookie, family
                       from ML       tracking, Redis revocation list

  RBAC check           New ---       require_role(UserRole.X) and
                       merged from   require_permissions(\[\...\]) read the
                       AI            \'role\' claim from the decoded JWT
                                     payload rather than from an API key
                                     store

  AI                   Rewritten     Hardcoded API_KEYS dict removed.
  middleware/auth.py                 get_current_user() from ML\'s deps.py
                                     becomes the single user extraction
                                     point. Roles: viewer, trader, admin.

  Permission registry  Kept from AI  ROLE_PERMISSIONS dict is retained; maps
                                     role strings to permission strings like
                                     \'write:kill_switch\'
  -------------------- ------------- ----------------------------------------

+---+---------------------------------------------------------------------+
| * | The AI codebase\'s in-memory API key store (dev-viewer-key-12345,   |
| * | etc.) is completely removed --- not migrated. It is a development   |
| ⚠ | stub incompatible with billion-dollar production requirements.      |
| * |                                                                     |
| * | In production, role assignment happens at user registration or via  |
|   | admin promotion. The JWT encodes the role so every request is       |
|   | self-contained --- no DB round-trip for auth.                       |
+---+---------------------------------------------------------------------+

**2.2 Database: Full Async Migration of All AI Services**

Every AI service currently uses synchronous SQLAlchemy sessions
(psycopg2 + Session). These will be fully rewritten to use AsyncSession
with asyncpg. This is not optional --- mixing sync DB calls inside an
async FastAPI event loop degrades throughput from \~1,400 req/s to \~550
req/s (async overhead without async benefit).

The migration pattern for every AI service is mechanical and consistent:

  ---------------- --------------------------------------- ----------------------------------------
  **Concern**      **Before (AI, sync)**                   **After (merged, async)**

  Import           from sqlalchemy.orm import Session      from sqlalchemy.ext.asyncio import
                                                           AsyncSession

  Dependency       db: Session = Depends(get_db)           db: AsyncSession = Depends(get_db)
  injection                                                

  Query execution  result = db.execute(stmt)               result = await db.execute(stmt)

  Scalar fetch     db.execute(stmt).scalar_one_or_none()   await
                                                           db.execute(stmt).scalar_one_or_none()
                                                           \[or .scalars().first()\]

  Commit           db.commit() / db.rollback()             await db.commit() / await db.rollback()

  Session close    db.close() in finally block             Handled automatically by async context
                                                           manager in get_db()

  Background       SessionLocal() sync sessions            AsyncSession via async_sessionmaker ---
  worker                                                   worker uses its own async engine
                                                           instance

  Alembic env.py   Sync migration runner                   Async runner: run_async_migrations()
                                                           with
                                                           connection.run_sync(do_run_migrations)
  ---------------- --------------------------------------- ----------------------------------------

+---+---------------------------------------------------------------------+
| * | WARNING --- greenlet bridge tax: SQLAlchemy\'s async ORM uses a     |
| * | greenlet bridge internally. Per production benchmarks, async ORM is |
| ! | 1.5--2.5× slower per query than raw asyncpg. This is the            |
| * | irreducible cost. Mitigation: use select() with explicit column     |
| * | lists (avoid SELECT \*), disable statement_cache_size for PgBouncer |
|   | compatibility if used, and prefer bulk operations over row-by-row   |
|   | loops.                                                              |
|   |                                                                     |
|   | All 20+ AI services must be rewritten. The conversion is mechanical |
|   | but must not be rushed --- each service needs testing               |
|   | post-conversion.                                                    |
+---+---------------------------------------------------------------------+

**2.3 ML Prediction Engine: Full ONNX Implementation**

The AI codebase\'s services/ml_prediction_engine.py is a stub using
simple moving averages. It is replaced entirely by the ML codebase\'s
full prediction stack: ONNX Runtime inference engine, multi-timeframe
ensemble, SHAP explainability, feature store, and evaluation gates.

The AI\'s signal_assembler.py references ML predictions as an input to
signal fusion (ml_weight: 0.4). This connection is implemented via a
direct function call to the ML inference module --- not an HTTP call.
The signal assembler imports from app.ml.inference.prediction_engine and
calls it directly.

  -------------------------------------- ------------ ---------------------------------------
  **File / Component**                   **Action**   **Detail**

  app/services/ml_prediction_engine.py   DELETE       Replaced by
                                                      app/ml/inference/prediction_engine.py
                                                      (ONNX Runtime)

  app/ml/ (entire directory)             KEEP ---     ensemble/, evaluation/, features/,
                                         from File 1  inference/, monitoring/, training/,
                                                      audit/, model_registry.py,
                                                      feature_store.py

  signal_assembler.py ml_weight path     REWRITE      Calls PredictionEngine.predict()
                                                      directly; returns confidence-weighted
                                                      prediction for signal fusion

  governance.py (AI)                     EXTEND       AI\'s model lifecycle
                                                      (shadow/paper/live) wraps ML\'s
                                                      development/staging/production
                                                      lifecycle; gates enforced before
                                                      promotion

  model_registry.py                      UNIFIED      One registry class combining ML\'s
                                                      encryption + artifact storage with
                                                      AI\'s state machine (shadow→paper→live)
  -------------------------------------- ------------ ---------------------------------------

**2.4 TimescaleDB: Universal Hypertable Strategy**

All time-series tables across both codebases are converted to
TimescaleDB hypertables. The production Docker image uses
timescale/timescaledb-ha:pg16 (the high-availability variant with all
extensions included).

  ----------------------- --------------------- ----------- ------------ ---------------------
  **Table**               **Time Column**       **Chunk**   **Compress   **segmentby /
                                                            After**      orderby**

  upstox_raw_ticks        event_timestamp       1 day       7 days       instrument_key /
                                                                         event_timestamp DESC

  upstox_minute_candles   bucket_start          7 days      30 days      instrument_key /
                                                                         bucket_start DESC

  stock_ohlcv             timestamp             7 days      30 days      symbol, exchange,
                                                                         timeframe / timestamp
                                                                         DESC

  technical_indicators    timestamp             7 days      30 days      symbol, timeframe /
                                                                         timestamp DESC

  ml_drift_metrics        timestamp             7 days      90 days      model_id / timestamp
                                                                         DESC

  ai_raw_events           event_timestamp       1 day       30 days      source_type /
                                                                         event_timestamp DESC

  ai_processed_events     processed_timestamp   1 day       30 days      (no segmentby) /
                                                                         processed_timestamp
                                                                         DESC

  ai_drift_reports        report_date           7 days      180 days     model_id /
                                                                         report_date DESC

  ai_regime_detections    detection_timestamp   1 day       90 days      symbol /
                                                                         detection_timestamp
                                                                         DESC

  ai_trading_signals      signal_timestamp      1 day       90 days      symbol /
                                                                         signal_timestamp DESC
  ----------------------- --------------------- ----------- ------------ ---------------------

+---+---------------------------------------------------------------------+
| * | Alembic known issue: TimescaleDB auto-creates an index on the time  |
| * | column when create_hypertable() is called. Alembic autogenerate     |
| ⚠ | will try to DROP this index on next migration. Fix: add an          |
| * | exclude_by_name filter in alembic/env.py that excludes              |
| * | TimescaleDB-generated index names (format:                          |
|   | {table}\_{time_col}\_idx).                                          |
|   |                                                                     |
|   | Compressed chunks are immutable. Schema migrations on hypertables   |
|   | with compressed chunks require: 1) remove compression policy, 2)    |
|   | decompress all chunks (SELECT decompress_chunk(c) FROM              |
|   | show_chunks(\...)), 3) run ALTER TABLE, 4) re-enable compression +  |
|   | policy. Build this into migration scripts for any hypertable schema |
|   | change.                                                             |
|   |                                                                     |
|   | Non-time-series tables (ml_predictions, ml_audit_logs,              |
|   | ai_ml_models, ai_source_credibility, users, etc.) remain as regular |
|   | PostgreSQL tables --- no hypertable conversion needed.              |
+---+---------------------------------------------------------------------+

**2.5 LLM Inference: Ollama Local GPU (Llama 3.1 8B)**

Ollama runs locally with GPU acceleration. It is used in two places: (1)
EventClassifier --- primary classification of financial events, with
GPT-4o as a confidence-gated fallback. (2) FakeNewsDetector layer 4 ---
LLM reasoning over content for inconsistency detection.

The Ollama client is initialized once at startup and injected as a
singleton into both services. No cloud LLM provider is used. The
OPENAI_API_KEY fallback in EventClassifier is kept but only fires when
Ollama returns confidence \< 0.7 --- it is optional in production; if
the key is absent the system falls back to rule-based classification.

  ------------------ ------------------- ------------------------------------------------
  **Concern**        **Value**           **Implementation note**

  Model              llama3.1:8b         Pull on first run: ollama pull llama3.1:8b in
                                         Dockerfile

  GPU requirement    NVIDIA GPU required docker-compose:
                     in prod             deploy.resources.reservations.devices: nvidia /
                                         count: all

  Ollama startup     Health probe in     App lifespan calls ollama.list() and logs model
  check              lifespan            availability before accepting traffic

  EventClassifier    ollama_model =      confidence_threshold = 0.7; openai_client
  init               \'llama3.1:8b\'     optional (OPENAI_API_KEY env var)

  FakeNewsDetector   \_call_llm() stub → Calls ollama.chat() with structured prompt;
  layer 4            real impl           parses JSON response {is_suspicious, reasoning}

  Async client       ollama library is   Wrap ollama calls in
                     sync                asyncio.get_event_loop().run_in_executor(None,
                                         \...) in background worker context only ---
                                         never in API request path
  ------------------ ------------------- ------------------------------------------------

**2.6 Worker Process: Separate Process, Unified Lifecycle**

The worker runs as a separate OS process --- not a thread, not an
in-process asyncio task. It is spawned inside the API\'s lifespan
context manager using asyncio.create_subprocess_exec(). When the API
shuts down, the lifespan teardown sends SIGTERM to the worker, waits up
to 30 seconds for clean shutdown, then escalates to SIGKILL.

The worker hosts all background loops: RSS ingestion, regime detection,
drift detection, safety monitoring. It has its own async event loop, its
own database connection pool (separate asyncpg pool --- does not share
the API\'s pool), and its own Redis connection.

  ---------------- ---------------------------------- ------------------------------------------------
  **Concern**      **Value**                          **Detail**

  Entry point      backend/app/worker.py              asyncio.run(worker_main()) --- standalone
                                                      invocable; also spawned by API

  Spawn mechanism  asyncio.create_subprocess_exec()   In API lifespan, pre-yield: proc = await
                                                      asyncio.create_subprocess_exec(sys.executable,
                                                      \'-m\', \'app.worker\', \...)

  Shutdown         SIGTERM → wait 30s → SIGKILL       Post-yield: proc.terminate(); await
                                                      asyncio.wait_for(proc.wait(), 30); if still
                                                      alive: proc.kill()

  Worker signal    asyncio loop.add_signal_handler    SIGTERM sets asyncio.Event(); all loops check
  handling                                            the event and drain cleanly before exit

  DB pool          Separate instance                  Worker creates its own create_async_engine() ---
                                                      does not import API\'s engine singleton

  Redis            Separate connection                Worker calls get_redis_client() which creates
                                                      its own pool; does not share API\'s Redis
                                                      connection

  Health reporting Redis heartbeat key                Worker sets worker:heartbeat key every 30s; API
                                                      health endpoint reads it to report worker status

  Log routing      stdout/stderr inherited            subprocess stdout/stderr pipe to API process;
                                                      structured JSON lines appear in unified log
                                                      stream
  ---------------- ---------------------------------- ------------------------------------------------

**3. Unified Repository Structure**

The merged codebase uses File 1\'s (ML) directory structure as the
canonical layout. File 2\'s (AI) flat structure is reorganized into it.
The result is one backend/ tree with a clear layer separation.

**3.1 Backend directory tree**

  ------------------------------ --------------------------- ----------------------
  **Path**                       **Contents**                **Source**

  backend/app/core/              Config, DB engine, Redis,   From ML ---
                                 security, JWT, exception    authoritative
                                 handlers, limiter           

  backend/app/api/v1/            All HTTP + WebSocket routes Merged: ML routes +
                                                             all AI routes

  backend/app/ml/                Full ML pipeline            From ML --- kept
                                                             entirely: ensemble,
                                                             evaluation, features,
                                                             inference, monitoring,
                                                             training, audit

  backend/app/ai/                AI intelligence layer       NEW directory --- AI
                                                             services reorganized
                                                             here (was
                                                             app/services/ in File
                                                             2)

  backend/app/ai/ingestion/      RSS fetcher, filing         From AI --- moved into
                                 scraper, social media       ai/ namespace
                                 monitor                     

  backend/app/ai/intelligence/   NLP engine, event           From AI --- moved +
                                 classifier, credibility     async rewrite
                                 scorer, fake news detector  

  backend/app/ai/fusion/         Signal assembler, signal    From AI --- moved +
                                 pipeline, confidence        async rewrite + ML
                                 calibrator, regime detector integration

  backend/app/ai/safety/         Kill switch manager, safety From AI --- moved +
                                 trigger engine, health      async rewrite
                                 monitor                     

  backend/app/ai/governance/     Unified model registry      Merged from both ---
                                 (ML + AI state machine),    new unified class
                                 drift detector              

  backend/app/ai/strategy/       Strategy orchestrator       From AI --- moved +
                                                             async rewrite

  backend/app/models/            All SQLAlchemy ORM models   Merged: ML models + AI
                                                             cai_models + types

  backend/app/schemas/           All Pydantic schemas        Merged: ML schemas +
                                                             AI cai_schemas +
                                                             analysis

  backend/app/services/          Market-facing services      From ML:
                                                             upstox_client,
                                                             tick_stream,
                                                             data_ingestion,
                                                             market_scanner,
                                                             hawk_eye, indicators

  backend/app/middleware/        HTTP middleware             From AI:
                                                             correlation_id,
                                                             metrics, rate_limit +
                                                             ML\'s security (JWT)

  backend/app/worker.py          Standalone worker entry     Refactored from AI +
                                 point                       new spawn/signal
                                                             handling

  backend/alembic/               Unified migration chain     Sequential 0001--000N;
                                                             ML migrations first,
                                                             AI migrations
                                                             renumbered, hypertable
                                                             migration added
  ------------------------------ --------------------------- ----------------------

**3.2 Frontend directory**

The ML frontend is the canonical base. AI frontend additions are merged
in. There are no conflicting component names --- the sets are disjoint
(ML has PredictionBadge/PredictionPanel; AI has SignalsPanel,
RegimePanel, EventsPanel, AIAnalysisCard, etc.).

  -------------------- ------------------------------ --------------------
  **Path**             **Contents**                   **Action**

  src/components/ml/   PredictionBadge.tsx,           From ML --- kept
                       PredictionPanel.tsx            as-is

  src/components/      AIAnalysisCard,                From AI --- added
                       AnalysisCardsSection,          
                       CAIRealtimeDemo,               
                       ConnectionStatus, EventsPanel, 
                       MLAnalysisCard, MLModelsPanel, 
                       RegimePanel,                   
                       SignalDetailModal,             
                       SignalsPanel,                  
                       SummaryVerdictCard             

  src/hooks/           useAIAnalysis,                 From AI --- added
                       useCAIWebSocket, useEvents,    
                       useEventsRealtime,             
                       useMLAnalysis, useModels,      
                       useModelsRealtime, useRegime,  
                       useRegimeRealtime, useSignals, 
                       useSignalsRealtime,            
                       useVerdictAnalysis             

  src/types/           ML types (ml.ts,               Merged --- no
                       hawk_eye.ts) + AI types        conflicts
                       (analysis.ts, events.ts,       
                       models.ts, regime.ts,          
                       signals.ts)                    

  src/app/             ML pages + AI pages            Merged --- additive
                       (cai-dashboard, cortex-ai)     

  src/lib/api.ts       Unified API client             Merge: ML
                                                      endpoints + AI
                                                      endpoints into one
                                                      typed client

  src/components/ui/   badge, button, card, table +   Merged --- additive
                       dialog, tabs (from AI)         
  -------------------- ------------------------------ --------------------

**4. File-by-File Conflict Resolution**

Every file that exists in both codebases or requires non-trivial merge
logic is addressed below. Files that are additive (exist only in one
codebase) are simply copied with namespace adjustment.

**4.1 backend/app/main.py**

  ------- ---------------------------------------------------------------------
  **→**   Resolution: Rewrite from scratch using ML\'s structure as the
          template. AI\'s on_event() hooks are ported into the lifespan context
          manager. Worker process spawn/teardown added to lifespan.

  ------- ---------------------------------------------------------------------

**Key changes from ML\'s main.py:**

-   Retain ML\'s asynccontextmanager lifespan pattern --- it is the
    correct modern FastAPI approach

-   Add worker process spawn: proc = await
    asyncio.create_subprocess_exec(sys.executable, \'-m\',
    \'app.worker\') pre-yield

-   Add worker teardown post-yield: proc.terminate() →
    asyncio.wait_for(proc.wait(), 30) → proc.kill() if still alive

-   Add Ollama health check to lifespan startup: verify llama3.1:8b is
    available via ollama.list()

-   Add all AI routers: ingestion, intelligence, governance, strategy,
    fusion, safety, cai_stream

-   Retain ML\'s full middleware stack: TrustedHostMiddleware,
    CORSMiddleware, GZipMiddleware, SlowAPIMiddleware

-   Add AI\'s middleware: CorrelationIdMiddleware, MetricsMiddleware
    (Prometheus) --- these stack before CORS

-   Add Prometheus /metrics endpoint from AI

-   Remove AI\'s deprecated \@app.on_event decorators entirely

-   Logging: use ML\'s python-json-logger (structured JSON) as the
    single logging backend; remove AI\'s structlog dependency

**Middleware order (outermost to innermost):**

-   CorrelationIdMiddleware --- track request IDs first

-   MetricsMiddleware --- measure all requests including auth failures

-   TrustedHostMiddleware --- only in production

-   CORSMiddleware --- with ML\'s strict allowlist (no wildcard)

-   GZipMiddleware --- compress responses

-   SlowAPIMiddleware --- rate limiting last (needs request already
    parsed)

-   RateLimitMiddleware from AI is REMOVED --- SlowAPI + Redis covers
    this

**4.2 backend/app/core/config.py**

  ------- ---------------------------------------------------------------------
  **→**   Resolution: ML\'s Settings class is the base. AI\'s settings fields
          are merged in as additional blocks. AI\'s flat config.py is deleted.

  ------- ---------------------------------------------------------------------

New setting groups added from AI (all with sensible defaults and field
validators):

-   Worker: ENABLE_BACKGROUND_TASKS: bool = True,
    WORKER_SHUTDOWN_TIMEOUT: int = 30

-   RSS ingestion: RSS_MIN_POLL_SECONDS, RSS_MAX_POLL_SECONDS,
    RSS_CONCURRENCY, RSS_POLL_JITTER_SECONDS, RSS_DEDUP_DISABLED

-   Regime detection: ACTIVE_SYMBOLS_REFRESH_SECONDS,
    REGIME_IDLE_SLEEP_SECONDS

-   Ollama: OLLAMA_BASE_URL: str = \'http://localhost:11434\',
    OLLAMA_MODEL: str = \'llama3.1:8b\', OLLAMA_TIMEOUT: int = 30

-   OpenAI fallback: OPENAI_API_KEY: str \| None = None (optional)

-   Prometheus: PROMETHEUS_ENABLED: bool = True

-   Upstox additions from AI: UPSTOX_INSTRUMENTS_URLS,
    UPSTOX_INSTRUMENTS_TTL_SECONDS,
    UPSTOX_INSTRUMENTS_STALE_WHILE_REVALIDATE_SECONDS, etc.

-   Cache paths: NSE_HOLIDAY_CACHE_FILE, UPSTOX_INSTRUMENTS_CACHE_FILE

-   Safety: LOSS_LIMIT_THRESHOLD: float = 10000.0,
    VOLATILITY_SPIKE_MULTIPLIER: float = 2.0,
    SIGNAL_FREQUENCY_THRESHOLD: int = 10

Removed from AI\'s config: AUTH_DISABLED (replaced by JWT), RUN_MODE
(replaced by worker subprocess), API_V1_PREFIX (hardcoded in routers as
per ML pattern), FRONTEND_URL (stays for CORS allowlist under
CORS_ALLOWED_ORIGINS).

**4.3 backend/app/core/database.py (unified)**

  ------- ---------------------------------------------------------------------
  **→**   Resolution: ML\'s async database.py is the single canonical database
          module. AI\'s sync database.py is deleted. AI\'s TimescaleDB
          hypertable + compression/retention logic is merged into ML\'s
          database.py init_db() function.

  ------- ---------------------------------------------------------------------

-   ML\'s create_async_engine() + async_sessionmaker + AsyncSession are
    kept exactly

-   AI\'s \_create_hypertables() + \_setup_compression_policies() are
    ported in --- converted to use conn.run_sync() where needed for
    asyncpg compatibility

-   AI\'s TimescaleDB extension check (CREATE EXTENSION IF NOT EXISTS
    timescaledb) is kept

-   AI\'s \_table_exists(), \_partition_column_in_pk_or_unique(),
    \_is_hypertable() helpers are merged in

-   AI\'s check_db_connection() is kept as a sync health check utility

-   AI\'s SessionLocal (sync) is removed --- the worker creates its own
    engine; there is no need for a sync session in the merged app

-   Alembic env.py is updated to use the async migration runner pattern:
    run_async_migrations() with connection.run_sync(do_run_migrations)

**4.4 Alembic Migrations --- Unified Chain**

  ------- ---------------------------------------------------------------------
  **→**   Resolution: A single linear migration chain. ML migrations retain
          their IDs. AI migrations are renumbered to follow ML\'s last
          migration. A new final migration adds hypertable conversions for
          ML\'s existing tables.

  ------- ---------------------------------------------------------------------

  ------------------------------ ------------------------------- ----------------------
  **Migration ID**               **Description**                 **Notes**

  0001_initial                   ML --- upstox tables,           Unchanged
                                 instruments                     

  0002_ml_system_tables          ML --- ml_drift_metrics,        Unchanged
                                 ml_predictions,                 
                                 ml_model_metadata               

  0003_add_user_tracking         ML --- user_id on               Unchanged
                                 ml_predictions, ml_audit_logs   

  0004_add_model_encryption      ML --- checksum, encrypted,     Unchanged
                                 status, lifecycle fields on     
                                 ml_model_metadata               

  0005_ticks_pk                  AI 0001 renumbered ---          down_revision =
                                 composite PK on                 \'0004\'
                                 upstox_raw_ticks                

  0006_instrument_master         AI 0002 renumbered ---          down_revision =
                                 instrument_master table         \'0005\'

  0007_cai_tables                AI 0003 renumbered --- all 13   down_revision =
                                 CAI tables                      \'0006\'

  0008_ingestion_sources         AI 0004 renumbered ---          down_revision =
                                 ai_ingestion_sources            \'0007\'

  0009_timescaledb_hypertables   NEW --- converts all applicable down_revision =
                                 tables to hypertables +         \'0008\'
                                 compression/retention policies  
  ------------------------------ ------------------------------- ----------------------

**Migration 0009 details:**

-   upgrade(): calls create_hypertable() for all tables in Section
    2.4\'s table. Wraps each in try/except with if_not_exists=True so
    reruns are safe.

-   upgrade(): calls enable compression + add_compression_policy() for
    each table after hypertable creation

-   upgrade(): calls add_retention_policy() for tables with retention
    requirements

-   downgrade(): drops retention policies, compression policies, and
    hypertables (via drop_table + recreate as regular tables for full
    reversibility)

-   Alembic env.py exclude_by_name filter: excludes all TimescaleDB
    auto-generated indexes from autogenerate diff to prevent false DROP
    INDEX migrations

**4.5 backend/requirements.txt**

  ------- ---------------------------------------------------------------------
  **→**   Resolution: Union of both. Version pins resolved in favor of the
          higher/newer version where compatible. Conflicting dependencies noted
          below.

  ------- ---------------------------------------------------------------------

  --------------------- ------------- ------------- ---------------------------
  **Package**           **ML          **AI          **Merged version / Notes**
                        version**     version**     

  fastapi               ML: 0.115.0   AI: 0.109.0   0.115.0 --- ML wins;
                                                    breaking changes in
                                                    0.109→0.115 are minimal

  uvicorn\[standard\]   ML: 0.32.0    AI: 0.27.0    0.32.0

  sqlalchemy            ML: 2.0.35    AI: 2.0.25    2.0.35

  pydantic              ML: 2.9.2     AI: 2.5.3     2.9.2

  numpy                 ML: 2.1.3     AI: 1.26.3    2.1.3 --- CRITICAL: verify
                                                    torch + transformers
                                                    compatibility with numpy
                                                    2.x

  redis\[hiredis\]      ML: 5.2.0     AI: 5.0.1 +   5.2.0 with hiredis bundled
                                      hiredis 2.3.2 

  httpx                 ML: 0.27.2    AI: 0.25.2    0.27.2 --- AI pinned 0.25.2
                                                    for ollama 0.1.6; update
                                                    ollama to latest which
                                                    supports 0.27+

  alembic               ML: 1.13.3    AI: 1.13.1    1.13.3

  structlog             AI only       ---           REMOVED --- replaced by
                                                    python-json-logger (ML\'s
                                                    choice, already configured)

  evidently             AI: 0.4.33    ---           Kept --- used by
                                                    drift_detector.py

  transformers          AI: 4.36.2    ---           Kept + verify numpy 2.x
                                                    compatibility

  torch                 AI: 2.1.2     ---           Kept --- required for
                                                    FinBERT; GPU variant if
                                                    CUDA available

  ollama                AI: 0.1.6     ---           Update to latest stable (≥
                                                    0.3.x supports async)

  spacy                 AI: 3.7.2     ---           Kept + en_core_web_sm model
                                                    download in Dockerfile

  prometheus-client     AI: 0.19.0    ---           Kept

  tenacity              AI: 0.8.2.3   ---           Kept --- used by
                                                    redis_client retry logic

  feedparser,           AI only       ---           Kept --- required for RSS
  beautifulsoup4, lxml,                             ingestion
  aiohttp                                           

  cryptography          ML only       ---           Kept --- required for model
                        (Fernet)                    artifact encryption

  python-json-logger    ML: 2.0.7     ---           Kept --- single logging
                                                    backend

  slowapi               ML: 0.1.9     ---           Kept --- rate limiting

  PyJWT                 ML: 2.10.0    ---           Kept --- JWT auth
  --------------------- ------------- ------------- ---------------------------

**4.6 docker-compose.yml**

  ------- ---------------------------------------------------------------------
  **→**   Resolution: ML\'s compose is the base. AI\'s additions (Ollama
          service, TimescaleDB image, Prometheus, network) are merged in.
          Worker process runs inside the API container --- no separate
          container.

  ------- ---------------------------------------------------------------------

-   DB image: timescale/timescaledb-ha:pg16 replaces postgres:16-alpine

-   Ollama service added with GPU reservation (nvidia, count: all) and
    health check

-   API service depends_on: db (healthy) + redis (healthy) + ollama
    (healthy)

-   API container entrypoint: alembic upgrade head && uvicorn
    app.main:app --- worker is spawned inside the Python process, not a
    separate container

-   Prometheus service added for metrics scraping (prometheus.yml
    mounted)

-   All services on cortex-network bridge network

-   Volumes: postgres_data, redis_data, ollama_data, ml_models (for
    encrypted ONNX artifacts)

-   Hardcoded passwords removed --- all secrets via env_file: .env with
    explicit .env.example

-   ML .env structure retained (SECRET_KEY, DATABASE_URL, UPSTOX\_\*,
    CORS_ALLOWED_ORIGINS, ML\_\*)

-   New env vars added: OLLAMA_BASE_URL, OLLAMA_MODEL, OPENAI_API_KEY
    (optional)

**4.7 services/market_scanner.py + services/indicators.py**

  ------- ---------------------------------------------------------------------
  **→**   Resolution: ML\'s versions are canonical. AI\'s versions are compared
          for any additions not present in ML, then ML\'s files are adopted
          unchanged.

  ------- ---------------------------------------------------------------------

-   ML\'s MarketScannerService uses AsyncSession with a single bulk JOIN
    query against TimescaleDB (stock_ohlcv + instrument_master). This is
    the correct high-performance implementation.

-   AI\'s MarketScannerService uses sync sessions + multiple Upstox API
    calls per symbol. This is slower and network-bound. It is not
    adopted.

-   AI\'s scan_market() has richer result categorization (top_gainers,
    top_losers, volume_spikes, breakouts) --- this output schema is
    adopted and ML\'s \_run_scan() is extended to return equivalent
    categories.

-   AI\'s ModeSpec / scan_type logic (market_close vs live scan) is
    adopted into ML\'s scanner as a timeframe selector.

-   indicators.py: ML\'s TechnicalIndicators class (RSI, MACD, Bollinger
    Bands, ATR, etc.) is kept. AI\'s indicators.py adds no new
    indicators --- it is deleted.

**4.8 Authentication: deps.py + middleware/auth.py**

  ------- ---------------------------------------------------------------------
  **→**   Resolution: ML\'s deps.py and core/security.py are the identity
          layer. AI\'s middleware/auth.py is rewritten to extract role from JWT
          claims instead of API keys.

  ------- ---------------------------------------------------------------------

-   ML\'s get_current_user() → returns dict with \'sub\' (user ID) and
    \'role\' (from JWT payload)

-   JWT token creation: create_token_pair() is updated to accept a role
    parameter and embed it in the access token payload

-   AI\'s require_role(UserRole.X) → rewritten to extract role from JWT
    payload via Depends(get_current_user), not from API key lookup

-   AI\'s require_permissions(\[\...\]) → same: reads role from JWT
    claims, checks against ROLE_PERMISSIONS dict

-   AI\'s API_KEYS in-memory store → completely removed

-   UserRole enum: viewer, trader, admin --- these strings are embedded
    in JWT \'role\' claim at token creation time

-   All AI API endpoints that use require_role() continue to work
    unchanged after this rewrite

**4.9 Unified Model Registry (ai/governance/model_registry.py)**

  ------- ---------------------------------------------------------------------
  **→**   Resolution: Merged into one class combining ML\'s encryption +
          artifact management with AI\'s shadow→paper→live state machine.

  ------- ---------------------------------------------------------------------

-   Class: UnifiedModelRegistry in app/ai/governance/model_registry.py

-   Inherits ML\'s Fernet encryption, SHA256 checksum, artifact storage,
    version tracking

-   Inherits AI\'s state machine: VALID_TRANSITIONS = {shadow:
    \[paper\], paper: \[live, shadow\], live: \[paper\]}

-   Adds quality gate enforcement: before promoting shadow→paper, runs
    ML\'s EvaluationGate.check_accuracy_threshold(). Before paper→live,
    runs full gate suite (accuracy + SHAP completeness + consistency)

-   Single DB model: UnifiedMLModel (replaces both MLModelMetadata and
    AIMLModel). Columns: all ML columns + AI\'s deployment_state +
    governance_metadata

-   Migration 0010_unify_model_tables: creates unified table, copies
    data from both source tables, drops originals

-   Redis pub/sub: on state transitions, publishes to
    cai:models:state_changes channel (AI\'s existing pattern)

**4.10 Redis Client**

  ------- ---------------------------------------------------------------------
  **→**   Resolution: ML\'s core/redis.py is the foundation. AI\'s
          redis_client.py pub/sub channels and RedisChannels constants are
          merged in.

  ------- ---------------------------------------------------------------------

-   ML\'s CacheService (get/set/delete with TTL) is kept --- used by ML
    prediction caching, scanner caching

-   AI\'s RedisChannels class (channel name constants) is merged into
    core/redis.py

-   AI\'s RedisClient with subscribe/publish/tenacity retry logic is
    merged in as a PubSubClient class

-   AI\'s close_redis_client() / get_redis_client() are merged into
    ML\'s close_redis() / get_redis() pattern

-   Single init_redis() call in lifespan initializes both the cache
    connection pool and the pub/sub connection

**4.11 AI Fusion Layer: Signal Assembler + ML Integration**

  ------- ---------------------------------------------------------------------
  **★**   This is the most architecturally important integration point. The
          signal assembler fuses event signals (AI), ML predictions (ML
          inference engine), and technical indicators into trading signals.

  ------- ---------------------------------------------------------------------

-   signal_assembler.py is rewritten to async (await all DB calls)

-   The ml_weight path (currently stub) is connected to PredictionEngine
    from app.ml.inference.prediction_engine

-   Call pattern: prediction = await
    asyncio.get_event_loop().run_in_executor(None,
    prediction_engine.predict, features, symbol) --- ONNX Runtime is
    CPU-bound and synchronous; offloaded to thread pool

-   The prediction result (direction, confidence, targets) is normalized
    to a \[-1.0, 1.0\] contribution score

-   Weight verification: event_weight (0.4) + ml_weight (0.4) +
    technical_weight (0.2) = 1.0 (already validated in constructor)

-   Signal storage: async db.add(AITradingSignal(\...)) + await
    db.commit()

-   Redis publish: after commit, publish signal JSON to
    RedisChannels.signals_for_symbol(symbol)

**5. Migration Execution Sequence**

Execute in strict order. Each phase must be validated before proceeding
to the next. The base codebase (File 1 ML) is always kept intact in its
original directory as the safety reference.

**Phase 1 --- Scaffolding & Infrastructure (Day 1)**

1.  Create new repository: cortex-unified. Do not modify either source
    repo.

2.  Copy File 1 (ML) entirely into cortex-unified as the starting point

3.  Create backend/app/ai/ directory with \_\_init\_\_.py and subdirs:
    ingestion/, intelligence/, fusion/, safety/, governance/, strategy/

4.  Merge requirements.txt per Section 4.5 resolution table

5.  Update Dockerfile: add spacy model download (python -m spacy
    download en_core_web_sm), ollama model pull, GPU support

6.  Write unified docker-compose.yml per Section 4.6

7.  Write unified .env.example with all new environment variables
    documented

8.  Update alembic/env.py: async migration runner + TimescaleDB index
    exclusion filter

**Phase 2 --- Database & Migrations (Day 1--2)**

9.  Merge ML database.py with AI\'s TimescaleDB init functions → single
    core/database.py

10. Renumber AI migrations 0001--0004 to 0005--0008 and update all
    down_revision values

11. Write migration 0009_timescaledb_hypertables for all tables per
    Section 2.4

12. Run: alembic upgrade head on a clean TimescaleDB instance and verify
    all tables + hypertables

13. Run: alembic downgrade base then alembic upgrade head to verify full
    round-trip

14. Verify: SELECT \* FROM timescaledb_information.hypertables --- all
    10 time-series tables listed

15. Verify: compression policies created --- SELECT \* FROM
    timescaledb_information.jobs WHERE proc_name =
    \'policy_compression\'

**Phase 3 --- Backend Core Merge (Day 2--3)**

16. Merge config.py: all new AI settings added to ML\'s Settings class

17. Merge redis.py: RedisChannels + PubSubClient merged into
    core/redis.py

18. Rewrite middleware/auth.py: JWT-based role extraction, remove API
    key store

19. Update core/security.py: embed role claim in JWT payload

20. Rewrite main.py: worker spawn, all AI routers, Ollama health check,
    middleware ordering

**Phase 4 --- AI Services Async Rewrite (Day 3--5)**

This is the largest volume of work. Each service follows the mechanical
conversion pattern in Section 2.2. Estimated 20+ files.

21. Move all AI services from app/services/ to app/ai/\[layer\]/ with
    namespace updates

22. Convert all Session → AsyncSession per conversion table in Section
    2.2

23. Convert all sync DB calls to async (await db.execute, await
    db.commit, etc.)

24. Convert all sync Alembic hooks to async-compatible pattern

25. Priority order: signal_pipeline.py → signal_assembler.py →
    nlp_engine.py → event_classifier.py → fake_news_detector.py →
    kill_switch_manager.py → safety_trigger_engine.py →
    regime_detector.py → strategy_orchestrator.py → remaining

26. After each service: run its test file to confirm behavior is
    preserved

**Phase 5 --- ML Integration (Day 5--6)**

27. Delete app/services/ml_prediction_engine.py (stub)

28. Wire signal_assembler.py to app.ml.inference.prediction_engine via
    run_in_executor

29. Build unified model registry in app/ai/governance/model_registry.py

30. Write migration 0010_unify_model_tables

31. Update all references to old model tables in governance.py,
    drift_detector.py, cai_stream.py

32. Implement Ollama LLM calls in fake_news_detector.\_call_llm() and
    event_classifier

**Phase 6 --- API Routes (Day 6)**

33. Register all AI routers in main.py with /api/v1 prefix

34. Update all AI route files to use ML\'s get_db() dependency
    (AsyncSession) and get_current_user()

35. Verify no route prefix conflicts: ML uses /api/v1/ml, /api/v1/auth,
    /api/v1/market-data, /api/v1/scanner, /api/v1/upstox,
    /api/v1/hawk-eye. AI adds /api/v1/ingestion, /api/v1/intelligence,
    /api/v1/governance, /api/v1/strategy, /api/v1/fusion,
    /api/v1/safety, /api/v1/cai

36. Test all endpoints via OpenAPI docs (/docs)

**Phase 7 --- Worker Process (Day 6--7)**

37. Refactor app/worker.py: add SIGTERM handler (asyncio Event), add
    heartbeat key writer, remove SessionLocal usage → own async engine

38. Test standalone: python -m app.worker --- verify all 4 background
    loops start

39. Test spawn from API lifespan: start API, check worker is running,
    check heartbeat in Redis

40. Test shutdown: send SIGTERM to API, verify worker receives SIGTERM
    and exits cleanly within 30s

41. Test escalation: patch worker to ignore SIGTERM, verify SIGKILL
    fires after timeout

**Phase 8 --- Frontend (Day 7--8)**

42. Merge AI frontend components into ML frontend --- all additive, no
    conflicts

43. Add AI pages (cai-dashboard, cortex-ai) to Next.js app router

44. Merge lib/api.ts: add all AI endpoint types and fetch functions

45. Add AI hooks (useCAIWebSocket, useSignals, useRegime, etc.)

46. Add new UI components from AI (dialog.tsx, tabs.tsx)

47. Run: npm run build --- fix all TypeScript errors

48. Run: npm run test --- all existing tests pass

**Phase 9 --- Full Integration Testing (Day 8--9)**

49. docker compose up --- verify all services start and pass health
    checks

50. Run ML test suite: pytest backend/tests/ --- all existing tests pass

51. Run AI test suite: backend/tests/ and frontend/\_\_tests\_\_/

52. End-to-end flow: seed RSS source → trigger ingestion → verify NLP
    result → verify signal generated → verify WebSocket delivery

53. End-to-end flow: submit ML prediction request → verify ONNX
    inference → verify signal fusion uses it

54. Kill switch test: activate global kill switch → verify signal
    pipeline is blocked

55. Drift detection test: submit drift data → verify ai_drift_reports
    row created → verify model state transition

56. JWT + RBAC test: verify viewer cannot call write:kill_switch
    endpoint

57. Load test: 1000 concurrent prediction requests --- verify \<250ms
    p95 latency

**6. Quality, Security & Observability Standards**

**6.1 Security hardening**

-   JWT: access tokens expire in 30 min; refresh tokens in 7 days with
    family rotation and Redis revocation. Reuse detection triggers full
    family revocation.

-   Secrets: all secrets loaded exclusively from environment variables.
    No defaults for SECRET_KEY, DATABASE_URL, UPSTOX_API_KEY,
    ML_MODEL_ENCRYPTION_KEY. App refuses to start if missing.

-   CORS: explicit allowlist only. Wildcard \'\*\' raises ValueError in
    Settings validator.

-   ML model artifacts: Fernet-encrypted at rest. SHA256 checksum
    validated on every load.

-   Rate limiting: SlowAPI + Redis covers all endpoints. ML prediction
    endpoints: 100/min. Kill switch: 5/min (trader+). Training: 1/hour.

-   Kill switch: activates in \<100ms via direct DB write + Redis
    pub/sub broadcast. Worker loop checks Redis before every signal
    publish.

-   RBAC: role embedded in JWT. No DB round-trip per request for
    authorization. Permissions are checked at handler entry via
    Depends().

**6.2 Observability**

-   Logging: python-json-logger (structured JSON to stdout). All log
    lines include: timestamp, level, logger name, request_id (from
    correlation ID middleware), user_id (where applicable).

-   Metrics: Prometheus client exposes /metrics. Key metrics: request
    latency histogram, prediction latency histogram, signal generation
    counter, kill switch activation counter, drift detection gauge,
    worker heartbeat age gauge.

-   Health endpoint: GET /health returns status of API, DB connectivity,
    Redis connectivity, Ollama availability, and worker heartbeat
    freshness.

-   Sentry: SENTRY_DSN env var enables error reporting (optional, ML
    already supports this).

-   ML monitoring: existing monitoring/ module (drift_detector, latency,
    metrics) continues unchanged.

**6.3 Code quality standards**

-   Linting: ruff (from ML\'s ruff.toml). Applied to all merged files.
    Zero tolerance for ruff errors in CI.

-   Type annotations: all function signatures fully annotated. No bare
    dict or list. Pydantic models for all API boundaries.

-   No print() statements. All output via logging module.

-   No bare except: clauses. All exceptions caught explicitly and logged
    with context.

-   No global mutable state in services. All service dependencies
    injected via constructor or FastAPI Depends().

-   All async functions must be awaited. asyncio.run() only in worker
    entry point. No asyncio.get_event_loop().run_until_complete() in
    request handlers.

-   Test coverage: minimum 80% for new merged code. Existing ML test
    suite must remain at 100% pass.

**7. Known Risks & Mitigations**

  --------------------- -------------- ------------------------------------------
  **Risk**              **Severity**   **Mitigation**

  numpy 2.x +           HIGH           torch 2.1.2 was released before numpy 2.x.
  torch/transformers                   Test immediately: import torch; import
  compatibility                        numpy as np. Update torch to ≥ 2.3.0 which
                                       supports numpy 2.x. If pinning is
                                       required, pin numpy to 1.26.x and update
                                       torch.

  Ollama startup        MEDIUM         Llama 3.1 8B takes 30--60s to load on
  latency                              first request. Lifespan health check must
                                       not fail app startup if model is loading.
                                       Use retry loop with timeout (60s), not a
                                       hard fail.

  TimescaleDB migration MEDIUM         If ML tables have existing data,
  on existing ML tables                create_hypertable() will fail unless the
                                       time column is in a unique index.
                                       Migration 0009 already checks
                                       \_partition_column_in_pk_or_unique() ---
                                       ensure all ML tables have the required
                                       constraints before migration.

  Alembic autogenerate  LOW            TimescaleDB creates internal indexes that
  false positives                      Alembic will try to DROP. The
                                       exclude_by_name filter must be in place
                                       before any developer runs alembic revision
                                       \--autogenerate.

  Worker crash during   MEDIUM         If worker crashes mid-pipeline, partial
  ingestion cycle                      events may remain in ai_processed_events
                                       with status=\'pending\'. Worker restart
                                       should resume from pending events. Add
                                       startup query: SELECT events WHERE
                                       processing_status = \'pending\' AND
                                       created_at \> now() - interval \'1 hour\'.

  PgBouncer statement   LOW (if used)  If PgBouncer is introduced in production
  cache + asyncpg                      in transaction mode, asyncpg\'s prepared
                                       statement cache will cause errors. Set
                                       statement_cache_size=0 in
                                       create_async_engine() and document this.

  Fake news detector    LOW            Ollama client is initialized once at
  layer 4 cold start                   worker startup. If Ollama is restarted
                                       after worker startup, the client will
                                       error. Add reconnect logic to \_call_llm()
                                       with exponential backoff.
  --------------------- -------------- ------------------------------------------

**8. Out of Scope (This Document)**

The following are NOT addressed in this merge plan and must be handled
separately:

-   IndicTrans2 translation implementation --- currently a placeholder
    in nlp_engine.py. The actual IndicTrans2 model integration
    (Hindi/Gujarati/Tamil → English) requires a separate model download
    and setup.

-   Social media monitor (filing_scraper, social_media_monitor) ---
    these require external API credentials (Twitter, BSE/NSE filing
    APIs) that are currently stubs.

-   ML model training pipeline --- training/ directory is present but
    requires labeled historical data, GPU compute, and a separate
    training job. The merge plan only ensures the training code is
    correctly included.

-   Kubernetes / ECS deployment manifests --- docker-compose covers
    dev/staging. Production deployment orchestration is a separate
    workstream.

-   Continuous Integration configuration --- GitHub Actions / GitLab CI
    pipelines with the test suite, lint, and build steps.

-   Monitoring dashboards --- Grafana / Prometheus dashboard
    configuration for production observability.

-   Data backfill --- populating ai_ingestion_sources,
    ai_source_credibility with real RSS sources and credibility baseline
    scores.

*CORTEX AI --- Merge Plan v1.0*

Engineering Internal --- April 2026
