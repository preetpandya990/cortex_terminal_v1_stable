"""Add encryption and lifecycle fields to ml_model_metadata

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-09 12:58:00

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add encryption and security fields
    op.add_column('ml_model_metadata', sa.Column('checksum', sa.String(64), nullable=True))
    op.add_column('ml_model_metadata', sa.Column('encrypted', sa.Boolean(), server_default='true', nullable=False))
    
    # Add lifecycle fields
    op.add_column('ml_model_metadata', sa.Column('status', sa.String(20), server_default='development', nullable=False))
    op.add_column('ml_model_metadata', sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False))
    op.add_column('ml_model_metadata', sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now(), nullable=False))
    
    # Create index on status for faster queries
    op.create_index('ix_ml_model_metadata_status', 'ml_model_metadata', ['status'])


def downgrade() -> None:
    op.drop_index('ix_ml_model_metadata_status', 'ml_model_metadata')
    op.drop_column('ml_model_metadata', 'updated_at')
    op.drop_column('ml_model_metadata', 'created_at')
    op.drop_column('ml_model_metadata', 'status')
    op.drop_column('ml_model_metadata', 'encrypted')
    op.drop_column('ml_model_metadata', 'checksum')
