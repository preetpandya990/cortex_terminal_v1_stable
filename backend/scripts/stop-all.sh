#!/usr/bin/env bash
# Stop all Cortex services

echo "Stopping Cortex services..."

# Stop API
if pkill -f "uvicorn app.main:app"; then
    echo "✓ API stopped"
else
    echo "  API was not running"
fi

# Stop Worker
if pkill -f "python -m app.worker"; then
    echo "✓ Worker stopped"
else
    echo "  Worker was not running"
fi

echo
echo "All services stopped"
