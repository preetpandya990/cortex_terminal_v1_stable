# Task 9.4 - Manual cURL Commands

## Setup
```bash
export API_URL="http://localhost:8000"
```

---

## 1. Health Check
```bash
curl $API_URL/health | jq
```

**Expected:**
```json
{"status": "ok", "version": "1.0.0"}
```

---

## 2. Login as Admin
```bash
curl -X POST $API_URL/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}' | jq

# Save token
export TOKEN=$(curl -s -X POST $API_URL/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}' \
  | jq -r '.access_token')

echo "Token: $TOKEN"
```

**Expected:**
```json
{
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "refresh_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "token_type": "bearer",
  "expires_in": 1800
}
```

---

## 3. Get Current User
```bash
curl $API_URL/api/v1/auth/me \
  -H "Authorization: Bearer $TOKEN" | jq
```

**Expected:**
```json
{
  "id": 1,
  "username": "admin",
  "email": "admin@cortex.local",
  "full_name": "Admin User",
  "role": "admin",
  "is_active": true,
  "created_at": "2026-04-11T...",
  "last_login": "2026-04-11T..."
}
```

---

## 4. Check Raw Events (RSS Ingestion)
```bash
# Get all raw events
curl $API_URL/api/v1/ingestion/events/raw?limit=20 \
  -H "Authorization: Bearer $TOKEN" | jq

# Count raw events
curl -s $API_URL/api/v1/ingestion/events/raw?limit=100 \
  -H "Authorization: Bearer $TOKEN" | jq 'length'

# Show titles only
curl -s $API_URL/api/v1/ingestion/events/raw?limit=10 \
  -H "Authorization: Bearer $TOKEN" | jq '.[] | .title'
```

**Expected (after RSS ingestion):**
```json
[
  {
    "event_id": 1,
    "source_type": "rss",
    "source_url": "https://economictimes.indiatimes.com/...",
    "title": "Reliance Industries Q4 results...",
    "timestamp": "2026-04-11T15:30:00Z"
  },
  ...
]
```

---

## 5. Check Processed Events (Event Processing)
```bash
# Get all processed events
curl $API_URL/api/v1/ingestion/events/processed?limit=20 \
  -H "Authorization: Bearer $TOKEN" | jq

# Count processed events
curl -s $API_URL/api/v1/ingestion/events/processed?limit=100 \
  -H "Authorization: Bearer $TOKEN" | jq 'length'
```

**Expected (after event processing):**
```json
[
  {
    "event_id": 1,
    "raw_event_id": 1,
    "processed_text": "Reliance Industries Q4 results...",
    "timestamp": "2026-04-11T15:30:30Z"
  },
  ...
]
```

---

## 6. Check Trading Signals (Signal Generation)
```bash
# Get all signals
curl $API_URL/api/v1/fusion/signals?limit=20 \
  -H "Authorization: Bearer $TOKEN" | jq

# Count signals
curl -s $API_URL/api/v1/fusion/signals?limit=100 \
  -H "Authorization: Bearer $TOKEN" | jq 'length'

# Show signals for specific symbol
curl -s "$API_URL/api/v1/fusion/signals?symbol=RELIANCE&limit=5" \
  -H "Authorization: Bearer $TOKEN" | jq

# Show signal details
curl -s $API_URL/api/v1/fusion/signals?limit=5 \
  -H "Authorization: Bearer $TOKEN" \
  | jq '.[] | {symbol, final_score, confidence, event_contribution, ml_contribution, timestamp}'
```

**Expected (after signal generation):**
```json
[
  {
    "symbol": "RELIANCE",
    "final_score": 45.23,
    "confidence": 0.78,
    "event_contribution": 35.5,
    "ml_contribution": null,
    "timestamp": "2026-04-11T15:31:00Z"
  },
  ...
]
```

---

## 7. Test RBAC (Viewer Access Control)
```bash
# Login as viewer
export VIEWER_TOKEN=$(curl -s -X POST $API_URL/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "viewer", "password": "viewer123"}' \
  | jq -r '.access_token')

# Try to read signals (should work)
curl $API_URL/api/v1/fusion/signals?limit=5 \
  -H "Authorization: Bearer $VIEWER_TOKEN" | jq

# Try to generate signal (should fail with 403)
curl -X POST $API_URL/api/v1/fusion/signals/generate/RELIANCE \
  -H "Authorization: Bearer $VIEWER_TOKEN" | jq
```

**Expected (403 Forbidden):**
```json
{
  "detail": "Insufficient permissions. Required role: trader"
}
```

---

## 8. Login as Trader (for comparison)
```bash
# Login as trader
export TRADER_TOKEN=$(curl -s -X POST $API_URL/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "trader", "password": "trader123"}' \
  | jq -r '.access_token')

# Try to generate signal (should work)
curl -X POST $API_URL/api/v1/fusion/signals/generate/RELIANCE \
  -H "Authorization: Bearer $TRADER_TOKEN" | jq
```

---

## Quick Status Check
```bash
# One-liner to check pipeline status
echo "Raw: $(curl -s $API_URL/api/v1/ingestion/events/raw?limit=100 -H "Authorization: Bearer $TOKEN" | jq 'length') | Processed: $(curl -s $API_URL/api/v1/ingestion/events/processed?limit=100 -H "Authorization: Bearer $TOKEN" | jq 'length') | Signals: $(curl -s $API_URL/api/v1/fusion/signals?limit=100 -H "Authorization: Bearer $TOKEN" | jq 'length')"
```

---

## Timeline Expectations

1. **Start services** → Immediate
2. **RSS ingestion** → 5-15 minutes (first cycle)
3. **Event processing** → 30 seconds after raw events appear
4. **Signal generation** → Immediate during event processing

---

## Troubleshooting

### No raw events?
```bash
# Check worker logs
tail -f logs/worker.log | grep -i "rss"

# Look for: "RSS ingestion cycle complete: X events"
```

### Raw events but no processed events?
```bash
# Check worker logs
tail -f logs/worker.log | grep -i "event processing"

# Look for: "Processing X unprocessed events"
```

### Processed events but no signals?
```bash
# Check worker logs
tail -f logs/worker.log | grep -i "signal"

# Look for: "Generated signal for SYMBOL: score=X"
```

### Authentication errors?
```bash
# Verify users exist
cd /home/preet/code/Cortex_Merge_AI-ML/backend
.venv/bin/python scripts/create_test_users.py
```

---

## Automated Test

Run all checks automatically:
```bash
./scripts/test-e2e-flow.sh
```
