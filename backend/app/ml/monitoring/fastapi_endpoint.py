"""
FastAPI endpoint for exposing Prometheus metrics.

This module provides the /metrics endpoint that Prometheus can scrape.

Requirements: 17.3, 17.4
"""

from fastapi import APIRouter, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry, REGISTRY

router = APIRouter()


@router.get("/metrics")
async def metrics_endpoint():
    """
    Prometheus metrics endpoint.
    
    This endpoint exposes all registered Prometheus metrics in the format
    that Prometheus expects. Prometheus will scrape this endpoint periodically
    (typically every 15-30 seconds).
    
    Returns:
        Response with metrics in Prometheus text format
        
    Example Prometheus configuration:
        ```yaml
        scrape_configs:
          - job_name: 'ml-prediction-system'
            scrape_interval: 15s
            static_configs:
              - targets: ['localhost:8000']
            metrics_path: '/metrics'
        ```
    
    Example curl request:
        ```bash
        curl http://localhost:8000/metrics
        ```
    
    Example output:
        ```
        # HELP ml_prediction_latency_seconds Time taken to generate a prediction
        # TYPE ml_prediction_latency_seconds histogram
        ml_prediction_latency_seconds_bucket{le="0.05"} 45.0
        ml_prediction_latency_seconds_bucket{le="0.1"} 89.0
        ml_prediction_latency_seconds_bucket{le="0.15"} 95.0
        ml_prediction_latency_seconds_bucket{le="0.2"} 98.0
        ml_prediction_latency_seconds_bucket{le="0.25"} 100.0
        ml_prediction_latency_seconds_bucket{le="+Inf"} 100.0
        ml_prediction_latency_seconds_count 100.0
        ml_prediction_latency_seconds_sum 8.234
        
        # HELP ml_prediction_requests_total Total number of prediction requests
        # TYPE ml_prediction_requests_total counter
        ml_prediction_requests_total{symbol="AAPL",timeframe="1h",status="success"} 1523.0
        ml_prediction_requests_total{symbol="AAPL",timeframe="1h",status="error"} 12.0
        ...
        ```
    """
    return Response(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST
    )


@router.get("/metrics/health")
async def metrics_health():
    """
    Health check endpoint for the metrics system.
    
    Returns basic information about the metrics collection status.
    
    Returns:
        dict: Health status information
    """
    try:
        # Try to generate metrics to verify system is working
        metrics_data = generate_latest(REGISTRY)
        
        return {
            "status": "healthy",
            "metrics_count": len(metrics_data.decode().split('\n')),
            "message": "Metrics collection is operational"
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "message": "Metrics collection encountered an error"
        }


# Example of how to integrate into your FastAPI app:
"""
from fastapi import FastAPI
from app.ml.monitoring.fastapi_endpoint import router as metrics_router

app = FastAPI()

# Include the metrics router
app.include_router(metrics_router, tags=["monitoring"])

# Or add the endpoint directly:
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from fastapi import Response

@app.get("/metrics")
async def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )
"""
