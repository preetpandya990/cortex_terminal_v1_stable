"""
Cortex AI — Strategy / Market Regime API
==========================================
Three endpoints:

  GET /strategy/market/overview
    Nifty 50 macro regime + all 10 index regimes computed from DB OHLCV.
    Cache: 15 min.

  GET /strategy/index/{index_key}/constituents
    Scrollable constituent stock list with individual regime + indicators.
    Cache: 5 min.

  GET /strategy/instrument/{instrument_key}/regime
    Single instrument detailed regime for the drill-down detail pane.
    Cache: 5 min.

All computation is driven by upstox_ohlcv (timeframe='1D') — no external
API calls, no randomised mock data.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import get_current_user
from app.core.limiter import limiter
from app.core.redis import CacheService, get_cache_service
from app.services.regime_service import NIFTY_INDICES, regime_service

logger = logging.getLogger(__name__)

router = APIRouter()

_CACHE_TTL_OVERVIEW = 900   # 15 min — indices don't change minute-to-minute
_CACHE_TTL_INDEX = 300      # 5 min
_CACHE_TTL_INSTRUMENT = 300 # 5 min


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _cache_get(cache: CacheService, key: str) -> dict | None:
    try:
        return await cache.get(key)
    except Exception:
        return None


async def _cache_set(cache: CacheService, key: str, value: dict, ttl: int) -> None:
    try:
        await cache.set(key, value, ttl=ttl)
    except Exception:
        pass


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/market/overview")
@limiter.limit("30/minute")
async def get_market_overview(
    request: Request,
    db: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Returns Nifty 50 macro regime + all 10 Nifty index regimes.

    Response shape:
    {
      "nifty50":    { index_key, name, regime, confidence, strength,
                      change_pct, trend, bullish_count, ... },
      "breadth":    { bullish_pct, bearish_pct, sideways_pct, volatile_pct },
      "all_indices": [ ...IndexRegime... ],
      "computed_at": "ISO-8601"
    }
    """
    cache_key = "regime:overview"
    cached = await _cache_get(cache, cache_key)
    if cached:
        return cached

    try:
        result = await regime_service.get_market_overview(db)
        await _cache_set(cache, cache_key, result, _CACHE_TTL_OVERVIEW)
        return result
    except Exception as exc:
        logger.exception("Market overview computation failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "regime_computation_failed", "message": str(exc)},
        )


@router.get("/index/{index_key}/constituents")
@limiter.limit("30/minute")
async def get_index_constituents(
    request: Request,
    index_key: str,
    db: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Returns the constituent stock list for the given Nifty index,
    each with regime, indicators, price, and % change.

    index_key: one of nifty50 | niftybank | niftyit | niftyauto |
                       niftyfmcg | niftypharma | niftymetal | niftyrealty |
                       niftymidcap50 | niftysmallcap50

    Response shape:
    {
      "index_key":   str,
      "name":        str,
      "constituents": [
        {
          instrument_key, trading_symbol, company_name,
          current_price, change_pct, change_pct_20d, trend,
          regime, confidence, strength,
          indicators: { adx, rsi, atr, atr_pct, bollinger_width }
        }
      ],
      "total": int,
      "computed_at": "ISO-8601"
    }
    """
    if index_key not in NIFTY_INDICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unknown_index",
                "message": f"'{index_key}' is not a recognised index key.",
                "valid_keys": list(NIFTY_INDICES.keys()),
            },
        )

    cache_key = f"regime:idx:{index_key}"
    cached = await _cache_get(cache, cache_key)
    if cached:
        return cached

    try:
        result = await regime_service.get_index_constituents(db, index_key)
        await _cache_set(cache, cache_key, result, _CACHE_TTL_INDEX)
        return result
    except Exception as exc:
        logger.exception("Index constituents computation failed for %s", index_key)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "regime_computation_failed", "message": str(exc)},
        )


@router.get("/instrument/{instrument_key}/regime")
@limiter.limit("60/minute")
async def get_instrument_regime(
    request: Request,
    instrument_key: str,
    db: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Returns detailed regime + full indicator set for a single instrument.
    Used by the instrument detail pane.

    Response shape:
    {
      instrument_key, trading_symbol, company_name,
      current_price, change_pct, change_pct_20d, trend,
      regime, confidence, strength, data_points,
      indicators: { adx, rsi, atr, atr_pct, bollinger_width },
      computed_at: "ISO-8601"
    }
    """
    safe_key = instrument_key.replace("/", "_").replace("|", "_")
    cache_key = f"regime:instr:{safe_key}"
    cached = await _cache_get(cache, cache_key)
    if cached:
        return cached

    try:
        # Resolve trading_symbol + name from instrument_master
        from sqlalchemy import text
        row = (await db.execute(
            text("""
                SELECT trading_symbol, name
                FROM instrument_master
                WHERE instrument_key = :key
                LIMIT 1
            """),
            {"key": instrument_key},
        )).fetchone()

        trading_symbol = row[0] if row else instrument_key.split("|")[-1]
        company_name = (row[1] if row else trading_symbol) or trading_symbol

        result = await regime_service.get_instrument_regime(
            db, instrument_key, trading_symbol, company_name
        )
        await _cache_set(cache, cache_key, result, _CACHE_TTL_INSTRUMENT)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Instrument regime failed for %s", instrument_key)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "regime_computation_failed", "message": str(exc)},
        )
