"""
Simplified ML Prediction API Endpoint
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, Optional

router = APIRouter()


class PredictionRequest(BaseModel):
    model_id: str
    features: Dict[str, Any]
    symbol: Optional[str] = None


class PredictionResponse(BaseModel):
    prediction: int
    confidence: float
    model_id: str
    model_version: str
    message: str


@router.post("/predict", response_model=PredictionResponse)
async def predict_single(request: PredictionRequest):
    """
    ML prediction endpoint - returns 'no predictions available' message
    """
    return PredictionResponse(
        prediction=0,
        confidence=0.0,
        model_id=request.model_id,
        model_version="1.0.0",
        message="No predictions available at this time. Model training required."
    )
