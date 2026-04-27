#!/usr/bin/env python3
"""
Data Ingestion Worker — Pre-flight Test Suite
=============================================
Validates DB connectivity, instrument master, existing data coverage,
Upstox API reachability, gap detection, and configuration before
running the production worker.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.services.data_ingestion_worker import DataIngestionWorker, TIMEFRAME_SPECS
from app.services.upstox_client import UpstoxClient

settings = get_settings()


async def test_database_connection() -> async_sessionmaker:
    print("Checking database connection ...")
    engine = create_async_engine(str(settings.DATABASE_URL), echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            assert (await session.execute(text("SELECT 1"))).scalar() == 1
        print("  OK  database reachable")
        return factory
    except Exception as exc:
        print(f"  FAIL  database: {exc}")
        sys.exit(1)


async def test_instrument_master(factory: async_sessionmaker) -> None:
    print("Checking instrument master ...")
    async with factory() as session:
        result = await session.execute(
            text("SELECT COUNT(*) FROM instrument_master WHERE exchange = 'NSE'")
        )
        count = result.scalar() or 0
    if count == 0:
        print("  FAIL  no NSE instruments found — run the instrument sync script first")
        sys.exit(1)
    print(f"  OK  {count:,} NSE instruments")


async def test_existing_data(factory: async_sessionmaker) -> None:
    print("Checking existing OHLCV coverage ...")
    async with factory() as session:
        result = await session.execute(text("""
            SELECT
                timeframe,
                COUNT(*)                        AS rows,
                COUNT(DISTINCT instrument_key)  AS instruments,
                MIN(timestamp)::date            AS earliest,
                MAX(timestamp)::date            AS latest
            FROM upstox_ohlcv
            GROUP BY timeframe
            ORDER BY timeframe
        """))
        rows = result.all()

    if not rows:
        print("  WARN  no OHLCV data yet — Phase 1 backfill will populate it")
        return

    for row in rows:
        span = (row.latest - row.earliest).days
        print(
            f"  {row.timeframe:10s}  {row.rows:>10,} rows  "
            f"{row.instruments:>5} instruments  "
            f"{row.earliest} → {row.latest}  ({span}d)"
        )


async def test_upstox_api() -> None:
    print("Checking Upstox API reachability ...")
    client = UpstoxClient()
    await client.start()
    try:
        # Use V3 path format: /historical-candle/{key}/{unit}/{interval}/{to}/{from}
        data = await client.get(
            "/historical-candle/NSE_EQ%7CINE002A01018/days/1/2026-04-15/2026-04-01"
        )
        if data.get("message") == "Mock data - Upstox API unavailable":
            print("  WARN  no access token — will use mock data (expected in dev)")
        else:
            candles = data.get("data", {}).get("candles", [])
            print(f"  OK  Upstox API live ({len(candles)} candles returned)")
    except Exception as exc:
        print(f"  WARN  Upstox API error: {exc}")
    finally:
        await client.stop()


async def test_gap_detection(factory: async_sessionmaker) -> None:
    print("Checking gap detection ...")
    client = UpstoxClient()
    await client.start()
    try:
        worker = DataIngestionWorker(factory, client)

        maintenance_tasks = await worker.detect_gaps(backfill=False)
        print(f"  Maintenance mode : {len(maintenance_tasks):,} chunks")

        if settings.DATA_INGESTION_BACKFILL_ENABLED:
            backfill_tasks = await worker.detect_gaps(backfill=True)
            print(f"  Backfill mode   : {len(backfill_tasks):,} chunks")
            if backfill_tasks:
                t = backfill_tasks[0]
                print(f"  First chunk     : {t.symbol} | {t.timeframe} | {t.from_date} → {t.to_date}")

        print("  OK  gap detection functional")
    finally:
        await client.stop()


def test_configuration() -> None:
    print("Checking configuration ...")
    checks = [
        ("DATA_INGESTION_ENABLED",            settings.DATA_INGESTION_ENABLED is True),
        ("DATA_INGESTION_BACKFILL_ENABLED",   settings.DATA_INGESTION_BACKFILL_ENABLED is True),
        ("DATA_INGESTION_REQUESTS_PER_MINUTE", settings.DATA_INGESTION_REQUESTS_PER_MINUTE >= 10),
        ("DATA_INGESTION_CONCURRENCY",        settings.DATA_INGESTION_CONCURRENCY >= 1),
        ("DATA_INGESTION_CHECK_INTERVAL",     settings.DATA_INGESTION_CHECK_INTERVAL >= 60),
        ("BULK_INSERT_BATCH_SIZE",            settings.BULK_INSERT_BATCH_SIZE >= 100),
        ("UPSTOX_BASE_URL (must be v3)",      "v3" in settings.UPSTOX_BASE_URL),
        ("UPSTOX_ACCESS_TOKEN",               settings.UPSTOX_ACCESS_TOKEN is not None),
    ]
    failed = False
    for name, ok in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {name}")
        if not ok:
            failed = True
    if failed:
        print("  Some configuration checks failed — review settings above")
        sys.exit(1)


def test_timeframe_specs() -> None:
    print("Checking timeframe specs ...")
    for key, spec in TIMEFRAME_SPECS.items():
        print(
            f"  {key:10s}  unit={spec.unit}/{spec.interval}  "
            f"chunk={spec.chunk_days}d  "
            f"from={spec.available_from}  "
            f"target={spec.target_days}d  "
            f"priority={spec.priority}"
        )
    print(f"  OK  {len(TIMEFRAME_SPECS)} timeframes configured")


async def main() -> None:
    print("=" * 72)
    print("Cortex AI — Data Ingestion Worker Pre-flight Checks")
    print("=" * 72)

    factory = await test_database_connection()
    await test_instrument_master(factory)
    await test_existing_data(factory)
    await test_upstox_api()
    test_configuration()
    test_timeframe_specs()
    await test_gap_detection(factory)

    print("\n" + "=" * 72)
    print("All checks passed")
    print("=" * 72)
    print("\nTo start the worker:  python -m app.worker")
    print("To watch progress:    tail -f logs/worker.log | grep ingestion")


if __name__ == "__main__":
    asyncio.run(main())
