"""ML system tables

Revision ID: 0002
Revises: 0001
Create Date: 2024-01-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0002'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade():
    # Create ml_drift_metrics table
    op.create_table(
        'ml_drift_metrics',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('model_id', sa.String(length=255), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('drift_detected', sa.Boolean(), nullable=False),
        sa.Column('drift_type', sa.String(length=50), nullable=True),
        sa.Column('feature_drift_scores', sa.JSON(), nullable=True),
        sa.Column('drifted_features', sa.JSON(), nullable=True),
        sa.Column('prediction_drift_score', sa.Float(), nullable=True),
        sa.Column('prediction_z_score', sa.Float(), nullable=True),
        sa.Column('prediction_ks_statistic', sa.Float(), nullable=True),
        sa.Column('prediction_p_value', sa.Float(), nullable=True),
        sa.Column('current_mean', sa.Float(), nullable=True),
        sa.Column('current_std', sa.Float(), nullable=True),
        sa.Column('historical_mean', sa.Float(), nullable=True),
        sa.Column('historical_std', sa.Float(), nullable=True),
        sa.Column('alerts', sa.JSON(), nullable=True),
        sa.Column('alert_sent', sa.Boolean(), nullable=True),
        sa.Column('alert_sent_at', sa.DateTime(), nullable=True),
        sa.Column('sample_count', sa.Integer(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ml_drift_metrics_model_id', 'ml_drift_metrics', ['model_id'])
    op.create_index('ix_ml_drift_metrics_timestamp', 'ml_drift_metrics', ['timestamp'])


    # Create ml_predictions table
    op.create_table(
        'ml_predictions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('model_id', sa.String(length=255), nullable=False),
        sa.Column('timestamp', sa.DateTime(), nullable=False),
        sa.Column('features', sa.JSON(), nullable=True),
        sa.Column('prediction', sa.Float(), nullable=False),
        sa.Column('prediction_proba', sa.JSON(), nullable=True),
        sa.Column('symbol', sa.String(length=50), nullable=True),
        sa.Column('prediction_type', sa.String(length=50), nullable=True),
        sa.Column('model_version', sa.String(length=50), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_ml_predictions_model_id', 'ml_predictions', ['model_id'])
    op.create_index('ix_ml_predictions_timestamp', 'ml_predictions', ['timestamp'])
    op.create_index('ix_ml_predictions_symbol', 'ml_predictions', ['symbol'])
    
    # Create ml_model_metadata table
    op.create_table(
        'ml_model_metadata',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('model_id', sa.String(length=255), nullable=False),
        sa.Column('model_name', sa.String(length=255), nullable=False),
        sa.Column('model_version', sa.String(length=50), nullable=False),
        sa.Column('trained_at', sa.DateTime(), nullable=False),
        sa.Column('training_samples', sa.Integer(), nullable=True),
        sa.Column('training_features', sa.JSON(), nullable=True),
        sa.Column('training_feature_stats', sa.JSON(), nullable=True),
        sa.Column('training_prediction_stats', sa.JSON(), nullable=True),
        sa.Column('training_metrics', sa.JSON(), nullable=True),
        sa.Column('validation_metrics', sa.JSON(), nullable=True),
        sa.Column('model_path', sa.String(length=500), nullable=True),
        sa.Column('onnx_path', sa.String(length=500), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('deployed_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('model_id')
    )
    op.create_index('ix_ml_model_metadata_model_id', 'ml_model_metadata', ['model_id'])


def downgrade():
    op.drop_index('ix_ml_model_metadata_model_id', table_name='ml_model_metadata')
    op.drop_table('ml_model_metadata')
    
    op.drop_index('ix_ml_predictions_symbol', table_name='ml_predictions')
    op.drop_index('ix_ml_predictions_timestamp', table_name='ml_predictions')
    op.drop_index('ix_ml_predictions_model_id', table_name='ml_predictions')
    op.drop_table('ml_predictions')
    
    op.drop_index('ix_ml_drift_metrics_timestamp', table_name='ml_drift_metrics')
    op.drop_index('ix_ml_drift_metrics_model_id', table_name='ml_drift_metrics')
    op.drop_table('ml_drift_metrics')
