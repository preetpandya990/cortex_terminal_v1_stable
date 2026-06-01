"""Unit tests for AuditLogger."""
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.audit.audit_logger import AuditLogger, PredictionAuditLog, TrainingRunAuditLog


@pytest.mark.asyncio
async def test_log_prediction_creates_entry(db_session: AsyncSession):
    """Test prediction log creation with all required fields."""
    logger = AuditLogger(db_session)
    
    log_id = await logger.log_prediction(
        user_id="test-user",
        symbol="BTCUSDT",
        timeframe="1h",
        model_version="1.0.0",
        feature_version="1.0.0",
        feature_vector={"rsi": 70.5, "macd": 0.5},
        direction="BUY",
        confidence=0.85,
        entry_price=100.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        take_profit_3=106.0,
        stop_loss=98.0,
        volatility_estimate=2.5,
        top_features=[{"name": "rsi", "importance": 0.8}],
        explanation_text="Strong bullish signal",
        latency_ms=45.2
    )
    
    assert log_id is not None
    assert isinstance(log_id, str)


@pytest.mark.asyncio
async def test_log_training_run(db_session: AsyncSession):
    """Test training run log creation."""
    logger = AuditLogger(db_session)
    
    log_id = await logger.log_training_run(
        experiment_name="test_experiment",
        model_version="1.0.0",
        training_start_date=datetime.utcnow() - timedelta(hours=2),
        training_end_date=datetime.utcnow(),
        training_duration_seconds=7200.0,
        directional_accuracy=0.88,
        buy_accuracy=0.85,
        sell_accuracy=0.82,
        hold_accuracy=0.90,
        avg_latency_ms=50.0,
        feature_version="1.0.0",
        training_samples=10000,
        validation_samples=2000,
        test_samples=1000,
        hyperparameters={"learning_rate": 0.001, "epochs": 100},
        status="completed"
    )
    
    assert log_id is not None


@pytest.mark.asyncio
async def test_log_model_deployment(db_session: AsyncSession):
    """Test model deployment log creation."""
    logger = AuditLogger(db_session)
    
    log_id = await logger.log_model_deployment(
        model_version="1.0.0",
        action="promote",
        from_stage="staging",
        to_stage="production",
        approved_by="admin-user",
        reason="Passed all quality gates"
    )
    
    assert log_id is not None


@pytest.mark.asyncio
async def test_query_logs_by_symbol(db_session: AsyncSession):
    """Test querying logs by symbol."""
    logger = AuditLogger(db_session)
    
    # Create test logs
    await logger.log_prediction(
        user_id="user1",
        symbol="BTCUSDT",
        timeframe="1h",
        model_version="1.0.0",
        feature_version="1.0.0",
        feature_vector={},
        direction="BUY",
        confidence=0.85,
        entry_price=100.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        take_profit_3=106.0,
        stop_loss=98.0,
        volatility_estimate=2.5,
        top_features=[],
        explanation_text="Test",
        latency_ms=50.0
    )
    
    logs = await logger.get_logs(symbol="BTCUSDT")
    
    assert len(logs) > 0
    assert all(log.symbol == "BTCUSDT" for log in logs)


@pytest.mark.asyncio
async def test_query_logs_by_date_range(db_session: AsyncSession):
    """Test querying logs by date range."""
    logger = AuditLogger(db_session)
    
    start_date = datetime.utcnow() - timedelta(days=7)
    end_date = datetime.utcnow()
    
    logs = await logger.get_logs(start_date=start_date, end_date=end_date)
    
    assert isinstance(logs, list)
    for log in logs:
        assert start_date <= log.timestamp <= end_date


@pytest.mark.asyncio
async def test_query_logs_by_model_version(db_session: AsyncSession):
    """Test querying logs by model version."""
    logger = AuditLogger(db_session)
    
    await logger.log_prediction(
        user_id="user1",
        symbol="ETHUSDT",
        timeframe="1h",
        model_version="2.0.0",
        feature_version="1.0.0",
        feature_vector={},
        direction="SELL",
        confidence=0.75,
        entry_price=200.0,
        take_profit_1=198.0,
        take_profit_2=196.0,
        take_profit_3=194.0,
        stop_loss=202.0,
        volatility_estimate=3.0,
        top_features=[],
        explanation_text="Test",
        latency_ms=55.0
    )
    
    logs = await logger.get_logs(model_version="2.0.0")
    
    assert len(logs) > 0
    assert all(log.model_version == "2.0.0" for log in logs)


