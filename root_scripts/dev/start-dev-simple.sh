#!/bin/bash

# Simple launcher using gnome-terminal (works on most Linux desktops)

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}🚀 Starting Cortex AI (Simple Mode)${NC}"

# Check if gnome-terminal is available
if command -v gnome-terminal &> /dev/null; then
    TERM_CMD="gnome-terminal"
elif command -v xterm &> /dev/null; then
    TERM_CMD="xterm -e"
else
    echo -e "${YELLOW}⚠️  No terminal emulator found. Use start-dev.sh with tmux instead${NC}"
    exit 1
fi

PROJECT_DIR=$(pwd)

# Start backend
gnome-terminal --tab --title="Backend (FastAPI)" -- bash -c "
    cd $PROJECT_DIR/backend
    echo '🐍 Starting FastAPI Backend...'
    source venv/bin/activate 2>/dev/null || source ../.venv/bin/activate 2>/dev/null || true
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    exec bash
"

# Start frontend
gnome-terminal --tab --title="Frontend (Next.js)" -- bash -c "
    cd $PROJECT_DIR/frontend
    echo '⚛️  Starting Next.js Frontend...'
    npm run dev
    exec bash
"

echo -e "${GREEN}✅ Started in separate terminals${NC}"
echo ""
echo -e "${GREEN}🔗 URLs:${NC}"
echo "  Backend:  http://localhost:8000"
echo "  Frontend: http://localhost:3000"
echo "  API Docs: http://localhost:8000/docs"
