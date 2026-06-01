"""create ml_features table

Revision ID: 0010_ml_features
Revises: 0009
Create Date: 2026-04-12 00:06:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '0010_ml_features'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade():
    # Create ml_features table
    op.create_table(
        'ml_features',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('timestamp', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('feature_vector', postgresql.JSONB(), nullable=False),
        sa.Column('feature_version', sa.String(10), nullable=False, server_default='v1.0'),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('NOW()')),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('symbol', 'timestamp', 'feature_version', name='uq_ml_features_symbol_timestamp_version')
    )
    
    # Create indexes
    op.create_index('idx_ml_features_symbol_timestamp', 'ml_features', ['symbol', 'timestamp'], postgresql_using='btree')
    op.create_index('idx_ml_features_timestamp', 'ml_features', ['timestamp'], postgresql_using='btree')
    
    # Note: TimescaleDB hypertable conversion should be done manually if needed:
    # SELECT create_hypertable('ml_features', 'timestamp', if_not_exists => TRUE);
    # This keeps the migration portable across PostgreSQL and TimescaleDB installations.


def downgrade():
    # Drop table
    op.drop_table('ml_features')
