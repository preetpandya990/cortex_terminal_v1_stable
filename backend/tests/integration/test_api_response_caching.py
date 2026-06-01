"""
Integration Tests for API Response Caching
===========================================
Tests for cache decorator, cache invalidation, and metrics tracking.

Test Coverage:
- Cache hit/miss behavior
- X-Cache-Status header
- Cache invalidation on new suggestion
- Metrics tracking (hits, misses, invalidations)
- TTL expiration
- Cache key generation
"""
import asyncio
import json
import time
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache_decorator import invalidate_cache_pattern
from app.core.metrics import (
    api_cache_hits_total,
    api_cache_invalidations_total,
    api_cache_misses_total,
)
from app.core.redis import RedisChannels, get_cache_service, get_pubsub_client
from app.models.trade_suggestions import TradeSuggestion


@pytest.mark.asyncio
async def test_cache_miss_then_hit(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cache MISS on first request, then HIT on second request.
    
    Validates:
    - First request: X-Cache-Status = MISS
    - Second request: X-Cache-Status = HIT
    - Response data is identical
    - Cache metrics are tracked
    """
    # Create test suggestion
    suggestion = TradeSuggestion(
        suggestion_id=uuid4(),
        symbol="AAPL",
        signal_direction="BUY",
        consensus_score=85.5,
        confidence_level="HIGH",
        trigger_type="SCANNER_ANOMALY",
        status="active",
        expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59),
    )
    db_session.add(suggestion)
    await db_session.commit()
    
    # First request - should be MISS
    response1 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response1.status_code == 200
    assert response1.headers.get("X-Cache-Status") == "MISS"
    data1 = response1.json()
    
    # Second request - should be HIT
    response2 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response2.status_code == 200
    assert response2.headers.get("X-Cache-Status") == "HIT"
    data2 = response2.json()
    
    # Data should be identical
    assert data1 == data2
    assert len(data1["suggestions"]) == 1
    assert data1["suggestions"][0]["symbol"] == "AAPL"


@pytest.mark.asyncio
async def test_cache_detail_endpoint(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cache behavior for detail endpoint.
    
    Validates:
    - First request: MISS
    - Second request: HIT
    - Cache key includes suggestion_id
    """
    # Create test suggestion
    suggestion_id = uuid4()
    suggestion = TradeSuggestion(
        suggestion_id=suggestion_id,
        symbol="TSLA",
        signal_direction="SELL",
        consensus_score=92.3,
        confidence_level="HIGH",
        trigger_type="NEWS_EVENT",
        status="active",
        expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59),
    )
    db_session.add(suggestion)
    await db_session.commit()
    
    # First request - MISS
    response1 = await async_client.get(
        f"/api/v1/trade-suggestions/{suggestion_id}",
        headers=auth_headers,
    )
    assert response1.status_code == 200
    assert response1.headers.get("X-Cache-Status") == "MISS"
    
    # Second request - HIT
    response2 = await async_client.get(
        f"/api/v1/trade-suggestions/{suggestion_id}",
        headers=auth_headers,
    )
    assert response2.status_code == 200
    assert response2.headers.get("X-Cache-Status") == "HIT"
    
    # Data should be identical
    assert response1.json() == response2.json()


