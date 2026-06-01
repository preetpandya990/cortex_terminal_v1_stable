"""create_ai_tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-10 20:00:00

Creates 13 AI microservice tables with TimescaleDB hypertables.
Requirements: 16.1, 16.2, 16.3, 16.4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create all 13 AI tables with indexes and hypertables."""
    
    # ai_raw_events (TimescaleDB hypertable)
    op.create_table(
        'ai_raw_events',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('event_timestamp', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('source_type', sa.String(50), nullable=False),
        sa.Column('source_url', sa.String(500), nullable=False),
        sa.Column('source_name', sa.String(200), nullable=False),
        sa.Column('content_hash', sa.String(64), nullable=False, unique=True, index=True),
        sa.Column('raw_content', sa.Text(), nullable=False),
        sa.Column('language', sa.String(10), nullable=True),
        sa.Column('metadata', postgresql.JSONB(), nullable=True),
        sa.Column('ingested_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('idx_ai_raw_events_source_type', 'ai_raw_events', ['source_type'])
    op.create_index('idx_ai_raw_events_ingested_at', 'ai_raw_events', ['ingested_at'])
    
    # ai_processed_events (TimescaleDB hypertable)
    op.create_table(
        'ai_processed_events',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('raw_event_id', sa.BigInteger(), sa.ForeignKey('ai_raw_events.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('processed_timestamp', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('translated_content', sa.Text(), nullable=True),
        sa.Column('detected_language', sa.String(10), nullable=True),
        sa.Column('translation_confidence', sa.Numeric(5, 2), nullable=True),
        sa.Column('processing_status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('idx_ai_processed_events_status', 'ai_processed_events', ['processing_status'])
    
    # ai_nlp_results
    op.create_table(
        'ai_nlp_results',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('processed_event_id', sa.BigInteger(), sa.ForeignKey('ai_processed_events.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('sentiment_score', sa.Numeric(5, 2), nullable=True),
        sa.Column('sentiment_label', sa.String(20), nullable=True),
        sa.Column('named_entities', postgresql.JSONB(), nullable=True),
        sa.Column('keywords', postgresql.JSONB(), nullable=True),
        sa.Column('model_used', sa.String(50), nullable=False),
        sa.Column('confidence_score', sa.Numeric(5, 2), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    
    # ai_event_classifications
    op.create_table(
        'ai_event_classifications',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('nlp_result_id', sa.BigInteger(), sa.ForeignKey('ai_nlp_results.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('event_type', sa.String(50), nullable=False, index=True),
        sa.Column('impact_score', sa.Numeric(5, 2), nullable=False),
        sa.Column('affected_symbols', postgresql.ARRAY(sa.String(20)), nullable=True, index=True),
        sa.Column('classification_confidence', sa.Numeric(5, 2), nullable=False),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('decay_half_life_hours', sa.Integer(), nullable=False, server_default='24'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('idx_ai_event_classifications_event_type', 'ai_event_classifications', ['event_type'])
    
    # ai_source_credibility
    op.create_table(
        'ai_source_credibility',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('source_name', sa.String(200), nullable=False, unique=True, index=True),
        sa.Column('source_type', sa.String(50), nullable=False),
        sa.Column('credibility_score', sa.Numeric(5, 2), nullable=False, server_default='50.0'),
        sa.Column('total_events', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('confirmed_events', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('contradicted_events', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_updated', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    
    # ai_fake_news_flags
    op.create_table(
        'ai_fake_news_flags',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('event_classification_id', sa.BigInteger(), sa.ForeignKey('ai_event_classifications.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('flag_status', sa.String(20), nullable=False),
        sa.Column('detection_layers', postgresql.JSONB(), nullable=False),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('flagged_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    
    # ai_ml_models
    op.create_table(
        'ai_ml_models',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('model_name', sa.String(100), nullable=False, unique=True, index=True),
        sa.Column('model_type', sa.String(50), nullable=False),
        sa.Column('deployment_state', sa.String(20), nullable=False, server_default='shadow'),
        sa.Column('model_version', sa.String(50), nullable=False),
        sa.Column('model_path', sa.String(500), nullable=True),
        sa.Column('training_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('accuracy', sa.Numeric(5, 2), nullable=True),
        sa.Column('precision', sa.Numeric(5, 2), nullable=True),
        sa.Column('recall', sa.Numeric(5, 2), nullable=True),
        sa.Column('f1_score', sa.Numeric(5, 2), nullable=True),
        sa.Column('governance_metadata', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('idx_ai_ml_models_deployment_state', 'ai_ml_models', ['deployment_state'])
    
    # ai_drift_reports
    op.create_table(
        'ai_drift_reports',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('model_id', sa.BigInteger(), sa.ForeignKey('ai_ml_models.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('report_timestamp', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('drift_detected', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('drift_score', sa.Numeric(5, 2), nullable=True),
        sa.Column('accuracy_drop', sa.Numeric(5, 2), nullable=True),
        sa.Column('distribution_metrics', postgresql.JSONB(), nullable=True),
        sa.Column('action_taken', sa.String(50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    
    # ai_regime_detections (TimescaleDB hypertable)
    op.create_table(
        'ai_regime_detections',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('detection_timestamp', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('regime_type', sa.String(50), nullable=False, index=True),
        sa.Column('confidence_score', sa.Numeric(5, 2), nullable=False),
        sa.Column('contributing_factors', postgresql.JSONB(), nullable=True),
        sa.Column('volatility_index', sa.Numeric(10, 4), nullable=True),
        sa.Column('volume_index', sa.Numeric(10, 4), nullable=True),
        sa.Column('trend_strength', sa.Numeric(5, 2), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('idx_ai_regime_detections_regime_type', 'ai_regime_detections', ['regime_type'])
    
    # ai_active_strategies
    op.create_table(
        'ai_active_strategies',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('strategy_id', sa.String(100), nullable=False, index=True),
        sa.Column('strategy_name', sa.String(200), nullable=False),
        sa.Column('regime_type', sa.String(50), nullable=False, index=True),
        sa.Column('priority_level', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        sa.Column('activated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('configuration', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('idx_ai_active_strategies_status', 'ai_active_strategies', ['status'])
    
    # ai_trading_signals (TimescaleDB hypertable)
    op.create_table(
        'ai_trading_signals',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('signal_timestamp', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('symbol', sa.String(20), nullable=False, index=True),
        sa.Column('action', sa.String(10), nullable=False),
        sa.Column('confidence_score', sa.Numeric(5, 2), nullable=False),
        sa.Column('strategy_id', sa.String(100), nullable=False, index=True),
        sa.Column('regime_type', sa.String(50), nullable=False),
        sa.Column('contributing_events', postgresql.JSONB(), nullable=True),
        sa.Column('ml_predictions', postgresql.JSONB(), nullable=True),
        sa.Column('technical_indicators', postgresql.JSONB(), nullable=True),
        sa.Column('reasoning', sa.Text(), nullable=True),
        sa.Column('metadata', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('idx_ai_trading_signals_symbol', 'ai_trading_signals', ['symbol'])
    op.create_index('idx_ai_trading_signals_action', 'ai_trading_signals', ['action'])
    
    # ai_kill_switches
    op.create_table(
        'ai_kill_switches',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('switch_type', sa.String(20), nullable=False),
        sa.Column('target_id', sa.String(100), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='inactive'),
        sa.Column('activated_by', sa.String(100), nullable=True),
        sa.Column('activation_reason', sa.Text(), nullable=True),
        sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deactivated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expiration_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )
    op.create_index('idx_ai_kill_switches_type_status', 'ai_kill_switches', ['switch_type', 'status'])
    
    # ai_safety_triggers
    op.create_table(
        'ai_safety_triggers',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('trigger_type', sa.String(50), nullable=False),
        sa.Column('trigger_condition', sa.Text(), nullable=False),
        sa.Column('threshold_value', sa.Numeric(10, 2), nullable=False),
        sa.Column('actual_value', sa.Numeric(10, 2), nullable=False),
        sa.Column('action_taken', sa.String(50), nullable=False),
        sa.Column('triggered_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False, index=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('metadata', postgresql.JSONB(), nullable=True),
    )
    op.create_index('idx_ai_safety_triggers_type', 'ai_safety_triggers', ['trigger_type'])

    # TimescaleDB hypertables + policies — skipped gracefully on plain Postgres
    op.execute("""
        DO $$ BEGIN
            CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
            PERFORM create_hypertable('ai_raw_events', 'event_timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
            PERFORM create_hypertable('ai_processed_events', 'processed_timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
            PERFORM create_hypertable('ai_regime_detections', 'detection_timestamp', chunk_time_interval => INTERVAL '7 days', if_not_exists => TRUE);
            PERFORM create_hypertable('ai_trading_signals', 'signal_timestamp', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
            ALTER TABLE ai_raw_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'source_type', timescaledb.compress_orderby = 'event_timestamp DESC');
            PERFORM add_compression_policy('ai_raw_events', INTERVAL '7 days', if_not_exists => TRUE);
            ALTER TABLE ai_processed_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'processing_status', timescaledb.compress_orderby = 'processed_timestamp DESC');
            PERFORM add_compression_policy('ai_processed_events', INTERVAL '7 days', if_not_exists => TRUE);
            ALTER TABLE ai_regime_detections SET (timescaledb.compress, timescaledb.compress_segmentby = 'regime_type', timescaledb.compress_orderby = 'detection_timestamp DESC');
            PERFORM add_compression_policy('ai_regime_detections', INTERVAL '30 days', if_not_exists => TRUE);
            ALTER TABLE ai_trading_signals SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol, strategy_id', timescaledb.compress_orderby = 'signal_timestamp DESC');
            PERFORM add_compression_policy('ai_trading_signals', INTERVAL '30 days', if_not_exists => TRUE);
            PERFORM add_retention_policy('ai_raw_events', INTERVAL '90 days', if_not_exists => TRUE);
            PERFORM add_retention_policy('ai_processed_events', INTERVAL '90 days', if_not_exists => TRUE);
            PERFORM add_retention_policy('ai_trading_signals', INTERVAL '365 days', if_not_exists => TRUE);
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'TimescaleDB not available, skipping hypertable setup: %', SQLERRM;
        END $$;
    """)


def downgrade() -> None:
    """Drop all AI tables in reverse order."""
    op.drop_table('ai_safety_triggers')
    op.drop_table('ai_kill_switches')
    op.drop_table('ai_trading_signals')
    op.drop_table('ai_active_strategies')
    op.drop_table('ai_regime_detections')
    op.drop_table('ai_drift_reports')
    op.drop_table('ai_ml_models')
    op.drop_table('ai_fake_news_flags')
    op.drop_table('ai_source_credibility')
    op.drop_table('ai_event_classifications')
    op.drop_table('ai_nlp_results')
    op.drop_table('ai_processed_events')
    op.drop_table('ai_raw_events')
