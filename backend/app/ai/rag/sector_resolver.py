# PATH: backend/app/ai/rag/sector_resolver.py
"""
Sector Resolver
===============
Resolves an instrument's sector and its sector-peer trading symbols, for the
RAG retriever's "sector" candidate tier (see retriever.py::_load_candidates).

Two independent sector taxonomies already exist elsewhere in this codebase:
  - CompanyFundamentalsProfile.sector (app.models.fundamentals) — Upstox-
    provided free text, populated only for companies with fundamentals
    coverage.
  - sector_map.get_sector() (app.services.sector_map) — a static ~200-ticker
    dict + keyword classifier, used today only by the market scanner API.

This module is the single reconciliation point between them: it prefers the
fundamentals-table sector when present (curated provider data), falling back
to the static map otherwise. It is explicitly NOT a taxonomy migration — the
two sources are never merged or cross-validated against each other, and
sector labels from one are never compared against labels from the other.
"""
from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fundamentals import CompanyFundamentalsProfile
from app.models.upstox_data import InstrumentMaster
from app.services import sector_map

# In-process TTL cache for (sector, peers) results, mirroring the pattern in
# app.ai.intelligence.credibility_scorer (_SCORE_CACHE_TTL_SECS = 900.0).
# Sector membership changes rarely, so this avoids a DB round trip on every
# explanation-generation call for the same instrument.
_CACHE_TTL_SECS = 900.0
_cache: dict[str, tuple[str | None, frozenset[str], float]] = {}

# Reverse index built once at import: sector -> frozenset(trading_symbol).
# Backs the static-map fallback path when no fundamentals-table sector/peer
# exists for the instrument.
_STATIC_SECTOR_PEERS: dict[str, frozenset[str]] = {}
_grouped: dict[str, set[str]] = {}
for _sym, _sec in sector_map._SYMBOL_SECTOR.items():
    _grouped.setdefault(_sec, set()).add(_sym)
_STATIC_SECTOR_PEERS = {sec: frozenset(syms) for sec, syms in _grouped.items()}
del _grouped, _sym, _sec


async def _fundamentals_peers(
    db: AsyncSession, sector: str, exclude_symbol: str
) -> frozenset[str]:
    """Other active instruments sharing ``sector`` per CompanyFundamentalsProfile."""
    stmt = (
        select(InstrumentMaster.trading_symbol)
        .join(
            CompanyFundamentalsProfile,
            CompanyFundamentalsProfile.instrument_key == InstrumentMaster.instrument_key,
        )
        .where(
            CompanyFundamentalsProfile.sector == sector,
            InstrumentMaster.trading_symbol != exclude_symbol,
            InstrumentMaster.is_active.is_(True),
        )
    )
    rows = (await db.execute(stmt)).scalars().all()
    return frozenset(rows)


async def resolve_sector_and_peers(
    db: AsyncSession,
    symbol: str,
    company_name: str | None,
    instrument_key: str | None,
) -> tuple[str | None, frozenset[str]]:
    """
    Resolve an instrument's sector and its sector-peer trading symbols.

    Preference order:
      1. CompanyFundamentalsProfile.sector for ``instrument_key``, if present
         — peers are other active instruments sharing that exact sector
         value.
      2. sector_map.get_sector(symbol, company_name) fallback — peers come
         from a reverse index over sector_map's static ticker->sector dict.
      3. (None, frozenset()) if neither resolves.

    Results are cached in-process for 900s keyed by instrument_key (or
    symbol, if no instrument_key is available).
    """
    symbol = symbol.strip().upper()
    cache_key = instrument_key or symbol
    now = time.monotonic()

    cached = _cache.get(cache_key)
    if cached is not None and cached[2] > now:
        return cached[0], cached[1]

    sector: str | None = None
    peers: frozenset[str] = frozenset()

    if instrument_key:
        row = await db.execute(
            select(CompanyFundamentalsProfile.sector).where(
                CompanyFundamentalsProfile.instrument_key == instrument_key
            )
        )
        sector = row.scalar_one_or_none()
        if sector:
            peers = await _fundamentals_peers(db, sector, symbol)

    if not sector:
        sector = sector_map.get_sector(symbol, company_name)
        if sector:
            peers = _STATIC_SECTOR_PEERS.get(sector, frozenset()) - {symbol}

    _cache[cache_key] = (sector, peers, now + _CACHE_TTL_SECS)
    return sector, peers
