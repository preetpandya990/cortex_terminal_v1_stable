"""
Company Fundamentals — Pydantic v2 Schemas
==========================================
Response serialisation types for the GET /api/v1/fundamentals/{instrument_key} endpoint.

All schemas are read-only response types — fundamentals data is fetched from
Upstox and never written by API consumers.

Design rules:
  - float | None for all numeric fields — percentages stored as decimal (8.94, not 0.0894)
  - period_date serialised as ISO date string (YYYY-MM-DD) for JSON portability
  - fetched_at serialised as ISO datetime string
  - from_attributes=True on all schemas so FastAPI can pass ORM objects directly
  - FundamentalsFullResponse is the single root type returned by the endpoint;
    available=False + reason field is the only response for non-EQ instruments
"""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


# ── Shared config ──────────────────────────────────────────────────────────────

class _OrmBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Sub-schemas ────────────────────────────────────────────────────────────────

class ProfileData(_OrmBase):
    company_profile:      str | None
    sector:               str | None
    sector_mcap_inr:      float | None
    sector_mcap_usd:      float | None
    sector_mcap_usd_unit: str | None


class KeyRatioItem(_OrmBase):
    name:          str
    company_value: float | None
    sector_value:  float | None


class PeriodValue(_OrmBase):
    value:       float
    period:      str
    period_date: date
    change_pct:  float | None


class CategoryHistory(_OrmBase):
    category: str
    history:  list[PeriodValue]


class BalanceSheetEntry(_OrmBase):
    total_asset:     float | None
    total_liability: float | None
    net_worth:       float | None
    period:          str
    period_date:     date


class ShareHoldingEntry(_OrmBase):
    promoters_pct:        float | None
    fii_pct:              float | None
    other_dii_pct:        float | None
    mutual_funds_pct:     float | None
    retail_and_other_pct: float | None
    period:               str
    period_date:          date


class CorporateAction(_OrmBase):
    action_type:     str
    expiry_date_str: str | None
    amount:          float | None
    ratio:           str | None
    event_details:   list[dict] | None


class CompetitorEntry(_OrmBase):
    instrument_key:  str
    trading_symbol:  str | None = None
    name:            str | None = None
    company_profile: str | None
    sector:          str | None
    mcap_inr_crore:  float | None
    mcap_usd:        float | None
    mcap_usd_unit:   str | None


# ── Root response ──────────────────────────────────────────────────────────────

class FundamentalsFullResponse(BaseModel):
    """
    Root response type for GET /fundamentals/{instrument_key}.

    When available=False the reason field explains why (derivatives, missing ISIN, etc.)
    and all data sections are None. The HTTP status is always 200 — unavailability is
    a business-level state, not an error.
    """
    available:       bool
    instrument_key:  str
    isin:            str | None = None
    reason:          str | None = None

    profile:          ProfileData | None = None
    key_ratios:       list[KeyRatioItem] | None = None
    income_statement: list[CategoryHistory] | None = None
    balance_sheet:    list[BalanceSheetEntry] | None = None
    cash_flow:        list[CategoryHistory] | None = None
    share_holdings:   list[ShareHoldingEntry] | None = None
    corporate_actions: list[CorporateAction] | None = None
    competitors:      list[CompetitorEntry] | None = None
    fetched_at:       datetime | None = None

    model_config = ConfigDict(from_attributes=True)
