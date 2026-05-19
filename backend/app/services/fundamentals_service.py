"""
Cortex AI — Company Fundamentals Service
=========================================
Fetches, parses, persists, and caches Upstox v2 Company Fundamentals data.

Architecture
------------
- Dedicated httpx.AsyncClient for the Upstox v2 fundamentals API base URL.
  This is intentionally separate from the v3 UpstoxClient used for market data.
- All 8 endpoints are fetched concurrently via asyncio.gather() per company.
- Redis is the primary read path (TTL 24h). DB is the fallback. Live fetch
  is the last resort and writes to both DB and Redis on success.
- EQ guard: derivatives (futures, options, indices) return available=False
  immediately — fundamentals data does not apply to non-equity instruments.
- Percentage string values from the API (e.g. "8.94%", "+10.53%") are parsed
  to float at write time. The DB stores clean decimals, never raw strings.
- net_worth is computed at write time (total_asset - total_liability) because
  the Upstox balance-sheet response has no equity/net-worth field.
- Competitors endpoint requires URL-encoded NSE_EQ|ISIN path parameter.
  All other 7 endpoints accept bare ISIN. This is confirmed from live API
  testing: bare ISIN returns UDAPI100011 on the competitors endpoint.

Redis key schema:
  cai:fundamentals:{instrument_key}  →  JSON blob, TTL 86400s (24h)
"""
from __future__ import annotations

import asyncio
import calendar
import json
import logging
import random
import time
import urllib.parse
from datetime import date, datetime, timezone
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.fundamentals import (
    CompanyBalanceSheet,
    CompanyCashFlow,
    CompanyCompetitors,
    CompanyCorporateActions,
    CompanyFundamentalsProfile,
    CompanyIncomeStatement,
    CompanyKeyRatios,
    CompanyShareHoldings,
)
from app.models.upstox_data import InstrumentMaster

logger = logging.getLogger(__name__)
settings = get_settings()

_FUNDAMENTALS_BASE = "https://api.upstox.com/v2/fundamentals/"
_REDIS_KEY_PREFIX  = "cai:fundamentals:"
_REDIS_TTL         = 86_400  # 24 hours base TTL
_REDIS_TTL_JITTER  = 0.10    # ±10% jitter applied at write time to spread expirations
_LIVE_FETCH_LOCK_PREFIX = "cai:fundamentals:inflight:"
_LIVE_FETCH_LOCK_TTL    = 120  # seconds; covers worst-case 9-endpoint fetch time

# Module-level lazy singleton — shared across all requests, no lifespan registration needed.
_http_client: httpx.AsyncClient | None = None
_http_client_lock = asyncio.Lock()

# ── Rate limiter (token bucket / leaky bucket) ─────────────────────────────────
# Upstox hard limits: 50 req/s burst | 500 req/min | 2000 req/30min.
# The 30-min window is the binding constraint: 2000 / 1800s = 1.11 req/s max.
# We target 1.0 req/s (1800 req/30min) so we never exhaust the quota regardless
# of what previous processes consumed in the same rolling window.
#
# All callers serialise through _rate_lock; each acquires a slot spaced at
# exactly 1/_RATE_RPS seconds apart. asyncio.sleep() inside the lock yields the
# event loop so other work continues while this coroutine waits.
_RATE_RPS     = 1.0  # req/s — 1800 req/30min, safely under Upstox's 2000 cap
_rate_lock    = asyncio.Lock()
_rate_next_slot: float = 0.0  # monotonic time when the next HTTP call may start

# Retry policy for 429 Too Many Requests.
# First retry after 60s because a 429 means the rolling window is saturated —
# 2s/4s retries accomplish nothing; the window won't clear that fast.
_MAX_RETRIES_429    = 4
_RETRY_BACKOFF_BASE = 60.0  # seconds; delays are 60, 120, 240, 480 s


# ── HTTP client ────────────────────────────────────────────────────────────────

async def _get_client() -> httpx.AsyncClient:
    """Return the shared fundamentals HTTP client, initialising on first use."""
    global _http_client
    if _http_client is None:
        async with _http_client_lock:
            if _http_client is None:
                _http_client = httpx.AsyncClient(
                    base_url=_FUNDAMENTALS_BASE,
                    timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
                    limits=httpx.Limits(
                        max_connections=60,
                        max_keepalive_connections=30,
                        keepalive_expiry=30,
                    ),
                    headers={"Accept": "application/json"},
                )
                logger.info("Fundamentals HTTP client initialised (base=%s)", _FUNDAMENTALS_BASE)
    return _http_client


# ── Pure parse helpers ─────────────────────────────────────────────────────────

def extract_isin(instrument_key: str) -> str:
    """'NSE_EQ|INE002A01018' → 'INE002A01018'"""
    parts = instrument_key.split("|", 1)
    if len(parts) != 2 or not parts[1]:
        raise ValueError(f"Cannot extract ISIN from instrument_key={instrument_key!r}")
    return parts[1]


def parse_ratio_value(v: str | float | None) -> float | None:
    """'8.94%' → 8.94  |  '18.89' → 18.89  |  None → None"""
    if v is None:
        return None
    try:
        return float(str(v).rstrip("%").strip())
    except (ValueError, TypeError):
        return None


