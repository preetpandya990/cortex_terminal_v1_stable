"""Initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-02 00:00:00.000000

This is the baseline migration generated from the current ORM models.
Run: alembic upgrade head
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── upstox_ohlcv ──────────────────────────────────────────────────────────
    op.create_table(
        "upstox_ohlcv",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_key", sa.String(100), nullable=False),
        sa.Column("timeframe", sa.String(10), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(18, 4), nullable=False),
        sa.Column("high", sa.Numeric(18, 4), nullable=False),
        sa.Column("low", sa.Numeric(18, 4), nullable=False),
        sa.Column("close", sa.Numeric(18, 4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=False),
        sa.Column("oi", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_key", "timeframe", "timestamp", name="uq_upstox_ohlcv"),
    )
    op.create_index("idx_upstox_instrument_timeframe_ts", "upstox_ohlcv", ["instrument_key", "timeframe", "timestamp"])
    op.create_index(op.f("ix_upstox_ohlcv_instrument_key"), "upstox_ohlcv", ["instrument_key"])

    # ── upstox_ticks ──────────────────────────────────────────────────────────
    op.create_table(
        "upstox_ticks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_key", sa.String(100), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_price", sa.Numeric(18, 4), nullable=False),
        sa.Column("volume", sa.BigInteger(), nullable=True),
        sa.Column("oi", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_upstox_tick_instrument_ts", "upstox_ticks", ["instrument_key", "timestamp"])

    # ── instrument_master ─────────────────────────────────────────────────────
    op.create_table(
        "instrument_master",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instrument_key", sa.String(200), nullable=False),
        sa.Column("trading_symbol", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("exchange", sa.String(20), nullable=False),
        sa.Column("instrument_type", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instrument_key"),
    )
    op.create_index(op.f("ix_instrument_master_instrument_key"), "instrument_master", ["instrument_key"], unique=True)
    op.create_index("idx_instrument_symbol", "instrument_master", ["trading_symbol"])
    op.create_index("idx_instrument_exchange", "instrument_master", ["exchange"])


def downgrade() -> None:
    op.drop_table("instrument_master")
    op.drop_table("upstox_ticks")
    op.drop_table("upstox_ohlcv")