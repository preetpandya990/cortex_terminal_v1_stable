"""Add isin + asset_class columns to instrument_master.

Revision ID: 0055
Revises: 0054
Create Date: 2026-07-14

Context
-------
Cortex is scoped to single-company equities only, but NSE's ``EQ`` trading
series covers "fully paid equity shares, ETFs, units of REITs/InvITs, and
partly-paid equity shares" identically — the exchange itself does not
distinguish a real stock from a fund/trust unit by series code. A recent
trade suggestion was generated for MSCI360 (an ETF), confirming this is a
real gap, not a one-off.

Upstox's raw instrument file carries a first-class ``isin`` field that
``instrument_fetch._normalize_instrument`` previously discarded. ISIN prefix
is an authoritative SEBI/AMFI-governed signal (``INE`` = corporate
securities via NSDL/CDSL, ``INF`` = mutual-fund/ETF units via AMFI), so this
migration adds:

  instrument_master.isin          — captured verbatim from the sync source.
  instrument_master.asset_class   — classified once per row at sync time by
                                     app.services.instrument_classifier
                                     (STOCK / ETF_FUND / TRUST_UNIT /
                                     UNCLASSIFIED). Defaults to
                                     'UNCLASSIFIED' for existing rows —
                                     fail-closed until the next sync
                                     reclassifies them, never silently
                                     treated as tradeable.

A partial index on the (trading_symbol, is_active=True, asset_class='STOCK')
hot path backs the eligibility gate used by symbol_validator.py and
market_scanner.py, mirroring idx_instrument_active from migration 0045.

Downgrade
---------
Drop the index, then both columns.
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: Union[str, None] = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "instrument_master",
        sa.Column(
            "isin",
            sa.String(length=12),
            nullable=True,
            comment="ISIN as reported by the instrument sync source; NULL for pre-existing rows until the next sync.",
        ),
    )
    op.add_column(
        "instrument_master",
        sa.Column(
            "asset_class",
            sa.String(length=20),
            nullable=False,
            server_default="UNCLASSIFIED",
            comment="STOCK / ETF_FUND / TRUST_UNIT / UNCLASSIFIED — classified at sync time by app.services.instrument_classifier. Only STOCK is eligible for scanning/signal generation.",
        ),
    )

    op.create_index(
        "idx_instrument_stock_active",
        "instrument_master",
        ["trading_symbol"],
        postgresql_where=sa.text("is_active AND asset_class = 'STOCK'"),
    )


def downgrade() -> None:
    op.drop_index("idx_instrument_stock_active", table_name="instrument_master")
    op.drop_column("instrument_master", "asset_class")
    op.drop_column("instrument_master", "isin")
