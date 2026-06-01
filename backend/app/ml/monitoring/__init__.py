"""
ML Monitoring Module

Provides comprehensive monitoring capabilities for the ML prediction system:
- Prometheus metrics for performance tracking
- Drift detection for model monitoring
- Latency tracking for SLA compliance

Requirements: 17.3, 17.4
"""

from app.ml.monitoring.metrics import (
    # Metrics objects
    prediction_latency_seconds,
    prediction_requests_total,
    model_accuracy_score,
    feature_computation_duration_seconds,
    feature_cache_hit_rate,
    feature_errors_total,
    model_inference_duration_seconds,
    shap_computation_duration_seconds,
    model_load_failures_total,
    active_predictions,
    model_info,
    
    # Context managers
    track_prediction_latency,
    track_feature_computation,
    track_model_inference,
    track_shap_computation,
    track_active_prediction,
    
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

__all__ = [
    # Metrics objects
    "prediction_latency_seconds",
    "prediction_requests_total",
    "model_accuracy_score",
    "feature_computation_duration_seconds",
    "feature_cache_hit_rate",
    "feature_errors_total",
    "model_inference_duration_seconds",
    "shap_computation_duration_seconds",
    "model_load_failures_total",
    "active_predictions",
    "model_info",
    
    # Context managers
    "track_prediction_latency",
    "track_feature_computation",
    "track_model_inference",
    "track_shap_computation",
    "track_active_prediction",
    
    # Recording functions
    "record_prediction_request",
    "update_model_accuracy",
    "update_cache_hit_rate",
    "record_feature_error",
    "record_model_load_failure",
    "set_model_info",
    
    # Decorator
    "track_prediction_metrics",
]
