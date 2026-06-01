"""
Example integration of Prometheus metrics into ML prediction system.

This file demonstrates how to integrate the metrics module into:
- Prediction endpoints
- Feature computation
- Model serving
- SHAP computation

Requirements: 17.3, 17.4
"""

from typing import Dict, Any, Optional
import asyncio
from datetime import datetime

# Import metrics
from app.ml.monitoring.metrics import (
    # Context managers
    track_prediction_latency,
    track_active_prediction,
    track_feature_computation,
    track_model_inference,
    track_shap_computation,
    # Recording functions
    record_prediction_request,
    update_model_accuracy,
    update_cache_hit_rate,
    record_feature_error,
    record_model_load_failure,
    set_model_info,
    # Decorator
    track_prediction_metrics,
)


# ============================================================================
# Example 1: Prediction Endpoint Integration
# ============================================================================

@track_prediction_metrics()
async def predict_stock_movement(
    symbol: str,
    timeframe: str,
    historical_data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Predict stock movement with automatic metric tracking.
    
    The decorator automatically tracks:
    - Prediction latency
    - Active predictions count
    - Request counts by symbol, timeframe, and status
    
    Args:
        symbol: Stock symbol (e.g., "AAPL")
        timeframe: Prediction timeframe (e.g., "1h", "1d")
        historical_data: Historical price and volume data
        
    Returns:
        Prediction result with confidence scores
    """
    # The decorator handles all the metric tracking automatically
    # Just implement your prediction logic
    
    # Compute features
    features = await compute_features_with_metrics(historical_data)
    
    # Load and run model
    prediction = await run_model_inference_with_metrics(
        model_name="xgboost_v1",
        features=features
    )
    
    # Compute explanations
    shap_values = await compute_shap_with_metrics(
        model_name="xgboost_v1",
        features=features
    )
    
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "prediction": prediction,
        "shap_values": shap_values,
        "timestamp": datetime.utcnow().isoformat()
    }


# ============================================================================
# Example 2: Feature Computation Integration
# ============================================================================

async def compute_features_with_metrics(
    data: Dict[str, Any],
    feature_set: str = "technical_indicators"
) -> Dict[str, float]:
    """
    Compute features with metric tracking.
    
    Tracks:
    - Feature computation duration
    - Feature errors
    - Cache hit rate
    
    Args:
        data: Input data for feature computation
        feature_set: Name of feature set to compute
        
    Returns:
        Computed features
    """
    with track_feature_computation(feature_set):
        try:
            # Check cache first
            cache_key = f"{feature_set}:{hash(str(data))}"
            cached_features = await check_cache(cache_key)
            
            if cached_features:
                # Update cache metrics
                await update_cache_metrics(hit=True)
                return cached_features
            
            # Cache miss - compute features
            await update_cache_metrics(hit=False)
            
            # Compute different feature types
            features = {}
            
            # Technical indicators
            try:
                features.update(compute_technical_indicators(data))
            except Exception as e:
                record_feature_error("technical_indicators", "computation_error")
                raise
            
            # Volume features
            try:
                features.update(compute_volume_features(data))
            except Exception as e:
                record_feature_error("volume_features", "computation_error")
                raise
            
            # Price action features
            try:
                features.update(compute_price_action_features(data))
            except Exception as e:
                record_feature_error("price_action", "computation_error")
                raise
            
            # Cache the results
            await cache_features(cache_key, features)
            
            return features
            
        except KeyError as e:
            record_feature_error(feature_set, "data_missing")
            raise
        except TimeoutError as e:
            record_feature_error(feature_set, "timeout")
            raise
        except Exception as e:
            record_feature_error(feature_set, "unknown_error")
            raise


# ============================================================================
# Example 3: Model Serving Integration
# ============================================================================

async def run_model_inference_with_metrics(
    model_name: str,
    features: Dict[str, float],
    model_version: str = "latest"
) -> Dict[str, Any]:
    """
    Run model inference with metric tracking.
    
    Tracks:
    - Model inference duration
    - Model load failures
    
    Args:
        model_name: Name of the model to use
        features: Input features
        model_version: Model version to use
        
    Returns:
        Model predictions
    """
    # Load model with error tracking
    try:
        model = await load_model(model_name, model_version)
    except FileNotFoundError:
        record_model_load_failure(model_name, "file_not_found")
        raise
    except ValueError as e:
        record_model_load_failure(model_name, "version_mismatch")
        raise
    except Exception as e:
        record_model_load_failure(model_name, "corrupted")
        raise
    
    # Run inference with timing
    with track_model_inference(model_name, model_version):
        prediction = model.predict(features)
    
    return {
        "prediction": prediction,
        "model_name": model_name,
        "model_version": model_version
    }


# ============================================================================
# Example 4: SHAP Computation Integration
# ============================================================================

async def compute_shap_with_metrics(
    model_name: str,
    features: Dict[str, float]
) -> Dict[str, Any]:
    """
    Compute SHAP values with metric tracking.
    
    Tracks:
    - SHAP computation duration
    
    Args:
        model_name: Name of the model
        features: Input features
        
    Returns:
        SHAP values and explanations
    """
    with track_shap_computation(model_name):
        # Load SHAP explainer
        explainer = await load_shap_explainer(model_name)
        
        # Compute SHAP values
        shap_values = explainer.shap_values(features)
        
        # Format explanations
        explanations = format_shap_explanations(shap_values, features)
    
    return explanations


# ============================================================================
# Example 5: Model Accuracy Update (Daily Job)
# ============================================================================

async def update_model_accuracy_metrics():
    """
    Update model accuracy metrics (run daily).
    
    This should be called by a scheduled job to update accuracy metrics
    after daily model evaluation.
    """
    models = ["xgboost_v1", "lightgbm_v1", "ensemble_v1"]
    
    for model_name in models:
        # Evaluate model on validation set
        metrics = await evaluate_model(model_name)
        
        # Update Prometheus metrics
        update_model_accuracy(model_name, "accuracy", metrics["accuracy"])
        update_model_accuracy(model_name, "f1", metrics["f1_score"])
        update_model_accuracy(model_name, "precision", metrics["precision"])
        update_model_accuracy(model_name, "recall", metrics["recall"])
        
        print(f"Updated accuracy metrics for {model_name}")


# ============================================================================
# Example 6: Cache Metrics Update (Periodic Job)
# ============================================================================

async def update_cache_metrics(hit: bool = None):
    """
    Update cache hit rate metrics.
    
    Can be called per-request (with hit parameter) or periodically
    to compute overall hit rate.
    
    Args:
        hit: Whether this was a cache hit (None for periodic update)
    """
    if hit is not None:
        # Per-request tracking (simplified)
        # In production, use a proper cache statistics tracker
        pass
    else:
        # Periodic update - compute overall hit rate
        cache_stats = await get_cache_statistics()
        
        for cache_type, stats in cache_stats.items():
            total = stats["hits"] + stats["misses"]
            hit_rate = (stats["hits"] / total * 100) if total > 0 else 0
            update_cache_hit_rate(cache_type, hit_rate)


# ============================================================================
# Example 7: Model Info Update (On Deployment)
# ============================================================================

async def register_model_deployment(
    model_name: str,
    version: str,
    framework: str,
    trained_date: str
):
    """
    Register model deployment information.
    
    Call this when deploying a new model version.
    
    Args:
        model_name: Name of the model
        version: Model version
        framework: ML framework used
        trained_date: Date when model was trained
    """
    set_model_info(model_name, version, framework, trained_date)
    print(f"Registered model deployment: {model_name} v{version}")


# ============================================================================
# Example 8: Manual Metric Tracking (Without Decorator)
# ============================================================================

async def predict_with_manual_tracking(
    symbol: str,
    timeframe: str,
    data: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Example of manual metric tracking without using the decorator.
    
    Use this approach when you need more control over metric recording.
    """
    # Track overall prediction latency
    with track_prediction_latency():
        # Track active predictions
        with track_active_prediction():
            try:
                # Your prediction logic here
                result = await make_prediction(symbol, timeframe, data)
                
                # Record successful request
                record_prediction_request(symbol, timeframe, "success")
                
                return result
                
            except TimeoutError:
                # Record timeout
                record_prediction_request(symbol, timeframe, "timeout")
                raise
                
            except Exception as e:
                # Record error
                record_prediction_request(symbol, timeframe, "error")
                raise


# ============================================================================
# Helper Functions (Stubs for demonstration)
# ============================================================================

async def check_cache(key: str) -> Optional[Dict[str, float]]:
    """Check if features are in cache."""
    # Implementation would check Redis or memory cache
    return None


async def cache_features(key: str, features: Dict[str, float]):
    """Cache computed features."""
    # Implementation would store in Redis or memory cache
    pass


def compute_technical_indicators(data: Dict[str, Any]) -> Dict[str, float]:
    """Compute technical indicators."""
    # Implementation would compute RSI, MACD, etc.
    return {}


def compute_volume_features(data: Dict[str, Any]) -> Dict[str, float]:
    """Compute volume-based features."""
    return {}


def compute_price_action_features(data: Dict[str, Any]) -> Dict[str, float]:
    """Compute price action features."""
    return {}


async def load_model(name: str, version: str):
    """Load ML model."""
    # Implementation would load from disk or model registry
    pass


async def load_shap_explainer(model_name: str):
    """Load SHAP explainer for model."""
    pass


def format_shap_explanations(shap_values, features):
    """Format SHAP values into explanations."""
    return {}


async def evaluate_model(model_name: str) -> Dict[str, float]:
    """Evaluate model and return metrics."""
    return {
        "accuracy": 0.85,
        "f1_score": 0.82,
        "precision": 0.88,
        "recall": 0.79
    }


async def get_cache_statistics() -> Dict[str, Dict[str, int]]:
    """Get cache statistics."""
    return {
        "redis": {"hits": 850, "misses": 150},
        "memory": {"hits": 950, "misses": 50}
    }


async def make_prediction(symbol: str, timeframe: str, data: Dict[str, Any]):
    """Make a prediction."""
    return {"prediction": 0.75}


# ============================================================================
# FastAPI Endpoint Example
# ============================================================================

"""
# Add to your FastAPI application:

from fastapi import FastAPI, HTTPException
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi.responses import Response

app = FastAPI()

@app.get("/metrics")
async def metrics_endpoint():
    '''Prometheus metrics endpoint.'''
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )

@app.post("/api/v1/predict")
async def predict_endpoint(
    symbol: str,
    timeframe: str,
    data: Dict[str, Any]
):
    '''Prediction endpoint with automatic metrics.'''
    try:
        result = await predict_stock_movement(symbol, timeframe, data)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
"""
