# Post-Training Workflow Issues Found

## Critical Issues to Fix Before Training Completes

### 1. ModelRegistry API Mismatch ❌ CRITICAL
**Location**: `scripts/production_training_orchestrator.py` line ~1080

**Problem**: Orchestrator uses wrong API signature
```python
# Current (WRONG):
await registry.register_model(
    model_name="xgboost_stock_predictor",
    model_version=self.config.model_version,
    ...
)

# Correct API (from app/ml/model_registry.py):
await registry.register_model(
    version=self.config.model_version,  # No model_name parameter!
    model_type="xgboost",
    artifact_path=model_paths['xgboost'],
    metrics={...},
    metadata={...},  # Not hyperparameters/training_data_info
    feature_version="1.0.0",
    status="development"
)
```

**Fix Required**: Update all `register_model()` calls in orchestrator to match actual API

---

### 2. Missing Dependency: onnxmltools ❌ CRITICAL
**Location**: `scripts/production_training_orchestrator.py` line ~970

**Problem**: XGBoost ONNX export requires `onnxmltools` but it's not installed

**Fix Required**:
```bash
pip install onnxmltools
```

Or update orchestrator to use native XGBoost export:
```python
# Alternative: Use XGBoost native ONNX export
import xgboost as xgb
model.save_model("model.json")
# Then convert JSON to ONNX
```

---

### 3. Missing Dependency: tf2onnx ⚠️ HIGH
**Location**: `scripts/production_training_orchestrator.py` line ~1000

**Problem**: GRU (Keras) ONNX export requires `tf2onnx` but may not be installed

**Fix Required**:
```bash
pip install tf2onnx
```

---

## Test Results Summary

| Component | Status | Issue |
|-----------|--------|-------|
| Model Registry | ❌ FAILED | API signature mismatch |
| ONNX Converter | ✅ SKIPPED | Not used for Keras models |
| XGBoost Export | ❌ FAILED | Missing onnxmltools |

---

## Recommended Actions (URGENT)

### Option 1: Quick Fix (Install Dependencies)
```bash
cd /home/preet/code/Cortex_Merge_AI-ML/backend
source .venv/bin/activate
pip install onnxmltools tf2onnx
```

### Option 2: Fix API Calls (15 minutes)
1. Update `register_model()` calls in orchestrator to match actual API
2. Test with validation script
3. Let training continue

### Option 3: Skip Model Registry (Temporary)
1. Comment out model registry registration step
2. Models will still be saved to disk
3. Can register manually later

---

## Impact if Not Fixed

If training completes without these fixes:
- ✅ Models will be trained successfully
- ✅ Models will be saved to disk (JSON/H5 format)
- ❌ ONNX export will fail
- ❌ Model registry registration will fail
- ⚠️ Will need to re-run export/registration manually

**Recommendation**: Install dependencies NOW (Option 1) - takes 30 seconds, prevents hours of wasted training.
