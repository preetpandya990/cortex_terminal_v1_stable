"""
Demand Registry — the "someone is watching" symbol set
======================================================
Maintains the set of symbols with real user demand: every instrument on any
user's watchlist plus every instrument with an active trade suggestion.
Used by forecast demand gating (WS6) to suppress background Gemini forecast
enqueues for symbols nobody is looking at — an uncapped RPD cost driver when
the correlation loop scans every actively traded instrument.

Identity forms
--------------
Callers pass whatever symbol form they hold: Pathway 1 passes instrument
keys ("NSE_EQ|INE002A01018"), Pathway 2 passes trading symbols ("RELIANCE").
The set therefore contains the UNION of all identity columns from both
source tables — membership works regardless of form, with no lossy
normalization layer to maintain.

Design
------
Lazily rebuilt Redis SET (``cortex:demand:symbols``) guarded by a freshness
key with a DEMAND_SYMBOLS_TTL_SECS TTL — no dedicated worker loop, no DB
trigger. The first membership check after expiry pays one cheap two-table
query (deliberate right-sizing per the fix plan). A concurrent rebuild race
is harmless: both writers produce the same set.

Failure policy: **fail open.** If Redis or the DB is unreachable the check
returns True — a transient infrastructure failure must degrade to the
pre-gating behavior (forecast enqueued), never to silently starving watched
symbols of forecasts.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

logger = logging.getLogger(__name__)

DEMAND_SET_KEY = "cortex:demand:symbols"
DEMAND_FRESH_KEY = "cortex:demand:symbols:fresh"

# All identity forms from both demand sources. UNION deduplicates; NULLs and
# empty strings are filtered in Python.
_DEMAND_SYMBOLS_SQL = text(
    """
    SELECT instrument_key AS a, trading_symbol AS b FROM watchlist_items
    UNION
    SELECT instrument_key AS a, trading_symbol AS b
    FROM trade_suggestions WHERE status = 'active'
    UNION
    SELECT symbol AS a, NULL AS b
    FROM trade_suggestions WHERE status = 'active'
    """
)


async def load_in_demand_symbols(db: AsyncSession) -> set[str]:
    """
    Load the in-demand symbol set from the database.

    Returns the union of every identity form (instrument_key, trading_symbol,
    suggestion symbol) across watchlist items and active trade suggestions.
    """
    result = await db.execute(_DEMAND_SYMBOLS_SQL)
    symbols: set[str] = set()
    for a, b in result.all():
        if a:
            symbols.add(a)
        if b:
            symbols.add(b)
    return symbols


async def refresh_demand_set(redis: Any, db: AsyncSession) -> set[str]:
    """
    Rebuild the Redis demand set from the DB and re-arm the freshness key.

    The set is written before the freshness key, so a reader observing the
    freshness key always sees a fully populated set. Key TTLs are padded past
    the freshness window so an expired-but-present set still serves reads
    during a rebuild race.
    """
    ttl = get_settings().DEMAND_SYMBOLS_TTL_SECS
    symbols = await load_in_demand_symbols(db)

    pipe = redis.pipeline(transaction=True)
    pipe.delete(DEMAND_SET_KEY)
    if symbols:
        pipe.sadd(DEMAND_SET_KEY, *symbols)
        # Padded expiry: the freshness key (exact TTL) drives rebuilds; the
        # set itself lingers a little longer to cover rebuild latency.
        pipe.expire(DEMAND_SET_KEY, ttl * 3)
    pipe.set(DEMAND_FRESH_KEY, "1", ex=ttl)
    await pipe.execute()

    logger.debug("demand_registry: refreshed %d in-demand symbols", len(symbols))
    return symbols


async def is_in_demand(redis: Any, db: AsyncSession, symbol: str) -> bool:
    """
    True when ``symbol`` (any identity form) has live user demand.

    Serves from the Redis set while fresh; the first call after expiry
    rebuilds it. Fails OPEN on any infrastructure error — gating must never
    starve watched symbols because Redis blinked.
    """
    if not symbol:
        return False
    try:
        if not await redis.get(DEMAND_FRESH_KEY):
            await refresh_demand_set(redis, db)
        return bool(await redis.sismember(DEMAND_SET_KEY, symbol))
    except Exception as exc:
        logger.warning(
            "demand_registry: lookup failed for %s — failing open (forecast "
            "allowed): %s",
            symbol, exc,
        )
        return True
