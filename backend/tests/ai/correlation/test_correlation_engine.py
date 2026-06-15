"""
Unit Tests for EventCorrelationEngine - Production Grade
=========================================================
Comprehensive test coverage for bidirectional multi-agent consensus system.

Coverage:
- CircuitBreaker: States, thresholds, recovery
- Consensus Logic: Weighted scoring, confidence levels
- Directional Alignment: BUY/SELL agreement checks
- Rejection Reasons: ML HOLD, direction mismatch, low confidence
- Pathways: Scanner anomaly (P1), News event (P2)
- Error Handling: Timeouts, exceptions, circuit breakers
- Performance: Latency tracking, Redis pub/sub

Author: Cortex AI Team
Version: 1.0.0
"""
import asyncio
import json
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from app.ai.correlation.engine import (
    CircuitBreaker,
    EventCorrelationEngine,
    CONSENSUS_HIGH_THRESHOLD,
    CONSENSUS_MEDIUM_THRESHOLD,
    SCANNER_WEIGHT,
    AI_WEIGHT,
    ML_WEIGHT,
)
from app.ai.fusion.models import AIEventClassification
from app.models.trade_suggestions import TradeSuggestion, EventCorrelation


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def mock_db():
    """Mock async database session."""
    db = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    # db.execute() is async; the returned Result's scalar_one_or_none() is
    # synchronous.  AsyncMock children are also AsyncMock, so calling
    # scalar_one_or_none() without await returns a coroutine and breaks the
    # deduplication guard in _compute_consensus.  Use a plain MagicMock for
    # the execute return value so the synchronous call returns None (no existing
    # suggestion) as expected by all unit tests here.
    execute_result = MagicMock()
    execute_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=execute_result)
    return db


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    redis = AsyncMock()
    redis.publish = AsyncMock()
    # redis-py's pipeline() is synchronous — it returns a Pipeline object whose
    # command methods (setex, zadd, etc.) are also synchronous; only execute()
    # is async.  AsyncMock children are also AsyncMock, so calling
    # self.redis.pipeline() without await creates an unawaited coroutine which
    # pytest turns into a PytestUnraisableExceptionWarning (treated as an error
    # via filterwarnings = error).  Use a plain MagicMock so the synchronous
    # call pattern matches real redis-py behaviour.
    pipeline = MagicMock()
    pipeline.setex = MagicMock()
    pipeline.zadd = MagicMock()
    pipeline.expire = MagicMock()
    pipeline.delete = MagicMock()
    pipeline.zrem = MagicMock()
    pipeline.execute = AsyncMock(return_value=[])
    redis.pipeline = MagicMock(return_value=pipeline)
    return redis


@pytest.fixture
def mock_signal_assembler():
    """Mock SignalAssembler."""
    assembler = AsyncMock()

    # Pathway 1 calls gather_ml_signals first (its indicator snapshot feeds the
    # news forecaster), then gather_news_forecast for the AI/news slot.
    # gather_event_signals is retained for backward compat but is no longer
    # called by the engine pathways; tests that used to configure it now use
    # gather_news_forecast instead.
    assembler.gather_ml_signals = AsyncMock(return_value={
        "score": 100.0,  # BUY
        "confidence": 0.90,
        "model": "ensemble_v1",
        "prediction": {
            "direction": "BUY",
            "entry_price": 1500.0,
            "stop_loss": 1450.0,
            "targets": [1600.0, 1650.0, 1700.0],
            "probabilities": {"BUY": 0.90, "SELL": 0.05, "HOLD": 0.05},
        },
    })

    assembler.gather_news_forecast = AsyncMock(return_value={
        "score": 75.0,
        "confidence": 0.85,
        "event_count": 2,
        "available": True,
        "source": "gemini",
        "direction": "BUY",
    })

    # Kept for direct callers; not invoked by engine Pathway 1 or 2.
    assembler.gather_event_signals = AsyncMock(return_value={
        "score": 75.0,
        "confidence": 0.85,
        "event_count": 2,
        "events": [{"id": 1, "type": "earnings", "impact": 80.0}],
    })

    return assembler


