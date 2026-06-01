"""Add regime_type/time_horizon to trade_suggestions; create user_suggestion_compliance table.

Revision ID: 0024
Revises: 0023
Create Date: 2026-05-12

Strategy compliance for trade suggestions — junction-table approach.

Changes:
  1. trade_suggestions: add regime_type VARCHAR(50), time_horizon VARCHAR(20)
     These are populated at suggestion-generation time by the correlation engine
     and feed the strategy filter pipeline's regime_gate / session_gate.

  2. user_suggestion_compliance: new table linking suggestions to per-user
     strategy evaluation results.  Written by the correlation engine immediately
     after each suggestion is committed; read by the trade-suggestions API to:
       • hard_gate users  — suppress non-passing suggestions
       • soft_filter users — tag matching strategies on suggestion cards

Indexes:
  • uq_usc_suggestion_user_strategy  — unique per (suggestion, user, strategy);
    allows safe ON CONFLICT DO NOTHING upsert from the worker.
  • idx_usc_user_suggestion           — fast lookup for API JOIN.
  • idx_usc_user_passed               — partial index for hard_gate filtering.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade():
    # ── 1. trade_suggestions: add regime_type, time_horizon ──────────────────
    op.add_column(
        'trade_suggestions',
        sa.Column('regime_type', sa.String(50), nullable=True),
    )
    op.add_column(
        'trade_suggestions',
        sa.Column('time_horizon', sa.String(20), nullable=True),
    )

    # ── 2. user_suggestion_compliance ─────────────────────────────────────────
    op.create_table(
        'user_suggestion_compliance',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            'suggestion_id',
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column(
            'strategy_id',
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column('passed', sa.Boolean(), nullable=False),
        # Full gate audit trail from PipelineResult.to_dict()
        sa.Column('pipeline_result', postgresql.JSONB(), nullable=True),
        sa.Column(
            'evaluated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('NOW()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id', name='pk_user_suggestion_compliance'),
        sa.ForeignKeyConstraint(
            ['suggestion_id'],
            ['trade_suggestions.suggestion_id'],
            ondelete='CASCADE',
            name='fk_usc_suggestion_id',
        ),
        sa.ForeignKeyConstraint(
            ['user_id'],
            ['users.id'],
            ondelete='CASCADE',
            name='fk_usc_user_id',
        ),
        sa.ForeignKeyConstraint(
            ['strategy_id'],
            ['strategies.id'],
            ondelete='CASCADE',
            name='fk_usc_strategy_id',
        ),
    )

    # Unique constraint — safe ON CONFLICT DO NOTHING upsert from worker
    op.create_index(
        'uq_usc_suggestion_user_strategy',
        'user_suggestion_compliance',
        ['suggestion_id', 'user_id', 'strategy_id'],
        unique=True,
    )

    # Fast lookup for API: "all compliance rows for user X on suggestion set S"
    op.create_index(
        'idx_usc_user_suggestion',
        'user_suggestion_compliance',
        ['user_id', 'suggestion_id'],
    )

    # Partial index for hard_gate filtering — only index passing rows
    op.create_index(
        'idx_usc_user_passed',
        'user_suggestion_compliance',
        ['user_id', 'suggestion_id'],
        postgresql_where=sa.text('passed = TRUE'),
    )


def downgrade():
    op.drop_index('idx_usc_user_passed', table_name='user_suggestion_compliance')
    op.drop_index('idx_usc_user_suggestion', table_name='user_suggestion_compliance')
    op.drop_index('uq_usc_suggestion_user_strategy', table_name='user_suggestion_compliance')
    op.drop_table('user_suggestion_compliance')

    op.drop_column('trade_suggestions', 'time_horizon')
    op.drop_column('trade_suggestions', 'regime_type')
