"""
Unit tests for MLRateLimiter with hybrid IP + user_id rate limiting.

Tests:
- Hybrid rate limiting (both IP and user_id)
- Sliding window algorithm
- Violation logging
- Redis integration
- HTTP 429 responses
- Retry-After headers

Requirements: 18.2, 32.1, 32.2
"""
import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from unittest.mock import AsyncMock, MagicMock

from app.ml.rate_limiter import MLRateLimiter


@pytest_asyncio.fixture
async def mock_redis():
    """Mock Redis client for rate limiter tests."""
    redis = AsyncMock()
    redis.zremrangebyscore = AsyncMock()
    redis.zcard = AsyncMock(return_value=0)
    redis.zadd = AsyncMock()
    redis.expire = AsyncMock()
    redis.incr = AsyncMock()
    return redis


@pytest_asyncio.fixture
async def rate_limiter(mock_redis):
    """MLRateLimiter instance with mocked Redis."""
    return MLRateLimiter(mock_redis)


@pytest_asyncio.fixture
def mock_request():
    """Mock FastAPI Request object."""
    request = MagicMock(spec=Request)
    request.client = MagicMock()
    request.client.host = "192.168.1.1"
    request.url = MagicMock()
    request.url.path = "/api/v1/ml/predict"
    return request


# ── Test Hybrid Rate Limiting ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_allows_within_limit(rate_limiter, mock_redis):
    """Test that requests within limit are allowed."""
    # Mock: 5 requests already made (under limit of 10)
    mock_redis.zcard.return_value = 5
    
    # Check rate limit
    allowed, metadata = await rate_limiter.check_rate_limit(
        user_id="user123",
        ip_address="192.168.1.1",
        endpoint="/api/v1/ml/predict",
        limit="10/minute",
    )
    
    assert allowed is True
    assert metadata["remaining"] >= 0


@pytest.mark.asyncio
async def test_rate_limit_blocks_user_over_limit(rate_limiter, mock_redis):
    """Test that user exceeding limit is blocked."""
    # Mock: 10 requests already made (at limit)
    mock_redis.zcard.return_value = 10
    
    # Check rate limit - should raise HTTPException
    with pytest.raises(HTTPException) as exc_info:
        await rate_limiter.check_rate_limit(
            user_id="user123",
            ip_address="192.168.1.1",
            endpoint="/api/v1/ml/predict",
            limit="10/minute",
        )
    
    assert exc_info.value.status_code == 429
    assert "rate_limit_exceeded" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_rate_limit_blocks_ip_over_limit(rate_limiter, mock_redis):
    """Test that IP exceeding limit is blocked."""
    # Mock: User under limit (5), but IP over limit (20)
    async def mock_zcard(key):
        if "user:" in key:
            return 5  # User under limit
        elif "ip:" in key:
            return 20  # IP at limit (2x user limit)
        return 0
    
    mock_redis.zcard.side_effect = mock_zcard
    
    # Check rate limit - should raise HTTPException for IP
    with pytest.raises(HTTPException) as exc_info:
        await rate_limiter.check_rate_limit(
            user_id="user123",
            ip_address="192.168.1.1",
            endpoint="/api/v1/ml/predict",
            limit="10/minute",
        )
    
    assert exc_info.value.status_code == 429
    assert "per IP" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_rate_limit_both_must_pass(rate_limiter, mock_redis):
    """Test that both user and IP limits must pass."""
    # Mock: User at limit (10), IP under limit (5)
    async def mock_zcard(key):
        if "user:" in key:
            return 10  # User at limit
        elif "ip:" in key:
            return 5  # IP under limit
        return 0
    
    mock_redis.zcard.side_effect = mock_zcard
    
    # Check rate limit - should fail because user is at limit
    with pytest.raises(HTTPException) as exc_info:
        await rate_limiter.check_rate_limit(
            user_id="user123",
            ip_address="192.168.1.1",
            endpoint="/api/v1/ml/predict",
            limit="10/minute",
        )
    
    assert exc_info.value.status_code == 429


# ── Test Sliding Window Algorithm ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sliding_window_removes_old_entries(rate_limiter, mock_redis):
    """Test that old entries outside window are removed."""
    mock_redis.zcard.return_value = 5
    
    await rate_limiter.check_rate_limit(
        user_id="user123",
        ip_address="192.168.1.1",
        endpoint="/api/v1/ml/predict",
        limit="10/minute",
    )
    
    # Verify old entries were removed
    assert mock_redis.zremrangebyscore.call_count == 2  # Once for user, once for IP


@pytest.mark.asyncio
async def test_sliding_window_adds_current_request(rate_limiter, mock_redis):
    """Test that current request is added to window."""
    mock_redis.zcard.return_value = 5
    
    await rate_limiter.check_rate_limit(
        user_id="user123",
        ip_address="192.168.1.1",
        endpoint="/api/v1/ml/predict",
        limit="10/minute",
    )
    
    # Verify current request was added
    assert mock_redis.zadd.call_count == 2  # Once for user, once for IP


@pytest.mark.asyncio
async def test_sliding_window_sets_expiry(rate_limiter, mock_redis):
    """Test that keys have TTL set."""
    mock_redis.zcard.return_value = 5
    
    await rate_limiter.check_rate_limit(
        user_id="user123",
        ip_address="192.168.1.1",
        endpoint="/api/v1/ml/predict",
        limit="10/minute",
    )
    
    # Verify TTL was set
    assert mock_redis.expire.call_count == 2  # Once for user, once for IP


# ── Test Period Parsing ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_period_minute(rate_limiter):
    """Test parsing 'minute' period."""
    seconds = rate_limiter._parse_period("minute")
    assert seconds == 60


