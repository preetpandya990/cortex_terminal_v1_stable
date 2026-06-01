# ML Model Training Guide

## Overview

This guide covers the complete model training process, including data preparation, hyperparameter tuning, training execution, and model evaluation.

---

## Training Architecture

### Model: MultiOutputModel (LSTM-based)

**Architecture**:
- Input: (batch_size, sequence_length, num_features)
- LSTM layers: 2 layers with dropout
- Output heads:
  - Direction classifier (3 classes: SELL, HOLD, BUY)
  - Confidence regressor (0-1)
  - Price regressors (entry, tp1, tp2, tp3, stop_loss)
  - Volatility regressor

**Loss Function**: MultiOutputLoss
- Classification loss (CrossEntropy) for direction
- Regression loss (MSE) for prices and confidence
- Weighted combination of all losses

---

## Quick Start

### 1. Basic Training

```python
from app.ml.training.trainer import Trainer
from app.ml.training.feature_pipeline import FeaturePipeline
from app.ml.models import MultiOutputModel
import torch

# Prepare data
pipeline = FeaturePipeline(session=db_session)
X, y = await pipeline.prepare_training_data(
    symbol="NSE_EQ|INE002A01018",
    timeframe="1d"
)

# Create data loaders
train_loader, val_loader = create_data_loaders(X, y, batch_size=32)

# Initialize model
model = MultiOutputModel(
    input_size=X.shape[2],  # Number of features
    hidden_size=128,
    num_layers=2,
    dropout=0.2
)

# Train
trainer = Trainer(model, session=db_session, learning_rate=0.001)
results = await trainer.train(
    train_loader=train_loader,
    val_loader=val_loader,
    num_epochs=100,
    early_stopping_patience=10
)

print(f"Best accuracy: {results['best_val_accuracy']:.4f}")
```

### 2. Train All Timeframes

```python
from app.ml.training.train_all_timeframes import train_all_timeframes

# Train models for all timeframes
results = await train_all_timeframes(
    session=db_session,
    symbol="NSE_EQ|INE002A01018",
    timeframes=["1d", "1w", "1M"]
)

for timeframe, result in results.items():
    print(f"{timeframe}: {result['accuracy']:.4f}")
```

---

## Data Preparation

### Step 1: Load OHLCV Data

```python
from app.ml.training.feature_pipeline import FeaturePipeline
from datetime import datetime

pipeline = FeaturePipeline(
    session=db_session,
    sequence_length=60,      # 60 time steps
    prediction_horizon=5     # Predict 5 days ahead
)

# Load and prepare data
X, y = await pipeline.prepare_training_data(
    symbol="NSE_EQ|INE002A01018",
    timeframe="1d",
    start_date=datetime(2020, 1, 1),
    end_date=datetime(2024, 12, 31),
    validate=True  # Validate data quality
)
```

**Output**:
- `X`: (num_samples, 60, 40+) - Feature sequences
- `y`: Dictionary with labels:
  - `direction`: (num_samples,) - 0=SELL, 1=HOLD, 2=BUY
  - `confidence`: (num_samples,) - 0.0 to 1.0
  - `entry_price`: (num_samples,)
  - `tp1`, `tp2`, `tp3`: (num_samples,) - Take profit levels
  - `stop_loss`: (num_samples,)
  - `volatility`: (num_samples,)

### Step 2: Train/Validation Split

```python
from sklearn.model_selection import train_test_split

# Temporal split (80/20)
split_idx = int(len(X) * 0.8)
X_train, X_val = X[:split_idx], X[split_idx:]
y_train, y_val = {}, {}

for key in y:
    y_train[key] = y[key][:split_idx]
    y_val[key] = y[key][split_idx:]
```

### Step 3: Create DataLoaders

```python
import torch
from torch.utils.data import TensorDataset, DataLoader

# Convert to tensors
X_train_tensor = torch.FloatTensor(X_train)
y_train_tensors = {k: torch.FloatTensor(v) for k, v in y_train.items()}

# Create dataset
train_dataset = TensorDataset(
    X_train_tensor,
    y_train_tensors["direction"],
    y_train_tensors["confidence"],
    # ... other labels
)

# Create loader
train_loader = DataLoader(
    train_dataset,
    batch_size=32,
    shuffle=True,
    num_workers=4
)
```

---

## Hyperparameter Tuning

### Key Hyperparameters

