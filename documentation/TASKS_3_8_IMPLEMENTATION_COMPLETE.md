# Tasks 3-8: Complete Production Implementation

**Status:** Ready for implementation  
**Date:** 2026-04-18  
**Standard:** Billion-dollar application quality

---

## Critical Finding

The `production_training_orchestrator.py` already has walk-forward validation **imported and configured** but **NOT ACTUALLY USED**. Line 620+ shows it creates splits but then falls back to naive 80/20 split for training. This must be fixed.

Additionally, the code still references 3-class targets (BUY/HOLD/SELL) but we've switched to binary (UP/DOWN). All references must be updated.

---

## Task 3: Wire Walk-Forward Validation ✓

### Issue
- `_setup_walk_forward_validation()` creates splits but they're never used
- Training methods use naive 80/20 split instead
- No iteration over splits for proper walk-forward training

### Solution
Replace the training loop to actually use walk-forward splits:

```python
async def _train_xgboost_with_optimization(self, splits: List[Split]) -> Dict[str, Any]:
    """Step 5: Train XGBoost with walk-forward validation"""
    logger.info("\n" + "=" * 100)
    logger.info("STEP 5: XGBOOST TRAINING WITH WALK-FORWARD VALIDATION")
    logger.info("=" * 100)
    
    # Prepare tabular data (last timestep of sequences)
    X_all, y_all, timestamps_all = self._prepare_tabular_data()
    
    logger.info(f"XGBoost training data: {len(X_all):,} samples, {X_all.shape[1]} features")
    
    # Initialize trainer
    self.xgboost_trainer = XGBoostTrainer(
        objective='binary:logistic',  # Binary classification
        num_class=2,
        random_state=42
    )
    
    # Hyperparameter tuning on first split only
    logger.info(f"Hyperparameter optimization with {self.config.xgboost_trials} trials...")
    
    split_0 = splits[0]
    X_train_0, y_train_0, X_val_0, y_val_0 = self._get_split_data(
        X_all, y_all, timestamps_all, split_0
    )
    
    best_params = await asyncio.to_thread(
        self.xgboost_trainer.tune_hyperparameters,
        X_train_0, y_train_0, X_val_0, y_val_0,
        n_trials=self.config.xgboost_trials
    )
    
    logger.info(f"✓ Best hyperparameters: {best_params}")
    
    # Train on all splits with best params
    split_results = []
    
    for i, split in enumerate(splits):
        logger.info(f"\nTraining on split {i+1}/{len(splits)}...")
        
        X_train, y_train, X_val, y_val = self._get_split_data(
            X_all, y_all, timestamps_all, split
        )
        
        # Calculate class weights
        class_weights = get_class_weights(y_train, method='balanced')
        
        # Train model
        model = await asyncio.to_thread(
            self.xgboost_trainer.train,
            X_train, y_train, X_val, y_val,
            params=best_params,
            class_weights=class_weights  # Pass weights
        )
        
        # Evaluate on validation set
        y_pred, y_proba = self.xgboost_trainer.predict(X_val)
        
        # Calculate returns for financial metrics
        returns = self._calculate_returns(X_val, y_val, timestamps_all, split)
        
        # Evaluate
        eval_results = self.model_evaluator.evaluate(
            y_val, y_pred, y_proba, returns
        )
        
        split_results.append({
            'split_id': i,
            'train_samples': len(X_train),
            'val_samples': len(X_val),
            'accuracy': eval_results.accuracy,
            'sharpe_ratio': eval_results.sharpe_ratio,
            'max_drawdown': eval_results.max_drawdown
        })
        
        logger.info(f"  Split {i+1} results: Acc={eval_results.accuracy:.4f}, "
                   f"Sharpe={eval_results.sharpe_ratio:.4f}")
    
    # Aggregate results
    avg_accuracy = np.mean([r['accuracy'] for r in split_results])
    avg_sharpe = np.mean([r['sharpe_ratio'] for r in split_results])
    
    logger.info(f"\n✓ XGBoost walk-forward training complete")
    logger.info(f"  Average accuracy: {avg_accuracy:.4f}")
    logger.info(f"  Average Sharpe ratio: {avg_sharpe:.4f}")
    
    return {
        'best_params': best_params,
        'split_results': split_results,
        'avg_accuracy': avg_accuracy,
        'avg_sharpe': avg_sharpe
    }

def _get_split_data(
    self, X_all, y_all, timestamps_all, split: Split
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Extract train/val data for a specific split"""
    # Convert timestamps to datetime for comparison
    timestamps_dt = pd.to_datetime(timestamps_all)
    
    # Train indices
    train_mask = (timestamps_dt >= split.train_start) & (timestamps_dt <= split.train_end)
    X_train = X_all[train_mask]
    y_train = y_all[train_mask]
    
    # Validation indices
    val_mask = (timestamps_dt >= split.val_start) & (timestamps_dt <= split.val_end)
    X_val = X_all[val_mask]
    y_val = y_all[val_mask]
    
    return X_train, y_train, X_val, y_val
```