@pytest.mark.asyncio
async def test_parse_period_hour(rate_limiter):
    """Test parsing 'hour' period."""
    seconds = rate_limiter._parse_period("hour")
    assert seconds == 3600


@pytest.mark.asyncio
async def test_parse_period_second(rate_limiter):
    """Test parsing 'second' period."""
    seconds = rate_limiter._parse_period("second")
    assert seconds == 1


@pytest.mark.asyncio
async def test_parse_period_day(rate_limiter):
    """Test parsing 'day' period."""
    seconds = rate_limiter._parse_period("day")
    assert seconds == 86400


@pytest.mark.asyncio
async def test_parse_period_invalid_raises_error(rate_limiter):
    """Test that invalid period raises error."""
    with pytest.raises(ValueError, match="Invalid period"):
        rate_limiter._parse_period("invalid")


# ── Test Violation Logging ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_violation_logging_increments_counter(rate_limiter, mock_redis):
    """Test that violations increment Redis counter."""
    await rate_limiter._log_violation(
        user_id="user123",
        ip_address="192.168.1.1",
        endpoint="/api/v1/ml/predict",
        limit_type="user",
        limit=10,
        window=60,
    )
    
    # Verify counter was incremented
    mock_redis.incr.assert_called_once()
    
    # Verify TTL was set on counter
    mock_redis.expire.assert_called_once()


@pytest.mark.asyncio
async def test_violation_logging_sets_ttl(rate_limiter, mock_redis):
    """Test that violation counter has 1-hour TTL."""
    await rate_limiter._log_violation(
        user_id="user123",
        ip_address="192.168.1.1",
        endpoint="/api/v1/ml/predict",
        limit_type="user",
        limit=10,
        window=60,
    )
    
    # Verify TTL is 1 hour (3600 seconds)
    mock_redis.expire.assert_called_with(
        "ml:ratelimit:violations:user123",
        3600
    )


# ── Test HTTP Response ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_response_includes_retry_after(rate_limiter, mock_redis):
    """Test that 429 response includes Retry-After header."""
    mock_redis.zcard.return_value = 10
    
    with pytest.raises(HTTPException) as exc_info:
        await rate_limiter.check_rate_limit(
            user_id="user123",
            ip_address="192.168.1.1",
            endpoint="/api/v1/ml/predict",
            limit="10/minute",
        )
    
    # Verify Retry-After header
    assert "Retry-After" in exc_info.value.headers
    assert exc_info.value.headers["Retry-After"] == "60"


@pytest.mark.asyncio
async def test_rate_limit_response_includes_error_details(rate_limiter, mock_redis):
    """Test that 429 response includes detailed error information."""
    mock_redis.zcard.return_value = 10
    
    with pytest.raises(HTTPException) as exc_info:
        await rate_limiter.check_rate_limit(
            user_id="user123",
            ip_address="192.168.1.1",
            endpoint="/api/v1/ml/predict",
            limit="10/minute",
        )
    
    # Verify error details
    detail = exc_info.value.detail
    assert detail["error"] == "rate_limit_exceeded"
    assert detail["limit"] == 10
    assert detail["window"] == "minute"
    assert detail["retry_after"] == 60


# ── Test Different Endpoints ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_per_endpoint(rate_limiter, mock_redis):
    """Test that rate limits are tracked per endpoint."""
    mock_redis.zcard.return_value = 4  # Under limit
    
    # Request to /predict endpoint
    await rate_limiter.check_rate_limit(
        user_id="user123",
        ip_address="192.168.1.1",
        endpoint="/api/v1/ml/predict",
        limit="10/minute",
    )
    
    # Request to /batch endpoint
    await rate_limiter.check_rate_limit(
        user_id="user123",
        ip_address="192.168.1.1",
        endpoint="/api/v1/ml/predict/batch",
        limit="5/minute",
    )
    
    # Verify different keys were used
    calls = mock_redis.zcard.call_args_list
    keys = [call[0][0] for call in calls]
    
    # Should have different endpoint names in keys
    predict_keys = [k for k in keys if "/predict" in k and "/batch" not in k]
    batch_keys = [k for k in keys if "/batch" in k]
    
    assert len(predict_keys) > 0
    assert len(batch_keys) > 0


# ── Test Remaining Requests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metadata_includes_remaining_requests(rate_limiter, mock_redis):
    """Test that metadata includes remaining request count."""
    mock_redis.zcard.return_value = 7  # 7 requests made, 3 remaining
    
    allowed, metadata = await rate_limiter.check_rate_limit(
        user_id="user123",
        ip_address="192.168.1.1",
        endpoint="/api/v1/ml/predict",
        limit="10/minute",
    )
    
    assert "remaining" in metadata
    assert metadata["remaining"] >= 0


@pytest.mark.asyncio
async def test_metadata_uses_minimum_remaining(rate_limiter, mock_redis):
    """Test that metadata uses minimum of user and IP remaining."""
    # Mock: User has 2 remaining, IP has 5 remaining
    async def mock_zcard(key):
        if "user:" in key:
            return 8  # 10 - 8 = 2 remaining
        elif "ip:" in key:
            return 15  # 20 - 15 = 5 remaining
        return 0
    
    mock_redis.zcard.side_effect = mock_zcard
    
    allowed, metadata = await rate_limiter.check_rate_limit(
        user_id="user123",
        ip_address="192.168.1.1",
        endpoint="/api/v1/ml/predict",
        limit="10/minute",
    )
    
    # Should use minimum (user's 2 remaining)
    assert metadata["remaining"] <= 2
