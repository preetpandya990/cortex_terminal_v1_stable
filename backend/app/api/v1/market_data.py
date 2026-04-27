# PATH: backend/app/api/v1/market_data.py
# ─────────────────────────────────
"""
Cortex AI — Market Data Router
================================
All endpoints require authentication.
Errors never expose internal details to clients.
Redis caches live quotes and instrument search results.
"""
import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import get_db
from app.core.redis import CacheService, get_cache_service
from app.core.security import get_current_user_id
from app.core.limiter import limiter
from app.schemas.market_data import InstrumentSearchResult, InstrumentSearchResponse, LivePriceResponse, OHLCVResponse
from app.models.upstox_data import UpstoxOHLCV
from app.services.upstox_client import UpstoxClient, get_upstox_client
from app.services.upstox_persistence import search_instruments_db

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(dependencies=[Depends(get_current_user_id)])


@router.get("/live/{instrument_key}", response_model=LivePriceResponse)
@limiter.limit(settings.RATE_LIMIT_MARKET_DATA)
async def get_live_price(
    request: Request,
    instrument_key: str,
    user_id: str = Depends(get_current_user_id),
    upstox: UpstoxClient = Depends(get_upstox_client),
    cache: CacheService = Depends(get_cache_service),
) -> LivePriceResponse:
    """Return the latest market quote. Cached for 1 second."""
    cache_key = f"live_price:{instrument_key}"
    cached = await cache.get(cache_key)
    if cached:
        return LivePriceResponse(**cached)

    data = await upstox.get(
        "/market-quote/ltp",
        params={"instrument_key": instrument_key},
    )
    # V3 API returns data with trading symbol key (e.g., "NSE_EQ:RELIANCE")
    # instead of instrument_key, so we get the first value
    data_dict = data.get("data", {})
    # Some instruments (suspended, unlisted, or BE-group) have no active quote.
    # Return null last_price rather than 404 so callers can handle it gracefully.
    if not data_dict:
        result = LivePriceResponse(instrument_key=instrument_key, last_price=None)
        await cache.set(cache_key, result.model_dump(mode="json"), ttl=settings.CACHE_TTL_LIVE_QUOTE)
        return result

    quote = next(iter(data_dict.values()))
    result = LivePriceResponse(
        instrument_key=instrument_key,
        last_price=quote.get("last_price"),
        timestamp=quote.get("timestamp"),
    )
    await cache.set(cache_key, result.model_dump(mode="json"), ttl=settings.CACHE_TTL_LIVE_QUOTE)
    return result


@router.get("/instruments/search", response_model=InstrumentSearchResponse)
@limiter.limit(settings.RATE_LIMIT_SEARCH)
async def search_instruments(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    q: str = Query(..., min_length=1, max_length=50),
    limit: int = Query(default=20, ge=1, le=100),
    cache: CacheService = Depends(get_cache_service),
) -> InstrumentSearchResponse:
    """Search instrument master by symbol or name. Min 1 char. Cached 5 min."""
    cache_key = f"instrument_search:{q.lower()}:{limit}"
    cached = await cache.get(cache_key)
    if cached:
        return InstrumentSearchResponse(**cached)

    results = await search_instruments_db(q=q, limit=limit)
    response = InstrumentSearchResponse(
        query=q,
        count=len(results),
        results=results
    )
    await cache.set(
        cache_key,
        response.model_dump(mode="json"),
        ttl=settings.CACHE_TTL_INSTRUMENTS,
    )
    return response


@router.get("/ohlcv/{instrument_key}", response_model=OHLCVResponse)
@limiter.limit(settings.RATE_LIMIT_MARKET_DATA)
async def get_ohlcv_data(
    request: Request,
    instrument_key: str,
    user_id: str = Depends(get_current_user_id),
    timeframe: str = Query(default="1m", pattern=r"^(1m|5m|15m|30m|1h|4h|1d|1w)$"),
    limit: int = Query(default=120, ge=1, le=2000),
    session: AsyncSession = Depends(get_db),
) -> OHLCVResponse:
    stmt = (
        select(UpstoxOHLCV)
        .where(
            UpstoxOHLCV.instrument_key == instrument_key,
            UpstoxOHLCV.timeframe == timeframe,
        )
        .order_by(UpstoxOHLCV.timestamp.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()

    candles = [
        [
            row.timestamp.isoformat(),
            float(row.open),
            float(row.high),
            float(row.low),
            float(row.close),
            int(row.volume),
            int(row.oi or 0),
        ]
        for row in rows
    ]
    candles.reverse()

    return OHLCVResponse(
        instrument_key=instrument_key,
        timeframe=timeframe,
        candles=candles,
        source="db",
    )
