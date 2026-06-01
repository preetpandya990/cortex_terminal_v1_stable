"""Create ml_prediction_outcomes table for ML governance and accuracy tracking.

Revision ID: 0023
Revises: 0022
Create Date: 2026-05-11

This table tracks ALL ML predictions (pattern detection, sentiment, ensemble)
for comprehensive governance, accuracy measurement, and continuous monitoring.

Key features:
- Unified tracking for all prediction types
- Links to paper trading outcomes (if traded)
- Measures prediction accuracy (for ALL predictions, not just traded)
- Supports historical accuracy analysis
- Enables ML model performance monitoring
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade():
    # Create ml_prediction_outcomes table
    op.create_table(
        'ml_prediction_outcomes',
        
        # Primary Key & Instrument
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False, server_default=sa.text('gen_random_uuid()')),
        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('instrument_key', sa.String(100), nullable=False),
        sa.Column('predicted_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        
        # ML Prediction Data
        sa.Column('model_version', sa.String(50), nullable=False),
        sa.Column('prediction_type', sa.String(50), nullable=False),  # 'PATTERN', 'SENTIMENT', 'ENSEMBLE'
        sa.Column('pattern_name', sa.String(50)),
        sa.Column('pattern_timeframe', sa.String(10)),
        sa.Column('signal_direction', sa.String(4), nullable=False),  # 'BUY', 'SELL'
        sa.Column('confidence_score', sa.Numeric(5, 4), nullable=False),
        sa.Column('confidence_level', sa.String(10)),  # 'HIGH', 'MEDIUM', 'LOW'
        
        # Price Targets
        sa.Column('predicted_entry_price', sa.Numeric(12, 4), nullable=False),
        sa.Column('predicted_stop_loss', sa.Numeric(12, 4)),
        sa.Column('predicted_tp1', sa.Numeric(12, 4)),
        sa.Column('predicted_tp2', sa.Numeric(12, 4)),
        sa.Column('predicted_tp3', sa.Numeric(12, 4)),
        
        # Execution Data (NULL if not traded)
        sa.Column('was_traded', sa.Boolean(), nullable=False, server_default=sa.text('FALSE')),
        sa.Column('portfolio_id', postgresql.UUID(as_uuid=True)),
        sa.Column('position_id', postgresql.UUID(as_uuid=True)),
        sa.Column('actual_entry_price', sa.Numeric(12, 4)),
        sa.Column('actual_exit_price', sa.Numeric(12, 4)),
        sa.Column('gross_pnl', sa.Numeric(14, 4)),
        sa.Column('net_pnl', sa.Numeric(14, 4)),
        
        # Outcome Measurement (for ALL predictions)
        sa.Column('outcome_status', sa.String(20), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column('ml_direction_correct', sa.Boolean()),
        sa.Column('hit_predicted_tp1', sa.Boolean(), server_default=sa.text('FALSE')),
        sa.Column('hit_predicted_tp2', sa.Boolean(), server_default=sa.text('FALSE')),
        sa.Column('hit_predicted_tp3', sa.Boolean(), server_default=sa.text('FALSE')),
        sa.Column('hit_predicted_sl', sa.Boolean(), server_default=sa.text('FALSE')),
        sa.Column('max_favorable_move_pct', sa.Numeric(8, 4)),
        sa.Column('max_adverse_move_pct', sa.Numeric(8, 4)),
        sa.Column('final_move_pct', sa.Numeric(8, 4)),
        sa.Column('measurement_window_days', sa.Integer(), server_default=sa.text('5')),
        sa.Column('measured_at', sa.TIMESTAMP(timezone=True)),
        
        # Metadata
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        
        # Primary Key
        sa.PrimaryKeyConstraint('id'),
        
        # Foreign Keys (optional - paper trading tables may not exist yet)
        # sa.ForeignKeyConstraint(['portfolio_id'], ['portfolios.id'], ondelete='SET NULL'),
        # sa.ForeignKeyConstraint(['position_id'], ['paper_positions.id'], ondelete='SET NULL'),
    )
    
    # Create indexes for query performance
    op.create_index(
        'idx_ml_pred_outcomes_symbol_predicted',
        'ml_prediction_outcomes',
        ['symbol', sa.text('predicted_at DESC')],
        postgresql_using='btree'
    )
    
    op.create_index(
        'idx_ml_pred_outcomes_status',
        'ml_prediction_outcomes',
        ['outcome_status', sa.text('predicted_at DESC')],
        postgresql_using='btree'
    )
    
    op.create_index(
        'idx_ml_pred_outcomes_pattern',
        'ml_prediction_outcomes',
        ['pattern_name', 'pattern_timeframe'],
        postgresql_using='btree'
    )
    
    op.create_index(
        'idx_ml_pred_outcomes_confidence',
        'ml_prediction_outcomes',
        ['confidence_level', 'ml_direction_correct'],
        postgresql_using='btree'
    )
    
    op.create_index(
        'idx_ml_pred_outcomes_predicted_at',
        'ml_prediction_outcomes',
        ['predicted_at'],
        postgresql_using='btree'
    )
    
    # Note: TimescaleDB hypertable conversion (optional, for time-series optimization):
    # SELECT create_hypertable('ml_prediction_outcomes', 'predicted_at', if_not_exists => TRUE);


def downgrade():
    # Drop table
    op.drop_table('ml_prediction_outcomes')
