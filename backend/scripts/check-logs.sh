#!/usr/bin/env bash
# Check logs for errors

BACKEND_DIR="/home/preet/code/Cortex_Merge_AI-ML/backend"
LOG_DIR="$BACKEND_DIR/logs"

echo "=== Cortex Logs Check ==="
echo

# Check API logs
if [ -f "$LOG_DIR/api.log" ]; then
    echo "📋 API Logs (last 30 lines):"
    echo "─────────────────────────────────────────"
    tail -30 "$LOG_DIR/api.log"
    echo
    
    # Check for errors
    ERROR_COUNT=$(grep -i "error\|exception\|traceback" "$LOG_DIR/api.log" | wc -l)
    if [ $ERROR_COUNT -gt 0 ]; then
        echo "⚠️  Found $ERROR_COUNT error lines in API logs"
        echo "Recent errors:"
        grep -i "error\|exception" "$LOG_DIR/api.log" | tail -5
    else
        echo "✓ No errors in API logs"
    fi
else
    echo "❌ API log file not found: $LOG_DIR/api.log"
fi

echo
echo "─────────────────────────────────────────"
echo

# Check Worker logs
if [ -f "$LOG_DIR/worker.log" ]; then
    echo "📋 Worker Logs (last 30 lines):"
    echo "─────────────────────────────────────────"
    tail -30 "$LOG_DIR/worker.log"
    echo
    
    # Check for errors
    ERROR_COUNT=$(grep -i "error\|exception\|traceback" "$LOG_DIR/worker.log" | wc -l)
    if [ $ERROR_COUNT -gt 0 ]; then
        echo "⚠️  Found $ERROR_COUNT error lines in Worker logs"
        echo "Recent errors:"
        grep -i "error\|exception" "$LOG_DIR/worker.log" | tail -5
    else
        echo "✓ No errors in Worker logs"
    fi
else
    echo "❌ Worker log file not found: $LOG_DIR/worker.log"
fi

echo
echo "─────────────────────────────────────────"
echo "Commands:"
echo "  Watch API logs:    tail -f $LOG_DIR/api.log"
echo "  Watch Worker logs: tail -f $LOG_DIR/worker.log"
echo "  Full API errors:   grep -i error $LOG_DIR/api.log"
echo "  Full Worker errors: grep -i error $LOG_DIR/worker.log"
