"""
Sector resolver tests
======================
NEWS_CONTEXT_RELEVANCE_GAP_FINDING.md fix: resolve_sector_and_peers reconciles
CompanyFundamentalsProfile.sector (curated, Upstox-sourced) against
sector_map's static ticker->sector dict, preferring the former when present.
These are integration tests — the resolver's fundamentals-first branch and
peer lookups both hit the real DB (rolled back per test via db_session).
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from app.ai.rag import sector_resolver
from app.ai.rag.sector_resolver import resolve_sector_and_peers
from app.models.fundamentals import CompanyFundamentalsProfile
from app.models.upstox_data import InstrumentMaster

pytestmark = pytest.mark.integration


async def _make_instrument(
    db, *, instrument_key: str, trading_symbol: str, name: str,
) -> None:
    db.add(
        InstrumentMaster(
            instrument_key=instrument_key,
            trading_symbol=trading_symbol,
            name=name,
            exchange="NSE",
            instrument_type="EQ",
            is_active=True,
        )
    )
    await db.flush()


async def _make_fundamentals_profile(
    db, *, instrument_key: str, sector: str,
) -> None:
    db.add(
        CompanyFundamentalsProfile(
            instrument_key=instrument_key,
            isin="INE000TEST01",
            sector=sector,
        )
    )
    await db.flush()


@pytest.fixture(autouse=True)
def _clear_sector_resolver_cache():
    """The resolver caches in-process for 900s — clear between tests."""
    sector_resolver._cache.clear()
    yield
    sector_resolver._cache.clear()


class TestFundamentalsSectorPreference:
    async def test_fundamentals_sector_wins_when_present(self, db_session):
        await _make_instrument(
            db_session, instrument_key="NSE_EQ|TESTFUND01",
            trading_symbol="TESTFUND", name="Test Fundamentals Ltd",
        )
        await _make_fundamentals_profile(
            db_session, instrument_key="NSE_EQ|TESTFUND01", sector="Specialty Widgets",
        )

        sector, peers = await resolve_sector_and_peers(
            db_session, "TESTFUND", "Test Fundamentals Ltd", "NSE_EQ|TESTFUND01",
        )
        assert sector == "Specialty Widgets"
        assert "TESTFUND" not in peers  # never includes itself

    async def test_fundamentals_peers_are_other_active_instruments_same_sector(self, db_session):
        await _make_instrument(
            db_session, instrument_key="NSE_EQ|TESTA01",
            trading_symbol="TESTA", name="Test Widget Co A",
        )
        await _make_fundamentals_profile(
            db_session, instrument_key="NSE_EQ|TESTA01", sector="Specialty Widgets",
        )
        await _make_instrument(
            db_session, instrument_key="NSE_EQ|TESTB01",
            trading_symbol="TESTB", name="Test Widget Co B",
        )
        await _make_fundamentals_profile(
            db_session, instrument_key="NSE_EQ|TESTB01", sector="Specialty Widgets",
        )
        await _make_instrument(
            db_session, instrument_key="NSE_EQ|TESTC01",
            trading_symbol="TESTC", name="Test Unrelated Co",
        )
        await _make_fundamentals_profile(
            db_session, instrument_key="NSE_EQ|TESTC01", sector="Unrelated Sector",
        )

        sector, peers = await resolve_sector_and_peers(
            db_session, "TESTA", "Test Widget Co A", "NSE_EQ|TESTA01",
        )
        assert sector == "Specialty Widgets"
        assert peers == frozenset({"TESTB"})

    async def test_no_fundamentals_row_falls_back_to_static_map(self, db_session):
        await _make_instrument(
            db_session, instrument_key="NSE_EQ|RELTEST01",
            trading_symbol="RELTEST", name="Reliance-shaped Test Co",
        )
        # No CompanyFundamentalsProfile row inserted — must fall back.
        sector, peers = await resolve_sector_and_peers(
            db_session, "RELIANCE", "Reliance Industries", None,
        )
        assert sector == "Oil & Gas"  # from sector_map._SYMBOL_SECTOR
        assert "ONGC" in peers  # a known Oil & Gas peer in the static map
        assert "RELIANCE" not in peers

    async def test_unresolvable_symbol_returns_none_and_empty_peers(self, db_session):
        sector, peers = await resolve_sector_and_peers(
            db_session, "TOTALLYUNKNOWNXYZ", None, None,
        )
        assert sector is None
        assert peers == frozenset()


class TestCache:
    async def test_result_is_cached_and_reused_within_ttl(self, db_session, monkeypatch):
        await _make_instrument(
            db_session, instrument_key="NSE_EQ|CACHETEST01",
            trading_symbol="CACHETEST", name="Cache Test Co",
        )
        await _make_fundamentals_profile(
            db_session, instrument_key="NSE_EQ|CACHETEST01", sector="Cache Sector",
        )

        first = await resolve_sector_and_peers(
            db_session, "CACHETEST", "Cache Test Co", "NSE_EQ|CACHETEST01",
        )
        assert first[0] == "Cache Sector"

        # Mutate the underlying data — a cached second call must not see it,
        # proving the result was served from the in-process cache, not re-queried.
        row = await db_session.get(CompanyFundamentalsProfile, "NSE_EQ|CACHETEST01")
        row.sector = "Mutated Sector"
        await db_session.flush()

        second = await resolve_sector_and_peers(
            db_session, "CACHETEST", "Cache Test Co", "NSE_EQ|CACHETEST01",
        )
        assert second[0] == "Cache Sector"  # still the cached value

    async def test_cache_expiry_forces_re_resolution(self, db_session, monkeypatch):
        await _make_instrument(
            db_session, instrument_key="NSE_EQ|EXPIRETEST01",
            trading_symbol="EXPIRETEST", name="Expiry Test Co",
        )
        await _make_fundamentals_profile(
            db_session, instrument_key="NSE_EQ|EXPIRETEST01", sector="Original Sector",
        )

        await resolve_sector_and_peers(
            db_session, "EXPIRETEST", "Expiry Test Co", "NSE_EQ|EXPIRETEST01",
        )

        row = await db_session.get(CompanyFundamentalsProfile, "NSE_EQ|EXPIRETEST01")
        row.sector = "Updated Sector"
        await db_session.flush()

        # Force the cache entry to look expired.
        cache_key = "NSE_EQ|EXPIRETEST01"
        sector, peers, _expiry = sector_resolver._cache[cache_key]
        sector_resolver._cache[cache_key] = (sector, peers, time.monotonic() - 1)

        refreshed = await resolve_sector_and_peers(
            db_session, "EXPIRETEST", "Expiry Test Co", "NSE_EQ|EXPIRETEST01",
        )
        assert refreshed[0] == "Updated Sector"
