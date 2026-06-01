# ML Model Deployment Runbook

## Overview

This runbook covers the complete process for training, evaluating, and deploying ML models to production.

---

## Prerequisites

- Access to training environment
- PostgreSQL database access
- Redis access
- ML_MODEL_ENCRYPTION_KEY environment variable set
- Python 3.11+ with dependencies installed

---

## 1. Model Training

### 1.1 Prepare Training Data

```bash
# Navigate to backend directory
cd backend

# Activate virtual environment
source .venv/bin/activate

# Run data preparation script
python scripts/prepare_training_data.py \
  --start-date 2023-01-01 \
  --end-date 2024-01-01 \
  --symbols BTCUSDT,ETHUSDT,BNBUSDT \
  --timeframe 1h \
  --output data/training/ohlcv_1h.parquet
```

**Expected Output**:
```
✓ Loaded 8760 OHLCV bars for BTCUSDT
✓ Loaded 8760 OHLCV bars for ETHUSDT
✓ Loaded 8760 OHLCV bars for BNBUSDT
✓ Total samples: 26,280
✓ Saved to data/training/ohlcv_1h.parquet
```

### 1.2 Train Model

```bash
# Run training pipeline
python -m app.ml.training.trainer \
  --data data/training/ohlcv_1h.parquet \
  --model-name lstm_transformer_v1 \
  --model-version 1.0.0 \
  --timeframe 1h \
  --epochs 100 \
  --batch-size 32 \
  --learning-rate 0.001 \
  --output models/lstm_transformer_v1_1.0.0.pth
```

**Expected Output**:
```
Epoch 1/100: loss=0.4523, val_loss=0.4201
Epoch 2/100: loss=0.3891, val_loss=0.3756
...
Epoch 100/100: loss=0.1234, val_loss=0.1456

Training completed in 2h 15m
✓ Model saved to models/lstm_transformer_v1_1.0.0.pth
```

### 1.3 Evaluate Model

```bash
# Run evaluation
python -m app.ml.evaluation.metrics \
  --model models/lstm_transformer_v1_1.0.0.pth \
  --test-data data/training/ohlcv_1h_test.parquet \
  --output reports/evaluation_1.0.0.json
```

**Expected Output**:
```json
{
  "directional_accuracy": 0.87,
  "buy_accuracy": 0.85,
  "sell_accuracy": 0.82,
  "hold_accuracy": 0.90,
  "avg_latency_ms": 45.2,
  "confusion_matrix": [[850, 100, 50], [120, 820, 60], [80, 90, 830]]
}
```

---

## 2. Quality Gate Checks

### 2.1 Accuracy Threshold

**Requirement**: Directional accuracy > 85%

```bash
# Check accuracy
python scripts/check_quality_gates.py \
  --report reports/evaluation_1.0.0.json \
  --min-accuracy 0.85
```

**Pass Criteria**:
- ✓ Directional accuracy ≥ 85%
- ✓ No class has accuracy < 80%
- ✓ Confusion matrix shows no severe bias

**If Failed**:
1. Review training data for quality issues
2. Adjust hyperparameters
3. Increase training data size
4. Retrain model

### 2.2 Latency Check

**Requirement**: P95 latency < 250ms

```bash
# Run latency benchmark
python scripts/benchmark_latency.py \
  --model models/lstm_transformer_v1_1.0.0.pth \
  --iterations 100
```

**Pass Criteria**:
- ✓ P95 latency < 250ms
- ✓ P99 latency < 300ms
- ✓ Average latency < 100ms

**If Failed**:
1. Optimize model architecture
2. Reduce model size
3. Enable ONNX optimizations
4. Consider quantization

### 2.3 Feature Drift Check

**Requirement**: Feature distribution matches training data

```bash
# Check feature drift
python scripts/check_feature_drift.py \
  --model models/lstm_transformer_v1_1.0.0.pth \
  --reference-data data/training/ohlcv_1h.parquet \
  --test-data data/validation/ohlcv_1h_recent.parquet
```

**Pass Criteria**:
- ✓ KL divergence < 0.1 for all features
- ✓ No features with extreme drift (> 0.5)

