#!/usr/bin/env bash
# Quick Test Script for Task 9.4 - RSS → Signal E2E Flow

set -e

BACKEND_DIR="/home/preet/code/Cortex_Merge_AI-ML/backend"
API_URL="http://localhost:8000"

echo "=== Cortex AI - Task 9.4 E2E Test ==="
echo

# Check if API is running
if ! curl -s "$API_URL/health" > /dev/null 2>&1; then
    echo "❌ API is not running on port 8000"
    echo "   Start it with: cd $BACKEND_DIR && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
    exit 1
fi

echo "✓ API is running"

# Login as admin
echo
echo "🔐 Logging in as admin..."
TOKEN=$(curl -s -X POST "$API_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}' \
  | jq -r '.access_token')

if [ "$TOKEN" == "null" ] || [ -z "$TOKEN" ]; then
    echo "❌ Login failed"
    exit 1
fi

echo "✓ Login successful"
echo "   Token: ${TOKEN:0:20}..."

# Get current user
echo
echo "👤 Getting current user profile..."
curl -s "$API_URL/api/v1/auth/me" \
  -H "Authorization: Bearer $TOKEN" | jq

# Check raw events
echo
echo "📰 Checking raw events..."
RAW_COUNT=$(curl -s "$API_URL/api/v1/ingestion/events/raw?limit=100" \
  -H "Authorization: Bearer $TOKEN" | jq 'length')

echo "   Found $RAW_COUNT raw events"

if [ "$RAW_COUNT" -eq 0 ]; then
    echo "   ⚠️  No raw events yet. Wait for RSS ingestion loop (5-15 min)"
    echo "   Monitor worker logs for: 'RSS ingestion cycle complete'"
fi

# Check processed events
echo
echo "⚙️  Checking processed events..."
PROCESSED_COUNT=$(curl -s "$API_URL/api/v1/ingestion/events/processed?limit=100" \
  -H "Authorization: Bearer $TOKEN" | jq 'length')

echo "   Found $PROCESSED_COUNT processed events"

if [ "$PROCESSED_COUNT" -eq 0 ] && [ "$RAW_COUNT" -gt 0 ]; then
    echo "   ⚠️  Raw events exist but not processed yet"
    echo "   Wait for event processing loop (30s interval)"
    echo "   Monitor worker logs for: 'Event processing cycle complete'"
fi

# Check signals
echo
echo "📊 Checking trading signals..."
SIGNAL_COUNT=$(curl -s "$API_URL/api/v1/fusion/signals?limit=100" \
  -H "Authorization: Bearer $TOKEN" | jq 'length')

echo "   Found $SIGNAL_COUNT trading signals"

if [ "$SIGNAL_COUNT" -gt 0 ]; then
    echo
    echo "✅ E2E Flow Working! Latest signals:"
    curl -s "$API_URL/api/v1/fusion/signals?limit=5" \
      -H "Authorization: Bearer $TOKEN" \
      | jq '.[] | {symbol, final_score, confidence, timestamp: .signal_timestamp}'
fi

# Test RBAC
echo
echo "🔒 Testing RBAC (viewer should be blocked)..."
VIEWER_TOKEN=$(curl -s -X POST "$API_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "viewer", "password": "viewer123"}' \
  | jq -r '.access_token')

RBAC_TEST=$(curl -s -X POST "$API_URL/api/v1/fusion/signals/generate/TEST" \
  -H "Authorization: Bearer $VIEWER_TOKEN" \
  | jq -r '.detail')

if [[ "$RBAC_TEST" == *"Insufficient permissions"* ]]; then
    echo "✓ RBAC working correctly (viewer blocked)"
else
    echo "❌ RBAC not working (viewer should be blocked)"
fi

echo
echo "=== Test Summary ==="
echo "Raw Events:       $RAW_COUNT"
echo "Processed Events: $PROCESSED_COUNT"
echo "Trading Signals:  $SIGNAL_COUNT"
echo

if [ "$SIGNAL_COUNT" -gt 0 ]; then
    echo "🎉 Task 9.4 E2E Flow: SUCCESS"
    echo "   RSS → Raw Events → Processing → Classification → Signals ✅"
else
    echo "⏳ Task 9.4 E2E Flow: IN PROGRESS"
    echo "   Waiting for RSS ingestion and event processing..."
    echo
    echo "Next steps:"
    echo "1. Ensure worker is running: .venv/bin/python -m app.worker"
    echo "2. Wait 5-15 minutes for RSS ingestion"
    echo "3. Wait 30 seconds for event processing"
    echo "4. Run this script again"
fi

echo
echo "For detailed logs:"
echo "  Worker: Check terminal running 'python -m app.worker'"
echo "  API:    Check terminal running 'uvicorn app.main:app'"
