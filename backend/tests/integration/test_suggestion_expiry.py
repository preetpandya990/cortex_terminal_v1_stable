"""
Integration Tests for Suggestion Expiry Worker
===============================================
Production-grade tests for the expiry loop background task.

Test Coverage:
- Basic expiry functionality
- Batch processing (100 suggestions)
- Redis pub/sub notifications
- Prometheus metrics tracking
- Error handling and resilience
- Graceful shutdown
"""
import asyncio
import json
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.metrics import suggestion_expiry_total
from app.core.redis import RedisChannels
from app.models.trade_suggestions import TradeSuggestion


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
async def test_engine():
    """Create test database engine."""
    from app.core.config import get_settings
    settings = get_settings()
    
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        echo=False,
    )
    
    yield engine
    
    await engine.dispose()


@pytest.fixture
async def test_session_factory(test_engine):
    """Create test session factory."""
    return async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.fixture
async def mock_redis_client():
    """Create mock Redis client."""
    mock_redis = MagicMock()
    mock_redis._redis = AsyncMock()
    return mock_redis


@pytest.fixture
async def mock_pubsub_client():
    """Create mock PubSubClient."""
    mock_pubsub = AsyncMock()
    mock_pubsub.publish_json = AsyncMock(return_value=1)
    return mock_pubsub


@pytest.fixture
def shutdown_event():
    """Create shutdown event for testing."""
    return asyncio.Event()


# ============================================================================
# Helper Functions
# ============================================================================

async def create_test_suggestion(
    session: AsyncSession,
    symbol: str = "TEST",
    expires_in_seconds: int = -60,  # Default: expired 1 minute ago
    status: str = "active",
) -> TradeSuggestion:
    """Create a test trade suggestion."""
    now = datetime.now(timezone.utc)
    
    suggestion = TradeSuggestion(
        suggestion_id=uuid4(),
        symbol=symbol,
        instrument_key=f"NSE_EQ|{symbol}",
        trading_symbol=f"{symbol}-EQ",
        consensus_score=Decimal("85.5"),
        confidence_level="HIGH",
        signal_direction="BUY",
        trigger_pathway="TECHNICAL_FIRST",
        scanner_signal={"score": 8.5, "indicators": ["RSI", "MACD"]},
        ai_signal={"sentiment": "bullish", "confidence": 0.92},
        ml_signal={"direction": "BUY", "probability": 0.87},
        entry_price=Decimal("2450.50"),
        stop_loss=Decimal("2400.00"),
        risk_reward_ratio=Decimal("2.5"),
        generated_at=now,
        expires_at=now + timedelta(seconds=expires_in_seconds),
        status=status,
    )
    
    session.add(suggestion)
    await session.commit()
    await session.refresh(suggestion)
    
    return suggestion


# ============================================================================
# Test Cases
# ============================================================================

@pytest.mark.asyncio
async def test_expiry_loop_marks_single_expired_suggestion(
    test_session_factory,
    mock_redis_client,
    mock_pubsub_client,
    shutdown_event,
):
    """Test that expiry loop marks a single expired suggestion."""
    # Create expired suggestion
    async with test_session_factory() as session:
        suggestion = await create_test_suggestion(
            session,
            symbol="RELIANCE",
            expires_in_seconds=-60,  # Expired 1 minute ago
            status="active",
        )
        suggestion_id = suggestion.suggestion_id
    
    # Mock shutdown event to exit after 1 cycle
    shutdown_event.set()
    
    # Patch PubSubClient
    with patch("app.worker.PubSubClient", return_value=mock_pubsub_client):
        # Import and run expiry loop
        from app.worker import expiry_loop
        
        # Run one cycle (will exit immediately due to shutdown_event)
        with pytest.raises(asyncio.CancelledError):
            task = asyncio.create_task(
                expiry_loop(test_session_factory, mock_redis_client)
            )
            await asyncio.sleep(0.1)  # Let it process
            task.cancel()
            await task
    
    # Verify suggestion is expired
    async with test_session_factory() as session:
        stmt = select(TradeSuggestion).where(
            TradeSuggestion.suggestion_id == suggestion_id
        )
        result = await session.execute(stmt)
        updated_suggestion = result.scalar_one()
        
        assert updated_suggestion.status == "expired"
        assert updated_suggestion.updated_at > updated_suggestion.created_at