def parse_change_pct(v: str | None) -> float | None:
    """'+10.53%' → 10.53  |  '-21.09%' → -21.09  |  None → None"""
    if v is None:
        return None
    try:
        return float(str(v).replace("%", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return None


def parse_period_to_date(period: str) -> date:
    """'Mar 2026' → date(2026, 3, 31)  (last calendar day of the reported month)"""
    dt = datetime.strptime(period.strip(), "%b %Y")
    last_day = calendar.monthrange(dt.year, dt.month)[1]
    return date(dt.year, dt.month, last_day)


def _parse_action_date(s: str | None) -> date | None:
    """'15-Jun-2026' → date(2026, 6, 15)  |  None / malformed → None"""
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").date()
    except (ValueError, TypeError):
        return None


def _auth_headers() -> dict[str, str]:
    token = settings.UPSTOX_ACCESS_TOKEN
    if not token:
        raise RuntimeError("UPSTOX_ACCESS_TOKEN not configured — cannot fetch fundamentals")
    return {"Authorization": f"Bearer {token}"}


def _jittered_ttl() -> int:
    """Return _REDIS_TTL ± _REDIS_TTL_JITTER to spread cache expirations across the fleet."""
    factor = 1.0 + random.uniform(-_REDIS_TTL_JITTER, _REDIS_TTL_JITTER)
    return int(_REDIS_TTL * factor)


# ── Fetch helpers ──────────────────────────────────────────────────────────────

async def _fetch(
    path: str,
    client: httpx.AsyncClient,
    params: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """
    GET a single fundamentals endpoint with rate-limiting and retry.

    Rate control: acquires the global _rate_lock before each attempt, enforcing
    a minimum gap of 1/_RATE_RPS seconds between successive HTTP calls across
    ALL concurrent coroutines. This caps sustained throughput at 1.0 req/s =
    1800 req/30min, safely under Upstox's 2000 req/30min hard limit.

    Retry: on HTTP 429 the slot is released and the coroutine sleeps outside
    the lock (so other requests can proceed) with exponential backoff starting
    at 60s (60, 120, 240, 480s). Up to _MAX_RETRIES_429 retries per endpoint.

    Other errors are soft-logged and return None so a single failing endpoint
    never aborts the other concurrent fetches for the same instrument.
    """
    global _rate_next_slot

    for attempt in range(_MAX_RETRIES_429 + 1):
        # Rate gate: serialise calls and enforce minimum inter-request spacing.
        async with _rate_lock:
            now = time.monotonic()
            wait = _rate_next_slot - now
            if wait > 0:
                await asyncio.sleep(wait)
            _rate_next_slot = time.monotonic() + (1.0 / _RATE_RPS)

        try:
            resp = await client.get(path, headers=_auth_headers(), params=params or {})
        except httpx.TimeoutException:
            logger.error("Fundamentals request timeout: path=%s (attempt %d)", path, attempt + 1)
            return None
        except Exception as exc:
            logger.error("Fundamentals request error path=%s: %s", path, exc)
            return None

        if resp.status_code == 200:
            body = resp.json()
            if body.get("status") == "success":
                return body.get("data")
            logger.warning("Fundamentals non-success status for path=%s body=%s", path, body)
            return None

        if resp.status_code == 429:
            if attempt < _MAX_RETRIES_429:
                backoff = _RETRY_BACKOFF_BASE * (2 ** attempt)
                logger.warning(
                    "Fundamentals 429 on path=%s — backing off %.0fs (attempt %d/%d)",
                    path, backoff, attempt + 1, _MAX_RETRIES_429,
                )
                await asyncio.sleep(backoff)
                continue
            logger.error("Fundamentals 429 exhausted retries for path=%s", path)
            return None

        logger.warning(
            "Fundamentals HTTP %s for path=%s body=%.200s",
            resp.status_code, path, resp.text,
        )
        return None

    return None  # unreachable; satisfies type checker


async def _fetch_all_raw(isin: str, instrument_key: str) -> dict[str, Any]:
    """Fetch all 9 endpoints concurrently. Returns dict of raw API data (or None per endpoint)."""
    client = await _get_client()

    # Competitors endpoint requires URL-encoded NSE_EQ|ISIN path param (confirmed from live tests).
    encoded_key = urllib.parse.quote(instrument_key, safe="")

    results = await asyncio.gather(
        _fetch(f"{isin}/profile",            client),
        _fetch(f"{isin}/key-ratios",         client),
        _fetch(f"{isin}/income-statement",   client, {"type": "standalone", "time_period": "yearly"}),
        _fetch(f"{isin}/income-statement",   client, {"type": "standalone", "time_period": "quarterly"}),
        _fetch(f"{isin}/balance-sheet",      client, {"type": "standalone"}),
        _fetch(f"{isin}/cash-flow",          client, {"type": "standalone"}),
        _fetch(f"{isin}/share-holdings",     client),
        _fetch(f"{isin}/corporate-actions",  client),
        _fetch(f"{encoded_key}/competitors", client),
        return_exceptions=False,
    )

    keys = [
        "profile", "key_ratios", "income_statement", "income_statement_quarterly",
        "balance_sheet", "cash_flow", "share_holdings", "corporate_actions", "competitors",
    ]
    return dict(zip(keys, results))


# ── DB upsert helpers ──────────────────────────────────────────────────────────

async def _upsert_profile(
    instrument_key: str, isin: str, data: dict, db: AsyncSession
) -> None:
    if not data:
        return
    inr = data.get("sector_market_cap_inr") or {}
    usd = data.get("sector_market_cap_usd") or {}
    row = {
        "instrument_key":       instrument_key,
        "isin":                 isin,
        "company_profile":      data.get("company_profile"),
        "sector":               data.get("sector"),
        "sector_mcap_inr":      inr.get("value"),
        "sector_mcap_usd":      usd.get("value"),
        "sector_mcap_usd_unit": usd.get("unit"),
        "fetched_at":           datetime.now(timezone.utc),
    }
    stmt = pg_insert(CompanyFundamentalsProfile).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=["instrument_key"],
        set_={k: stmt.excluded[k] for k in row if k != "instrument_key"},
    )
    await db.execute(stmt)


async def _upsert_key_ratios(
    instrument_key: str, isin: str, data: list, db: AsyncSession
) -> None:
    if not data:
        return
    mapping = {item["name"]: item for item in data if isinstance(item, dict)}
    row = {
        "instrument_key":   instrument_key,
        "isin":             isin,
        "pe":               parse_ratio_value(mapping.get("P/E",       {}).get("company_value")),
        "pb":               parse_ratio_value(mapping.get("P/B",       {}).get("company_value")),
        "roa":              parse_ratio_value(mapping.get("ROA",       {}).get("company_value")),
        "roe":              parse_ratio_value(mapping.get("ROE",       {}).get("company_value")),
        "roce":             parse_ratio_value(mapping.get("ROCE",      {}).get("company_value")),
        "ev_ebitda":        parse_ratio_value(mapping.get("EV/EBITDA", {}).get("company_value")),
        "pe_sector":        parse_ratio_value(mapping.get("P/E",       {}).get("sector_value")),
        "pb_sector":        parse_ratio_value(mapping.get("P/B",       {}).get("sector_value")),
        "roa_sector":       parse_ratio_value(mapping.get("ROA",       {}).get("sector_value")),
        "roe_sector":       parse_ratio_value(mapping.get("ROE",       {}).get("sector_value")),
        "roce_sector":      parse_ratio_value(mapping.get("ROCE",      {}).get("sector_value")),
        "ev_ebitda_sector": parse_ratio_value(mapping.get("EV/EBITDA", {}).get("sector_value")),
        "extra_ratios":     data,
        "fetched_at":       datetime.now(timezone.utc),
    }
    stmt = pg_insert(CompanyKeyRatios).values(**row)
    stmt = stmt.on_conflict_do_update(
        index_elements=["instrument_key"],
        set_={k: stmt.excluded[k] for k in row if k != "instrument_key"},
    )
    await db.execute(stmt)


async def _upsert_income_statement(
    instrument_key: str, isin: str, data: dict, db: AsyncSession
) -> None:
    if not data:
        return
    statement_type = data.get("type", "consolidated")
    time_period    = data.get("time_period", "yearly")
    categories     = data.get("income_statement", [])

    # Pivot category-history structure into flat rows keyed by period_date.
    pivot: dict[str, dict] = {}
    cat_map = {"revenue": "revenue", "operating_profit": "op_profit", "net_profit": "net_profit"}
    for cat in categories:
        cat_name = cat.get("category", "")
        col_base = cat_map.get(cat_name)
        if not col_base:
            continue
        for entry in cat.get("history", []):
            period_label = entry.get("period", "")
            if not period_label:
                continue
            try:
                pd_key = parse_period_to_date(period_label).isoformat()
            except (ValueError, KeyError):
                continue
            if pd_key not in pivot:
                pivot[pd_key] = {"period_label": period_label}
            pivot[pd_key][col_base]               = entry.get("value")
            pivot[pd_key][f"{col_base}_change_pct"] = parse_change_pct(entry.get("change"))

    now = datetime.now(timezone.utc)
    rows = [
        {
            "instrument_key": instrument_key,
            "isin":           isin,
            "period_label":   v["period_label"],
            "period_date":    date.fromisoformat(pd_key),
            "time_period":    time_period,
            "statement_type": statement_type,
            "revenue":               v.get("revenue"),
            "revenue_change_pct":    v.get("revenue_change_pct"),
            "op_profit":             v.get("op_profit"),
            "op_profit_change_pct":  v.get("op_profit_change_pct"),
            "net_profit":            v.get("net_profit"),
            "net_profit_change_pct": v.get("net_profit_change_pct"),
            "fetched_at":            now,
        }
        for pd_key, v in pivot.items()
    ]
    if not rows:
        return
    stmt = pg_insert(CompanyIncomeStatement).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_cis_instrument_period_type",
        set_={
            "revenue":               stmt.excluded.revenue,
            "revenue_change_pct":    stmt.excluded.revenue_change_pct,
            "op_profit":             stmt.excluded.op_profit,
            "op_profit_change_pct":  stmt.excluded.op_profit_change_pct,
            "net_profit":            stmt.excluded.net_profit,
            "net_profit_change_pct": stmt.excluded.net_profit_change_pct,
            "fetched_at":            stmt.excluded.fetched_at,
        },
    )
    await db.execute(stmt)


async def _upsert_balance_sheet(
    instrument_key: str, isin: str, data: dict, db: AsyncSession
) -> None:
    if not data:
        return
    statement_type = data.get("type", "consolidated")
    now = datetime.now(timezone.utc)
    rows = []
    for entry in data.get("history", []):
        period_label = entry.get("period", "")
        if not period_label:
            continue
        try:
            period_date = parse_period_to_date(period_label)
        except (ValueError, KeyError):
            continue
        total_asset    = entry.get("total_asset")
        total_liability = entry.get("total_liability")
        net_worth = None
        if total_asset is not None and total_liability is not None:
            try:
                net_worth = float(total_asset) - float(total_liability)
            except (TypeError, ValueError):
                pass
        rows.append({
            "instrument_key":  instrument_key,
            "isin":            isin,
            "period_label":    period_label,
            "period_date":     period_date,
            "statement_type":  statement_type,
            "total_asset":     total_asset,
            "total_liability": total_liability,
            "net_worth":       net_worth,
            "fetched_at":      now,
        })
    if not rows:
        return
    stmt = pg_insert(CompanyBalanceSheet).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_cbs_instrument_period_type",
        set_={
            "total_asset":    stmt.excluded.total_asset,
            "total_liability": stmt.excluded.total_liability,
            "net_worth":      stmt.excluded.net_worth,
            "fetched_at":     stmt.excluded.fetched_at,
        },
    )
    await db.execute(stmt)