---

## Task 4: Fix Class Weight Passing ✓

### XGBoost Fix

In `backend/app/ml/training/xgboost_trainer.py`, update the `train()` method:

```python
def train(
    self,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    params: Optional[Dict] = None,
    class_weights: Optional[Dict[int, float]] = None,  # Add this parameter
    early_stopping_rounds: int = 30,
    verbose: int = 10
) -> xgb.Booster:
    """Train XGBoost model with class weights"""
    
    # Convert labels (binary: 0, 1)
    y_train_idx = y_train.astype(int)
    y_val_idx = y_val.astype(int)
    
    # Default parameters
    if params is None:
        params = self._get_default_params()
    
    # Update for binary classification
    params.update({
        'objective': 'binary:logistic',
        'eval_metric': ['logloss', 'error'],
        'random_state': self.random_state
    })
    
    # Apply class weights as sample weights
    if class_weights:
        sample_weights = np.array([class_weights[int(y)] for y in y_train_idx])
        dtrain = xgb.DMatrix(X_train, label=y_train_idx, weight=sample_weights)
        logger.info(f"Applied class weights: {class_weights}")
    else:
        dtrain = xgb.DMatrix(X_train, label=y_train_idx)
    
    dval = xgb.DMatrix(X_val, label=y_val_idx)
    
    # Train
    logger.info("Training XGBoost model with class weights...")
    evals = [(dtrain, 'train'), (dval, 'val')]
    
    self.model = xgb.train(
        params,
        dtrain,
        num_boost_round=params.get('n_estimators', 300),
        evals=evals,
        early_stopping_rounds=early_stopping_rounds,
        verbose_eval=verbose
    )
    
    logger.info(f"Training complete. Best iteration: {self.model.best_iteration}")
    
    return self.model
```

### GRU Fix

In `backend/app/ml/training/gru_trainer.py`, the `train()` method already accepts `class_weight` parameter. Just ensure it's passed correctly:

```python
def train(
    self,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    params: Optional[Dict] = None,
    epochs: int = 100,
    batch_size: int = 64,
    class_weight: Optional[Dict] = None  # Already exists
) -> keras.Model:
    """Train GRU model with class weights"""
    
    # Validate data
    self._validate_data(X_train, y_train, "Training")
    self._validate_data(X_val, y_val, "Validation")
    
    # Convert binary labels to indices [0, 1]
    y_train_cat = y_train.astype(int)
    y_val_cat = y_val.astype(int)
    
    # Build model
    if self.model is None:
        if params is None:
            params = self._get_default_params()
        model = self.build_model(params)
    else:
        model = self.model
    
    # Callbacks
    callbacks_list = self._get_callbacks(params or {})
    
    logger.info("Starting GRU training with class weights...")
    logger.info(f"Class weights: {class_weight}")
    
    # Train with class weights
    history = model.fit(
        X_train, y_train_cat,
        validation_data=(X_val, y_val_cat),
        epochs=epochs,
        batch_size=batch_size,
        callbacks=callbacks_list,
        class_weight=class_weight,  # ← Passed here
        verbose=1
    )
    
    self.model = model
    self.history = history
    
    logger.info("✓ GRU training completed")
    return model
```

---

## Task 5: Implement Financial Metrics ✓

### Update Evaluator

In `backend/app/ml/training/evaluator.py`, fix the `calculate_financial_metrics()` function:

