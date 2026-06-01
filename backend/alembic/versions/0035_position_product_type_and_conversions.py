"""Add product_type to paper_positions and create paper_position_conversions

Revision ID: 0035
Revises: 0034
Create Date: 2026-05-18

Two schema changes required by the CNC↔MIS position conversion feature:

  1. paper_positions.product_type (VARCHAR 10, NOT NULL, default 'CNC')
     The ORM model defined this column from day one; it was missing from the
     original 0014 migration. All existing rows are backfilled to 'CNC' (safe:
     all positions were opened without an explicit product type, so CNC delivery
     is the correct default for NSE equity).

  2. paper_position_conversions (new table)
     Append-only audit trail of every CNC↔MIS conversion. One row per successful
     conversion; never updated or deleted. Mirrors the ORM model defined in
     app/models/paper_trading.py:PaperPositionConversion.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0035'
down_revision: Union[str, None] = '0034'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Add product_type to paper_positions ────────────────────────────────
    op.add_column(
        'paper_positions',
        sa.Column(
            'product_type',
            sa.String(10),
            nullable=False,
            server_default='CNC',
        ),
    )
    op.create_check_constraint(
        'ck_paper_positions_product_type',
        'paper_positions',
        "product_type IN ('CNC', 'MIS', 'NRML')",
    )

    # ── 2. Create paper_position_conversions ──────────────────────────────────
    op.create_table(
        'paper_position_conversions',
        sa.Column(
            'id',
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text('gen_random_uuid()'),
        ),
        sa.Column(
            'position_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('paper_positions.id', ondelete='CASCADE'),
            nullable=False,
            index=True,
        ),
        sa.Column(
            'portfolio_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('portfolios.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('from_product', sa.String(10), nullable=False),
        sa.Column('to_product',   sa.String(10), nullable=False),
        sa.Column('quantity',     sa.Integer,    nullable=False),
        sa.Column('note',         sa.String(255)),
        sa.Column(
            'converted_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "from_product IN ('CNC', 'MIS') AND to_product IN ('CNC', 'MIS')",
            name='ck_position_conversions_products',
        ),
        sa.CheckConstraint(
            'from_product <> to_product',
            name='ck_position_conversions_distinct',
        ),
        sa.CheckConstraint(
            'quantity > 0',
            name='ck_position_conversions_quantity_positive',
        ),
    )


def downgrade() -> None:
    op.drop_table('paper_position_conversions')
    op.drop_constraint('ck_paper_positions_product_type', 'paper_positions')
    op.drop_column('paper_positions', 'product_type')
