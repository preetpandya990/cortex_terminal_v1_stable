"""
Cortex AI — Production Health Checks
=====================================
Kubernetes-compatible health check endpoints following industry best practices.

Health Check Types:
- Liveness: Process is alive and responsive (no dependency checks)
- Readiness: Service can handle traffic (checks DB + Redis)
- Detailed: Comprehensive component status for monitoring

Best Practices:
- Liveness probes NEVER check external dependencies
- Readiness probes check critical dependencies only
- Fast response times (<100ms for liveness, <500ms for readiness)
- Proper HTTP status codes (200 = healthy, 503 = unhealthy)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import engine
from app.core.redis import get_redis

logger = logging.getLogger(__name__)


class HealthStatus:
    """Health check status container."""
    
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    
    def __init__(self):
        self.status = self.HEALTHY
        self.checks: dict[str, dict[str, Any]] = {}
        self.timestamp = datetime.now(timezone.utc).isoformat()
    
    def add_check(self, name: str, healthy: bool, details: dict[str, Any] | None = None, critical: bool = True):
        """
        Add a health check result.
        
        Args:
            name: Check name
            healthy: Whether check passed
            details: Additional details
            critical: If False, failure only marks as degraded, not unhealthy
        """
        self.checks[name] = {
            "status": self.HEALTHY if healthy else self.UNHEALTHY,
            "details": details or {},
            "critical": critical
        }
        
        if not healthy:
            if critical:
                self.status = self.UNHEALTHY
            elif self.status == self.HEALTHY:
                self.status = self.DEGRADED
    
    def is_healthy(self) -> bool:
        """Check if overall status is healthy."""
        return self.status == self.HEALTHY
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "status": self.status,
            "timestamp": self.timestamp,
            "checks": self.checks
        }


async def check_database(timeout: float = 2.0) -> tuple[bool, dict[str, Any]]:
    """
    Check database connectivity.
    
    Args:
        timeout: Query timeout in seconds
    
    Returns:
        (healthy, details) tuple
    """
    try:
        async with asyncio.timeout(timeout):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        
        return True, {"connected": True, "response_time_ms": "<2000"}
        
    except asyncio.TimeoutError:
        logger.warning("Database health check timed out")
        return False, {"connected": False, "error": "timeout"}
    except Exception as exc:
        logger.error(f"Database health check failed: {exc}")
        return False, {"connected": False, "error": str(exc)}


async def check_redis(timeout: float = 2.0) -> tuple[bool, dict[str, Any]]:
    """
    Check Redis connectivity.
    
    Args:
        timeout: Ping timeout in seconds
    
    Returns:
        (healthy, details) tuple
    """
    try:
        redis_client = get_redis()
        
        async with asyncio.timeout(timeout):
            await redis_client.ping()
        
        return True, {"connected": True, "response_time_ms": "<2000"}
        
    except asyncio.TimeoutError:
        logger.warning("Redis health check timed out")
        return False, {"connected": False, "error": "timeout"}
    except RuntimeError as exc:
        # Redis not initialized
        logger.error(f"Redis not initialized: {exc}")
        return False, {"connected": False, "error": "not_initialized"}
    except Exception as exc:
        logger.error(f"Redis health check failed: {exc}")
        return False, {"connected": False, "error": str(exc)}


async def liveness_check() -> dict[str, Any]:
    """
    Liveness probe for Kubernetes.
    
    Checks if the process is alive and responsive. Does NOT check external dependencies.
    
    Use Case:
    - Kubernetes liveness probe
    - Detects deadlocks, infinite loops, or process crashes
    - Failure triggers pod restart
    
    Returns:
        Always returns healthy unless process is broken
    """
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


async def readiness_check() -> tuple[bool, dict[str, Any]]:
    """
    Readiness probe for Kubernetes.
    
    Checks if the service can handle traffic by verifying critical dependencies.
    
    Use Case:
    - Kubernetes readiness probe
    - Load balancer health checks
    - Failure removes pod from service endpoints
    
    Returns:
        (is_ready, status_dict) tuple
    """
    result = HealthStatus()
    
    # Check database (critical)
    db_healthy, db_details = await check_database(timeout=2.0)
    result.add_check("database", db_healthy, db_details, critical=True)
    
    # Check Redis (critical)
    redis_healthy, redis_details = await check_redis(timeout=2.0)
    result.add_check("redis", redis_healthy, redis_details, critical=True)
    
    return result.is_healthy(), result.to_dict()


async def detailed_check() -> dict[str, Any]:
    """
    Detailed health check for monitoring and debugging.
    
    Provides comprehensive status of all components including non-critical ones.
    
    Use Case:
    - Monitoring dashboards
    - Debugging and troubleshooting
    - Operational visibility
    
    Returns:
        Detailed status dictionary
    """
    result = HealthStatus()
    
    # Check database (critical)
    db_healthy, db_details = await check_database(timeout=2.0)
    result.add_check("database", db_healthy, db_details, critical=True)
    
    # Check Redis (critical)
    redis_healthy, redis_details = await check_redis(timeout=2.0)
    result.add_check("redis", redis_healthy, redis_details, critical=True)
    
    # Add system info
    import sys
    import platform
    
    result.checks["system"] = {
        "status": "healthy",
        "details": {
            "python_version": sys.version.split()[0],
            "platform": platform.system(),
            "process_id": None  # Don't expose PID for security
        },
        "critical": False
    }
    
    # Add application info
    from app.core.config import get_settings
    settings = get_settings()
    
    result.checks["application"] = {
        "status": "healthy",
        "details": {
            "name": settings.APP_NAME,
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT
        },
        "critical": False
    }
    
    return result.to_dict()
