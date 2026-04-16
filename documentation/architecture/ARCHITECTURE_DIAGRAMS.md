# Cortex AI Architecture - Visual Diagrams

## System Context Diagram (C1)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                                                                                 │
│                         CORTEX AI TRADING PLATFORM                              │
│                                                                                 │
│  ┌───────────────────────────────────────────────────────────────────────────┐ │
│  │                    DETERMINISTIC BOUNDARY                                  │ │
│  │  • Authentication & Authorization (JWT + RBAC)                             │ │
│  │  • Market Data Ingestion (REST + WebSocket)                                │ │
│  │  • Database Operations (CRUD, Time-series queries)                         │ │
│  │  • API Gateway & Rate Limiting                                             │ │
│  └───────────────────────────────────────────────────────────────────────────┘ │
│                                                                                 │
│  ┌───────────────────────────────────────────────────────────────────────────┐ │
│  │              NON-DETERMINISTIC BOUNDARY (AI/ML)                            │ │
│  │                                                                             │ │
│  │  • ML Prediction Engine (XGBoost + GRU Ensemble)                           │ │
│  │    Confidence: ≥87% accuracy @ P95 latency <100ms                          │ │
│  │    Fallback: Return cached predictions on model failure                    │ │
│  │                                                                             │ │
│  │  • LLM Intelligence Layer (Ollama Llama 3.1 8B)                            │ │
│  │    Confidence: Event classification with reasoning                         │ │
│  │    Fallback: Rule-based classification on LLM timeout                      │ │
│  │                                                                             │ │
│  │  • Regime Detection (Statistical + ML Hybrid)                              │ │
│  │    Confidence: Regime classification (trending/ranging/volatile)           │ │
│  │    Fallback: Default to "ranging" regime on detection failure              │ │
│  └───────────────────────────────────────────────────────────────────────────┘ │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
         ▲                                    ▲                          ▲
         │                                    │                          │
    ┌────┴────┐                          ┌───┴───┐                 ┌────┴────┐
    │ Traders │                          │ Admins│                 │Analysts │
    │ (trader)│                          │(admin)│                 │(viewer) │
    └─────────┘                          └───────┘                 └─────────┘
         │                                    │                          │
         └────────────────────────────────────┴──────────────────────────┘
                                         │
                                    HTTPS/WSS
                                         │
         ┌───────────────────────────────┴───────────────────────────────┐
         │                                                                 │
    ┌────┴────┐                    ┌──────────┐                    ┌─────┴─────┐
    │ Upstox  │                    │   RSS    │                    │  Ollama   │
    │   API   │                    │  Feeds   │                    │   (LLM)   │
    │(External)                    │(External)│                    │  (Local)  │
    └─────────┘                    └──────────┘                    └───────────┘
```

## Bounded Context Map (DDD)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         BOUNDED CONTEXT MAP                                     │
└─────────────────────────────────────────────────────────────────────────────────┘

                              ┌─────────────────────┐
                              │  Market Data        │
                              │  Context            │◄──── Upstox API
                              │                     │      (REST + WS)
                              │  Team: Backend      │
                              │  Infra              │
                              └──────────┬──────────┘
                                         │
                         Redis Pub/Sub: market_data.tick
                                         │
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                    ▼
        ┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
        │ Signal            │ │ AI Intelligence   │ │ User Interface    │
        │ Intelligence      │ │ Context           │ │ Context           │
        │ Context (ML)      │ │                   │ │                   │
        │                   │ │ Team: AI Research │ │ Team: Frontend    │
        │ Team: ML Eng.     │ └─────────┬─────────┘ └───────────────────┘
        └─────────┬─────────┘           │
                  │                     │
    Redis: ml.prediction    Redis: ai.event_classified
                  │                     │
                  └──────────┬──────────┘
                             ▼
                  ┌───────────────────┐
                  │ Signal Fusion     │
                  │ Context           │
                  │                   │
                  │ Team: Trading     │
                  │ Strategy          │
                  └─────────┬─────────┘
                            │
              Redis: fusion.signal
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
    ┌───────────────────┐   ┌───────────────────┐
    │ Risk & Safety     │   │ Strategy          │
    │ Context           │   │ Orchestration     │
    │                   │   │ Context           │
    │ Team: Risk Mgmt   │   │                   │
    └───────────────────┘   │ Team: Trading     │
                            │ Strategy          │
                            └───────────────────┘
```