async def _upsert_cash_flow(
    instrument_key: str, isin: str, data: dict, db: AsyncSession
) -> None:
    if not data:
        return
    statement_type = data.get("type", "consolidated")
    categories     = data.get("cash_flow", [])
    pivot: dict[str, dict] = {}
    cat_map = {
        "operating":  ("operating_cf",  "operating_cf_change_pct"),
        "investing":  ("investing_cf",  "investing_cf_change_pct"),
        "financing":  ("financing_cf",  "financing_cf_change_pct"),
    }
    for cat in categories:
        cat_name = cat.get("category", "")
        cols = cat_map.get(cat_name)
        if not cols:
            continue
        val_col, pct_col = cols
        for entry in cat.get("history", []):
            period_label = entry.get("period", "")
            if not period_label:
                continue
            try:
                pd_key = parse_period_to_date(period_label).isoformat()
            except (ValueError, KeyError):
                continue
            if pd_key not in pivot:
                pivot[pd_key] = {"period_label": period_label}
            pivot[pd_key][val_col] = entry.get("value")
            pivot[pd_key][pct_col] = parse_change_pct(entry.get("change"))

    now = datetime.now(timezone.utc)
    rows = [
        {
            "instrument_key":           instrument_key,
            "isin":                     isin,
            "period_label":             v["period_label"],
            "period_date":              date.fromisoformat(pd_key),
            "statement_type":           statement_type,
            "operating_cf":             v.get("operating_cf"),
            "investing_cf":             v.get("investing_cf"),
            "financing_cf":             v.get("financing_cf"),
            "operating_cf_change_pct":  v.get("operating_cf_change_pct"),
            "investing_cf_change_pct":  v.get("investing_cf_change_pct"),
            "financing_cf_change_pct":  v.get("financing_cf_change_pct"),
            "fetched_at":               now,
        }
        for pd_key, v in pivot.items()
    ]
    if not rows:
        return
    stmt = pg_insert(CompanyCashFlow).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_ccf_instrument_period_type",
        set_={
            "operating_cf":            stmt.excluded.operating_cf,
            "investing_cf":            stmt.excluded.investing_cf,
            "financing_cf":            stmt.excluded.financing_cf,
            "operating_cf_change_pct": stmt.excluded.operating_cf_change_pct,
            "investing_cf_change_pct": stmt.excluded.investing_cf_change_pct,
            "financing_cf_change_pct": stmt.excluded.financing_cf_change_pct,
            "fetched_at":              stmt.excluded.fetched_at,
        },
    )
    await db.execute(stmt)


