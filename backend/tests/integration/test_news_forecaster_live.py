"""
Real-data integration tests for the Gemini news forecaster.

Runs against the live Postgres (real events + real OHLCV), live Redis, the real
ML feature pipeline, and the real Gemini API — no mocks, no synthetic data.

Coverage:
  - the indicator snapshot is captured from the real feature pipeline (the same
    numbers the ML model sees) — zero Gemini calls;
  - gather_news_forecast over real events + real indicators returns a valid
    event-slot signal, exercising either the live Gemini forecast or the
    deterministic NLP fallback (both are correct outcomes);
  - the per-symbol cache serves a repeat call without a second Gemini call.

A symbol is discovered at runtime (recent classified events + daily OHLCV); the
module skips if the live data has no such symbol or GEMINI_API_KEY is unset.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.ai.fusion.news_forecaster import score_from_direction
from app.ai.fusion.signal_assembler import SignalAssembler
from app.ai.intelligence.llm_client import CortexIntelligenceClient
from app.core.config import get_settings
from app.core.redis import close_redis, get_redis, init_redis
from app.ml.inference.feature_loader import FeatureLoader

pytestmark = [
    pytest.mark.integration,
    # google-genai's Client.__del__ schedules a redundant transport-close at GC
    # (closed explicitly in the fixture/lifespan) — filter that known SDK artifact.
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
    pytest.mark.skipif(
        not get_settings().GEMINI_API_KEY,
        reason="GEMINI_API_KEY not set — live forecaster integration skipped",
    ),
]

_DISCOVER_SQL = text("""
    SELECT sym
    FROM ai_event_classifications ec,
         LATERAL unnest(ec.affected_symbols) AS sym
    JOIN instrument_master im ON im.trading_symbol = sym
    JOIN upstox_ohlcv o ON o.instrument_key = im.instrument_key AND o.timeframe = '1D'
    WHERE ec.created_at > NOW() - make_interval(hours => 48)
    GROUP BY sym
    HAVING COUNT(DISTINCT o.timestamp) >= 60
    ORDER BY COUNT(*) DESC
    LIMIT 1
""")


@pytest.fixture
async def gemini_client():
    await CortexIntelligenceClient.initialize()
    yield
    import app.ai.intelligence.llm_client as L
    client, L._client = L._client, None
    if client is not None:
        await client.aclose()


@pytest.fixture
async def redis():
    await init_redis()
    yield get_redis()
    await close_redis()


@pytest.fixture
async def symbol(db_session):
    """A real symbol with recent classified events AND daily OHLCV."""
    sym = (await db_session.execute(_DISCOVER_SQL)).scalar()
    if not sym:
        pytest.skip("No symbol with recent classified events + daily OHLCV in live data")
    return sym


async def test_indicator_snapshot_is_real(db_session, redis, symbol):
    """The ML feature pipeline yields real, human-readable indicators (no Gemini)."""
    loader = FeatureLoader(db=db_session, redis=redis)
    snapshot: dict = {}
    await loader.load_features(symbol, timeframe="1d", indicator_snapshot_out=snapshot)

    assert snapshot, "indicator snapshot should be populated from real features"
    # At least one canonical technical indicator must be present with a real value.
    known = {"rsi_14", "rsi_21", "macd_line", "ema_20", "ema_50", "atr_14"}
    present = known & snapshot.keys()
    assert present, f"expected canonical indicators, got keys: {sorted(snapshot)[:10]}"
    for k in present:
        assert isinstance(snapshot[k], float)


async def test_forecast_over_real_events_and_indicators(
    db_session, redis, symbol, gemini_client
):
    """gather_news_forecast over real events + real indicators → valid event-slot.

    Accepts both the live Gemini forecast and the deterministic NLP fallback —
    both are correct real outcomes (fallback covers quota/timeout)."""
    assembler = SignalAssembler(redis=redis)

    loader = FeatureLoader(db=db_session, redis=redis)
    indicators: dict = {}
    await loader.load_features(symbol, timeframe="1d", indicator_snapshot_out=indicators)

    event_signals = await assembler.gather_event_signals(db_session, symbol)
    assert event_signals.get("events"), f"{symbol} should have recent events"

    result = await assembler.gather_news_forecast(
        db_session, symbol,
        ml_signal={"indicators": indicators},
        event_signals=event_signals,
    )

    # Event-slot contract (consumed unchanged by fuse_signals / engine consensus).
    assert result["available"] is True
    assert -100.0 <= float(result["score"]) <= 100.0
    assert 0.0 <= float(result["confidence"]) <= 1.0
    assert result["forecast_source"] in {"gemini", "fallback"}

    if result["forecast_source"] == "gemini":
        assert result["direction"] in {"BUY", "SELL", "HOLD"}
        assert result["rationale"]
        # Score is the deterministic mapping of the model's own direction+confidence.
        expected = score_from_direction(result["direction"], result["confidence"])
        assert result["score"] == pytest.approx(expected)

        # Second identical call is served from cache (no extra Gemini call).
        cached = await assembler.gather_news_forecast(
            db_session, symbol,
            ml_signal={"indicators": indicators},
            event_signals=event_signals,
        )
        assert cached == result
