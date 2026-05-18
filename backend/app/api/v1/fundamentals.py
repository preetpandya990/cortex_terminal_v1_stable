"""
Company Fundamentals — API Router
===================================
GET /api/v1/fundamentals/{instrument_key}

Returns the full fundamentals snapshot for any NSE EQ instrument via a
Redis → DB → live-fetch cascade. The response is always HTTP 200:
  - available=true  + all data sections populated → EQ instrument, data found
  - available=false + reason field                → derivative / fetch failure

Rate limit: 30 req/min per user (UI-triggered, not hot-path).
Auth:       Standard JWT Bearer (same as all other endpoints).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.redis import get_redis
from app.models.user import User
from app.schemas.fundamentals import FundamentalsFullResponse
from app.services import fundamentals_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/{instrument_key:path}",
    response_model=FundamentalsFullResponse,
    summary="Get company fundamentals",
    description=(
        "Returns profile, key ratios, financial statements, shareholding history, "
        "corporate actions, and competitor data for an NSE EQ instrument. "
        "Cached for 24 hours. Non-EQ instruments (F&O, indices) return available=false."
    ),
)
@limiter.limit("30/minute")
async def get_fundamentals(
    request: Request,
    instrument_key: str = Path(
        ...,
        description="Upstox instrument key, e.g. NSE_EQ|INE002A01018",
        min_length=5,
    ),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
) -> FundamentalsFullResponse:
    redis = get_redis()

    # EQ guard — derivatives don't have fundamental data.
    if not await fundamentals_service.is_eq_instrument(instrument_key, db):
        logger.info("Fundamentals not available for non-EQ instrument=%s", instrument_key)
        return FundamentalsFullResponse(
            available=False,
            instrument_key=instrument_key,
            reason="derivatives_not_supported",
        )

    data = await fundamentals_service.get_fundamentals(instrument_key, db, redis)
    return FundamentalsFullResponse(**data)
