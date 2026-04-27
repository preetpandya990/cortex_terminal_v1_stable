# Frontend Error Analysis & Resolution Plan

**Date**: 2026-04-23  
**Severity**: CRITICAL - Production Blocker  
**Status**: Analysis Complete, Ready for Implementation

---

## Error Summary

### 1. Maximum Update Depth Exceeded (CRITICAL)
**Root Cause**: Infinite loop in `useWebSocket` hook caused by circular dependencies in `useEffect`

### 2. WebSocket Connection Failures (HIGH)
**Root Cause**: Backend not accessible or CORS issues

### 3. API Network Errors (HIGH)
**Root Cause**: Backend API not responding or network connectivity issues

---

## Detailed Analysis

### Error 1: Maximum Update Depth Exceeded

**Location**: `frontend/src/hooks/useWebSocket.ts`

**Problem**: The `connect` function is defined with `useCallback` and has dependencies that include functions (`send`, `subscribe`, `startHeartbeat`, `stopHeartbeat`) which themselves depend on `connect`, creating a circular dependency chain.

**Evidence from Code**:
```typescript
// Line 216: setState inside ws.onopen
ws.onopen = () => {
  setState('connected');  // ❌ Triggers re-render
  // ...
  startHeartbeat();       // ❌ Depends on send
  // ...
  subscribe(room);        // ❌ Depends on send
};

// Line 262: setState inside ws.onerror
ws.onerror = (event) => {
  setState('error');      // ❌ Triggers re-render
};

// Line 312: setState in disconnect
setState('disconnected'); // ❌ Triggers re-render

// Line 295: connect has circular dependencies
const connect = useCallback(() => {
  // ...
}, [url, token, reconnect, reconnectAttempts, getReconnectDelay, 
    startHeartbeat, stopHeartbeat, send, subscribe, autoSubscribeRooms, 
    onConnect, onMessage, onDisconnect, onError]);
    // ❌ These dependencies change on every render
```

**Why This Happens**:
1. `connect` depends on `send`, `subscribe`, `startHeartbeat`, `stopHeartbeat`
2. These functions depend on `send`
3. `send` depends on `wsRef` which changes when connection state changes
4. State changes trigger re-renders
5. Re-renders recreate callbacks
6. New callbacks trigger `useEffect` with `connect` dependency
7. **INFINITE LOOP**

**React's Limit**: 50 nested updates before throwing error

---

### Error 2: WebSocket Connection Failures

**Location**: `frontend/src/hooks/useWebSocket.ts:261`

**Problem**: WebSocket cannot connect to backend

**Possible Causes**:
1. Backend not running on expected port
2. WebSocket URL incorrect
3. CORS policy blocking WebSocket upgrade
4. Network firewall blocking WebSocket connections
5. Backend WebSocket endpoint not implemented

**Current URL**: `ws://localhost:8000/api/v1/trade-suggestions/ws`

---

### Error 3: API Network Errors

**Location**: `frontend/src/lib/api.ts:129`

**Problem**: HTTP requests failing to reach backend

**Possible Causes**:
1. Backend API not running
2. CORS not configured correctly
3. Network connectivity issues
4. Incorrect API base URL

**Current Base URL**: Check `NEXT_PUBLIC_API_URL` in `.env.local`

---

## Root Cause Analysis

### Primary Issue: useWebSocket Hook Architecture

The current implementation has **fundamental design flaws**:

1. **Circular Dependencies**: Functions depend on each other in a cycle
2. **Unstable References**: Callbacks recreated on every render
3. **State Updates in Callbacks**: Triggers re-renders that recreate callbacks
4. **Missing Memoization**: Functions not properly memoized
5. **useEffect Dependency Hell**: Too many dependencies causing cascading updates

### Industry Best Practices (2026)

Based on research from production React applications:

1. **Use `useRef` for WebSocket instance** - Never changes reference
2. **Minimize `useCallback` dependencies** - Only include stable values
3. **Use functional state updates** - `setState(prev => ...)` to avoid dependencies
4. **Separate connection logic from event handlers** - Decouple concerns
5. **Use `useEffectEvent` (React 19+)** - For event handlers without reactivity
6. **Single `useEffect` for connection** - Don't split connection logic

---

## Solution Architecture

### Approach: Complete Rewrite of useWebSocket Hook

