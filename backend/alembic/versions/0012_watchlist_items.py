"""Add watchlist_items table

Revision ID: 0012
Revises: 0011
Create Date: 2026-04-23 19:37:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, None] = '0011_trade_suggestions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create watchlist_items table
    op.create_table(
        'watchlist_items',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('instrument_key', sa.String(length=100), nullable=False),
        sa.Column('trading_symbol', sa.String(length=50), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=True),
        sa.Column('exchange', sa.String(length=20), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'instrument_key', name='uq_user_instrument'),
        sa.UniqueConstraint('user_id', 'position', name='uq_user_position')
    )
    
    # Create indexes
    op.create_index('ix_watchlist_items_user_id', 'watchlist_items', ['user_id'])
    op.create_index('ix_watchlist_items_instrument_key', 'watchlist_items', ['instrument_key'])


def downgrade() -> None:
    # Drop indexes
    op.drop_index('ix_watchlist_items_instrument_key', table_name='watchlist_items')
    op.drop_index('ix_watchlist_items_user_id', table_name='watchlist_items')
    
    # Drop table
    op.drop_table('watchlist_items')