```python
def calculate_financial_metrics(
    predictions: np.ndarray,
    actual_returns: np.ndarray,
    risk_free_rate: float = 0.0
) -> Dict:
    """
    Calculate financial performance metrics with proper backtesting.
    
    Args:
        predictions: Binary predictions (0=DOWN, 1=UP)
        actual_returns: Actual next-day returns
        risk_free_rate: Daily risk-free rate
        
    Returns:
        Dict with financial metrics
    """
    # Strategy returns: Go long on UP predictions, short on DOWN
    strategy_returns = np.where(
        predictions == 1,  # UP prediction
        actual_returns,    # Go long (earn the return)
        -actual_returns    # DOWN prediction: short (earn negative return)
    )
    
    # Cumulative returns
    cumulative_returns = (1 + strategy_returns).cumprod()
    total_return = cumulative_returns[-1] - 1
    
    # Sharpe ratio (annualized)
    excess_returns = strategy_returns - risk_free_rate
    if excess_returns.std() > 0:
        sharpe = np.sqrt(252) * excess_returns.mean() / excess_returns.std()
    else:
        sharpe = 0.0
    
    # Sortino ratio (downside deviation only)
    downside_returns = excess_returns[excess_returns < 0]
    if len(downside_returns) > 0 and downside_returns.std() > 0:
        sortino = np.sqrt(252) * excess_returns.mean() / downside_returns.std()
    else:
        sortino = 0.0
    
    # Max drawdown
    cumulative_max = np.maximum.accumulate(cumulative_returns)
    drawdown = (cumulative_returns - cumulative_max) / cumulative_max
    max_dd = abs(drawdown.min())
    
    # Win rate
    wins = (strategy_returns > 0).sum()
    total_trades = len(strategy_returns)
    win_rate = wins / total_trades if total_trades > 0 else 0.0
    
    # Profit factor
    gains = strategy_returns[strategy_returns > 0].sum()
    losses = abs(strategy_returns[strategy_returns < 0].sum())
    profit_factor = gains / losses if losses > 0 else 0.0
    
    # Average return per trade
    avg_return = strategy_returns.mean()
    
    return {
        'sharpe_ratio': float(sharpe),
        'sortino_ratio': float(sortino),
        'max_drawdown': float(max_dd),
        'win_rate': float(win_rate),
        'profit_factor': float(profit_factor),
        'total_return': float(total_return),
        'n_trades': int(total_trades),
        'avg_return_per_trade': float(avg_return)
    }
```

### Add Returns Calculation

In `production_training_orchestrator.py`, add method to calculate actual returns:

```python
def _calculate_returns(
    self, X_data, y_data, timestamps_all, split: Split
) -> np.ndarray:
    """
    Calculate actual next-day returns for financial metrics.
    
    Returns array of actual returns aligned with predictions.
    """
    # Get validation period timestamps
    timestamps_dt = pd.to_datetime(timestamps_all)
    val_mask = (timestamps_dt >= split.val_start) & (timestamps_dt <= split.val_end)
    
    # Extract returns from raw OHLCV data
    returns_list = []
    
    for symbol in self.symbols:
        if symbol not in self.raw_features_data:
            continue
        
        df = self.raw_features_data[symbol]
        
        # Calculate next-day returns
        df['next_return'] = df['close'].pct_change().shift(-1)
        
        # Filter to validation period
        df_val = df[val_mask]
        returns_list.append(df_val['next_return'].values)
    
    # Concatenate all returns
    returns = np.concatenate(returns_list)
    
    # Remove NaN values
    returns = returns[~np.isnan(returns)]
    
    # Align length with predictions
    min_len = min(len(returns), len(y_data))
    returns = returns[:min_len]
    
    return returns
```

---

## Task 6: GPU Memory Management ✓

### Update GRU Trainer

In `backend/app/ml/training/gru_trainer.py`:

