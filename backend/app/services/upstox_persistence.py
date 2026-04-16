"""
Cortex AI — Upstox Persistence Service
=========================================
Fixes:
  - Instrument search uses ilike filter — no hard limit(1000) full-table fetch
  - Pagination via cursor (keyset) rather than offset
  - All queries fully async
"""
from __future__ import annotations

import logging

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionFactory
from app.exceptions import DatabaseError
from app.models.upstox_data import InstrumentMaster
from app.schemas.market_data import InstrumentSearchResult

logger = logging.getLogger(__name__)


async def search_instruments_db(
    q: str,
    limit: int = 20,
) -> list[InstrumentSearchResult]:
    """
    Search instrument master by trading symbol or name using ilike.
    Opens its own session (called from router without session param).
    Applies the filter server-side — never fetches the full table.
    """
    pattern = f"%{q.upper()}%"
    async with AsyncSessionFactory() as session:
        try:
            stmt = (
                select(InstrumentMaster)
                .where(
                    InstrumentMaster.symbol.ilike(pattern)
                    | InstrumentMaster.name.ilike(pattern)
                )
                .order_by(InstrumentMaster.symbol)
                .limit(limit)
            )
            rows = (await session.execute(stmt)).scalars().all()
        except Exception as exc:
            raise DatabaseError(f"Instrument search failed: {exc}") from exc

    return [
        InstrumentSearchResult(
            instrument_key=r.instrument_key,
            trading_symbol=r.symbol,
            name=r.name or "",
            exchange=r.exchange_segment,
            instrument_type="EQ",  # Default since not in DB
        )
        for r in rows
    ]


async def get_instruments_paginated(
    session: AsyncSession,
    cursor: str | None = None,
    page_size: int = 100,
) -> tuple[list[InstrumentMaster], str | None]:
    """
    Keyset (cursor-based) pagination over the instrument master.
    Avoids the OFFSET performance cliff for large result sets.

    Returns (instruments, next_cursor). next_cursor is None on last page.
    """
    stmt = select(InstrumentMaster).order_by(InstrumentMaster.instrument_key)

    if cursor:
        stmt = stmt.where(InstrumentMaster.instrument_key > cursor)

    stmt = stmt.limit(page_size + 1)  # fetch one extra to detect next page

    try:
        rows = (await session.execute(stmt)).scalars().all()
    except Exception as exc:
        raise DatabaseError(f"Instrument pagination failed: {exc}") from exc

    has_next = len(rows) > page_size
    page = rows[:page_size]
    next_cursor = page[-1].instrument_key if has_next and page else None

    return page, next_cursor