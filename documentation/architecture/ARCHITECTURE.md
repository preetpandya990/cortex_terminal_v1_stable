# Cortex AI Trading Platform — Architecture Documentation

**Version:** 1.1.1  
**Last Updated:** June 1, 2026  
**Classification:** Engineering Internal  
**Framework:** Domain-Driven Design (DDD) + RAD-AI Extensions  
**Compliance:** EU AI Act Annex IV Ready

---

## Document Purpose & Audience

This document is the authoritative architectural reference for the **Cortex AI Trading Platform**, a production-grade AI/ML-driven trading signal platform for the Indian equity markets (NSE). It supersedes the April 14, 2026 revision.

| Audience | Use this document to… |
|---|---|
| **New engineers** | Understand bounded contexts, data flows, and integration contracts before touching code |
| **ML engineers** | Understand the full training → governance → inference pipeline |
| **DevOps / SRE** | Understand deployment topology, observability stack, and operational procedures |
| **Architects** | Evaluate design decisions, quality attributes, and the AI debt register |
| **Compliance** | Satisfy EU AI Act Annex IV documentation requirements for high-risk AI systems |

For fresh-install / migration instructions, see `README.md`. For cross-module graph queries, see `graphify-out/GRAPH_REPORT.md`.

---

## Executive Summary

### System Overview

Cortex AI is a unified, production-hardened signal platform that combines:

- **Real-time NSE market data** ingested from the Upstox v3 API (REST + WebSocket, Protobuf-encoded ticks)
- **Cross-sectional ML ensemble** (XGBoost + GRU) trained on 2,011 NSE equities with CPCV validation and a deflated-Sharpe quality gate
- **LLM-augmented intelligence layer** powered by Ollama (Llama 3.1 8B) for event classification, fake-news detection, and NLP analysis
- **Company fundamentals pipeline** ingesting 8 Upstox fundamental endpoints into 8 dedicated tables, with 20 derived ML features
- **Strategy marketplace** with a rule-based engine, backtesting, and an activation FSM
- **Paper trading engine** with full NSE statutory-charge modelling (STT, exchange fees, SEBI, GST, stamp duty) and post-close counterfactual monitoring
- **Production observability** via Prometheus + Grafana (6 dashboards), structured JSON logging with correlation-ID middleware, circuit breakers, and Evidently drift detection

### Key Metrics (post-overhaul, verified 2026-06-01)

| Metric | Value | Source |
|---|---|---|
| **Codebase** | ~64 k LOC backend (Python) · ~39 k LOC frontend (TS/TSX) | Static analysis, audit 2026-06-01 |
| **DB tables** | 53 across 9 domains | Alembic schema + ORM models |
| **Migrations** | 41 linear forward-only (head = `0039`) | `backend/alembic/versions/` |
| **Test suite** | 82 files · 7 categories | `backend/tests/` (under version control) |
| **ML ensemble accuracy** | **68.0%** directional on 30 k held-out samples | `training_results_20260601_032246.json` |
| **Ensemble Sharpe** | 1.09 · deflated Sharpe 0.73 · PBO 9.5% | Same |
| **Ensemble vs standalones** | XGB 64.0% / GRU 66.4% → **Ensemble 68.0%** | Same |
| **Training universe** | 2,011 of 2,551 NSE EQ symbols (78.8% coverage) | Same |
| **Features** | 69 (44 technical + 5 sentiment + 20 fundamental) | `feature_pipeline.get_all_feature_names()` |
| **Latency SLA (target)** | Consensus <100 ms p95 · prediction <2 s | Architecture targets (not yet load-tested) |

### Technology Stack

| Layer | Component | Version |
|---|---|---|
| **Runtime** | Python | 3.11.15 (hard-pinned) |
| **API** | FastAPI / Uvicorn | 0.115.0 / 0.32.0 |
| **ORM / driver** | SQLAlchemy 2 async / asyncpg | 2.0.35 / 0.29.0 |
| **Migrations** | Alembic | 1.13.3 |
| **Validation** | Pydantic v2 / pydantic-settings | 2.9.2 / 2.6.1 |
| **Time-series DB** | TimescaleDB (PostgreSQL 16) | pg16 |
| **Cache / pub-sub** | Redis + hiredis | 7 / 5.2.0 |
| **ML — training** | TensorFlow/Keras · XGBoost · Optuna · scikit-learn | 2.21.0 · 2.0.3 · 3.5.0 · 1.4.0 |
| **ML — inference** | ONNX Runtime (GPU) · Treelite (compiled .so) | 1.19.2 · — |
| **ML — NLP / sentiment** | transformers (FinBERT) · spaCy | 4.46.3 · 3.7.2 |
| **ML — patterns** | TA-Lib (61 candlestick patterns) | ≥0.4.28 |
| **ML — drift** | Evidently | 0.4.33 |
| **LLM inference** | Ollama / OpenAI fallback | 0.3.0 / 1.10.0 |
| **Numerics** | numpy · pandas · PyTorch (CPU-only) | 1.26.4 · 3.0.2 · 2.11.0+cpu |
| **Frontend** | Next.js · React | 16.1.6 · 19.2.3 |
| **Server state** | TanStack Query | 5.90.20 |
| **Charts** | lightweight-charts | v5 |
| **Observability** | Prometheus client · Grafana | 0.19 · 11.4.0 |

> **Critical pin constraint.** `numpy 1.26.4` and `scikit-learn 1.4.0` are hard-pinned. `numpy ≥ 2.0` breaks TensorFlow 2.21's ABI and silently disables GPU. `torch` is CPU-only on the reference build host (4 GB GPU, WSL2 CUDA 12.2 ceiling). Never run `pip install -U` on any of `numpy / sklearn / torch / tensorflow` without `--dry-run` first. See `README.md §ML Environment Constraints`.

---

## 1. System Context (C1)

### 1.1 Context Boundary

```
┌─────────────────────────────────────────────────────────────────────┐
│                    CORTEX AI TRADING PLATFORM                        │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │  DETERMINISTIC BOUNDARY                                     │    │
│  │  • JWT/RBAC authentication           • Rate limiting        │    │
│  │  • Market data ingestion             • DB CRUD + migrations │    │
│  │  • Fundamentals pipeline             • Paper trading engine │    │
│  │  • Strategy rule evaluation          • Safety kill switch   │    │
│  └────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │  NON-DETERMINISTIC BOUNDARY (AI/ML)                         │    │
│  │  • ML ensemble (XGBoost + GRU)                              │    │
│  │    68.0% directional · DSR 0.73 · Fallback: HOLD            │    │
│  │  • LLM event classifier (Ollama Llama 3.1 8B)               │    │
│  │    Fallback: rule-based keyword classification              │    │
│  │  • Regime detector (statistical + ML hybrid)                │    │
│  │    Fallback: default "ranging"                              │    │
│  │  • Drift detector (Evidently KS test, every 300 s)          │    │
│  │    Action: alert only — no automatic retraining             │    │
│  └────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

### 1.2 External Systems & Actors

**External systems**

| System | Protocol | Purpose |
|---|---|---|
| **Upstox API v3** | REST + WSS (Protobuf-encoded ticks) | Historical OHLCV · real-time ticks · 8 fundamental endpoints · OAuth 2.0 |
| **Ollama** | HTTP `localhost:11434` | Llama 3.1 8B — event classification, fake-news detection |
| **OpenAI API** | HTTPS | Optional LLM fallback when Ollama is unavailable |
| **RSS feeds** | HTTP/HTTPS | Financial news (ET, Moneycontrol, Bloomberg, etc.) — 300–900 s poll with jitter |
| **NSE Holiday API** | HTTPS | Trading-calendar data, cached in `.cache/nse_holidays.json` |

**Human actors**

| Role | Permissions |
|---|---|
| **Viewer** | Read market data, ML predictions, signals, fundamentals |
| **Trader** | Viewer + generate signals, manage watchlist, execute and monitor paper trades |
| **Admin** | Trader + promote/demote models, manage users, activate kill switch, dispatch ML training |

---

## 2. Domain-Driven Design: Bounded Contexts

The system is decomposed into **8 bounded contexts** with explicit integration contracts via Redis pub/sub channels and well-defined DB table ownership.

### 2.1 Context Map

```
┌────────────────────────┐
│  Market Data           │◄──── Upstox v3 API (REST + WSS Protobuf)
│  app/services/         │◄──── RSS feeds (news)
│  market_feed.py        │
│  data_ingestion*.py    │
│  Owns: upstox_ticks    │
│        upstox_ohlcv    │
│        instrument_master│
│        stock_ohlcv     │
└──────────┬─────────────┘
           │ Redis: market_data.tick / market_data.candle
           ▼
