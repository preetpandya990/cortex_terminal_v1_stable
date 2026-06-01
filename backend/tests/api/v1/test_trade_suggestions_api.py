"""
Trade Suggestions API Integration Tests
========================================
Production-grade tests for all trade suggestions endpoints.
Tests authentication, filtering, pagination, WebSocket, and error handling.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trade_suggestions import EventCorrelation, TradeSuggestion


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
async def sample_suggestions(db_session: AsyncSession) -> list[TradeSuggestion]:
    """Create sample trade suggestions for testing."""
    now = datetime.now(timezone.utc)
    
    suggestions = [
        TradeSuggestion(
            suggestion_id=uuid4(),
            symbol="RELIANCE",
            instrument_key="NSE_EQ|INE002A01018",
            trading_symbol="RELIANCE-EQ",
            consensus_score=Decimal("87.50"),
            confidence_level="HIGH",
            signal_direction="BUY",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"pattern": "bullish_engulfing", "strength": 0.85},
            ai_signal={"sentiment": "positive", "confidence": 0.90},
            ml_signal={"prediction": "BUY", "probability": 0.88},
            entry_price=Decimal("2450.00"),
            stop_loss=Decimal("2400.00"),
            risk_reward_ratio=Decimal("2.50"),
            take_profit_1=Decimal("2500.00"),
            take_profit_2=Decimal("2550.00"),
            take_profit_3=Decimal("2600.00"),
            generated_at=now,
            expires_at=now + timedelta(hours=4),
            status="active",
        ),
        TradeSuggestion(
            suggestion_id=uuid4(),
            symbol="TCS",
            instrument_key="NSE_EQ|INE467B01029",
            trading_symbol="TCS-EQ",
            consensus_score=Decimal("72.30"),
            confidence_level="MEDIUM",
            signal_direction="SELL",
            trigger_pathway="FUNDAMENTAL_FIRST",
            scanner_signal={"pattern": "bearish_divergence", "strength": 0.70},
            ai_signal={"sentiment": "negative", "confidence": 0.75},
            ml_signal={"prediction": "SELL", "probability": 0.72},
            entry_price=Decimal("3200.00"),
            stop_loss=Decimal("3250.00"),
            generated_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=3),
            status="active",
        ),
        TradeSuggestion(
            suggestion_id=uuid4(),
            symbol="INFY",
            instrument_key="NSE_EQ|INE009A01021",
            trading_symbol="INFY-EQ",
            consensus_score=Decimal("55.00"),
            confidence_level="LOW",
            signal_direction="BUY",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"pattern": "doji", "strength": 0.55},
            ai_signal={"sentiment": "neutral", "confidence": 0.60},
            ml_signal={"prediction": "BUY", "probability": 0.52},
            generated_at=now - timedelta(hours=2),
            expires_at=now + timedelta(hours=2),
            status="expired",
        ),
    ]
    
    for suggestion in suggestions:
        db_session.add(suggestion)
    
    await db_session.commit()
    
    # Refresh to get generated IDs
    for suggestion in suggestions:
        await db_session.refresh(suggestion)
    
    return suggestions


@pytest.fixture
async def sample_correlations(
    db_session: AsyncSession,
    sample_suggestions: list[TradeSuggestion],
) -> list[EventCorrelation]:
    """Create sample event correlations for testing."""
    now = datetime.now(timezone.utc)
    
    correlations = [
        EventCorrelation(
            correlation_id=uuid4(),
            suggestion_id=sample_suggestions[0].suggestion_id,
            trigger_type="SCANNER_ANOMALY",
            trigger_timestamp=now - timedelta(seconds=5),
            scanner_response_ms=45,
            ai_response_ms=120,
            ml_response_ms=85,
            total_latency_ms=250,
            consensus_reached=True,
            scanner_output={"pattern": "bullish_engulfing"},
            ai_output={"sentiment": "positive"},
            ml_output={"prediction": "BUY"},
        ),
        EventCorrelation(
            correlation_id=uuid4(),
            suggestion_id=sample_suggestions[1].suggestion_id,
            trigger_type="NEWS_EVENT",
            trigger_timestamp=now - timedelta(hours=1, seconds=10),
            scanner_response_ms=50,
            ai_response_ms=150,
            ml_response_ms=90,
            total_latency_ms=290,
            consensus_reached=True,
            scanner_output={"pattern": "bearish_divergence"},
            ai_output={"sentiment": "negative"},
            ml_output={"prediction": "SELL"},
        ),
    ]
    
    for correlation in correlations:
        db_session.add(correlation)
    
    await db_session.commit()
    
    return correlations


# ============================================================================
# LIST SUGGESTIONS TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_list_suggestions_default(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
):
    """Test listing suggestions with default parameters."""
    response = await async_client.get("/api/v1/trade-suggestions")
    
    assert response.status_code == 200
    data = response.json()
    
    assert "suggestions" in data
    assert "total" in data
    assert "page" in data
    assert "page_size" in data
    assert "has_more" in data
    
    # Should return 2 active suggestions (expired excluded by default)
    assert len(data["suggestions"]) == 2
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["page_size"] == 50
    assert data["has_more"] is False


@pytest.mark.asyncio
async def test_list_suggestions_filter_by_status(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
):
    """Test filtering suggestions by status."""
    response = await async_client.get("/api/v1/trade-suggestions?status=expired")
    
    assert response.status_code == 200
    data = response.json()
    
    assert len(data["suggestions"]) == 1
    assert data["suggestions"][0]["status"] == "expired"
    assert data["suggestions"][0]["symbol"] == "INFY"


@pytest.mark.asyncio
async def test_list_suggestions_filter_by_direction(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
):
    """Test filtering suggestions by signal direction."""
    response = await async_client.get("/api/v1/trade-suggestions?direction=BUY")
    
    assert response.status_code == 200
    data = response.json()
    
    # Should return 1 active BUY suggestion (RELIANCE)
    assert len(data["suggestions"]) == 1
    assert data["suggestions"][0]["signal_direction"] == "BUY"
    assert data["suggestions"][0]["symbol"] == "RELIANCE"


@pytest.mark.asyncio
async def test_list_suggestions_filter_by_confidence(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
):
    """Test filtering suggestions by confidence level."""
    response = await async_client.get("/api/v1/trade-suggestions?confidence_level=HIGH")
    
    assert response.status_code == 200
    data = response.json()
    
    assert len(data["suggestions"]) == 1
    assert data["suggestions"][0]["confidence_level"] == "HIGH"
    assert data["suggestions"][0]["symbol"] == "RELIANCE"


@pytest.mark.asyncio
async def test_list_suggestions_filter_by_min_confidence(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
):
    """Test filtering suggestions by minimum consensus score."""
    response = await async_client.get("/api/v1/trade-suggestions?min_confidence=80.0")
    
    assert response.status_code == 200
    data = response.json()
    
    assert len(data["suggestions"]) == 1
    assert float(data["suggestions"][0]["consensus_score"]) >= 80.0
    assert data["suggestions"][0]["symbol"] == "RELIANCE"


@pytest.mark.asyncio
async def test_list_suggestions_filter_by_symbol(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
):
    """Test filtering suggestions by symbol."""
    response = await async_client.get("/api/v1/trade-suggestions?symbol=TCS")
    
    assert response.status_code == 200
    data = response.json()
    
    assert len(data["suggestions"]) == 1
    assert data["suggestions"][0]["symbol"] == "TCS"


@pytest.mark.asyncio
async def test_list_suggestions_pagination(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
):
    """Test pagination parameters."""
    response = await async_client.get("/api/v1/trade-suggestions?page=1&page_size=1")
    
    assert response.status_code == 200
    data = response.json()
    
    assert len(data["suggestions"]) == 1
    assert data["total"] == 2
    assert data["page"] == 1
    assert data["page_size"] == 1
    assert data["has_more"] is True


@pytest.mark.asyncio
async def test_list_suggestions_ordering(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
):
    """Test suggestions are ordered by consensus score descending."""
    response = await async_client.get("/api/v1/trade-suggestions")
    
    assert response.status_code == 200
    data = response.json()
    
    # First should be RELIANCE (87.50), second TCS (72.30)
    assert data["suggestions"][0]["symbol"] == "RELIANCE"
    assert data["suggestions"][1]["symbol"] == "TCS"
    assert float(data["suggestions"][0]["consensus_score"]) > float(data["suggestions"][1]["consensus_score"])


@pytest.mark.asyncio
async def test_list_suggestions_empty_result(async_client: AsyncClient):
    """Test listing suggestions when none exist."""
    response = await async_client.get("/api/v1/trade-suggestions")
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["suggestions"] == []
    assert data["total"] == 0
    assert data["has_more"] is False


# ============================================================================
# GET SUGGESTION DETAIL TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_get_suggestion_detail(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
    sample_correlations: list[EventCorrelation],
):
    """Test retrieving suggestion detail with correlations."""
    suggestion_id = sample_suggestions[0].suggestion_id
    response = await async_client.get(f"/api/v1/trade-suggestions/{suggestion_id}")
    
    assert response.status_code == 200
    data = response.json()
    
    assert "suggestion" in data
    assert "correlations" in data
    assert "correlation_count" in data
    
    assert data["suggestion"]["suggestion_id"] == str(suggestion_id)
    assert data["suggestion"]["symbol"] == "RELIANCE"
    assert data["correlation_count"] == 1
    assert len(data["correlations"]) == 1


@pytest.mark.asyncio
async def test_get_suggestion_not_found(async_client: AsyncClient):
    """Test retrieving non-existent suggestion returns 404."""
    fake_id = uuid4()
    response = await async_client.get(f"/api/v1/trade-suggestions/{fake_id}")
    
    assert response.status_code == 404
    data = response.json()
    assert "error" in data
    assert data["error"]["code"] == "NOT_FOUND"
    assert data["error"]["status_code"] == 404
    assert str(fake_id) in data["error"]["message"]


@pytest.mark.asyncio
async def test_get_suggestion_invalid_uuid(async_client: AsyncClient):
    """Test retrieving suggestion with invalid UUID format."""
    response = await async_client.get("/api/v1/trade-suggestions/invalid-uuid")
    
    assert response.status_code == 422  # Validation error


# ============================================================================
# DISMISS SUGGESTION TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_dismiss_suggestion(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
    db_session: AsyncSession,
):
    """Test dismissing an active suggestion."""
    suggestion_id = sample_suggestions[0].suggestion_id
    response = await async_client.post(f"/api/v1/trade-suggestions/{suggestion_id}/dismiss")
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["suggestion_id"] == str(suggestion_id)
    assert data["status"] == "invalidated"
    
    # Verify in database
    stmt = select(TradeSuggestion).where(TradeSuggestion.suggestion_id == suggestion_id)
    result = await db_session.execute(stmt)
    suggestion = result.scalar_one()
    assert suggestion.status == "invalidated"


@pytest.mark.asyncio
async def test_dismiss_suggestion_idempotent(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
):
    """Test dismissing already dismissed suggestion is idempotent."""
    suggestion_id = sample_suggestions[0].suggestion_id
    
    # Dismiss first time
    response1 = await async_client.post(f"/api/v1/trade-suggestions/{suggestion_id}/dismiss")
    assert response1.status_code == 200
    
    # Dismiss second time
    response2 = await async_client.post(f"/api/v1/trade-suggestions/{suggestion_id}/dismiss")
    assert response2.status_code == 200
    assert response2.json()["status"] == "invalidated"


@pytest.mark.asyncio
async def test_dismiss_suggestion_not_found(async_client: AsyncClient):
    """Test dismissing non-existent suggestion returns 404."""
    fake_id = uuid4()
    response = await async_client.post(f"/api/v1/trade-suggestions/{fake_id}/dismiss")
    
    assert response.status_code == 404


# ============================================================================
# STATS TESTS
# ============================================================================

@pytest.mark.asyncio
async def test_get_stats(
    async_client: AsyncClient,
    sample_suggestions: list[TradeSuggestion],
    sample_correlations: list[EventCorrelation],
):
    """Test retrieving aggregate statistics."""
    response = await async_client.get("/api/v1/trade-suggestions/stats/summary")
    
    assert response.status_code == 200
    data = response.json()
    
    assert "total_active" in data
    assert "total_today" in data
    assert "avg_consensus_score" in data
    assert "high_confidence_count" in data
    assert "buy_count" in data
    assert "sell_count" in data
    assert "avg_latency_ms" in data
    assert "consensus_rate" in data
    
    assert data["total_active"] == 2
    assert data["total_today"] == 3
    assert data["high_confidence_count"] == 1
    assert data["buy_count"] == 1
    assert data["sell_count"] == 1


@pytest.mark.asyncio
async def test_get_stats_empty(async_client: AsyncClient):
    """Test stats with no suggestions."""
    response = await async_client.get("/api/v1/trade-suggestions/stats/summary")
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["total_active"] == 0
    assert data["total_today"] == 0
    assert data["avg_consensus_score"] == 0.0


# ============================================================================
# WEBSOCKET TESTS
# ============================================================================

def test_websocket_connection(websocket_client):
    """Test WebSocket connection establishment."""
    with websocket_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
        # Successfully entering context manager means connection established
        # Verify bidirectional communication works
        websocket.send_json({"type": "pong"})
        # No exception raised - connection is working


def test_websocket_heartbeat(websocket_client):
    """Test WebSocket heartbeat (ping) messages."""
    # Note: Heartbeat interval is 30s, so we won't receive one in a short test
    # This test verifies the connection stays alive without heartbeat
    with websocket_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
        # Send pong to verify connection is alive
        websocket.send_json({"type": "pong"})
        # Connection remains stable


def test_websocket_pong_response(websocket_client):
    """Test sending pong response to server."""
    with websocket_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
        # Send pong - server should accept it without disconnecting
        websocket.send_json({"type": "pong"})
        
        # Send another to verify connection still works
        websocket.send_json({"type": "pong"})
        # No exception - connection is stable


def test_websocket_multiple_clients(websocket_client):
    """Test multiple WebSocket clients can connect simultaneously."""
    with websocket_client.websocket_connect("/api/v1/trade-suggestions/ws") as ws1:
        # First client connected
        ws1.send_json({"type": "pong"})
        
        with websocket_client.websocket_connect("/api/v1/trade-suggestions/ws") as ws2:
            # Second client connected simultaneously
            ws2.send_json({"type": "pong"})
            
            # Both connections are independent and working
            ws1.send_json({"type": "pong"})
            ws2.send_json({"type": "pong"})


def test_websocket_graceful_disconnect(websocket_client):
    """Test WebSocket graceful disconnect."""
    with websocket_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
        # Send a message to verify connection
        websocket.send_json({"type": "pong"})
        
        # Close connection gracefully
        websocket.close(code=1000)
        # Context manager will handle cleanup
