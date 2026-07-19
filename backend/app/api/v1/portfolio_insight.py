"""
Portfolio Insight & Advise — API router
=========================================
Read-only advisory endpoints over the authenticated user's active paper
portfolio:

  GET  /portfolio-insight/stats   — the portfolio risk panel (B4). Fast, pure
                                     DB math, react-query cacheable.
  POST /portfolio-insight/advice  — on-demand AI advice (B5). Cached by
                                     materiality; degrades to stale-cached on
                                     quota exhaustion, never 500s.

The whole feature is gated by ``INSIGHT_ENABLED`` (staged rollout): both
endpoints 404 when the flag is off, so nothing is exposed before release.
Conventions mirror ``paper_trading.py`` — router-level auth dependency,
``Request``-first signatures for the limiter, thin controller → service.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.config import get_settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.redis import get_redis
from app.core.security import get_current_user_id
from app.models.user import User
from app.schemas.portfolio_insight import PortfolioAdvice, PortfolioInsightStats
from app.services.paper_trading import portfolio_advice_service, portfolio_service
from app.services.paper_trading.portfolio_insight_service import (
    compute_portfolio_insight_stats,
)


async def _require_insight_enabled() -> None:
    """404 the whole feature when the staged-rollout flag is off."""
    if not get_settings().INSIGHT_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


router = APIRouter(
    dependencies=[Depends(get_current_user_id), Depends(_require_insight_enabled)],
)


def _uid(user: User) -> int:
    return int(user.id)


@router.get(
    "/stats",
    response_model=PortfolioInsightStats,
    summary="Portfolio risk panel (capital-at-risk, concentration, correlation, stress)",
)
@limiter.limit("60/minute")
async def get_insight_stats(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> PortfolioInsightStats:
    """Compute the portfolio-level risk panel for the user's active portfolio."""
    portfolio = await portfolio_service.get_active_portfolio(session, _uid(current_user))
    return await compute_portfolio_insight_stats(session, portfolio)


@router.post(
    "/advice",
    response_model=PortfolioAdvice,
    summary="On-demand AI portfolio advice (cached, quota-safe)",
)
@limiter.limit("10/minute")
async def post_insight_advice(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> PortfolioAdvice:
    """
    Generate (or serve cached) AI advice for the user's active portfolio.

    Cached by a materiality hash so an unchanged portfolio never re-spends quota;
    on quota/rate-limit exhaustion the last-cached advice is returned with
    ``stale=True`` (never 500).
    """
    portfolio = await portfolio_service.get_active_portfolio(session, _uid(current_user))
    return await portfolio_advice_service.generate_portfolio_advice(session, portfolio, redis)
