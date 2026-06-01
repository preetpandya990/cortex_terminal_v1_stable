# ML Model Lifespan Management
**Cortex AI Trading Platform**  
**Date:** 2026-04-20  
**Status:** ✅ Production Ready

---

## Overview

Production ML models are loaded during application startup via FastAPI's lifespan context manager. This ensures models are ready before the first request and properly cleaned up on shutdown.

---

## Architecture

### Lifespan Flow

```
Application Startup
    ↓
Initialize Redis & Database
    ↓
Load Production Models from Registry
    ├─ Create RegistryModelLoader
    ├─ Load XGBoost (Treelite .so)
    ├─ Load GRU (ONNX)
    ├─ Create EnsemblePredictor
    └─ Run Health Check
    ↓
Store in app.state
    ├─ app.state.ml_ensemble (metadata)
    └─ app.state.ml_predictor (inference engine)
    ↓
Application Ready (accept requests)
    ↓
... handle requests ...
    ↓
Application Shutdown
    ↓
Cleanup ML Models
    ├─ Set predictor to None
    └─ Set ensemble to None
    ↓
Close Database & Redis
    ↓
Shutdown Complete
```

### Key Components

**1. RegistryModelLoader** (`app/ml/inference/registry_loader.py`)
- Loads models from database registry
- Verifies checksums
- Validates model health
- Thread-safe with asyncio locks

**2. EnsemblePredictor** (`app/ml/inference/ensemble_predictor.py`)
- Wraps XGBoost (Treelite) + GRU (ONNX)
- Handles ensemble weighting
- Provides unified prediction interface

**3. FastAPI Dependencies** (`app/api/deps.py`)
- `get_ml_predictor()` - Access predictor from endpoints
- `get_ml_ensemble()` - Access ensemble metadata
- Returns 503 if models not loaded

---

## Implementation Details

### Startup (app/main.py)

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # ... other initialization ...
    
    # Load production ML models from registry
    try:
        async with AsyncSessionLocal() as session:
            loader = RegistryModelLoader(
                session=session,
                num_threads=4,
                use_gpu=False,
            )
            
            ensemble = await loader.load_production_ensemble()
            predictor = EnsemblePredictor.from_loaded_ensemble(ensemble)
            
            app.state.ml_ensemble = ensemble
            app.state.ml_predictor = predictor
            
            # Health check
            health = await loader.health_check()
            if health["status"] != "healthy":
                raise RuntimeError("Model health check failed")
                
    except Exception as e:
        app.state.ml_ensemble = None
        app.state.ml_predictor = None
        
        # Fail fast in production
        if settings.is_production:
            raise RuntimeError(f"Critical: Failed to load models: {e}")
    
    yield  # Application runs
    
    # Cleanup on shutdown
    app.state.ml_predictor = None
    app.state.ml_ensemble = None
```

### Endpoint Usage

```python
from app.api.deps import MLPredictor
from fastapi import APIRouter

router = APIRouter()

@router.post("/predict")
async def predict(
    predictor: MLPredictor,
    features: PredictionRequest,
):
    """Make prediction using loaded models."""
    result = await predictor.predict(features.data)
    return result
```

---

## Error Handling

### Startup Failures

**Production Environment:**
- Models MUST load successfully
- Application fails to start if models unavailable
- Prevents serving requests without ML capability

**Non-Production Environment:**
- Models are optional
- Application starts even if models fail to load
- Endpoints return 503 when models unavailable

### Runtime Errors

**Model Not Available (503):**
```json
{
  "detail": "ML models not available. Service is starting up or models failed to load."
}
```

**Health Check Failure:**
- Logged as ERROR
- Application startup aborted
- Prevents serving with unhealthy models

---

## Performance Characteristics

### Startup Time
- **Model Loading:** ~2-5 seconds
  - XGBoost (Treelite): ~500ms
  - GRU (ONNX): ~1-2s
  - Health Check: ~500ms
- **Total Startup:** ~10-15 seconds (including DB, Redis, etc.)

### Memory Usage
- **XGBoost (.so):** ~4-5 MB
- **GRU (ONNX):** ~8-10 MB
- **ONNX Runtime:** ~50-100 MB
- **Total ML Memory:** ~60-120 MB

### Inference Latency
- **XGBoost:** 0.29ms/sample
- **GRU:** 2.61ms/sample
- **End-to-End:** <10ms (well under 250ms target)

---

## Health Checks

### Startup Health Check

Validates:
1. ✅ Models exist in registry
2. ✅ Model files accessible
3. ✅ Checksums match
4. ✅ Models can load
5. ✅ Synthetic inference works
6. ✅ Output shapes correct

### Runtime Health Check

Available via `loader.health_check()`:

```python
{
  "status": "healthy",
  "models": {
    "xgboost": {
      "version": "1.0.0_xgboost",
      "status": "production",
      "accuracy": 0.6581
    },
    "gru": {
      "version": "1.0.0_gru",
      "status": "production",
      "accuracy": 0.5317
    }
  },
  "ensemble_weights": {
    "xgboost": 0.75,
    "gru": 0.25
  }
}
```

---

## Monitoring & Observability

### Startup Logs

```
INFO  Cortex AI starting [env=production]
INFO  Prometheus metrics initialized
INFO  Redis initialized
INFO  Loading production ensemble from registry...
INFO  ✓ Production ML models loaded: XGBoost v1.0.0_xgboost + GRU v1.0.0_gru
INFO  ✓ ML model health check passed
INFO  All services initialized — ready
```

### Shutdown Logs

```
INFO  Cortex AI shutting down
INFO  Cleaning up ML models...
INFO  Shutdown complete
```

### Error Logs

```
ERROR Failed to load production ML models: Model not found in registry
ERROR Model health check failed: Checksum mismatch
```

---

## Testing

### Unit Tests
- Model loading logic
- Health check validation
- Error handling

### Integration Tests
- Full lifespan cycle
- Model accessibility via dependencies
- Graceful degradation

### Test Coverage
```bash
# Run lifespan tests
pytest tests/integration/test_lifespan_ml_loading.py -v