**Design Principles**:
1. ✅ **Stable References**: Use `useRef` for all mutable values
2. ✅ **Minimal Dependencies**: Only URL and token in useEffect
3. ✅ **Functional Updates**: Use `setState(prev => ...)` everywhere
4. ✅ **Event Handler Pattern**: Separate event handlers from connection logic
5. ✅ **Single Source of Truth**: One useEffect for connection lifecycle
6. ✅ **Proper Cleanup**: Clear all timers and close connections on unmount

### Key Changes

#### 1. Use Refs for Stable References
```typescript
// ✅ GOOD: Stable references
const wsRef = useRef<WebSocket | null>(null);
const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
const heartbeatIntervalRef = useRef<NodeJS.Timeout | null>(null);
const callbacksRef = useRef({ onMessage, onConnect, onDisconnect, onError });

// Update callbacks without triggering re-renders
useEffect(() => {
  callbacksRef.current = { onMessage, onConnect, onDisconnect, onError };
}, [onMessage, onConnect, onDisconnect, onError]);
```

#### 2. Functional State Updates
```typescript
// ❌ BAD: Creates dependency on state
setState('connected');

// ✅ GOOD: No dependency needed
setState(() => 'connected');
```

#### 3. Single Connection Effect
```typescript
// ✅ GOOD: Only depends on stable values
useEffect(() => {
  let ws: WebSocket | null = null;
  let reconnectTimeout: NodeJS.Timeout | null = null;
  let heartbeatInterval: NodeJS.Timeout | null = null;
  
  const connect = () => {
    // All logic here, no external dependencies
  };
  
  connect();
  
  return () => {
    // Cleanup everything
    if (reconnectTimeout) clearTimeout(reconnectTimeout);
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    if (ws) ws.close();
  };
}, [url, token]); // Only stable dependencies
```

#### 4. Expose Methods via Refs
```typescript
// ✅ GOOD: Stable API
const sendRef = useRef<(data: any) => void>(() => {});
const subscribeRef = useRef<(room: string) => void>(() => {});

return {
  send: sendRef.current,
  subscribe: subscribeRef.current,
  // ...
};
```

---

## Implementation Plan

### Phase 1: Fix useWebSocket Hook (30 minutes)

**File**: `frontend/src/hooks/useWebSocket.ts`

**Changes**:
1. Remove all `useCallback` hooks
2. Move all logic into single `useEffect`
3. Use `useRef` for callbacks
4. Use functional state updates
5. Implement proper cleanup
6. Add comprehensive error handling
7. Add connection state machine
8. Add retry with exponential backoff

**Testing**:
- [ ] No infinite loops
- [ ] Connects successfully
- [ ] Reconnects on disconnect
- [ ] Handles errors gracefully
- [ ] Cleans up on unmount
- [ ] No memory leaks

### Phase 2: Fix API Client (10 minutes)

**File**: `frontend/src/lib/api.ts`

**Changes**:
1. Add better error logging
2. Add retry logic for network errors
3. Add request timeout
4. Add connection health check
5. Improve error messages

### Phase 3: Environment Configuration (5 minutes)

**File**: `frontend/.env.local`

**Verify**:
```env
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_WS_URL=ws://localhost:8000/api/v1/trade-suggestions/ws
```

### Phase 4: Integration Testing (15 minutes)

**Tests**:
1. Start backend + worker
2. Start frontend
3. Verify WebSocket connects
4. Verify API calls work
5. Test reconnection
6. Test error handling
7. Monitor for infinite loops

---

## Reference Implementation

### Production-Grade useWebSocket Pattern (2026)

