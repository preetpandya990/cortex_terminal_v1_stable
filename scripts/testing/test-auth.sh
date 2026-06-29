#!/bin/bash

# Test authentication flow

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}🔐 Testing Authentication Flow${NC}"
echo ""

# Test 1: Dev Login
echo -n "1. Testing dev-login endpoint... "
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST http://localhost:8000/api/v1/auth/dev-login)
HTTP_CODE=$(echo "$RESPONSE" | tail -n1)
BODY=$(echo "$RESPONSE" | head -n-1)

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "${GREEN}✅ Success${NC}"
    ACCESS_TOKEN=$(echo "$BODY" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
    echo "   Access token: ${ACCESS_TOKEN:0:50}..."
else
    echo -e "${RED}❌ Failed (HTTP $HTTP_CODE)${NC}"
    echo "   Response: $BODY"
    exit 1
fi

echo ""

# Test 2: Use token to access protected endpoint
echo -n "2. Testing protected endpoint with token... "
PROTECTED_RESPONSE=$(curl -s -w "\n%{http_code}" \
    -H "Authorization: Bearer $ACCESS_TOKEN" \
    http://localhost:8000/api/v1/auth/me)
HTTP_CODE=$(echo "$PROTECTED_RESPONSE" | tail -n1)

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "${GREEN}✅ Success${NC}"
    USER_INFO=$(echo "$PROTECTED_RESPONSE" | head -n-1)
    echo "   User: $(echo "$USER_INFO" | grep -o '"username":"[^"]*"' | cut -d'"' -f4)"
    echo "   Role: $(echo "$USER_INFO" | grep -o '"role":"[^"]*"' | cut -d'"' -f4)"
else
    echo -e "${RED}❌ Failed (HTTP $HTTP_CODE)${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}✅ Authentication flow working correctly!${NC}"
echo ""
echo "You can now:"
echo "  1. Click 'Dev Login' button in the UI"
echo "  2. Access protected endpoints"
echo "  3. View authenticated user status"
