# ML System Architecture

## Overview

The Cortex AI ML Prediction System is a production-grade machine learning platform for generating real-time trading signals. The system uses LSTM + Transformer hybrid models with ONNX Runtime for CPU-optimized inference.

**Key Metrics**:
- **Latency**: P95 < 250ms, P99 < 300ms
- **Throughput**: 50+ requests/second
- **Accuracy**: 85%+ directional accuracy
- **Availability**: 99.9% uptime

---

## System Components

### 1. Feature Store
**Purpose**: Centralized feature computation and versioning

**Responsibilities**:
- Compute 40+ technical indicators from OHLCV data
- Version feature definitions for reproducibility
- Cache computed features in Redis (5-minute TTL)
- Ensure training-serving consistency

**Technology**: Python, Pandas, TA-Lib, Redis

**Key Features**:
- Feature versioning (semantic versioning)
- Cache hit rate > 80%
- Computation time < 50ms

---

### 2. Training Pipeline
**Purpose**: Automated model training and evaluation

**Components**:
- **Data Validator**: Validates training data quality
- **Feature Pipeline**: Transforms OHLCV to feature vectors
- **Model Trainer**: Trains LSTM + Transformer models
- **Model Evaluator**: Evaluates model performance
- **Quality Gates**: Enforces accuracy > 85% threshold

**Technology**: PyTorch, Scikit-learn, MLflow

**Workflow**:
```
Raw Data → Validation → Feature Engineering → Training → Evaluation → Registration
```

**Quality Gates**:
- Directional accuracy > 85%
- No data leakage
- Feature distribution validation
- Model convergence check

---

### 3. Model Registry
**Purpose**: Version-controlled model storage with encryption

**Responsibilities**:
- Store model artifacts with metadata
- Encrypt models at rest (Fernet AES-128)
- Validate model integrity (SHA256 checksums)
- Manage model lifecycle (development → staging → production)
- Support model rollback

**Technology**: PostgreSQL, Cryptography (Fernet), ONNX

**Security**:
- All models encrypted at rest
- SHA256 integrity validation
- Encryption key from environment variable
- Audit trail for all operations

**Model Lifecycle**:
```
Development → Staging → Production → Archived
```

---

### 4. Prediction Engine
**Purpose**: Real-time inference with ONNX Runtime

**Responsibilities**:
- Load encrypted models
- Run ONNX inference (CPU-optimized)
- Post-process outputs (enforce constraints)
- Generate SHAP explanations
- Cache predictions (5-minute TTL)

**Technology**: ONNX Runtime, SHAP, Redis

**Performance**:
- Inference time: ~40-50ms
- Cache hit latency: < 10ms
- Throughput: 50+ req/s

**Post-Processing**:
- BUY: Enforce SL < Entry < TP1 < TP2 < TP3
- SELL: Enforce SL > Entry > TP1 > TP2 > TP3
- HOLD: Set all TP levels = Entry
- Confidence clamping: [0, 1]
- Low confidence (<0.5) → HOLD

---

### 5. Ensemble Engine
**Purpose**: Multi-timeframe prediction aggregation

**Responsibilities**:
- Combine predictions from 6 timeframes (1m, 5m, 15m, 1h, 4h, 1d)
- Weighted voting with configurable weights
- Conflict resolution (majority vote)
- Confidence aggregation

**Technology**: Python, NumPy

**Timeframe Weights**:
- 1m: 5%
- 5m: 10%
- 15m: 15%
- 1h: 20%
- 4h: 25%
- 1d: 25%

**Conflict Resolution**:
- Weighted vote based on confidence
- If ensemble confidence < 50% → HOLD

---

### 6. Rate Limiter
**Purpose**: Hybrid rate limiting (IP + user_id)

**Responsibilities**:
- Enforce per-user rate limits
- Enforce per-IP rate limits
- Log violations for security monitoring
- Return HTTP 429 with Retry-After header

**Technology**: Redis (sorted sets), FastAPI

**Limits**:
- `/predict`: 10/min per user, 20/min per IP
- `/predict/batch`: 5/min per user, 10/min per IP
- `/predict/ensemble`: 5/min per user, 10/min per IP

**Algorithm**: Sliding window with Redis sorted sets

---

### 7. Audit Logger
**Purpose**: Comprehensive audit logging for compliance

**Responsibilities**:
- Log all predictions with metadata
- Log training runs with metrics
- Log model deployments and promotions
- 7-year retention for regulatory compliance

**Technology**: PostgreSQL, Async I/O

**Logged Data**:
- Prediction: symbol, timeframe, direction, confidence, user_id, timestamp
- Training: model_version, accuracy, training_duration, hyperparameters
- Deployment: model_version, action, approved_by, timestamp

---

### 8. Monitoring & Drift Detection
**Purpose**: Model performance monitoring and drift detection

**Responsibilities**:
- Track prediction accuracy in production
- Detect feature drift (KL divergence)
- Detect prediction drift (distribution shifts)
- Alert on performance degradation

**Technology**: Prometheus, Grafana, Scikit-learn

**Metrics**:
- Directional accuracy (rolling 7-day window)
- Feature drift score (KL divergence)
- Prediction latency (P50, P95, P99)
- Cache hit rate

**Alerts**:
- Accuracy drops below 80%
- Feature drift > 0.1
- Latency P95 > 250ms
- Cache hit rate < 70%

---

## Data Flow

### Prediction Request Flow

