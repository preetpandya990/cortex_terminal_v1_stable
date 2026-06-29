#!/bin/bash
# Quick Reference - Cortex AI Development

cat << 'EOF'
╔══════════════════════════════════════════════════════════════╗
║           CORTEX AI - DEVELOPMENT QUICK START               ║
╚══════════════════════════════════════════════════════════════╝

🚀 START SERVICES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Recommended (with monitoring):
    ./start-dev.sh

  Simple (separate terminals):
    ./start-dev-simple.sh

🛑 STOP SERVICES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    ./stop-dev.sh

🏥 HEALTH CHECK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    ./health-check.sh

🔗 URLS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Frontend:  http://localhost:3000
  Backend:   http://localhost:8000
  API Docs:  http://localhost:8000/docs

⌨️  TMUX COMMANDS (when using start-dev.sh)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Navigate:     Ctrl+B then Arrow Keys
  Detach:       Ctrl+B then D
  Re-attach:    tmux attach -t cortex-dev
  Scroll:       Ctrl+B then [ (q to exit)
  Zoom pane:    Ctrl+B then Z

📋 LOGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Backend:   tail -f /tmp/cortex-backend.log
  Frontend:  tail -f /tmp/cortex-frontend.log

🔧 TROUBLESHOOTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Port 8000 busy:  kill -9 $(lsof -t -i :8000)
  Port 3000 busy:  kill -9 $(lsof -t -i :3000)
  
  Backend deps:    cd backend && pip install -r requirements.txt
  Frontend deps:   cd frontend && npm install

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EOF
