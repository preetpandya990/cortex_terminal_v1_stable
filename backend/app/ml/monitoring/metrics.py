"""
Prometheus metrics for ML prediction system monitoring.

This module provides comprehensive metrics for tracking:
- Prediction latency and request counts
- Feature computation performance and cache efficiency
- Model serving performance including SHAP computation
- Error tracking and failure monitoring

Requirements: 17.3, 17.4
"""

from prometheus_client import Counter, Histogram, Gauge, Info
from typing import Optional
import time
from contextlib import contextmanager
from functools import wraps


# ============================================================================
# Prediction Metrics (Task 25.1)
# ============================================================================

# Prediction latency histogram with specific buckets for SLA monitoring
# Buckets: 50ms, 100ms, 150ms, 200ms, 250ms
prediction_latency_seconds = Histogram(
    'ml_prediction_latency_seconds',
    'Time taken to generate a prediction',
    buckets=(0.05, 0.1, 0.15, 0.2, 0.25, float('inf'))
)

# Prediction request counter with labels for filtering
prediction_requests_total = Counter(
    'ml_prediction_requests_total',
    'Total number of prediction requests',
    ['symbol', 'timeframe', 'status']
)

# Model accuracy gauge (updated daily)
model_accuracy_score = Gauge(
    'ml_model_accuracy_score',
    'Current model accuracy score',
    ['model_name', 'metric_type']
)


# ============================================================================
# Feature Computation Metrics (Task 25.2)
# ============================================================================

# Feature computation duration histogram
feature_computation_duration_seconds = Histogram(
    'ml_feature_computation_duration_seconds',
    'Time taken to compute features',
    ['feature_set'],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, float('inf'))
)

# Feature cache hit rate gauge
feature_cache_hit_rate = Gauge(
    'ml_feature_cache_hit_rate',
    'Percentage of feature requests served from cache',
    ['cache_type']
)

# Feature errors counter
feature_errors_total = Counter(
    'ml_feature_errors_total',
    'Total number of feature computation errors',
    ['feature_name', 'error_type']
)


# ============================================================================
# Model Serving Metrics (Task 25.3)
# ============================================================================

# Model inference duration histogram
model_inference_duration_seconds = Histogram(
    'ml_model_inference_duration_seconds',
    'Time taken for model inference',
    ['model_name', 'model_version'],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, float('inf'))
)

# SHAP computation duration histogram
shap_computation_duration_seconds = Histogram(
    'ml_shap_computation_duration_seconds',
    'Time taken to compute SHAP values',
    ['model_name'],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, float('inf'))
)

# Model load failures counter
model_load_failures_total = Counter(
    'ml_model_load_failures_total',
    'Total number of model loading failures',
    ['model_name', 'failure_reason']
)


# ============================================================================
# Additional Useful Metrics
# ============================================================================

# Model info metric (for tracking deployed models)
model_info = Info(
    'ml_model_info',
    'Information about deployed ML models'
)

# Active predictions gauge
active_predictions = Gauge(
    'ml_active_predictions',
    'Number of predictions currently being processed'
)


# ============================================================================
# D1 — Live Drift Observability
# ============================================================================

ml_model_drift_score = Gauge(
    'ml_model_drift_score',
    'Composite drift score for the live production model (z-score of prediction distribution vs training baseline)',
    ['model_name'],
)

ml_model_drift_flagged = Gauge(
    'ml_model_drift_flagged',
    '1 if a drift advisory flag (challenger_recommended) is currently set for the model, 0 otherwise',
    ['model_name'],
)

ml_model_live_realised_sharpe = Gauge(
    'ml_model_live_realised_sharpe',
    'Realised per-trade information ratio (mean/std of net returns) from closed paper trades since deployment',
    ['model_name'],
)

ml_model_live_directional_accuracy = Gauge(
    'ml_model_live_directional_accuracy',
    'Realised directional accuracy (fraction of correct direction calls) from closed paper trades since deployment',
    ['model_name'],
)

ml_model_live_trade_count = Gauge(
    'ml_model_live_trade_count',
    'Number of closed paper trades available for live drift metrics since deployment',
    ['model_name'],
)


# ============================================================================
# Helper Functions and Context Managers
# ============================================================================

@contextmanager
def track_prediction_latency():
    """
    Context manager to track prediction latency.
    
    Usage:
        with track_prediction_latency():
            result = make_prediction(...)
    """
    start_time = time.time()
    try:
        yield
    finally:
        duration = time.time() - start_time
        prediction_latency_seconds.observe(duration)


@contextmanager
def track_feature_computation(feature_set: str = "default"):
    """
    Context manager to track feature computation duration.
    
    Args:
        feature_set: Name of the feature set being computed
        
    Usage:
        with track_feature_computation("technical_indicators"):
            features = compute_features(...)
    """
    start_time = time.time()
    try:
        yield
    finally:
        duration = time.time() - start_time
        feature_computation_duration_seconds.labels(feature_set=feature_set).observe(duration)


@contextmanager
def track_model_inference(model_name: str, model_version: str = "latest"):
    """
    Context manager to track model inference duration.
    
    Args:
        model_name: Name of the model
        model_version: Version of the model
        
    Usage:
        with track_model_inference("xgboost_v1", "1.0.0"):
            prediction = model.predict(features)
    """
    start_time = time.time()
    try:
        yield
    finally:
        duration = time.time() - start_time
        model_inference_duration_seconds.labels(
            model_name=model_name,
            model_version=model_version
        ).observe(duration)


