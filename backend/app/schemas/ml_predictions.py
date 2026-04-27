"""
Pydantic schemas for ML predictions and drift monitoring.
"""

from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, ConfigDict, Field


class _MLBase(BaseModel):
    model_config = ConfigDict(protected_namespaces=())


class DriftMetricResponse(_MLBase):
    """Response schema for drift metric."""
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    model_id: str
    timestamp: datetime
    drift_detected: bool
    drift_type: Optional[str] = None
    feature_drift_scores: Optional[Dict[str, Any]] = None
    drifted_features: Optional[List[str]] = None
    prediction_drift_score: Optional[float] = None
    prediction_z_score: Optional[float] = None
    prediction_ks_statistic: Optional[float] = None
    prediction_p_value: Optional[float] = None
    current_mean: Optional[float] = None
    current_std: Optional[float] = None
    historical_mean: Optional[float] = None
    historical_std: Optional[float] = None
    alerts: Optional[List[Dict[str, Any]]] = None
    alert_sent: Optional[bool] = None
    alert_sent_at: Optional[datetime] = None
    sample_count: Optional[int] = None
    notes: Optional[str] = None


class DriftMetricListResponse(BaseModel):
    """Response schema for list of drift metrics."""
    
    metrics: List[DriftMetricResponse]
    count: int


class PredictionResponse(_MLBase):
    """Response schema for ML prediction.

    When `available` is False the prediction fields are None and `unavailable_reason`
    describes why (e.g. "insufficient_data"). Callers must check `available` first.
    """
    model_config = ConfigDict(protected_namespaces=())

    symbol: str
    available: bool = True
    unavailable_reason: Optional[str] = None

    direction: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    take_profit_2: Optional[float] = None
    take_profit_3: Optional[float] = None
    volatility: Optional[float] = None
    probabilities: Optional[Dict[str, float]] = None
    model_version: Optional[str] = None
    predicted_at: Optional[datetime] = None


class ModelMetadataResponse(_MLBase):
    """Response schema for model metadata."""
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: int
    model_id: str
    model_name: str
    model_version: str
    trained_at: datetime
    training_samples: Optional[int] = None
    training_features: Optional[List[str]] = None
    training_feature_stats: Optional[Dict[str, Any]] = None
    training_prediction_stats: Optional[Dict[str, Any]] = None
    training_metrics: Optional[Dict[str, Any]] = None
    validation_metrics: Optional[Dict[str, Any]] = None
    model_path: Optional[str] = None
    onnx_path: Optional[str] = None
    is_active: Optional[bool] = None
    deployed_at: Optional[datetime] = None


class DriftDetectionRequest(_MLBase):
    """Request schema for manual drift detection."""

    model_id: str = Field(..., description="Model ID to check for drift")
    drift_threshold: Optional[float] = Field(2.0, ge=0.5, le=5.0, description="Drift threshold in standard deviations")
    lookback_days: Optional[int] = Field(7, ge=1, le=90, description="Days of data to analyze")


class PredictionRequest(_MLBase):
    """Request schema for ML prediction."""

    symbol: str = Field(..., description="NSE instrument key, e.g. NSE_EQ|INE002A01018")
    timeframe: str = Field("1d", description="Candle timeframe: 1d, 1w")


class BatchPredictionRequest(_MLBase):
    """Request schema for batch predictions."""

    model_id: str = Field(..., description="Model ID to use for predictions")
    predictions: List[PredictionRequest] = Field(..., description="List of prediction requests")


class BatchPredictionResponse(BaseModel):
    """Response schema for batch predictions."""

    predictions: List[PredictionResponse]
    count: int
    errors: Optional[List[Dict[str, Any]]] = None


class PredictionError(_MLBase):
    """Error schema for prediction failures."""

    error: str
    detail: Optional[str] = None
    model_id: Optional[str] = None




class TimeframePredictionResponse(BaseModel):
    """Response schema for individual timeframe prediction in ensemble."""

    timeframe: str = Field(..., description="Timeframe (daily, weekly, monthly)")
    direction: str = Field(..., description="Prediction direction (buy, sell, hold)")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score")
    entry_price: Optional[float] = Field(None, description="Entry price")
    tp1: Optional[float] = Field(None, description="Take profit 1")
    tp2: Optional[float] = Field(None, description="Take profit 2")
    tp3: Optional[float] = Field(None, description="Take profit 3")
    stop_loss: Optional[float] = Field(None, description="Stop loss")
    volatility: Optional[float] = Field(None, description="Volatility estimate")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")


class EnsemblePredictionRequest(BaseModel):
    """Request schema for ensemble prediction."""

    symbol: str = Field(..., description="Stock symbol")
    timeframes: List[str] = Field(
        ...,
        description="List of timeframes (daily, weekly, monthly)",
        min_length=1,
        max_length=3
    )
    user_id: Optional[str] = Field(None, description="User ID for audit logging")

    class Config:
        json_schema_extra = {
            "example": {
                "symbol": "AAPL",
                "timeframes": ["daily", "weekly", "monthly"],
                "user_id": "user123"
            }
        }


class EnsemblePredictionResponse(BaseModel):
    """Response schema for ensemble prediction."""

    symbol: str = Field(..., description="Stock symbol")
    direction: str = Field(..., description="Ensemble prediction direction")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Ensemble confidence score")
    timeframe_predictions: Dict[str, TimeframePredictionResponse] = Field(
        ...,
        description="Individual predictions per timeframe"
    )
    confidence_breakdown: Dict[str, float] = Field(
        ...,
        description="Confidence contribution per timeframe"
    )
    conflict_resolved: bool = Field(..., description="Whether conflict resolution was applied")
    conflict_resolution_method: Optional[str] = Field(
        None,
        description="Method used for conflict resolution"
    )
    metadata: Optional[Dict[str, Any]] = Field(None, description="Additional metadata")
    warnings: Optional[List[str]] = Field(None, description="Warning messages")

    class Config:
        json_schema_extra = {
            "example": {
                "symbol": "AAPL",
                "direction": "buy",
                "confidence": 0.85,
                "timeframe_predictions": {
                    "daily": {
                        "timeframe": "daily",
                        "direction": "buy",
                        "confidence": 0.90,
                        "entry_price": 150.0,
                        "tp1": 155.0,
                        "tp2": 160.0,
                        "tp3": 165.0,
                        "stop_loss": 145.0,
                        "volatility": 0.02
                    }
                },
                "confidence_breakdown": {
                    "daily": 0.45,
                    "weekly": 0.27,
                    "monthly": 0.13
                },
                "conflict_resolved": False,
                "conflict_resolution_method": None
            }
        }
