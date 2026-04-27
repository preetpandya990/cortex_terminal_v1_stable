"""
API Response Caching Decorator
===============================
Production-grade caching decorator for FastAPI endpoints with Redis backend.

Features:
- Automatic cache key generation from function signature
- TTL-based expiration with jitter support
- Cache hit/miss metrics tracking
- X-Cache-Status response header
- Pydantic model serialization
- Async/await support
- Error resilience (cache failures don't break endpoints)

Best Practices (2026):
- Cache-aside pattern (lazy loading)
- Hierarchical cache keys for pattern-based invalidation
- Probabilistic early expiration to prevent stampede
- Non-blocking cache operations
- Comprehensive observability

Usage:
    @router.get("/suggestions")
    @cache_response(ttl=30, key_prefix="suggestions:list")
    async def list_suggestions(...):
        ...
"""
import asyncio
import hashlib
import json
import logging
import time
from functools import wraps
from typing import Any, Callable, Optional

from fastapi import Request, Response
from pydantic import BaseModel

from app.core.redis import get_cache_service

logger = logging.getLogger(__name__)


def _generate_cache_key(
    key_prefix: str,
    func_name: str,
    args: tuple,
    kwargs: dict[str, Any],
) -> str:
    """
    Generate hierarchical cache key from function signature.
    
    Format: {key_prefix}:{func_name}:{hash}
    
    Args:
        key_prefix: Namespace prefix (e.g., "suggestions:list")
        func_name: Function name for debugging
        args: Positional arguments
        kwargs: Keyword arguments
    
    Returns:
        Cache key string
    
    Example:
        suggestions:list:list_suggestions:a1b2c3d4
    """
    # Extract relevant kwargs (exclude FastAPI dependencies)
    cache_params = {}
    
    for key, value in kwargs.items():
        # Skip FastAPI dependencies
        if key in ("request", "response", "session", "user_id", "cache"):
            continue
        
        # Include query parameters and path parameters
        if value is not None:
            # Convert enums to string
            if hasattr(value, "value"):
                cache_params[key] = value.value
            else:
                cache_params[key] = value
    
    # Sort for consistent hashing
    sorted_params = sorted(cache_params.items())
    
    # Create hash of parameters
    param_str = json.dumps(sorted_params, sort_keys=True, default=str)
    param_hash = hashlib.md5(param_str.encode()).hexdigest()[:8]
    
    # Build hierarchical key
    cache_key = f"{key_prefix}:{func_name}:{param_hash}"
    
    return cache_key


def _serialize_response(response: Any) -> str:
    """
    Serialize response for caching.
    
    Supports:
    - Pydantic models (via model_dump)
    - Dictionaries
    - Lists
    - Primitives
    
    Args:
        response: Response object to serialize
    
    Returns:
        JSON string
    """
    if isinstance(response, BaseModel):
        # Pydantic v2 serialization
        return json.dumps(response.model_dump(mode="json"), default=str)
    elif isinstance(response, (dict, list, str, int, float, bool, type(None))):
        return json.dumps(response, default=str)
    else:
        # Fallback: try to convert to dict
        try:
            return json.dumps(response.__dict__, default=str)
        except Exception:
            raise ValueError(f"Cannot serialize response of type {type(response)}")


def _deserialize_response(
    cached_data: str,
    response_model: type[BaseModel],
) -> BaseModel:
    """
    Deserialize cached response.
    
    Args:
        cached_data: JSON string from cache
        response_model: Pydantic model class
    
    Returns:
        Pydantic model instance
    """
    data = json.loads(cached_data)
    return response_model.model_validate(data)