@pytest.fixture
def correlation_engine(mock_signal_assembler, mock_redis):
    """Create EventCorrelationEngine instance."""
    return EventCorrelationEngine(
        signal_assembler=mock_signal_assembler,
        redis=mock_redis,
    )


@pytest.fixture
def scanner_signal_buy():
    """Sample scanner signal - BUY direction."""
    return {
        "instrument_key": "NSE_EQ|INE002A01018",
        "trading_symbol": "RELIANCE-EQ",
        "direction": "buy",
        "confidence": 85.0,
        "price_change_pct": 3.5,
        "volume_ratio": 2.8,
        "signals": ["volume_spike", "breakout"],
    }


@pytest.fixture
def scanner_signal_sell():
    """Sample scanner signal - SELL direction."""
    return {
        "instrument_key": "NSE_EQ|INE009A01021",
        "trading_symbol": "INFY-EQ",
        "direction": "sell",
        "confidence": 80.0,
        "price_change_pct": -2.5,
        "volume_ratio": 2.2,
        "signals": ["breakdown", "volume_spike"],
    }


@pytest.fixture
def news_event():
    """Sample news event."""
    return AIEventClassification(
        id=1,
        event_type="earnings_beat",
        impact_score=Decimal("85.0"),
        classification_confidence=Decimal("0.92"),
        affected_symbols=["NSE_EQ|INE002A01018", "NSE_EQ|INE009A01021"],
        created_at=datetime.now(timezone.utc),
        decay_half_life_hours=24.0,
    )


# ============================================================================
# TEST CIRCUIT BREAKER
# ============================================================================

class TestCircuitBreaker:
    """Test CircuitBreaker fault tolerance."""

    def test_init_state_closed(self):
        """Circuit breaker starts in closed state."""
        breaker = CircuitBreaker()
        assert breaker.state == "closed"
        assert breaker.failure_count == 0
        assert breaker.last_failure_time is None

    def test_record_success_in_closed(self):
        """Recording success in closed state maintains state."""
        breaker = CircuitBreaker()
        breaker.record_success()
        assert breaker.state == "closed"
        assert breaker.failure_count == 0

    def test_record_failure_increments_count(self):
        """Failures increment counter."""
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        assert breaker.failure_count == 1
        assert breaker.state == "closed"
        assert breaker.last_failure_time is not None

    def test_opens_after_threshold(self):
        """Circuit opens after threshold failures."""
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == "closed"
        
        breaker.record_failure()
        assert breaker.state == "open"
        assert breaker.failure_count == 3

    def test_can_attempt_when_closed(self):
        """Attempts allowed when closed."""
        breaker = CircuitBreaker()
        assert breaker.can_attempt() is True

    def test_cannot_attempt_when_open(self):
        """Attempts blocked when open."""
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.record_failure()
        assert breaker.state == "open"
        assert breaker.can_attempt() is False

    def test_transitions_to_half_open_after_timeout(self):
        """Transitions to half-open after timeout."""
        breaker = CircuitBreaker(failure_threshold=1, timeout_seconds=1)
        breaker.record_failure()
        assert breaker.state == "open"
        
        # Simulate timeout
        breaker.last_failure_time = datetime.now(timezone.utc) - timedelta(seconds=2)
        
        assert breaker.can_attempt() is True
        assert breaker.state == "half_open"

    def test_half_open_closes_on_success(self):
        """Half-open closes on success."""
        breaker = CircuitBreaker(failure_threshold=1, timeout_seconds=1)
        breaker.record_failure()
        breaker.last_failure_time = datetime.now(timezone.utc) - timedelta(seconds=2)
        breaker.can_attempt()  # Transition to half_open
        
        breaker.record_success()
        assert breaker.state == "closed"
        assert breaker.failure_count == 0

    def test_half_open_reopens_on_failure(self):
        """Half-open reopens on failure."""
        breaker = CircuitBreaker(failure_threshold=1, timeout_seconds=1)
        breaker.record_failure()
        breaker.last_failure_time = datetime.now(timezone.utc) - timedelta(seconds=2)
        breaker.can_attempt()  # Transition to half_open
        
        breaker.record_failure()
        assert breaker.state == "open"


