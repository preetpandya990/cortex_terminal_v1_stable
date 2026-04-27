# Production Blockers - Verification Steps

**Date**: 2026-04-23  
**Status**: Ready for Testing

---

## Quick Verification Guide

### Step 1: Restart Services (if needed)

The fixes have been applied to the code. If services are already running, they should auto-reload:

- **Frontend**: Turbopack auto-reloads on file changes
- **Backend**: Uvicorn auto-reloads on file changes (if --reload flag is set)
- **Worker**: May need manual restart

If auto-reload didn't work, restart manually:

```bash
# Stop all services
docker-compose down

# Start all services
docker-compose up -d

# Or restart individual services
docker-compose restart backend
docker-compose restart worker
```

---

## Step 2: Frontend Verification

### 2.1 Check Browser Console

1. Open browser to `http://localhost:3000/hawk-eye-radar`
2. Open DevTools (F12)
3. Go to Console tab
4. Hard refresh (Ctrl+Shift+R or Cmd+Shift+R)

**Expected Results**:
- ✅ NO "Maximum update depth exceeded" errors
- ✅ NO infinite loop errors
- ✅ See "[WebSocket] Connecting to..." message
- ✅ See "[WebSocket] Connected" message
- ✅ See "[Hawk-Eye] WebSocket connected" message

**If you see errors**:
- Check if Turbopack shows errors in terminal
- Verify `frontend/src/hooks/useWebSocket.ts` was updated
- Check browser Network tab for WebSocket connection

### 2.2 Check WebSocket Connections

1. In DevTools, go to Network tab
2. Filter by "WS" (WebSocket)
3. Refresh page

**Expected Results**:
- ✅ Exactly **1 WebSocket connection** (not 253)
- ✅ Connection status: "101 Switching Protocols"
- ✅ Connection stays open (green indicator)

**If you see multiple connections**:
- Clear browser cache and hard refresh
- Check if multiple tabs are open
- Verify the fix was applied correctly

### 2.3 Check UI Status Indicator

1. Look at top-right of Hawk-Eye Radar page
2. Check the status indicator

**Expected Results**:
- ✅ Shows green WiFi icon with "Live" text when connected
- ✅ Shows gray WiFi-off icon with "Connecting..." when disconnected

---

## Step 3: Backend Verification

### 3.1 Check Backend Logs

```bash
# View backend logs
docker-compose logs -f backend --tail=100

# Or if running locally
tail -f backend/logs/app.log
```

**Expected Results**:
- ✅ NO "Received pong from client" messages
- ✅ NO uvicorn ping/pong debug messages like:
  - "% sending keepalive ping"
  - "> PING [binary, 4 bytes]"
  - "< PONG [binary, 4 bytes]"
  - "% received keepalive pong"
- ✅ See normal application logs (INFO level)
- ✅ See WebSocket connection logs: "Client {id} connected"

**If you see pong messages**:
- Check if backend auto-reloaded (look for "Application startup complete")
- Manually restart backend: `docker-compose restart backend`
- Verify `backend/app/core/logging_config.py` was updated

### 3.2 Check Circuit Breaker

```bash
# Search for circuit breaker errors
docker-compose logs backend | grep -i "circuit"

# Search for OHLCV fetch errors
docker-compose logs backend | grep -i "ohlcv"
```

**Expected Results**:
- ✅ NO "object CircuitBreaker can't be used in 'async with' statement" errors
- ✅ See successful OHLCV fetches (if data ingestion is running)
- ✅ Circuit breaker state changes logged (if rate limit hit)

**If you see circuit breaker errors**:
- Check if backend auto-reloaded
- Manually restart backend: `docker-compose restart backend`
- Verify `backend/app/services/upstox_client.py` was updated

---

## Step 4: Integration Testing

### 4.1 Real-Time Updates Test

1. Open Hawk-Eye Radar page in browser
2. Keep browser console open
3. Wait for new trade suggestions (or trigger manually if you have test data)

**Expected Results**:
- ✅ New suggestions appear automatically (no page refresh needed)
- ✅ See "[Hawk-Eye] New suggestion received: {id}" in console
- ✅ UI updates immediately
- ✅ No errors or warnings

### 4.2 Stability Test

1. Leave the page open for 5 minutes
2. Monitor browser console and backend logs

