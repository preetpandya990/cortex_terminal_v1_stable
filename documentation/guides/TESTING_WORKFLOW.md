# Task 9.4 Testing Workflow

## Quick Start

### 1. Start Services
```bash
cd /home/preet/code/Cortex_Merge_AI-ML/backend

# Start API (logs to logs/api.log)
./scripts/start-api.sh

# Start Worker (logs to logs/worker.log)
./scripts/start-worker.sh
```

### 2. Check Logs
```bash
# Check both logs for errors
./scripts/check-logs.sh

# Or watch live:
tail -f logs/api.log      # API logs
tail -f logs/worker.log   # Worker logs
```

### 3. Run Tests
```bash
# Automated E2E test
./scripts/test-task-9.4.sh
```

### 4. Stop Services
```bash
./scripts/stop-all.sh
```

---

## Workflow for Debugging Together

### When Something Goes Wrong:

**You say:** "Check the logs"

**I run:**
```bash
./scripts/check-logs.sh
```

**I analyze the errors and fix them**

**Then restart:**
```bash
./scripts/stop-all.sh
./scripts/start-api.sh
./scripts/start-worker.sh
```

---

## Log Locations

- **API logs:** `backend/logs/api.log`
- **Worker logs:** `backend/logs/worker.log`

---

## Common Commands

```bash
# Check if services are running
ps aux | grep -E "(uvicorn|app.worker)" | grep -v grep

# Check API health
curl http://localhost:8000/health

# Login and get token
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}' | jq

# Check raw events
curl http://localhost:8000/api/v1/ingestion/events/raw \
  -H "Authorization: Bearer $TOKEN" | jq

# Check signals
curl http://localhost:8000/api/v1/fusion/signals \
  -H "Authorization: Bearer $TOKEN" | jq
```

---

## Expected Timeline

1. **API starts:** ~2 seconds
2. **Worker starts:** ~2 seconds
3. **RSS ingestion:** 5-15 minutes (first cycle)
4. **Event processing:** 30 seconds after raw events appear
5. **Signals generated:** Immediately after classification

---

## Success Indicators in Logs

### API logs should show:
```
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### Worker logs should show:
```
INFO: Cortex AI starting [env=development]
INFO: RSS ingestion loop started
INFO: Event processing loop started
INFO: Regime detection loop started
INFO: Drift detection loop started
INFO: Safety monitoring loop started
INFO: Heartbeat loop started
INFO: Started 6 background tasks
```

### After RSS ingestion:
```
INFO: RSS ingestion cycle complete: 25 events, 0 errors
```

### After event processing:
```
INFO: Processing 10 unprocessed events
INFO: Generated signal for RELIANCE: score=45.23, confidence=0.78
INFO: Event processing cycle complete: 10 events processed
```

---

## Ready to Start!

Run: `./scripts/start-api.sh && ./scripts/start-worker.sh`

Then tell me to check logs if anything goes wrong.
