"""
Instrument asset-class classifier.
===================================
Cortex only trades single-company equities — no futures, no ETFs, no
mutual-fund units, no REIT/InvIT trust units. NSE does not distinguish these
by trading series: the ``EQ`` series covers "fully paid equity shares, ETFs,
units of REITs/InvITs, and partly-paid equity shares" identically, so
``instrument_type`` alone (see ``instrument_fetch._ALLOWED_TYPES``) cannot
separate a real stock from a fund unit.

Classification signals, in precedence order:
  1. Trading symbol matches the curated REIT/InvIT registry below — these are
     issued with a corporate-registrar (``INE``-prefixed) ISIN, same as
     ordinary stock, so ISIN prefix alone cannot catch them.
  2. ISIN prefix ``INF`` — SEBI/AMFI-issued prefix reserved for mutual-fund
     and ETF units (AMFI is the registering authority, not NSDL/CDSL).
  3. ISIN prefix ``INE`` — the standard prefix for Indian corporate
     securities (equity/preference shares/debentures) issued via NSDL/CDSL.
  4. Anything else (missing/malformed ISIN, unrecognised prefix) is
     ``UNCLASSIFIED`` — an explicit allow-list failure, not a fallback to
     ``STOCK``. Every eligibility gate in the codebase must check for the
     literal ``STOCK`` value; a new NSE instrument category that doesn't
     match any rule above is excluded by default rather than silently
     let through.

This module is pure (no I/O, no DB access) so classification is unit-testable
in isolation. It is called once per row at sync time
(``data_ingestion.sync_instrument_master``) and the result is persisted to
``instrument_master.asset_class`` — never re-derived at query time.
"""
from __future__ import annotations

from enum import Enum


class AssetClass(str, Enum):
    STOCK = "STOCK"
    ETF_FUND = "ETF_FUND"
    TRUST_UNIT = "TRUST_UNIT"
    UNCLASSIFIED = "UNCLASSIFIED"


# ── Curated REIT/InvIT registry ────────────────────────────────────────────────
# REITs and InvITs use a corporate-registrar (INE-prefixed) ISIN, identical in
# form to ordinary equity, so they cannot be distinguished by ISIN prefix and
# must be matched by trading symbol instead. India lists only a couple dozen
# of these total and new listings are rare (roughly one every few months), so
# a small in-repo list — reviewed like any other reference-data change — is
# the appropriate maintenance model rather than a live external fetch.
#
# NOTE: this is a starting list compiled from public sources at the time this
# module was written. Verify against NSE's official REIT/InvIT listing
# (nseindia.com) before relying on it in production, and re-verify
# periodically as new trusts list.
_TRUST_UNIT_SYMBOLS: frozenset[str] = frozenset({
    # REITs
    "EMBASSY",     # Embassy Office Parks REIT
    "MINDSPACE",   # Mindspace Business Parks REIT
    "BIRET",       # Brookfield India Real Estate Trust
    "NXST",        # Nexus Select Trust
    "KRT",         # Knowledge Realty Trust
    # InvITs
    "INDIGRID",    # IndiGrid Infrastructure Trust
    "IRBINVIT",    # IRB InvIT Fund
    "PGINVIT",     # PowerGrid Infrastructure Investment Trust
    "INDUSINFRA",  # Indus Infra Trust (formerly Bharat Highways InvIT)
})

_MUTUAL_FUND_ISIN_PREFIX = "INF"
_CORPORATE_ISIN_PREFIX = "INE"


def classify_asset(isin: str | None, trading_symbol: str) -> AssetClass:
    """Classify an NSE instrument as STOCK, ETF_FUND, TRUST_UNIT, or UNCLASSIFIED.

    Args:
        isin: the instrument's ISIN, if known (e.g. ``"INE001A01036"``).
        trading_symbol: the NSE trading symbol (e.g. ``"RELIANCE"``).

    Returns:
        The classified ``AssetClass``. Never raises — an unrecognised or
        missing ISIN yields ``UNCLASSIFIED`` rather than an exception, since
        this runs inline in a bulk sync loop over thousands of rows.
    """
    symbol = trading_symbol.strip().upper() if trading_symbol else ""
    if symbol in _TRUST_UNIT_SYMBOLS:
        return AssetClass.TRUST_UNIT

    if not isin:
        return AssetClass.UNCLASSIFIED

    prefix = isin.strip().upper()[:3]
    if prefix == _MUTUAL_FUND_ISIN_PREFIX:
        return AssetClass.ETF_FUND
    if prefix == _CORPORATE_ISIN_PREFIX:
        return AssetClass.STOCK

    return AssetClass.UNCLASSIFIED
