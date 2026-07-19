"""
Paper Trading — Portfolio-Insight ML-Param Cache
=================================================
Single source of truth for the small, slow-changing ML inputs that the live
``P(hit TP before SL)`` metric (see ``hit_probability.py``) needs on the P&L
recompute hot loop — cached in Redis so the hot loop never runs ML inference.

The cache is written by the worker-sidecar refresher (``InsightMLParamsRefresher``,
workstream B2) and read by the P&L worker frame enrichment (workstream B3).
This module owns the **contract** shared across all producers/consumers:

  • the Redis key layout,                     ``mlparams_key``
  • the on-demand refresh channel + publisher, ``request_refresh``
  • the value serialization (read/write),      ``write_mlparams`` / ``read_mlparams``
  • the freshness predicate for the sweep,     ``needs_refresh``
  • the 3-class → scalar ``prob_up`` collapse,  ``derive_prob_up``

Keeping these here (rather than duplicated at each call site) guarantees the
key format and payload schema can never drift between the writer and the reader.

``prob_up`` derivation
----------------------
The ensemble emits three calibrated class probabilities ``{sell, hold, buy}``.
The barrier model needs a single scalar ``prob_up ∈ [0, 1]``.  We collapse the
classes with their **ordinal expected value** — scoring DOWN=0, HOLD=0.5, UP=1:

    prob_up = (P(buy) + 0.5·P(hold)) / (P(buy) + P(hold) + P(sell))

This treats HOLD as genuinely directionless (it contributes exactly 0.5), is
neutral (0.5) whenever ``P(buy) ≈ P(sell)`` regardless of how much mass HOLD
holds, is robust when the model is undecided, and reduces to ``P(up)`` for a
binary model with no HOLD class.

Value schema (JSON at ``cai:paper:insight:mlparams:{instrument_key}``)
----------------------------------------------------------------------
    {"prob_up": float, "sigma": float, "ts": float}

  prob_up : ordinal-expected-value up-probability in [0, 1] (drift edge input).
  sigma   : annualised historical volatility from the same snapshot (cached for
            the insight stats service / regime display; the barrier drift itself
            is parameterised by prob_up + edge_lambda, not sigma).
  ts      : Unix epoch (seconds) the params were computed — observability only;
            staleness is enforced by the key's Redis TTL, not by this field.

Only **available** snapshots are ever written — a degraded/unavailable snapshot
leaves the key absent, so B3 sees "no params", falls back to the neutral
(zero-edge) estimate, and flags the value stale.  We never cache a fabricated
number.
"""
from __future__ import annotations

import json
import logging
import math
import time
from typing import Any, Mapping

from app.core.config import get_settings
from app.core.redis import RedisChannels

logger = logging.getLogger(__name__)

# ── Redis key layout ────────────────────────────────────────────────────────────
MLPARAMS_KEY_PREFIX = "cai:paper:insight:mlparams:"


def mlparams_key(instrument_key: str) -> str:
    """Redis key holding the cached ML params for one instrument."""
    return f"{MLPARAMS_KEY_PREFIX}{instrument_key}"


# ── prob_up derivation (pure) ─────────────────────────────────────────────────────

def derive_prob_up(probabilities: Mapping[str, Any] | None) -> float | None:
    """
    Collapse calibrated ``{sell, hold, buy}`` class probabilities into a single
    ordinal-expected-value ``prob_up ∈ [0, 1]`` (see module docstring).

    Returns ``None`` when the required classes are missing, non-numeric, or
    non-finite, or when the total mass is non-positive — the caller then treats
    the instrument as unscored rather than caching a meaningless value.
    """
    if not probabilities:
        return None
    try:
        buy = float(probabilities["buy"])
        sell = float(probabilities["sell"])
        hold = float(probabilities.get("hold", 0.0))
    except (KeyError, TypeError, ValueError):
        return None

    if not (math.isfinite(buy) and math.isfinite(sell) and math.isfinite(hold)):
        return None

    # Clamp calibration dust (a class can round marginally below 0).
    buy = max(0.0, buy)
    sell = max(0.0, sell)
    hold = max(0.0, hold)

    total = buy + hold + sell
    if total <= 0.0:
        return None

    p = (buy + 0.5 * hold) / total
    return min(1.0, max(0.0, p))


# ── Value read / write ────────────────────────────────────────────────────────────

async def write_mlparams(
    redis: Any,
    instrument_key: str,
    *,
    prob_up: float,
    sigma: float,
    ttl_seconds: int,
    ts: float | None = None,
) -> None:
    """
    Persist an instrument's ML params with a TTL.  The TTL is the sole staleness
    boundary: once it lapses, B3 sees no key and degrades to the neutral estimate.
    """
    payload = json.dumps(
        {
            "prob_up": prob_up,
            "sigma": sigma,
            "ts": time.time() if ts is None else ts,
        }
    )
    await redis.set(mlparams_key(instrument_key), payload, ex=ttl_seconds)


async def read_mlparams(redis: Any, instrument_key: str) -> dict[str, Any] | None:
    """
    Fetch an instrument's cached ML params, or ``None`` if absent/corrupt.

    A corrupt payload is treated exactly like a cache miss so a single bad write
    can never crash the reader (B3's hot loop).
    """
    raw = await redis.get(mlparams_key(instrument_key))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "prob_up" not in data:
        return None
    return data


async def needs_refresh(
    redis: Any,
    instrument_key: str,
    *,
    margin_seconds: int,
) -> bool:
    """
    True when an instrument's cached params are missing or expiring soon.

    Uses the key's remaining Redis TTL so the sweep re-scores an instrument
    *before* its params lapse (no window where B3 sees a stale-then-absent key).
    ``-2`` (missing) and ``-1`` (present but no expiry — should never happen for
    our TTL'd keys, but treated as needing a fresh, correctly-expiring write)
    both mean "refresh".
    """
    ttl = await redis.ttl(mlparams_key(instrument_key))
    if ttl is None or ttl < 0:
        return True
    return ttl < margin_seconds


# ── On-demand refresh request (producer side) ──────────────────────────────────────

async def request_refresh(redis: Any, instrument_key: str) -> None:
    """
    Ask the sidecar refresher to score ``instrument_key`` immediately.

    Best-effort and always non-fatal — the periodic sweep is the safety net, so
    a publish failure here only delays warm-up, it never breaks the caller
    (e.g. the order-placement request path).  No-ops when the feature is
    disabled so callers need no feature-flag branch of their own.
    """
    if not get_settings().INSIGHT_ENABLED:
        return
    if not instrument_key:
        return
    try:
        await redis.publish(
            RedisChannels.PAPER_INSIGHT_REFRESH_REQUEST,
            json.dumps({"instrument_key": instrument_key}),
        )
    except Exception as exc:  # noqa: BLE001 — never propagate into the caller
        logger.warning(
            "insight_cache: refresh publish failed for %s: %s", instrument_key, exc
        )