@pytest.mark.asyncio
async def test_log_retention_cleanup(db_session: AsyncSession):
    """Test log retention and cleanup."""
    logger = AuditLogger(db_session, retention_years=7)
    
    # Mock old logs
    cutoff_date = datetime.utcnow() - timedelta(days=365 * 7 + 1)
    
    deleted_count = await logger.cleanup_old_logs(cutoff_date)
    
    assert deleted_count >= 0


@pytest.mark.asyncio
async def test_async_write_queue(db_session: AsyncSession):
    """Test async write queue for non-blocking logging."""
    logger = AuditLogger(db_session)
    await logger.start()
    
    # Log should be queued, not blocking
    log_id = await logger.log_prediction(
        user_id="user1",
        symbol="BTCUSDT",
        timeframe="1h",
        model_version="1.0.0",
        feature_version="1.0.0",
        feature_vector={},
        direction="BUY",
        confidence=0.85,
        entry_price=100.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        take_profit_3=106.0,
        stop_loss=98.0,
        volatility_estimate=2.5,
        top_features=[],
        explanation_text="Test",
        latency_ms=50.0
    )
    
    assert log_id is not None
    
    await logger.stop()


@pytest.mark.asyncio
async def test_log_with_ai_sentiment(db_session: AsyncSession):
    """Test logging with optional AI sentiment."""
    logger = AuditLogger(db_session)
    
    log_id = await logger.log_prediction(
        user_id="user1",
        symbol="BTCUSDT",
        timeframe="1h",
        model_version="1.0.0",
        feature_version="1.0.0",
        feature_vector={},
        direction="BUY",
        confidence=0.85,
        entry_price=100.0,
        take_profit_1=102.0,
        take_profit_2=104.0,
        take_profit_3=106.0,
        stop_loss=98.0,
        volatility_estimate=2.5,
        top_features=[],
        explanation_text="Test",
        latency_ms=50.0,
        ai_sentiment="bullish",
        ai_confidence=0.92
    )
    
    assert log_id is not None


@pytest.mark.asyncio
async def test_query_logs_pagination(db_session: AsyncSession):
    """Test log querying with pagination."""
    logger = AuditLogger(db_session)
    
    # Create multiple logs
    for i in range(20):
        await logger.log_prediction(
            user_id=f"user{i}",
            symbol="BTCUSDT",
            timeframe="1h",
            model_version="1.0.0",
            feature_version="1.0.0",
            feature_vector={},
            direction="BUY",
            confidence=0.85,
            entry_price=100.0,
            take_profit_1=102.0,
            take_profit_2=104.0,
            take_profit_3=106.0,
            stop_loss=98.0,
            volatility_estimate=2.5,
            top_features=[],
            explanation_text="Test",
            latency_ms=50.0
        )
    
    # Query with limit
    logs = await logger.get_logs(symbol="BTCUSDT", limit=10)
    
    assert len(logs) <= 10


@pytest.mark.asyncio
async def test_failed_training_run_log(db_session: AsyncSession):
    """Test logging failed training run."""
    logger = AuditLogger(db_session)
    
    log_id = await logger.log_training_run(
        experiment_name="failed_experiment",
        model_version="1.0.0",
        training_start_date=datetime.utcnow() - timedelta(hours=1),
        training_end_date=datetime.utcnow(),
        training_duration_seconds=3600.0,
        directional_accuracy=0.0,
        buy_accuracy=0.0,
        sell_accuracy=0.0,
        hold_accuracy=0.0,
        avg_latency_ms=0.0,
        feature_version="1.0.0",
        training_samples=0,
        validation_samples=0,
        test_samples=0,
        hyperparameters={},
        status="failed",
        error_message="Out of memory"
    )
    
    assert log_id is not None
