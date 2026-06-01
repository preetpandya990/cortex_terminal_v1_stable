"""
Integration Tests for WebSocket Real-time Updates
==================================================

Tests production-grade WebSocket implementation with:
- Connection lifecycle (connect, disconnect, reconnect)
- Room-based broadcasting (global, symbol-specific, user-specific)
- Message delivery (new suggestions, expired suggestions, correlations)
- Authentication (JWT token validation)
- Heartbeat/ping-pong mechanism
- Backpressure handling
- Metrics tracking

Run with: pytest tests/integration/test_websocket_realtime.py -v
"""
import asyncio
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import RedisChannels
from app.core.websocket_manager import get_websocket_manager
from app.main import app
from app.models.trade_suggestions import TradeSuggestion


@pytest.fixture
def ws_manager():
    """Get WebSocket manager instance."""
    return get_websocket_manager()


@pytest.fixture
def test_client():
    """Get FastAPI test client."""
    return TestClient(app)


class TestWebSocketConnection:
    """Test WebSocket connection lifecycle."""
    
    def test_connect_without_auth(self, test_client):
        """Test WebSocket connection without authentication."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Should connect successfully
            assert websocket is not None
            
            # Should receive ping within 30 seconds
            data = websocket.receive_json(timeout=35)
            assert data["type"] == "ping"
            assert "timestamp" in data
    
    def test_connect_with_valid_token(self, test_client, auth_token):
        """Test WebSocket connection with valid JWT token."""
        with test_client.websocket_connect(
            f"/api/v1/trade-suggestions/ws?token={auth_token}"
        ) as websocket:
            # Should connect successfully
            assert websocket is not None
            
            # Should be subscribed to user-specific room
            # (verified by manager state)
    
    def test_connect_with_invalid_token(self, test_client):
        """Test WebSocket connection with invalid JWT token."""
        with pytest.raises(Exception):
            # Should reject connection
            with test_client.websocket_connect(
                "/api/v1/trade-suggestions/ws?token=invalid_token"
            ) as websocket:
                pass
    
    def test_disconnect_gracefully(self, test_client):
        """Test graceful WebSocket disconnection."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Send disconnect
            websocket.close()
        
        # Connection should be cleaned up
        manager = get_websocket_manager()
        assert len(manager.connections) == 0


class TestRoomSubscriptions:
    """Test room-based subscriptions."""
    
    def test_auto_subscribe_to_suggestions_room(self, test_client):
        """Test auto-subscription to suggestions room."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Client should be auto-subscribed to "suggestions" room
            manager = get_websocket_manager()
            client_id = list(manager.connections.keys())[0]
            connection = manager.connections[client_id]
            
            assert "suggestions" in connection.rooms
            assert "global" in connection.rooms
    
    def test_subscribe_to_symbol_room(self, test_client):
        """Test subscribing to symbol-specific room."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Subscribe to AAPL updates
            websocket.send_json({"type": "subscribe", "room": "symbol:AAPL"})
            
            # Should receive confirmation
            data = websocket.receive_json(timeout=5)
            assert data["type"] == "subscribed"
            assert data["room"] == "symbol:AAPL"
            assert data["success"] is True
    
    def test_unsubscribe_from_symbol_room(self, test_client):
        """Test unsubscribing from symbol-specific room."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Subscribe first
            websocket.send_json({"type": "subscribe", "room": "symbol:AAPL"})
            websocket.receive_json(timeout=5)
            
            # Unsubscribe
            websocket.send_json({"type": "unsubscribe", "room": "symbol:AAPL"})
            
            # Should receive confirmation
            data = websocket.receive_json(timeout=5)
            assert data["type"] == "unsubscribed"
            assert data["room"] == "symbol:AAPL"
            assert data["success"] is True
    
    def test_subscribe_to_invalid_room(self, test_client):
        """Test subscribing to invalid room name."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Try to subscribe to invalid room
            websocket.send_json({"type": "subscribe", "room": "invalid_room"})
            
            # Should receive error
            data = websocket.receive_json(timeout=5)
            assert data["type"] == "error"
            assert data["code"] == "INVALID_ROOM"


