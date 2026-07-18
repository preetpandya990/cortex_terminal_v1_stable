"""
Tests for app.services.instrument_classifier — pure asset-class classification.

Cortex is scoped to single-company equities only. NSE's EQ trading series
covers stocks, ETFs, mutual-fund units, and REIT/InvIT trust units
identically, so classification here relies on ISIN prefix (INE = corporate
securities, INF = AMFI-registered fund units) plus a curated REIT/InvIT
symbol registry for the cases ISIN prefix alone cannot catch (REITs/InvITs
are issued with a corporate-registrar INE-prefixed ISIN, same as stock).
"""
from __future__ import annotations

import pytest

from app.services.instrument_classifier import AssetClass, classify_asset


@pytest.mark.parametrize(
    "isin, symbol, expected",
    [
        # Ordinary corporate ISIN -> STOCK.
        ("INE001A01036", "RELIANCE", AssetClass.STOCK),
        ("ine002a01018", "SBIN", AssetClass.STOCK),  # lowercase input normalised
        # AMFI/mutual-fund-registered ISIN -> ETF_FUND, real examples.
        ("INF579M01BP5", "MSCI360", AssetClass.ETF_FUND),
        ("INF204KB17I5", "GOLDBEES", AssetClass.ETF_FUND),
        ("INF204KC1402", "SILVERBEES", AssetClass.ETF_FUND),
        # Curated REIT/InvIT registry wins even though these carry an
        # INE-prefixed (corporate-registrar) ISIN, same as ordinary stock.
        ("INE0FDU25010", "BIRET", AssetClass.TRUST_UNIT),
        ("INE1ABCDEFGH", "EMBASSY", AssetClass.TRUST_UNIT),
        ("INE1ABCDEFGH", "indigrid", AssetClass.TRUST_UNIT),  # case-insensitive symbol match
        # Missing or unrecognised ISIN -> UNCLASSIFIED (fail closed, never STOCK).
        (None, "UNKNOWN", AssetClass.UNCLASSIFIED),
        ("", "UNKNOWN", AssetClass.UNCLASSIFIED),
        ("US0378331005", "AAPL", AssetClass.UNCLASSIFIED),  # foreign ISIN prefix
    ],
)
def test_classify_asset(isin, symbol, expected):
    assert classify_asset(isin, symbol) is expected


def test_trust_unit_registry_takes_precedence_over_isin_prefix():
    # Even an INF-prefixed ISIN (which would otherwise mean ETF_FUND) must
    # still classify as TRUST_UNIT if the symbol is a known REIT/InvIT —
    # the symbol registry is checked first.
    assert classify_asset("INF999X99999", "MINDSPACE") is AssetClass.TRUST_UNIT


def test_classify_asset_never_raises_on_malformed_input():
    assert classify_asset("X", "SYM") is AssetClass.UNCLASSIFIED
    assert classify_asset("IN", "SYM") is AssetClass.UNCLASSIFIED