**If Failed**:
1. Investigate data quality issues
2. Retrain with more recent data
3. Update feature engineering logic

---

## 3. Model Registration

### 3.1 Convert to ONNX

```bash
# Convert PyTorch model to ONNX
python -m app.ml.inference.onnx_converter \
  --pytorch-model models/lstm_transformer_v1_1.0.0.pth \
  --output models/lstm_transformer_v1_1.0.0.onnx \
  --opset-version 14
```

**Expected Output**:
```
✓ Model converted to ONNX
✓ Validation passed (outputs match PyTorch)
✓ Saved to models/lstm_transformer_v1_1.0.0.onnx
```

### 3.2 Register Model

```bash
# Register model in registry
python scripts/register_model.py \
  --model-path models/lstm_transformer_v1_1.0.0.onnx \
  --version 1.0.0 \
  --model-type lstm_transformer \
  --metrics reports/evaluation_1.0.0.json \
  --feature-version 1.0.0 \
  --status development
```

**Expected Output**:
```
✓ Model encrypted with Fernet
✓ SHA256 checksum computed: a1b2c3d4...
✓ Model registered in database
✓ Model ID: lstm_transformer_v1_1.0.0
✓ Status: development
```

---

## 4. Staging Deployment

### 4.1 Promote to Staging

```bash
# Promote model to staging
python scripts/promote_model.py \
  --version 1.0.0 \
  --from-status development \
  --to-status staging \
  --approved-by "your_username"
```

**Expected Output**:
```
✓ Model 1.0.0 promoted to staging
✓ Previous staging model demoted to development
✓ Audit log created
```

### 4.2 Staging Validation

```bash
# Run staging tests
python scripts/test_staging_model.py \
  --version 1.0.0 \
  --test-symbols BTCUSDT,ETHUSDT \
  --iterations 100
```

**Pass Criteria**:
- ✓ All predictions return valid outputs
- ✓ Latency within acceptable range
- ✓ No errors or exceptions
- ✓ SHAP explanations generated

**If Failed**:
1. Review error logs
2. Check model loading
3. Verify feature computation
4. Rollback if necessary

---

## 5. Production Deployment

### 5.1 A/B Testing (Optional)

```bash
# Enable A/B testing (50/50 split)
python scripts/enable_ab_test.py \
  --model-a production_current \
  --model-b 1.0.0 \
  --traffic-split 50 \
  --duration 24h
```

**Monitor**:
- Accuracy comparison
- Latency comparison
- Error rate comparison
- User feedback

### 5.2 Promote to Production

```bash
# Promote to production
python scripts/promote_model.py \
  --version 1.0.0 \
  --from-status staging \
  --to-status production \
  --approved-by "your_username" \
  --reason "Passed all quality gates and A/B testing"
```

**Expected Output**:
```
✓ Model 1.0.0 promoted to production
✓ Previous production model demoted to staging
✓ Audit log created
✓ Deployment notification sent
```

### 5.3 Post-Deployment Validation

```bash
# Verify production deployment
python scripts/verify_production.py \
  --version 1.0.0 \
  --test-symbols BTCUSDT,ETHUSDT,BNBUSDT \
  --iterations 50
```

**Checklist**:
- [ ] Model loads successfully
- [ ] Predictions return valid outputs
- [ ] Latency within SLA (P95 < 250ms)
- [ ] Cache hit rate > 70%
- [ ] No errors in logs
- [ ] Monitoring dashboards updated

---

## 6. Monitoring

### 6.1 Key Metrics to Monitor

**First 1 Hour**:
- Error rate (should be 0%)
- Latency (P95, P99)
- Prediction distribution
- Cache hit rate

**First 24 Hours**:
- Directional accuracy (rolling window)
- Feature drift score
- User feedback
- System resource usage

**First 7 Days**:
- Long-term accuracy trends
- Drift detection alerts
- Performance degradation
- Comparison with previous model

### 6.2 Monitoring Commands

```bash
# Check current production model
python scripts/get_production_model.py

# Check model accuracy (last 24h)
python scripts/check_accuracy.py --window 24h

# Check feature drift
python scripts/check_drift.py --model-version 1.0.0

# View recent predictions
python scripts/view_predictions.py --limit 100
```

