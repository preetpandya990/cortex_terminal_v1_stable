"""Company Fundamentals data layer.

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-16

Introduces 8 new tables for Upstox v2 Company Fundamentals API data:

  company_fundamentals_profile   — sector context + market cap (upserted, one row per company)
  company_key_ratios             — P/E, P/B, ROE, ROCE, EV/EBITDA + sector benchmarks (upserted)
  company_income_statement       — revenue / op-profit / net-profit history (yearly + quarterly)
  company_balance_sheet          — total assets / liabilities / net worth history
  company_cash_flow              — operating / investing / financing CF history
  company_share_holdings         — quarterly promoter / FII / DII / MF / retail breakdown
  company_corporate_actions      — dividends, bonus, splits, rights (event log, DELETE+INSERT)
  company_competitors            — peer companies with sector market cap (DELETE+INSERT)

Design decisions:
  - instrument_key (e.g. "NSE_EQ|INE002A01018") is the primary business key throughout,
    consistent with the rest of the system. isin is a derived search convenience column.
  - All monetary values are in Indian Rupees (Crore) as returned by the Upstox API.
  - Percentage values are stored as raw decimal (8.94 for 8.94%), never as fraction.
  - Historical tables carry period_date DATE (last calendar day of the reported month)
    for point-in-time-correct ML feature joins.
  - Profile and key_ratios use ON CONFLICT DO UPDATE (one current snapshot per company).
  - Historical tables (income_statement, balance_sheet, cash_flow, share_holdings) use
    ON CONFLICT DO UPDATE keyed on (instrument_key, period_date, [time_period, statement_type]).
  - corporate_actions and competitors are refreshed via DELETE+INSERT on each API fetch.
  - net_worth on balance_sheet is computed at write time: total_asset - total_liability.
    The API has no equity/net-worth field; this avoids recomputing it at query time.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── UPGRADE ────────────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ── 1. company_fundamentals_profile ──────────────────────────────────────────
    op.create_table(
        "company_fundamentals_profile",
        sa.Column("instrument_key",      sa.String(200),    nullable=False),
        sa.Column("isin",                sa.String(20),     nullable=False),
        sa.Column("company_profile",     sa.Text(),         nullable=True),
        sa.Column("sector",              sa.String(100),    nullable=True),
        sa.Column("sector_mcap_inr",     sa.Numeric(20, 2), nullable=True),
        sa.Column("sector_mcap_usd",     sa.Numeric(20, 4), nullable=True),
        sa.Column("sector_mcap_usd_unit", sa.String(10),   nullable=True),
        sa.Column(
            "fetched_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("instrument_key", name="pk_cfp_instrument_key"),
    )
    op.create_index("idx_cfp_isin", "company_fundamentals_profile", ["isin"])

    # ── 2. company_key_ratios ─────────────────────────────────────────────────────
    op.create_table(
        "company_key_ratios",
        sa.Column("instrument_key",   sa.String(200),    nullable=False),
        sa.Column("isin",             sa.String(20),     nullable=False),
        sa.Column("pe",               sa.Numeric(10, 4), nullable=True),
        sa.Column("pb",               sa.Numeric(10, 4), nullable=True),
        sa.Column("roa",              sa.Numeric(10, 4), nullable=True),
        sa.Column("roe",              sa.Numeric(10, 4), nullable=True),
        sa.Column("roce",             sa.Numeric(10, 4), nullable=True),
        sa.Column("ev_ebitda",        sa.Numeric(10, 4), nullable=True),
        sa.Column("pe_sector",        sa.Numeric(10, 4), nullable=True),
        sa.Column("pb_sector",        sa.Numeric(10, 4), nullable=True),
        sa.Column("roa_sector",       sa.Numeric(10, 4), nullable=True),
        sa.Column("roe_sector",       sa.Numeric(10, 4), nullable=True),
        sa.Column("roce_sector",      sa.Numeric(10, 4), nullable=True),
        sa.Column("ev_ebitda_sector", sa.Numeric(10, 4), nullable=True),
        sa.Column(
            "fetched_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("instrument_key", name="pk_ckr_instrument_key"),
    )
    op.create_index("idx_ckr_isin", "company_key_ratios", ["isin"])

    # ── 3. company_income_statement ───────────────────────────────────────────────
    op.create_table(
        "company_income_statement",
        sa.Column("id",             sa.BigInteger(), nullable=False),
        sa.Column("instrument_key", sa.String(200),  nullable=False),
        sa.Column("isin",           sa.String(20),   nullable=False),
        sa.Column("period_label",   sa.String(20),   nullable=False),
        sa.Column("period_date",    sa.Date(),       nullable=False),
        sa.Column("time_period",    sa.String(20),   nullable=False),
        sa.Column("statement_type", sa.String(20),   nullable=False),
        sa.Column("revenue",               sa.Numeric(20, 2), nullable=True),
        sa.Column("revenue_change_pct",    sa.Numeric(8, 4),  nullable=True),
        sa.Column("op_profit",             sa.Numeric(20, 2), nullable=True),
        sa.Column("op_profit_change_pct",  sa.Numeric(8, 4),  nullable=True),
        sa.Column("net_profit",            sa.Numeric(20, 2), nullable=True),
        sa.Column("net_profit_change_pct", sa.Numeric(8, 4),  nullable=True),
        sa.Column(
            "fetched_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cis_id"),
        sa.UniqueConstraint(
            "instrument_key", "period_date", "time_period", "statement_type",
            name="uq_cis_instrument_period_type",
        ),
    )
    op.create_index(
        "idx_cis_instrument_period", "company_income_statement",
        ["instrument_key", "period_date"],
    )
    op.execute(
        "CREATE SEQUENCE IF NOT EXISTS company_income_statement_id_seq"
        " START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1"
    )
    op.execute(
        "ALTER TABLE company_income_statement"
        " ALTER COLUMN id SET DEFAULT nextval('company_income_statement_id_seq')"
    )

    # ── 4. company_balance_sheet ──────────────────────────────────────────────────
    op.create_table(
        "company_balance_sheet",
        sa.Column("id",              sa.BigInteger(), nullable=False),
        sa.Column("instrument_key",  sa.String(200),  nullable=False),
        sa.Column("isin",            sa.String(20),   nullable=False),
        sa.Column("period_label",    sa.String(20),   nullable=False),
        sa.Column("period_date",     sa.Date(),       nullable=False),
        sa.Column("statement_type",  sa.String(20),   nullable=False),
        sa.Column("total_asset",     sa.Numeric(20, 2), nullable=True),
        sa.Column("total_liability", sa.Numeric(20, 2), nullable=True),
        sa.Column("net_worth",       sa.Numeric(20, 2), nullable=True),
        sa.Column(
            "fetched_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cbs_id"),
        sa.UniqueConstraint(
            "instrument_key", "period_date", "statement_type",
            name="uq_cbs_instrument_period_type",
        ),
    )
    op.create_index(
        "idx_cbs_instrument_period", "company_balance_sheet",
        ["instrument_key", "period_date"],
    )
    op.execute(
        "CREATE SEQUENCE IF NOT EXISTS company_balance_sheet_id_seq"
        " START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1"
    )
    op.execute(
        "ALTER TABLE company_balance_sheet"
        " ALTER COLUMN id SET DEFAULT nextval('company_balance_sheet_id_seq')"
    )

    # ── 5. company_cash_flow ──────────────────────────────────────────────────────
    op.create_table(
        "company_cash_flow",
        sa.Column("id",             sa.BigInteger(), nullable=False),
        sa.Column("instrument_key", sa.String(200),  nullable=False),
        sa.Column("isin",           sa.String(20),   nullable=False),
        sa.Column("period_label",   sa.String(20),   nullable=False),
        sa.Column("period_date",    sa.Date(),       nullable=False),
        sa.Column("statement_type", sa.String(20),   nullable=False),
        sa.Column("operating_cf",            sa.Numeric(20, 2), nullable=True),
        sa.Column("investing_cf",            sa.Numeric(20, 2), nullable=True),
        sa.Column("financing_cf",            sa.Numeric(20, 2), nullable=True),
        sa.Column("operating_cf_change_pct", sa.Numeric(8, 4),  nullable=True),
        sa.Column("investing_cf_change_pct", sa.Numeric(8, 4),  nullable=True),
        sa.Column("financing_cf_change_pct", sa.Numeric(8, 4),  nullable=True),
        sa.Column(
            "fetched_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ccf_id"),
        sa.UniqueConstraint(
            "instrument_key", "period_date", "statement_type",
            name="uq_ccf_instrument_period_type",
        ),
    )
    op.create_index(
        "idx_ccf_instrument_period", "company_cash_flow",
        ["instrument_key", "period_date"],
    )
    op.execute(
        "CREATE SEQUENCE IF NOT EXISTS company_cash_flow_id_seq"
        " START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1"
    )
    op.execute(
        "ALTER TABLE company_cash_flow"
        " ALTER COLUMN id SET DEFAULT nextval('company_cash_flow_id_seq')"
    )

    # ── 6. company_share_holdings ─────────────────────────────────────────────────
    op.create_table(
        "company_share_holdings",
        sa.Column("id",             sa.BigInteger(), nullable=False),
        sa.Column("instrument_key", sa.String(200),  nullable=False),
        sa.Column("isin",           sa.String(20),   nullable=False),
        sa.Column("period_label",   sa.String(20),   nullable=False),
        sa.Column("period_date",    sa.Date(),       nullable=False),
        sa.Column("promoters_pct",        sa.Numeric(6, 2), nullable=True),
        sa.Column("fii_pct",              sa.Numeric(6, 2), nullable=True),
        sa.Column("other_dii_pct",        sa.Numeric(6, 2), nullable=True),
        sa.Column("mutual_funds_pct",     sa.Numeric(6, 2), nullable=True),
        sa.Column("retail_and_other_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column(
            "fetched_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_csh_id"),
        sa.UniqueConstraint(
            "instrument_key", "period_date",
            name="uq_csh_instrument_period",
        ),
    )
    op.create_index(
        "idx_csh_instrument_period", "company_share_holdings",
        ["instrument_key", "period_date"],
    )
    op.execute(
        "CREATE SEQUENCE IF NOT EXISTS company_share_holdings_id_seq"
        " START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1"
    )
    op.execute(
        "ALTER TABLE company_share_holdings"
        " ALTER COLUMN id SET DEFAULT nextval('company_share_holdings_id_seq')"
    )

    # ── 7. company_corporate_actions ──────────────────────────────────────────────
    op.create_table(
        "company_corporate_actions",
        sa.Column("id",              sa.BigInteger(), nullable=False),
        sa.Column("instrument_key",  sa.String(200),  nullable=False),
        sa.Column("isin",            sa.String(20),   nullable=False),
        sa.Column("action_type",     sa.String(50),   nullable=False),
        sa.Column("expiry_date_str", sa.String(50),   nullable=True),
        sa.Column("amount",          sa.Numeric(12, 4), nullable=True),
        sa.Column("ratio",           sa.String(20),     nullable=True),
        sa.Column("event_details",   postgresql.JSONB(), nullable=True),
        sa.Column(
            "fetched_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cca_id"),
    )
    op.create_index("idx_cca_instrument_key", "company_corporate_actions", ["instrument_key"])
    op.execute(
        "CREATE SEQUENCE IF NOT EXISTS company_corporate_actions_id_seq"
        " START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1"
    )
    op.execute(
        "ALTER TABLE company_corporate_actions"
        " ALTER COLUMN id SET DEFAULT nextval('company_corporate_actions_id_seq')"
    )

    # ── 8. company_competitors ────────────────────────────────────────────────────
    op.create_table(
        "company_competitors",
        sa.Column("id",              sa.BigInteger(), nullable=False),
        sa.Column("instrument_key",  sa.String(200),  nullable=False),
        sa.Column("competitor_key",  sa.String(200),  nullable=False),
        sa.Column("company_profile", sa.Text(),       nullable=True),
        sa.Column("sector",          sa.String(100),  nullable=True),
        sa.Column("mcap_inr_crore",  sa.Numeric(20, 2), nullable=True),
        sa.Column("mcap_usd",        sa.Numeric(20, 4), nullable=True),
        sa.Column("mcap_usd_unit",   sa.String(10),     nullable=True),
        sa.Column(
            "fetched_at", sa.TIMESTAMP(timezone=True), nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cc_id"),
    )
    op.create_index("idx_cc_instrument_key", "company_competitors", ["instrument_key"])
    op.execute(
        "CREATE SEQUENCE IF NOT EXISTS company_competitors_id_seq"
        " START WITH 1 INCREMENT BY 1 NO MINVALUE NO MAXVALUE CACHE 1"
    )
    op.execute(
        "ALTER TABLE company_competitors"
        " ALTER COLUMN id SET DEFAULT nextval('company_competitors_id_seq')"
    )


# ── DOWNGRADE ──────────────────────────────────────────────────────────────────

def downgrade() -> None:
    op.drop_table("company_competitors")
    op.execute("DROP SEQUENCE IF EXISTS company_competitors_id_seq")

    op.drop_table("company_corporate_actions")
    op.execute("DROP SEQUENCE IF EXISTS company_corporate_actions_id_seq")

    op.drop_table("company_share_holdings")
    op.execute("DROP SEQUENCE IF EXISTS company_share_holdings_id_seq")

    op.drop_table("company_cash_flow")
    op.execute("DROP SEQUENCE IF EXISTS company_cash_flow_id_seq")

    op.drop_table("company_balance_sheet")
    op.execute("DROP SEQUENCE IF EXISTS company_balance_sheet_id_seq")

    op.drop_table("company_income_statement")
    op.execute("DROP SEQUENCE IF EXISTS company_income_statement_id_seq")

    op.drop_table("company_key_ratios")
    op.drop_table("company_fundamentals_profile")