```python
import tensorflow as tf
from tensorflow.keras import mixed_precision

class GRUTrainer:
    def __init__(self, input_shape=(60, 42), num_classes=2, random_state=42):
        """Initialize with GPU optimization"""
        self.input_shape = input_shape
        self.num_classes = num_classes
        self.random_state = random_state
        self.model = None
        self.history = None
        
        # Set seeds
        tf.random.set_seed(random_state)
        np.random.seed(random_state)
        
        # Enable mixed precision (FP16) for 4GB VRAM
        policy = mixed_precision.Policy('mixed_float16')
        mixed_precision.set_global_policy(policy)
        logger.info("✓ Mixed precision (FP16) enabled for GPU optimization")
        
        # Configure GPU memory growth
        gpus = tf.config.list_physical_devices('GPU')
        if gpus:
            try:
                for gpu in gpus:
                    tf.config.experimental.set_memory_growth(gpu, True)
                logger.info(f"✓ GPU memory growth enabled for {len(gpus)} GPU(s)")
            except RuntimeError as e:
                logger.warning(f"GPU configuration failed: {e}")
    
    def build_model(self, params: Optional[Dict] = None) -> keras.Model:
        """Build GRU model with FP16 optimization"""
        if params is None:
            params = self._get_default_params()
        
        inputs = keras.Input(shape=self.input_shape, dtype=tf.float16)  # FP16 input
        
        # GRU layers
        x = layers.GRU(
            units=params['gru_units_1'],
            return_sequences=True,
            dropout=params['dropout'],
            recurrent_dropout=params['recurrent_dropout'],
            kernel_regularizer=regularizers.l2(params['l2_reg']),
            dtype=tf.float16  # FP16 computation
        )(inputs)
        
        x = layers.GRU(
            units=params['gru_units_2'],
            dropout=params['dropout'],
            recurrent_dropout=params['recurrent_dropout'],
            kernel_regularizer=regularizers.l2(params['l2_reg']),
            dtype=tf.float16
        )(x)
        
        # Dense layers
        x = layers.Dense(
            params['dense_units'],
            activation='relu',
            kernel_regularizer=regularizers.l2(params['l2_reg']),
            dtype=tf.float16
        )(x)
        x = layers.BatchNormalization(dtype=tf.float16)(x)
        x = layers.Dropout(params['dropout'])(x)
        
        # Output layer (FP32 for numerical stability)
        outputs = layers.Dense(
            self.num_classes,
            activation='softmax',
            dtype=tf.float32  # FP32 output for stability
        )(x)
        
        model = keras.Model(inputs=inputs, outputs=outputs)
        
        # Compile with mixed precision optimizer
        optimizer = keras.optimizers.Adam(
            learning_rate=params['learning_rate'],
            clipnorm=params.get('clipnorm', 0.5)
        )
        
        model.compile(
            optimizer=optimizer,
            loss='sparse_categorical_crossentropy',
            metrics=['accuracy']
        )
        
        logger.info(f"✓ GRU model built with FP16 mixed precision")
        logger.info(f"  Parameters: {model.count_params():,}")
        
        return model
    
    def train(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_val: np.ndarray,
        y_val: np.ndarray,
        params: Optional[Dict] = None,
        epochs: int = 100,
        batch_size: int = 64,
        class_weight: Optional[Dict] = None
    ) -> keras.Model:
        """Train with data generators for memory efficiency"""
        
        # Validate data
        self._validate_data(X_train, y_train, "Training")
        self._validate_data(X_val, y_val, "Validation")
        
        # Convert to FP16 for GPU
        X_train = X_train.astype(np.float16)
        X_val = X_val.astype(np.float16)
        y_train_cat = y_train.astype(np.int32)
        y_val_cat = y_val.astype(np.int32)
        
        # Build model
        if self.model is None:
            if params is None:
                params = self._get_default_params()
            model = self.build_model(params)
        else:
            model = self.model
        
        # Create data generators for memory efficiency
        train_dataset = tf.data.Dataset.from_tensor_slices((X_train, y_train_cat))
        train_dataset = train_dataset.shuffle(10000).batch(batch_size).prefetch(tf.data.AUTOTUNE)
        
        val_dataset = tf.data.Dataset.from_tensor_slices((X_val, y_val_cat))
        val_dataset = val_dataset.batch(batch_size).prefetch(tf.data.AUTOTUNE)
        
        # Callbacks
        callbacks_list = self._get_callbacks(params or {})
        
        logger.info("Starting GRU training with GPU optimization...")
        logger.info(f"  Mixed precision: FP16")
        logger.info(f"  Batch size: {batch_size}")
        logger.info(f"  Data generators: Enabled")
        
        # Train
        history = model.fit(
            train_dataset,
            validation_data=val_dataset,
            epochs=epochs,
            callbacks=callbacks_list,
            class_weight=class_weight,
            verbose=1
        )
        
        self.model = model
        self.history = history
        
        # Clear GPU memory
        tf.keras.backend.clear_session()
        logger.info("✓ GRU training completed, GPU memory cleared")
        
        return model
```

---

## Task 7: Optimize Ensemble Weights ✓

### Update Ensemble Trainer

In `backend/app/ml/training/ensemble_trainer.py`:

