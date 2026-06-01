"""Add time_horizon, expires_at, target_price, stop_loss to ai_trading_signals

Revision ID: 0013
Revises: 0012
Create Date: 2026-04-28 00:00:00.000000

Adds four columns required by the Trading Signals revamp:
  - time_horizon  : intraday / swing / positional (derived from prediction timeframe)
  - expires_at    : computed signal TTL anchored to NSE market hours
  - target_price  : TP1 from ML ensemble (entry_price), nullable
  - stop_loss     : ATR-based stop from ML ensemble, nullable

All columns are nullable for backward compatibility with existing rows.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'ai_trading_signals',
        sa.Column('time_horizon', sa.String(length=20), nullable=True),
    )
    op.add_column(
        'ai_trading_signals',
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'ai_trading_signals',
        sa.Column('target_price', sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.add_column(
        'ai_trading_signals',
        sa.Column('stop_loss', sa.Numeric(precision=12, scale=2), nullable=True),
    )

    op.create_index(
        'idx_ai_signals_time_horizon',
        'ai_trading_signals',
        ['time_horizon'],
    )
    op.create_index(
        'idx_ai_signals_expires_at',
        'ai_trading_signals',
        ['expires_at'],
    )


def downgrade() -> None:
    op.drop_index('idx_ai_signals_expires_at', table_name='ai_trading_signals')
    op.drop_index('idx_ai_signals_time_horizon', table_name='ai_trading_signals')

    op.drop_column('ai_trading_signals', 'stop_loss')
    op.drop_column('ai_trading_signals', 'target_price')
    op.drop_column('ai_trading_signals', 'expires_at')
    op.drop_column('ai_trading_signals', 'time_horizon')
