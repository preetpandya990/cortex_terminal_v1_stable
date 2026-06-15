"""
Real-data integration tests for the OHLCV staleness guard.

Runs against the live database — verifies the robust (outlier-proof) data
frontier, that symbols at the frontier are fresh while genuinely-lagging symbols
are flagged, and that the news forecaster abstains when the ML signal is stale.
No mocks, no synthetic data, no Gemini calls.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.ai.fusion.signal_assembler import SignalAssembler
from app.core.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def assembler():
    return SignalAssembler()


async def test_frontier_is_the_bulk_not_an_outlier(db_session, assembler):
    frontier = await assembler._ohlcv_frontier(db_session)
    assert frontier is not None
    # The frontier day must itself carry the bulk of the universe (the count
    # threshold), so a single instrument synced ahead cannot define it.
    n = (await db_session.execute(text(
        "SELECT COUNT(*) FROM upstox_ohlcv WHERE timeframe='1D' "
        "AND date_trunc('day', timestamp) = :d"
    ), {"d": frontier})).scalar()
    assert n >= get_settings().STALENESS_FRONTIER_MIN_INSTRUMENTS


async def test_symbol_at_frontier_is_fresh(db_session, assembler):
    frontier = await assembler._ohlcv_frontier(db_session)
    ik = (await db_session.execute(text(
        "SELECT instrument_key FROM (SELECT instrument_key, "
        "date_trunc('day', max(timestamp)) d FROM upstox_ohlcv WHERE timeframe='1D' "
        "GROUP BY instrument_key) x WHERE d = :f LIMIT 1"
    ), {"f": frontier})).scalar()
    assert ik, "expected at least one instrument at the frontier"
    stale, info = await assembler._data_stale(db_session, ik)
    assert stale is False
    assert info["lag_days"] == 0


async def test_lagging_symbol_is_flagged(db_session, assembler):
    frontier = await assembler._ohlcv_frontier(db_session)
    tol = get_settings().STALENESS_MAX_LAG_DAYS
    # A symbol whose latest bar is clearly behind the frontier (beyond tolerance).
    ik = (await db_session.execute(text(
        "SELECT instrument_key FROM (SELECT instrument_key, "
        "date_trunc('day', max(timestamp)) d FROM upstox_ohlcv WHERE timeframe='1D' "
        "GROUP BY instrument_key) x WHERE d < :cut LIMIT 1"
    ), {"cut": frontier - timedelta(days=tol + 2)})).scalar()
    if not ik:
        pytest.skip("no sufficiently-lagging instrument in the live data")
    stale, info = await assembler._data_stale(db_session, ik)
    assert stale is True
    assert info["lag_days"] > tol


async def test_forecaster_abstains_when_signal_stale(db_session, assembler):
    # A stale ML signal must make the forecaster abstain WITHOUT any Gemini call.
    event_signals = await assembler.gather_event_signals(db_session, "RELIANCE")
    result = await assembler.gather_news_forecast(
        db_session, "RELIANCE",
        ml_signal={"stale": True, "available": False},
        event_signals=event_signals,
    )
    assert result["forecast_source"] == "stale_data"
    assert result["available"] is False