async def _upsert_share_holdings(
    instrument_key: str, isin: str, data: list, db: AsyncSession
) -> None:
    if not data:
        return
    cat_map = {
        "promoters":        "promoters_pct",
        "fii":              "fii_pct",
        "other_dii":        "other_dii_pct",
        "mutual_funds":     "mutual_funds_pct",
        "retail_and_other": "retail_and_other_pct",
    }
    pivot: dict[str, dict] = {}
    for cat in data:
        cat_name = cat.get("category", "")
        col = cat_map.get(cat_name)
        if not col:
            continue
        for entry in cat.get("history", []):
            period_label = entry.get("period", "")
            if not period_label:
                continue
            try:
                pd_key = parse_period_to_date(period_label).isoformat()
            except (ValueError, KeyError):
                continue
            if pd_key not in pivot:
                pivot[pd_key] = {"period_label": period_label}
            pivot[pd_key][col] = entry.get("value")

    now = datetime.now(timezone.utc)
    rows = [
        {
            "instrument_key":     instrument_key,
            "isin":               isin,
            "period_label":       v["period_label"],
            "period_date":        date.fromisoformat(pd_key),
            "promoters_pct":      v.get("promoters_pct"),
            "fii_pct":            v.get("fii_pct"),
            "other_dii_pct":      v.get("other_dii_pct"),
            "mutual_funds_pct":   v.get("mutual_funds_pct"),
            "retail_and_other_pct": v.get("retail_and_other_pct"),
            "fetched_at":         now,
        }
        for pd_key, v in pivot.items()
    ]
    if not rows:
        return
    stmt = pg_insert(CompanyShareHoldings).values(rows)
    stmt = stmt.on_conflict_do_update(
        constraint="uq_csh_instrument_period",
        set_={
            "promoters_pct":        stmt.excluded.promoters_pct,
            "fii_pct":              stmt.excluded.fii_pct,
            "other_dii_pct":        stmt.excluded.other_dii_pct,
            "mutual_funds_pct":     stmt.excluded.mutual_funds_pct,
            "retail_and_other_pct": stmt.excluded.retail_and_other_pct,
            "fetched_at":           stmt.excluded.fetched_at,
        },
    )
    await db.execute(stmt)


async def _replace_corporate_actions(
    instrument_key: str, isin: str, data: list, db: AsyncSession
) -> None:
    """Delete all existing rows for this company, then bulk-insert fresh data."""
    await db.execute(
        delete(CompanyCorporateActions).where(
            CompanyCorporateActions.instrument_key == instrument_key
        )
    )
    if not data:
        return
    now = datetime.now(timezone.utc)
    rows = [
        {
            "instrument_key":  instrument_key,
            "isin":            isin,
            "action_type":     item.get("name", ""),
            "expiry_date_str": item.get("expiry_date"),
            "expiry_date":     _parse_action_date(item.get("expiry_date")),
            "amount":          item.get("amount"),
            "ratio":           item.get("ratio"),
            "event_details":   item.get("event_details"),
            "fetched_at":      now,
        }
        for item in data
        if isinstance(item, dict)
    ]
    if rows:
        await db.execute(pg_insert(CompanyCorporateActions).values(rows))


async def _resolve_competitor_symbols(
    data: list,
    db: AsyncSession,
) -> dict[str, tuple[str | None, str | None]]:
    """Look up trading_symbol and name from instrument_master for a list of competitor items.

    Returns a dict mapping instrument_key → (trading_symbol, name).
    Keys absent from instrument_master map to (None, None).
    """
    keys = [
        item.get("instrument_key", "")
        for item in data
        if isinstance(item, dict) and item.get("instrument_key")
    ]
    if not keys:
        return {}
    result = await db.execute(
        select(
            InstrumentMaster.instrument_key,
            InstrumentMaster.trading_symbol,
            InstrumentMaster.name,
        ).where(InstrumentMaster.instrument_key.in_(keys))
    )
    return {row.instrument_key: (row.trading_symbol, row.name) for row in result.all()}


async def _replace_competitors(
    instrument_key: str,
    isin: str,
    data: list,
    symbol_map: dict[str, tuple[str | None, str | None]],
    db: AsyncSession,
) -> None:
    """Delete all existing competitor rows for this company, then bulk-insert fresh data."""
    await db.execute(
        delete(CompanyCompetitors).where(
            CompanyCompetitors.instrument_key == instrument_key
        )
    )
    if not data:
        return
    now = datetime.now(timezone.utc)
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        key = item.get("instrument_key", "")
        sym, name = symbol_map.get(key, (None, None))
        rows.append({
            "instrument_key":  instrument_key,
            "competitor_key":  key,
            "trading_symbol":  sym,
            "name":            name,
            "company_profile": item.get("company_profile"),
            "sector":          item.get("sector"),
            "mcap_inr_crore":  (item.get("sector_market_cap_inr") or {}).get("value"),
            "mcap_usd":        (item.get("sector_market_cap_usd") or {}).get("value"),
            "mcap_usd_unit":   (item.get("sector_market_cap_usd") or {}).get("unit"),
            "fetched_at":      now,
        })
    if rows:
        await db.execute(pg_insert(CompanyCompetitors).values(rows))


# ── DB assembly helpers ────────────────────────────────────────────────────────

async def _assemble_income_statement(
    instrument_key: str,
    db: AsyncSession,
    *,
    time_period: str = "yearly",
) -> tuple[list | None, datetime | None]:
    result = await db.execute(
        select(CompanyIncomeStatement)
        .where(
            CompanyIncomeStatement.instrument_key == instrument_key,
            CompanyIncomeStatement.time_period    == time_period,
            CompanyIncomeStatement.statement_type == "standalone",
        )
        .order_by(CompanyIncomeStatement.period_date)
    )
    rows = result.scalars().all()
    if not rows:
        return None, None
    fetched_at = max(r.fetched_at for r in rows)
    pivot: dict[str, dict] = {}
    for r in rows:
        pd_key = r.period_date.isoformat()
        if pd_key not in pivot:
            pivot[pd_key] = {"period_label": r.period_label, "period_date": r.period_date}
        pivot[pd_key]["revenue"]               = r.revenue
        pivot[pd_key]["revenue_change_pct"]    = r.revenue_change_pct
        pivot[pd_key]["op_profit"]             = r.op_profit
        pivot[pd_key]["op_profit_change_pct"]  = r.op_profit_change_pct
        pivot[pd_key]["net_profit"]            = r.net_profit
        pivot[pd_key]["net_profit_change_pct"] = r.net_profit_change_pct

    cat_defs = [
        ("revenue",          "revenue",    "revenue_change_pct"),
        ("operating_profit", "op_profit",  "op_profit_change_pct"),
        ("net_profit",       "net_profit", "net_profit_change_pct"),
    ]
    data = [
        {
            "category": cat_name,
            "history": [
                {
                    "value":       v.get(val_col),
                    "period":      v["period_label"],
                    "period_date": v["period_date"],
                    "change_pct":  v.get(pct_col),
                }
                for v in pivot.values()
                if v.get(val_col) is not None
            ],
        }
        for cat_name, val_col, pct_col in cat_defs
    ]
    return data, fetched_at


