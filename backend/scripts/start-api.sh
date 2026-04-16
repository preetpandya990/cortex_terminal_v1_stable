#!/usr/bin/env bash
# Start API server with file logging

BACKEND_DIR="/home/preet/code/Cortex_Merge_AI-ML/backend"
LOG_DIR="$BACKEND_DIR/logs"
LOG_FILE="$LOG_DIR/api.log"

# Create logs directory
mkdir -p "$LOG_DIR"

# Kill existing API process
pkill -f "uvicorn app.main:app" 2>/dev/null || true
sleep 1

echo "Starting Cortex API server..."
echo "Logs: $LOG_FILE"

cd "$BACKEND_DIR"

# Start API with logging to file
nohup .venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --log-level info \
  > "$LOG_FILE" 2>&1 &

API_PID=$!
echo "API started with PID: $API_PID"

# Wait a moment and check if it's running
sleep 2

if ps -p $API_PID > /dev/null; then
    echo "✓ API is running"
    echo
    echo "Commands:"
    echo "  Check logs:  tail -f $LOG_FILE"
    echo "  Stop API:    pkill -f 'uvicorn app.main:app'"
    echo "  Health:      curl http://localhost:8000/health"
else
    echo "❌ API failed to start. Check logs:"
    tail -20 "$LOG_FILE"
    exit 1
fi
