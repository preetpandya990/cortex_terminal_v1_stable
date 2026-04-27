"""
API dependencies for FastAPI endpoints.
"""
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db as get_db_session
from app.core.security import get_current_user_id


# Re-export for convenience
get_db = get_db_session


async def get_current_user(user_id: Annotated[str, Depends(get_current_user_id)]) -> dict:
    """
    Get current authenticated user.
    
    Args:
        user_id: User ID from JWT token
        
    Returns:
        User dictionary with user_id
    """
    return {"user_id": user_id}


async def get_ml_predictor(request: Request):
    """
    Get ML predictor from application state.
    
    Raises:
        HTTPException: 503 if models not loaded
        
    Returns:
        EnsemblePredictor instance
    """
    predictor = getattr(request.app.state, "ml_predictor", None)
    
    if predictor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ML models not available. Service is starting up or models failed to load.",
        )
    
    return predictor


async def get_ml_ensemble(request: Request):
    """
    Get ML ensemble metadata from application state.
    
    Raises:
        HTTPException: 503 if models not loaded
        
    Returns:
        LoadedEnsemble instance
    """
    ensemble = getattr(request.app.state, "ml_ensemble", None)
    
    if ensemble is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ML models not available. Service is starting up or models failed to load.",
        )
    
    return ensemble


# Type aliases for convenience
MLPredictor = Annotated[object, Depends(get_ml_predictor)]
MLEnsemble = Annotated[object, Depends(get_ml_ensemble)]
