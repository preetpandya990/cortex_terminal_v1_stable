#!/bin/bash

# Verify Upstox mock data is working

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}🔍 Testing Upstox Integration${NC}"
echo ""

# Get auth token first
echo -n "1. Getting auth token... "
AUTH_RESPONSE=$(curl -s -X POST http://localhost:8000/api/v1/auth/dev-login)
ACCESS_TOKEN=$(echo "$AUTH_RESPONSE" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)

if [ -z "$ACCESS_TOKEN" ]; then
    echo -e "${RED}❌ Failed to get token${NC}"
    exit 1
fi
echo -e "${GREEN}✅${NC}"

# Test market data endpoint
echo -n "2. Testing market data endpoint... "
MARKET_RESPONSE=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    "http://localhost:8000/api/v1/market-data/live/NSE_EQ|INE002A01018")
HTTP_CODE=$(echo "$MARKET_RESPONSE" | tail -n1)
BODY=$(echo "$MARKET_RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "${GREEN}✅ Success${NC}"
    PRICE=$(echo "$BODY" | grep -o '"last_price":[0-9.]*' | cut -d':' -f2)
    echo "   Last Price: ₹$PRICE"
else
    echo -e "${RED}❌ Failed (HTTP $HTTP_CODE)${NC}"
    echo "   Response: $BODY"
    exit 1
fi

echo ""

# Test candles endpoint
echo -n "3. Testing candles endpoint... "
CANDLES_RESPONSE=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    "http://localhost:8000/api/v1/upstox/candles/intraday?instrument_key=NSE_EQ|INE002A01018&interval=1&unit=minutes")
HTTP_CODE=$(echo "$CANDLES_RESPONSE" | tail -n1)

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "${GREEN}✅ Success${NC}"
    CANDLE_COUNT=$(echo "$CANDLES_RESPONSE" | head -n-1 | grep -o '"candles":\[' | wc -l)
    echo "   Candles received: $CANDLE_COUNT"
else
    echo -e "${RED}❌ Failed (HTTP $HTTP_CODE)${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}✅ All tests passed!${NC}"
echo ""
echo -e "${YELLOW}Note:${NC} If you see mock data warnings in backend logs, that's expected."
echo "The system is using realistic mock data because Upstox token is expired."