| Parameter | Default | Range | Description |
|-----------|---------|-------|-------------|
| `hidden_size` | 128 | 64-256 | LSTM hidden units |
| `num_layers` | 2 | 1-3 | LSTM layers |
| `dropout` | 0.2 | 0.1-0.5 | Dropout rate |
| `learning_rate` | 0.001 | 0.0001-0.01 | Adam LR |
| `batch_size` | 32 | 16-128 | Batch size |
| `sequence_length` | 60 | 30-120 | Input sequence length |

### Grid Search

```python
from itertools import product

# Define search space
param_grid = {
    "hidden_size": [64, 128, 256],
    "num_layers": [1, 2, 3],
    "dropout": [0.1, 0.2, 0.3],
    "learning_rate": [0.0001, 0.001, 0.01]
}

best_accuracy = 0
best_params = None

# Grid search
for hidden_size, num_layers, dropout, lr in product(*param_grid.values()):
    model = MultiOutputModel(
        input_size=X.shape[2],
        hidden_size=hidden_size,
        num_layers=num_layers,
        dropout=dropout
    )
    
    trainer = Trainer(model, session=db_session, learning_rate=lr)
    results = await trainer.train(train_loader, val_loader, num_epochs=50)
    
    if results["best_val_accuracy"] > best_accuracy:
        best_accuracy = results["best_val_accuracy"]
        best_params = {
            "hidden_size": hidden_size,
            "num_layers": num_layers,
            "dropout": dropout,
            "learning_rate": lr
        }

print(f"Best params: {best_params}")
print(f"Best accuracy: {best_accuracy:.4f}")
```

### Random Search (Faster)

```python
import random

# Random search (20 iterations)
for _ in range(20):
    params = {
        "hidden_size": random.choice([64, 128, 256]),
        "num_layers": random.choice([1, 2, 3]),
        "dropout": random.uniform(0.1, 0.5),
        "learning_rate": 10 ** random.uniform(-4, -2)
    }
    
    # Train with params
    model = MultiOutputModel(input_size=X.shape[2], **params)
    trainer = Trainer(model, session=db_session, learning_rate=params["learning_rate"])
    results = await trainer.train(train_loader, val_loader, num_epochs=50)
    
    # Track best
    # ...
```

---

## Training Execution

### Training Loop

```python
trainer = Trainer(
    model=model,
    session=db_session,
    device="cuda" if torch.cuda.is_available() else "cpu",
    learning_rate=0.001,
    checkpoint_dir="models/checkpoints"
)

results = await trainer.train(
    train_loader=train_loader,
    val_loader=val_loader,
    num_epochs=100,
    early_stopping_patience=10,
    experiment_name="btc_daily_v1"
)
```

**Training Features**:
- Early stopping (stops if no improvement for N epochs)
- Model checkpointing (saves best model)
- Metrics tracking (loss, accuracy, precision, recall, F1)
- Experiment logging to database

### Monitoring Training

```python
# Access training history
history = results["training_history"]

import matplotlib.pyplot as plt

# Plot loss
plt.plot(history["train_loss"], label="Train")
plt.plot(history["val_loss"], label="Validation")
plt.xlabel("Epoch")
plt.ylabel("Loss")
plt.legend()
plt.show()

# Plot accuracy
plt.plot(history["train_accuracy"], label="Train")
plt.plot(history["val_accuracy"], label="Validation")
plt.xlabel("Epoch")
plt.ylabel("Accuracy")
plt.legend()
plt.show()
```

---

## Model Evaluation

### Evaluation Metrics

```python
from sklearn.metrics import classification_report, confusion_matrix

# Get predictions
model.eval()
with torch.no_grad():
    predictions = model(X_val_tensor)
    direction_pred = predictions["direction"].argmax(dim=1).cpu().numpy()

# Classification report
print(classification_report(
    y_val["direction"],
    direction_pred,
    target_names=["SELL", "HOLD", "BUY"]
))

# Confusion matrix
cm = confusion_matrix(y_val["direction"], direction_pred)
print(cm)
```

### Quality Gates

Before deploying, verify model meets quality gates:

| Metric | Threshold | Check |
|--------|-----------|-------|
| Accuracy | > 85% | ✅ |
| Precision (BUY) | > 80% | ✅ |
| Recall (BUY) | > 75% | ✅ |
| F1 Score | > 80% | ✅ |
| P95 Latency | < 250ms | ✅ |

