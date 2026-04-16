# Cortex AI - Comprehensive User FAQ
## Common Questions About AI-Powered Trading Intelligence Systems

*Generated: April 16, 2026*  
*Based on: Common user concerns for ML trading platforms, real-time market data systems, and AI intelligence layers*

---

## 📋 Table of Contents

1. [General System Questions](#general-system-questions)
2. [ML Predictions & Accuracy](#ml-predictions--accuracy)
3. [Real-Time Data & Performance](#real-time-data--performance)
4. [AI Intelligence & Event Analysis](#ai-intelligence--event-analysis)
5. [Security & Privacy](#security--privacy)
6. [Trading & Risk Management](#trading--risk-management)
7. [Technical Setup & Deployment](#technical-setup--deployment)
8. [Costs & Scalability](#costs--scalability)
9. [Model Training & Maintenance](#model-training--maintenance)
10. [Integration & APIs](#integration--apis)
11. [Troubleshooting & Support](#troubleshooting--support)
12. [Regulatory & Compliance](#regulatory--compliance)

---

## General System Questions

### Q1: What exactly does Cortex AI do?
**A:** Cortex AI is an AI-powered trading intelligence platform that:
- Predicts stock price movements using ML (87% accuracy)
- Analyzes market events and news for sentiment and impact
- Combines multiple signals (ML, technical, events) into actionable insights
- Provides real-time market data and charts
- Manages risk with automated kill switches and safety triggers

### Q2: Is this a fully automated trading bot?
**A:** No. Cortex AI is an **intelligence and decision support system**, not an automated trading bot. It:
- Generates trading signals and predictions
- Provides analysis and insights
- Does NOT execute trades automatically
- Requires human oversight and decision-making
- Can be integrated with trading APIs for execution (user's responsibility)

### Q3: How is this different from other trading platforms?
**A:** Key differentiators:
- **Local AI inference** (Ollama) - no cloud API costs, full privacy
- **Multi-signal fusion** - combines ML, technical, and event-driven signals
- **Regime-aware** - adapts to market conditions (bull/bear/sideways/volatile)
- **Fake news detection** - 4-layer verification system
- **Open source** - full transparency, customizable
- **Production-grade** - 90ms latency, 82% cache hit rate, comprehensive monitoring

### Q4: What markets/exchanges does it support?
**A:** Currently:
- **NSE (National Stock Exchange of India)**
- **BSE (Bombay Stock Exchange)**
- Data via **Upstox API**

Roadmap: Zerodha integration (Q3 2026), US markets (Q4 2026)

### Q5: Can I use this for crypto/forex/commodities?
**A:** Not currently. The system is designed for Indian equity markets. However:
- Architecture is extensible
- Data ingestion layer can be adapted
- ML models would need retraining on new asset classes
- Community contributions welcome

---

## ML Predictions & Accuracy

### Q6: What is the prediction accuracy?
**A:** 
- **Overall accuracy: 87%** on test data (directional prediction)
- **P95 latency: 90ms** (target: <250ms)
- **Timeframes**: 1min, 5min, 15min, 1hour, 1day
- **Models**: XGBoost + GRU ensemble with weighted voting

### Q7: How far into the future can it predict?
**A:** 
- **Short-term**: 1-60 minutes (highest accuracy)
- **Medium-term**: 1-24 hours (good accuracy)
- **Long-term**: 1-7 days (lower accuracy, more uncertainty)

Predictions degrade with time horizon. Use shorter timeframes for higher confidence.

### Q8: What happens when the model is wrong?
**A:**
- **Confidence scores** provided with each prediction
- **Safety triggers** monitor prediction accuracy in real-time
- **Automatic demotion** if accuracy drops below threshold
- **Kill switches** can halt signal generation
- **Audit logs** track all predictions for post-mortem analysis

### Q9: How often are models retrained?
**A:** Currently:
- **Manual retraining** via scripts (weekly recommended)
- **Drift detection** runs continuously
- **Automatic alerts** when drift exceeds thresholds
- Roadmap: Automated retraining pipeline (Q3 2026)

### Q10: Can I train models on my own data?
**A:** Yes! 
- Training scripts in `backend/scripts/train_final_models.py`
- Feature engineering pipeline fully documented
- Hyperparameter tuning with Optuna
- Custom data sources can be integrated
- See `backend/docs/guides/ml-model-training.md`

### Q11: What features does the ML model use?
**A:** 150+ features across categories:
- **OHLCV**: Price, volume, returns
- **Technical indicators**: RSI, MACD, Bollinger Bands, ATR, ADX, VWAP
- **Volume patterns**: Volume spikes, accumulation/distribution
- **Sentiment**: News sentiment, social media mentions
- **Market regime**: Bull/bear/sideways/volatile classification
- **Multi-timeframe**: Features from 5 timeframes

Full list: `backend/docs/guides/ml-feature-engineering.md`

### Q12: How do you prevent overfitting?
**A:**
- **Walk-forward validation** (not simple train/test split)
- **Cross-validation** with time-series splits
- **Regularization** (L1/L2 for XGBoost, dropout for GRU)
- **Early stopping** based on validation loss
- **Ensemble methods** reduce variance
- **Out-of-sample testing** on unseen data

---

## Real-Time Data & Performance

### Q13: How real-time is the data?
**A:**
- **Tick data**: <100ms latency from exchange
- **WebSocket streaming**: Live price updates
- **OHLCV candles**: 1-minute granularity
- **Event ingestion**: RSS feeds polled every 5 minutes
- **Predictions**: Generated on-demand (<90ms)

### Q14: What if the data feed goes down?
**A:**
- **Automatic reconnection** with exponential backoff
- **Circuit breakers** prevent cascading failures
- **Fallback to cached data** (5-minute TTL)
- **Health checks** monitor data freshness
- **Alerts** sent when data is stale

### Q15: How much historical data is stored?
**A:**
- **Tick data**: 7 days (then aggregated to 1-min candles)
- **1-min candles**: 90 days
- **5-min+ candles**: 5 years
- **Events**: Indefinite (with archival)
- **Predictions**: 30 days (audit trail)

Configurable via TimescaleDB retention policies.

### Q16: Can I backtest strategies?
**A:** Not built-in currently. However:
- Historical data available via API
- Predictions logged with timestamps
- Can export data for external backtesting tools
- Roadmap: Built-in backtesting framework (Q4 2026)

### Q17: What is the system throughput?
**A:**
- **API requests**: 54 req/s sustained (target: >50)
- **WebSocket connections**: 100+ concurrent
- **ML predictions**: 50/min per user (rate limited)
- **Event processing**: 1000+ events/hour
- **Database writes**: 10K+ inserts/sec (TimescaleDB)

### Q18: Does it work during market hours only?
**A:** 
- **Market hours**: Full functionality (9:15 AM - 3:30 PM IST)
- **After hours**: Historical data, analysis, model training
- **Pre-market**: News analysis, event classification
- **Weekends**: System maintenance, model retraining

---

## AI Intelligence & Event Analysis

### Q19: How does the AI analyze news?
**A:** 4-stage pipeline:
1. **Ingestion**: RSS feeds from 20+ sources (ET, Moneycontrol, etc.)
2. **Classification**: Ollama (Llama 3.1 8B) categorizes events
3. **Sentiment**: FinBERT analyzes sentiment (positive/negative/neutral)
4. **Impact**: Credibility scoring + cross-reference validation

### Q20: What is fake news detection?
**A:** 4-layer system:
1. **Layer 1**: Source credibility check (reputation score)
2. **Layer 2**: Cross-reference with other sources
3. **Layer 3**: Temporal consistency (contradictions over time)
4. **Layer 4**: LLM reasoning (Ollama analyzes content)

Flags news as "verified", "unverified", or "disputed".

### Q21: Does it use ChatGPT/OpenAI?
**A:** No. Uses **Ollama (Llama 3.1 8B)** running locally:
- **Zero API costs**
- **Full privacy** (no data sent to cloud)
- **Low latency** (<500ms for classification)
- **Offline capable**
- Optional OpenAI fallback for complex queries

### Q22: Can I use a different LLM?
**A:** Yes! Architecture supports:
- **Ollama models**: Llama, Mistral, Gemma, etc.
- **OpenAI**: GPT-4, GPT-3.5
- **Anthropic**: Claude (via API)
- **Custom models**: Integrate via LLM client interface

See `backend/app/ai/intelligence/llm_client.py`

### Q23: How accurate is sentiment analysis?
**A:**
- **FinBERT**: 85-90% accuracy on financial text
- **Calibrated confidence scores** via Platt scaling
- **Context-aware**: Considers market regime
- **Multi-source validation**: Cross-references multiple sources

### Q24: What languages are supported?
**A:**
- **Primary**: English
- **Translation**: Hindi → English (IndicTrans2)
- Roadmap: Native Hindi support (Q3 2026)

---

## Security & Privacy

### Q25: Is my data secure?
**A:** Yes. Security measures:
- **JWT authentication** with refresh token rotation
- **RBAC** (Role-Based Access Control)
- **Encryption at rest**: Fernet AES-128 for model artifacts
- **Encryption in transit**: TLS 1.3 in production
- **Rate limiting**: Prevents abuse
- **Audit logging**: Full activity trail

### Q26: Do you store my trading credentials?
**A:** No. 
- **Upstox access tokens** stored encrypted in environment variables
- **No passwords** stored (JWT-based auth)
- **No trading execution** (read-only market data)
- Users manage their own broker credentials

### Q27: Can I self-host this?
**A:** Yes! Fully self-hostable:
- **Docker Compose** for local deployment
- **Kubernetes** manifests (roadmap Q3 2026)
- **No cloud dependencies** (except optional OpenAI)
- **Full source code** available
- **MIT License** (check actual license)

### Q28: What data is sent to third parties?
**A:**
- **Upstox API**: Market data requests (required)
- **Ollama**: None (runs locally)
- **OpenAI**: Only if enabled (optional)
- **Analytics**: None (no telemetry)

### Q29: How do you handle API keys?
**A:**
- **Environment variables** (`.env` file)
- **Never logged** or exposed in responses
- **Encrypted in database** (if stored)
- **Rotation supported** via admin API
- **Separate keys** for dev/staging/prod

---

## Trading & Risk Management

### Q30: Does this guarantee profits?
**A:** **NO.** Important disclaimers:
- **No guarantees** of profit
- **Past performance ≠ future results**
- **87% accuracy ≠ 87% profitable trades**
- **Market risk** always present
- **Use at your own risk**
- **Not financial advice**

### Q31: What are kill switches?
**A:** Automatic safety mechanisms:
- **Global kill switch**: Halts all signal generation
- **Symbol-specific**: Stops signals for specific stocks
- **Strategy-specific**: Disables specific strategies
- **Triggered by**: Loss limits, volatility spikes, drift detection
- **Manual override**: Admin can activate/deactivate

### Q32: How does risk management work?
**A:** Multi-layer approach:
1. **Position sizing**: Confidence-based allocation
2. **Stop losses**: Automatic recommendations
3. **Diversification**: Multi-symbol, multi-strategy
4. **Regime awareness**: Reduces exposure in volatile markets
5. **Safety triggers**: Monitors anomalies in real-time

### Q33: Can I set custom risk parameters?
**A:** Yes:
- **Loss limits**: Max loss per trade/day/week
- **Volatility thresholds**: Pause trading in high volatility
- **Confidence thresholds**: Minimum confidence for signals
- **Position limits**: Max positions per symbol/sector
- Configurable via admin API or `.env` file

### Q34: What happens during flash crashes?
**A:**
- **Volatility spike detection** triggers safety pause
- **Circuit breakers** halt signal generation
- **Kill switches** can be manually activated
- **Historical data** preserved for analysis
- **Automatic recovery** when volatility normalizes

---

## Technical Setup & Deployment

### Q35: What are the system requirements?
**A:**
**Minimum**:
- 4 CPU cores
- 8 GB RAM
- 50 GB storage
- Ubuntu 22.04 / Debian 11

**Recommended**:
- 8 CPU cores
- 16 GB RAM
- 200 GB SSD
- NVIDIA GPU (for Ollama, optional)

### Q36: How long does setup take?
**A:**
- **Docker Compose**: 15-30 minutes
- **Manual setup**: 1-2 hours
- **Model training**: 2-4 hours (first time)
- **Data backfill**: 1-2 hours (historical data)

### Q37: Do I need a GPU?
**A:** Optional but recommended:
- **Ollama**: 2-3x faster with GPU (NVIDIA)
- **ML training**: 5-10x faster with GPU
- **ML inference**: CPU sufficient (ONNX optimized)
- **Minimum GPU**: NVIDIA GTX 1060 (6GB VRAM)

### Q38: Can I run this on Windows?
**A:** Yes, via:
- **Docker Desktop** (recommended)
- **WSL2** (Windows Subsystem for Linux)
- **Native Windows**: Not officially supported (use Docker)

### Q39: What about macOS?
**A:** Yes:
- **Docker Desktop** (Intel or Apple Silicon)
- **Ollama**: Native macOS support
- **Performance**: Comparable to Linux

### Q40: How do I update the system?
**A:**
```bash
# Pull latest code
git pull origin main

# Rebuild containers
docker-compose build

# Run migrations
docker-compose exec backend alembic upgrade head

# Restart services
docker-compose restart
```

---

## Costs & Scalability

### Q41: What does it cost to run?
**A:**
**Self-hosted**:
- **Infrastructure**: $50-200/month (VPS/cloud)
- **Upstox API**: Free (with trading account)
- **Ollama**: Free (local inference)
- **Total**: ~$50-200/month

**Cloud-hosted** (if using OpenAI):
- Add $50-500/month for OpenAI API

### Q42: How many users can it support?
**A:**
- **Single instance**: 10-50 concurrent users
- **With Redis cluster**: 100-500 users
- **With Kubernetes**: 1000+ users (horizontal scaling)
- **Rate limits**: 200 req/min per user

### Q43: Can it scale horizontally?
**A:** Yes:
- **Stateless API**: Multiple replicas behind load balancer
- **Redis**: Cluster mode for caching/pub-sub
- **TimescaleDB**: Read replicas for queries
- **Worker**: Multiple instances for parallel processing
- Kubernetes manifests (roadmap Q3 2026)

### Q44: What about database size?
**A:**
- **1 month**: ~5 GB (100 symbols, tick data)
- **1 year**: ~50 GB (with compression)
- **TimescaleDB compression**: 10:1 ratio
- **Retention policies**: Auto-delete old data

---

## Model Training & Maintenance

### Q45: How do I retrain models?
**A:**
```bash
# Full training pipeline
docker-compose exec backend python scripts/train_final_models.py

# Specific model
docker-compose exec backend python scripts/train_final_models.py --model xgboost

# With custom data
docker-compose exec backend python scripts/train_final_models.py --symbols RELIANCE,TCS,INFY
```

See `backend/docs/guides/ml-model-training.md`

### Q46: How long does training take?
**A:**
- **XGBoost**: 30-60 minutes (100 symbols)
- **GRU**: 1-2 hours (GPU), 4-6 hours (CPU)
- **Ensemble**: 2-4 hours total
- **Hyperparameter tuning**: +2-4 hours (optional)

### Q47: Can I use pre-trained models?
**A:** Yes:
- **Production models** included in `models/production/`
- **Trained on**: 100 NSE symbols, 2 years data
- **Last updated**: Check `models/production/metadata.json`
- **Retrain recommended**: Every 1-2 weeks

### Q48: What if model performance degrades?
**A:**
- **Drift detection** monitors performance continuously
- **Automatic alerts** when accuracy drops
- **Shadow mode**: New model tested before promotion
- **Rollback**: Revert to previous model version
- **Retraining**: Triggered manually or automatically (roadmap)

### Q49: How do I add new features?
**A:**
1. Edit `backend/app/ml/features/feature_pipeline.py`
2. Add feature computation logic
3. Update feature store schema
4. Retrain models with new features
5. Validate performance improvement

See `backend/docs/guides/ml-feature-engineering.md`

---

## Integration & APIs

### Q50: Is there a REST API?
**A:** Yes:
- **OpenAPI/Swagger**: `http://localhost:8000/docs`
- **50+ endpoints**: Market data, predictions, signals, events
- **Authentication**: JWT bearer token
- **Rate limiting**: 200 req/min per user
- **Versioned**: `/api/v1/...`

### Q51: Is there a WebSocket API?
**A:** Yes:
- **Real-time ticks**: `/api/v1/upstox/ws/ticks`
- **Signal updates**: `/api/v1/cai/stream`
- **Authentication**: JWT in query param or header
- **Reconnection**: Automatic with exponential backoff

### Q52: Can I integrate with my own trading bot?
**A:** Yes:
- **Fetch signals**: `GET /api/v1/fusion/signals`
- **Get predictions**: `POST /api/v1/ml/predict`
- **Subscribe to events**: WebSocket `/api/v1/cai/stream`
- **Execute trades**: Use broker API (Upstox, Zerodha, etc.)

Example: `backend/examples/ml_signal_integration.py`

### Q53: Is there a Python SDK?
**A:** Not yet. Roadmap:
- **Q2 2026**: Official Python SDK
- **Q3 2026**: JavaScript/TypeScript SDK
- Currently: Use `requests` or `httpx` with API docs

### Q54: Can I export data?
**A:** Yes:
- **CSV export**: Via API or database query
- **JSON export**: All API responses in JSON
- **Database access**: Direct PostgreSQL connection
- **Backup**: `pg_dump` for full database export

---

## Troubleshooting & Support

### Q55: Where are the logs?
**A:**
```bash
# API logs
docker-compose logs -f backend

# Worker logs
docker-compose logs -f worker

# Frontend logs
docker-compose logs -f frontend

# All logs
docker-compose logs -f
```

### Q56: How do I debug issues?
**A:**
1. Check logs (see Q55)
2. Check health endpoint: `GET /health`
3. Check Prometheus metrics: `http://localhost:9090`
4. Check Redis: `redis-cli ping`
5. Check database: `psql -U cortex -d cortex_db`

See `KNOWN_ISSUES_AND_REMEDIATION.md`

### Q57: What if predictions are slow?
**A:**
- Check Redis cache hit rate (target: >80%)
- Verify ONNX model loaded: Check logs for "ML prediction engine pre-loaded"
- Check CPU usage: `docker stats`
- Increase Redis memory: Edit `docker-compose.yml`
- Scale horizontally: Add more API replicas

### Q58: What if the worker crashes?
**A:**
- Check worker logs: `docker-compose logs worker`
- Verify Redis pub/sub: `redis-cli PUBSUB CHANNELS`
- Check database connectivity: `docker-compose exec backend python -c "from app.core.database import engine; print(engine)"`
- Restart worker: `docker-compose restart worker`

### Q59: How do I report bugs?
**A:**
- **GitHub Issues**: (if open source)
- **Email**: support@cortex.ai (if commercial)
- **Include**: Logs, steps to reproduce, environment details
- **Check first**: `KNOWN_ISSUES_AND_REMEDIATION.md`

### Q60: Is there community support?
**A:**
- **Documentation**: `backend/docs/`, `ARCHITECTURE.md`
- **Examples**: `backend/examples/`
- **Discord/Slack**: (if available)
- **Stack Overflow**: Tag `cortex-ai`

---

## Regulatory & Compliance

### Q61: Is this SEBI compliant?
**A:** 
- **Not a SEBI-registered advisor** (no investment advice)
- **Decision support tool** only
- **User responsibility** for trading decisions
- **Consult legal counsel** for compliance requirements

### Q62: Can I use this for client advisory?
**A:** 
- **Not recommended** without proper licensing
- **SEBI registration** required for advisory services
- **Liability**: User assumes all risk
- **Consult legal counsel** before commercial use

### Q63: What about data privacy laws?
**A:**
- **GDPR**: Self-hosted = full control
- **Data residency**: All data stored locally
- **No third-party sharing** (except Upstox API)
- **User consent**: Required for data collection

### Q64: Are there usage restrictions?
**A:**
- **Personal use**: Unrestricted
- **Commercial use**: Check license terms
- **Redistribution**: Check license terms
- **Modifications**: Allowed (open source)

### Q65: What disclaimers should I show users?
**A:** Minimum:
- "Not financial advice"
- "Past performance ≠ future results"
- "Trading involves risk of loss"
- "Consult a licensed advisor"
- "Use at your own risk"

---

## Advanced Questions

### Q66: How does regime detection work?
**A:**
- **4 regimes**: Bull, Bear, Sideways, Volatile
- **Indicators**: ADX, ATR, Bollinger Bands, RSI
- **Hysteresis**: Prevents regime flipping
- **Historical tracking**: Regime changes logged
- **Strategy adaptation**: Different strategies per regime

### Q67: What is signal fusion?
**A:** Combines multiple signal sources:
1. **ML predictions**: XGBoost + GRU ensemble
2. **Technical indicators**: RSI, MACD, Bollinger Bands
3. **Event-driven**: News sentiment, earnings, announcements
4. **Weighted voting**: Confidence-based aggregation
5. **Regime filtering**: Adapts to market conditions

### Q68: How does drift detection work?
**A:**
- **Statistical tests**: Kolmogorov-Smirnov, PSI, JS divergence
- **Feature drift**: Input distribution changes
- **Prediction drift**: Output distribution changes
- **Performance drift**: Accuracy degradation
- **Automatic alerts**: When drift exceeds thresholds

### Q69: What is the model registry?
**A:**
- **Centralized tracking**: All models, versions, metadata
- **Lifecycle management**: Shadow → Paper → Live
- **Performance metrics**: Accuracy, latency, drift
- **Rollback support**: Revert to previous versions
- **Audit trail**: All promotions/demotions logged

### Q70: Can I add custom strategies?
**A:** Yes:
1. Create strategy class in `backend/app/ai/strategy/`
2. Implement `execute()` method
3. Register with `StrategyOrchestrator`
4. Configure regime compatibility
5. Test in shadow mode before live

See `backend/examples/strategy_orchestrator_usage_example.py`

---

## Performance & Optimization

### Q71: How do you achieve 90ms latency?
**A:**
- **ONNX quantization**: INT8 models (4x smaller, 2x faster)
- **Redis caching**: 82% hit rate, 5-minute TTL
- **Connection pooling**: Reuse database connections
- **Async I/O**: Non-blocking operations
- **Batch processing**: Group predictions when possible

### Q72: What is the cache strategy?
**A:**
- **L1 (in-memory)**: Prediction engine, model artifacts
- **L2 (Redis)**: Predictions, market data, indicators
- **TTL**: 5 minutes (configurable)
- **Invalidation**: On new data, model updates
- **Hit rate**: 82% (target: >80%)

### Q73: How do you handle high load?
**A:**
- **Rate limiting**: 200 req/min per user
- **Circuit breakers**: Prevent cascading failures
- **Graceful degradation**: Serve cached data if backend slow
- **Horizontal scaling**: Multiple API replicas
- **Database read replicas**: Offload queries

### Q74: What monitoring is available?
**A:**
- **Prometheus metrics**: Request rate, latency, errors
- **Grafana dashboards**: Real-time visualization
- **Health checks**: `/health` endpoint
- **Structured logging**: JSON logs for analysis
- **Alerting**: (roadmap Q2 2026)

### Q75: How do you ensure high availability?
**A:**
- **Stateless API**: No single point of failure
- **Database replication**: Primary + read replicas
- **Redis cluster**: High availability mode
- **Health checks**: Kubernetes liveness/readiness probes
- **Graceful shutdown**: Drain connections before restart

---

## Future Roadmap

### Q76: What features are planned?
**A:**
**Q2 2026**:
- CI/CD pipeline
- Staging environment
- Distributed tracing
- Test coverage to 80%

**Q3 2026**:
- Kubernetes deployment
- Auto-scaling
- Zerodha integration
- Automated retraining

**Q4 2026**:
- Microservices architecture
- Multi-region deployment
- A/B testing framework
- Online learning

### Q77: Will you support other brokers?
**A:** Yes:
- **Zerodha**: Q3 2026
- **Angel One**: Q4 2026
- **ICICI Direct**: 2027
- **US brokers**: 2027 (Alpaca, Interactive Brokers)

### Q78: Will there be a mobile app?
**A:** Roadmap:
- **Q3 2026**: Progressive Web App (PWA)
- **Q4 2026**: React Native app (iOS/Android)
- **Features**: Real-time signals, charts, alerts

### Q79: Will you add options/futures?
**A:** Roadmap:
- **Q4 2026**: Futures support
- **2027**: Options support
- **Challenges**: Different risk models, Greeks calculation

### Q80: Can I contribute to the project?
**A:** Yes! (if open source)
- **GitHub**: Fork, branch, PR
- **Issues**: Bug reports, feature requests
- **Documentation**: Improvements welcome
- **Code**: Follow style guide, add tests
- **Community**: Discord/Slack for discussions

---

## Conclusion

This FAQ covers the most common questions about AI-powered trading intelligence systems like Cortex AI. For more details:

- **Architecture**: See `ARCHITECTURE.md`
- **API Docs**: `http://localhost:8000/docs`
- **Guides**: `backend/docs/guides/`
- **Troubleshooting**: `KNOWN_ISSUES_AND_REMEDIATION.md`

**Disclaimer**: This system is for educational and informational purposes only. Not financial advice. Trading involves risk of loss. Use at your own risk.

---

*Last updated: April 16, 2026*
