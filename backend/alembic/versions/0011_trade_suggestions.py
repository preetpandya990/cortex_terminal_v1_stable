"""create trade_suggestions and event_correlations tables

Revision ID: 0011_trade_suggestions
Revises: 0010_ml_features
Create Date: 2026-04-21 23:46:00.000000

PERFORMANCE NOTE: Standard PostgreSQL (TimescaleDB not enabled)
================================================================
This migration creates tables optimized for standard PostgreSQL.

For production-grade time-series performance, enable TimescaleDB:
  1. CREATE EXTENSION timescaledb;
  2. SELECT create_hypertable('trade_suggestions', 'generated_at');
  3. SELECT create_hypertable('event_correlations', 'trigger_timestamp');
  4. SELECT add_retention_policy('trade_suggestions', INTERVAL '7 days');
  5. SELECT add_retention_policy('event_correlations', INTERVAL '30 days');

Without TimescaleDB:
- Implement application-level data cleanup (7-day retention for suggestions)
- Monitor table sizes (expect ~1GB per million suggestions)
- Consider manual partitioning if table exceeds 10M rows

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0011_trade_suggestions'
down_revision: Union[str, None] = '0010_ml_features'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Trade Suggestions Table ────────────────────────────────────────────────
    op.create_table(
        'trade_suggestions',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('suggestion_id', postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text('gen_random_uuid()')),
        
        # Instrument identification
        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('instrument_key', sa.String(100), nullable=False),
        sa.Column('trading_symbol', sa.String(50), nullable=True),
        
        # Consensus metadata
        sa.Column('consensus_score', sa.Numeric(5, 2), nullable=False),
        sa.Column('confidence_level', sa.String(20), nullable=False),
        sa.Column('signal_direction', sa.String(10), nullable=False),
        
        # Pathway tracking
        sa.Column('trigger_pathway', sa.String(20), nullable=False),
        
        # Contributing signals (JSONB for flexibility)
        sa.Column('scanner_signal', postgresql.JSONB(), nullable=False),
        sa.Column('ai_signal', postgresql.JSONB(), nullable=False),
        sa.Column('ml_signal', postgresql.JSONB(), nullable=False),
        
        # Trade parameters
        sa.Column('entry_price', sa.Numeric(12, 2), nullable=True),
        sa.Column('stop_loss', sa.Numeric(12, 2), nullable=True),
        sa.Column('risk_reward_ratio', sa.Numeric(5, 2), nullable=True),
        sa.Column('take_profit_1', sa.Numeric(12, 2), nullable=True),
        sa.Column('take_profit_2', sa.Numeric(12, 2), nullable=True),
        sa.Column('take_profit_3', sa.Numeric(12, 2), nullable=True),
        
        # Temporal metadata
        sa.Column('generated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('expires_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='active'),
        
        # Audit trail
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('suggestion_id', name='uq_trade_suggestions_suggestion_id'),
        sa.CheckConstraint('consensus_score >= 0 AND consensus_score <= 100', name='ck_trade_suggestions_consensus_score'),
        sa.CheckConstraint("confidence_level IN ('HIGH', 'MEDIUM', 'LOW')", name='ck_trade_suggestions_confidence_level'),
        sa.CheckConstraint("signal_direction IN ('BUY', 'SELL')", name='ck_trade_suggestions_signal_direction'),
        sa.CheckConstraint("trigger_pathway IN ('TECHNICAL_FIRST', 'FUNDAMENTAL_FIRST')", name='ck_trade_suggestions_trigger_pathway'),
        sa.CheckConstraint("status IN ('active', 'expired', 'executed', 'invalidated')", name='ck_trade_suggestions_status'),
    )
    
    # Indexes for high-performance queries
    op.create_index('idx_suggestions_symbol_status', 'trade_suggestions', ['symbol', 'status'], 
                    postgresql_where=sa.text("status = 'active'"))
    op.create_index('idx_suggestions_generated_at_desc', 'trade_suggestions', [sa.text('generated_at DESC')])
    op.create_index('idx_suggestions_consensus_score', 'trade_suggestions', [sa.text('consensus_score DESC')], 
                    postgresql_where=sa.text("status = 'active'"))
    op.create_index('idx_suggestions_confidence_level', 'trade_suggestions', ['confidence_level'], 
                    postgresql_where=sa.text("status = 'active'"))
    op.create_index('idx_suggestions_direction', 'trade_suggestions', ['signal_direction'], 
                    postgresql_where=sa.text("status = 'active'"))
    
    # Composite index for landing page query optimization (<10ms target)
    op.create_index('idx_suggestions_landing_page', 'trade_suggestions', 
                    ['status', sa.text('consensus_score DESC'), sa.text('generated_at DESC')],
                    postgresql_where=sa.text("status = 'active'"))
    
    # ── Event Correlations Table ───────────────────────────────────────────────
    op.create_table(
        'event_correlations',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('suggestion_id', postgresql.UUID(as_uuid=True), nullable=True),
        
        # Trigger metadata
        sa.Column('trigger_type', sa.String(20), nullable=False),
        sa.Column('trigger_timestamp', sa.TIMESTAMP(timezone=True), nullable=False),
        
        # Agent response times (for latency monitoring)
        sa.Column('scanner_response_ms', sa.Integer(), nullable=True),
        sa.Column('ai_response_ms', sa.Integer(), nullable=True),
        sa.Column('ml_response_ms', sa.Integer(), nullable=True),
        sa.Column('total_latency_ms', sa.Integer(), nullable=True),
        
        # Consensus decision
        sa.Column('consensus_reached', sa.Boolean(), nullable=False),
        sa.Column('rejection_reason', sa.String(200), nullable=True),
        
        # Raw agent outputs (for debugging)
        sa.Column('scanner_output', postgresql.JSONB(), nullable=True),
        sa.Column('ai_output', postgresql.JSONB(), nullable=True),
        sa.Column('ml_output', postgresql.JSONB(), nullable=True),
        
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('correlation_id', name='uq_event_correlations_correlation_id'),
        sa.ForeignKeyConstraint(['suggestion_id'], ['trade_suggestions.suggestion_id'], ondelete='SET NULL'),
        sa.CheckConstraint("trigger_type IN ('SCANNER_ANOMALY', 'NEWS_EVENT')", name='ck_event_correlations_trigger_type'),
    )
    
    # Indexes for monitoring and debugging
    op.create_index('idx_correlations_suggestion', 'event_correlations', ['suggestion_id'])
    op.create_index('idx_correlations_latency', 'event_correlations', ['total_latency_ms'], 
                    postgresql_where=sa.text('consensus_reached = TRUE'))
    op.create_index('idx_correlations_rejection', 'event_correlations', ['rejection_reason'], 
                    postgresql_where=sa.text('consensus_reached = FALSE'))
    op.create_index('idx_correlations_trigger_timestamp', 'event_correlations', [sa.text('trigger_timestamp DESC')])


def downgrade() -> None:
    # Drop tables (cascade will handle foreign keys)
    op.drop_table('event_correlations')
    op.drop_table('trade_suggestions')