async def _assemble_balance_sheet(
    instrument_key: str, db: AsyncSession
) -> tuple[list | None, datetime | None]:
    result = await db.execute(
        select(CompanyBalanceSheet)
        .where(
            CompanyBalanceSheet.instrument_key == instrument_key,
            CompanyBalanceSheet.statement_type == "standalone",
        )
        .order_by(CompanyBalanceSheet.period_date)
    )
    rows = result.scalars().all()
    if not rows:
        return None, None
    fetched_at = max(r.fetched_at for r in rows)
    return [
        {
            "total_asset":     r.total_asset,
            "total_liability": r.total_liability,
            "net_worth":       r.net_worth,
            "period":          r.period_label,
            "period_date":     r.period_date,
        }
        for r in rows
    ], fetched_at


async def _assemble_cash_flow(
    instrument_key: str, db: AsyncSession
) -> tuple[list | None, datetime | None]:
    result = await db.execute(
        select(CompanyCashFlow)
        .where(
            CompanyCashFlow.instrument_key == instrument_key,
            CompanyCashFlow.statement_type == "standalone",
        )
        .order_by(CompanyCashFlow.period_date)
    )
    rows = result.scalars().all()
    if not rows:
        return None, None
    fetched_at = max(r.fetched_at for r in rows)
    pivot: dict[str, dict] = {}
    for r in rows:
        pd_key = r.period_date.isoformat()
        pivot[pd_key] = {
            "period_label":            r.period_label,
            "period_date":             r.period_date,
            "operating_cf":            r.operating_cf,
            "investing_cf":            r.investing_cf,
            "financing_cf":            r.financing_cf,
            "operating_cf_change_pct": r.operating_cf_change_pct,
            "investing_cf_change_pct": r.investing_cf_change_pct,
            "financing_cf_change_pct": r.financing_cf_change_pct,
        }
    cat_defs = [
        ("operating", "operating_cf", "operating_cf_change_pct"),
        ("investing", "investing_cf", "investing_cf_change_pct"),
        ("financing", "financing_cf", "financing_cf_change_pct"),
    ]
    data = [
        {
            "category": cat_name,
            "history": [
                {
                    "value":       v.get(val_col),
                    "period":      v["period_label"],
                    "period_date": v["period_date"],
                    "change_pct":  v.get(pct_col),
                }
                for v in pivot.values()
                if v.get(val_col) is not None
            ],
        }
        for cat_name, val_col, pct_col in cat_defs
    ]
    return data, fetched_at


async def _assemble_share_holdings(
    instrument_key: str, db: AsyncSession
) -> tuple[list | None, datetime | None]:
    result = await db.execute(
        select(CompanyShareHoldings)
        .where(CompanyShareHoldings.instrument_key == instrument_key)
        .order_by(CompanyShareHoldings.period_date)
    )
    rows = result.scalars().all()
    if not rows:
        return None, None
    fetched_at = max(r.fetched_at for r in rows)
    return [
        {
            "promoters_pct":        r.promoters_pct,
            "fii_pct":              r.fii_pct,
            "other_dii_pct":        r.other_dii_pct,
            "mutual_funds_pct":     r.mutual_funds_pct,
            "retail_and_other_pct": r.retail_and_other_pct,
            "period":               r.period_label,
            "period_date":          r.period_date,
        }
        for r in rows
    ], fetched_at


async def _assemble_corporate_actions(
    instrument_key: str, db: AsyncSession
) -> tuple[list | None, datetime | None]:
    result = await db.execute(
        select(CompanyCorporateActions)
        .where(CompanyCorporateActions.instrument_key == instrument_key)
        .order_by(CompanyCorporateActions.id.desc())
    )
    rows = result.scalars().all()
    if not rows:
        return None, None
    fetched_at = max(r.fetched_at for r in rows)
    return [
        {
            "action_type":     r.action_type,
            "expiry_date_str": r.expiry_date_str,
            "expiry_date":     r.expiry_date,
            "amount":          r.amount,
            "ratio":           r.ratio,
            "event_details":   r.event_details,
        }
        for r in rows
    ], fetched_at


async def _assemble_competitors(
    instrument_key: str, db: AsyncSession
) -> tuple[list | None, datetime | None]:
    result = await db.execute(
        select(CompanyCompetitors)
        .where(CompanyCompetitors.instrument_key == instrument_key)
    )
    rows = result.scalars().all()
    if not rows:
        return None, None
    fetched_at = max(r.fetched_at for r in rows)
    return [
        {
            "instrument_key":  r.competitor_key,
            "trading_symbol":  r.trading_symbol,
            "name":            r.name,
            "company_profile": r.company_profile,
            "sector":          r.sector,
            "mcap_inr_crore":  r.mcap_inr_crore,
            "mcap_usd":        r.mcap_usd,
            "mcap_usd_unit":   r.mcap_usd_unit,
        }
        for r in rows
    ], fetched_at


def _build_share_holdings_response(sh_raw: list) -> list | None:
    """Pivot category-based share holdings API data into per-period flat dicts."""
    if not sh_raw:
        return None
    cat_map = {
        "promoters":        "promoters_pct",
        "fii":              "fii_pct",
        "other_dii":        "other_dii_pct",
        "mutual_funds":     "mutual_funds_pct",
        "retail_and_other": "retail_and_other_pct",
    }
    pivot: dict[str, dict] = {}
    for cat in sh_raw:
        col = cat_map.get(cat.get("category", ""))
        if not col:
            continue
        for entry in cat.get("history", []):
            period_label = entry.get("period", "")
            if not period_label:
                continue
            try:
                pd = parse_period_to_date(period_label)
            except (ValueError, KeyError):
                continue
            pd_key = pd.isoformat()
            if pd_key not in pivot:
                pivot[pd_key] = {"period": period_label, "period_date": pd}
            pivot[pd_key][col] = entry.get("value")

    result = sorted(pivot.values(), key=lambda x: x["period_date"])
    return result if result else None


