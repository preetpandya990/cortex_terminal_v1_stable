# Cortex AI Architecture - Quick Reference

**Full Documentation**: See `ARCHITECTURE.md` (2,499 lines)

## 📋 Document Structure

1. **Executive Summary** - System overview, metrics, tech stack
2. **System Context (C1)** - External systems, actors, boundaries
3. **Domain-Driven Design** - 6 bounded contexts with context map
4. **Container Architecture (C2)** - Deployment containers, protocols
5. **Component Architecture (C3)** - Backend/frontend structure
6. **Data Architecture** - Database schema, Redis structures, data lineage
7. **Security Architecture** - JWT, RBAC, encryption, rate limiting
8. **Operational Architecture** - Deployment, monitoring, backup, scaling
9. **RAD-AI Extensions** - AI-specific architecture (8 extensions)
10. **Architecture Decision Records** - 5 key ADRs
11. **Quality Attributes** - Performance, scalability, reliability, security
12. **Technology Radar** - Current stack, roadmap, technical debt
13. **Glossary** - Ubiquitous language per bounded context
14. **References** - Papers, books, documentation links
15. **Appendices** - Environment vars, API endpoints, troubleshooting

## 🏗️ 6 Bounded Contexts

```
┌─────────────────────┐
│  Market Data        │ ← Upstox API (REST + WebSocket)
│  Context            │   Owns: Tick ingestion, OHLCV storage
└──────┬──────────────┘
       │ publishes: market_data.tick
       ▼
┌─────────────────────┐        ┌─────────────────────┐
│  Signal Intelligence│        │  AI Intelligence    │
│  Context (ML)       │        │  Context (LLM)      │
│  Owns: ML models    │        │  Owns: Event class. │
└──────┬──────────────┘        └──────┬──────────────┘
       │ publishes: ml.prediction      │ publishes: ai.event
       ▼                               ▼
┌─────────────────────────────────────────────────────┐
│  Signal Fusion Context                              │
│  Owns: Multi-signal fusion, regime filtering        │
└──────┬───────────────────────────────────────────────┘
       │ publishes: fusion.signal
       ▼
┌─────────────────────┐        ┌─────────────────────┐
│  Risk & Safety      │        │  Strategy           │
│  Context            │        │  Orchestration      │
│  Owns: Kill switch  │        │  Owns: Regime detect│
└─────────────────────┘        └─────────────────────┘
```

## 🎯 Key Metrics (Production)

| Metric | Target | Actual | Status |
|--------|--------|--------|--------|
| ML Accuracy | ≥85% | 87% | ✅ |
| P95 Latency | <250ms | 90ms | ✅ 2.8x better |
| Throughput | >50 req/s | 54 req/s | ✅ |
| Cache Hit Rate | >80% | 82% | ✅ |

## 🔧 Technology Stack

**Backend**: FastAPI + SQLAlchemy 2.0 (async) + TimescaleDB + Redis  
**ML**: XGBoost + GRU (TensorFlow) + Ensemble  
**AI**: Ollama (Llama 3.1 8B, local GPU)  
**Frontend**: Next.js 15 + TypeScript + TailwindCSS  
**Infrastructure**: Docker + Docker Compose (→ Kubernetes planned)

## 🚀 Quick Start

```bash
# 1. Clone and setup
git clone <repo>
cd Cortex_Merge_AI-ML

# 2. Configure environment
cp .env.example .env
# Edit .env with your credentials

# 3. Start services
docker-compose up -d

# 4. Run migrations
docker-compose exec backend alembic upgrade head

# 5. Access
# Backend: http://localhost:8000
# Frontend: http://localhost:3000
# API Docs: http://localhost:8000/docs
```

## 📊 Architecture Highlights

### RAD-AI Framework (IEEE ICSA 2026)
- **E1**: AI Boundary Delineation (deterministic vs. non-deterministic)
- **E2**: Model Registry View (5 models tracked)
- **E3**: Data Pipeline View (5-stage ML pipeline with quality gates)
- **E4**: Responsible AI Concepts (fairness, explainability, safety)
- **E5**: AI Decision Records (5 ADRs documented)
- **E6**: AI Quality Scenarios (5 scenarios: drift, staleness, adversarial, timeout, latency)
- **E7**: AI Debt Register (6 debt items tracked)
- **E8**: Operational AI View (monitoring, retraining, deployment, rollback)

### Security
- **Authentication**: JWT (30min access + 7-day refresh with rotation)
- **Authorization**: RBAC (viewer, trader, admin)
- **Encryption**: Fernet AES-128 for model artifacts
- **Rate Limiting**: 100 req/min (IP), 200 req/min (user), 50 predictions/min (ML)

### Data Architecture
- **TimescaleDB**: Hypertables for time-series (compression, retention policies)
- **Redis**: Caching (TTL: 300s), pub/sub (6 channels), rate limiting
- **Model Storage**: Encrypted artifacts with SHA-256 checksums

## 🔍 Key Design Decisions (ADRs)

1. **TimescaleDB** over InfluxDB - PostgreSQL compatibility, ACID, compression
2. **Ollama** over OpenAI - Zero cost, low latency, privacy, local control
3. **XGBoost + GRU Ensemble** - 87% accuracy, 90ms latency
4. **JWT with Refresh Rotation** - Stateless, secure, revocable
5. **DDD with 6 Bounded Contexts** - Clear ownership, loose coupling, scalability

## 📈 Roadmap

**Q2 2026**: CI/CD, staging deployment, distributed tracing, test coverage (80%)  
**Q3 2026**: Production (K8s), auto-scaling, secondary data source, feedback loop detection  
**Q4 2026**: Microservices, ONNX serving, multi-region, A/B testing, online learning

## 🆘 Troubleshooting

**Database fails**: Check `DATABASE_URL`, verify PostgreSQL running  
**Redis fails**: Check `REDIS_URL`, verify Redis running  
**Ollama not responding**: Check `http://localhost:11434/api/tags`, pull model  
**ML predictions slow**: Check Redis cache hit rate, verify model cached  
**Worker not processing**: Check logs, verify Redis pub/sub, check DB connectivity

## 📚 Documentation Links

- **API Docs**: `backend/docs/api/ML_PREDICTION_API.md`
- **Deployment**: `backend/docs/runbooks/ML_MODEL_DEPLOYMENT.md`
- **ML System**: `backend/docs/architecture/ML_SYSTEM_ARCHITECTURE.md`
- **Feature Engineering**: `backend/docs/guides/ml-feature-engineering.md`
- **Model Training**: `backend/docs/guides/ml-model-training.md`
- **Prediction Usage**: `backend/docs/guides/ml-prediction-usage.md`

## 🎓 References

- **RAD-AI Framework**: arXiv:2603.28735 (Larsen & Moghaddam, IEEE ICSA 2026)
- **C4 Model**: https://c4model.com
- **arc42 Template**: https://arc42.org
- **DDD for AI**: "AI Systems Need Bounded Contexts" (nilus.be, May 2025)

---

**For complete details, see `ARCHITECTURE.md`**