## Container Deployment Diagram (C2)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         DEPLOYMENT ARCHITECTURE                                 │
└─────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────┐
│   Web Browser    │
│  (Next.js SPA)   │
│  Port: 3000      │
└────────┬─────────┘
         │ HTTPS (REST + WebSocket)
         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          BACKEND CONTAINERS                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │  FastAPI Application (Uvicorn)                                            │ │
│  │  Port: 8000                                                                │ │
│  │  • REST API endpoints (/api/v1/*)                                         │ │
│  │  • WebSocket endpoints (/ws/*)                                            │ │
│  │  • JWT authentication middleware                                          │ │
│  │  • Rate limiting (Redis-backed)                                           │ │
│  │  • CORS handling                                                          │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                                 │
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │  Async Worker Process (Python)                                            │ │
│  │  • RSS ingestion loop (300-900s interval)                                 │ │
│  │  • Event processing loop (continuous)                                     │ │
│  │  • Regime detection loop (60s interval)                                   │ │
│  │  • Safety monitoring loop (30s interval)                                  │ │
│  │  • Drift detection scheduler (300s interval)                              │ │
│  │  • Heartbeat loop (10s interval)                                          │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
         │                                    │
         │ PostgreSQL                         │ Redis Protocol
         │ (asyncpg)                          │
         ▼                                    ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         DATA LAYER CONTAINERS                                   │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │  TimescaleDB (PostgreSQL 14+)                                             │ │
│  │  Port: 5432                                                                │ │
│  │  • Hypertables: upstox_ticks, upstox_ohlcv, ml_predictions               │ │
│  │  • Compression: 7-day policy on tick data                                 │ │
│  │  • Retention: 90 days for ticks, 2 years for OHLCV                       │ │
│  │  • Connection pool: 20 connections, 10 overflow                           │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                                 │
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │  Redis 7+                                                                  │ │
│  │  Port: 6379                                                                │ │
│  │  • Cache: Feature vectors, predictions (TTL: 300s)                        │ │
│  │  • Pub/Sub: Real-time signal distribution                                 │ │
│  │  • Rate limiting: Token bucket per user/IP                                │ │
│  │  • Session store: JWT family tracking, revocation list                    │ │
│  │  • Max connections: 50                                                    │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
         │
         │ HTTP
         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         AI/ML CONTAINERS                                        │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│  ┌──────────────────────────────────────────────────────────────────────────┐ │
│  │  Ollama (LLM Inference Server)                                            │ │
│  │  Port: 11434                                                               │ │
│  │  • Model: Llama 3.1 8B (quantized)                                        │ │
│  │  • GPU: CUDA-enabled (optional, CPU fallback)                             │ │
│  │  • Timeout: 30s per request                                               │ │
│  │  • Concurrency: 5 parallel requests                                       │ │
│  └──────────────────────────────────────────────────────────────────────────┘ │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

## ML Data Pipeline (RAD-AI E3)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         ML DATA PIPELINE                                        │
└─────────────────────────────────────────────────────────────────────────────────┘

Stage 1: Data Ingestion
├─ Source: Upstox API (REST + WebSocket)
├─ Quality Gate: Schema Validation (100% compliance)
├─ Quality Gate: Timestamp Validation (100% compliance)
└─ Output: TimescaleDB.upstox_ticks, upstox_ohlcv
         │
         ▼
Stage 2: Feature Engineering
├─ Input: TimescaleDB.upstox_ohlcv (60-day window)
├─ Transformation: Technical indicators (RSI, MACD, Bollinger, etc.)
├─ Quality Gate: Null Percentage (<5%)
├─ Quality Gate: Value Range (within 3σ)
└─ Output: Redis cache (features:{symbol}:{timeframe})
         │
         ▼
Stage 3: Model Training
├─ Input: Cached features + labels (target_generator)
├─ Transformation: Train/test split (80/20), walk-forward validation
├─ Quality Gate: Accuracy Threshold (≥85%)
├─ Quality Gate: Overfitting Check (train-test gap <10%)
└─ Output: Encrypted model artifacts (backend/models/production/)
         │
         ▼
Stage 4: Model Inference
├─ Input: Cached features (from Stage 2)
├─ Transformation: Model.predict() → ensemble fusion
├─ Quality Gate: Confidence Threshold (≥0.5)
├─ Quality Gate: Latency SLA (P95 <250ms)
└─ Output: TimescaleDB.ml_predictions, Redis pub/sub
         │
         ▼
Stage 5: Drift Detection
├─ Input: Production features vs. training features
├─ Transformation: Kolmogorov-Smirnov test per feature
├─ Quality Gate: Drift Threshold (KS-statistic <0.1)
└─ Output: TimescaleDB.ml_drift_metrics
```

## Model Lifecycle (RAD-AI E8)

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         MODEL LIFECYCLE                                         │
└─────────────────────────────────────────────────────────────────────────────────┘

┌──────────────┐
│   Training   │
│   Complete   │
└──────┬───────┘
       │
       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  SHADOW STAGE                                                                 │
│  • Model runs in parallel with live model                                    │
│  • Predictions logged but not used                                           │
│  • Duration: 24 hours minimum                                                │
│  • Promotion Criteria: Accuracy ≥85%, latency <250ms, no errors             │
└──────┬───────────────────────────────────────────────────────────────────────┘
       │ Promote (manual approval)
       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  PAPER STAGE                                                                  │
│  • Model predictions used for simulated trading (no real orders)             │
│  • Duration: 7 days minimum                                                  │
│  • Promotion Criteria: Paper profit >0, Sharpe >1.0, max drawdown <5%       │
└──────┬───────────────────────────────────────────────────────────────────────┘
       │ Promote (manual approval)
       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  LIVE STAGE                                                                   │
│  • Model predictions used for real trading                                   │
│  • Continuous monitoring (drift, accuracy, latency)                          │
│  • Rollback Triggers: Accuracy <75%, latency >500ms, error rate >5%         │
└──────┬───────────────────────────────────────────────────────────────────────┘
       │ Rollback or Archive (after 30 days)
       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  ARCHIVED STAGE                                                               │
│  • Model replaced by newer version                                           │
│  • Artifacts retained for audit/rollback                                     │
│  • Retention: 1 year                                                         │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Security Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         SECURITY LAYERS                                         │
└─────────────────────────────────────────────────────────────────────────────────┘

┌──────────────────┐
│   Web Browser    │
└────────┬─────────┘
         │ HTTPS/TLS 1.3
         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Layer 1: Network Security                                                      │
│  • HTTPS only (no HTTP fallback)                                                │
│  • CORS with explicit origin allowlist                                          │
│  • Security headers (CSP, HSTS, X-Frame-Options)                                │
└─────────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Layer 2: Authentication                                                        │
│  • JWT (HS256) with 30-minute expiration                                        │
│  • Refresh token (7 days) with rotation                                         │
│  • Family tracking (revoke all tokens in session)                               │
│  • HTTP-only cookies (prevent XSS)                                              │
└─────────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Layer 3: Authorization (RBAC)                                                  │
│  • Viewer: Read-only access to market data, predictions                         │
│  • Trader: Execute trades, generate signals                                     │
│  • Admin: Manage users, promote models, activate kill switch                    │
└─────────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Layer 4: Rate Limiting                                                         │
│  • IP-based: 100 req/min (anonymous)                                            │
│  • User-based: 200 req/min (authenticated)                                      │
│  • ML-specific: 50 predictions/min per user                                     │
│  • Algorithm: Token bucket (Redis-backed)                                       │
└─────────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Layer 5: Data Protection                                                       │
│  • Model encryption: Fernet (AES-128-CBC + HMAC-SHA256)                         │
│  • Password hashing: bcrypt (cost factor 12)                                    │
│  • SQL injection prevention: SQLAlchemy parameterized queries                   │
│  • XSS prevention: React auto-escaping, CSP headers                             │
└─────────────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│  Layer 6: Audit Logging                                                         │
│  • All ML predictions logged (user, timestamp, prediction, confidence)          │
│  • Model promotions logged (admin, timestamp, model_id, status)                 │
│  • Kill switch activations logged (admin, timestamp, reason)                    │
│  • Retention: 1 year                                                            │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

**For complete architecture details, see `ARCHITECTURE.md`**