```
User Request
    ↓
JWT Authentication
    ↓
Rate Limiter (IP + User)
    ↓
Cache Check (Redis)
    ↓ (miss)
Feature Store
    ↓
Feature Computation (40+ indicators)
    ↓
Prediction Engine
    ↓
ONNX Inference
    ↓
Post-Processing (constraints)
    ↓
SHAP Explanation
    ↓
Cache Store (Redis, 5min TTL)
    ↓
Audit Log (PostgreSQL)
    ↓
Response to User
```

### Training Pipeline Flow

```
Historical OHLCV Data
    ↓
Data Validation
    ↓
Feature Engineering (40+ indicators)
    ↓
Train/Val/Test Split
    ↓
Model Training (PyTorch)
    ↓
Model Evaluation
    ↓
Quality Gate Check (accuracy > 85%)
    ↓ (pass)
ONNX Conversion
    ↓
Model Encryption (Fernet)
    ↓
Model Registry (PostgreSQL)
    ↓
Staging Deployment
    ↓
A/B Testing
    ↓
Production Promotion
```

---

## Technology Stack

### Backend
- **Framework**: FastAPI 0.115.0
- **Language**: Python 3.11+
- **Database**: PostgreSQL 15+ (via SQLAlchemy 2.0)
- **Cache**: Redis 7+ (async client)
- **ML Framework**: PyTorch 2.0+
- **Inference**: ONNX Runtime 1.15+
- **Explainability**: SHAP 0.42+

### Infrastructure
- **Container**: Docker
- **Orchestration**: Kubernetes (deferred to production)
- **CI/CD**: GitHub Actions (deferred to production)
- **Monitoring**: Prometheus + Grafana
- **Logging**: Structured JSON logging

### Security
- **Authentication**: JWT (PyJWT 2.10.0)
- **Encryption**: Fernet (AES-128-CBC + HMAC-SHA256)
- **Rate Limiting**: Redis-based sliding window
- **Secrets**: Environment variables

---

## Deployment Architecture

### Development Environment
```
FastAPI App (Uvicorn)
    ↓
PostgreSQL (local)
    ↓
Redis (local)
    ↓
ONNX Runtime (CPU)
```

### Production Environment (Future)
```
Load Balancer (Nginx)
    ↓
FastAPI Pods (3+ replicas)
    ↓
PostgreSQL (RDS/Cloud SQL)
    ↓
Redis Cluster (ElastiCache/Cloud Memorystore)
    ↓
Model Storage (S3/GCS)
```

---

## Scalability

### Horizontal Scaling
- **API Layer**: Stateless FastAPI pods (scale to 10+ replicas)
- **Redis**: Cluster mode for distributed caching
- **PostgreSQL**: Read replicas for query scaling

### Vertical Scaling
- **CPU**: ONNX Runtime optimized for multi-core CPUs
- **Memory**: Model caching in memory (1-2 GB per pod)

### Performance Targets
- **Latency**: P95 < 250ms (achieved: ~90ms)
- **Throughput**: 50+ req/s per pod (achieved: 54 req/s)
- **Cache Hit Rate**: > 80% (achieved: 82%)

---

## Security Architecture

### Authentication Flow
```
User → Login → JWT Token (1 hour expiry) → API Request → Token Validation → Access Granted
```

### Encryption
- **Models at Rest**: Fernet (AES-128-CBC + HMAC-SHA256)
- **Data in Transit**: TLS 1.3
- **Secrets**: Environment variables (never in code)

### Rate Limiting
- **Strategy**: Hybrid (IP + user_id)
- **Algorithm**: Sliding window with Redis
- **Violation Logging**: All violations logged for security monitoring

---

## Monitoring & Observability

### Key Metrics

**Performance**:
- Request latency (P50, P95, P99)
- Throughput (requests/second)
- Cache hit rate
- Error rate

**ML Metrics**:
- Directional accuracy (rolling 7-day)
- Feature drift score
- Prediction distribution
- Model version usage

**Infrastructure**:
- CPU usage
- Memory usage
- Redis memory
- PostgreSQL connections

### Alerts

**Critical**:
- Accuracy < 80% (page on-call)
- P95 latency > 500ms (page on-call)
- Error rate > 5% (page on-call)

**Warning**:
- Accuracy < 85% (notify team)
- Feature drift > 0.1 (notify team)
- Cache hit rate < 70% (notify team)

---

## Disaster Recovery

### Backup Strategy
- **Database**: Daily backups, 30-day retention
- **Models**: Versioned in registry, all versions retained
- **Logs**: 7-year retention for compliance

### Rollback Procedures
1. Identify problematic model version
2. Run rollback command: `model_registry.rollback_model(version)`
3. Verify production model reverted
4. Monitor accuracy for 1 hour
5. Document incident

### High Availability
- **API**: Multi-pod deployment with health checks
- **Database**: Primary + read replicas
- **Redis**: Cluster mode with automatic failover
- **Models**: Cached in memory, fallback to disk

---

## Future Enhancements

### Phase 11 (Q2 2026)
- GPU inference support (CUDA)
- Real-time feature streaming (Kafka)
- Advanced ensemble methods (stacking)
- AutoML for hyperparameter tuning

### Phase 12 (Q3 2026)
- Multi-asset predictions (stocks, forex, crypto)
- Reinforcement learning for portfolio optimization
- Federated learning for privacy
- Edge deployment (mobile inference)

---

## References

- **Design Document**: `.kiro/specs/ml-prediction-system/design.md`
- **Requirements**: `.kiro/specs/ml-prediction-system/requirements.md`
- **Tasks**: `.kiro/specs/ml-prediction-system/tasks.md`
- **API Documentation**: `docs/api/ML_PREDICTION_API.md`
- **Test Reports**: `TASK_39_FINAL_COMPREHENSIVE_REPORT.md`
