"""Add user tracking to ML predictions

Revision ID: 0003
Revises: 0002
Create Date: 2024-04-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    # Add user_id column to ml_predictions table
    op.add_column('ml_predictions', sa.Column('user_id', sa.String(length=255), nullable=True))
    op.create_index('ix_ml_predictions_user_id', 'ml_predictions', ['user_id'])
    
    # Create ml_audit_logs table for authentication and security events
    op.create_table(
        'ml_audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('user_id', sa.String(length=255), nullable=True),
        sa.Column('endpoint', sa.String(length=255), nullable=True),
        sa.Column('ip_address', sa.String(length=50), nullable=True),
        sa.Column('user_agent', sa.String(length=500), nullable=True),
        sa.Column('request_data', sa.JSON(), nullable=True),
        sa.Column('response_status', sa.Integer(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('prediction_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ml_audit_logs_timestamp', 'ml_audit_logs', ['timestamp'])
    op.create_index('ix_ml_audit_logs_user_id', 'ml_audit_logs', ['user_id'])
    op.create_index('ix_ml_audit_logs_event_type', 'ml_audit_logs', ['event_type'])


def downgrade():
    op.drop_index('ix_ml_audit_logs_event_type', table_name='ml_audit_logs')
    op.drop_index('ix_ml_audit_logs_user_id', table_name='ml_audit_logs')
    op.drop_index('ix_ml_audit_logs_timestamp', table_name='ml_audit_logs')
    op.drop_table('ml_audit_logs')
    
    op.drop_index('ix_ml_predictions_user_id', table_name='ml_predictions')
    op.drop_column('ml_predictions', 'user_id')
