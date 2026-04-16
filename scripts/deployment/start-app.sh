#!/bin/bash

# Cortex AI - Application Startup Script
# Starts both backend (FastAPI) and frontend (Next.js)

set -e

echo "🚀 Starting Cortex AI Application..."
echo ""

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if Docker services are running
echo -e "${BLUE}Checking Docker services...${NC}"
if ! docker ps | grep -q "cortex-postgres"; then
    echo -e "${YELLOW}⚠️  PostgreSQL not running. Starting Docker services...${NC}"
    cd /home/preet/code/Cortex_Merge_AI-ML
    docker-compose up -d
    echo "Waiting for services to be ready..."
    sleep 5
fi

# Check if ports are available
if lsof -i :8000 >/dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  Port 8000 is already in use. Stopping existing backend...${NC}"
    pkill -f "uvicorn" || true
    sleep 2
fi

if lsof -i :3000 >/dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  Port 3000 is already in use. Stopping existing frontend...${NC}"
    pkill -f "next dev" || true
    sleep 2
fi

# Start Backend
echo ""
echo -e "${BLUE}Starting Backend (FastAPI)...${NC}"
cd /home/preet/code/Cortex_Merge_AI-ML/backend

# Activate virtual environment and start backend in background
source .venv/bin/activate
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload > backend.log 2>&1 &
BACKEND_PID=$!
echo -e "${GREEN}✓ Backend started (PID: $BACKEND_PID)${NC}"
echo "  URL: http://localhost:8000"
echo "  Docs: http://localhost:8000/docs"
echo "  Logs: backend/backend.log"

# Wait for backend to be ready
echo ""
echo "Waiting for backend to be ready..."
for i in {1..30}; do
    if curl -s http://localhost:8000/health >/dev/null 2>&1; then
        echo -e "${GREEN}✓ Backend is ready!${NC}"
        break
    fi
    if [ $i -eq 30 ]; then
        echo -e "${YELLOW}⚠️  Backend taking longer than expected. Check backend.log${NC}"
    fi
    sleep 1
done

# Start Frontend
echo ""
echo -e "${BLUE}Starting Frontend (Next.js)...${NC}"
cd /home/preet/code/Cortex_Merge_AI-ML/frontend

# Start frontend in background
nohup npm run dev > frontend.log 2>&1 &
FRONTEND_PID=$!
echo -e "${GREEN}✓ Frontend started (PID: $FRONTEND_PID)${NC}"
echo "  URL: http://localhost:3000"
echo "  Logs: frontend/frontend.log"

# Wait for frontend to be ready
echo ""
echo "Waiting for frontend to be ready..."
for i in {1..30}; do
    if curl -s http://localhost:3000 >/dev/null 2>&1; then
        echo -e "${GREEN}✓ Frontend is ready!${NC}"
        break
    fi
    if [ $i -eq 30 ]; then
        echo -e "${YELLOW}⚠️  Frontend taking longer than expected. Check frontend.log${NC}"
    fi
    sleep 1
done

# Summary
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}🎉 Cortex AI Application Started!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Services:"
echo "  • Backend:  http://localhost:8000"
echo "  • API Docs: http://localhost:8000/docs"
echo "  • Frontend: http://localhost:3000"
echo ""
echo "Process IDs:"
echo "  • Backend:  $BACKEND_PID"
echo "  • Frontend: $FRONTEND_PID"
echo ""
echo "Logs:"
echo "  • Backend:  tail -f backend/backend.log"
echo "  • Frontend: tail -f frontend/frontend.log"
echo ""
echo "To stop:"
echo "  • Backend:  kill $BACKEND_PID"
echo "  • Frontend: kill $FRONTEND_PID"
echo "  • Or run:   pkill -f 'uvicorn|next dev'"
echo ""
echo -e "${BLUE}Press Ctrl+C to view logs, or open http://localhost:3000 in your browser${NC}"
echo ""

# Save PIDs to file for easy stopping
echo "$BACKEND_PID" > /tmp/cortex_backend.pid
echo "$FRONTEND_PID" > /tmp/cortex_frontend.pid

# Follow logs
tail -f backend/backend.log frontend/frontend.log