┌────────────────────────┐    ┌────────────────────────┐
│  Signal Intelligence   │◄───│  AI Intelligence       │
│  app/ml/               │    │  app/ai/intelligence/  │
│                        │    │  app/ai/ingestion/     │
│  Owns: ml_model_meta   │    │  Owns: ai_raw_events   │
│        ml_predictions  │    │        ai_processed_   │
│        ml_features     │    │          events        │
│        ml_drift_metrics│    │        ai_fake_news_   │
│        ml_experiments  │    │          flags         │
│        ml_audit_logs   │    │        ai_source_cred  │
└──────────┬─────────────┘    └──────────┬─────────────┘
           │ Redis: ml.prediction         │ Redis: ai.event_classified
           ▼                             ▼
┌─────────────────────────────────────────────────────┐
│  Signal Fusion   app/ai/fusion/ · app/ai/correlation/│
│  Owns: ai_trading_signals · ai_active_strategies    │
│        event_correlations · trade_suggestions       │
└─────────────────────────────┬───────────────────────┘
                              │ Redis: fusion.signal
         ┌────────────────────┼────────────────────┐
         ▼                    ▼                    ▼
┌─────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│  Risk & Safety  │  │  Strategy        │  │  Paper Trading   │
│  app/ai/safety/ │  │  Marketplace     │  │  app/services/   │
│                 │  │  app/services/   │  │  paper_trading/  │
│  Owns:          │  │  strategy_engine │  │                  │
│  ai_kill_switch │  │                  │  │  Owns: portfolios│
│  ai_safety_trig │  │  Owns: strategies│  │  paper_orders    │
│                 │  │  strategy_activ  │  │  paper_positions │
│  Redis:         │  │  strategy_trades │  │  paper_fills     │
│  safety.trigger │  │  strategy_backts │  │  paper_pnl_snaps │
└─────────────────┘  └──────────────────┘  │  paper_outcomes  │
                                           │  post_close_mon  │
                                           └──────────────────┘

┌────────────────────────────────────────┐
│  Fundamentals   app/services/          │
│  fundamentals_service.py               │
│  fundamentals_refresh.py               │
│                                        │
│  Owns: company_fundamentals_profile    │
│        company_key_ratios              │
│        company_income_statement        │
│        company_balance_sheet           │
│        company_cash_flow               │
│        company_corporate_actions       │
│        company_competitors             │
│        company_share_holdings          │
└────────────────────────────────────────┘
```

### 2.2 Context Details

#### Context 1: Market Data

**Responsibility**: Ingest, validate, normalise, and persist market data from Upstox.

**Key components**:
- `MarketFeedService` — unified WebSocket base (`base_websocket.py`) wrapping the Upstox v3 Protobuf tick stream. Per-instrument throttle: 250 ms before Redis publish.
- `DataIngestionService` — OHLCV backfill, gap detection, bulk upsert (`BULK_INSERT_BATCH_SIZE = 1000`). Circuit breaker (5 consecutive failures → open, 300 s recovery). Rate: 40 req/min, 1 concurrent request (below Cloudflare burst threshold on the historical-candle endpoint).
- `MarketScannerService` — single windowed SQL query across all instruments (replaced prior N-query per-symbol design).
- `CandleService` — on-demand OHLCV assembly for chart endpoints.

**Hypertables**: `upstox_ticks` (7-day compression, 90-day retention); `upstox_ohlcv` (2-year retention).

**Quality gates**: Schema validation (reject on missing `instrument_key`/`ltp`/`timestamp`), duplicate suppression, no-future-timestamp check.

---

#### Context 2: Signal Intelligence (ML)

**Responsibility**: Generate ML-based directional predictions using the production cross-sectional ensemble.

**Model architecture summary**:

| Dimension | Current state |
|---|---|
| **Scope** | Cross-sectional — one model trained on all 2,011 eligible NSE EQ symbols simultaneously |
| **Label scheme** | Binary: UP (1) / DOWN (0) via ATR-normalised symmetric dead zone. HOLD is a runtime policy (Selective Classification), not a training label. |
| **Features** | 69 total: 44 technical (cross-sectionally normalised, scale-invariant) + 5 sentiment (FinBERT) + 20 fundamental |
| **Sequence** | 60-bar GRU input; XGBoost uses same 69 features as a flat vector |
| **Validation** | CPCV: 8 groups, 2 held-out, 5-bar horizon, 5-bar embargo — prevents lookahead leakage |
| **HPO** | Optuna: 100 trials (XGBoost); Keras Tuner: 5 trials (GRU, VRAM capped at 1.6 GB) |
| **Quality gate** | Ensemble deflated Sharpe must exceed best standalone DSR; PBO < 50%; backtest must be accretive |
| **Ensemble weights** | XGBoost 0.5106 / GRU 0.4894 (Sharpe-maximising grid search over 200 CPCV paths, PBO regularisation L2 = 0.1) |
| **Production metrics** | Accuracy 68.0% · Sharpe 1.09 · DSR 0.73 · PBO 9.5% · AUC-PR 0.729 (30 k held-out) |
| **Inference artefacts** | Treelite-compiled `.so` (XGBoost) + ONNX (GRU) — **not** pickle; plaintext + SHA-256 integrity check |
| **Model version** | 1.1.1 (production active) |

**Feedback loop**: Paper trading outcomes → `FeedbackLoader` (SHA-256 signed Parquet bundles) → feedback weights → challenger retraining input.

**Retraining**: Weekly challenger run via systemd `cortex-retrain.timer`. Never auto-promotes — human gate required via `promote_model.py`.

---

#### Context 3: AI Intelligence

**Responsibility**: Process news events via LLM classification, fake-news detection, and FinBERT sentiment scoring.

**Components**:
- `RSSFetcher` — polls multiple financial feeds (300–900 s jitter, 5 concurrent)
- `EventClassifier` — Ollama Llama 3.1 8B structured JSON output (category, reasoning); falls back to rule-based keyword matching on timeout
- `FakeNewsDetector` — 4-layer pipeline: source credibility → cross-reference → sentiment consistency → LLM reasoning
- `NLPEngine` — spaCy NER + FinBERT (ONNX-GPU) for entity extraction and sentence-level sentiment
- `CredibilityScorer` — per-source accuracy tracking updated per event

---

#### Context 4: Signal Fusion

**Responsibility**: Assemble ML predictions, technical indicators, event signals, and correlation anomalies into unified trading signals.

**Fusion inputs**:
- ML prediction confidence (primary driver)
- Technical indicator score (RSI, MACD, Bollinger, ATR-normalised VWAP)
- Event sentiment with time-decay on news age
- 61 TA-Lib candlestick patterns via `PatternDetectionService`
- Correlation engine anomaly score (`app/ai/correlation/engine.py`)

**Circuit breakers**:
- Signal frequency > `SIGNAL_FREQUENCY_THRESHOLD` (100/hr) → suppress 1 hour
- Volatility spike > `VOLATILITY_SPIKE_MULTIPLIER` (3×) average → suppress 30 min
- Kill switch active → suppress all signals immediately

**Signal scheduler**: Every 15 min during NSE market hours for the 100-symbol scheduled universe (Nifty 50 + Nifty Next 50). On-demand signals available via API with 15-min Redis cache.

---

#### Context 5: Risk & Safety

**Responsibility**: Portfolio risk monitoring, kill switch management, safety trigger enforcement.

**Safety loop**: 30-second cycle checks loss threshold (5%), volatility spike (3×), signal frequency. Fail-safe: kill switch defaults ON on detection failure. `safety.trigger_activated` published to Redis on any activation. Grafana alert configured.

---

#### Context 6: Strategy Marketplace

**Responsibility**: User-defined rule-based trading strategies with backtesting and an activation lifecycle FSM.

**Strategy engine components** (`app/services/strategy_engine/`):
- `RuleEvaluator` — evaluates signal-based entry/exit conditions against live trade suggestions
- `BacktestEngine` / `BacktestRunner` — historical simulation with full NSE charge modelling
- `ActivationFSM` — lifecycle: `DRAFT → ACTIVE → PAUSED → ARCHIVED`
- `FilterPipeline` — pre-filters the live signal universe before rule evaluation
- `SLTPWorker` — monitors active strategy positions for stop-loss / take-profit triggers
- `Dispatcher` — routes matched signals to strategy subscribers

**Tables**: `strategies`, `strategy_activations`, `strategy_audit_logs`, `strategy_backtest_runs`, `strategy_trades`, `user_strategy_subscriptions`

---

#### Context 7: Paper Trading

**Responsibility**: Simulated trading with production-accurate P&L, NSE statutory charges, and post-close counterfactual monitoring.

**Charge calculator** (`charge_calculator.py`): Decimal arithmetic modelling STT (0.1% CNC buy-side), NSE/BSE exchange fees, SEBI turnover fee, GST (18% on brokerage), stamp duty, and configurable slippage.

**Post-close monitoring** (migration 0037): After NSE close (15:30 IST), monitors open suggestion outcomes against counterfactual price paths for ML feedback quality assessment.

**Tables**: `paper_orders`, `paper_positions`, `paper_fills`, `paper_pnl_snapshots`, `paper_trade_outcomes`, `paper_position_conversions`, `portfolios`, `post_close_monitors`

---

#### Context 8: Fundamentals

**Responsibility**: Ingest and serve company fundamental data from 8 Upstox API endpoints; compute 20 ML features.

**8 endpoints**: Profile, key ratios, income statement, balance sheet, cash flow, corporate actions, competitors, shareholding pattern (rate limit: 50 req/s).

**Refresh schedule** (configurable via env vars):
- Key ratios: daily post-close at 15:45 IST (trading days only)
- Corporate actions: nightly at 18:30 IST
- Full universe refresh: nightly at 01:00 IST

**20 ML features** (`fundamental_features.py`): valuation (P/E, P/B, ROE, ROCE, EV/EBITDA), revenue growth (YoY, CAGR), profit growth (YoY, CAGR), margins (operating margin, 3Y avg), balance sheet health (net worth log, CAGR, debt ratio + trend), cash flow quality (operating CF growth, CAGR), ownership dynamics (promoter holding %, FII %, promoter change).

---

## 3. Container Architecture (C2)

```
┌─────────────────┐    ┌───────────────────────────────────────┐    ┌──────────────────────┐
│  Browser        │    │  FastAPI (Uvicorn)   :8000            │    │  TimescaleDB (PG16)  │
│  Next.js :3000  │◄──►│  30 routers · 5 WS endpoints         │◄──►│  53 tables           │
└─────────────────┘    │  JWT · SlowAPI · GZip · TrustedHost  │    │  Hypertables + comp. │
                       └──────────────────┬────────────────────┘    └──────────────────────┘
                                          │
               ┌──────────────────────────┼────────────────────────┐
               ▼                          ▼                         ▼
   ┌───────────────────┐     ┌──────────────────┐      ┌──────────────────────┐
   │  Worker Process   │     │  Redis 7 :6379   │      │  Ollama :11434       │
   │  app.worker       │     │  50 connections  │      │  Llama 3.1 8B        │
   │  11 async loops   │     │  cache + pub/sub │      │  GPU or CPU fallback │
   │  (systemd)        │     └──────────────────┘      └──────────────────────┘
   └───────────────────┘
                              ┌───────────────────────────────────┐
   ┌──────────────────────┐   │  Prometheus :9090                 │
   │  Scheduled Retrain   │   │  scrapes /metrics (FastAPI)       │
   │  systemd timer       │   └──────────────────┬────────────────┘
   │  → orchestrator      │                      │
   │    --fresh           │   ┌──────────────────▼────────────────┐
   │  → challenger dir    │   │  Grafana :3001 · 6 dashboards     │
   │  → human gate        │   └───────────────────────────────────┘
   └──────────────────────┘
