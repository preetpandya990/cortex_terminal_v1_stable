"""
Sync Upstox instrument master from the official JSON file.

Usage:
  python scripts/sync_instruments.py

Notes:
- Streams and parses JSON to avoid loading the full file into memory.
- Filters to NSE equities by default (segment == "NSE_EQ").
"""
from __future__ import annotations

import asyncio
import gzip
import io
import logging
from pathlib import Path
from typing import Any

import httpx
import ijson

# Ensure backend/ is on sys.path for app imports when running as a script
import sys

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.core.config import get_settings
from app.core.database import AsyncSessionFactory
from app.services.data_ingestion import sync_instrument_master

logger = logging.getLogger("sync_instruments")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

settings = get_settings()

DEFAULT_SEGMENT = "NSE_EQ"
ALLOWED_TYPES = {"EQ", "BE"}
BATCH_SIZE = settings.BULK_INSERT_BATCH_SIZE


def _normalize_instrument(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("segment") != DEFAULT_SEGMENT:
        return None
    if item.get("instrument_type") not in ALLOWED_TYPES:
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
        "instrument_type": item.get("instrument_type") or "EQ",
    }


def _iter_instruments(stream: io.BufferedReader) -> Any:
    # The file is a JSON array. Stream items incrementally.
    return ijson.items(stream, "item")


async def _download_instruments() -> io.BytesIO:
    url = str(settings.UPSTOX_INSTRUMENTS_URL)
    logger.info("Downloading instruments file: %s", url)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
        return io.BytesIO(response.content)


async def main() -> None:
    blob = await _download_instruments()

    if str(settings.UPSTOX_INSTRUMENTS_URL).endswith(".gz"):
        raw = gzip.GzipFile(fileobj=blob)
    else:
        raw = blob

    total = 0
    batch: list[dict[str, Any]] = []

    async with AsyncSessionFactory() as session:
        for item in _iter_instruments(raw):
            if not isinstance(item, dict):
                continue
            normalized = _normalize_instrument(item)
            if not normalized:
                continue
            batch.append(normalized)
            if len(batch) >= BATCH_SIZE:
                total += await sync_instrument_master(session, batch)
                logger.info("Synced %d instruments", total)
                batch = []

        if batch:
            total += await sync_instrument_master(session, batch)

    logger.info("Instrument sync complete. Total rows: %d", total)


if __name__ == "__main__":
    asyncio.run(main())