---

## 7. Rollback Procedures

### 7.1 When to Rollback

**Immediate Rollback**:
- Accuracy drops below 80%
- Error rate > 5%
- P95 latency > 500ms
- Critical bugs discovered

**Planned Rollback**:
- Feature drift > 0.2
- Accuracy drops below 85%
- User complaints increase

### 7.2 Rollback Steps

```bash
# 1. Identify previous production model
python scripts/get_model_history.py --status production

# 2. Rollback to previous version
python scripts/rollback_model.py \
  --current-version 1.0.0 \
  --rollback-to 0.9.5 \
  --reason "Accuracy degradation detected" \
  --approved-by "your_username"

# 3. Verify rollback
python scripts/verify_production.py --version 0.9.5

# 4. Monitor for 1 hour
python scripts/monitor_model.py --version 0.9.5 --duration 1h

# 5. Document incident
python scripts/create_incident_report.py \
  --model-version 1.0.0 \
  --issue "Accuracy degradation" \
  --resolution "Rolled back to 0.9.5"
```

**Expected Output**:
```
✓ Model 0.9.5 promoted to production
✓ Model 1.0.0 demoted to staging
✓ Audit log created
✓ Incident report created
✓ Team notified
```

---

## 8. Troubleshooting

### Issue: Model Registration Fails

**Symptoms**: Error during model registration

**Diagnosis**:
```bash
# Check encryption key
echo $ML_MODEL_ENCRYPTION_KEY

# Check model file
ls -lh models/lstm_transformer_v1_1.0.0.onnx

# Check database connection
python scripts/test_db_connection.py
```

**Resolution**:
1. Verify encryption key is set
2. Verify model file exists and is valid ONNX
3. Check database connectivity
4. Review error logs

### Issue: High Latency in Production

**Symptoms**: P95 latency > 250ms

**Diagnosis**:
```bash
# Run latency profiler
python scripts/profile_latency.py --version 1.0.0

# Check cache hit rate
python scripts/check_cache_stats.py

# Check system resources
top
```

**Resolution**:
1. Increase cache TTL if hit rate is low
2. Optimize feature computation
3. Scale horizontally (add more pods)
4. Enable ONNX optimizations

### Issue: Accuracy Degradation

**Symptoms**: Accuracy drops below 85%

**Diagnosis**:
```bash
# Check feature drift
python scripts/check_drift.py --model-version 1.0.0

# Analyze recent predictions
python scripts/analyze_predictions.py --window 7d

# Compare with training data
python scripts/compare_distributions.py
```

**Resolution**:
1. If drift detected: Retrain with recent data
2. If no drift: Investigate market regime change
3. Consider ensemble with multiple models
4. Rollback if necessary

---

## 9. Checklist

### Pre-Deployment
- [ ] Training data prepared and validated
- [ ] Model trained with acceptable loss
- [ ] Directional accuracy > 85%
- [ ] P95 latency < 250ms
- [ ] Feature drift check passed
- [ ] Model converted to ONNX
- [ ] Model registered in registry
- [ ] Staging tests passed

### Deployment
- [ ] Model promoted to production
- [ ] Post-deployment validation passed
- [ ] Monitoring dashboards updated
- [ ] Team notified
- [ ] Documentation updated

### Post-Deployment
- [ ] Monitor for 1 hour (no errors)
- [ ] Monitor for 24 hours (accuracy stable)
- [ ] Monitor for 7 days (no drift)
- [ ] Incident report (if issues)
- [ ] Retrospective (lessons learned)

---

## 10. Contacts

**On-Call Engineer**: oncall@cortex.ai  
**ML Team Lead**: ml-lead@cortex.ai  
**DevOps Team**: devops@cortex.ai  
**Incident Response**: incidents@cortex.ai

---

## 11. References

- **Architecture**: `docs/architecture/ML_SYSTEM_ARCHITECTURE.md`
- **API Documentation**: `docs/api/ML_PREDICTION_API.md`
- **Troubleshooting**: `docs/runbooks/ML_TROUBLESHOOTING.md`
- **Monitoring**: `docs/runbooks/ML_MONITORING.md`