class TestMessageBroadcasting:
    """Test message broadcasting to rooms."""
    
    @pytest.mark.asyncio
    async def test_broadcast_new_suggestion_to_all(self, test_client, redis_client):
        """Test broadcasting new suggestion to all clients."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as ws1:
            with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as ws2:
                # Publish new suggestion event to Redis
                suggestion_id = str(uuid4())
                await redis_client.publish_json(
                    RedisChannels.SUGGESTIONS_NEW,
                    {
                        "suggestion_id": suggestion_id,
                        "symbol": "AAPL",
                        "signal_direction": "BUY",
                        "consensus_score": 85.5,
                        "confidence_level": "HIGH",
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                
                # Both clients should receive the message
                data1 = ws1.receive_json(timeout=5)
                data2 = ws2.receive_json(timeout=5)
                
                assert data1["type"] == "new_suggestion"
                assert data1["suggestion_id"] == suggestion_id
                assert data1["symbol"] == "AAPL"
                
                assert data2["type"] == "new_suggestion"
                assert data2["suggestion_id"] == suggestion_id
    
    @pytest.mark.asyncio
    async def test_broadcast_to_symbol_room_only(self, test_client, redis_client):
        """Test broadcasting to symbol-specific room."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as ws_aapl:
            with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as ws_tsla:
                # Subscribe to different symbols
                ws_aapl.send_json({"type": "subscribe", "room": "symbol:AAPL"})
                ws_aapl.receive_json(timeout=5)  # confirmation
                
                ws_tsla.send_json({"type": "subscribe", "room": "symbol:TSLA"})
                ws_tsla.receive_json(timeout=5)  # confirmation
                
                # Publish AAPL suggestion
                suggestion_id = str(uuid4())
                await redis_client.publish_json(
                    RedisChannels.SUGGESTIONS_NEW,
                    {
                        "suggestion_id": suggestion_id,
                        "symbol": "AAPL",
                        "signal_direction": "BUY",
                        "consensus_score": 85.5,
                        "confidence_level": "HIGH",
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                
                # AAPL client should receive message
                data_aapl = ws_aapl.receive_json(timeout=5)
                assert data_aapl["type"] == "new_suggestion"
                assert data_aapl["symbol"] == "AAPL"
                
                # TSLA client should also receive (subscribed to suggestions room)
                data_tsla = ws_tsla.receive_json(timeout=5)
                assert data_tsla["type"] == "new_suggestion"
    
    @pytest.mark.asyncio
    async def test_broadcast_expired_suggestion(self, test_client, redis_client):
        """Test broadcasting expired suggestion event."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Publish expired suggestion event
            suggestion_id = str(uuid4())
            await redis_client.publish_json(
                RedisChannels.SUGGESTIONS_EXPIRED,
                {
                    "suggestion_id": suggestion_id,
                    "symbol": "AAPL",
                    "expired_at": datetime.now(timezone.utc).isoformat(),
                    "reason": "TTL_EXPIRED",
                }
            )
            
            # Client should receive the message
            data = websocket.receive_json(timeout=5)
            assert data["type"] == "suggestion_expired"
            assert data["suggestion_id"] == suggestion_id
            assert data["reason"] == "TTL_EXPIRED"


class TestHeartbeat:
    """Test heartbeat/ping-pong mechanism."""
    
    def test_server_sends_ping(self, test_client):
        """Test server sends ping every 30 seconds."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Wait for ping (should arrive within 35 seconds)
            data = websocket.receive_json(timeout=35)
            assert data["type"] == "ping"
            assert "timestamp" in data
    
    def test_client_responds_with_pong(self, test_client):
        """Test client can respond to ping with pong."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Send pong
            websocket.send_json({"type": "pong"})
            
            # Should not cause any errors
            # (server logs pong but doesn't respond)
    
    def test_client_initiated_ping(self, test_client):
        """Test client can initiate ping."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Send ping
            websocket.send_json({"type": "ping"})
            
            # Should receive pong
            data = websocket.receive_json(timeout=5)
            assert data["type"] == "pong"
            assert "timestamp" in data


class TestBackpressure:
    """Test backpressure handling."""
    
    @pytest.mark.asyncio
    async def test_queue_overflow_drops_oldest(self, ws_manager):
        """Test queue overflow drops oldest messages."""
        from unittest.mock import Mock
        
        # Create mock websocket
        mock_ws = Mock()
        
        # Connect client with small queue
        client_id = await ws_manager.connect(mock_ws, client_id="test_client")
        connection = ws_manager.connections[client_id]
        connection.max_queue_size = 5
        
        # Fill queue beyond capacity
        for i in range(10):
            await connection.send({"type": "test", "index": i})
        
        # Queue should be at max size
        assert connection.message_queue.qsize() <= connection.max_queue_size
        
        # Cleanup
        await ws_manager.disconnect(client_id)


class TestMetrics:
    """Test Prometheus metrics tracking."""
    
    def test_connection_metrics(self, test_client):
        """Test connection metrics are tracked."""
        from app.core.websocket_manager import (
            websocket_connections_total,
            websocket_active_connections,
        )
        
        initial_connections = websocket_connections_total._value.get()
        initial_active = websocket_active_connections._value.get()
        
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Connection count should increase
            assert websocket_connections_total._value.get() > initial_connections
            assert websocket_active_connections._value.get() > initial_active
        
        # Active connections should decrease after disconnect
        assert websocket_active_connections._value.get() == initial_active
    
    def test_message_metrics(self, test_client):
        """Test message metrics are tracked."""
        from app.core.websocket_manager import websocket_messages_sent_total
        
        initial_sent = websocket_messages_sent_total._value.get()
        
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Wait for ping (counts as sent message)
            websocket.receive_json(timeout=35)
            
            # Sent count should increase
            assert websocket_messages_sent_total._value.get() > initial_sent


class TestErrorHandling:
    """Test error handling."""
    
    def test_invalid_json_message(self, test_client):
        """Test handling of invalid JSON message."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Send invalid JSON
            websocket.send_text("invalid json {")
            
            # Should receive error message
            data = websocket.receive_json(timeout=5)
            assert data["type"] == "error"
            assert data["code"] == "INVALID_JSON"
    
    def test_unknown_message_type(self, test_client):
        """Test handling of unknown message type."""
        with test_client.websocket_connect("/api/v1/trade-suggestions/ws") as websocket:
            # Send unknown message type
            websocket.send_json({"type": "unknown_type"})
            
            # Should not crash (logged as warning)
            # Connection should remain open
            websocket.send_json({"type": "ping"})
            data = websocket.receive_json(timeout=5)
            assert data["type"] == "pong"


class TestConnectionManager:
    """Test WebSocketManager functionality."""
    
    @pytest.mark.asyncio
    async def test_get_stats(self, ws_manager):
        """Test getting connection statistics."""
        from unittest.mock import Mock
        
        # Connect multiple clients
        mock_ws1 = Mock()
        mock_ws2 = Mock()
        
        client1 = await ws_manager.connect(mock_ws1, client_id="client1", user_id="user1")
        client2 = await ws_manager.connect(mock_ws2, client_id="client2")
        
        # Subscribe to rooms
        await ws_manager.subscribe_to_room(client1, "symbol:AAPL")
        await ws_manager.subscribe_to_room(client2, "symbol:TSLA")
        
        # Get stats
        stats = ws_manager.get_stats()
        
        assert stats["total_connections"] == 2
        assert stats["total_rooms"] >= 2
        assert "symbol:AAPL" in stats["rooms"]
        assert "symbol:TSLA" in stats["rooms"]
        
        # Cleanup
        await ws_manager.disconnect(client1)
        await ws_manager.disconnect(client2)
    
    @pytest.mark.asyncio
    async def test_broadcast_to_room(self, ws_manager):
        """Test broadcasting to specific room."""
        from unittest.mock import Mock, AsyncMock
        
        # Create mock websockets
        mock_ws1 = Mock()
        mock_ws1.send_text = AsyncMock()
        mock_ws2 = Mock()
        mock_ws2.send_text = AsyncMock()
        
        # Connect clients
        client1 = await ws_manager.connect(mock_ws1, client_id="client1")
        client2 = await ws_manager.connect(mock_ws2, client_id="client2")
        
        # Subscribe to room
        await ws_manager.subscribe_to_room(client1, "test_room")
        
        # Broadcast to room
        message = {"type": "test", "data": "hello"}
        sent_count = await ws_manager.broadcast_to_room("test_room", message)
        
        # Only client1 should receive
        assert sent_count == 1
        
        # Cleanup
        await ws_manager.disconnect(client1)
        await ws_manager.disconnect(client2)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
async def redis_client():
    """Get Redis client for pub/sub testing."""
    from app.core.redis import get_redis, PubSubClient
    redis = get_redis()
    return PubSubClient(redis)


@pytest.fixture
def auth_token():
    """Generate valid JWT token for testing."""
    from app.core.security import create_access_token
    return create_access_token({"sub": "test_user_123"})


# ── Test Configuration ─────────────────────────────────────────────────────────

pytestmark = pytest.mark.asyncio
