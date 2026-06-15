"""
Operator entrypoint for an on-demand instrument-master sync.

Shares the *exact* fetch + reconcile path used by the in-app
``InstrumentSyncService`` (``app.services.instrument_fetch`` +
``app.services.data_ingestion.sync_instrument_master``), so a manual run and a
scheduled run can never diverge in behaviour.

Like the scheduler, the sync runs under the shared PostgreSQL advisory lock, so
a manual invocation can never race the background scheduler.

Usage
-----
  python scripts/sync_instruments.py              # conditional GET; sync if changed
  python scripts/sync_instruments.py --force      # ignore the 304 cache; full re-sync
  python scripts/sync_instruments.py --dry-run    # fetch + report planned changes, no writes

Exit codes
----------
  0  success (synced, or nothing to do)
  1  advisory lock held by another process (sync skipped)
  2  sanity guard aborted the sync, or an unexpected error occurred
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make ``app.*`` importable when run directly (matches every other backend script).
BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.redis import close_redis, get_redis, init_redis  # noqa: E402
from app.exceptions import SyncSanityError  # noqa: E402
from app.services.data_ingestion import sync_instrument_master  # noqa: E402
from app.services.instrument_fetch import fetch_instruments, persist_validators  # noqa: E402
from app.services.instrument_sync_service import (  # noqa: E402
    _LOCK_KEY,
    _advisory_unlock,
    _try_advisory_lock,
)

logger = logging.getLogger("sync_instruments")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_EXIT_OK = 0
_EXIT_LOCK_HELD = 1
_EXIT_ERROR = 2


async def _run(*, force: bool, dry_run: bool) -> int:
    await init_redis()
    try:
        redis = get_redis()
        async with AsyncSessionLocal() as lock_session:
            if not await _try_advisory_lock(lock_session, _LOCK_KEY):
                logger.warning("Advisory lock held by another process; skipping sync.")
                return _EXIT_LOCK_HELD
            try:
                fetched = await fetch_instruments(redis, force=force)
                if fetched.not_modified:
                    logger.info("Instrument file unchanged (HTTP 304); nothing to do.")
                    return _EXIT_OK

                async with AsyncSessionLocal() as session:
                    result = await sync_instrument_master(
                        session, fetched.instruments, dry_run=dry_run
                    )

                if dry_run:
                    logger.info(
                        "DRY RUN — no changes written. Planned: seen=%d inserted=%d "
                        "updated=%d reactivated=%d delisted=%d active=%d->%d",
                        result.total_seen, result.inserted, result.updated,
                        result.reactivated, result.delisted,
                        result.active_before, result.active_after,
                    )
                else:
                    # Persist validators only after a real, successful sync.
                    await persist_validators(redis, fetched.etag, fetched.last_modified)
                    logger.info(
                        "Sync complete: seen=%d inserted=%d updated=%d reactivated=%d "
                        "delisted=%d active=%d->%d",
                        result.total_seen, result.inserted, result.updated,
                        result.reactivated, result.delisted,
                        result.active_before, result.active_after,
                    )
                return _EXIT_OK
            finally:
                await _advisory_unlock(lock_session, _LOCK_KEY)
    except SyncSanityError as exc:
        logger.error("Sync aborted by sanity guard: %s", exc)
        return _EXIT_ERROR
    except Exception:
        logger.error("Instrument sync failed", exc_info=True)
        return _EXIT_ERROR
    finally:
        await close_redis()


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync the Upstox instrument master.")
    parser.add_argument(
        "--force", action="store_true",
        help="Bypass the conditional-GET cache and re-download the full file.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report the changes that would be made without writing to the database.",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(_run(force=args.force, dry_run=args.dry_run))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
