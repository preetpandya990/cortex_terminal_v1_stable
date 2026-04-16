# Final Model Training Script

## Overview
Production-grade script to train final models using best hyperparameters found during hyperparameter search.

## Features
- ✅ Loads best hyperparameters from Keras Tuner (GRU: 87.36% val_accuracy)
- ✅ Trains on full dataset (80/20 train/val split for early stopping)
- ✅ Exports to native formats (JSON/H5) and ONNX
- ✅ Registers in encrypted model registry
- ✅ Comprehensive logging and error handling
- ✅ Production-ready with world-class standards

## Usage

### 1. Kill Current Training (if still running)
```bash
kill 65170
```

### 2. Run Final Training
```bash
cd /home/preet/code/Cortex_Merge_AI-ML/backend
source .venv/bin/activate
python scripts/train_final_models.py
```

### 3. Expected Duration
- **~30-45 minutes** total
- XGBoost: ~5 minutes
- GRU: ~20-30 minutes (with best hyperparameters)
- Ensemble: ~5 minutes
- Export/Registry: ~5 minutes

## What It Does

1. **Loads Best Hyperparameters**
   - GRU: From `hyperparameter_tuning/gru_stock_prediction/`
   - XGBoost: Uses production defaults

2. **Trains Final Models**
   - Same 50 symbols as hyperparameter search
   - 3 years of data
   - 47 features (42 technical + 5 sentiment)

3. **Exports Models**
   - Native: `models/production/models/xgboost_final.json`, `gru_final.h5`
   - ONNX: `models/production/onnx/xgboost_final.onnx`, `gru_final.onnx`

4. **Registers in Model Registry**
   - Encrypted storage
   - Full metadata and metrics
   - Status: "production"

5. **Saves Results**
   - JSON file with all metrics and configuration
   - Timestamped for tracking

## Output

```
models/production/
├── models/
│   ├── xgboost_final.json
│   └── gru_final.h5
├── onnx/
│   ├── xgboost_final.onnx
│   └── gru_final.onnx
└── final_training_results_YYYYMMDD_HHMMSS.json
```

## Best Hyperparameters Found

### GRU (87.36% validation accuracy)
```python
{
    'gru_units_1': 192,
    'gru_units_2': 48,
    'dense_units': 16,
    'dropout': 0.1,
    'recurrent_dropout': 0.2,
    'l2_reg': 0.000492,
    'learning_rate': 0.000749
}
```

### XGBoost (Production Defaults)
```python
{
    'max_depth': 6,
    'learning_rate': 0.1,
    'n_estimators': 300,
    'subsample': 0.8,
    'colsample_bytree': 0.8
}
```

## Monitoring

Watch the log file in real-time:
```bash
tail -f final_training_*.log
```

## Next Steps

After training completes:
1. Verify models in `models/production/`
2. Check metrics in results JSON
3. Test E2E prediction flow
4. Deploy to production

## Troubleshooting

**If GRU hyperparameters fail to load:**
- Script will use sensible defaults
- Check `hyperparameter_tuning/gru_stock_prediction/` exists

**If ONNX export fails:**
- Native models still saved
- Can export manually later

**If registry registration fails:**
- Models still saved to disk
- Can register manually later

## Configuration

Edit `FinalTrainingConfig` in script to customize:
- `n_symbols`: Number of stocks (default: 50)
- `lookback_years`: Historical data (default: 3)
- `max_epochs`: Training epochs (default: 100)
- `early_stopping_patience`: Patience (default: 15)
