#!/bin/bash

# Stop Cortex AI Development Environment

SESSION_NAME="cortex-dev"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}🛑 Stopping Cortex AI Development Environment${NC}"

if tmux has-session -t $SESSION_NAME 2>/dev/null; then
    tmux kill-session -t $SESSION_NAME
    echo -e "${GREEN}✅ Session killed${NC}"
    
    # Clean up log files
    rm -f /tmp/cortex-backend.log /tmp/cortex-frontend.log
    echo -e "${GREEN}✅ Log files cleaned${NC}"
else
    echo -e "${RED}❌ No session found${NC}"
fi
