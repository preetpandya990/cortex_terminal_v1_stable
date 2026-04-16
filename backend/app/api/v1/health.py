"""
Cortex AI — ML Health Check Endpoints
======================================
Health check endpoints for ML system components with graceful degradation.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis, CacheService, get_cache_service
from app.models.ml_data import MLModelMetadata

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["Health"])


class HealthCheckResult:
    """Health check result container."""
    
    def __init__(self):
        self.status = "healthy"
        self.checks = {}
        self.timestamp = datetime.now(timezone.utc).isoformat()
    
    def add_check(self, name: str, healthy: bool, details: dict[str, Any] | None = None):
        """Add a health check result."""
        self.checks[name] = {
            "status": "healthy" if healthy else "unhealthy",
            "details": details or {}
        }
        if not healthy:
            self.status = "degraded"
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "status": self.status,
            "timestamp": self.timestamp,
            "checks": self.checks
        }


async def check_database_health(db: AsyncSession) -> tuple[bool, dict[str, Any]]:
    """
    Check database connectivity and ML tables.
    
    Requirements: 17.5, 19.4
    """
    try:
        # Test basic connectivity
        await db.execute(text("SELECT 1"))
        
        # Check ml_model_metadata table exists and has data
        stmt = select(MLModelMetadata).where(MLModelMetadata.is_active == True).limit(1)
        result = await db.execute(stmt)
        active_model = result.scalar_one_or_none()
        
        details = {
            "connected": True,
            "active_model_available": active_model is not None,
        }
        
        if active_model:
            details["active_model_id"] = active_model.model_id
            details["active_model_version"] = active_model.model_version
        
        return True, details
        
    except Exception as exc:
        logger.error(f"Database health check failed: {exc}", exc_info=True)
        return False, {
            "connected": False,
            "error": str(exc)
        }


async def check_redis_health(cache: CacheService) -> tuple[bool, dict[str, Any]]:
    """
    Check Redis connectivity and feature cache.
    
    Requirements: 17.5, 19.4
    """
    try:
        redis_client = cache._redis
        
        # Test basic connectivity
        await redis_client.ping()
        
        # Get cache statistics
        stats = await cache.get_cache_stats()
        
        details = {
            "connected": True,
            "total_keys": stats.get("total_keys", 0),
            "hit_rate": stats.get("hit_rate", 0.0),
            "memory_used": stats.get("memory_used", "unknown"),
        }
        
        return True, details
        
    except Exception as exc:
        logger.error(f"Redis health check failed: {exc}", exc_info=True)
        return False, {
            "connected": False,
            "error": str(exc)
        }


async def check_model_health(db: AsyncSession) -> tuple[bool, dict[str, Any]]:
    """
    Check ML model availability and readiness.
    
    Requirements: 17.5, 19.4
    """
    try:
        # Check for active production model
        stmt = (
            select(MLModelMetadata)
            .where(MLModelMetadata.is_active == True)
            .order_by(MLModelMetadata.deployed_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        active_model = result.scalar_one_or_none()
        
        if not active_model:
            return False, {
                "model_loaded": False,
                "error": "No active production model found"
            }
        
        # Check model file exists (basic check)
        import os
        model_exists = False
        if active_model.onnx_path:
            model_exists = os.path.exists(active_model.onnx_path)
        
        details = {
            "model_loaded": True,
            "model_id": active_model.model_id,
            "model_version": active_model.model_version,
            "model_name": active_model.model_name,
            "deployed_at": active_model.deployed_at.isoformat() if active_model.deployed_at else None,
            "model_file_exists": model_exists,
            "onnx_path": active_model.onnx_path,
        }
        
        return model_exists, details
        
    except Exception as exc:
        logger.error(f"Model health check failed: {exc}", exc_info=True)
        return False, {
            "model_loaded": False,
            "error": str(exc)
        }


@router.get(
    "/ml",
    response_model=None,
    status_code=status.HTTP_200_OK,
)
async def ml_health_check(
    db: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
) -> JSONResponse:
    """
    Comprehensive ML system health check.
    
    Checks:
    - Model availability (latest production model loaded)
    - Database connectivity (query ml_models table)
    - Redis connectivity (test feature cache)
    
    Returns:
    - HTTP 200: All systems healthy
    - HTTP 503: One or more critical systems unavailable
    
    Requirements: 17.5, 19.4
    """
    result = HealthCheckResult()
    
    # Check database
    db_healthy, db_details = await check_database_health(db)
    result.add_check("database", db_healthy, db_details)
    
    # Check Redis
    redis_healthy, redis_details = await check_redis_health(cache)
    result.add_check("redis", redis_healthy, redis_details)
    
    # Check model availability
    model_healthy, model_details = await check_model_health(db)
    result.add_check("model", model_healthy, model_details)
    
    # Determine overall status
    response_data = result.to_dict()
    
    # Return 503 if model is unavailable (critical component)
    if not model_healthy:
        logger.warning("ML health check failed: model unavailable")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response_data
        )
    
    # Return 200 if all checks pass or only non-critical components are degraded
    http_status = status.HTTP_200_OK
    if result.status == "degraded":
        # If database or Redis is down but model is available, still return 503
        if not db_healthy or not redis_healthy:
            http_status = status.HTTP_503_SERVICE_UNAVAILABLE
            logger.warning("ML health check degraded: database or Redis unavailable")
    
    return JSONResponse(
        status_code=http_status,
        content=response_data
    )


@router.get(
    "/ml/ready",
    response_model=None,
    status_code=status.HTTP_200_OK,
)
async def ml_readiness_check(
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    ML system readiness check for Kubernetes/load balancer probes.
    
    Returns:
    - HTTP 200: System ready to accept requests
    - HTTP 503: System not ready (model unavailable)
    
    Requirements: 17.5, 19.4
    """
    try:
        # Check if model is available
        model_healthy, model_details = await check_model_health(db)
        
        if not model_healthy:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "ready": False,
                    "reason": "Model unavailable",
                    "details": model_details
                }
            )
        
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "ready": True,
                "model": model_details
            }
        )
        
    except Exception as exc:
        logger.error(f"Readiness check failed: {exc}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "ready": False,
                "reason": str(exc)
            }
        )


@router.get(
    "/ml/live",
    response_model=None,
    status_code=status.HTTP_200_OK,
)
async def ml_liveness_check() -> JSONResponse:
    """
    ML system liveness check for Kubernetes probes.
    
    Simple check that the service is running and responsive.
    
    Returns:
    - HTTP 200: Service is alive
    
    Requirements: 17.5, 19.4
    """
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "alive": True,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    )