# ============================================================================
# TEST CONSENSUS COMPUTATION
# ============================================================================

class TestConsensusComputation:
    """Test weighted consensus scoring and confidence levels."""

    @pytest.mark.asyncio
    async def test_high_confidence_buy_suggestion(
        self,
        correlation_engine,
        mock_db,
        scanner_signal_buy,
    ):
        """All agents agree BUY with high confidence → HIGH suggestion."""
        scanner_conf = 85.0
        ai_conf = 90.0
        ml_conf = 88.0

        scanner_signal = {**scanner_signal_buy, "confidence": scanner_conf}
        # available=True + event_count=2 → standard 3-weight formula (no redistribution).
        ai_signal = {"score": 80.0, "confidence": ai_conf / 100, "available": True, "event_count": 2}
        ml_signal = {
            "score": 100.0,
            "confidence": ml_conf / 100,
            "prediction": {
                "direction": "BUY",
                "entry_price": 1500.0,
                "stop_loss": 1450.0,
                "targets": [1600.0, 1650.0, 1700.0],
            },
        }

        # The engine blends scanner_conf 70/30 with the volume-ratio signal.
        # scanner_signal_buy has volume_ratio=2.8 → vol_conf=28.0
        # blended = min(85*0.70 + 28*0.30, 100) = min(59.5+8.4, 100) = 67.9
        # consensus = 0.30*67.9 + 0.40*90 + 0.30*88 = 20.37+36.0+26.4 = 82.77 (HIGH)
        _vol_conf = min(scanner_signal["volume_ratio"], 10.0) / 10.0 * 100.0
        blended_scanner = min(scanner_conf * 0.70 + _vol_conf * 0.30, 100.0)
        expected_score = (
            SCANNER_WEIGHT * blended_scanner
            + AI_WEIGHT * ai_conf
            + ML_WEIGHT * ml_conf
        )
        assert expected_score >= CONSENSUS_HIGH_THRESHOLD

        suggestion = await correlation_engine._compute_consensus(
            db=mock_db,
            correlation_id=uuid4(),
            trigger_type="SCANNER_ANOMALY",
            trigger_timestamp=datetime.now(timezone.utc),
            scanner_signal=scanner_signal,
            ai_signal=ai_signal,
            ml_signal=ml_signal,
            latencies={"scanner_ms": 10, "ai_ms": 50, "ml_ms": 40, "total_ms": 100},
        )

        assert suggestion is not None
        assert suggestion.signal_direction == "BUY"
        assert suggestion.confidence_level == "HIGH"
        assert float(suggestion.consensus_score) == pytest.approx(expected_score, rel=0.01)
        assert suggestion.status == "active"
        assert mock_db.add.call_count == 2  # TradeSuggestion + EventCorrelation
        assert mock_db.commit.await_count == 2

    @pytest.mark.asyncio
    async def test_medium_confidence_sell_suggestion(
        self,
        correlation_engine,
        mock_db,
        scanner_signal_sell,
    ):
        """All agents agree SELL with medium confidence → MEDIUM suggestion."""
        scanner_conf = 70.0
        ai_conf = 65.0
        ml_conf = 68.0

        scanner_signal = {**scanner_signal_sell, "confidence": scanner_conf}
        # Negative score → SELL; available=True + event_count=2 → standard 3-weight formula.
        ai_signal = {"score": -60.0, "confidence": ai_conf / 100, "available": True, "event_count": 2}
        ml_signal = {
            "score": -100.0,
            "confidence": ml_conf / 100,
            "prediction": {
                "direction": "SELL",
                "entry_price": 1400.0,
                "stop_loss": 1450.0,
                "targets": [1300.0, 1250.0, 1200.0],
            },
        }

        # The engine blends scanner_conf 70/30 with the volume-ratio signal.
        # scanner_signal_sell has volume_ratio=2.2 → vol_conf=22.0
        # blended = min(70*0.70 + 22*0.30, 100) = min(49.0+6.6, 100) = 55.6
        # consensus = 0.30*55.6 + 0.40*65 + 0.30*68 = 16.68+26.0+20.4 = 63.08 (MEDIUM)
        _vol_conf = min(scanner_signal["volume_ratio"], 10.0) / 10.0 * 100.0
        blended_scanner = min(scanner_conf * 0.70 + _vol_conf * 0.30, 100.0)
        expected_score = (
            SCANNER_WEIGHT * blended_scanner
            + AI_WEIGHT * ai_conf
            + ML_WEIGHT * ml_conf
        )
        assert CONSENSUS_MEDIUM_THRESHOLD <= expected_score < CONSENSUS_HIGH_THRESHOLD

        suggestion = await correlation_engine._compute_consensus(
            db=mock_db,
            correlation_id=uuid4(),
            trigger_type="NEWS_EVENT",
            trigger_timestamp=datetime.now(timezone.utc),
            scanner_signal=scanner_signal,
            ai_signal=ai_signal,
            ml_signal=ml_signal,
            latencies={"scanner_ms": 15, "ai_ms": 45, "ml_ms": 35, "total_ms": 95},
        )

        assert suggestion is not None
        assert suggestion.signal_direction == "SELL"
        assert suggestion.confidence_level == "MEDIUM"
        assert float(suggestion.consensus_score) == pytest.approx(expected_score, rel=0.01)