```

### 3.1 Worker: 11 Concurrent Background Loops

| Loop | Interval | Responsibility |
|---|---|---|
| `heartbeat_loop` | 30 s | `worker:heartbeat` in Redis (TTL 60 s) — external liveness signal |
| `rss_ingestion_loop` | 300–900 s + jitter | Financial RSS polling and event parsing |
| `event_processing_loop` | Continuous | Ollama classification → `ai_processed_events` |
| `data_ingestion_loop` | Hourly gap-check | OHLCV gap detection and Upstox backfill |
| `regime_detection_loop` | Daily at 16:00 IST | Post-close market regime classification |
| `safety_monitoring_loop` | 30 s | Loss limit, volatility spike, signal frequency checks |
| `drift_detection_loop` | 300 s | Evidently KS-test against training distribution baseline |
| `expiry_loop` | Continuous | Mark stale `trade_suggestions` expired; publish expiry events |
| `cache_invalidation_loop` | Event-driven | Subscribe `SUGGESTIONS_NEW`; invalidate list-cache keys |
| `correlation_loop` | 30 s | Cross-reference scanner anomalies with classified news events |
| Paper trading P&L loop | Per-tick | Real-time P&L recomputation for open paper positions |

### 3.2 WebSocket Endpoints (5)

| Endpoint | Purpose |
|---|---|
| `/api/v1/upstox/ws` | Upstox market feed (Protobuf ticks → JSON → Redis pub/sub) |
| `/api/v1/trade-suggestions/ws` | Live trade suggestion updates |
| `/api/v1/paper-trading/ws` | Real-time paper P&L stream |
| `/api/v1/cai/ws` | CAI dashboard (signals, events, regime, ML activity) |
| `/api/v1/admin/training/ws` | ML training console live log stream |

### 3.3 Container Communication

| Source | Target | Protocol | P95 SLA |
|---|---|---|---|
| Browser | FastAPI | HTTPS / WSS | <200 ms REST · <100 ms WS |
| FastAPI | TimescaleDB | asyncpg | <50 ms |
| FastAPI | Redis | RESP | <10 ms |
| FastAPI | Ollama | HTTP | <30 s timeout |
| Worker | TimescaleDB | asyncpg (separate engine + pool) | <100 ms |
| Worker | Redis | RESP | <10 ms |
| FastAPI | Upstox REST | HTTPS | <1 s |
| FastAPI | Upstox WS | WSS Protobuf | <50 ms tick-to-Redis |

---

## 4. Backend Component Architecture (C3)

```
backend/app/
├── main.py                    # FastAPI app factory · 30 routers registered
├── worker.py                  # 11-loop async background worker (11 concurrent tasks)
├── exceptions.py              # Custom exception hierarchy
│
├── core/                      # Cross-cutting infrastructure
│   ├── config.py              # Pydantic-settings (fail-fast, placeholder rejection,
│   │                          #   wildcard-CORS validator, no ML_MODEL_ENCRYPTION_KEY)
│   ├── database.py            # Async SQLAlchemy engine + separate worker engine
│   ├── redis.py               # Redis client · pub/sub helpers · TTL wrappers
│   ├── security.py            # JWT creation / validation (HS256)
│   ├── auth.py                # RBAC FastAPI dependency injectors
│   ├── limiter.py             # SlowAPI (globally wired to all routers)
│   ├── cache_decorator.py     # @cache(ttl=N) response-caching decorator
│   ├── circuit_breaker.py     # Configurable circuit breaker (threshold + timeout)
│   ├── retry.py               # Exponential backoff decorator
│   ├── metrics.py             # Prometheus counter/gauge/histogram definitions
│   ├── pagination.py          # Cursor-based pagination helpers
│   ├── request_context.py     # Request-scoped context vars (correlation ID)
│   ├── websocket_manager.py   # Typed WS connection manager
│   ├── logging_config.py      # Structured JSON logging setup
│   ├── json_formatter.py      # Log JSON serialiser
│   ├── health_checks.py       # Readiness / liveness probe logic
│   └── exception_handlers.py  # Registered handlers + correlation-ID response middleware
│
├── middleware/
│   ├── metrics.py             # Prometheus request instrumentation (per-route counters)
│   └── request_id.py          # Correlation-ID injection via X-Request-ID header
│
├── models/                    # SQLAlchemy ORM (53 tables across 3 files)
│   ├── user.py                # users · refresh_tokens · user_preferences · watchlist_items
│   ├── upstox_data.py         # upstox_ticks (HT) · upstox_ohlcv (HT) · instrument_master
│   │                          #   stock_ohlcv
│   └── ml_data.py             # All remaining 40+ tables (see §6.1 for full list)
│
├── schemas/                   # Pydantic v2 request / response schemas (per domain)
│
├── api/v1/                    # 30 REST + WS routers
│   ├── auth.py                # /auth — register · login · logout · refresh
│   ├── admin_users.py         # /admin — user CRUD (admin-only)
│   ├── admin_strategies.py    # /admin/strategies — strategy governance
│   ├── admin_training.py      # /admin/training — dispatch console + WS log stream
│   ├── market_data.py         # /market-data — OHLCV · live price · candles
│   ├── scanner.py             # /scanner — windowed multi-factor scan
│   ├── hawk_eye.py            # /hawk-eye — ML-augmented scanner
│   ├── upstox.py              # /upstox — ingest · status · WS tick feed
│   ├── trade_suggestions.py   # /trade-suggestions — CRUD + WS
│   ├── watchlist.py           # /watchlist — CRUD
│   ├── ml_predictions.py      # /ml — predict · ensemble-predict · models
│   ├── ml_patterns.py         # /ml/patterns — 61 TA-Lib candlestick patterns
│   ├── ml_drift.py            # /ml/drift — scores · detect
│   ├── paper_trading.py       # /paper-trading — orders · positions · P&L + WS
│   ├── strategies.py          # /strategies — marketplace CRUD · backtest · activate
│   ├── fundamentals.py        # /fundamentals — profile · ratios · financials · …
│   ├── ai_sentiment.py        # /ai/sentiment · /finbert
│   ├── ai_stream.py           # /ai/stream — SSE analysis stream
│   ├── ingestion.py           # /ingestion — events · status
│   ├── intelligence.py        # /intelligence — classify · fake-news
│   ├── governance.py          # /governance — models · promote · demote
│   ├── strategy.py            # /strategy — regime · history
│   ├── fusion.py              # /fusion — signals · generate
│   ├── safety.py              # /safety — kill-switch · triggers
│   ├── cai.py                 # /cai — dashboard · activity + WS
│   ├── users.py               # /users — profile · preferences
│   └── health.py              # /health · /health/ready · /health/ml · /metrics
│
├── services/                  # Business logic services
│   ├── base_websocket.py      # Abstract WS service (single unified base class)
│   ├── market_feed.py         # Upstox Protobuf tick stream + Redis throttle + publish
│   ├── data_ingestion.py      # OHLCV orchestrator (API-facing, circuit-breaker backed)
│   ├── data_ingestion_worker.py # Worker-process ingestion loop
│   ├── candle_service.py      # On-demand OHLCV assembly
│   ├── market_scanner.py      # Single windowed SQL scanner
│   ├── hawk_eye.py            # ML-augmented multi-factor scanner
│   ├── indicators.py          # Technical indicators (Wilder RSI, daily-reset VWAP, …)
│   ├── pattern_detection_service.py # TA-Lib 61 candlestick patterns
│   ├── signal_scheduler.py    # 15-min scheduled signal generation (100-symbol universe)
│   ├── sentiment_analysis_service.py # FinBERT sentence-level scoring
│   ├── fundamentals_service.py  # Fundamental data query + 20 ML feature computation
│   ├── fundamentals_refresh.py  # 3-schedule refresh orchestrator
│   ├── strategy_service.py    # Strategy CRUD + lifecycle management
│   ├── regime_service.py      # Regime classification query helper
│   ├── upstox_client.py       # Shared pooled httpx client (50 conns, keepalive)
│   ├── upstox_persistence.py  # Upstox data write helpers
│   ├── symbol_validator.py    # NSE EQ eligibility check (against instrument_master)
│   ├── suggestion_compliance.py # Per-user suggestion compliance audit trail
│   ├── sector_map.py          # NSE sector / industry mapping
│   ├── user_preferences_service.py # User preference CRUD
│   ├── ml_feedback_backfill.py # Historical paper-outcome backfill
│   ├── market_calendar.py     # NSE trading hours + holiday calendar
│   ├── paper_trading/         # Paper trading sub-services
│   │   ├── order_service.py
│   │   ├── position_service.py
│   │   ├── portfolio_service.py
│   │   ├── pnl_worker.py
│   │   ├── charge_calculator.py  # STT · exchange · SEBI · GST · stamp · slippage
│   │   ├── conversion_service.py # Intraday → CNC conversion
│   │   ├── outcome_service.py
│   │   ├── price_target_service.py
│   │   ├── qty_suggester.py
│   │   └── post_close_monitor_service.py
│   └── strategy_engine/
│       ├── rule_evaluator.py
│       ├── activation_fsm.py  # DRAFT → ACTIVE → PAUSED → ARCHIVED
│       ├── backtest_engine.py
│       ├── backtest_runner.py
│       ├── filter_pipeline.py
│       ├── dispatcher.py
│       ├── market_context.py
│       └── sl_tp_worker.py
│
├── ml/                        # ML system (fully under version control post-overhaul)
│   ├── config.py              # ML constants + SCHEDULED_RETRAIN configuration
│   ├── features/
│   │   ├── ohlcv_features.py  # 44 technical features (cross-sectionally normalised)
│   │   ├── sentiment_features.py  # 5 FinBERT-derived features
│   │   ├── fundamental_features.py # 20 fundamental features
│   │   ├── feature_pipeline.py # Multi-source assembly → 69 features
│   │   ├── target_generator.py # Binary labels with ATR symmetric dead zone
│   │   ├── symbol_selector.py # IPO quarantine (180 d), coverage filter (≥60% bars)
│   │   ├── engine.py          # Real-time feature materialisation (inference path)
│   │   ├── technical_indicators.py
│   │   ├── timeframe_features.py
│   │   └── validation.py
│   ├── training/
│   │   ├── xgboost_trainer.py # XGBoost + Optuna (100 trials)
│   │   ├── gru_trainer.py     # GRU + Keras Tuner (5 trials; VRAM cap 1.6 GB)
│   │   ├── ensemble_trainer.py # Sharpe-maximising weight search + PBO
│   │   ├── evaluator.py       # Accuracy · Sharpe · DSR · PBO · AUC-PR
│   │   ├── walk_forward.py    # Legacy walk-forward (retained; CPCV is primary)
│   │   ├── feedback_loader.py # SHA-256 signed Parquet feedback bundles
│   │   ├── data_validator.py  # Training data integrity gates
│   │   ├── checkpoint_manager.py # Redis-backed training checkpoints
│   │   └── tuner.py           # Shared HPO utilities
│   ├── evaluation/
│   │   ├── cpcv.py            # Combinatorial Purged CV (8 groups, 2 held, embargo 5)
│   │   ├── deflated_sharpe.py # DSR + PBO (Bailey et al. 2016)
│   │   ├── backtest.py        # Full backtest (long-only, CNC, 5 bps slippage)
│   │   ├── event_backtest.py  # News-event-conditioned backtest
│   │   ├── metrics.py         # Shared metric functions
│   │   ├── shap_validator.py  # SHAP feature importance validation
│   │   └── promotion_report.py # Challenger vs incumbent comparison
│   ├── inference/
│   │   ├── prediction_engine.py   # Cache → features → predict → audit
│   │   ├── ensemble_predictor.py  # Weighted Treelite + ONNX fusion
│   │   ├── registry_loader.py     # Bootstrap + self-healing loader (SHA-256 verify)
│   │   ├── calibrator.py          # Isotonic regression probability calibration
│   │   ├── feature_loader.py      # Feature materialisation for inference
│   │   ├── shap_explainer.py      # On-demand SHAP explanations
│   │   └── onnx_converter.py      # ONNX export utility
│   ├── monitoring/
│   │   ├── drift_detector.py  # Evidently KS-test on live vs training distributions
│   │   ├── drift_scheduler.py # 300-s worker loop
│   │   ├── metrics.py         # Prometheus ML metrics
│   │   └── latency.py         # Prediction latency instrumentation
│   └── audit/
│       └── audit_logger.py    # Immutable prediction audit trail → ml_audit_logs
│
└── ai/
    ├── ingestion/             # RSS fetching + event parsing
    ├── intelligence/          # Ollama LLM · spaCy NER · FinBERT · fake-news detection
    ├── fusion/                # Signal assembly · pipeline · domain models
    ├── strategy/              # Regime detector · strategy orchestrator
    ├── safety/                # Safety trigger engine · kill switch manager
    ├── governance/            # unified_model_registry · AI-layer drift detector
    └── correlation/           # Scanner × news anomaly correlation engine
