#!/usr/bin/env bash
# Start Worker process with file logging

BACKEND_DIR="/home/preet/code/Cortex_Merge_AI-ML/backend"
LOG_DIR="$BACKEND_DIR/logs"
LOG_FILE="$LOG_DIR/worker.log"

# Create logs directory
mkdir -p "$LOG_DIR"

# Kill existing worker process
pkill -f "python -m app.worker" 2>/dev/null || true
sleep 1

echo "Starting Cortex Worker process..."
echo "Logs: $LOG_FILE"

cd "$BACKEND_DIR"

# Start worker with logging to file
nohup .venv/bin/python -m app.worker \
  > "$LOG_FILE" 2>&1 &

WORKER_PID=$!
echo "Worker started with PID: $WORKER_PID"

# Wait a moment and check if it's running
sleep 2

if ps -p $WORKER_PID > /dev/null; then
    echo "✓ Worker is running"
    echo
    echo "Commands:"
    echo "  Check logs:  tail -f $LOG_FILE"
    echo "  Stop worker: pkill -f 'python -m app.worker'"
else
    echo "❌ Worker failed to start. Check logs:"
    tail -20 "$LOG_FILE"
    exit 1
fi