@contextmanager
def track_shap_computation(model_name: str):
    """
    Context manager to track SHAP computation duration.
    
    Args:
        model_name: Name of the model
        
    Usage:
        with track_shap_computation("xgboost_v1"):
            shap_values = explainer.shap_values(features)
    """
    start_time = time.time()
    try:
        yield
    finally:
        duration = time.time() - start_time
        shap_computation_duration_seconds.labels(model_name=model_name).observe(duration)


@contextmanager
def track_active_prediction():
    """
    Context manager to track active predictions count.
    
    Usage:
        with track_active_prediction():
            result = make_prediction(...)
    """
    active_predictions.inc()
    try:
        yield
    finally:
        active_predictions.dec()


def record_prediction_request(symbol: str, timeframe: str, status: str):
    """
    Record a prediction request.
    
    Args:
        symbol: Stock symbol (e.g., "AAPL", "GOOGL")
        timeframe: Timeframe for prediction (e.g., "1h", "1d")
        status: Request status ("success", "error", "timeout")
        
    Usage:
        record_prediction_request("AAPL", "1h", "success")
    """
    prediction_requests_total.labels(
        symbol=symbol,
        timeframe=timeframe,
        status=status
    ).inc()


def update_model_accuracy(model_name: str, metric_type: str, score: float):
    """
    Update model accuracy gauge.
    
    Args:
        model_name: Name of the model
        metric_type: Type of metric (e.g., "accuracy", "f1", "precision", "recall")
        score: Accuracy score (0.0 to 1.0)
        
    Usage:
        update_model_accuracy("xgboost_v1", "accuracy", 0.85)
    """
    model_accuracy_score.labels(
        model_name=model_name,
        metric_type=metric_type
    ).set(score)


def update_cache_hit_rate(cache_type: str, hit_rate: float):
    """
    Update feature cache hit rate.
    
    Args:
        cache_type: Type of cache (e.g., "redis", "memory")
        hit_rate: Hit rate as percentage (0.0 to 100.0)
        
    Usage:
        update_cache_hit_rate("redis", 85.5)
    """
    feature_cache_hit_rate.labels(cache_type=cache_type).set(hit_rate)


def record_feature_error(feature_name: str, error_type: str):
    """
    Record a feature computation error.
    
    Args:
        feature_name: Name of the feature that failed
        error_type: Type of error (e.g., "computation_error", "data_missing", "timeout")
        
    Usage:
        record_feature_error("rsi_14", "computation_error")
    """
    feature_errors_total.labels(
        feature_name=feature_name,
        error_type=error_type
    ).inc()


def record_model_load_failure(model_name: str, failure_reason: str):
    """
    Record a model loading failure.
    
    Args:
        model_name: Name of the model that failed to load
        failure_reason: Reason for failure (e.g., "file_not_found", "corrupted", "version_mismatch")
        
    Usage:
        record_model_load_failure("xgboost_v1", "file_not_found")
    """
    model_load_failures_total.labels(
        model_name=model_name,
        failure_reason=failure_reason
    ).inc()


def set_model_info(model_name: str, version: str, framework: str, trained_date: str):
    """
    Set model information.
    
    Args:
        model_name: Name of the model
        version: Model version
        framework: ML framework used (e.g., "xgboost", "sklearn", "pytorch")
        trained_date: Date when model was trained (ISO format)
        
    Usage:
        set_model_info("xgboost_v1", "1.0.0", "xgboost", "2024-01-15")
    """
    model_info.info({
        'model_name': model_name,
        'version': version,
        'framework': framework,
        'trained_date': trained_date
    })


# ============================================================================
# Decorator for automatic metric tracking
# ============================================================================

def track_prediction_metrics(symbol: str = None, timeframe: str = None):
    """
    Decorator to automatically track prediction metrics.
    
    Args:
        symbol: Stock symbol (if None, will try to extract from function args)
        timeframe: Timeframe (if None, will try to extract from function args)
        
    Usage:
        @track_prediction_metrics()
        async def predict(symbol: str, timeframe: str):
            ...
    """
    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            # Extract symbol and timeframe from args if not provided
            _symbol = symbol or kwargs.get('symbol', 'unknown')
            _timeframe = timeframe or kwargs.get('timeframe', 'unknown')
            
            with track_prediction_latency():
                with track_active_prediction():
                    try:
                        result = await func(*args, **kwargs)
                        record_prediction_request(_symbol, _timeframe, 'success')
                        return result
                    except Exception as e:
                        record_prediction_request(_symbol, _timeframe, 'error')
                        raise
        
        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            # Extract symbol and timeframe from args if not provided
            _symbol = symbol or kwargs.get('symbol', 'unknown')
            _timeframe = timeframe or kwargs.get('timeframe', 'unknown')
            
            with track_prediction_latency():
                with track_active_prediction():
                    try:
                        result = func(*args, **kwargs)
                        record_prediction_request(_symbol, _timeframe, 'success')
                        return result
                    except Exception as e:
                        record_prediction_request(_symbol, _timeframe, 'error')
                        raise
        
        # Return appropriate wrapper based on whether function is async
        import inspect
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper
    
    return decorator