```

---

## 5. Frontend Architecture

```
frontend/src/
├── app/                       # Next.js 16 App Router — 19 pages
│   ├── page.tsx               # Main dashboard (stock detail + 3 analysis cards)
│   ├── login/                 # Login
│   ├── scanner/               # Market scanner
│   ├── hawk-eye-radar/        # Hawk Eye ML scanner
│   ├── cortex-ai/             # CAI intelligence dashboard
│   ├── stocks/[symbol]/       # Per-symbol candlestick + AI analysis
│   ├── strategies/            # Strategy marketplace (browse, create, detail,
│   │                          #   edit, backtests, activations)
│   ├── settings/              # User preferences
│   └── admin/                 # Admin panel
│       ├── audit/             # Prediction audit log viewer
│       ├── governance/        # Model promote / demote UI
│       ├── training/          # ML training console (live WS log stream)
│       ├── users/             # User management
│       └── strategies/        # Strategy governance
│
├── components/
│   ├── ui/                    # shadcn/ui primitives
│   ├── AnalysisCardsSection.tsx  # ML Pattern + Sentiment + Prediction summary
│   ├── MLPatternCard.tsx      # TA-Lib pattern display
│   ├── PredictionSummaryCard.tsx # Ensemble prediction with confidence
│   └── HealthCheckWrapper.tsx # Liveness-gated page wrapper
│
├── hooks/                     # Custom React hooks (domain-aligned)
│   ├── useChart.ts            # lightweight-charts v5 lifecycle
│   ├── useMLActivity.ts       # ML prediction activity (Redis-backed)
│   ├── useTrainingRunStream.ts# Admin training WS stream
│   └── [useSignals, useRegime, useEvents, useModels, useCAIWebSocket, …]
│
└── lib/
    ├── api.ts / api-client.ts # Authenticated fetch wrappers
    ├── chart-policy.ts        # Chart data fallback + staleness policy
    └── candle-transforms.ts   # OHLCV → CandlestickData (lightweight-charts v5 API)