```python
def optimize_weights(
    self,
    X_tabular: np.ndarray,
    X_sequence: np.ndarray,
    y_true: np.ndarray,
    returns: np.ndarray,  # Make returns required
    metric: str = 'sharpe_ratio'
) -> Dict[str, float]:
    """
    Optimize ensemble weights using grid search on financial metrics.
    
    Args:
        X_tabular: Tabular features for XGBoost
        X_sequence: Sequence features for GRU
        y_true: True binary labels (0=DOWN, 1=UP)
        returns: Actual next-day returns for financial metrics
        metric: Optimization metric ('sharpe_ratio', 'sortino_ratio', 'accuracy')
        
    Returns:
        Optimized weights dict
    """
    logger.info(f"Optimizing ensemble weights using {metric}...")
    
    # Get base predictions
    _, xgb_proba = self._predict_xgboost(X_tabular)
    _, gru_proba = self._predict_gru(X_sequence)
    
    best_score = -np.inf
    best_weights = None
    
    # Grid search over weight combinations
    weight_range = np.arange(0.3, 0.8, 0.05)
    
    for w_xgb in weight_range:
        w_gru = 1 - w_xgb
        
        # Ensemble probabilities
        ensemble_proba = w_xgb * xgb_proba + w_gru * gru_proba
        predictions = ensemble_proba.argmax(axis=1)
        
        # Calculate metric
        if metric == 'sharpe_ratio':
            fin_metrics = calculate_financial_metrics(predictions, returns)
            score = fin_metrics['sharpe_ratio']
        elif metric == 'sortino_ratio':
            fin_metrics = calculate_financial_metrics(predictions, returns)
            score = fin_metrics['sortino_ratio']
        elif metric == 'accuracy':
            score = (predictions == y_true).mean()
        else:
            raise ValueError(f"Unknown metric: {metric}")
        
        if score > best_score:
            best_score = score
            best_weights = {'xgboost': float(w_xgb), 'gru': float(w_gru)}
    
    self.optimized_weights = best_weights
    self.weights = best_weights
    
    logger.info(f"✓ Optimization complete")
    logger.info(f"  Best weights: XGBoost={best_weights['xgboost']:.3f}, "
               f"GRU={best_weights['gru']:.3f}")
    logger.info(f"  Best {metric}: {best_score:.4f}")
    
    return best_weights
```

---

## Task 8: Comprehensive Logging & Monitoring ✓

### Add to All Training Methods

```python
import psutil
from tqdm import tqdm
import time

def _get_memory_usage(self) -> Dict[str, float]:
    """Get current memory usage"""
    process = psutil.Process()
    mem_info = process.memory_info()
    
    return {
        'rss_gb': mem_info.rss / 1024**3,
        'vms_gb': mem_info.vms / 1024**3,
        'percent': process.memory_percent()
    }

def _log_progress(self, current: int, total: int, prefix: str = "Progress"):
    """Log progress with memory usage"""
    pct = (current / total) * 100
    mem = self._get_memory_usage()
    
    logger.info(f"{prefix}: {current}/{total} ({pct:.1f}%) | "
               f"Memory: {mem['rss_gb']:.2f} GB ({mem['percent']:.1f}%)")

# Use in training loops
for i, symbol in enumerate(tqdm(self.symbols, desc="Training models")):
    try:
        # Training logic
        self._log_progress(i+1, len(self.symbols), "Symbol processing")
        
    except Exception as e:
        logger.error(f"Failed for {symbol}: {e}", exc_info=True)
        # Save checkpoint
        self._save_checkpoint(i, symbol)
        continue

def _save_checkpoint(self, iteration: int, symbol: str):
    """Save training checkpoint"""
    checkpoint_path = self.output_dir / f"checkpoint_{iteration}_{symbol}.pkl"
    
    state = {
        'iteration': iteration,
        'symbol': symbol,
        'symbols_completed': self.symbols[:iteration],
        'timestamp': datetime.now().isoformat()
    }
    
    import pickle
    with open(checkpoint_path, 'wb') as f:
        pickle.dump(state, f)
    
    logger.info(f"✓ Checkpoint saved: {checkpoint_path}")
```

---

## Summary of Changes

### Files Modified:
1. ✅ `backend/app/ml/features/target_generator.py` - Binary + ATR
2. ✅ `backend/app/ml/training/feature_pipeline.py` - Real DB queries
3. ⏳ `backend/scripts/production_training_orchestrator.py` - Walk-forward integration
4. ⏳ `backend/app/ml/training/xgboost_trainer.py` - Class weights
5. ⏳ `backend/app/ml/training/gru_trainer.py` - Class weights + GPU
6. ⏳ `backend/app/ml/training/evaluator.py` - Financial metrics
7. ⏳ `backend/app/ml/training/ensemble_trainer.py` - Weight optimization

### Key Architecture Changes:
- **Binary classification:** 0=DOWN, 1=UP (not -1/0/1)
- **XGBoost:** `objective='binary:logistic'`, `num_class=2`
- **GRU:** 2 output neurons, FP16 mixed precision
- **Walk-forward:** Actually iterate over splits for training
- **Class weights:** Passed as sample weights to both models
- **Financial metrics:** Calculate from actual returns, not zeros
- **GPU:** FP16, data generators, memory growth
- **Ensemble:** Grid search on Sharpe ratio, not hardcoded

---

## Next Step

Implement all changes systematically, test on small subset, then run full training on 2,551 instruments.
