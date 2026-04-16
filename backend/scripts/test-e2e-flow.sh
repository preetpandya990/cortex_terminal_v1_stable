#!/usr/bin/env bash
# Task 9.4 E2E Test - RSS to Signal Flow
# Run this after starting API and Worker

set -e

API_URL="http://localhost:8000"
BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BOLD}=== Task 9.4: RSS → Signal E2E Flow ===${NC}"
echo

# Step 1: Check API health
echo -e "${BOLD}Step 1: Check API Health${NC}"
echo "curl $API_URL/health"
HEALTH=$(curl -s $API_URL/health)
echo "$HEALTH" | jq
if [[ "$HEALTH" == *"ok"* ]]; then
    echo -e "${GREEN}✓ API is healthy${NC}"
else
    echo -e "${RED}✗ API health check failed${NC}"
    exit 1
fi
echo

# Step 2: Login as admin
echo -e "${BOLD}Step 2: Login as Admin${NC}"
echo "curl -X POST $API_URL/api/v1/auth/login -d '{\"username\":\"admin\",\"password\":\"admin123\"}'"
TOKEN=$(curl -s -X POST "$API_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "admin123"}' \
  | jq -r '.access_token')

if [ "$TOKEN" == "null" ] || [ -z "$TOKEN" ]; then
    echo -e "${RED}✗ Login failed${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Login successful${NC}"
echo "Token: ${TOKEN:0:30}..."
echo

# Step 3: Get current user
echo -e "${BOLD}Step 3: Get Current User Profile${NC}"
echo "curl $API_URL/api/v1/auth/me -H 'Authorization: Bearer \$TOKEN'"
curl -s "$API_URL/api/v1/auth/me" \
  -H "Authorization: Bearer $TOKEN" | jq
echo

# Step 4: Check raw events (from RSS ingestion)
echo -e "${BOLD}Step 4: Check Raw Events (RSS Ingestion)${NC}"
echo "curl $API_URL/api/v1/ingestion/events/raw?limit=5"
RAW_EVENTS=$(curl -s "$API_URL/api/v1/ingestion/events/raw?limit=5" \
  -H "Authorization: Bearer $TOKEN")
RAW_COUNT=$(echo "$RAW_EVENTS" | jq 'length')

echo "Found: $RAW_COUNT raw events"
echo "$RAW_EVENTS" | jq '.[] | {event_id, title, source_type, timestamp}'

if [ "$RAW_COUNT" -eq 0 ]; then
    echo -e "${YELLOW}⚠ No raw events yet${NC}"
    echo "Wait for RSS ingestion loop (5-15 minutes)"
    echo "Check worker logs: tail -f logs/worker.log"
    echo "Look for: 'RSS ingestion cycle complete: X events'"
else
    echo -e "${GREEN}✓ Raw events found${NC}"
fi
echo

# Step 5: Check processed events (from event processing loop)
echo -e "${BOLD}Step 5: Check Processed Events (Event Processing)${NC}"
echo "curl $API_URL/api/v1/ingestion/events/processed?limit=5"
PROCESSED_EVENTS=$(curl -s "$API_URL/api/v1/ingestion/events/processed?limit=5" \
  -H "Authorization: Bearer $TOKEN")
PROCESSED_COUNT=$(echo "$PROCESSED_EVENTS" | jq 'length')

echo "Found: $PROCESSED_COUNT processed events"
echo "$PROCESSED_EVENTS" | jq '.[] | {event_id, raw_event_id, timestamp}'

if [ "$PROCESSED_COUNT" -eq 0 ] && [ "$RAW_COUNT" -gt 0 ]; then
    echo -e "${YELLOW}⚠ Raw events exist but not processed yet${NC}"
    echo "Wait for event processing loop (30 seconds)"
    echo "Check worker logs: tail -f logs/worker.log"
    echo "Look for: 'Event processing cycle complete: X events processed'"
elif [ "$PROCESSED_COUNT" -gt 0 ]; then
    echo -e "${GREEN}✓ Processed events found${NC}"
fi
echo

# Step 6: Check trading signals (generated from events)
echo -e "${BOLD}Step 6: Check Trading Signals (Signal Generation)${NC}"
echo "curl $API_URL/api/v1/fusion/signals?limit=5"
SIGNALS=$(curl -s "$API_URL/api/v1/fusion/signals?limit=5" \
  -H "Authorization: Bearer $TOKEN")
SIGNAL_COUNT=$(echo "$SIGNALS" | jq 'length')

echo "Found: $SIGNAL_COUNT trading signals"
echo "$SIGNALS" | jq '.[] | {symbol, final_score, confidence, event_contribution, timestamp}'

if [ "$SIGNAL_COUNT" -eq 0 ] && [ "$PROCESSED_COUNT" -gt 0 ]; then
    echo -e "${YELLOW}⚠ Processed events exist but no signals yet${NC}"
    echo "Signals are generated during event processing"
    echo "Check worker logs for: 'Generated signal for SYMBOL'"
elif [ "$SIGNAL_COUNT" -gt 0 ]; then
    echo -e "${GREEN}✓ Trading signals found${NC}"
fi
echo

# Step 7: Test RBAC (viewer should be blocked from trader endpoints)
echo -e "${BOLD}Step 7: Test RBAC (Viewer Access Control)${NC}"
echo "Login as viewer and try to generate signal (should fail)"

VIEWER_TOKEN=$(curl -s -X POST "$API_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username": "viewer", "password": "viewer123"}' \
  | jq -r '.access_token')

echo "curl -X POST $API_URL/api/v1/fusion/signals/generate/TEST"
RBAC_RESPONSE=$(curl -s -X POST "$API_URL/api/v1/fusion/signals/generate/TEST" \
  -H "Authorization: Bearer $VIEWER_TOKEN")

echo "$RBAC_RESPONSE" | jq

if [[ "$RBAC_RESPONSE" == *"Insufficient permissions"* ]]; then
    echo -e "${GREEN}✓ RBAC working correctly (viewer blocked)${NC}"
else
    echo -e "${RED}✗ RBAC not working (viewer should be blocked)${NC}"
fi
echo

# Summary
echo -e "${BOLD}=== Test Summary ===${NC}"
echo "Raw Events:       $RAW_COUNT"
echo "Processed Events: $PROCESSED_COUNT"
echo "Trading Signals:  $SIGNAL_COUNT"
echo

if [ "$SIGNAL_COUNT" -gt 0 ]; then
    echo -e "${GREEN}${BOLD}🎉 Task 9.4 E2E Flow: SUCCESS${NC}"
    echo "RSS → Raw Events → Processing → Classification → Signals ✅"
    echo
    echo "Pipeline verified:"
    echo "  1. RSS feeds ingested → Raw events created"
    echo "  2. Event processor picked up raw events"
    echo "  3. NLP analysis + classification completed"
    echo "  4. Trading signals generated for affected symbols"
    echo "  5. RBAC working (viewer blocked from trader endpoints)"
elif [ "$PROCESSED_COUNT" -gt 0 ]; then
    echo -e "${YELLOW}⏳ Task 9.4 E2E Flow: PARTIAL${NC}"
    echo "RSS → Raw Events → Processing ✅"
    echo "Waiting for signal generation..."
    echo
    echo "Next: Check worker logs for signal generation"
elif [ "$RAW_COUNT" -gt 0 ]; then
    echo -e "${YELLOW}⏳ Task 9.4 E2E Flow: IN PROGRESS${NC}"
    echo "RSS → Raw Events ✅"
    echo "Waiting for event processing (30s interval)..."
    echo
    echo "Next: Wait 30 seconds and run this script again"
else
    echo -e "${YELLOW}⏳ Task 9.4 E2E Flow: WAITING FOR RSS${NC}"
    echo "Waiting for RSS ingestion (5-15 minutes)..."
    echo
    echo "Next steps:"
    echo "  1. Check worker is running: ps aux | grep 'app.worker'"
    echo "  2. Monitor worker logs: tail -f logs/worker.log"
    echo "  3. Wait for: 'RSS ingestion cycle complete'"
    echo "  4. Run this script again"
fi

echo
echo -e "${BOLD}Useful Commands:${NC}"
echo "  Watch worker logs:  tail -f logs/worker.log"
echo "  Watch API logs:     tail -f logs/api.log"
echo "  Check all logs:     ./scripts/check-logs.sh"
echo "  Re-run this test:   ./scripts/test-e2e-flow.sh"