```

**State management**: TanStack Query 5 for all server state (deduplication, background refetch, cache). No Redux or Zustand.

**Real-time**: 5 WebSocket connections (market feed, trade suggestions, paper P&L, CAI dashboard, admin training). SSE for scanner. Connection management uses in-band auth / reauth protocol (see `documentation/api/MARKET_FEED_WEBSOCKET_API.md`).

---

## 6. Data Architecture

### 6.1 Database Schema Overview (53 Tables)

**Hypertables** (TimescaleDB, partitioned by timestamp):

| Table | Compression | Retention | Purpose |
|---|---|---|---|
| `upstox_ticks` | 7 days | 90 days | Real-time Protobuf tick data |
| `upstox_ohlcv` | — | 2 years | Historical OHLCV bars |
| `ml_predictions` | — | 1 year | Model prediction records |
| `ml_drift_metrics` | — | — | Evidently KS drift scores |
| `ai_processed_events` | — | — | LLM-classified news events |
| `ai_trading_signals` | — | — | Fused trading signals |
| `post_close_monitors` | — | — | Post-session counterfactual tracking |

**Regular tables by domain** (46 tables):

| Domain | Tables |
|---|---|
| **Auth** | `users` · `refresh_tokens` · `user_preferences` |
| **Market** | `instrument_master` · `stock_ohlcv` · `watchlist_items` |
| **ML** | `ml_model_metadata` · `ml_features` · `ml_audit_logs` · `ml_prediction_outcomes` · `ml_feedback_errors` · `ml_experiments` |
| **AI** | `ai_raw_events` · `ai_event_classifications` · `ai_fake_news_flags` · `ai_source_credibility` · `ai_nlp_results` · `ai_kill_switches` · `ai_safety_triggers` · `ai_regime_detections` · `ai_active_strategies` · `ai_drift_reports` · `ai_ml_models` · `event_correlations` |
| **Trade signals** | `trade_suggestions` · `user_suggestion_compliance` |
| **Paper trading** | `portfolios` · `paper_orders` · `paper_positions` · `paper_fills` · `paper_pnl_snapshots` · `paper_trade_outcomes` · `paper_position_conversions` |
| **Strategies** | `strategies` · `strategy_activations` · `strategy_audit_logs` · `strategy_backtest_runs` · `strategy_trades` · `user_strategy_subscriptions` |
| **Fundamentals** | `company_fundamentals_profile` · `company_key_ratios` · `company_income_statement` · `company_balance_sheet` · `company_cash_flow` · `company_corporate_actions` · `company_competitors` · `company_share_holdings` |

> **Open item (R6)**: Three model-metadata tables coexist — `ml_model_metadata`, `ai_ml_models`, and `unified_model_registry`. Consolidation onto `unified_model_registry` as the single canonical record is tracked in the remediation roadmap.

### 6.2 Redis Data Structures

**Cache keys** (TTL varies by endpoint configuration):
```
cache:features:{symbol}:{timeframe}          → feature vector (300 s)
cache:prediction:{symbol}:{timeframe}        → prediction result (300–3600 s)
cache:ohlcv:{symbol}:{interval}:{date}       → OHLCV bars
cache:scan_results:latest                    → scanner output (30 s market / 900 s closed)
cache:suggestions:list:{user_id}             → suggestion list (30 s)
cache:signal:on_demand:{symbol}              → on-demand signal (900 s)
worker:heartbeat                             → worker liveness (60 s TTL)
ml:training:checkpoint:{run_id}             → training progress state
```

**Pub/Sub channels**:
```
market_data.tick                             → per-instrument real-time tick
market_data.candle.{symbol}.{interval}       → OHLCV bar update
ml.prediction.{symbol}                       → ML prediction event
ai.event_classified                          → classified news event
fusion.signal.{symbol}                       → fused trading signal
safety.trigger_activated                     → safety trigger alert
regime.detected                              → regime change notification
SUGGESTIONS_NEW                              → cache-invalidation trigger
suggestion.expired.{id}                      → suggestion expiry event
```

**Auth / session**:
```
session:family:{family_id}                   → Set of active JTIs
session:revoked:{jti}                        → revocation timestamp
ratelimit:user:{user_id}                     → SlowAPI token bucket
```

### 6.3 ML Data Lineage

```
Upstox REST (OHLCV backfill)
Upstox WSS (real-time ticks)           ──► upstox_ohlcv (TimescaleDB)
                                                │
