# Baseline Prediction Statistics for Drift Detection

## Overview

Production-grade baseline statistics system for ML drift monitoring. Establishes statistical baselines from training/evaluation data to detect when live predictions drift from expected distributions.

## Architecture

### Components

1. **Manual Population Script** (`scripts/populate_baseline_statistics.py`)
   - Generates predictions on historical data
   - Samples 5,000 predictions across all training symbols
   - Lookback: 60 days
   - Computes statistics for raw and filtered predictions

2. **Automated Pipeline** (`app/ml/baseline_computer.py`)
   - Integrates with model promotion workflow
   - Automatically computes baseline on promotion to production
   - Lightweight (500 samples for speed)

3. **Drift Detector** (`app/ai/governance/drift_detector.py`)
   - Compares live predictions against baseline
   - Uses z-score approach for drift detection
   - Triggers alerts when drift exceeds threshold

## Statistics Schema

```json
{
  "raw_predictions": {
    "mean": 0.658,
    "std": 0.15,
    "min": 0.0,
    "max": 1.0,
    "sample_size": 30000
  },
  "filtered_predictions": {
    "mean": 0.75,
    "std": 0.12,
    "min": 0.6,
    "max": 1.0,
    "sample_size": 18000,
    "confidence_threshold": 0.6
  },
  "computed_at": "2026-04-20T10:31:00Z",
  "data_period": "2026-02-20 to 2026-04-20"
}
```

## Usage

### Populate Baseline for All Production Models

```bash
cd backend
source .venv/bin/activate
DATABASE_URL="postgresql+asyncpg://cortex:cortex_pg@localhost:5433/cortex_db" \
python scripts/populate_baseline_statistics.py --all-production
```

### Populate Baseline for Single Model

```bash
python scripts/populate_baseline_statistics.py --model-id xgboost_1.0.0_xgboost
```

### Automated (During Model Promotion)

Baseline statistics are automatically computed when models are promoted to production via the model promotion pipeline.

## Drift Detection

The drift detector (`DriftDetector.check_drift()`) uses baseline statistics to:

1. Compare current prediction mean against baseline mean
2. Calculate z-score: `z = |current_mean - baseline_mean| / baseline_std`
3. Trigger alert if `z > threshold` (default: 2.0 sigma)
4. Automatically demote models: live → paper → shadow → retired

## Configuration

- **Sample Size**: 5,000 predictions (configurable)
- **Lookback Period**: 60 days (configurable)
- **Confidence Threshold**: 0.6 (configurable)
- **Drift Threshold**: 2.0 sigma (set in `app/core/config.py`)

## Current Baselines

| Model | Mean | Std | Sample Size | Accuracy |
|-------|------|-----|-------------|----------|
| XGBoost | 0.658 | 0.15 | 30,000 | 65.81% |
| GRU | 0.532 | 0.18 | 30,000 | 53.17% |

## Best Practices

1. **Recompute Periodically**: Run baseline computation monthly to account for market regime changes
2. **Monitor Drift Alerts**: Set up monitoring for drift alerts via Redis pub/sub
3. **Validate Before Promotion**: Always compute baseline before promoting models to production
4. **Document Changes**: Update this document when baseline computation logic changes

## Troubleshooting

### Insufficient Data

If historical data is insufficient (<100 samples), the system falls back to default statistics based on training evaluation results.

### Table Not Found

Ensure `upstox_ohlcv` table exists and contains recent data (last 60 days minimum).

### Slow Performance

Reduce `sample_size` or `lookback_days` parameters for faster computation.

## References

- Industry Standard: PSI (Population Stability Index) > 0.1-0.25 indicates drift
- Z-score > 2.0 sigma = 95% confidence interval
- Best practices: [Monitoring Model Drift in Production](https://srikanthdevarajan.substack.com/p/monitoring-model-drift-in-production)
