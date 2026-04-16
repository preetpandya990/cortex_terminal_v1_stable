#!/bin/bash
# Enable TimescaleDB - Migration Script
# This script recreates the database container with TimescaleDB support

set -e

echo "🔄 Enabling TimescaleDB Extension"
echo "=================================="
echo ""

# Check if docker-compose is available
if ! command -v docker &> /dev/null; then
    echo "❌ Docker not found. Please install Docker first."
    exit 1
fi

echo "⚠️  WARNING: This will recreate the database container"
echo "   Current data will be preserved in the volume"
echo ""
read -p "Continue? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "1️⃣  Stopping current database container..."
docker stop cortex_merge_ai-ml-db-1 || true

echo ""
echo "2️⃣  Removing old container (data volume preserved)..."
docker rm cortex_merge_ai-ml-db-1 || true

echo ""
echo "3️⃣  Starting new TimescaleDB container..."
docker compose up -d db

echo ""
echo "4️⃣  Waiting for database to be ready..."
sleep 10

echo ""
echo "5️⃣  Enabling TimescaleDB extension..."
docker exec -i cortex-db psql -U cortex -d cortex_db << 'EOF'
CREATE EXTENSION IF NOT EXISTS timescaledb;
SELECT extname, extversion FROM pg_extension WHERE extname = 'timescaledb';
EOF

echo ""
echo "6️⃣  Converting time-series tables to hypertables..."
docker exec -i cortex-db psql -U cortex -d cortex_db << 'EOF'
-- Convert OHLCV data to hypertable
SELECT create_hypertable('upstox_ohlcv', 'timestamp', 
    if_not_exists => TRUE,
    migrate_data => TRUE
);

-- Convert tick data to hypertable
SELECT create_hypertable('upstox_ticks', 'timestamp',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

-- Convert ML predictions to hypertable
SELECT create_hypertable('ml_predictions', 'timestamp',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

-- Convert trading signals to hypertable
SELECT create_hypertable('ai_trading_signals', 'created_at',
    if_not_exists => TRUE,
    migrate_data => TRUE
);

-- Verify hypertables
SELECT hypertable_name, num_chunks 
FROM timescaledb_information.hypertables
ORDER BY hypertable_name;
EOF

echo ""
echo "7️⃣  Enabling compression on OHLCV data..."
docker exec -i cortex-db psql -U cortex -d cortex_db << 'EOF'
-- Enable compression on OHLCV (compress by instrument_key)
ALTER TABLE upstox_ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_key,timeframe'
);

-- Add compression policy (compress data older than 7 days)
SELECT add_compression_policy('upstox_ohlcv', INTERVAL '7 days');

-- Verify compression settings
SELECT * FROM timescaledb_information.compression_settings
WHERE hypertable_name = 'upstox_ohlcv';
EOF

echo ""
echo "✅ TimescaleDB enabled successfully!"
echo ""
echo "📊 Summary:"
echo "  - Extension: timescaledb"
echo "  - Hypertables: 4 (upstox_ohlcv, upstox_ticks, ml_predictions, ai_trading_signals)"
echo "  - Compression: Enabled on upstox_ohlcv (7-day policy)"
echo ""
echo "🔍 Verify with:"
echo "  docker exec -it cortex-db psql -U cortex -d cortex_db -c \"\\dx timescaledb\""
echo ""
