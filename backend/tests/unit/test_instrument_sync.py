"""
Tests for the instrument-master sync pipeline.

Coverage
--------
Fetch (app.services.instrument_fetch):
  - filtering + de-duplication of the raw Upstox payload
  - HTTP 304 conditional-GET short-circuit + conditional request headers
  - zero-instrument and decompressed-size-cap guards

Reconcile (app.services.data_ingestion.sync_instrument_master):
  - insert / update / soft-delete (delist) / reactivate semantics
  - exact, arithmetically-consistent counts
  - idempotency, dry-run (no writes), and the sanity guard (rollback)

Concurrency (app.services.instrument_sync_service):
  - the advisory lock admits exactly one holder at a time

Consumers (app.services.symbol_validator):
  - the eligibility gate excludes delisted instruments
  - resolvers default to active-only and opt in via include_inactive

Reconcile/consumer tests run inside the rolled-back ``db_session`` transaction
and start from a clean ``instrument_master`` (the truncate is part of the
transaction and never touches real data).
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, select

from app.exceptions import SyncSanityError, UpstoxAPIError
from app.models.upstox_data import InstrumentMaster
from app.services import instrument_fetch
from app.services.data_ingestion import sync_instrument_master


# ── Helpers ──────────────────────────────────────────────────────────────────────
def _gz(payload) -> bytes:
    """Gzip-compress a JSON-serializable payload (mimics NSE.json.gz)."""
    return gzip.compress(json.dumps(payload).encode())


def _raw(instrument_key: str, trading_symbol: str, *, segment="NSE_EQ", itype="EQ", name="ACME") -> dict:
    return {
        "segment": segment,
        "instrument_type": itype,
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "name": name,
        "exchange": "NSE",
    }


def _norm(instrument_key: str, trading_symbol: str, *, name="ACME", itype="EQ") -> dict:
    """A normalized instrument dict as produced by the fetcher / consumed by the reconciler."""
    return {
        "instrument_key": instrument_key,
        "trading_symbol": trading_symbol,
        "name": name,
        "exchange": "NSE",
        "instrument_type": itype,
    }


class _FakeRedis:
    """Minimal async Redis stand-in for conditional-GET validator storage."""

    def __init__(self, initial: str | None = None) -> None:
        self.store: dict[str, str] = {}
        if initial is not None:
            self.store[instrument_fetch._HTTP_CACHE_KEY] = initial

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


def _patch_download(monkeypatch, *, status=200, payload=None, etag=None, last_modified=None, capture=None):
    """Stub the thin httpx boundary (``_download``) with a canned response.

    The real httpx wiring in ``_download`` is validated end-to-end against the
    live CDN; these unit tests exercise the surrounding fetch logic (conditional
    headers, decompression, parse/filter, 304 handling) deterministically.
    ``capture`` (a dict) receives the request headers ``fetch_instruments`` built.
    """
    async def fake_download(client, url, headers):
        if capture is not None:
            capture["headers"] = dict(headers)
        if status == httpx.codes.NOT_MODIFIED:
            return status, b"", httpx.Headers()
        resp_headers = httpx.Headers(
            {k: v for k, v in {"ETag": etag, "Last-Modified": last_modified}.items() if v}
        )
        return status, _gz(payload if payload is not None else []), resp_headers

    monkeypatch.setattr(instrument_fetch, "_download", fake_download)


# ── Fetch: filtering & de-duplication ─────────────────────────────────────────────
async def test_fetch_filters_segment_type_and_dedups(monkeypatch):
    payload = [
        _raw("NSE_EQ|INE001", "AAA"),                      # keep
        _raw("NSE_EQ|INE002", "BBB", itype="BE"),          # keep (BE allowed)
        _raw("NSE_EQ|INE003", "CCC", itype="FUT"),         # drop (type)
        _raw("BSE_EQ|INE004", "DDD", segment="BSE_EQ"),     # drop (segment)
        _raw("NSE_EQ|INE001", "AAA"),                       # drop (duplicate key)
        {"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "EEE"},  # drop (no key)
    ]
    _patch_download(monkeypatch, payload=payload, etag='"v1"')

    result = await instrument_fetch.fetch_instruments(_FakeRedis(), force=True)

    assert result.not_modified is False
    keys = sorted(i["instrument_key"] for i in result.instruments)
    assert keys == ["NSE_EQ|INE001", "NSE_EQ|INE002"]
    assert result.etag == '"v1"'


async def test_fetch_304_short_circuits_and_sends_validator(monkeypatch):
    cached = json.dumps({"etag": '"v1"', "last_modified": None})
    capture: dict = {}
    _patch_download(monkeypatch, status=304, capture=capture)

    result = await instrument_fetch.fetch_instruments(_FakeRedis(initial=cached), force=False)

    assert result.not_modified is True
    assert result.instruments == []
    assert capture["headers"].get("If-None-Match") == '"v1"'


async def test_fetch_force_skips_conditional_header(monkeypatch):
    cached = json.dumps({"etag": '"v1"', "last_modified": None})
    capture: dict = {}
    _patch_download(monkeypatch, payload=[_raw("NSE_EQ|INE001", "AAA")], capture=capture)

    await instrument_fetch.fetch_instruments(_FakeRedis(initial=cached), force=True)

    assert "If-None-Match" not in capture["headers"]


async def test_fetch_zero_instruments_raises(monkeypatch):
    _patch_download(monkeypatch, payload=[])
    with pytest.raises(UpstoxAPIError):
        await instrument_fetch.fetch_instruments(_FakeRedis(), force=True)


async def test_fetch_decompressed_cap_guard(monkeypatch):
    # Shrink the ceiling so a normal payload trips the gzip-bomb guard.
    monkeypatch.setattr(instrument_fetch, "_MAX_DECOMPRESSED_BYTES", 8)
    _patch_download(monkeypatch, payload=[_raw("NSE_EQ|INE001", "AAA")])
    with pytest.raises(UpstoxAPIError):
        await instrument_fetch.fetch_instruments(_FakeRedis(), force=True)


async def test_persist_validators_roundtrip_and_noop():
    fake = _FakeRedis()
    # No validators -> nothing stored (nothing to condition on next time).
    await instrument_fetch.persist_validators(fake, None, None)
    assert instrument_fetch._HTTP_CACHE_KEY not in fake.store
    # With validators -> stored and readable back for the next conditional GET.
    await instrument_fetch.persist_validators(fake, '"v9"', "Tue, 10 Jun 2026 00:00:00 GMT")
    etag, last_modified = await instrument_fetch._read_validators(fake)
    assert etag == '"v9"' and last_modified == "Tue, 10 Jun 2026 00:00:00 GMT"


# ── Reconcile: helpers ─────────────────────────────────────────────────────────────
# Setup is committed into the outer (fixture-owned) transaction so the reconciler's
# own commit/rollback boundary operates on a *fresh* savepoint and can never undo
# the seeded fixture state. The outer transaction is rolled back at fixture teardown,
# so the real instrument_master is never touched.
async def _clean_slate(session):
    await session.execute(delete(InstrumentMaster))
    await session.commit()


async def _seed(session, key, symbol, *, is_active=True, last_seen=None):
    session.add(
        InstrumentMaster(
            instrument_key=key,
            trading_symbol=symbol,
            name=symbol,
            exchange="NSE",
            instrument_type="EQ",
            is_active=is_active,
            last_seen_at=last_seen or datetime.now(timezone.utc),
            delisted_at=None if is_active else datetime.now(timezone.utc),
        )
    )
    await session.commit()


async def _row(session, key) -> InstrumentMaster | None:
    return (
        await session.execute(select(InstrumentMaster).where(InstrumentMaster.instrument_key == key))
    ).scalar_one_or_none()


# ── Reconcile: behaviour ───────────────────────────────────────────────────────────
async def test_insert_new_instruments(db_session):
    await _clean_slate(db_session)
    result = await sync_instrument_master(
        db_session, [_norm("NSE_EQ|INE001", "AAA"), _norm("NSE_EQ|INE002", "BBB")],
        min_instruments=1,
    )
    assert result.inserted == 2
    assert result.delisted == 0
    assert result.active_after == 2
    assert (await _row(db_session, "NSE_EQ|INE001")).is_active is True


async def test_delisting_soft_deletes_missing(db_session):
    await _clean_slate(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=1)
    await _seed(db_session, "NSE_EQ|INE001", "AAA", last_seen=old)
    await _seed(db_session, "NSE_EQ|INE002", "BBB", last_seen=old)  # will drop out

    result = await sync_instrument_master(db_session, [_norm("NSE_EQ|INE001", "AAA")], min_instruments=1)

    assert result.delisted == 1
    kept, gone = await _row(db_session, "NSE_EQ|INE001"), await _row(db_session, "NSE_EQ|INE002")
    assert kept.is_active is True and kept.delisted_at is None
    assert gone.is_active is False and gone.delisted_at is not None


async def test_reactivation_clears_delisted(db_session):
    await _clean_slate(db_session)
    await _seed(db_session, "NSE_EQ|INE001", "AAA", is_active=False,
                last_seen=datetime.now(timezone.utc) - timedelta(days=5))

    result = await sync_instrument_master(db_session, [_norm("NSE_EQ|INE001", "AAA")], min_instruments=1)

    assert result.reactivated == 1
    row = await _row(db_session, "NSE_EQ|INE001")
    assert row.is_active is True and row.delisted_at is None


async def test_idempotent_resync(db_session):
    await _clean_slate(db_session)
    universe = [_norm("NSE_EQ|INE001", "AAA"), _norm("NSE_EQ|INE002", "BBB")]
    await sync_instrument_master(db_session, universe, min_instruments=1)
    result = await sync_instrument_master(db_session, universe, min_instruments=1)
    assert result.inserted == 0 and result.delisted == 0 and result.reactivated == 0


async def test_counts_are_arithmetically_consistent(db_session):
    await _clean_slate(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=1)
    await _seed(db_session, "NSE_EQ|INE001", "AAA", last_seen=old)         # stays
    await _seed(db_session, "NSE_EQ|INE002", "BBB", last_seen=old)         # delisted
    await _seed(db_session, "NSE_EQ|INE003", "CCC", is_active=False, last_seen=old)  # reactivated

    result = await sync_instrument_master(
        db_session,
        [_norm("NSE_EQ|INE001", "AAA"), _norm("NSE_EQ|INE003", "CCC"), _norm("NSE_EQ|INE004", "DDD")],
        min_instruments=1,
    )
    assert result.total_seen == result.inserted + result.updated
    assert result.active_after == result.active_before - result.delisted + result.inserted + result.reactivated
    assert result.inserted == 1 and result.delisted == 1 and result.reactivated == 1


async def test_dry_run_writes_nothing(db_session):
    await _clean_slate(db_session)
    await _seed(db_session, "NSE_EQ|INE001", "AAA",
                last_seen=datetime.now(timezone.utc) - timedelta(days=1))

    result = await sync_instrument_master(
        db_session, [_norm("NSE_EQ|INE002", "BBB"), _norm("NSE_EQ|INE003", "CCC")],
        min_instruments=1, dry_run=True,
    )
    # Result reflects the *planned* change...
    assert result.inserted == 2 and result.delisted == 1
    # ...but the table is untouched (original active row still active, no new rows).
    assert (await _row(db_session, "NSE_EQ|INE001")).is_active is True
    assert await _row(db_session, "NSE_EQ|INE002") is None


async def test_sanity_guard_aborts_and_preserves_table(db_session):
    await _clean_slate(db_session)
    for i in range(10):
        await _seed(db_session, f"NSE_EQ|INE{i:03d}", f"SYM{i}")

    # File holds 1 instrument vs 10 active -> below the 90% floor -> abort.
    with pytest.raises(SyncSanityError):
        await sync_instrument_master(db_session, [_norm("NSE_EQ|INE000", "SYM0")])

    # Nothing delisted; all 10 still active.
    active = (
        await db_session.execute(
            select(InstrumentMaster).where(InstrumentMaster.is_active.is_(True))
        )
    ).scalars().all()
    assert len(active) == 10


# ── Concurrency: advisory lock ─────────────────────────────────────────────────────
async def test_advisory_lock_admits_one_holder():
    from app.core.database import AsyncSessionLocal
    from app.services.instrument_sync_service import _LOCK_KEY, _advisory_unlock, _try_advisory_lock

    async with AsyncSessionLocal() as s1, AsyncSessionLocal() as s2:
        try:
            assert await _try_advisory_lock(s1, _LOCK_KEY) is True
            assert await _try_advisory_lock(s2, _LOCK_KEY) is False  # contended
            await _advisory_unlock(s1, _LOCK_KEY)
            assert await _try_advisory_lock(s2, _LOCK_KEY) is True   # now free
        finally:
            await _advisory_unlock(s1, _LOCK_KEY)
            await _advisory_unlock(s2, _LOCK_KEY)


# ── Consumers: eligibility & resolvers ─────────────────────────────────────────────
async def test_eligibility_and_resolvers_respect_is_active(db_session):
    from app.services.symbol_validator import symbol_validator as sv

    await _clean_slate(db_session)
    await _seed(db_session, "NSE_EQ|INE100", "LIVE")
    await _seed(db_session, "NSE_EQ|INE200", "DEAD", is_active=False,
                last_seen=datetime.now(timezone.utc) - timedelta(days=1))

    # Eligibility gate: only the live symbol qualifies.
    assert await sv._db_check_single("LIVE", db_session) is True
    assert await sv._db_check_single("DEAD", db_session) is False
    assert await sv._db_check_batch(["LIVE", "DEAD"], db_session) == {"LIVE"}

    # Resolver default (active-only) hides the delisted symbol; include_inactive reveals it.
    assert await sv.get_instrument_key("DEAD", db_session) is None
    assert await sv.get_instrument_key("DEAD", db_session, include_inactive=True) == "NSE_EQ|INE200"