```python
from app.ml.training.evaluation import evaluate_model

metrics = await evaluate_model(model, val_loader)

# Check quality gates
assert metrics["accuracy"] > 0.85, "Accuracy too low"
assert metrics["precision_buy"] > 0.80, "Precision too low"
assert metrics["recall_buy"] > 0.75, "Recall too low"
assert metrics["f1_score"] > 0.80, "F1 score too low"

print("✅ All quality gates passed")
```

---

## Model Registration

### Register Trained Model

```python
from app.ml.model_registry import ModelRegistry

registry = ModelRegistry(
    session=db_session,
    storage_path="ml_models",
    encryption_key=os.getenv("ML_MODEL_ENCRYPTION_KEY")
)

# Register model
model_id = await registry.register_model(
    model=model,
    name="btc_daily_v1",
    version="1.0.0",
    symbol="NSE_EQ|INE002A01018",
    timeframe="1d",
    feature_version="1.0.0",
    metrics={
        "accuracy": 0.87,
        "precision": 0.85,
        "recall": 0.82,
        "f1_score": 0.83
    },
    hyperparameters={
        "hidden_size": 128,
        "num_layers": 2,
        "dropout": 0.2,
        "learning_rate": 0.001
    }
)

print(f"Model registered: {model_id}")
```

---

## Advanced Training

### Transfer Learning

```python
# Load pretrained model
pretrained_model = await registry.load_model("btc_daily_v1")

# Freeze early layers
for param in pretrained_model.lstm.parameters():
    param.requires_grad = False

# Fine-tune on new data
trainer = Trainer(pretrained_model, session=db_session, learning_rate=0.0001)
results = await trainer.train(new_train_loader, new_val_loader, num_epochs=20)
```

### Multi-Asset Training

```python
symbols = [
    "NSE_EQ|INE002A01018",  # Reliance
    "NSE_EQ|INE009A01021",  # Infosys
    "NSE_EQ|INE040A01034"   # HDFC Bank
]

for symbol in symbols:
    X, y = await pipeline.prepare_training_data(symbol, "1d")
    train_loader, val_loader = create_data_loaders(X, y)
    
    model = MultiOutputModel(input_size=X.shape[2])
    trainer = Trainer(model, session=db_session)
    results = await trainer.train(train_loader, val_loader)
    
    await registry.register_model(
        model=model,
        name=f"{symbol}_daily",
        version="1.0.0",
        symbol=symbol,
        timeframe="1d"
    )
```

### Ensemble Training

```python
# Train multiple models with different seeds
models = []
for seed in range(5):
    torch.manual_seed(seed)
    model = MultiOutputModel(input_size=X.shape[2])
    trainer = Trainer(model, session=db_session)
    results = await trainer.train(train_loader, val_loader)
    models.append(model)

# Register ensemble
await registry.register_ensemble(
    models=models,
    name="btc_daily_ensemble",
    version="1.0.0",
    strategy="voting"
)
```

---

## Troubleshooting

### Issue: Training loss not decreasing
**Solutions**:
- Reduce learning rate (try 0.0001)
- Increase batch size
- Check data normalization
- Verify labels are correct

### Issue: Overfitting (train acc >> val acc)
**Solutions**:
- Increase dropout (0.3-0.5)
- Add L2 regularization
- Reduce model complexity
- Get more training data

### Issue: Underfitting (both accuracies low)
**Solutions**:
- Increase model capacity (hidden_size, num_layers)
- Reduce dropout
- Train for more epochs
- Add more features

### Issue: Out of memory
**Solutions**:
- Reduce batch size
- Reduce sequence length
- Use gradient accumulation
- Train on CPU if GPU memory limited

---

## Best Practices

1. **Always use temporal splits** - Never shuffle time-series data
2. **Validate data quality** - Check for missing values, outliers
3. **Track experiments** - Log all hyperparameters and metrics
4. **Use early stopping** - Prevent overfitting
5. **Save checkpoints** - Don't lose best model
6. **Test on unseen data** - Verify generalization
7. **Monitor training** - Watch for anomalies
8. **Version everything** - Models, features, data

---

## References

- **Trainer**: `app/ml/training/trainer.py`
- **Feature Pipeline**: `app/ml/training/feature_pipeline.py`
- **Model Architecture**: `app/ml/models/multi_output_model.py`
- **Model Registry**: `app/ml/model_registry.py`
- **Evaluation**: `app/ml/training/evaluation.py`

---

**Last Updated**: 2026-04-09  
**Version**: 1.0.0
