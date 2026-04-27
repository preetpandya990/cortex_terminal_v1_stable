"""
Cortex AI — Production-Grade WebSocket Connection Manager
==========================================================

Features (2026 Best Practices):
- Connection lifecycle management (connect, disconnect, cleanup)
- Room-based broadcasting (per-symbol, per-user subscriptions)
- Heartbeat/ping-pong for connection health
- Backpressure handling with bounded queues
- Connection metrics (Prometheus)
- Structured logging with context
- Graceful shutdown handling
- Memory-efficient connection tracking

References:
- https://blog.greeden.me/en/2025/10/28/weaponizing-real-time-websocket-sse-notifications-with-fastapi
- https://markaicode.com/real-time-ai-dashboard-react-websocket-fastapi/
"""
import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import WebSocket, WebSocketDisconnect
from prometheus_client import Counter, Gauge, Histogram

logger = logging.getLogger(__name__)

# ── Prometheus Metrics ─────────────────────────────────────────────────────────
websocket_connections_total = Counter(
    "websocket_connections_total",
    "Total WebSocket connections established",
    ["endpoint"],
)

websocket_disconnections_total = Counter(
    "websocket_disconnections_total",
    "Total WebSocket disconnections",
    ["endpoint", "reason"],
)

websocket_active_connections = Gauge(
    "websocket_active_connections",
    "Current number of active WebSocket connections",
    ["endpoint"],
)

websocket_messages_sent_total = Counter(
    "websocket_messages_sent_total",
    "Total WebSocket messages sent",
    ["endpoint", "message_type"],
)

websocket_messages_received_total = Counter(
    "websocket_messages_received_total",
    "Total WebSocket messages received",
    ["endpoint", "message_type"],
)