**Expected Results**:
- ✅ No errors or warnings
- ✅ WebSocket connection stays open
- ✅ Memory usage stable (check DevTools Performance tab)
- ✅ CPU usage normal (<10% idle)
- ✅ No connection multiplying

### 4.3 Reconnection Test

1. Stop backend: `docker-compose stop backend`
2. Wait 5 seconds
3. Check browser console - should show "Disconnected"
4. Start backend: `docker-compose start backend`
5. Wait 10 seconds

**Expected Results**:
- ✅ Browser shows "Connecting..." status
- ✅ WebSocket automatically reconnects
- ✅ Browser shows "Live" status after reconnection
- ✅ See reconnection attempts in console
- ✅ No infinite loops or errors

---

## Step 5: Performance Verification

### 5.1 Log Volume Check

```bash
# Count log entries in last 5 minutes
docker-compose logs backend --since 5m | wc -l

# Expected: ~100-200 lines (not 1000+)
```

**Before Fix**: 1000+ lines per 5 minutes (ping/pong spam)  
**After Fix**: 100-200 lines per 5 minutes (90% reduction)

### 5.2 Memory Check

```bash
# Check container memory usage
docker stats --no-stream

# Expected:
# - backend: <500MB
# - frontend: <200MB
# - worker: <300MB
```

### 5.3 WebSocket Latency Check

1. Open browser DevTools
2. Go to Network tab → WS
3. Click on WebSocket connection
4. Go to "Messages" tab
5. Send a ping message (if supported)

**Expected Results**:
- ✅ Ping/pong latency: <50ms
- ✅ Message delivery: <100ms
- ✅ No message queue buildup

---

## Troubleshooting

### Frontend Still Shows Errors

**Solution 1**: Clear browser cache
```bash
# Chrome/Edge: Ctrl+Shift+Delete
# Firefox: Ctrl+Shift+Delete
# Safari: Cmd+Option+E
```

**Solution 2**: Hard refresh
```bash
# Windows/Linux: Ctrl+Shift+R
# Mac: Cmd+Shift+R
```

**Solution 3**: Restart frontend
```bash
cd frontend
npm run dev
```

### Backend Still Shows Pong Messages

**Solution 1**: Check if auto-reload worked
```bash
docker-compose logs backend | grep "Application startup complete"
```

**Solution 2**: Manual restart
```bash
docker-compose restart backend
```

**Solution 3**: Rebuild container
```bash
docker-compose up -d --build backend
```

### WebSocket Won't Connect

**Solution 1**: Check backend is running
```bash
docker-compose ps backend
```

**Solution 2**: Check WebSocket endpoint
```bash
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
  http://localhost:8000/api/v1/trade-suggestions/ws
```

**Solution 3**: Check firewall/network
```bash
# Test backend health
curl http://localhost:8000/health

# Test WebSocket port
telnet localhost 8000
```

---

## Success Criteria

All of the following must be true:

- [ ] Frontend shows 0 Turbopack errors
- [ ] Exactly 1 WebSocket connection (not 253)
- [ ] No "Maximum update depth exceeded" errors
- [ ] Backend logs show NO pong messages
- [ ] Backend logs show NO circuit breaker errors
- [ ] Data ingestion working (OHLCV fetches successful)
- [ ] Real-time updates working (new suggestions appear)
- [ ] System stable for 5+ minutes
- [ ] Log volume reduced by 90%+
- [ ] Memory usage stable

---

## Next Steps After Verification

Once all checks pass:

1. ✅ Mark production blockers as resolved
2. ✅ Update project status documentation
3. ✅ Proceed with Tier 3 enhancements:
   - Query optimization (postponed until production data available)
   - Rate limiting (change from IP-based to per-user)
   - Performance monitoring
   - Load testing

---

## Quick Command Reference

```bash
# View all logs
docker-compose logs -f

# View specific service logs
docker-compose logs -f backend
docker-compose logs -f frontend
docker-compose logs -f worker

# Restart services
docker-compose restart backend
docker-compose restart frontend
docker-compose restart worker

# Rebuild and restart
docker-compose up -d --build

# Check service status
docker-compose ps

# Check resource usage
docker stats

# Stop all services
docker-compose down

# Start all services
docker-compose up -d
```

---

**Status**: Ready for Verification  
**Estimated Time**: 10-15 minutes  
**Risk Level**: Low (all fixes are production-grade)
