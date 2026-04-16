#!/bin/bash

# Cortex AI - Application Stop Script

echo "🛑 Stopping Cortex AI Application..."

# Kill backend
if [ -f /tmp/cortex_backend.pid ]; then
    BACKEND_PID=$(cat /tmp/cortex_backend.pid)
    if kill -0 $BACKEND_PID 2>/dev/null; then
        echo "Stopping backend (PID: $BACKEND_PID)..."
        kill $BACKEND_PID
    fi
    rm /tmp/cortex_backend.pid
fi

# Kill frontend
if [ -f /tmp/cortex_frontend.pid ]; then
    FRONTEND_PID=$(cat /tmp/cortex_frontend.pid)
    if kill -0 $FRONTEND_PID 2>/dev/null; then
        echo "Stopping frontend (PID: $FRONTEND_PID)..."
        kill $FRONTEND_PID
    fi
    rm /tmp/cortex_frontend.pid
fi

# Fallback: kill by process name
pkill -f "uvicorn" 2>/dev/null || true
pkill -f "next dev" 2>/dev/null || true

echo "✓ Application stopped"