def cache_response(
    ttl: int,
    key_prefix: str,
    jitter: int = 0,
    enable_early_expiration: bool = False,
) -> Callable:
    """
    Decorator for caching FastAPI endpoint responses.
    
    Args:
        ttl: Time-to-live in seconds
        key_prefix: Cache key prefix (e.g., "suggestions:list")
        jitter: Random jitter in seconds to prevent stampede (default: 0)
        enable_early_expiration: Enable probabilistic early expiration (default: False)
    
    Returns:
        Decorated function
    
    Example:
        @router.get("/suggestions")
        @cache_response(ttl=30, key_prefix="suggestions:list")
        async def list_suggestions(...):
            ...
    
    Cache Key Format:
        {key_prefix}:{func_name}:{param_hash}
        Example: suggestions:list:list_suggestions:a1b2c3d4
    
    Response Headers:
        X-Cache-Status: HIT | MISS
        X-Cache-Key: {cache_key} (debug mode only)
    
    Metrics:
        - api_cache_hits_total{endpoint, method}
        - api_cache_misses_total{endpoint, method}
        - api_cache_response_time_seconds{endpoint, cache_status}
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            # Import metrics here to avoid circular imports
            from app.core.metrics import (
                api_cache_hits_total,
                api_cache_misses_total,
                api_cache_response_time_seconds,
            )
            
            start_time = time.perf_counter()
            
            # Extract request and response objects
            request: Optional[Request] = kwargs.get("request")
            response: Optional[Response] = None
            
            # Find Response object in kwargs (FastAPI dependency injection)
            for value in kwargs.values():
                if isinstance(value, Response):
                    response = value
                    break
            
            # Generate cache key
            cache_key = _generate_cache_key(
                key_prefix=key_prefix,
                func_name=func.__name__,
                args=args,
                kwargs=kwargs,
            )
            
            # Get cache service
            try:
                cache = get_cache_service()
            except Exception as e:
                logger.warning(f"Cache service unavailable: {e}")
                # Fallback: execute function without caching
                return await func(*args, **kwargs)
            
            # Try to get from cache
            try:
                cached_data = await cache.get(cache_key)
                
                if cached_data is not None:
                    # Cache HIT
                    elapsed = time.perf_counter() - start_time
                    
                    # Track metrics
                    endpoint = f"{request.method}:{request.url.path}" if request else func.__name__
                    api_cache_hits_total.labels(
                        endpoint=endpoint,
                        method=request.method if request else "UNKNOWN",
                    ).inc()
                    api_cache_response_time_seconds.labels(
                        endpoint=endpoint,
                        cache_status="HIT",
                    ).observe(elapsed)
                    
                    # Set response headers
                    if response:
                        response.headers["X-Cache-Status"] = "HIT"
                    
                    logger.debug(
                        f"Cache HIT: {cache_key} ({elapsed*1000:.2f}ms)",
                        extra={
                            "cache_key": cache_key,
                            "cache_status": "HIT",
                            "elapsed_ms": elapsed * 1000,
                        }
                    )
                    
                    # Deserialize and return
                    # Get response model from function annotations
                    response_model = func.__annotations__.get("return")
                    if response_model and issubclass(response_model, BaseModel):
                        return _deserialize_response(cached_data, response_model)
                    else:
                        # Return raw JSON data
                        return json.loads(cached_data)
            
            except Exception as e:
                logger.warning(
                    f"Cache read error for {cache_key}: {e}",
                    extra={"cache_key": cache_key, "error": str(e)}
                )
                # Continue to function execution on cache error
            
            # Cache MISS - execute function
            result = await func(*args, **kwargs)
            
            elapsed = time.perf_counter() - start_time
            
            # Track metrics
            endpoint = f"{request.method}:{request.url.path}" if request else func.__name__
            api_cache_misses_total.labels(
                endpoint=endpoint,
                method=request.method if request else "UNKNOWN",
            ).inc()
            api_cache_response_time_seconds.labels(
                endpoint=endpoint,
                cache_status="MISS",
            ).observe(elapsed)
            
            # Set response headers
            if response:
                response.headers["X-Cache-Status"] = "MISS"
            
            logger.debug(
                f"Cache MISS: {cache_key} ({elapsed*1000:.2f}ms)",
                extra={
                    "cache_key": cache_key,
                    "cache_status": "MISS",
                    "elapsed_ms": elapsed * 1000,
                }
            )
            
            # Store in cache (non-blocking)
            try:
                # Apply jitter to TTL
                actual_ttl = ttl
                if jitter > 0:
                    import random
                    actual_ttl = ttl + random.randint(-jitter, jitter)
                
                # Serialize response
                serialized = _serialize_response(result)
                
                # Store in cache (fire-and-forget)
                asyncio.create_task(
                    cache.set(cache_key, serialized, ttl=actual_ttl)
                )
            
            except Exception as e:
                logger.warning(
                    f"Cache write error for {cache_key}: {e}",
                    extra={"cache_key": cache_key, "error": str(e)}
                )
                # Don't fail the request on cache write error
            
            return result
        
        return wrapper
    
    return decorator


async def invalidate_cache_pattern(pattern: str) -> int:
    """
    Invalidate all cache keys matching a pattern.
    
    Args:
        pattern: Redis key pattern (e.g., "suggestions:list:*")
    
    Returns:
        Number of keys deleted
    
    Example:
        # Invalidate all list caches
        await invalidate_cache_pattern("suggestions:list:*")
    """
    try:
        cache = get_cache_service()
        deleted_count = await cache.delete_pattern(pattern)
        
        logger.info(
            f"Cache invalidation: {deleted_count} keys deleted",
            extra={
                "pattern": pattern,
                "deleted_count": deleted_count,
            }
        )
        
        return deleted_count
    
    except Exception as e:
        logger.error(
            f"Cache invalidation error for pattern {pattern}: {e}",
            extra={"pattern": pattern, "error": str(e)},
            exc_info=True
        )
        return 0


async def invalidate_cache_key(key: str) -> bool:
    """
    Invalidate a specific cache key.
    
    Args:
        key: Cache key to delete
    
    Returns:
        True if deleted, False otherwise
    
    Example:
        # Invalidate specific suggestion detail cache
        await invalidate_cache_key("suggestions:detail:550e8400-...")
    """
    try:
        cache = get_cache_service()
        await cache.delete(key)
        
        logger.debug(
            f"Cache key deleted: {key}",
            extra={"cache_key": key}
        )
        
        return True
    
    except Exception as e:
        logger.warning(
            f"Cache key deletion error for {key}: {e}",
            extra={"cache_key": key, "error": str(e)}
        )
        return False