# Run with database
DATABASE_URL="postgresql+asyncpg://..." pytest tests/integration/test_lifespan_ml_loading.py
```

---

## Best Practices

### ✅ DO

1. **Load models in lifespan** - Not at module import time
2. **Use async context managers** - Proper resource cleanup
3. **Fail fast in production** - Don't serve without models
4. **Run health checks** - Validate before accepting requests
5. **Store in app.state** - Thread-safe, request-accessible
6. **Log verbosely** - Track startup/shutdown progress
7. **Handle errors gracefully** - Set to None, return 503

### ❌ DON'T

1. **Don't load at import time** - Breaks worker processes
2. **Don't use global variables** - Not thread-safe
3. **Don't ignore health checks** - Catch issues early
4. **Don't leak resources** - Always cleanup on shutdown
5. **Don't block startup** - Use async operations
6. **Don't swallow errors** - Log and fail appropriately

---

## Troubleshooting

### Models Not Loading

**Symptom:** Application starts but endpoints return 503

**Causes:**
1. No production models in registry
2. Model files missing/corrupted
3. Checksum mismatch
4. Database connection issues

**Solution:**
```bash
# Check production models
python scripts/promote_model.py status

# Verify model files
ls -lh models/production/treelite/
ls -lh models/production/onnx/

# Check logs
tail -f logs/cortex.log | grep "ML"
```

### Startup Hangs

**Symptom:** Application doesn't become ready

**Causes:**
1. Database connection timeout
2. Large model files (slow disk I/O)
3. Health check timeout

**Solution:**
```bash
# Check database connectivity
psql $DATABASE_URL -c "SELECT 1"

# Monitor startup
tail -f logs/cortex.log

# Increase timeouts (if needed)
export DB_POOL_TIMEOUT=60
```

### Memory Issues

**Symptom:** OOM errors during startup

**Causes:**
1. Multiple workers loading models
2. Insufficient container memory
3. Memory leaks

**Solution:**
```bash
# Check memory usage
docker stats cortex-api

# Reduce workers
export WEB_CONCURRENCY=2

# Increase memory limit
docker run --memory=2g cortex-api
```

---

## Production Deployment

### Docker

```dockerfile
# Ensure models are in image
COPY models/production /app/models/production

# Set production environment
ENV ENVIRONMENT=production

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1
```

### Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
      - name: cortex-api
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 10
        readinessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 15
          periodSeconds: 5
        resources:
          requests:
            memory: "512Mi"
            cpu: "500m"
          limits:
            memory: "2Gi"
            cpu: "2000m"
```

---

## Security Considerations

1. **Model Integrity:** Checksums verified on load
2. **Access Control:** Models loaded from trusted registry
3. **Encryption:** Optional model encryption at rest
4. **Audit Trail:** All model loads logged
5. **Fail-Safe:** Production requires healthy models

---

## Future Enhancements

1. **Hot Reload:** Reload models without restart
2. **A/B Testing:** Load multiple model versions
3. **Canary Deployment:** Gradual model rollout
4. **Model Caching:** Pre-warm model cache
5. **GPU Support:** Enable GPU inference
6. **Model Sharding:** Distribute models across workers

---

## References

- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/)
- [ONNX Runtime Best Practices](https://onnxruntime.ai/docs/performance/)
- [Treelite Documentation](https://treelite.readthedocs.io/)
- [Production ML Serving Guide](https://vife.ai/blog/fastapi-machine-learning-model-serving-guide)

---

**Maintained by:** Cortex AI Team  
**Last Updated:** 2026-04-20  
**Status:** Production Ready ✅