ai_processed_events (FinBERT sentiment) ────────┤
company_key_ratios / fundamentals ──────────────┤
                                                ▼
                                    FeaturePipeline
                                    (69 features, cross-sectional normalisation)
                                                │
                                                ▼  Redis cache (300 s)
                                    TrainingOrchestrator
                                    ├─ CPCV validation (8 groups, embargo 5)
                                    ├─ Optuna HPO (XGBoost 100t / GRU 5t)
                                    ├─ Deflated Sharpe quality gate
                                    └─ Isotonic calibration
                                                │
                                                ▼
                                    models/production/
                                    1.1.1_xgboost.json + .so (Treelite)
                                    1.1.1_gru.keras + .onnx
                                    1.1.1_*_calibrator.pkl
                                    metadata.json (pointer) + 1.1.1_metadata.json
                                                │ SHA-256 verified at every load
                                                ▼
                                    RegistryLoader → EnsemblePredictor
                                                │
                                                ▼
                                    ml_predictions (hypertable) + Redis pub/sub
                                                │
                                                ▼
                                    PaperTradingEngine
                                                │
                                                ▼
                                    paper_trade_outcomes + post_close_monitors
                                                │
                                                ▼
                                    FeedbackLoader (SHA-256 Parquet bundles)
                                    → challenger retrain input
```

---

## 7. Security Architecture

### 7.1 Authentication & Authorization

**JWT token structure**:
```json
{
  "sub": "user_id",
  "type": "access",
  "role": "trader",
  "exp": 1748956800,
  "iat": 1748955000,
  "jti": "uuid-v4"
}
```

**Token lifecycle**:
1. Login → access token (30 min, HS256) + refresh token (7 days)
2. Both delivered as HttpOnly cookies — never in the response body
3. Rotation: refresh token is single-use; a new pair is issued on every refresh
4. Family revocation: logout revokes all JTIs in the family (Redis set `session:family:{id}`)
5. Periodic cleanup: expired tokens purged from DB and Redis

**RBAC permission matrix**:

| Endpoint group | Viewer | Trader | Admin |
|---|---|---|---|
| Market data · ML predictions · fundamentals | ✅ | ✅ | ✅ |
| Generate signals · paper trades | ❌ | ✅ | ✅ |
| Strategy creation + activation | ❌ | ✅ | ✅ |
| Kill switch · model promotion / demotion | ❌ | ❌ | ✅ |
| User management · ML training console | ❌ | ❌ | ✅ |

**Config hardening** (`core/config.py`):
- `SECRET_KEY`: required, min 32 chars, placeholder-rejecting field validator
- `CORS_ALLOWED_ORIGINS`: explicit allowlist required — model validator refuses `*` and refuses to start
- `TrustedHostMiddleware` wired in `main.py`
- No `CERT_NONE` / `check_hostname=False` anywhere in the codebase (verified via audit 2026-06-01)

### 7.2 Model Storage Security

Models are stored as **plaintext binary artifacts** with **SHA-256 integrity verification** at every load (`registry_loader._sha256_file`). There is no encryption-at-rest. Access control is enforced at the filesystem and infrastructure level. Artifact checksums are recorded in `models/production/models/1.1.1_metadata.json`.

> The `ML_MODEL_ENCRYPTION_KEY` setting was present in an older revision and has been removed from `config.py`. If it appears in a legacy `.env` file it is harmless — the app ignores unknown env vars (`extra="ignore"`).

### 7.3 Rate Limiting

SlowAPI is wired globally. Per-route defaults (overridable via env vars):

| Route group | Default |
|---|---|
| Scanner | 10/minute |
| Instrument search | 60/minute |
| Market data | 120/minute |
| ML predictions | 100/minute |

### 7.4 Additional Controls

| Control | Implementation |
|---|---|
| SQL injection | SQLAlchemy parameterised queries throughout — no raw SQL with user input |
| XSS | React auto-escaping + Content-Security-Policy headers |
| CSRF | SameSite cookies + CORS origin allowlist |
| Audit trail | All ML predictions logged to `ml_audit_logs` (append-only) |
| Request tracing | Correlation-ID injected per request; propagated through all log records |
| Secrets at startup | All required secrets validated on boot; placeholders rejected; app refuses to start on failure |

---

## 8. ML System Architecture (Deep Dive)

### 8.1 Training Pipeline

```
1. SymbolSelector
   ├─ Filter: NSE EQ equities only (instrument_master)
   ├─ IPO quarantine: 180-day post-listing
   ├─ Coverage gate: ≥60% bar completeness over 10-year lookback
   └─ Output: 2,011 of 2,551 requested symbols (78.8% coverage)

2. FeaturePipeline  (cross-sectional — all symbols simultaneously)
   ├─ 44 technical features (ohlcv_features.py — cross-sectionally normalised,
   │   price-level invariant; price deviations relative to moving averages, etc.)
   ├─ 5 sentiment features (FinBERT scores from ai_processed_events)
   ├─ 20 fundamental features (company_key_ratios et al.)
   └─ 69 total features · sequence_length = 60 bars for GRU

3. TargetGenerator
   └─ Binary labels via ATR dead zone:
      forward_return > +threshold → 1 (UP)
      forward_return < −threshold → 0 (DOWN)
      |forward_return| ≤ threshold → NaN  (excluded — dead zone)
      threshold = 0.5 × 14-day ATR / close  (clamped ≥ 0.001)
      → ~50/50 natural class balance on retained samples
      → ~20–30% dead zone (noise filtered from training)

4. CPCV (Combinatorial Purged Cross-Validation)
   └─ 8 groups · 2 held-out · horizon = 5 bars · embargo = 5 bars
      Prevents lookahead leakage across folds.
      Produces 7 combinatorially independent backtest paths.

5. Hyperparameter Optimisation
   ├─ XGBoost: 100 Optuna trials (TPE sampler)
   └─ GRU: 5 Keras Tuner trials (max_epochs=200, early_stopping_patience=20)
      GRU VRAM capped at 1.6 GB for co-existence with live inference

6. Ensemble Weight Optimisation
   └─ Sharpe-maximising grid search (w ∈ [0,1], step 0.005)
      200 CPCV paths · PBO regularisation (L2 prior = 0.1)
      w_star = 0.5106 (XGBoost), 0.4894 (GRU)
      Gate: ensemble DSR must exceed best standalone DSR

7. Probability Calibration
   └─ Isotonic regression per model (ConfidenceCalibrator)

8. Quality Gate
   ├─ Ensemble deflated Sharpe > best standalone deflated Sharpe ✅
   ├─ PBO < 50% ✅ (9.5%)
   └─ Accretive: ensemble Sharpe > both standalones ✅

9. Artefact Export
   ├─ XGBoost: .json (native) + Treelite-compiled .so (inference)
   ├─ GRU: .keras (training archive) + ONNX (inference)
   ├─ Calibrators: .pkl (isotonic, one per model)
   └─ metadata.json per version (checksums, feature manifest, gate metrics)
```

### 8.2 Inference Path

```
POST /api/v1/ml/predict
    ↓
PredictionEngine.predict()
    ├─ Redis hit? → return cached result (300 s / 3600 s TTL)
    └─ miss → FeatureLoader.load_features()
                   ├─ Redis feature cache hit? → use cached
                   └─ miss → FeaturePipeline.compute_features()
                              (OHLCV from TimescaleDB + fundamentals + events)
                              → cache in Redis
    ↓
EnsemblePredictor.predict()
    ├─ XGBoost Treelite .so (CPU, sub-millisecond)
    └─ GRU ONNX Runtime (GPU, shares GPU with FinBERT)
    → weighted sum (0.5106 XGB + 0.4894 GRU) → raw probability
    ↓
ConfidenceCalibrator.calibrate()
    → isotonic-calibrated probability
    ↓
Selective Classification:
    P(UP) ≥ confidence_threshold → signal = UP
    P(UP) ≤ 1 − confidence_threshold → signal = DOWN
    otherwise → signal = HOLD  (not a training label)
    ↓
AuditLogger.log_prediction() → ml_audit_logs (append-only)
    ↓
return PredictionResponse
```

### 8.3 Model Governance Lifecycle

```
Training run  →  challenger dir (models/production/challenger_{ts}/)
                       │
           (human review: metrics report, DSR, backtest, promotion_report.py)
                       │
               promote_model.py staging --version <v>
                       │  status = staging  (shadow paper trading, 7-day observation)
                       │
               reeval_production_model.py  →  exit 0 (MEETS_BAR) or 2 (DEMOTE_RECOMMENDED)
                       │
               promote_model.py production --version <v>
                       │  status = production / active
                       │
               RegistryLoader.bootstrap_production_models()
               (idempotent; self-healing: repairs stale onnx_path, recomputes checksum)
