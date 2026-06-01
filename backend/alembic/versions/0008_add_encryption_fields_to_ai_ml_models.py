"""add_encryption_fields_to_ai_ml_models

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-10 22:17:00

Add encryption fields (artifact_encrypted, artifact_sha256, timeframe) to ai_ml_models table.
This supports the UnifiedModelRegistry with encrypted model storage.

Requirements: 13.9, 13.10, 18.4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add encryption and timeframe fields to ai_ml_models."""
    
    # Add new columns
    op.add_column('ai_ml_models', sa.Column('timeframe', sa.String(10), nullable=True))
    op.add_column('ai_ml_models', sa.Column('artifact_encrypted', sa.LargeBinary(), nullable=True))
    op.add_column('ai_ml_models', sa.Column('artifact_sha256', sa.String(64), nullable=True))
    
    # Create indexes
    op.create_index('idx_ai_ml_models_timeframe', 'ai_ml_models', ['timeframe'])
    op.create_index('idx_ai_ml_models_state_timeframe', 'ai_ml_models', ['deployment_state', 'timeframe'])


def downgrade() -> None:
    """Remove encryption and timeframe fields from ai_ml_models."""
    
    # Drop indexes
    op.drop_index('idx_ai_ml_models_state_timeframe', table_name='ai_ml_models')
    op.drop_index('idx_ai_ml_models_timeframe', table_name='ai_ml_models')
    
    # Drop columns
    op.drop_column('ai_ml_models', 'artifact_sha256')
    op.drop_column('ai_ml_models', 'artifact_encrypted')
    op.drop_column('ai_ml_models', 'timeframe')
