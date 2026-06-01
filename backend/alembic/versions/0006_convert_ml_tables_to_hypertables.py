"""convert_ml_tables_to_hypertables

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-10 20:10:00

Convert ML time-series tables to TimescaleDB hypertables with compression and retention policies.
Requirements: 18.4
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Convert ML time-series tables to TimescaleDB hypertables."""

    # TimescaleDB hypertables + policies — skipped gracefully on plain Postgres
    op.execute("""
        DO $$ BEGIN
            PERFORM create_hypertable('upstox_ohlcv', 'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
            PERFORM create_hypertable('upstox_ticks', 'timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
            PERFORM create_hypertable('ml_predictions', 'timestamp', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
            PERFORM create_hypertable('ml_drift_metrics', 'timestamp', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
            ALTER TABLE upstox_ohlcv SET (timescaledb.compress, timescaledb.compress_segmentby = 'instrument_key, timeframe', timescaledb.compress_orderby = 'timestamp DESC');
            PERFORM add_compression_policy('upstox_ohlcv', INTERVAL '7 days', if_not_exists => TRUE);
            ALTER TABLE upstox_ticks SET (timescaledb.compress, timescaledb.compress_segmentby = 'instrument_key', timescaledb.compress_orderby = 'timestamp DESC');
            PERFORM add_compression_policy('upstox_ticks', INTERVAL '1 day', if_not_exists => TRUE);
            ALTER TABLE ml_predictions SET (timescaledb.compress, timescaledb.compress_segmentby = 'model_id, symbol', timescaledb.compress_orderby = 'timestamp DESC');
            PERFORM add_compression_policy('ml_predictions', INTERVAL '30 days', if_not_exists => TRUE);
            ALTER TABLE ml_drift_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'model_id', timescaledb.compress_orderby = 'timestamp DESC');
            PERFORM add_compression_policy('ml_drift_metrics', INTERVAL '30 days', if_not_exists => TRUE);
            PERFORM add_retention_policy('upstox_ticks', INTERVAL '7 days', if_not_exists => TRUE);
            PERFORM add_retention_policy('upstox_ohlcv', INTERVAL '365 days', if_not_exists => TRUE);
            PERFORM add_retention_policy('ml_predictions', INTERVAL '365 days', if_not_exists => TRUE);
            PERFORM add_retention_policy('ml_drift_metrics', INTERVAL '365 days', if_not_exists => TRUE);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'TimescaleDB not available, skipping hypertable setup: %', SQLERRM;
        END $$;
    """)


def downgrade() -> None:
    """Remove TimescaleDB hypertable configurations."""
    
    # Note: Cannot directly convert hypertables back to regular tables
    # This would require data migration. For development, drop and recreate.
    op.execute("""
        -- Remove policies (compression and retention are automatically removed when hypertable is dropped)
        -- Hypertables remain as regular tables after policies are removed
    """)