# ============================================================================
# TEST REJECTION REASONS
# ============================================================================

class TestRejectionReasons:
    """Test consensus rejection scenarios."""

    @pytest.mark.asyncio
    async def test_ml_hold_rejection(
        self,
        correlation_engine,
        mock_db,
        scanner_signal_buy,
    ):
        """ML HOLD signal → immediate rejection."""
        ai_signal = {"score": 75.0, "confidence": 0.85}
        ml_signal = {
            "score": 0.0,
            "confidence": 0.70,
            "prediction": {"direction": "HOLD"},
        }
        
        suggestion = await correlation_engine._compute_consensus(
            db=mock_db,
            correlation_id=uuid4(),
            trigger_type="SCANNER_ANOMALY",
            trigger_timestamp=datetime.now(timezone.utc),
            scanner_signal=scanner_signal_buy,
            ai_signal=ai_signal,
            ml_signal=ml_signal,
            latencies={"scanner_ms": 10, "ai_ms": 50, "ml_ms": 40, "total_ms": 100},
        )
        
        assert suggestion is None
        # Verify correlation recorded with rejection reason
        mock_db.add.assert_called_once()
        correlation = mock_db.add.call_args[0][0]
        assert isinstance(correlation, EventCorrelation)
        assert correlation.rejection_reason == "ML_NEUTRAL"
        assert correlation.consensus_reached is False

    @pytest.mark.asyncio
    async def test_direction_mismatch_rejection(
        self,
        correlation_engine,
        mock_db,
        scanner_signal_buy,
    ):
        """Direction mismatch → rejection."""
        # Scanner: BUY, AI: SELL, ML: BUY
        ai_signal = {"score": -70.0, "confidence": 0.85}  # SELL
        ml_signal = {
            "score": 100.0,
            "confidence": 0.88,
            "prediction": {"direction": "BUY"},
        }
        
        suggestion = await correlation_engine._compute_consensus(
            db=mock_db,
            correlation_id=uuid4(),
            trigger_type="SCANNER_ANOMALY",
            trigger_timestamp=datetime.now(timezone.utc),
            scanner_signal=scanner_signal_buy,
            ai_signal=ai_signal,
            ml_signal=ml_signal,
            latencies={"scanner_ms": 10, "ai_ms": 50, "ml_ms": 40, "total_ms": 100},
        )
        
        assert suggestion is None
        correlation = mock_db.add.call_args[0][0]
        assert "DIRECTION_MISMATCH" in correlation.rejection_reason
        assert "Scanner=BUY" in correlation.rejection_reason
        assert "AI=SELL" in correlation.rejection_reason

    @pytest.mark.asyncio
    async def test_low_confidence_rejection(
        self,
        correlation_engine,
        mock_db,
        scanner_signal_buy,
    ):
        """Consensus score <60 → rejection."""
        scanner_conf = 50.0
        ai_conf = 55.0
        ml_conf = 52.0

        scanner_signal = {**scanner_signal_buy, "confidence": scanner_conf}
        # available=True + event_count=2 → standard 3-weight formula (no redistribution).
        ai_signal = {"score": 60.0, "confidence": ai_conf / 100, "available": True, "event_count": 2}
        ml_signal = {
            "score": 100.0,
            "confidence": ml_conf / 100,
            "prediction": {"direction": "BUY"},
        }

        # The engine blends scanner_conf 70/30 with the volume-ratio signal.
        # scanner_signal_buy has volume_ratio=2.8 → vol_conf=28.0
        # blended = min(50*0.70 + 28*0.30, 100) = min(35.0+8.4, 100) = 43.4
        # consensus = 0.30*43.4 + 0.40*55 + 0.30*52 = 13.02+22.0+15.6 = 50.62 (<60)
        _vol_conf = min(scanner_signal["volume_ratio"], 10.0) / 10.0 * 100.0
        blended_scanner = min(scanner_conf * 0.70 + _vol_conf * 0.30, 100.0)
        expected_score = (
            SCANNER_WEIGHT * blended_scanner
            + AI_WEIGHT * ai_conf
            + ML_WEIGHT * ml_conf
        )
        assert expected_score < CONSENSUS_MEDIUM_THRESHOLD

        suggestion = await correlation_engine._compute_consensus(
            db=mock_db,
            correlation_id=uuid4(),
            trigger_type="SCANNER_ANOMALY",
            trigger_timestamp=datetime.now(timezone.utc),
            scanner_signal=scanner_signal,
            ai_signal=ai_signal,
            ml_signal=ml_signal,
            latencies={"scanner_ms": 10, "ai_ms": 50, "ml_ms": 40, "total_ms": 100},
        )

        assert suggestion is None
        correlation = mock_db.add.call_args[0][0]
        assert "LOW_CONFIDENCE" in correlation.rejection_reason
        # Verify the rejection reason contains the actual computed score (2 d.p.)
        assert str(expected_score)[:4] in correlation.rejection_reason