```typescript
export function useWebSocket(options: UseWebSocketOptions): UseWebSocketReturn {
  const { url, token, onMessage, onConnect, onDisconnect, onError } = options;
  
  // State
  const [state, setState] = useState<WebSocketState>('disconnected');
  const [lastMessage, setLastMessage] = useState<WebSocketMessage | null>(null);
  const [error, setError] = useState<Event | null>(null);
  const [reconnectCount, setReconnectCount] = useState(0);
  
  // Refs for stable references
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const shouldReconnectRef = useRef(true);
  const callbacksRef = useRef({ onMessage, onConnect, onDisconnect, onError });
  
  // Update callbacks without triggering effects
  useEffect(() => {
    callbacksRef.current = { onMessage, onConnect, onDisconnect, onError };
  }, [onMessage, onConnect, onDisconnect, onError]);
  
  // Single effect for connection lifecycle
  useEffect(() => {
    shouldReconnectRef.current = true;
    let ws: WebSocket | null = null;
    let reconnectTimeout: NodeJS.Timeout | null = null;
    let heartbeatInterval: NodeJS.Timeout | null = null;
    
    const connect = () => {
      if (ws?.readyState === WebSocket.OPEN) return;
      
      setState(() => 'connecting');
      const wsUrl = token ? `${url}?token=${token}` : url;
      ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      
      ws.onopen = () => {
        setState(() => 'connected');
        setError(() => null);
        reconnectAttemptsRef.current = 0;
        setReconnectCount(() => 0);
        
        // Start heartbeat
        heartbeatInterval = setInterval(() => {
          ws?.send(JSON.stringify({ type: 'pong' }));
        }, 30000);
        
        callbacksRef.current.onConnect?.();
      };
      
      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          setLastMessage(() => data);
          callbacksRef.current.onMessage?.(data);
        } catch (err) {
          console.error('[WebSocket] Parse error:', err);
        }
      };
      
      ws.onerror = (event) => {
        setState(() => 'error');
        setError(() => event);
        callbacksRef.current.onError?.(event);
      };
      
      ws.onclose = () => {
        setState(() => 'disconnected');
        if (heartbeatInterval) clearInterval(heartbeatInterval);
        callbacksRef.current.onDisconnect?.();
        
        // Reconnect logic
        if (shouldReconnectRef.current && reconnectAttemptsRef.current < 10) {
          reconnectAttemptsRef.current++;
          const delay = Math.min(1000 * Math.pow(2, reconnectAttemptsRef.current), 30000);
          reconnectTimeout = setTimeout(connect, delay);
        }
      };
    };
    
    connect();
    
    return () => {
      shouldReconnectRef.current = false;
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      if (heartbeatInterval) clearInterval(heartbeatInterval);
      if (ws) ws.close();
    };
  }, [url, token]); // Only stable dependencies
  
  // Expose stable API
  const send = useCallback((data: any) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);
  
  return {
    state,
    isConnected: state === 'connected',
    lastMessage,
    error,
    reconnectCount,
    send,
  };
}
```

---

## Success Criteria

### Must Have ✅
- [ ] No infinite loops or maximum update depth errors
- [ ] WebSocket connects successfully
- [ ] WebSocket reconnects on disconnect
- [ ] API calls work without errors
- [ ] Proper error handling and logging
- [ ] Clean unmount without memory leaks

### Performance Targets
- [ ] Connection time: <500ms
- [ ] Reconnection time: <2s (with backoff)
- [ ] Memory usage: Stable (no leaks)
- [ ] CPU usage: <1% idle

### Code Quality
- [ ] TypeScript strict mode: No errors
- [ ] ESLint: No warnings
- [ ] React DevTools: No warnings
- [ ] Production build: No errors

---

## Risk Assessment

### High Risk ⚠️
- Complete rewrite of WebSocket hook
- Potential breaking changes to components using the hook

### Mitigation
1. Create new hook file first (`useWebSocket.v2.ts`)
2. Test thoroughly before replacing
3. Keep old hook as backup
4. Gradual rollout per component
5. Add comprehensive error logging

---

## Timeline

**Total Estimated Time**: 60 minutes

1. **Analysis & Planning**: ✅ Complete (30 min)
2. **Implementation**: 30 min
   - useWebSocket rewrite: 20 min
   - API client fixes: 5 min
   - Environment check: 5 min
3. **Testing**: 15 min
4. **Documentation**: 15 min

**Total**: 90 minutes for production-ready implementation

---

## Next Steps

**AWAITING APPROVAL** to proceed with implementation.

**Questions**:
1. Approve complete rewrite of useWebSocket hook?
2. Should we create v2 file first or replace directly?
3. Any specific WebSocket features needed (rooms, authentication, etc.)?
4. Preferred testing approach (manual, automated, both)?

---

## References

- [React useEffect Infinite Loops - 2026 Best Practices](https://copyprogramming.com/howto/prevent-infinite-loop-in-useeffect-when-setting-state)
- [WebSocket in React - Complete Guide 2026](https://copyprogramming.com/howto/reconnecting-web-socket-using-react-hooks)
- [Maximum Update Depth Exceeded - Solutions](https://copyprogramming.com/howto/react-how-do-i-get-rid-of-this-maximum-update-depth-exceeded-error)
- [React Hooks Best Practices 2026](https://copyprogramming.com/howto/react-hooks-setstate-in-useeffect-infinite-loop)

---

**Report Generated**: 2026-04-23  
**Status**: Ready for Implementation  
**Priority**: CRITICAL - Production Blocker
