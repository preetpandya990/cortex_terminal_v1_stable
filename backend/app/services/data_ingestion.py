"""
Cortex AI — Bulk Ingestion Service
====================================
Replaces per-row INSERT loops with batch PostgreSQL upserts.

Key improvements over the original:
  - insert().values(batch).on_conflict_do_nothing() — single round-trip per batch
  - Configurable batch size (default 1000 rows)
  - Explicit typed errors — never swallows exceptions silently
  - datetime.now(timezone.utc) throughout (replaces deprecated utcnow)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.exceptions import DatabaseError, SyncSanityError, UpstoxAPIError
from app.models.upstox_data import UpstoxOHLCV, InstrumentMaster
from app.services.instrument_classifier import AssetClass, classify_asset
from app.services.upstox_client import UpstoxClient

logger = logging.getLogger(__name__)
settings = get_settings()

# Floor below which the active universe can never shrink in a single sync: the
# delist ratio guard refuses to act on a file holding fewer active instruments
# than this fraction of the current active set. A genuine market never sheds
# this much overnight, so a drop this large means a truncated/corrupt file.
_DELIST_RATIO_FLOOR = 0.9

# ── Candle ingestion ───────────────────────────────────────────────────────────
async def bulk_upsert_ohlcv(
    session: AsyncSession,
    rows: Sequence[dict],
    *,
    batch_size: int | None = None,
) -> int:
    """
    Upsert a sequence of OHLCV candle dicts into upstox_ohlcv.
    Uses PostgreSQL INSERT ... ON CONFLICT DO NOTHING — idempotent and efficient.
    Returns the number of rows processed.
    """
    if not rows:
        return 0

    size = batch_size or settings.BULK_INSERT_BATCH_SIZE
    total = 0

    for i in range(0, len(rows), size):
        batch = list(rows[i : i + size])
        stmt = (
            pg_insert(UpstoxOHLCV)
            .values(batch)
            .on_conflict_do_nothing(
                index_elements=["instrument_key", "timeframe", "timestamp"]
            )
        )
        try:
            await session.execute(stmt)
            total += len(batch)
            logger.debug("Upserted batch of %d OHLCV rows", len(batch))
        except Exception as exc:
            logger.error("OHLCV bulk upsert failed for batch starting at %d: %s", i, exc)
            raise DatabaseError(f"OHLCV bulk upsert failed: {exc}") from exc

    await session.commit()
    logger.info("Bulk OHLCV upsert complete: %d rows processed", total)
    return total


# ── Instrument master sync ─────────────────────────────────────────────────────
@dataclass(slots=True)
class InstrumentSyncResult:
    """Outcome of a reconciling instrument-master sync.

    All counts are exact and derived from in-transaction aggregates, so they
    reconcile arithmetically: ``active_after == active_before - delisted +
    inserted + reactivated`` and ``total_seen == inserted + updated``.
    """

    total_seen: int        # in-scope instruments present in the source file
    inserted: int          # brand-new instrument_keys
    updated: int           # pre-existing rows refreshed (includes reactivated)
    reactivated: int       # rows that flipped is_active false -> true (relistings)
    delisted: int          # rows soft-deleted this run (dropped from the file)
    active_before: int     # active rows before the sync (basis for the guard)
    active_after: int      # active rows after the sync
    total_after: int       # all rows after the sync (active + inactive)
    unclassified: int      # in-scope rows this run whose asset_class could not
                            # be determined (missing/unrecognised ISIN) — should
                            # stay near zero; a growing count signals upstream
                            # data drift, since UNCLASSIFIED rows are excluded
                            # from scanning/signal generation (fail-closed).


async def sync_instrument_master(
    session: AsyncSession,
    instruments: Sequence[dict],
    *,
    batch_size: int | None = None,
    min_instruments: int | None = None,
    dry_run: bool = False,
) -> InstrumentSyncResult:
    """Reconcile ``instrument_master`` against the current instrument file.

    Runs as a single atomic transaction:

      1. Snapshot current row/active counts.
      2. **Sanity guard** — abort (rollback) if the file holds implausibly few
         instruments, so a truncated/corrupt source can never mass-delist the
         universe.
      3. Upsert every in-scope instrument, stamping ``last_seen_at`` to this
         run's start and (re)activating the row.
      4. Reconcile delistings — soft-delete any still-active row whose watermark
         predates this run (i.e. absent from the file).
      5. Commit once.

    The whole operation is idempotent: a re-run with the same file is a no-op
    beyond refreshing ``last_seen_at``/``updated_at``.

    Args:
        session: an open async session; this function owns the transaction
            (commits on success, rolls back on any failure).
        instruments: normalized, de-duplicated in-scope instrument dicts
            (see ``instrument_fetch``).
        batch_size: upsert batch size; defaults to ``BULK_INSERT_BATCH_SIZE``.
        min_instruments: absolute floor for the sanity guard; defaults to
            ``INSTRUMENT_SYNC_MIN_INSTRUMENTS``.
        dry_run: when True, perform all work and compute exact counts but roll
            back instead of committing — the table is left untouched. The
            returned result reflects what *would* have changed.

    Returns:
        InstrumentSyncResult with exact reconciliation counts.

    Raises:
        SyncSanityError: the file failed the sanity guard (transaction rolled
            back; the table is unchanged).
        DatabaseError: any database failure (transaction rolled back).
    """
    total_seen = len(instruments)
    size = batch_size or settings.BULK_INSERT_BATCH_SIZE
    floor = min_instruments if min_instruments is not None else settings.INSTRUMENT_SYNC_MIN_INSTRUMENTS

    # Pin a single watermark for the whole run. Rows touched by the upsert get
    # this exact value; the reconcile step delists rows whose watermark is older.
    sync_started_at = datetime.now(timezone.utc)

    try:
        # 1. Snapshot ──────────────────────────────────────────────────────────
        before = (
            await session.execute(
                select(
                    func.count(InstrumentMaster.id),
                    func.count(InstrumentMaster.id).filter(InstrumentMaster.is_active.is_(True)),
                )
            )
        ).one()
        total_before, active_before = int(before[0]), int(before[1])

        # 2. Sanity guard ──────────────────────────────────────────────────────
        # A real NSE session never sheds >10% of its listings overnight, and the
        # universe never collapses below an absolute floor. Either signals a bad
        # file — abort before any write touches the table.
        min_required = max(floor, int(active_before * _DELIST_RATIO_FLOOR))
        if total_seen < min_required:
            await session.rollback()
            raise SyncSanityError(
                f"instrument file holds {total_seen} in-scope instruments, "
                f"below the required floor of {min_required} "
                f"(active_before={active_before}, abs_floor={floor}); sync aborted"
            )

        # 3. Upsert ────────────────────────────────────────────────────────────
        # Present rows are (re)activated and watermarked; mutable fields refreshed.
        # asset_class is classified once here, at sync time, and persisted —
        # never re-derived at query time (see instrument_classifier).
        unclassified = 0
        for i in range(0, total_seen, size):
            batch = []
            for row in instruments[i : i + size]:
                asset_class = classify_asset(row.get("isin"), row["trading_symbol"])
                if asset_class is AssetClass.UNCLASSIFIED:
                    unclassified += 1
                batch.append({
                    "instrument_key": row["instrument_key"],
                    "trading_symbol": row["trading_symbol"],
                    "name": row["name"],
                    "exchange": row["exchange"],
                    "instrument_type": row["instrument_type"],
                    "isin": row.get("isin"),
                    "asset_class": asset_class.value,
                    "is_active": True,
                    "delisted_at": None,
                    "last_seen_at": sync_started_at,
                    "updated_at": sync_started_at,
                })
            ins = pg_insert(InstrumentMaster)
            stmt = ins.values(batch).on_conflict_do_update(
                index_elements=["instrument_key"],
                set_={
                    "trading_symbol": ins.excluded.trading_symbol,
                    "name": ins.excluded.name,
                    "exchange": ins.excluded.exchange,
                    "instrument_type": ins.excluded.instrument_type,
                    "isin": ins.excluded.isin,
                    "asset_class": ins.excluded.asset_class,
                    "is_active": ins.excluded.is_active,
                    "delisted_at": ins.excluded.delisted_at,
                    "last_seen_at": ins.excluded.last_seen_at,
                    "updated_at": ins.excluded.updated_at,
                },
            )
            await session.execute(stmt)

        # 4. Reconcile delistings ───────────────────────────────────────────────
        # Any row still active but not touched by this run dropped out of the
        # file — soft-delete it (preserve history + foreign references).
        delist_result = await session.execute(
            update(InstrumentMaster)
            .where(
                InstrumentMaster.is_active.is_(True),
                InstrumentMaster.last_seen_at < sync_started_at,
            )
            .values(is_active=False, delisted_at=func.now())
        )
        delisted = int(delist_result.rowcount or 0)

        # Post-snapshot for exact, arithmetically-consistent counts.
        after = (
            await session.execute(
                select(
                    func.count(InstrumentMaster.id),
                    func.count(InstrumentMaster.id).filter(InstrumentMaster.is_active.is_(True)),
                )
            )
        ).one()
        total_after, active_after = int(after[0]), int(after[1])

        if dry_run:
            await session.rollback()
        else:
            await session.commit()
    except SyncSanityError:
        raise
    except Exception as exc:
        await session.rollback()
        logger.error("Instrument master sync failed: %s", exc, exc_info=True)
        raise DatabaseError(f"Instrument master sync failed: {exc}") from exc

    # Derive the breakdown from the snapshots (no extra round trips):
    #   inserted    = net row growth (upsert only inserts or updates)
    #   updated     = file rows that were not inserts
    #   reactivated = net active growth attributable to flips, backing out
    #                 inserts and delistings
    inserted = total_after - total_before
    updated = total_seen - inserted
    reactivated = active_after - active_before + delisted - inserted

    result = InstrumentSyncResult(
        total_seen=total_seen,
        inserted=inserted,
        updated=updated,
        reactivated=reactivated,
        delisted=delisted,
        active_before=active_before,
        active_after=active_after,
        total_after=total_after,
        unclassified=unclassified,
    )
    logger.info(
        "Instrument master sync complete: seen=%d inserted=%d updated=%d "
        "reactivated=%d delisted=%d active=%d->%d unclassified=%d",
        total_seen, inserted, updated, reactivated, delisted, active_before, active_after,
        unclassified,
    )
    if unclassified:
        logger.warning(
            "Instrument sync produced %d UNCLASSIFIED row(s) — these are "
            "excluded from scanning/signal generation until reclassified; "
            "investigate if this count is non-trivial or growing",
            unclassified,
        )
    return result


# ── Candle fetcher ─────────────────────────────────────────────────────────────
class DataIngestionService:
    """
    High-level service for fetching and storing OHLCV candle data.
    Lifecycle: instantiated once in lifespan, stored on app.state.
    """

    def __init__(self, upstox_client: UpstoxClient) -> None:
        self._client = upstox_client

    async def fetch_and_store_candles(
        self,
        session: AsyncSession,
        instrument_key: str,
        timeframe: str,
        from_date: str,
        to_date: str,
    ) -> int:
        """
        Fetch OHLCV candles from Upstox and bulk-upsert into the database.
        Returns the count of rows upserted.
        """
        try:
            data = await self._client.get(
                f"/historical-candle/{instrument_key}/{timeframe}/{to_date}/{from_date}"
            )
        except UpstoxAPIError:
            raise  # typed — let caller decide handling

        candles = data.get("data", {}).get("candles", [])
        if not candles:
            logger.info("No candles returned for %s [%s]", instrument_key, timeframe)
            return 0

        rows = [
            {
                "instrument_key": instrument_key,
                "timeframe": timeframe,
                "timestamp": candle[0],
                "open": candle[1],
                "high": candle[2],
                "low": candle[3],
                "close": candle[4],
                "volume": candle[5],
                "oi": candle[6] if len(candle) > 6 else 0,
            }
            for candle in candles
        ]

        return await bulk_upsert_ohlcv(session, rows)