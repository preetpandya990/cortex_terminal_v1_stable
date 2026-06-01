"""create_unified_model_registry

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-10 20:15:00

Create unified model registry linking ML and AI model tables.
Requirements: 18.4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create unified model registry table."""
    
    op.create_table(
        'unified_model_registry',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('model_id', sa.String(255), nullable=False, unique=True, index=True),
        sa.Column('model_name', sa.String(255), nullable=False),
        sa.Column('model_type', sa.String(50), nullable=False),  # ml_prediction, ai_classification, ai_sentiment, ai_regime
        sa.Column('framework', sa.String(50), nullable=False),  # onnx, pytorch, sklearn, ollama
        sa.Column('version', sa.String(50), nullable=False),
        sa.Column('deployment_state', sa.String(20), nullable=False, server_default='shadow'),  # shadow, paper, live
        sa.Column('ml_model_metadata_id', sa.Integer(), sa.ForeignKey('ml_model_metadata.id', ondelete='SET NULL'), nullable=True),
        sa.Column('ai_ml_model_id', sa.BigInteger(), sa.ForeignKey('ai_ml_models.id', ondelete='SET NULL'), nullable=True),
        sa.Column('model_path', sa.String(500), nullable=True),
        sa.Column('onnx_path', sa.String(500), nullable=True),
        sa.Column('config', postgresql.JSONB(), nullable=True),
        sa.Column('metrics', postgresql.JSONB(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('deployed_at', sa.DateTime(timezone=True), nullable=True),
    )
    
    op.create_index('idx_unified_model_registry_type', 'unified_model_registry', ['model_type'])
    op.create_index('idx_unified_model_registry_state', 'unified_model_registry', ['deployment_state'])
    op.create_index('idx_unified_model_registry_active', 'unified_model_registry', ['is_active'])


def downgrade() -> None:
    """Drop unified model registry table."""
    op.drop_table('unified_model_registry')
