# WebSocket Real-time Updates - Quick Reference

## Connection

### Backend
```python
from app.core.websocket_manager import get_websocket_manager

manager = get_websocket_manager()
client_id = await manager.connect(websocket, user_id="user_123")
```

### Frontend
```typescript
import { useWebSocket } from '@/hooks/useWebSocket';

const { isConnected, subscribe } = useWebSocket({
  url: 'ws://localhost:8000/api/v1/trade-suggestions/ws',
  token: authToken,
  onMessage: (data) => console.log(data),
});
```

---

## Room Subscriptions

### Subscribe to Symbol
```typescript
subscribe('symbol:AAPL');
```

### Unsubscribe from Symbol
```typescript
unsubscribe('symbol:AAPL');
```

### Auto-Subscribe on Connect
```typescript
useWebSocket({
  url: '...',
  autoSubscribeRooms: ['symbol:AAPL', 'symbol:TSLA'],
});
```

---

## Message Types

### New Suggestion
```json
{
  "type": "new_suggestion",
  "suggestion_id": "...",
  "symbol": "AAPL",
  "signal_direction": "BUY",
  "consensus_score": 85.5
}
```

### Suggestion Expired
```json
{
  "type": "suggestion_expired",
  "suggestion_id": "...",
  "symbol": "AAPL",
  "reason": "TTL_EXPIRED"
}
```

### Heartbeat
```json
{
  "type": "ping",
  "timestamp": "2026-04-23T12:00:00Z"
}
```

---

## Broadcasting

### Broadcast to All
```python
await manager.broadcast_to_all(message)
```

### Broadcast to Room
```python
await manager.broadcast_to_room("symbol:AAPL", message)
```

### Broadcast to User
```python
await manager.broadcast_to_room("user:user_123", message)
```

---

## Metrics

### Connection Metrics
```promql
websocket_active_connections{endpoint="/ws"}
websocket_connections_total{endpoint="/ws"}
```

### Message Metrics
```promql
websocket_messages_sent_total{endpoint="/ws",message_type="new_suggestion"}
rate(websocket_message_send_duration_seconds_sum[5m])
```

### Broadcast Metrics
```promql
histogram_quantile(0.95, websocket_broadcast_duration_seconds_bucket)
```

---

## Common Patterns

### Invalidate Cache on Update
```typescript
onMessage: (data) => {
  if (data.type === 'new_suggestion') {
    queryClient.invalidateQueries(['trade-suggestions']);
  }
}
```

### Show Toast Notification
```typescript
onMessage: (data) => {
  if (data.type === 'new_suggestion') {
    toast.success(`New ${data.signal_direction} for ${data.symbol}`);
  }
}
```

### Connection Status Indicator
```typescript
const { isConnected } = useWebSocket({ ... });

return (
  <div className={isConnected ? 'text-green-500' : 'text-red-500'}>
    {isConnected ? 'Connected' : 'Disconnected'}
  </div>
);
```

---

## Troubleshooting

### Connection Failed
```bash
# Check CORS
# Check WebSocket endpoint is accessible
# Verify token is valid
```

### Messages Not Received
```bash
# Verify subscription to correct room
# Check Redis pub/sub is working
# Check Redis listener is running
```

### High Latency
```bash
# Check queue size
# Use room-based broadcasting
# Scale horizontally
```

---

## Configuration

### Environment Variables
```bash
WEBSOCKET_HEARTBEAT_INTERVAL=30
WEBSOCKET_MAX_QUEUE_SIZE=100
WEBSOCKET_IDLE_TIMEOUT=300
```

### Nginx Configuration
```nginx
location /api/v1/trade-suggestions/ws {
    proxy_pass http://backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
}
```

---

## Testing

### Manual Test
```bash
# Install wscat
npm install -g wscat

# Connect
wscat -c ws://localhost:8000/api/v1/trade-suggestions/ws

# Subscribe to room
> {"type":"subscribe","room":"symbol:AAPL"}

# Send ping
> {"type":"ping"}
```

### Integration Tests
```bash
pytest tests/integration/test_websocket_realtime.py -v
```

---

**Enhancement**: 2.4 - WebSocket Real-time Updates  
**Status**: ✅ Complete