# ============================================================================
# TEST PATHWAYS
# ============================================================================

class TestPathway1:
    """Test Pathway 1: Scanner Anomaly → AI + ML."""

    @pytest.mark.asyncio
    async def test_scanner_anomaly_success(
        self,
        correlation_engine,
        mock_db,
        mock_signal_assembler,
        scanner_signal_buy,
    ):
        """Scanner anomaly triggers AI+ML validation → suggestion."""
        # Pathway 1 (TECHNICAL_FIRST) calls gather_news_forecast, not gather_event_signals.
        mock_signal_assembler.gather_news_forecast.return_value = {
            "score": 80.0,
            "confidence": 0.88,
            "event_count": 2,
            "available": True,
            "source": "gemini",
            "direction": "BUY",
        }
        mock_signal_assembler.gather_ml_signals.return_value = {
            "score": 100.0,
            "confidence": 0.90,
            "prediction": {
                "direction": "BUY",
                "entry_price": 1500.0,
                "stop_loss": 1450.0,
                "targets": [1600.0, 1650.0, 1700.0],
            },
        }

        suggestion = await correlation_engine.on_scanner_anomaly(
            db=mock_db,
            scanner_signal=scanner_signal_buy,
        )

        assert suggestion is not None
        assert suggestion.signal_direction == "BUY"
        assert suggestion.trigger_pathway == "TECHNICAL_FIRST"
        mock_signal_assembler.gather_news_forecast.assert_awaited_once()
        mock_signal_assembler.gather_ml_signals.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_scanner_anomaly_timeout(
        self,
        correlation_engine,
        mock_db,
        scanner_signal_buy,
    ):
        """Signal gathering timeout → None with TIMEOUT reason."""
        # Patch _gather_signals_pathway1 to raise TimeoutError
        with patch.object(
            correlation_engine,
            '_gather_signals_pathway1',
            side_effect=asyncio.TimeoutError()
        ):
            suggestion = await correlation_engine.on_scanner_anomaly(
                db=mock_db,
                scanner_signal=scanner_signal_buy,
            )
        
        assert suggestion is None
        correlation = mock_db.add.call_args[0][0]
        assert correlation.rejection_reason == "TIMEOUT"

    @pytest.mark.asyncio
    async def test_scanner_anomaly_error(
        self,
        correlation_engine,
        mock_db,
        mock_signal_assembler,
        scanner_signal_buy,
    ):
        """Exception during gathering → None with ERROR reason."""
        # Pathway 1 gathers via gather_news_forecast; simulate that failure.
        mock_signal_assembler.gather_news_forecast.side_effect = Exception("DB connection failed")

        suggestion = await correlation_engine.on_scanner_anomaly(
            db=mock_db,
            scanner_signal=scanner_signal_buy,
        )

        assert suggestion is None
        correlation = mock_db.add.call_args[0][0]
        assert "ERROR" in correlation.rejection_reason
        assert "DB connection failed" in correlation.rejection_reason