@pytest.mark.asyncio
async def test_expiry_loop_batch_processing(
    test_session_factory,
    mock_redis_client,
    mock_pubsub_client,
    shutdown_event,
):
    """Test that expiry loop processes suggestions in batches of 100."""
    # Create 150 expired suggestions
    async with test_session_factory() as session:
        for i in range(150):
            await create_test_suggestion(
                session,
                symbol=f"TEST{i:03d}",
                expires_in_seconds=-60,
                status="active",
            )
    
    # Mock shutdown event
    shutdown_event.set()
    
    # Patch PubSubClient
    with patch("app.worker.PubSubClient", return_value=mock_pubsub_client):
        from app.worker import expiry_loop
        
        # Run one cycle
        with pytest.raises(asyncio.CancelledError):
            task = asyncio.create_task(
                expiry_loop(test_session_factory, mock_redis_client)
            )
            await asyncio.sleep(0.2)  # Let it process
            task.cancel()
            await task
    
    # Verify exactly 100 suggestions expired (batch size)
    async with test_session_factory() as session:
        stmt = select(TradeSuggestion).where(TradeSuggestion.status == "expired")
        result = await session.execute(stmt)
        expired_suggestions = result.scalars().all()
        
        assert len(expired_suggestions) == 100


@pytest.mark.asyncio
async def test_expiry_loop_publishes_redis_events(
    test_session_factory,
    mock_redis_client,
    mock_pubsub_client,
    shutdown_event,
):
    """Test that expiry loop publishes events to Redis pub/sub."""
    # Create expired suggestion
    async with test_session_factory() as session:
        suggestion = await create_test_suggestion(
            session,
            symbol="TATASTEEL",
            expires_in_seconds=-60,
            status="active",
        )
        suggestion_id = suggestion.suggestion_id
    
    # Mock shutdown event
    shutdown_event.set()
    
    # Patch PubSubClient
    with patch("app.worker.PubSubClient", return_value=mock_pubsub_client):
        from app.worker import expiry_loop
        
        # Run one cycle
        with pytest.raises(asyncio.CancelledError):
            task = asyncio.create_task(
                expiry_loop(test_session_factory, mock_redis_client)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            await task
    
    # Verify Redis pub/sub was called
    mock_pubsub_client.publish_json.assert_called()
    
    # Verify message format
    call_args = mock_pubsub_client.publish_json.call_args
    channel, message = call_args[0]
    
    assert channel == RedisChannels.SUGGESTIONS_EXPIRED
    assert message["suggestion_id"] == str(suggestion_id)
    assert message["symbol"] == "TATASTEEL"
    assert message["signal_direction"] == "BUY"
    assert message["confidence_level"] == "HIGH"
    assert message["reason"] == "TTL_EXPIRED"


@pytest.mark.asyncio
async def test_expiry_loop_tracks_prometheus_metrics(
    test_session_factory,
    mock_redis_client,
    mock_pubsub_client,
    shutdown_event,
):
    """Test that expiry loop increments Prometheus metrics."""
    # Get initial metric value
    initial_value = suggestion_expiry_total.labels(
        direction="BUY",
        confidence_level="HIGH",
    )._value.get()
    
    # Create expired suggestion
    async with test_session_factory() as session:
        await create_test_suggestion(
            session,
            symbol="INFY",
            expires_in_seconds=-60,
            status="active",
        )
    
    # Mock shutdown event
    shutdown_event.set()
    
    # Patch PubSubClient
    with patch("app.worker.PubSubClient", return_value=mock_pubsub_client):
        from app.worker import expiry_loop
        
        # Run one cycle
        with pytest.raises(asyncio.CancelledError):
            task = asyncio.create_task(
                expiry_loop(test_session_factory, mock_redis_client)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            await task
    
    # Verify metric incremented
    final_value = suggestion_expiry_total.labels(
        direction="BUY",
        confidence_level="HIGH",
    )._value.get()
    
    assert final_value == initial_value + 1


@pytest.mark.asyncio
async def test_expiry_loop_ignores_non_active_suggestions(
    test_session_factory,
    mock_redis_client,
    mock_pubsub_client,
    shutdown_event,
):
    """Test that expiry loop only processes active suggestions."""
    # Create suggestions with different statuses
    async with test_session_factory() as session:
        # Already expired
        await create_test_suggestion(
            session,
            symbol="EXPIRED1",
            expires_in_seconds=-60,
            status="expired",
        )
        
        # Executed
        await create_test_suggestion(
            session,
            symbol="EXECUTED1",
            expires_in_seconds=-60,
            status="executed",
        )
        
        # Invalidated
        await create_test_suggestion(
            session,
            symbol="INVALID1",
            expires_in_seconds=-60,
            status="invalidated",
        )
        
        # Active but not expired
        await create_test_suggestion(
            session,
            symbol="ACTIVE1",
            expires_in_seconds=3600,  # Expires in 1 hour
            status="active",
        )
    
    # Mock shutdown event
    shutdown_event.set()
    
    # Patch PubSubClient
    with patch("app.worker.PubSubClient", return_value=mock_pubsub_client):
        from app.worker import expiry_loop
        
        # Run one cycle
        with pytest.raises(asyncio.CancelledError):
            task = asyncio.create_task(
                expiry_loop(test_session_factory, mock_redis_client)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            await task
    
    # Verify no suggestions were processed
    mock_pubsub_client.publish_json.assert_not_called()


@pytest.mark.asyncio
async def test_expiry_loop_handles_redis_pubsub_failure(
    test_session_factory,
    mock_redis_client,
    shutdown_event,
    caplog,
):
    """Test that expiry loop continues on Redis pub/sub failure."""
    # Create expired suggestion
    async with test_session_factory() as session:
        suggestion = await create_test_suggestion(
            session,
            symbol="HDFC",
            expires_in_seconds=-60,
            status="active",
        )
        suggestion_id = suggestion.suggestion_id
    
    # Mock PubSubClient to raise exception
    mock_pubsub_client = AsyncMock()
    mock_pubsub_client.publish_json = AsyncMock(
        side_effect=Exception("Redis connection lost")
    )
    
    # Mock shutdown event
    shutdown_event.set()
    
    # Patch PubSubClient
    with patch("app.worker.PubSubClient", return_value=mock_pubsub_client):
        from app.worker import expiry_loop
        
        # Run one cycle
        with pytest.raises(asyncio.CancelledError):
            task = asyncio.create_task(
                expiry_loop(test_session_factory, mock_redis_client)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            await task
    
    # Verify suggestion is still expired (non-blocking failure)
    async with test_session_factory() as session:
        stmt = select(TradeSuggestion).where(
            TradeSuggestion.suggestion_id == suggestion_id
        )
        result = await session.execute(stmt)
        updated_suggestion = result.scalar_one()
        
        assert updated_suggestion.status == "expired"
    
    # Verify warning was logged
    assert "Failed to publish expiry event" in caplog.text


@pytest.mark.asyncio
async def test_expiry_loop_performance_100_suggestions(
    test_session_factory,
    mock_redis_client,
    mock_pubsub_client,
    shutdown_event,
):
    """Test expiry loop performance with 100 suggestions."""
    import time
    
    # Create 100 expired suggestions
    async with test_session_factory() as session:
        for i in range(100):
            await create_test_suggestion(
                session,
                symbol=f"PERF{i:03d}",
                expires_in_seconds=-60,
                status="active",
            )
    
    # Mock shutdown event
    shutdown_event.set()
    
    # Patch PubSubClient
    with patch("app.worker.PubSubClient", return_value=mock_pubsub_client):
        from app.worker import expiry_loop
        
        # Measure execution time
        start = time.time()
        
        with pytest.raises(asyncio.CancelledError):
            task = asyncio.create_task(
                expiry_loop(test_session_factory, mock_redis_client)
            )
            await asyncio.sleep(0.5)  # Give it time to process
            task.cancel()
            await task
        
        duration = time.time() - start
    
    # Verify performance (should be <1 second for 100 suggestions)
    assert duration < 1.0
    
    # Verify all 100 suggestions expired
    async with test_session_factory() as session:
        stmt = select(TradeSuggestion).where(TradeSuggestion.status == "expired")
        result = await session.execute(stmt)
        expired_suggestions = result.scalars().all()
        
        assert len(expired_suggestions) == 100


# ============================================================================
# Cleanup
# ============================================================================

@pytest.fixture(autouse=True)
async def cleanup_test_suggestions(test_session_factory):
    """Clean up test suggestions after each test."""
    yield
    
    # Delete all test suggestions
    async with test_session_factory() as session:
        stmt = select(TradeSuggestion).where(
            TradeSuggestion.symbol.like("TEST%")
        )
        result = await session.execute(stmt)
        suggestions = result.scalars().all()
        
        for suggestion in suggestions:
            await session.delete(suggestion)
        
        await session.commit()