```

**Rollback**: `promote_model.py rollback --model-name xgboost|gru` transitions back to the previous production version.

### 8.4 TFT Status (Architecture Decision)

Temporal Fusion Transformer was benchmarked on the current build host (`bench_tft_cpu.py`). CPU inference latency was infeasible for the real-time signal path (WSL2, 4 GB GPU, CUDA 12.2 ceiling). Excluded from the production ensemble. Decision recorded in `models/production/metadata.json §architecture`. Revisit when a 16+ GB GPU with CUDA 12.9+ is available.

---

## 9. Operational Architecture

### 9.1 Observability Stack

**Prometheus** (`:9090`): scrapes FastAPI's `/metrics` endpoint. `prometheus.yml` uses `host.docker.internal:8000` for Docker deployments. Bare-metal deployments point to `localhost:8000`.

**Grafana** (`:3001`): 6 dashboards:
1. API performance (request rate, latency p50/p95/p99, error rate)
2. ML prediction health (accuracy gauge, prediction volume, cache hit rate)
3. Worker loop health (iterations per loop, duration, error rate)
4. Redis stats (cache hit rate, pub/sub throughput, memory)
5. DB connection pool (active connections, query latency)
6. Safety & governance (kill switch events, model deployment state)

**Prometheus metric catalogue**:

```
# HTTP
http_requests_total{method, endpoint, status_code}
http_request_duration_seconds{method, endpoint}

# ML
ml_predictions_total{symbol, model_type}
ml_prediction_latency_ms{model_type}
ml_model_accuracy_score{model_id}
ml_drift_detected_total{feature_name}
ml_model_load_failures_total{model_name}
model_deployment_state{model_name}          ← gauge: 0=unloaded / 1=loaded

# Worker
worker_loop_iterations_total{loop_name}
worker_loop_duration_seconds{loop_name}
worker_errors_total{loop_name}

# Paper trading
paper_trade_pnl_total{symbol}
paper_trade_charges_total{charge_type}
```

**Structured logging**: JSON format via `core/logging_config.py`. Correlation-ID (`X-Request-ID`) injected by `middleware/request_id.py` and propagated through all log records in the request scope. Log level: `INFO` (production), `DEBUG` (development).

**Alerting thresholds**:

| Alert | Condition | Severity |
|---|---|---|
| High error rate | >5% 5xx errors in 5 min | Critical |
| ML model unloaded | `model_deployment_state = 0` | Critical |
| Drift detected | `ml_drift_detected_total` increments | Warning |
| Kill switch activated | any activation event | Critical |
| Worker heartbeat missing | `worker:heartbeat` key expired in Redis | Warning |

### 9.2 Background Service Management

```bash
# Always-on worker (11 loops)
sudo cp backend/cortex-worker.service /etc/systemd/system/
sudo systemctl enable --now cortex-worker.service
# Memory cap: 2 GB · CPU cap: 200% · EnvironmentFile: backend/.env

# Weekly scheduled challenger retrain (off-band, never auto-promotes)
sudo cp deploy/cortex-retrain.{service,timer} /etc/systemd/system/
sudo systemctl enable --now cortex-retrain.timer

