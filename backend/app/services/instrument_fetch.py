"""
Cortex AI — Instrument file fetcher.
====================================
Downloads and parses Upstox's daily begin-of-day (BOD) NSE instrument file
(``NSE.json.gz``) into a normalized, filtered instrument list.

Why this module exists separately from the sync/reconcile logic
---------------------------------------------------------------
Acquisition (network + decompression + parsing) is the I/O-bound, failure-prone
half of an instrument sync; reconciliation (``data_ingestion``) is the
transactional, database half.  Keeping them apart makes each independently
testable and lets the scheduler short-circuit cheaply when nothing changed.

Conditional GET
---------------
Upstox regenerates the file once per trading day (~06:00 IST) and serves it as a
static CDN object with strong validators (``ETag`` / ``Last-Modified``).  We send
the previously seen validators on every request; the CDN answers ``304 Not
Modified`` when the file is unchanged, so the common case (most scheduler ticks)
does zero parsing and zero database work.

Crucially, this module only *reads* the cached validators to build the request.
Persisting the *new* validators is deferred to the caller via
``persist_validators`` and must happen only after a fully successful sync — so a
file that later fails the downstream sanity guard is never cached behind a 304
and is re-fetched on the next run.

Compression
-----------
The ``.gz`` suffix is part of the CDN object, not an HTTP ``Content-Encoding``,
so httpx does not transparently inflate it — we decompress explicitly.  Both the
compressed download and the decompressed output are size-capped: the download
cap bounds network/memory, and the capped decompression refuses to inflate past
a ceiling (defence against a malformed or pathologically compressed payload)
instead of allocating unbounded memory.
"""
from __future__ import annotations

import io
import json
import logging
import zlib
from dataclasses import dataclass, field
from typing import Any

import httpx
import ijson
from redis.asyncio import Redis

from app.core.config import get_settings
from app.exceptions import UpstoxAPIError

logger = logging.getLogger(__name__)
settings = get_settings()

# ── NSE cash-equity series taxonomy ────────────────────────────────────────────
# These constants are the canonical definitions for the NSE tradeable universe.
# Any change here must be mirrored in:
#   symbol_validator._ELIGIBLE_INSTRUMENT_TYPES
#   schemas.trade_suggestions._RESTRICTED_NSE_SERIES
#
# Series included in the active trading universe:
#   EQ  — normal continuous market, T+1 settlement
#   BE  — trade-to-trade / surveillance, delivery-only
#   BZ  — regulatory-action / penalty series (active SEBI/NSE order), delivery-only
#   SM  — SME (Small & Medium Enterprise) main board
#   ST  — SME trade-to-trade, delivery compulsory
#
# Series requiring a platform risk disclaimer before trading (BZ/SM/ST):
#   delivery-only restrictions, regulatory or SME-specific constraints mean these
#   instruments carry elevated risk not present in standard EQ/BE instruments.

_ALLOWED_SEGMENT = "NSE_EQ"
_ALLOWED_TYPES = frozenset({"EQ", "BE", "BZ", "SM", "ST"})

# ── Conditional-GET cache ───────────────────────────────────────────────────────
# Holds the validators of the last successfully *synced* file. TTL is generous;
# the entry is refreshed on every successful sync, so it never expires in steady
# state and simply self-heals if Redis is flushed (next run re-fetches).
_HTTP_CACHE_KEY = "instrument_sync:http_cache"
_HTTP_CACHE_TTL_SECONDS = 7 * 24 * 3600

# ── Resource bounds ─────────────────────────────────────────────────────────────
# The real NSE_EQ file is a few MiB compressed / tens of MiB inflated; these caps
# are generous headroom whose purpose is to fail fast on an anomalous payload
# rather than to right-size the common case.
_MAX_COMPRESSED_BYTES = 64 * 1024 * 1024      # 64 MiB raw download ceiling
_MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024   # 512 MiB inflate ceiling
_DOWNLOAD_CHUNK_BYTES = 1 << 20               # 1 MiB streaming read granularity

_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


@dataclass(slots=True)
class FetchResult:
    """Outcome of a fetch attempt.

    ``not_modified`` is True when the CDN answered 304; in that case
    ``instruments`` is empty and the caller should skip the sync entirely.
    On a 200, ``instruments`` holds the normalized/filtered rows and ``etag`` /
    ``last_modified`` carry the validators to persist *after* a successful sync.
    """

    not_modified: bool
    instruments: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None


def _normalize_instrument(item: dict[str, Any]) -> dict[str, Any] | None:
    """Project a raw Upstox record to our schema, or None if it is out of scope."""
    if item.get("segment") != _ALLOWED_SEGMENT:
        return None
    if item.get("instrument_type") not in _ALLOWED_TYPES:
        return None

    instrument_key = item.get("instrument_key")
    trading_symbol = item.get("trading_symbol")
    if not instrument_key or not trading_symbol:
        return None

    return {
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "name": item.get("name") or "",
        "exchange": item.get("exchange") or "NSE",
        "instrument_type": item.get("instrument_type"),
        "isin": item.get("isin"),
    }