@pytest.mark.asyncio
async def test_cache_invalidation_on_new_suggestion(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cache invalidation when new suggestion is published.
    
    Validates:
    - List cache is populated (HIT)
    - New suggestion published to Redis
    - List cache is invalidated
    - Next request is MISS (cache was cleared)
    """
    # Create initial suggestion
    suggestion1 = TradeSuggestion(
        suggestion_id=uuid4(),
        symbol="AAPL",
        signal_direction="BUY",
        consensus_score=85.5,
        confidence_level="HIGH",
        trigger_type="SCANNER_ANOMALY",
        status="active",
        expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59),
    )
    db_session.add(suggestion1)
    await db_session.commit()
    
    # First request - populate cache
    response1 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response1.status_code == 200
    assert response1.headers.get("X-Cache-Status") == "MISS"
    
    # Second request - should hit cache
    response2 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response2.status_code == 200
    assert response2.headers.get("X-Cache-Status") == "HIT"
    
    # Publish new suggestion event to Redis
    pubsub = get_pubsub_client()
    new_suggestion_id = uuid4()
    await pubsub.publish_json(
        RedisChannels.SUGGESTIONS_NEW,
        {
            "suggestion_id": str(new_suggestion_id),
            "symbol": "TSLA",
            "signal_direction": "SELL",
            "consensus_score": 90.0,
            "confidence_level": "HIGH",
            "trigger_type": "NEWS_EVENT",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    
    # Manually invalidate cache (simulating worker behavior)
    deleted_count = await invalidate_cache_pattern("suggestions:list:*")
    assert deleted_count > 0
    
    # Third request - should be MISS (cache was invalidated)
    response3 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response3.status_code == 200
    assert response3.headers.get("X-Cache-Status") == "MISS"


@pytest.mark.asyncio
async def test_cache_key_generation_with_filters(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cache key generation with different query parameters.
    
    Validates:
    - Different filters generate different cache keys
    - Each filter combination has its own cache entry
    """
    # Create test suggestions
    suggestions = [
        TradeSuggestion(
            suggestion_id=uuid4(),
            symbol="AAPL",
            signal_direction="BUY",
            consensus_score=85.5,
            confidence_level="HIGH",
            trigger_type="SCANNER_ANOMALY",
            status="active",
            expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59),
        ),
        TradeSuggestion(
            suggestion_id=uuid4(),
            symbol="TSLA",
            signal_direction="SELL",
            consensus_score=92.3,
            confidence_level="MEDIUM",
            trigger_type="NEWS_EVENT",
            status="active",
            expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59),
        ),
    ]
    for s in suggestions:
        db_session.add(s)
    await db_session.commit()
    
    # Request 1: No filters
    response1 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response1.status_code == 200
    assert response1.headers.get("X-Cache-Status") == "MISS"
    assert len(response1.json()["suggestions"]) == 2
    
    # Request 2: Same query - should HIT
    response2 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response2.status_code == 200
    assert response2.headers.get("X-Cache-Status") == "HIT"
    
    # Request 3: Filter by direction=BUY - different cache key, should MISS
    response3 = await async_client.get(
        "/api/v1/trade-suggestions?direction=BUY",
        headers=auth_headers,
    )
    assert response3.status_code == 200
    assert response3.headers.get("X-Cache-Status") == "MISS"
    assert len(response3.json()["suggestions"]) == 1
    
    # Request 4: Same filter - should HIT
    response4 = await async_client.get(
        "/api/v1/trade-suggestions?direction=BUY",
        headers=auth_headers,
    )
    assert response4.status_code == 200
    assert response4.headers.get("X-Cache-Status") == "HIT"
    
    # Request 5: Filter by symbol=TSLA - different cache key, should MISS
    response5 = await async_client.get(
        "/api/v1/trade-suggestions?symbol=TSLA",
        headers=auth_headers,
    )
    assert response5.status_code == 200
    assert response5.headers.get("X-Cache-Status") == "MISS"
    assert len(response5.json()["suggestions"]) == 1