class TestPathway2:
    """Test Pathway 2: News Event → Scanner + ML."""

    @pytest.mark.asyncio
    async def test_news_event_multiple_symbols(
        self,
        mock_db,
        mock_redis,
        news_event,
    ):
        """News event triggers validation for multiple symbols."""
        # Create fresh mocks for this test.
        # Pathway 2 (FUNDAMENTAL_FIRST) calls gather_ml_signals + gather_news_forecast.
        # scanner_cache=None → synthetic fallback (confidence=50, volume_ratio=1.0).
        # blended_scanner = min(50*0.70 + 10*0.30, 100) = 38.0
        # gather_news_forecast: available=True, event_count=2 → standard 3-weight
        # consensus = 0.30*38 + 0.40*88 + 0.30*90 = 73.6 (MEDIUM ≥ 60 → suggestion)
        assembler = AsyncMock()
        assembler.gather_ml_signals = AsyncMock(return_value={
            "score": 100.0,
            "confidence": 0.90,
            "prediction": {
                "direction": "BUY",
                "entry_price": 1500.0,
                "stop_loss": 1450.0,
                "targets": [1600.0, 1650.0, 1700.0],
            },
        })
        assembler.gather_news_forecast = AsyncMock(return_value={
            "score": 80.0,
            "confidence": 0.88,
            "event_count": 2,
            "available": True,
            "source": "gemini",
            "direction": "BUY",
        })

        engine = EventCorrelationEngine(
            signal_assembler=assembler,
            redis=mock_redis,
        )
        
        suggestions = await engine.on_news_event(
            db=mock_db,
            event=news_event,
        )
        
        assert len(suggestions) == 2  # Two affected symbols
        assert all(s.trigger_pathway == "FUNDAMENTAL_FIRST" for s in suggestions)
        assert all(s.signal_direction == "BUY" for s in suggestions)

    @pytest.mark.asyncio
    async def test_news_event_partial_success(
        self,
        correlation_engine,
        mock_db,
        mock_signal_assembler,
        news_event,
    ):
        """News event with one symbol failing → returns successful ones."""
        call_count = 0
        
        async def ml_signals_with_failure(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("ML service unavailable")
            return {
                "score": 100.0,
                "confidence": 0.90,
                "prediction": {
                    "direction": "BUY",
                    "entry_price": 1500.0,
                    "stop_loss": 1450.0,
                    "targets": [1600.0],
                },
            }
        
        mock_signal_assembler.gather_ml_signals.side_effect = ml_signals_with_failure
        
        suggestions = await correlation_engine.on_news_event(
            db=mock_db,
            event=news_event,
        )
        
        assert len(suggestions) == 1  # Only one succeeded


# ============================================================================
# TEST TRADE PARAMETERS
# ============================================================================

class TestTradeParameters:
    """Test trade parameter extraction and calculations."""

    @pytest.mark.asyncio
    async def test_risk_reward_calculation(
        self,
        correlation_engine,
        mock_db,
        scanner_signal_buy,
    ):
        """Risk/reward ratio calculated correctly."""
        entry = 1500.0
        stop_loss = 1450.0
        target = 1600.0
        
        ai_signal = {"score": 80.0, "confidence": 0.88}
        ml_signal = {
            "score": 100.0,
            "confidence": 0.90,
            "prediction": {
                "direction": "BUY",
                "entry_price": entry,
                "stop_loss": stop_loss,
                "targets": [target, 1650.0, 1700.0],
            },
        }
        
        suggestion = await correlation_engine._compute_consensus(
            db=mock_db,
            correlation_id=uuid4(),
            trigger_type="SCANNER_ANOMALY",
            trigger_timestamp=datetime.now(timezone.utc),
            scanner_signal=scanner_signal_buy,
            ai_signal=ai_signal,
            ml_signal=ml_signal,
            latencies={"scanner_ms": 10, "ai_ms": 50, "ml_ms": 40, "total_ms": 100},
        )
        
        risk = abs(entry - stop_loss)  # 50
        reward = abs(target - entry)  # 100
        expected_rr = reward / risk  # 2.0
        
        assert suggestion.entry_price == Decimal(str(entry))
        assert suggestion.stop_loss == Decimal(str(stop_loss))
        assert suggestion.take_profit_1 == Decimal(str(target))
        assert float(suggestion.risk_reward_ratio) == pytest.approx(expected_rr, rel=0.01)

    @pytest.mark.asyncio
    async def test_expiry_calculation(
        self,
        correlation_engine,
        mock_db,
        scanner_signal_buy,
    ):
        """Suggestion expires 24 hours after generation."""
        trigger_time = datetime.now(timezone.utc)
        
        ai_signal = {"score": 80.0, "confidence": 0.88}
        ml_signal = {
            "score": 100.0,
            "confidence": 0.90,
            "prediction": {
                "direction": "BUY",
                "entry_price": 1500.0,
                "stop_loss": 1450.0,
                "targets": [1600.0],
            },
        }
        
        suggestion = await correlation_engine._compute_consensus(
            db=mock_db,
            correlation_id=uuid4(),
            trigger_type="SCANNER_ANOMALY",
            trigger_timestamp=trigger_time,
            scanner_signal=scanner_signal_buy,
            ai_signal=ai_signal,
            ml_signal=ml_signal,
            latencies={"scanner_ms": 10, "ai_ms": 50, "ml_ms": 40, "total_ms": 100},
        )
        
        expected_expiry = trigger_time + timedelta(hours=24)
        assert suggestion.generated_at == trigger_time
        assert suggestion.expires_at == expected_expiry


# ============================================================================
# TEST LATENCY & REDIS
# ============================================================================

class TestLatencyAndRedis:
    """Test performance monitoring and real-time notifications."""

    @pytest.mark.asyncio
    async def test_latency_tracking(
        self,
        correlation_engine,
        mock_db,
        scanner_signal_buy,
    ):
        """Latencies recorded in EventCorrelation."""
        ai_signal = {"score": 80.0, "confidence": 0.88}
        ml_signal = {
            "score": 100.0,
            "confidence": 0.90,
            "prediction": {
                "direction": "BUY",
                "entry_price": 1500.0,
                "stop_loss": 1450.0,
                "targets": [1600.0],
            },
        }
        
        latencies = {
            "scanner_ms": 12,
            "ai_ms": 48,
            "ml_ms": 35,
            "total_ms": 95,
        }
        
        await correlation_engine._compute_consensus(
            db=mock_db,
            correlation_id=uuid4(),
            trigger_type="SCANNER_ANOMALY",
            trigger_timestamp=datetime.now(timezone.utc),
            scanner_signal=scanner_signal_buy,
            ai_signal=ai_signal,
            ml_signal=ml_signal,
            latencies=latencies,
        )
        
        # Check EventCorrelation was created with latencies
        calls = [call for call in mock_db.add.call_args_list]
        correlation_call = [c for c in calls if isinstance(c[0][0], EventCorrelation)][0]
        correlation = correlation_call[0][0]
        
        assert correlation.scanner_response_ms == 12
        assert correlation.ai_response_ms == 48
        assert correlation.ml_response_ms == 35
        assert correlation.total_latency_ms == 95

    @pytest.mark.asyncio
    async def test_redis_publish_on_success(
        self,
        correlation_engine,
        mock_db,
        mock_redis,
        scanner_signal_buy,
    ):
        """Successful suggestion publishes to Redis on the SUGGESTIONS_NEW channel."""
        # available=True + event_count=2 → standard 3-weight formula (no redistribution).
        ai_signal = {"score": 80.0, "confidence": 0.88, "available": True, "event_count": 2}
        ml_signal = {
            "score": 100.0,
            "confidence": 0.90,
            "prediction": {
                "direction": "BUY",
                "entry_price": 1500.0,
                "stop_loss": 1450.0,
                "targets": [1600.0],
            },
        }

        suggestion = await correlation_engine._compute_consensus(
            db=mock_db,
            correlation_id=uuid4(),
            trigger_type="SCANNER_ANOMALY",
            trigger_timestamp=datetime.now(timezone.utc),
            scanner_signal=scanner_signal_buy,
            ai_signal=ai_signal,
            ml_signal=ml_signal,
            latencies={"scanner_ms": 10, "ai_ms": 50, "ml_ms": 40, "total_ms": 100},
        )

        assert suggestion is not None

        # The engine publishes to three channels on success:
        #   1. LLM_EXPLANATION_PENDING  (fire-and-forget explanation trigger)
        #   2. SUGGESTIONS_NEW          (full JSON payload for WebSocket clients)
        #   3. CORRELATIONS_COMPLETED   (ML-activity live feed)
        # Verify that SUGGESTIONS_NEW was published with a JSON payload that
        # includes the correct suggestion_id — channel order is an implementation
        # detail so we match by channel name, not call index.
        published_channels = [
            call.args[0] for call in mock_redis.publish.await_args_list
        ]
        assert "cai:suggestions:new" in published_channels, (
            f"Expected SUGGESTIONS_NEW publish; got channels: {published_channels}"
        )
        # Find the SUGGESTIONS_NEW call and validate the payload shape.
        suggestions_new_call = next(
            call for call in mock_redis.publish.await_args_list
            if call.args[0] == "cai:suggestions:new"
        )
        payload = json.loads(suggestions_new_call.args[1])
        assert payload["suggestion_id"] == str(suggestion.suggestion_id)
        assert payload["signal_direction"] == "BUY"

    @pytest.mark.asyncio
    async def test_redis_publish_failure_handled(
        self,
        correlation_engine,
        mock_db,
        mock_redis,
        scanner_signal_buy,
    ):
        """Redis publish failure doesn't break suggestion creation."""
        mock_redis.publish.side_effect = Exception("Redis connection lost")
        
        ai_signal = {"score": 80.0, "confidence": 0.88}
        ml_signal = {
            "score": 100.0,
            "confidence": 0.90,
            "prediction": {
                "direction": "BUY",
                "entry_price": 1500.0,
                "stop_loss": 1450.0,
                "targets": [1600.0],
            },
        }
        
        # Should not raise exception
        suggestion = await correlation_engine._compute_consensus(
            db=mock_db,
            correlation_id=uuid4(),
            trigger_type="SCANNER_ANOMALY",
            trigger_timestamp=datetime.now(timezone.utc),
            scanner_signal=scanner_signal_buy,
            ai_signal=ai_signal,
            ml_signal=ml_signal,
            latencies={"scanner_ms": 10, "ai_ms": 50, "ml_ms": 40, "total_ms": 100},
        )
        
        assert suggestion is not None  # Suggestion still created