def _decompress_capped(raw: bytes) -> bytes:
    """Gunzip ``raw`` with a hard output ceiling.

    Uses ``decompressobj`` with a bounded ``max_length`` so a malformed or
    pathologically compressed payload is rejected *before* it is fully inflated,
    rather than risking an unbounded allocation.
    """
    decompressor = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)  # 16 => gzip header
    # max_length caps produced output; if input remains (unconsumed_tail) the
    # stream would inflate past the ceiling, so we reject it.
    out = decompressor.decompress(raw, _MAX_DECOMPRESSED_BYTES + 1)
    if decompressor.unconsumed_tail or len(out) > _MAX_DECOMPRESSED_BYTES:
        raise UpstoxAPIError(
            f"instrument file exceeded decompressed ceiling of {_MAX_DECOMPRESSED_BYTES} bytes"
        )
    out += decompressor.flush()
    return out


def _parse_and_filter(decompressed: bytes) -> list[dict[str, Any]]:
    """Incrementally parse the JSON array and project to the in-scope universe."""
    instruments: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in ijson.items(io.BytesIO(decompressed), "item"):
        if not isinstance(item, dict):
            continue
        normalized = _normalize_instrument(item)
        if normalized is None:
            continue
        # Guard against duplicate instrument_keys in the source — the DB upsert
        # would otherwise raise "ON CONFLICT DO UPDATE command cannot affect row
        # a second time" when a batch contains the same key twice.
        key = normalized["instrument_key"]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        instruments.append(normalized)
    return instruments


async def _read_validators(redis: Redis) -> tuple[str | None, str | None]:
    """Return (etag, last_modified) from the conditional-GET cache, if present."""
    try:
        cached = await redis.get(_HTTP_CACHE_KEY)
    except Exception as exc:  # Redis is a soft dependency here — degrade to full fetch
        logger.warning("Instrument HTTP cache read failed (%s); fetching unconditionally", exc)
        return None, None
    if not cached:
        return None, None
    try:
        payload = json.loads(cached)
        return payload.get("etag"), payload.get("last_modified")
    except (ValueError, AttributeError):
        return None, None


async def persist_validators(redis: Redis, etag: str | None, last_modified: str | None) -> None:
    """Persist response validators so the next fetch can issue a conditional GET.

    Call this only after a fully successful sync.  No-op when the response
    carried no validators (nothing to condition on next time).
    """
    if not etag and not last_modified:
        return
    payload = json.dumps({"etag": etag, "last_modified": last_modified})
    try:
        await redis.set(_HTTP_CACHE_KEY, payload, ex=_HTTP_CACHE_TTL_SECONDS)
    except Exception as exc:  # Non-fatal: worst case is a full fetch next run
        logger.warning("Instrument HTTP cache write failed (%s); next run will re-fetch", exc)


async def _download(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> tuple[int, bytes, httpx.Headers]:
    """Stream the response body, enforcing the compressed-size ceiling."""
    async with client.stream("GET", url, headers=headers) as resp:
        if resp.status_code == httpx.codes.NOT_MODIFIED:
            return resp.status_code, b"", resp.headers
        resp.raise_for_status()
        buf = bytearray()
        async for chunk in resp.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
            buf.extend(chunk)
            if len(buf) > _MAX_COMPRESSED_BYTES:
                raise UpstoxAPIError(
                    f"instrument file exceeded download ceiling of {_MAX_COMPRESSED_BYTES} bytes"
                )
        return resp.status_code, bytes(buf), resp.headers


async def fetch_instruments(redis: Redis, *, force: bool = False) -> FetchResult:
    """Fetch, decompress, parse, and filter the Upstox instrument file.

    Args:
        redis: client used to read the conditional-GET validators.
        force: when True, skip the conditional GET and always download in full
            (operator escape hatch for ``scripts/sync_instruments.py --force``).

    Returns:
        ``FetchResult`` — ``not_modified=True`` when the CDN answered 304, else
        the parsed instrument list plus the new validators to persist on success.

    Raises:
        UpstoxAPIError: on any network, decompression, or parse failure, or when
            the file parses to zero in-scope instruments.
    """
    url = str(settings.UPSTOX_INSTRUMENTS_URL)

    headers: dict[str, str] = {}
    if not force:
        etag, last_modified = await _read_validators(redis)
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            status, raw, resp_headers = await _download(client, url, headers)
    except httpx.HTTPError as exc:
        raise UpstoxAPIError(f"instrument file download failed: {exc}") from exc

    if status == httpx.codes.NOT_MODIFIED:
        logger.info("Instrument file unchanged (HTTP 304); skipping parse")
        return FetchResult(not_modified=True)

    decompressed = _decompress_capped(raw)
    instruments = _parse_and_filter(decompressed)

    if not instruments:
        # A zero-instrument parse means a corrupt/empty file or an upstream
        # schema change — never let it propagate to reconciliation.
        raise UpstoxAPIError("instrument file parsed to zero in-scope instruments")

    logger.info(
        "Instrument file fetched: %d in-scope instruments (%.1f MiB compressed)",
        len(instruments),
        len(raw) / (1024 * 1024),
    )
    return FetchResult(
        not_modified=False,
        instruments=instruments,
        etag=resp_headers.get("ETag"),
        last_modified=resp_headers.get("Last-Modified"),
    )
