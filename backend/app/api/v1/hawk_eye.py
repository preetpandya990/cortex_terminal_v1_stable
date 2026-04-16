"""
Cortex AI — HawkEye Router
============================
Multi-timeframe scan endpoint. Authenticated and rate-limited.
Results cached in Redis to avoid re-running the full scan on every request.
"""
import logging

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import CacheService, get_cache_service
from app.core.security import get_current_user_id
from app.core.limiter import limiter
from app.schemas.hawk_eye import HawkEyeResponse
from app.services.hawk_eye import HawkEyeService, TIMEFRAME_WEIGHTS

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user_id)])

_VALID_TIMEFRAMES = set(TIMEFRAME_WEIGHTS.keys())


@router.get("/scan", response_model=HawkEyeResponse)
@limiter.limit("10/minute")
async def hawk_eye_scan(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    timeframes: list[str] | None = Query(
        default=None, 
        description="Timeframes to scan. Defaults to all: 1w,1d,4h,1h,15m"
    ),
    force_refresh: bool = Query(default=False),
    session: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
) -> HawkEyeResponse:
    """
    Run a multi-timeframe HawkEye scan.
    For each instrument, computes signals across all requested timeframes
    and returns a composite directional signal with agreement percentage.
    """
    # Validate timeframes
    requested = timeframes or list(_VALID_TIMEFRAMES)
    invalid =[tf for tf in requested if tf not in _VALID_TIMEFRAMES]
    if invalid:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail=f"Invalid timeframes: {invalid}. Valid: {sorted(_VALID_TIMEFRAMES)}",
        )

    service = HawkEyeService(cache=cache)
    return await service.scan(session, timeframes=requested, force_refresh=force_refresh)



@router.get("/analyze")
@limiter.limit("30/minute")
async def analyze_instrument(
    request: Request,
    instrument_key: str = Query(...),
    timeframe: str = Query("1d"),
    user_id: str = Depends(get_current_user_id),
    cache: CacheService = Depends(get_cache_service),
):
    """Get technical analysis for a specific instrument."""
    cache_key = f"hawk_eye:analyze:{instrument_key}:{timeframe}"
    cached = await cache.get(cache_key)
    if cached:
        return cached

    # Mock analysis data - integrate with actual service later
    result = {
        "instrument_key": instrument_key,
        "timeframe": timeframe,
        "indicators": {
            "rsi": 55.3,
            "macd": {"value": 2.5, "signal": 1.8, "histogram": 0.7},
            "moving_averages": {
                "sma_20": 1250.5,
                "sma_50": 1245.2,
                "ema_20": 1252.1
            },
            "bollinger_bands": {
                "upper": 1280.0,
                "middle": 1250.0,
                "lower": 1220.0
            }
        },
        "signals": {
            "trend": "bullish",
            "strength": "moderate",
            "recommendation": "hold"
        }
    }

    await cache.set(cache_key, result, ttl=300)
    return result


@router.get("/fundamentals")
@limiter.limit("30/minute")
async def get_fundamentals(
    request: Request,
    instrument_key: str = Query(...),
    user_id: str = Depends(get_current_user_id),
    cache: CacheService = Depends(get_cache_service),
):
    """Get fundamental data for a specific instrument."""
    cache_key = f"hawk_eye:fundamentals:{instrument_key}"
    cached = await cache.get(cache_key)
    if cached:
        return cached

    # Mock fundamentals data - integrate with actual data source later
    result = {
        "instrument_key": instrument_key,
        "company_name": "Sample Company Ltd",
        "sector": "Technology",
        "market_cap": 50000000000,
        "pe_ratio": 25.5,
        "pb_ratio": 3.2,
        "dividend_yield": 1.5,
        "eps": 45.2,
        "roe": 18.5,
        "debt_to_equity": 0.5,
        "current_ratio": 1.8,
        "revenue_growth": 15.2,
        "profit_margin": 12.5
    }

    await cache.set(cache_key, result, ttl=3600)  # Cache for 1 hour
    return result