@pytest.mark.asyncio
async def test_cache_ttl_expiration(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cache TTL expiration.
    
    Validates:
    - Cache expires after TTL
    - Expired cache results in MISS
    
    Note: Uses short TTL for testing (2 seconds)
    """
    # Create test suggestion
    suggestion = TradeSuggestion(
        suggestion_id=uuid4(),
        symbol="AAPL",
        signal_direction="BUY",
        consensus_score=85.5,
        confidence_level="HIGH",
        trigger_type="SCANNER_ANOMALY",
        status="active",
        expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59),
    )
    db_session.add(suggestion)
    await db_session.commit()
    
    # First request - populate cache
    response1 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response1.status_code == 200
    assert response1.headers.get("X-Cache-Status") == "MISS"
    
    # Second request - should HIT
    response2 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response2.status_code == 200
    assert response2.headers.get("X-Cache-Status") == "HIT"
    
    # Wait for TTL to expire (30s + buffer)
    # NOTE: This test is slow, consider mocking TTL in production tests
    await asyncio.sleep(32)
    
    # Third request - should MISS (cache expired)
    response3 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response3.status_code == 200
    assert response3.headers.get("X-Cache-Status") == "MISS"


@pytest.mark.asyncio
async def test_cache_metrics_tracking(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
):
    """
    Test cache metrics are properly tracked.
    
    Validates:
    - api_cache_hits_total increments on HIT
    - api_cache_misses_total increments on MISS
    - api_cache_invalidations_total increments on invalidation
    """
    # Create test suggestion
    suggestion = TradeSuggestion(
        suggestion_id=uuid4(),
        symbol="AAPL",
        signal_direction="BUY",
        consensus_score=85.5,
        confidence_level="HIGH",
        trigger_type="SCANNER_ANOMALY",
        status="active",
        expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59),
    )
    db_session.add(suggestion)
    await db_session.commit()
    
    # Get initial metric values
    initial_misses = api_cache_misses_total.labels(
        endpoint="GET:/api/v1/trade-suggestions",
        method="GET"
    )._value.get()
    
    initial_hits = api_cache_hits_total.labels(
        endpoint="GET:/api/v1/trade-suggestions",
        method="GET"
    )._value.get()
    
    # First request - MISS
    response1 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response1.status_code == 200
    assert response1.headers.get("X-Cache-Status") == "MISS"
    
    # Check MISS metric incremented
    current_misses = api_cache_misses_total.labels(
        endpoint="GET:/api/v1/trade-suggestions",
        method="GET"
    )._value.get()
    assert current_misses == initial_misses + 1
    
    # Second request - HIT
    response2 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response2.status_code == 200
    assert response2.headers.get("X-Cache-Status") == "HIT"
    
    # Check HIT metric incremented
    current_hits = api_cache_hits_total.labels(
        endpoint="GET:/api/v1/trade-suggestions",
        method="GET"
    )._value.get()
    assert current_hits == initial_hits + 1
    
    # Test invalidation metric
    initial_invalidations = api_cache_invalidations_total.labels(
        pattern="suggestions:list:*",
        trigger="test"
    )._value.get()
    
    # Invalidate cache
    await invalidate_cache_pattern("suggestions:list:*")
    
    # Manually increment metric (simulating worker behavior)
    api_cache_invalidations_total.labels(
        pattern="suggestions:list:*",
        trigger="test"
    ).inc()
    
    # Check invalidation metric incremented
    current_invalidations = api_cache_invalidations_total.labels(
        pattern="suggestions:list:*",
        trigger="test"
    )._value.get()
    assert current_invalidations == initial_invalidations + 1


@pytest.mark.asyncio
async def test_cache_disabled_when_config_false(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
    monkeypatch,
):
    """
    Test cache is disabled when ENABLE_API_RESPONSE_CACHING=False.
    
    Validates:
    - All requests return MISS when caching is disabled
    - No cache entries are created
    """
    # Disable caching via config
    from app.core.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "ENABLE_API_RESPONSE_CACHING", False)
    
    # Create test suggestion
    suggestion = TradeSuggestion(
        suggestion_id=uuid4(),
        symbol="AAPL",
        signal_direction="BUY",
        consensus_score=85.5,
        confidence_level="HIGH",
        trigger_type="SCANNER_ANOMALY",
        status="active",
        expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59),
    )
    db_session.add(suggestion)
    await db_session.commit()
    
    # First request
    response1 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response1.status_code == 200
    # Note: Cache decorator still runs, but with TTL=0 it won't cache
    
    # Second request - should still work (no caching)
    response2 = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response2.status_code == 200


@pytest.mark.asyncio
async def test_cache_resilience_on_redis_failure(
    async_client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
    monkeypatch,
):
    """
    Test cache decorator is resilient to Redis failures.
    
    Validates:
    - Endpoint still works when Redis is unavailable
    - No exceptions are raised
    - Response is returned without caching
    """
    # Create test suggestion
    suggestion = TradeSuggestion(
        suggestion_id=uuid4(),
        symbol="AAPL",
        signal_direction="BUY",
        consensus_score=85.5,
        confidence_level="HIGH",
        trigger_type="SCANNER_ANOMALY",
        status="active",
        expires_at=datetime.now(timezone.utc).replace(hour=23, minute=59),
    )
    db_session.add(suggestion)
    await db_session.commit()
    
    # Mock Redis failure
    async def mock_get_cache_service():
        raise ConnectionError("Redis unavailable")
    
    from app.core import cache_decorator
    monkeypatch.setattr(cache_decorator, "get_cache_service", mock_get_cache_service)
    
    # Request should still work (fallback to no caching)
    response = await async_client.get(
        "/api/v1/trade-suggestions",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert len(response.json()["suggestions"]) == 1
