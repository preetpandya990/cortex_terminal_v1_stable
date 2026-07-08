"""
Forecast cache key — shared builder
===================================
The news-forecast result cache is written by two producers (the background
batch worker via signal_assembler's enqueue payload, and the WS7 demand
explanation path) and read on the consensus hot path. All of them MUST derive
the key through this one function: a reimplementation that hashed even
slightly differently would produce non-colliding keys and silently never hit.

Key shape: ``cortex:news_forecast:{symbol}:{sha256(symbol+event_ids+rounded_indicators)[:16]}``
— a new event or a materially changed indicator value busts the cache.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

FORECAST_CACHE_PREFIX = "cortex:news_forecast"
# Writers take the result TTL from settings.NEWS_FORECAST_CACHE_TTL — one
# freshness window for every producer; do not hardcode a TTL here.


def forecast_cache_key(
    symbol: str,
    events: list[dict],
    indicators: dict[str, Any],
) -> str:
    """Stable key over symbol + news identity + rounded numeric indicators."""
    ev_ids = sorted(
        str(e.get("id") or e.get("article_title") or e.get("type") or "")
        for e in events
    )
    ind = {
        k: round(float(v), 4)
        for k, v in indicators.items()
        if isinstance(v, (int, float))
    }
    basis = json.dumps({"s": symbol, "e": ev_ids, "i": ind}, sort_keys=True, default=str)
    digest = hashlib.sha256(basis.encode()).hexdigest()[:16]
    return f"{FORECAST_CACHE_PREFIX}:{symbol}:{digest}"
