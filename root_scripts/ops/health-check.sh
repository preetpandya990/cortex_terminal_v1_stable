#!/bin/bash

# Health check script - monitors both services

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "🏥 Cortex AI Health Check"
echo "=========================="
echo ""

# Check Backend
echo -n "Backend (http://localhost:8000): "
if curl -s http://localhost:8000/health > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Running${NC}"
    BACKEND_VERSION=$(curl -s http://localhost:8000/health | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
    echo "  Version: $BACKEND_VERSION"
else
    echo -e "${RED}❌ Not responding${NC}"
fi

echo ""

# Check Frontend
echo -n "Frontend (http://localhost:3000): "
if curl -s http://localhost:3000 > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Running${NC}"
else
    echo -e "${RED}❌ Not responding${NC}"
fi

echo ""

# Check Database
echo -n "Database Connection: "
DB_CHECK=$(curl -s http://localhost:8000/api/v1/ml/health 2>/dev/null | grep -o '"database":{[^}]*}')
if echo "$DB_CHECK" | grep -q '"status":"healthy"'; then
    echo -e "${GREEN}✅ Connected${NC}"
else
    echo -e "${RED}❌ Not connected${NC}"
fi

echo ""

# Check Redis
echo -n "Redis Connection: "
REDIS_CHECK=$(curl -s http://localhost:8000/api/v1/ml/health 2>/dev/null | grep -o '"redis":{[^}]*}')
if echo "$REDIS_CHECK" | grep -q '"status":"healthy"'; then
    echo -e "${GREEN}✅ Connected${NC}"
else
    echo -e "${RED}❌ Not connected${NC}"
fi

echo ""
echo "=========================="
echo "Run './start-dev.sh' to start all services"
