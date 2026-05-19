"""
Company Fundamentals ORM Models
================================
SQLAlchemy 2.0 declarative models for the Company Fundamentals subsystem.

Data is sourced from the Upstox v2 Fundamentals API and persisted point-in-time
correct for downstream ML feature joins. All monetary values are in Indian
Rupees (Crore) as returned by the API.

Table refresh strategies:
  - CompanyFundamentalsProfile   → upserted (one current row per company)
  - CompanyKeyRatios             → upserted (one current row per company)
  - CompanyIncomeStatement       → upserted per (instrument_key, period_date, time_period, statement_type)
  - CompanyBalanceSheet          → upserted per (instrument_key, period_date, statement_type)
  - CompanyCashFlow              → upserted per (instrument_key, period_date, statement_type)
  - CompanyShareHoldings         → upserted per (instrument_key, period_date)
  - CompanyCorporateActions      → DELETE + INSERT per company on each fetch
  - CompanyCompetitors           → DELETE + INSERT per company on each fetch
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CompanyFundamentalsProfile(Base):
    """Sector context and market cap snapshot — one row per company, upserted on each fetch."""

    __tablename__ = "company_fundamentals_profile"
    __table_args__ = (
        Index("idx_cfp_isin", "isin"),
    )

    instrument_key:       Mapped[str]        = mapped_column(String(200), primary_key=True)
    isin:                 Mapped[str]        = mapped_column(String(20),  nullable=False)
    company_profile:      Mapped[str | None] = mapped_column(Text(),      nullable=True)
    sector:               Mapped[str | None] = mapped_column(String(100), nullable=True)
    sector_mcap_inr:      Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    sector_mcap_usd:      Mapped[float | None] = mapped_column(Numeric(20, 4), nullable=True)
    sector_mcap_usd_unit: Mapped[str | None]   = mapped_column(String(10), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class CompanyKeyRatios(Base):
    """Current key financial ratios with sector benchmarks — one row per company, upserted."""

    __tablename__ = "company_key_ratios"
    __table_args__ = (
        Index("idx_ckr_isin", "isin"),
    )

    instrument_key:   Mapped[str]          = mapped_column(String(200), primary_key=True)
    isin:             Mapped[str]          = mapped_column(String(20),  nullable=False)
    pe:               Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    pb:               Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    roa:              Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    roe:              Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    roce:             Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    ev_ebitda:        Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    pe_sector:        Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    pb_sector:        Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    roa_sector:       Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    roe_sector:       Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    roce_sector:      Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    ev_ebitda_sector: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    extra_ratios:     Mapped[list | None]  = mapped_column(JSONB(astext_type=Text()), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class CompanyIncomeStatement(Base):
    """Revenue / operating-profit / net-profit history — yearly and quarterly."""

    __tablename__ = "company_income_statement"
    __table_args__ = (
        UniqueConstraint(
            "instrument_key", "period_date", "time_period", "statement_type",
            name="uq_cis_instrument_period_type",
        ),
        Index("idx_cis_instrument_period", "instrument_key", "period_date"),
    )

    id:             Mapped[int]  = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key: Mapped[str]  = mapped_column(String(200), nullable=False)
    isin:           Mapped[str]  = mapped_column(String(20),  nullable=False)
    period_label:   Mapped[str]  = mapped_column(String(20),  nullable=False)
    period_date:    Mapped[date] = mapped_column(Date(),      nullable=False)
    time_period:    Mapped[str]  = mapped_column(String(20),  nullable=False)
    statement_type: Mapped[str]  = mapped_column(String(20),  nullable=False)
    revenue:               Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    revenue_change_pct:    Mapped[float | None] = mapped_column(Numeric(),      nullable=True)
    op_profit:             Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    op_profit_change_pct:  Mapped[float | None] = mapped_column(Numeric(),      nullable=True)
    net_profit:            Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    net_profit_change_pct: Mapped[float | None] = mapped_column(Numeric(),      nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class CompanyBalanceSheet(Base):
    """Total assets / liabilities / net worth history. net_worth computed at write time."""

    __tablename__ = "company_balance_sheet"
    __table_args__ = (
        UniqueConstraint(
            "instrument_key", "period_date", "statement_type",
            name="uq_cbs_instrument_period_type",
        ),
        Index("idx_cbs_instrument_period", "instrument_key", "period_date"),
    )

    id:              Mapped[int]  = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key:  Mapped[str]  = mapped_column(String(200), nullable=False)
    isin:            Mapped[str]  = mapped_column(String(20),  nullable=False)
    period_label:    Mapped[str]  = mapped_column(String(20),  nullable=False)
    period_date:     Mapped[date] = mapped_column(Date(),      nullable=False)
    statement_type:  Mapped[str]  = mapped_column(String(20),  nullable=False)
    total_asset:     Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    total_liability: Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    net_worth:       Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class CompanyCashFlow(Base):
    """Operating / investing / financing cash flow history."""

    __tablename__ = "company_cash_flow"
    __table_args__ = (
        UniqueConstraint(
            "instrument_key", "period_date", "statement_type",
            name="uq_ccf_instrument_period_type",
        ),
        Index("idx_ccf_instrument_period", "instrument_key", "period_date"),
    )

    id:             Mapped[int]  = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key: Mapped[str]  = mapped_column(String(200), nullable=False)
    isin:           Mapped[str]  = mapped_column(String(20),  nullable=False)
    period_label:   Mapped[str]  = mapped_column(String(20),  nullable=False)
    period_date:    Mapped[date] = mapped_column(Date(),      nullable=False)
    statement_type: Mapped[str]  = mapped_column(String(20),  nullable=False)
    operating_cf:            Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    investing_cf:            Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    financing_cf:            Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    operating_cf_change_pct: Mapped[float | None] = mapped_column(Numeric(), nullable=True)
    investing_cf_change_pct: Mapped[float | None] = mapped_column(Numeric(), nullable=True)
    financing_cf_change_pct: Mapped[float | None] = mapped_column(Numeric(), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class CompanyShareHoldings(Base):
    """Quarterly shareholding breakdown across promoter, FII, DII, MF, and retail."""

    __tablename__ = "company_share_holdings"
    __table_args__ = (
        UniqueConstraint(
            "instrument_key", "period_date",
            name="uq_csh_instrument_period",
        ),
        Index("idx_csh_instrument_period", "instrument_key", "period_date"),
    )

    id:             Mapped[int]  = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key: Mapped[str]  = mapped_column(String(200), nullable=False)
    isin:           Mapped[str]  = mapped_column(String(20),  nullable=False)
    period_label:   Mapped[str]  = mapped_column(String(20),  nullable=False)
    period_date:    Mapped[date] = mapped_column(Date(),      nullable=False)
    promoters_pct:        Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    fii_pct:              Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    other_dii_pct:        Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    mutual_funds_pct:     Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    retail_and_other_pct: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class CompanyCorporateActions(Base):
    """Corporate event log (dividends, bonus, splits, rights) — DELETE+INSERT per company."""

    __tablename__ = "company_corporate_actions"
    __table_args__ = (
        Index("idx_cca_instrument_key", "instrument_key"),
    )

    id:              Mapped[int]        = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key:  Mapped[str]        = mapped_column(String(200), nullable=False)
    isin:            Mapped[str]        = mapped_column(String(20),  nullable=False)
    action_type:     Mapped[str]        = mapped_column(String(50),  nullable=False)
    expiry_date_str: Mapped[str | None] = mapped_column(String(50),  nullable=True)
    expiry_date:     Mapped[date | None] = mapped_column(Date(),     nullable=True)
    amount:          Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    ratio:           Mapped[str | None]   = mapped_column(String(20), nullable=True)
    event_details:   Mapped[list | None]  = mapped_column(JSONB(astext_type=Text()), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )


class CompanyCompetitors(Base):
    """Peer companies with sector market cap — DELETE+INSERT per subject company on each fetch."""

    __tablename__ = "company_competitors"
    __table_args__ = (
        Index("idx_cc_instrument_key", "instrument_key"),
    )

    id:              Mapped[int]        = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key:  Mapped[str]        = mapped_column(String(200), nullable=False)
    competitor_key:  Mapped[str]        = mapped_column(String(200), nullable=False)
    trading_symbol:  Mapped[str | None] = mapped_column(String(50),  nullable=True)
    name:            Mapped[str | None] = mapped_column(String(200), nullable=True)
    company_profile: Mapped[str | None] = mapped_column(Text(),      nullable=True)
    sector:          Mapped[str | None] = mapped_column(String(100), nullable=True)
    mcap_inr_crore:  Mapped[float | None] = mapped_column(Numeric(20, 2), nullable=True)
    mcap_usd:        Mapped[float | None] = mapped_column(Numeric(20, 4), nullable=True)
    mcap_usd_unit:   Mapped[str | None]   = mapped_column(String(10), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