websocket_message_send_duration_seconds = Histogram(
    "websocket_message_send_duration_seconds",
    "WebSocket message send duration in seconds",
    ["endpoint", "message_type"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

websocket_broadcast_duration_seconds = Histogram(
    "websocket_broadcast_duration_seconds",
    "WebSocket broadcast duration in seconds",
    ["endpoint", "room"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

websocket_queue_size = Gauge(
    "websocket_queue_size",
    "Current WebSocket message queue size per connection",
    ["endpoint", "client_id"],
)


# ── Connection Class ───────────────────────────────────────────────────────────
class WebSocketConnection:
    """
    Represents a single WebSocket connection with message queue and metadata.
    
    Features:
    - Bounded message queue (prevents memory exhaustion)
    - Connection metadata (user_id, client_id, rooms)
    - Last activity tracking (for idle timeout)
    - Backpressure handling (drops oldest messages when full)
    """
    
    def __init__(
        self,
        websocket: WebSocket,
        client_id: str,
        user_id: str | None = None,
        max_queue_size: int = 100,
    ):
        self.websocket = websocket
        self.client_id = client_id
        self.user_id = user_id
        self.connected_at = time.time()
        self.last_activity = time.time()
        self.rooms: set[str] = set()
        
        # Bounded queue for backpressure handling
        self.message_queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self.max_queue_size = max_queue_size
        
        # Send task for async message delivery
        self._send_task: asyncio.Task | None = None
    
    async def send(self, message: dict[str, Any]) -> None:
        """
        Send message to client (non-blocking).
        
        If queue is full, drops oldest message (backpressure handling).
        """
        try:
            self.message_queue.put_nowait(message)
        except asyncio.QueueFull:
            # Drop oldest message (FIFO)
            try:
                self.message_queue.get_nowait()
                self.message_queue.put_nowait(message)
                logger.warning(
                    f"Queue full for client {self.client_id}, dropped oldest message"
                )
            except Exception as e:
                logger.error(f"Failed to handle queue overflow: {e}")
    
    async def _send_loop(self) -> None:
        """Background task that sends queued messages to the WebSocket."""
        try:
            while True:
                message = await self.message_queue.get()
                
                try:
                    start_time = time.time()
                    await self.websocket.send_text(json.dumps(message))
                    duration = time.time() - start_time
                    
                    # Track metrics
                    websocket_message_send_duration_seconds.labels(
                        endpoint="/ws",
                        message_type=message.get("type", "unknown"),
                    ).observe(duration)
                    
                    websocket_messages_sent_total.labels(
                        endpoint="/ws",
                        message_type=message.get("type", "unknown"),
                    ).inc()
                    
                    self.last_activity = time.time()
                
                except Exception as e:
                    logger.error(
                        f"Failed to send message to client {self.client_id}: {e}"
                    )
                    break
        
        except asyncio.CancelledError:
            logger.debug(f"Send loop cancelled for client {self.client_id}")
            raise
    
    def start_send_loop(self) -> None:
        """Start the background send loop."""
        if self._send_task is None or self._send_task.done():
            self._send_task = asyncio.create_task(self._send_loop())
    
    async def stop_send_loop(self) -> None:
        """Stop the background send loop."""
        if self._send_task and not self._send_task.done():
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
    
    def join_room(self, room: str) -> None:
        """Add connection to a room."""
        self.rooms.add(room)
        logger.debug(f"Client {self.client_id} joined room {room}")
    
    def leave_room(self, room: str) -> None:
        """Remove connection from a room."""
        self.rooms.discard(room)
        logger.debug(f"Client {self.client_id} left room {room}")
    
    def get_connection_duration(self) -> float:
        """Get connection duration in seconds."""
        return time.time() - self.connected_at


# ── Connection Manager ─────────────────────────────────────────────────────────
class WebSocketManager:
    """
    Production-grade WebSocket connection manager.
    
    Features:
    - Connection lifecycle management
    - Room-based broadcasting (pub/sub pattern)
    - Heartbeat/ping-pong for connection health
    - Backpressure handling with bounded queues
    - Connection metrics (Prometheus)
    - Graceful shutdown handling
    
    Rooms:
    - "global" - All connected clients
    - "user:{user_id}" - Per-user subscriptions
    - "symbol:{symbol}" - Per-symbol subscriptions
    - "suggestions" - Trade suggestions updates
    """
    
    def __init__(self, heartbeat_interval: int = 30):
        self.connections: dict[str, WebSocketConnection] = {}
        self.rooms: dict[str, set[str]] = defaultdict(set)
        self.heartbeat_interval = heartbeat_interval
        self._heartbeat_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
    
    async def connect(
        self,
        websocket: WebSocket,
        client_id: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """
        Accept WebSocket connection and register client.
        
        Args:
            websocket: FastAPI WebSocket instance
            client_id: Optional client ID (generated if not provided)
            user_id: Optional user ID for per-user subscriptions
        
        Returns:
            client_id: Unique client identifier
        """
        await websocket.accept()
        
        # Generate client ID if not provided
        if client_id is None:
            client_id = str(uuid4())[:8]
        
        # Create connection object
        connection = WebSocketConnection(
            websocket=websocket,
            client_id=client_id,
            user_id=user_id,
            max_queue_size=100,
        )
        
        # Register connection
        async with self._lock:
            self.connections[client_id] = connection
            
            # Add to global room
            self.rooms["global"].add(client_id)
            connection.join_room("global")
            
            # Add to user room if authenticated
            if user_id:
                user_room = f"user:{user_id}"
                self.rooms[user_room].add(client_id)
                connection.join_room(user_room)
        
        # Start send loop
        connection.start_send_loop()
        
        # Track metrics
        websocket_connections_total.labels(endpoint="/ws").inc()
        websocket_active_connections.labels(endpoint="/ws").inc()
        
        # Start heartbeat if first connection
        if len(self.connections) == 1 and self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        
        logger.info(
            f"Client {client_id} connected (user: {user_id or 'anonymous'}, "
            f"total: {len(self.connections)})"
        )
        
        return client_id
    
    async def disconnect(self, client_id: str, reason: str = "normal") -> None:
        """
        Disconnect client and cleanup resources.
        
        Args:
            client_id: Client identifier
            reason: Disconnect reason (normal, error, timeout)
        """
        async with self._lock:
            connection = self.connections.pop(client_id, None)
            
            if connection is None:
                return
            
            # Remove from all rooms
            for room in list(connection.rooms):
                self.rooms[room].discard(client_id)
                if not self.rooms[room]:
                    del self.rooms[room]
            
            # Stop send loop
            await connection.stop_send_loop()
        
        # Track metrics
        websocket_disconnections_total.labels(endpoint="/ws", reason=reason).inc()
        websocket_active_connections.labels(endpoint="/ws").dec()
        
        duration = connection.get_connection_duration()
        logger.info(
            f"Client {client_id} disconnected (reason: {reason}, "
            f"duration: {duration:.1f}s, total: {len(self.connections)})"
        )
        
        # Stop heartbeat if no connections
        if len(self.connections) == 0 and self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
    
    async def send_to_client(
        self,
        client_id: str,
        message: dict[str, Any]
    ) -> bool:
        """
        Send message to specific client.
        
        Args:
            client_id: Client identifier
            message: Message dictionary
        
        Returns:
            True if sent successfully, False otherwise
        """
        connection = self.connections.get(client_id)
        if connection is None:
            return False
        
        try:
            await connection.send(message)
            return True
        except Exception as e:
            logger.error(f"Failed to send to client {client_id}: {e}")
            return False
    
    async def broadcast_to_room(
        self,
        room: str,
        message: dict[str, Any],
        exclude: set[str] | None = None,
    ) -> int:
        """
        Broadcast message to all clients in a room.
        
        Args:
            room: Room name
            message: Message dictionary
            exclude: Optional set of client IDs to exclude
        
        Returns:
            Number of clients message was sent to
        """
        start_time = time.time()
        exclude = exclude or set()
        
        client_ids = self.rooms.get(room, set()) - exclude
        if not client_ids:
            return 0
        
        sent_count = 0
        failed_clients = []
        
        for client_id in client_ids:
            connection = self.connections.get(client_id)
            if connection is None:
                continue
            
            try:
                await connection.send(message)
                sent_count += 1
            except Exception as e:
                logger.error(f"Failed to send to client {client_id}: {e}")
                failed_clients.append(client_id)
        
        # Cleanup failed connections
        for client_id in failed_clients:
            await self.disconnect(client_id, reason="send_error")
        
        # Track metrics
        duration = time.time() - start_time
        websocket_broadcast_duration_seconds.labels(
            endpoint="/ws",
            room=room,
        ).observe(duration)
        
        logger.debug(
            f"Broadcast to room '{room}': {sent_count}/{len(client_ids)} clients "
            f"({duration*1000:.1f}ms)"
        )
        
        return sent_count
    
    async def broadcast_to_all(
        self,
        message: dict[str, Any],
        exclude: set[str] | None = None,
    ) -> int:
        """
        Broadcast message to all connected clients.
        
        Args:
            message: Message dictionary
            exclude: Optional set of client IDs to exclude
        
        Returns:
            Number of clients message was sent to
        """
        return await self.broadcast_to_room("global", message, exclude)
    
    async def subscribe_to_room(self, client_id: str, room: str) -> bool:
        """
        Subscribe client to a room.
        
        Args:
            client_id: Client identifier
            room: Room name
        
        Returns:
            True if subscribed successfully, False otherwise
        """
        connection = self.connections.get(client_id)
        if connection is None:
            return False
        
        async with self._lock:
            self.rooms[room].add(client_id)
            connection.join_room(room)
        
        logger.info(f"Client {client_id} subscribed to room '{room}'")
        return True
    
    async def unsubscribe_from_room(self, client_id: str, room: str) -> bool:
        """
        Unsubscribe client from a room.
        
        Args:
            client_id: Client identifier
            room: Room name
        
        Returns:
            True if unsubscribed successfully, False otherwise
        """
        connection = self.connections.get(client_id)
        if connection is None:
            return False
        
        async with self._lock:
            self.rooms[room].discard(client_id)
            connection.leave_room(room)
            
            # Cleanup empty rooms
            if not self.rooms[room]:
                del self.rooms[room]
        
        logger.info(f"Client {client_id} unsubscribed from room '{room}'")
        return True
    
    async def _heartbeat_loop(self) -> None:
        """Send ping to all clients every N seconds."""
        try:
            while True:
                await asyncio.sleep(self.heartbeat_interval)
                
                message = {
                    "type": "ping",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                
                await self.broadcast_to_all(message)
                
                logger.debug(
                    f"Heartbeat sent to {len(self.connections)} clients"
                )
        
        except asyncio.CancelledError:
            logger.info("Heartbeat loop cancelled")
            raise
    
    def get_stats(self) -> dict[str, Any]:
        """Get connection statistics."""
        return {
            "total_connections": len(self.connections),
            "total_rooms": len(self.rooms),
            "rooms": {
                room: len(clients)
                for room, clients in self.rooms.items()
            },
            "connections": [
                {
                    "client_id": conn.client_id,
                    "user_id": conn.user_id,
                    "connected_at": conn.connected_at,
                    "duration": conn.get_connection_duration(),
                    "rooms": list(conn.rooms),
                    "queue_size": conn.message_queue.qsize(),
                }
                for conn in self.connections.values()
            ],
        }


# ── Global Manager Instance ────────────────────────────────────────────────────
_manager: WebSocketManager | None = None


def get_websocket_manager() -> WebSocketManager:
    """Get global WebSocket manager instance."""
    global _manager
    if _manager is None:
        _manager = WebSocketManager(heartbeat_interval=30)
    return _manager
