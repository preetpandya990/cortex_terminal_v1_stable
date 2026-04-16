#!/usr/bin/env bash
# Install Cortex Worker as systemd service

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/cortex-worker.service"
SYSTEMD_DIR="/etc/systemd/system"

echo "=== Cortex Worker Service Installation ==="
echo

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "❌ This script must be run as root (use sudo)"
    exit 1
fi

# Check if service file exists
if [ ! -f "$SERVICE_FILE" ]; then
    echo "❌ Service file not found: $SERVICE_FILE"
    exit 1
fi

# Stop existing service if running
if systemctl is-active --quiet cortex-worker; then
    echo "⏸️  Stopping existing cortex-worker service..."
    systemctl stop cortex-worker
fi

# Copy service file
echo "📋 Installing service file..."
cp "$SERVICE_FILE" "$SYSTEMD_DIR/cortex-worker.service"
chmod 644 "$SYSTEMD_DIR/cortex-worker.service"

# Reload systemd
echo "🔄 Reloading systemd daemon..."
systemctl daemon-reload

# Enable service
echo "✅ Enabling cortex-worker service..."
systemctl enable cortex-worker

echo
echo "=== Installation Complete ==="
echo
echo "Service commands:"
echo "  Start:   sudo systemctl start cortex-worker"
echo "  Stop:    sudo systemctl stop cortex-worker"
echo "  Restart: sudo systemctl restart cortex-worker"
echo "  Status:  sudo systemctl status cortex-worker"
echo "  Logs:    sudo journalctl -u cortex-worker -f"
echo
echo "To start the service now, run:"
echo "  sudo systemctl start cortex-worker"
