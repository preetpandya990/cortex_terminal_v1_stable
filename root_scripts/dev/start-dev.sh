#!/bin/bash

# Cortex AI - Development Environment Launcher
# Starts backend and frontend in tmux with live log monitoring

set -e

SESSION_NAME="cortex-dev"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}🚀 Starting Cortex AI Development Environment${NC}"

# Check if tmux is installed
if ! command -v tmux &> /dev/null; then
    echo -e "${RED}❌ tmux is not installed. Installing...${NC}"
    sudo apt-get update && sudo apt-get install -y tmux
fi

# Kill existing session if it exists
if tmux has-session -t $SESSION_NAME 2>/dev/null; then
    echo -e "${YELLOW}⚠️  Existing session found. Killing...${NC}"
    tmux kill-session -t $SESSION_NAME
fi

# Create new tmux session
echo -e "${GREEN}📦 Creating tmux session: $SESSION_NAME${NC}"

# Create session with backend window
tmux new-session -d -s $SESSION_NAME -n "backend"

# Split window horizontally for frontend
tmux split-window -h -t $SESSION_NAME:0

# Split each pane vertically for logs
tmux split-window -v -t $SESSION_NAME:0.0
tmux split-window -v -t $SESSION_NAME:0.2

# Now we have 4 panes:
# 0: Backend process
# 1: Backend logs
# 2: Frontend process  
# 3: Frontend logs

# Configure backend pane (top-left)
tmux send-keys -t $SESSION_NAME:0.0 "cd $(pwd)/backend" C-m
tmux send-keys -t $SESSION_NAME:0.0 "echo '🐍 Starting FastAPI Backend...'" C-m
tmux send-keys -t $SESSION_NAME:0.0 "source venv/bin/activate 2>/dev/null || source ../.venv/bin/activate 2>/dev/null || echo 'No venv found, using system python'" C-m
tmux send-keys -t $SESSION_NAME:0.0 "uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload 2>&1 | tee /tmp/cortex-backend.log" C-m

# Configure backend logs pane (bottom-left)
tmux send-keys -t $SESSION_NAME:0.1 "cd $(pwd)/backend" C-m
tmux send-keys -t $SESSION_NAME:0.1 "echo '📋 Backend Logs (watching for errors...)'" C-m
tmux send-keys -t $SESSION_NAME:0.1 "sleep 3 && tail -f /tmp/cortex-backend.log | grep --line-buffered -E 'ERROR|CRITICAL|Exception|Traceback|Failed|Error'" C-m

# Configure frontend pane (top-right)
tmux send-keys -t $SESSION_NAME:0.2 "cd $(pwd)/frontend" C-m
tmux send-keys -t $SESSION_NAME:0.2 "echo '⚛️  Starting Next.js Frontend...'" C-m
tmux send-keys -t $SESSION_NAME:0.2 "npm run dev 2>&1 | tee /tmp/cortex-frontend.log" C-m

# Configure frontend logs pane (bottom-right)
tmux send-keys -t $SESSION_NAME:0.3 "cd $(pwd)/frontend" C-m
tmux send-keys -t $SESSION_NAME:0.3 "echo '📋 Frontend Logs (watching for errors...)'" C-m
tmux send-keys -t $SESSION_NAME:0.3 "sleep 3 && tail -f /tmp/cortex-frontend.log | grep --line-buffered -E 'ERROR|Error|error|Failed|failed|Exception|Warning'" C-m

# Set pane titles
tmux select-pane -t $SESSION_NAME:0.0 -T "Backend Process"
tmux select-pane -t $SESSION_NAME:0.1 -T "Backend Errors"
tmux select-pane -t $SESSION_NAME:0.2 -T "Frontend Process"
tmux select-pane -t $SESSION_NAME:0.3 -T "Frontend Errors"

# Resize panes for better visibility
tmux resize-pane -t $SESSION_NAME:0.1 -y 15
tmux resize-pane -t $SESSION_NAME:0.3 -y 15

echo ""
echo -e "${GREEN}✅ Development environment started!${NC}"
echo ""
echo -e "${YELLOW}📺 Layout:${NC}"
echo "  ┌─────────────────┬─────────────────┐"
echo "  │  Backend (API)  │  Frontend (UI)  │"
echo "  ├─────────────────┼─────────────────┤"
echo "  │ Backend Errors  │ Frontend Errors │"
echo "  └─────────────────┴─────────────────┘"
echo ""
echo -e "${GREEN}🔗 URLs:${NC}"
echo "  Backend:  http://localhost:8000"
echo "  Frontend: http://localhost:3000"
echo "  API Docs: http://localhost:8000/docs"
echo ""
echo -e "${YELLOW}⌨️  Commands:${NC}"
echo "  Attach:   tmux attach -t $SESSION_NAME"
echo "  Detach:   Ctrl+B then D"
echo "  Stop:     ./stop-dev.sh (or tmux kill-session -t $SESSION_NAME)"
echo "  Navigate: Ctrl+B then arrow keys"
echo ""

# Attach to session
tmux attach -t $SESSION_NAME