# Monitoring
systemctl list-timers cortex-retrain.timer
journalctl -u cortex-worker.service -f
journalctl -u cortex-retrain.service --since "1 week ago"
```

### 9.3 Operational Scripts

| Script | Purpose |
|---|---|
| `preflight_check.py` | Full environment validation before service start |
| `reeval_production_model.py` | C3 audit report: exit 0 = MEETS_BAR, exit 2 = DEMOTE_RECOMMENDED |
| `promote_model.py` | Model lifecycle transitions (staging → production, rollback, demote) |
| `production_training_orchestrator.py --fresh` | Full challenger retrain (~8–10 h, GPU required) |
| `scheduled_retrain.py --once` | Single retrain invocation (equivalent to timer firing) |
| `repair_model_registry.py` | Fix stale registry records (onnx_path / checksum mismatch) |
| `backfill_fundamentals.py` | One-time fundamentals historical backfill at 1 req/s |
| `smoke_test_e2e.py` | End-to-end smoke test (API + model + signal flow) |
| `locustfile.py` | Locust load test harness for API throughput benchmarking |

### 9.4 Deployment

**Docker Compose** (`docker-compose.yml`): TimescaleDB · Redis · Ollama · Prometheus · API · Frontend. The API container runs `alembic upgrade head` before starting uvicorn. ML artefacts live in the `ml_models` Docker volume.

**Bare metal** (required for GPU training): See `README.md §Migration checklist` for the full step-by-step procedure including Python version pins and GPU verification.

**Two `.env` files**: root (read by Docker Compose) and `backend/.env` (read by bare-metal processes and systemd). Keep them in sync; divergence silently uses the wrong database URL.

---

## 10. RAD-AI Extensions

### E1: AI Boundary Delineation

| Component | Type | Confidence Spec | Fallback |
|---|---|---|---|
| **ML Ensemble (XGB + GRU)** | Binary direction + HOLD | 68.0% directional · DSR 0.73 · PBO 9.5% | Cached prediction or HOLD |
| **LLM Event Classifier** | Category + reasoning | ≥80% on labelled set | Rule-based keyword matching |
| **Fake News Detector** | Binary flag + score | Precision ≥75% | Default "unverified" |
| **Regime Detector** | trending / ranging / volatile | ≥70% accuracy | Default "ranging" |
| **FinBERT Sentiment** | Score −1..+1 | Validated on financial corpora | Raw keyword polarity |
| **Drift Detector** | Binary flag + KS-statistic | Alert at 2σ | Alert only; no automatic action |

### E2: Model Registry (Current Production State)

| Field | Value |
|---|---|
| **Production version** | **1.1.1** |
| **Trained at** | 2026-06-01T03:22:26Z |
| **Promoted at** | 2026-06-01T11:43:00Z |
| **Training run** | `training_results_20260601_032246.json` |
| **Symbol universe** | 2,011 NSE EQ (78.8% of 2,551 requested) |
| **Feature version** | 1.0.0 — 69 features |
| **Sequence length** | 60 bars (GRU) |
| **Label scheme** | Binary UP/DOWN · HOLD at inference (Selective Classification) |
| **Ensemble weights** | XGBoost 0.5106 / GRU 0.4894 |
| **Directional accuracy** | 68.0% (30 k held-out) |
| **Sharpe / DSR / PBO** | 1.09 / 0.73 / 9.5% |
| **Inference artefacts** | `1.1.1_xgboost_inference.so` · `1.1.1_gru_inference.onnx` |
| **Model card** | `models/production/models/1.1.1_metadata.json` |
| **Registry source of truth** | `unified_model_registry` (PostgreSQL) · `metadata.json` is a human-readable pointer |

Previous versions: `1.1.0` (archived — training origin), all ≤`1.0.x` demoted / archived.

### E3: Data Pipeline Quality Gates

| Stage | Gate | Threshold | Action on failure |
|---|---|---|---|
| Ingestion | Schema + timestamp validation | 100% | Reject record, log error |
| Feature engineering | Null percentage | <5% per feature | Impute with median, log warning |
| Feature engineering | Value range | Within 3σ | Clip to bounds, log warning |
| Training | CPCV all paths positive Sharpe | 7/7 paths | Reject training run |
| Training | Deflated Sharpe gate | DSR > best standalone | Reject model |
| Inference | Confidence threshold | Configurable | Emit HOLD, do not emit UP/DOWN |
| Inference | Latency SLA | <2 s | Alert on-call |
| Drift | KS-statistic | <2σ | Alert, schedule human review |

### E4: Responsible AI

| Component | Explainability | Human oversight | Privacy |
|---|---|---|---|
| **ML Ensemble** | SHAP values on demand (`shap_explainer.py`) | Predictions reviewed before trade execution; admin can demote at any time | No PII — public market data only |
| **LLM Classifier** | Structured reasoning in output | Fake-news flags reviewable by admin | No PII — public news only |
| **Signal Fusion** | Per-component weights logged per signal | Kill switch suppresses all signals instantly | No PII |
| **Paper Trading** | Full statutory charge breakdown per trade | Trader reviews P&L before converting to live | No PII |

### E5: Architecture Decision Records

**ADR-001: Cross-sectional ensemble over per-symbol models**  
*Prior state*: One model per symbol+timeframe → hundreds of artefacts, no cross-stock generalisation, sparse data for small caps.  
*Decision*: Single cross-sectional model on all eligible NSE EQ symbols; features cross-sectionally normalised (scale-invariant).  
*Outcome*: 2,011-symbol training set, dramatically reduced artefact count, better generalisation on low-liquidity names.

**ADR-002: CPCV over walk-forward validation**  
*Prior state*: Walk-forward produced path-dependent splits with implicit in-sample leakage.  
*Decision*: Combinatorial Purged CV (Bailey et al.) — 8 groups, 2 held-out, 5-bar embargo. Produces 7 independent backtest paths enabling PBO estimation.  
*Outcome*: Stronger overfitting protection, DSR computable, path distribution verifiable.

**ADR-003: Binary labels with ATR dead zone over 3-class**  
*Prior state*: 3-class (UP/DOWN/NEUTRAL) training labels. NEUTRAL class was large, noisy, and degraded decision boundary quality.  
*Decision*: Binary training labels with symmetric ATR dead zone. HOLD is a runtime confidence-threshold policy, not a training label.  
*Outcome*: Natural ~50/50 class balance on retained samples, cleaner boundaries, HOLD rate independently tunable at inference.

**ADR-004: TFT excluded from production ensemble**  
*Context*: TFT benchmarked — CPU inference latency infeasible on current 4 GB GPU / WSL2 CUDA 12.2 ceiling.  
*Decision*: Excluded. Revisit when 16+ GB GPU with CUDA 12.9+ available.  
*Outcome*: Production ensemble is XGBoost + GRU only; TFT decision recorded in `models/production/metadata.json`.

**ADR-005: Plaintext binary + SHA-256 over Fernet encryption-at-rest**  
*Prior state*: `ML_MODEL_ENCRYPTION_KEY` config field existed (Fernet AES-128) but was never consumed — models stored plaintext.  
*Decision*: Remove the dead config field; document the actual control (plaintext + SHA-256 integrity on every load). Access control is at infrastructure level.  
*Outcome*: Config reflects reality; no false appearance of encryption-at-rest.

### E6: Quality Scenarios

**Scenario 1: Ensemble below DSR gate**  
Trigger: CPCV evaluation shows ensemble DSR ≤ best standalone DSR.  
Response: `production_training_orchestrator.py` exits non-zero; no artefacts written to production.  
Measure: Gate failure detected within seconds of evaluation.

**Scenario 2: Feature drift detected**  
Trigger: KS-statistic for ≥1 feature exceeds 2σ threshold.  
Response: `ml_drift_detected_total` increments; Grafana alert fires; ML engineer notified.  
Measure: Alert within 300 s (drift check interval).

**Scenario 3: Registry artefact missing or corrupt**  
Trigger: SHA-256 mismatch or file not found at startup.  
Response: `RegistryLoader.bootstrap_production_models` attempts self-healing (recompute checksum, repair `onnx_path`). If artefact absent, service starts without ML (prediction endpoints return 503).  
Measure: Self-healing or failure logged within startup sequence.

**Scenario 4: LLM timeout**  
Trigger: Ollama HTTP request exceeds 30 s.  
Response: `EventClassifier` falls back to rule-based keyword classification; fallback flag logged.  
Measure: Fallback within 30 s; total event processing < 35 s.

**Scenario 5: Kill switch activated**  
Trigger: Safety loop detects loss limit / volatility breach, or admin manual activation.  
Response: Kill switch set active; all signal fusion suppressed via circuit breaker; `safety.trigger_activated` published to Redis; Grafana alert fires.  
Measure: Signal suppression within one 30-s safety loop cycle.

### E7: AI Debt Register

| Item | Severity | Description | Tracking |
|---|---|---|---|
| **3 coexisting model metadata tables** | Medium | `ml_model_metadata`, `ai_ml_models`, `unified_model_registry` all present; ambiguous DB source of truth | R6 — next sprint |
| **GPU contention: training vs inference** | Medium | GRU training, Ollama, and FinBERT ONNX-GPU share one local GPU. Heavy retrain degrades live inference latency | R9 — next quarter |
| **No CI for test suite** | Medium | 82-file suite exists but not wired on every push; quality guarantees are manual | R2 — deferred |
| **Bleeding-edge pandas / torch pins** | Low | `pandas 3.0.2`, `torch 2.11.0` — compatibility risk on numerically sensitive pipeline | R7 — next sprint |
| **No hashed lockfile** | Low | `requirements.txt` uses `==` pins but no `pip-compile` / `uv` hashes | R7 — next sprint |
| **SLAs not load-tested** | Low | Latency targets asserted in architecture; no load-test harness in artefacts | R10 — next quarter |
| **Hidden feedback loop** | Medium | Traders may adjust behaviour based on ML signals, affecting future training labels | Open — no schedule |

### E8: Operational AI View

**Monitoring post-promotion checklist**:
- [ ] `model_deployment_state{model_name}` = 1 in Grafana
- [ ] `ml_prediction_latency_ms` within SLA
- [ ] `ml_drift_detected_total` counter stable
- [ ] `reeval_production_model.py` exits 0 (MEETS_BAR)
- [ ] `worker:heartbeat` key present in Redis
- [ ] Paper trading P&L converging (first few trades)

**Retraining policy**:
- Cadence: Weekly challenger run (systemd `cortex-retrain.timer`)
- Triggers: Also on-demand (`scheduled_retrain.py --once`) or on drift alert
- Human gate: Always required before promotion (`promote_model.py production`)
- Rollback SLA target: <15 minutes from decision to traffic-shifted to previous version

---

## Appendix: What Changed Since the April 2026 Revision

This document supersedes the April 14, 2026 revision. The following material changes have been made:

| Dimension | April 2026 (stale) | June 2026 (current) |
|---|---|---|
| Version | 1.0.0 | 1.1.1 |
| LOC | 32,806 | ~103 k (64 k backend + 39 k frontend) |
| ML accuracy | "87%" (uncited) | **68.0%** (measured, 30 k holdout) |
| Test coverage | "36/94 (38%)" | 82 files under version control |
| Frontend framework | Next.js 15+ | Next.js 16.1.6 / React 19.2.3 |
| ML labels | 3-class (UP/DOWN/NEUTRAL) | Binary (UP/DOWN) + HOLD at inference |
| Training method | Walk-forward, 60-day windows | CPCV, 10-year lookback |
| Ensemble weights | XGB 0.6 / GRU 0.4 (fixed) | XGB 0.5106 / GRU 0.4894 (Sharpe-optimised) |
| Features | "40+" | 69 (44 technical + 5 sentiment + 20 fundamental) |
| Model format | `.pkl.enc` (Fernet) | `.keras` / `.json` / `.so` / `.onnx` (plaintext + SHA-256) |
| Model scope | Per-symbol/timeframe | Cross-sectional, 2,011 symbols |
| DB tables | ~15 described | 53 (40 migrations) |
| Bounded contexts | 6 | 8 (added Strategy Marketplace, Fundamentals) |
| API routers | ~15 | 30 |
| Worker loops | 8 described | 11 |
| Observability | Mentioned | Prometheus + Grafana (6 dashboards) deployed |
| Paper trading | Not described | Full NSE charge modelling + post-close monitoring |
| Model encryption | Fernet AES-128 (claimed) | Removed — plaintext + SHA-256 (actual) |

**Scope of this document**: Static analysis of the repository as of 2026-06-01. Live database state, runtime/dynamic behaviour, test execution results, load testing, and penetration testing remain out of scope. For test artefacts, see `documentation/testing/`. For the cross-module graph, see `graphify-out/GRAPH_REPORT.md`.