# ── Core public API ────────────────────────────────────────────────────────────

async def fetch_and_store_all(
    instrument_key: str,
    isin: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """
    Fetch all 8 endpoints concurrently, persist to DB, and return the assembled response dict.
    Commit is done here; caller should NOT commit after this returns.
    """
    raw = await _fetch_all_raw(isin, instrument_key)
    now = datetime.now(timezone.utc)

    # DB upserts are sequential — AsyncSession is not safe for concurrent access.
    # Running gather() over multiple execute() calls on the same session causes
    # "Method 'close()' can't be called here" state errors under high concurrency.
    await _upsert_profile(instrument_key, isin, raw.get("profile") or {}, db)
    await _upsert_key_ratios(instrument_key, isin, raw.get("key_ratios") or [], db)
    await _upsert_income_statement(instrument_key, isin, raw.get("income_statement") or {}, db)
    await _upsert_income_statement(instrument_key, isin, raw.get("income_statement_quarterly") or {}, db)
    await _upsert_balance_sheet(instrument_key, isin, raw.get("balance_sheet") or {}, db)
    await _upsert_cash_flow(instrument_key, isin, raw.get("cash_flow") or {}, db)
    comp_raw   = raw.get("competitors") or []
    symbol_map = await _resolve_competitor_symbols(comp_raw, db)

    await _upsert_share_holdings(instrument_key, isin, raw.get("share_holdings") or [], db)
    await _replace_corporate_actions(instrument_key, isin, raw.get("corporate_actions") or [], db)
    await _replace_competitors(instrument_key, isin, comp_raw, symbol_map, db)
    await db.commit()

    # Build canonical response dict from the raw API data.
    profile_raw    = raw.get("profile") or {}
    kr_raw         = raw.get("key_ratios") or []
    inr_mcap       = (profile_raw.get("sector_market_cap_inr") or {})
    usd_mcap       = (profile_raw.get("sector_market_cap_usd") or {})
    kr_mapping     = {item["name"]: item for item in kr_raw if isinstance(item, dict)}

    def _kr(name: str, field: str) -> float | None:
        return parse_ratio_value(kr_mapping.get(name, {}).get(field))

    income_raw            = raw.get("income_statement") or {}
    income_quarterly_raw  = raw.get("income_statement_quarterly") or {}
    bs_raw      = raw.get("balance_sheet") or {}
    cf_raw      = raw.get("cash_flow") or {}
    sh_raw      = raw.get("share_holdings") or []
    ca_raw      = raw.get("corporate_actions") or []
    # comp_raw and symbol_map already defined above (used by _replace_competitors)

    _IS_CAT_MAP = {
        "revenue":          "revenue",
        "operating_profit": "operating_profit",
        "net_profit":       "net_profit",
    }
    _CF_CAT_MAP = {
        "operating": "operating",
        "investing":  "investing",
        "financing":  "financing",
    }

    def _build_category_history(categories: list, cat_map: dict) -> list:
        result = []
        for cat in categories:
            cat_name = cat.get("category", "")
            display  = cat_map.get(cat_name, cat_name)
            history  = []
            for entry in cat.get("history", []):
                period_label = entry.get("period", "")
                try:
                    pd = parse_period_to_date(period_label)
                except (ValueError, KeyError):
                    continue
                history.append({
                    "value":       entry.get("value"),
                    "period":      period_label,
                    "period_date": pd,
                    "change_pct":  parse_change_pct(entry.get("change")),
                })
            result.append({"category": display, "history": history})
        return result

    income_categories           = income_raw.get("income_statement", [])            if isinstance(income_raw, dict)           else []
    income_quarterly_categories = income_quarterly_raw.get("income_statement", [])  if isinstance(income_quarterly_raw, dict) else []
    cf_categories               = cf_raw.get("cash_flow", [])                       if isinstance(cf_raw, dict)               else []

    income_yearly_list    = _build_category_history(income_categories,           _IS_CAT_MAP)
    income_quarterly_list = _build_category_history(income_quarterly_categories, _IS_CAT_MAP)

    return {
        "available":       True,
        "instrument_key":  instrument_key,
        "isin":            isin,
        "profile": {
            "company_profile":      profile_raw.get("company_profile"),
            "sector":               profile_raw.get("sector"),
            "sector_mcap_inr":      inr_mcap.get("value"),
            "sector_mcap_usd":      usd_mcap.get("value"),
            "sector_mcap_usd_unit": usd_mcap.get("unit"),
        } if profile_raw else None,
        "key_ratios": [
            {
                "name":          item.get("name"),
                "company_value": parse_ratio_value(item.get("company_value")),
                "sector_value":  parse_ratio_value(item.get("sector_value")),
            }
            for item in kr_raw
        ] if kr_raw else None,
        "income_statement": {
            "yearly":    income_yearly_list,
            "quarterly": income_quarterly_list,
        },
        "balance_sheet": [
            {
                "total_asset":     entry.get("total_asset"),
                "total_liability": entry.get("total_liability"),
                "net_worth":       (
                    float(entry["total_asset"]) - float(entry["total_liability"])
                    if entry.get("total_asset") is not None and entry.get("total_liability") is not None
                    else None
                ),
                "period":      entry.get("period", ""),
                "period_date": parse_period_to_date(entry["period"]) if entry.get("period") else None,
            }
            for entry in (bs_raw.get("history", []) if isinstance(bs_raw, dict) else [])
        ] or None,
        "cash_flow": _build_category_history(cf_categories, _CF_CAT_MAP) or None,
        "share_holdings": _build_share_holdings_response(sh_raw) or None,
        "corporate_actions": [
            {
                "action_type":     item.get("name", ""),
                "expiry_date_str": item.get("expiry_date"),
                "expiry_date":     _parse_action_date(item.get("expiry_date")),
                "amount":          item.get("amount"),
                "ratio":           item.get("ratio"),
                "event_details":   item.get("event_details"),
            }
            for item in ca_raw
        ] if ca_raw else None,
        "competitors": [
            {
                "instrument_key":  item.get("instrument_key", ""),
                "trading_symbol":  symbol_map.get(item.get("instrument_key", ""), (None, None))[0],
                "name":            symbol_map.get(item.get("instrument_key", ""), (None, None))[1],
                "company_profile": item.get("company_profile"),
                "sector":          item.get("sector"),
                "mcap_inr_crore":  (item.get("sector_market_cap_inr") or {}).get("value"),
                "mcap_usd":        (item.get("sector_market_cap_usd") or {}).get("value"),
                "mcap_usd_unit":   (item.get("sector_market_cap_usd") or {}).get("unit"),
            }
            for item in comp_raw
        ] if comp_raw else None,
        "fetched_at": now,
        "section_fetched_at": {
            "profile":           now,
            "key_ratios":        now,
            "income_statement":  now,
            "balance_sheet":     now,
            "cash_flow":         now,
            "share_holdings":    now,
            "corporate_actions": now,
            "competitors":       now,
        },
    }


# ── Targeted (single-section) fetch functions ──────────────────────────────────
# Used by the refresh scheduler to update individual data sections without
# triggering a full 9-endpoint fetch. Each function:
#   1. Fetches only its endpoint(s) from Upstox
#   2. Upserts into DB
#   3. Commits
#   4. Deletes the Redis cache key so the next read re-assembles from fresh DB data

async def _invalidate_redis_cache(instrument_key: str, redis: Any) -> None:
    """Delete the Redis cache entry for this instrument after a targeted DB update."""
    try:
        await redis.delete(_redis_key(instrument_key))
    except Exception as exc:
        logger.warning("Redis invalidation failed for %s: %s", instrument_key, exc)


async def fetch_and_store_key_ratios(
    instrument_key: str,
    isin: str,
    db: AsyncSession,
    redis: Any,
) -> None:
    """Fetch key-ratios endpoint only; upsert into DB; invalidate Redis cache."""
    client = await _get_client()
    data   = await _fetch(f"{isin}/key-ratios", client)
    await _upsert_key_ratios(instrument_key, isin, data or [], db)
    await db.commit()
    await _invalidate_redis_cache(instrument_key, redis)


async def fetch_and_store_corporate_actions(
    instrument_key: str,
    isin: str,
    db: AsyncSession,
    redis: Any,
) -> None:
    """Fetch corporate-actions endpoint only; replace in DB; invalidate Redis cache."""
    client = await _get_client()
    data   = await _fetch(f"{isin}/corporate-actions", client)
    await _replace_corporate_actions(instrument_key, isin, data or [], db)
    await db.commit()
    await _invalidate_redis_cache(instrument_key, redis)


async def fetch_and_store_financials(
    instrument_key: str,
    isin: str,
    db: AsyncSession,
    redis: Any,
) -> None:
    """
    Fetch income-statement (yearly + quarterly), balance-sheet, and cash-flow.
    4 concurrent Upstox requests; upserts are sequential (single AsyncSession).
    """
    client = await _get_client()

    is_yearly, is_quarterly, bs_data, cf_data = await asyncio.gather(
        _fetch(f"{isin}/income-statement", client, {"type": "standalone", "time_period": "yearly"}),
        _fetch(f"{isin}/income-statement", client, {"type": "standalone", "time_period": "quarterly"}),
        _fetch(f"{isin}/balance-sheet",    client, {"type": "standalone"}),
        _fetch(f"{isin}/cash-flow",        client, {"type": "standalone"}),
    )

    await _upsert_income_statement(instrument_key, isin, is_yearly   or {}, db)
    await _upsert_income_statement(instrument_key, isin, is_quarterly or {}, db)
    await _upsert_balance_sheet(instrument_key, isin, bs_data or {}, db)
    await _upsert_cash_flow(instrument_key, isin, cf_data or {}, db)
    await db.commit()
    await _invalidate_redis_cache(instrument_key, redis)


async def fetch_and_store_share_holdings(
    instrument_key: str,
    isin: str,
    db: AsyncSession,
    redis: Any,
) -> None:
    """Fetch share-holdings endpoint only; upsert into DB; invalidate Redis cache."""
    client = await _get_client()
    data   = await _fetch(f"{isin}/share-holdings", client)
    await _upsert_share_holdings(instrument_key, isin, data or [], db)
    await db.commit()
    await _invalidate_redis_cache(instrument_key, redis)


async def fetch_and_store_profile_and_competitors(
    instrument_key: str,
    isin: str,
    db: AsyncSession,
    redis: Any,
) -> None:
    """Fetch profile + competitors; upsert into DB; invalidate Redis cache."""
    client      = await _get_client()
    encoded_key = urllib.parse.quote(instrument_key, safe="")

    profile_data, comp_data = await asyncio.gather(
        _fetch(f"{isin}/profile",            client),
        _fetch(f"{encoded_key}/competitors", client),
    )

    await _upsert_profile(instrument_key, isin, profile_data or {}, db)
    comp_list  = comp_data or []
    symbol_map = await _resolve_competitor_symbols(comp_list, db)
    await _replace_competitors(instrument_key, isin, comp_list, symbol_map, db)
    await db.commit()
    await _invalidate_redis_cache(instrument_key, redis)


async def assemble_from_db(instrument_key: str, db: AsyncSession) -> dict[str, Any] | None:
    """
    Assemble the full response dict from DB tables. Returns None if the profile row is absent
    (meaning this company has never been fetched — caller should trigger a live fetch).
    """
    profile_result = await db.execute(
        select(CompanyFundamentalsProfile)
        .where(CompanyFundamentalsProfile.instrument_key == instrument_key)
    )
    profile = profile_result.scalar_one_or_none()
    if profile is None:
        return None

    kr_result = await db.execute(
        select(CompanyKeyRatios)
        .where(CompanyKeyRatios.instrument_key == instrument_key)
    )
    kr = kr_result.scalar_one_or_none()

    # Sequential reads — same AsyncSession concurrency constraint as writes.
    income_yearly,    income_yearly_fat    = await _assemble_income_statement(instrument_key, db, time_period="yearly")
    income_quarterly, income_quarterly_fat = await _assemble_income_statement(instrument_key, db, time_period="quarterly")
    balance,  balance_fat = await _assemble_balance_sheet(instrument_key, db)
    cashflow, cf_fat      = await _assemble_cash_flow(instrument_key, db)
    holdings, sh_fat      = await _assemble_share_holdings(instrument_key, db)
    actions,  ca_fat      = await _assemble_corporate_actions(instrument_key, db)
    comps,    comp_fat    = await _assemble_competitors(instrument_key, db)

    # income_statement is always returned as a structured container.
    # Consumers show "data not yet available" when both lists are empty.
    income_fat = max(
        (f for f in (income_yearly_fat, income_quarterly_fat) if f is not None),
        default=None,
    )
    income_data = {
        "yearly":    income_yearly    or [],
        "quarterly": income_quarterly or [],
    }

    key_ratios = None
    if kr:
        kr_map = {
            "P/E":       (kr.pe,        kr.pe_sector),
            "P/B":       (kr.pb,        kr.pb_sector),
            "ROA":       (kr.roa,       kr.roa_sector),
            "ROE":       (kr.roe,       kr.roe_sector),
            "ROCE":      (kr.roce,      kr.roce_sector),
            "EV/EBITDA": (kr.ev_ebitda, kr.ev_ebitda_sector),
        }
        key_ratios = [
            {"name": k, "company_value": cv, "sector_value": sv}
            for k, (cv, sv) in kr_map.items()
        ]

    return {
        "available":      True,
        "instrument_key": instrument_key,
        "isin":           profile.isin,
        "profile": {
            "company_profile":      profile.company_profile,
            "sector":               profile.sector,
            "sector_mcap_inr":      float(profile.sector_mcap_inr) if profile.sector_mcap_inr else None,
            "sector_mcap_usd":      float(profile.sector_mcap_usd) if profile.sector_mcap_usd else None,
            "sector_mcap_usd_unit": profile.sector_mcap_usd_unit,
        },
        "key_ratios":        key_ratios,
        "income_statement":  income_data,
        "balance_sheet":     balance,
        "cash_flow":         cashflow,
        "share_holdings":    holdings,
        "corporate_actions": actions,
        "competitors":       comps,
        "fetched_at":        profile.fetched_at,
        "section_fetched_at": {
            "profile":           profile.fetched_at,
            "key_ratios":        kr.fetched_at if kr else None,
            "income_statement":  income_fat,
            "balance_sheet":     balance_fat,
            "cash_flow":         cf_fat,
            "share_holdings":    sh_fat,
            "corporate_actions": ca_fat,
            "competitors":       comp_fat,
        },
    }


def _redis_key(instrument_key: str) -> str:
    return f"{_REDIS_KEY_PREFIX}{instrument_key}"


def _serialize(data: dict) -> str:
    return json.dumps(data, default=str)


def _deserialize(raw: str) -> dict:
    return json.loads(raw)


async def get_fundamentals(
    instrument_key: str,
    db: AsyncSession,
    redis: Any,
) -> dict[str, Any]:
    """
    Primary read path:
      1. Redis cache hit → return immediately (fast path, ~1ms)
      2. DB populated   → assemble, write to Redis with jitter, return
      3. Neither        → acquire NX distributed lock, live fetch, persist, return

    TTL jitter (±10%) prevents a cache stampede when many instruments were
    fetched at the same time (e.g. after a backfill run) and their TTLs expire
    simultaneously.

    The NX lock on path 3 ensures that if many requests arrive for the same
    instrument simultaneously (thundering herd), only one coroutine executes
    the 9-endpoint live fetch; the others wait and then hit the DB/cache that
    the winner just populated.

    Non-EQ instruments are rejected before this function is called (see API layer).
    """
    rkey = _redis_key(instrument_key)

    # 1. Redis
    try:
        cached = await redis.get(rkey)
        if cached:
            return _deserialize(cached)
    except Exception as exc:
        logger.warning("Redis read failed for %s: %s", instrument_key, exc)

    # 2. DB
    try:
        db_data = await assemble_from_db(instrument_key, db)
        if db_data:
            try:
                await redis.set(rkey, _serialize(db_data), ex=_jittered_ttl())
            except Exception as exc:
                logger.warning("Redis write failed for %s: %s", instrument_key, exc)
            return db_data
    except Exception as exc:
        logger.error("DB assembly failed for %s: %s", instrument_key, exc)

    # 3. Live fetch — guarded by a Redis NX lock to prevent thundering herd.
    #    Multiple coroutines racing to path 3 for the same instrument_key will
    #    serialise: the first acquires the lock, fetches and caches; the rest
    #    poll at 1-second intervals until the lock releases, then hit path 1/2.
    try:
        isin = extract_isin(instrument_key)
    except ValueError as exc:
        return {"available": False, "instrument_key": instrument_key, "isin": None, "reason": str(exc)}

    lock_key = f"{_LIVE_FETCH_LOCK_PREFIX}{instrument_key}"

    # Try to acquire the NX lock (SET ... NX EX).
    try:
        acquired = await redis._redis.set(lock_key, "1", nx=True, ex=_LIVE_FETCH_LOCK_TTL)
    except Exception:
        acquired = True  # Redis unavailable — proceed without lock (safe degradation)

    if not acquired:
        # Another coroutine holds the lock. Poll until it releases or TTL expires,
        # then re-check Redis/DB before considering a fresh fetch.
        logger.debug("Live fetch lock contention for %s — waiting for peer", instrument_key)
        for _ in range(_LIVE_FETCH_LOCK_TTL):
            await asyncio.sleep(1.0)
            try:
                still_locked = await redis._redis.exists(lock_key)
            except Exception:
                still_locked = False
            if not still_locked:
                break

        # Re-check Redis and DB — the lock holder should have populated both.
        try:
            cached = await redis.get(rkey)
            if cached:
                return _deserialize(cached)
        except Exception:
            pass
        try:
            db_data = await assemble_from_db(instrument_key, db)
            if db_data:
                return db_data
        except Exception:
            pass
        # Fallthrough: lock holder may have failed — attempt our own fetch.

    try:
        live_data = await fetch_and_store_all(instrument_key, isin, db)
        try:
            await redis.set(rkey, _serialize(live_data), ex=_jittered_ttl())
        except Exception as exc:
            logger.warning("Redis write failed for %s: %s", instrument_key, exc)
        return live_data
    except Exception as exc:
        logger.error("Live fundamentals fetch failed for %s: %s", instrument_key, exc)
        return {
            "available":      False,
            "instrument_key": instrument_key,
            "isin":           None,
            "reason":         "fetch_failed",
        }
    finally:
        # Release the lock only if we were the one that acquired it.
        if acquired:
            try:
                await redis._redis.delete(lock_key)
            except Exception:
                pass


async def is_eq_instrument(instrument_key: str, db: AsyncSession) -> bool:
    """Returns True iff the instrument is of type EQ. Derivatives return False."""
    result = await db.execute(
        select(InstrumentMaster.instrument_type)
        .where(InstrumentMaster.instrument_key == instrument_key)
    )
    row = result.scalar_one_or_none()
    return row == "EQ"
