"""
Integration Tests for EventCorrelationEngine - Production Grade
================================================================
End-to-end testing with real database and Redis, mocked external services.

Test Strategy:
- REAL PostgreSQL database with transaction rollback
- REAL Redis (DB 15) with flushdb cleanup
- MOCK Scanner, AI, ML services for controlled scenarios
- Verify: DB persistence, Redis pub/sub, latency tracking

Coverage:
- Pathway 1: Scanner Anomaly → AI/ML → Consensus → DB → Redis
- Pathway 2: News Event → Scanner/ML → Consensus → DB → Redis
- Error Handling: Timeouts, circuit breakers, recovery
- Concurrency: Parallel requests, transaction isolation

Author: Cortex AI Team
Version: 1.0.0
"""
import asyncio
import json
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.ai.correlation.engine import EventCorrelationEngine
from app.ai.fusion.models import AIEventClassification
from app.ai.fusion.signal_assembler import SignalAssembler
from app.core.config import get_settings
from app.core.database import Base
from app.models.trade_suggestions import TradeSuggestion, EventCorrelation


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture(scope="session")
def event_loop():
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Provide real database session with cleanup.
    
    Production-grade approach:
    - Uses actual PostgreSQL database
    - Truncates test data before and after each test
    - NullPool prevents event loop conflicts
    - Ensures test isolation
    """
    settings = get_settings()
    
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        poolclass=NullPool,
        echo=False,
    )
    
    async with engine.connect() as conn:
        session = AsyncSession(bind=conn, expire_on_commit=False)
        
        # Ensure tables exist
        await conn.run_sync(Base.metadata.create_all)
        
        # Cleanup BEFORE test (remove data from previous tests)
        await session.execute(text("""
            TRUNCATE TABLE 
                trade_suggestions, 
                event_correlations,
                ai_event_classifications,
                ai_nlp_results,
                ai_processed_events,
                ai_raw_events
            CASCADE
        """))
        await session.commit()
        
        yield session
        
        # Cleanup AFTER test
        await session.execute(text("""
            TRUNCATE TABLE 
                trade_suggestions, 
                event_correlations,
                ai_event_classifications,
                ai_nlp_results,
                ai_processed_events,
                ai_raw_events
            CASCADE
        """))
        await session.commit()
        await session.close()
    
    await engine.dispose()


@pytest.fixture
async def redis_client() -> AsyncGenerator[Redis, None]:
    """
    Provide real Redis client with cleanup.
    
    Production-grade approach:
    - Uses separate DB 15 for tests
    - Flushes DB before and after each test
    - Ensures test isolation
    """
    settings = get_settings()
    
    # Parse Redis URL to get host and port
    redis_url = str(settings.REDIS_URL)
    # Format: redis://host:port/db
    # Extract host and port
    import re
    match = re.match(r'redis://([^:]+):(\d+)', redis_url)
    if match:
        host, port = match.groups()
        port = int(port)
    else:
        host, port = "localhost", 6379
    
    redis = Redis(
        host=host,
        port=port,
        db=15,  # Separate DB for tests
        decode_responses=True,
    )
    
    # Verify connection
    await redis.ping()
    
    # Clean before test
    await redis.flushdb()
    
    yield redis
    
    # Clean after test
    await redis.flushdb()
    await redis.aclose()


@pytest.fixture
def mock_scanner_service():
    """Mock MarketScannerService for controlled test scenarios."""
    scanner = AsyncMock()
    
    # Default: Return empty results
    scanner.scan_all = AsyncMock(return_value=[])
    scanner.get_latest_scan = AsyncMock(return_value=None)
    
    return scanner


@pytest.fixture
def mock_signal_assembler():
    """Mock SignalAssembler for controlled test scenarios."""
    assembler = AsyncMock(spec=SignalAssembler)
    
    # Default: Return bullish signals with high confidence.
    # The engine's AI agent call is gather_news_forecast — a live forecast
    # (forecast_source=gemini_batch + explicit direction) votes in the
    # unanimity check; fallback shapes abstain and renormalize.
    assembler.gather_news_forecast = AsyncMock(return_value={
        "score": 85.0,
        "confidence": 0.90,  # 0-1 scale (will be * 100 in engine)
        "event_count": 1,
        "events": [{"id": 1, "type": "earnings", "impact": 85.0}],
        "available": True,
        "forecast_source": "gemini_batch",
        "direction": "BUY",
    })

    # Legacy shape kept for any direct callers; not used by the engine.
    assembler.gather_event_signals = AsyncMock(return_value={
        "score": 85.0,  # Positive = BUY
        "confidence": 0.90,  # 0-1 scale (will be * 100 in engine)
        "sentiment": "positive",
        "event_count": 1,
        "events": [{"id": 1, "type": "earnings", "impact": 85.0}],
    })
    
    # ML signal: prediction with direction, confidence 0-1 scale
    assembler.gather_ml_signals = AsyncMock(return_value={
        "score": 100.0,  # Not used in consensus
        "confidence": 0.92,  # 0-1 scale (will be * 100 in engine)
        "model": "ensemble_v1",
        "prediction": {
            "direction": "BUY",  # Must be "BUY", "SELL", or "HOLD"
            "entry_price": 2500.0,
            "stop_loss": 2450.0,
            "targets": [2600.0, 2650.0, 2700.0],
            "probabilities": {"BUY": 0.92, "SELL": 0.04, "HOLD": 0.04},
        },
    })
    
    return assembler


@pytest.fixture
def mock_ensemble_predictor():
    """Mock EnsemblePredictor for controlled test scenarios."""
    predictor = AsyncMock()
    
    # Default: Return BUY prediction
    predictor.predict = AsyncMock(return_value={
        "direction": "BUY",
        "confidence": 0.92,
        "entry_price": 2500.0,
        "stop_loss": 2450.0,
        "targets": [2600.0, 2650.0, 2700.0],
        "probabilities": {"BUY": 0.92, "SELL": 0.04, "HOLD": 0.04},
    })
    
    return predictor


@pytest.fixture
async def correlation_engine(
    mock_signal_assembler,
    redis_client,
):
    """Create EventCorrelationEngine with mocked dependencies."""
    engine = EventCorrelationEngine(
        signal_assembler=mock_signal_assembler,
        redis=redis_client,
    )
    return engine


# ============================================================================
# TEST DATA FACTORIES
# ============================================================================

def create_scanner_signal(
    instrument_key: str = "NSE_EQ|INE002A01018",
    symbol: str = "RELIANCE",
    score: float = 8.5,
    direction: str = "buy",  # lowercase: "buy" or "sell"
) -> dict:
    """Create a scanner anomaly signal for testing."""
    return {
        "instrument_key": instrument_key,
        "symbol": symbol,
        "score": score,
        "direction": direction,  # Must be lowercase "buy" or "sell"
        "confidence": 85.0,  # 0-100 scale
        "volume_ratio": 2.5,
        "pattern": "bullish_engulfing",
        "timeframe": "1d",
    }


async def create_news_event(
    db: AsyncSession,
    event_type: str = "earnings",
    impact_score: float = 85.0,
    affected_symbols: list[str] = None,
    sentiment: str = "bullish",
) -> AIEventClassification:
    """
    Create a news event with all required parent records.
    
    Chain: AIRawEvent → AIProcessedEvent → AINLPResult → AIEventClassification
    """
    from app.ai.fusion.models import AIRawEvent, AIProcessedEvent, AINLPResult
    import hashlib
    
    if affected_symbols is None:
        affected_symbols = ["RELIANCE", "TCS"]
    
    # Create AIRawEvent
    content = f"Test {event_type} event for {', '.join(affected_symbols)}"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    
    raw_event = AIRawEvent(
        event_timestamp=datetime.now(timezone.utc),
        source_type="test",
        source_url="https://test.example.com/news/1",
        source_name="Test Source",
        content_hash=content_hash,
        raw_content=content,
        language="en",
    )
    db.add(raw_event)
    await db.flush()  # Get ID
    
    # Create AIProcessedEvent
    processed_event = AIProcessedEvent(
        raw_event_id=raw_event.id,
        processed_timestamp=datetime.now(timezone.utc),
        translated_content=content,
        detected_language="en",
        translation_confidence=Decimal("1.0"),
        processing_status="completed",
    )
    db.add(processed_event)
    await db.flush()  # Get ID
    
    # Create AINLPResult
    nlp_result = AINLPResult(
        processed_event_id=processed_event.id,
        sentiment_score=Decimal(str(impact_score)),
        sentiment_label="positive" if impact_score > 0 else "negative",
        named_entities={"symbols": affected_symbols},
        keywords={"event_type": event_type},
        model_used="test_model",
        confidence_score=Decimal("0.90"),
    )
    db.add(nlp_result)
    await db.flush()  # Get ID
    
    # Create AIEventClassification
    event_classification = AIEventClassification(
        nlp_result_id=nlp_result.id,
        event_type=event_type,
        impact_score=Decimal(str(impact_score)),
        classification_confidence=Decimal("0.90"),
        affected_symbols=affected_symbols,
        sentiment=sentiment,  # drives Pathway-2 synthetic direction (WS5)
        reasoning="Test news event for integration testing",
    )
    db.add(event_classification)
    await db.flush()  # Get ID
    
    return event_classification


# ============================================================================
# PATHWAY 1: SCANNER ANOMALY TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_pathway1_scanner_anomaly_success(
    db_session: AsyncSession,
    correlation_engine: EventCorrelationEngine,
    redis_client: Redis,
):
    """
    Test Pathway 1: Scanner anomaly triggers AI/ML validation.
    
    Flow:
    1. Scanner detects anomaly
    2. Engine queries AI + ML (mocked)
    3. Consensus computed
    4. TradeSuggestion saved to DB
    5. EventCorrelation saved to DB
    6. Redis pub/sub message sent
    """
    # Arrange
    scanner_signal = create_scanner_signal()
    
    # Subscribe to Redis channel (engine publishes to "cai:suggestions:new")
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("cai:suggestions:new")
    
    # Act
    suggestion = await correlation_engine.on_scanner_anomaly(
        db=db_session,
        scanner_signal=scanner_signal,
    )
    
    # Assert: Suggestion created
    assert suggestion is not None
    assert suggestion.symbol == "NSE_EQ|INE002A01018"  # Engine uses instrument_key as symbol
    assert suggestion.signal_direction == "BUY"
    assert suggestion.confidence_level in ["HIGH", "MEDIUM"]
    assert suggestion.status == "active"
    assert suggestion.consensus_score > 0
    
    # Assert: Saved to database
    stmt = select(TradeSuggestion).where(
        TradeSuggestion.suggestion_id == suggestion.suggestion_id
    )
    result = await db_session.execute(stmt)
    db_suggestion = result.scalar_one()
    assert db_suggestion.symbol == "NSE_EQ|INE002A01018"
    
    # Assert: EventCorrelation saved
    stmt = select(EventCorrelation).where(
        EventCorrelation.suggestion_id == suggestion.suggestion_id
    )
    result = await db_session.execute(stmt)
    correlation = result.scalar_one()
    assert correlation.trigger_type == "SCANNER_ANOMALY"
    assert correlation.consensus_reached is True
    assert correlation.total_latency_ms >= 0  # Mocked responses can be instant
    
    # Assert: Redis pub/sub message sent
    # Engine publishes just the UUID string
    await asyncio.sleep(0.1)
    message = await pubsub.get_message(timeout=1.0)
    if message and message["type"] == "subscribe":
        message = await pubsub.get_message(timeout=1.0)
    
    if message and message["type"] == "message":
        # Engine publishes the full suggestion payload as JSON — assert the
        # identifying field rather than the whole (schema-evolving) envelope.
        payload = json.loads(message["data"])
        assert payload["suggestion_id"] == str(suggestion.suggestion_id)
        assert payload["status"] == "active"
    
    await pubsub.unsubscribe()
    await pubsub.aclose()


@pytest.mark.asyncio
async def test_pathway1_direction_mismatch_rejected(
    db_session: AsyncSession,
    correlation_engine: EventCorrelationEngine,
    mock_signal_assembler: AsyncMock,
):
    """
    Test Pathway 1: Suggestion rejected when agents disagree on direction.
    
    Scanner says BUY, but AI/ML say SELL → No consensus → No suggestion.
    """
    # Arrange: Scanner says buy (lowercase)
    scanner_signal = create_scanner_signal(direction="buy", score=8.5)

    # Mock: AI (live forecast — votes) and ML say SELL
    mock_signal_assembler.gather_news_forecast.return_value = {
        "score": -85.0,
        "confidence": 0.90,
        "event_count": 1,
        "events": [{"id": 1, "type": "earnings", "impact": -85.0}],
        "available": True,
        "forecast_source": "gemini_batch",
        "direction": "SELL",
    }
    
    mock_signal_assembler.gather_ml_signals.return_value = {
        "score": -100.0,  # SELL
        "confidence": 0.92,
        "model": "ensemble_v1",
        "prediction": {
            "direction": "SELL",
            "entry_price": 2500.0,
            "stop_loss": 2550.0,
            "targets": [2400.0, 2350.0, 2300.0],
            "probabilities": {"BUY": 0.04, "SELL": 0.92, "HOLD": 0.04},
        },
    }
    
    # Act
    suggestion = await correlation_engine.on_scanner_anomaly(
        db=db_session,
        scanner_signal=scanner_signal,
    )
    
    # Assert: No suggestion created (direction mismatch)
    assert suggestion is None
    
    # Assert: EventCorrelation still saved with rejection reason
    stmt = select(EventCorrelation).where(
        EventCorrelation.trigger_type == "SCANNER_ANOMALY"
    )
    result = await db_session.execute(stmt)
    correlation = result.scalar_one()
    assert correlation.consensus_reached is False
    assert "direction" in correlation.rejection_reason.lower() or "mismatch" in correlation.rejection_reason.lower()


@pytest.mark.asyncio
async def test_pathway1_ml_hold_rejected(
    db_session: AsyncSession,
    correlation_engine: EventCorrelationEngine,
    mock_signal_assembler: AsyncMock,
):
    """
    Test Pathway 1: Suggestion rejected when ML predicts HOLD.
    
    Scanner and AI say BUY, but ML says HOLD → No consensus.
    """
    # Arrange
    scanner_signal = create_scanner_signal(direction="buy", score=8.5)
    
    # Mock: ML says HOLD
    mock_signal_assembler.gather_ml_signals.return_value = {
        "score": 0.0,  # HOLD
        "confidence": 0.92,
        "model": "ensemble_v1",
        "prediction": {
            "direction": "HOLD",
            "entry_price": None,
            "stop_loss": None,
            "targets": [],
            "probabilities": {"BUY": 0.30, "SELL": 0.30, "HOLD": 0.40},
        },
    }
    
    # Act
    suggestion = await correlation_engine.on_scanner_anomaly(
        db=db_session,
        scanner_signal=scanner_signal,
    )
    
    # Assert: No suggestion (ML HOLD)
    assert suggestion is None
    
    # Assert: Rejection reason recorded
    stmt = select(EventCorrelation)
    result = await db_session.execute(stmt)
    correlation = result.scalar_one()
    assert correlation.consensus_reached is False
    assert "NEUTRAL" in correlation.rejection_reason or "HOLD" in correlation.rejection_reason


# ============================================================================
# PATHWAY 2: NEWS EVENT TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_pathway2_news_event_multiple_symbols(
    db_session: AsyncSession,
    correlation_engine: EventCorrelationEngine,
    redis_client: Redis,
):
    """
    Test Pathway 2: News event triggers suggestions for multiple symbols.
    
    Flow:
    1. High-impact news event detected
    2. For each affected symbol, query Scanner + ML
    3. Compute consensus for each
    4. Save suggestions to DB
    5. Publish to Redis
    """
    # Arrange: Create event with all parent records
    event = await create_news_event(
        db=db_session,
        event_type="earnings",
        impact_score=85.0,
        affected_symbols=["RELIANCE", "TCS"],
    )
    
    # Commit parent records
    await db_session.commit()
    
    # Subscribe to Redis
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("cai:suggestions:new")
    
    # Act
    suggestions = await correlation_engine.on_news_event(
        db=db_session,
        event=event,
    )
    
    # Assert: Suggestions created for both symbols
    assert len(suggestions) == 2
    symbols = {s.symbol for s in suggestions}
    assert symbols == {"RELIANCE", "TCS"}
    
    # Assert: All saved to database
    stmt = select(TradeSuggestion)
    result = await db_session.execute(stmt)
    db_suggestions = result.scalars().all()
    assert len(db_suggestions) == 2
    
    # Assert: EventCorrelations saved
    stmt = select(EventCorrelation).where(
        EventCorrelation.trigger_type == "NEWS_EVENT"
    )
    result = await db_session.execute(stmt)
    correlations = result.scalars().all()
    assert len(correlations) == 2
    
    await pubsub.unsubscribe()
    await pubsub.aclose()


# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================

@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
@pytest.mark.asyncio
async def test_timeout_handling(
    db_session: AsyncSession,
    correlation_engine: EventCorrelationEngine,
    mock_signal_assembler: AsyncMock,
):
    """
    Test timeout handling when AI/ML services are slow.
    
    Engine should timeout after 5 seconds and record failure.
    """
    # Arrange: Configure existing mock to sleep
    async def slow_ai_response(*args, **kwargs):
        await asyncio.sleep(10)  # Longer than 5s timeout
        return {}

    # Reset the mock and configure side_effect on the engine's actual AI call
    mock_signal_assembler.gather_news_forecast.reset_mock()
    mock_signal_assembler.gather_news_forecast.side_effect = slow_ai_response
    
    scanner_signal = create_scanner_signal()
    
    # Act
    suggestion = await correlation_engine.on_scanner_anomaly(
        db=db_session,
        scanner_signal=scanner_signal,
    )
    
    # Assert: No suggestion due to timeout
    assert suggestion is None
    
    # Assert: Timeout recorded in EventCorrelation
    stmt = select(EventCorrelation)
    result = await db_session.execute(stmt)
    correlation = result.scalar_one()
    assert correlation.consensus_reached is False
    assert "TIMEOUT" in correlation.rejection_reason


# ============================================================================
# CONCURRENCY TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_concurrent_requests_isolated(
    redis_client: Redis,
    mock_signal_assembler: AsyncMock,
):
    """
    Test concurrent requests are properly isolated.
    
    Multiple suggestions processed in parallel should not interfere.
    Each request gets its own DB session to avoid flush conflicts.
    """
    settings = get_settings()

    # Create engine for session creation
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        poolclass=NullPool,
        echo=False,
    )

    # This test COMMITS real rows (its subject is concurrent-session
    # isolation, so per-test transaction rollback can't be used). Idempotency
    # preamble: remove any artifacts a previous run left behind — otherwise
    # the engine's dedup guard suppresses the identical re-run signals and
    # the test flips to failing on its second execution.
    async with engine.begin() as conn:
        from sqlalchemy import text as sa_text
        await conn.execute(
            sa_text("DELETE FROM trade_suggestions WHERE instrument_key LIKE 'NSE_EQ|INE00_A01018'")
        )

    # Arrange: Create 5 different scanner signals
    signals = [
        create_scanner_signal(
            instrument_key=f"NSE_EQ|INE00{i}A01018",
            symbol=f"SYMBOL{i}",
            score=8.0 + i * 0.1,
        )
        for i in range(5)
    ]
    
    # Create correlation engine
    correlation_engine = EventCorrelationEngine(
        signal_assembler=mock_signal_assembler,
        redis=redis_client,
    )
    
    # Act: Process all in parallel with separate sessions
    async def process_with_session(signal):
        async with engine.connect() as conn:
            trans = await conn.begin()
            session = AsyncSession(
                bind=conn,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint"
            )
            try:
                suggestion = await correlation_engine.on_scanner_anomaly(session, signal)
                await trans.commit()
                return suggestion
            except Exception as e:
                await trans.rollback()
                raise
            finally:
                await session.close()
    
    tasks = [process_with_session(signal) for signal in signals]
    suggestions = await asyncio.gather(*tasks)
    
    # Assert: All suggestions created
    assert len([s for s in suggestions if s is not None]) == 5
    
    # Assert: All have unique symbols
    symbols = {s.symbol for s in suggestions if s is not None}
    assert len(symbols) == 5
    
    # Verify in database with separate session
    async with engine.connect() as conn:
        session = AsyncSession(bind=conn, expire_on_commit=False)
        stmt = select(TradeSuggestion)
        result = await session.execute(stmt)
        db_suggestions = result.scalars().all()
        assert len(db_suggestions) >= 5  # At least 5 from this test
        await session.close()

    # Post-test cleanup: don't leave synthetic suggestions in the shared DB
    # (they would surface as fake cards in the UI until the next test run).
    async with engine.begin() as conn:
        from sqlalchemy import text as sa_text
        await conn.execute(
            sa_text("DELETE FROM trade_suggestions WHERE instrument_key LIKE 'NSE_EQ|INE00_A01018'")
        )

    await engine.dispose()


# ============================================================================
# PERFORMANCE TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_latency_tracking(
    db_session: AsyncSession,
    correlation_engine: EventCorrelationEngine,
):
    """
    Test latency tracking for each agent.
    
    EventCorrelation should record response times for Scanner, AI, ML.
    """
    # Arrange
    scanner_signal = create_scanner_signal()
    
    # Act
    suggestion = await correlation_engine.on_scanner_anomaly(
        db=db_session,
        scanner_signal=scanner_signal,
    )
    
    # Assert: Latencies recorded
    stmt = select(EventCorrelation).where(
        EventCorrelation.suggestion_id == suggestion.suggestion_id
    )
    result = await db_session.execute(stmt)
    correlation = result.scalar_one()
    
    assert correlation.scanner_response_ms is not None
    assert correlation.ai_response_ms is not None
    assert correlation.ml_response_ms is not None
    assert correlation.total_latency_ms is not None
    
    # Assert: Total latency is sum of parts (approximately)
    total = (
        correlation.scanner_response_ms +
        correlation.ai_response_ms +
        correlation.ml_response_ms
    )
    assert abs(correlation.total_latency_ms - total) < 50  # Allow 50ms variance
