"""
One-off retroactive cleanup: purge ETF/REIT/InvIT contamination.

Context
-------
Cortex is scoped to single-company equities only. Before the asset-class
filter (migration 0055, app.services.instrument_classifier) existed, the
scanner/correlation engine treated ETFs and REIT/InvIT trust units exactly
like ordinary stocks — e.g. a real ``BUY MSCI360`` (an ETF) trade suggestion
was generated. This script retroactively cleans up that contamination now
that ``instrument_master.asset_class`` correctly identifies these
instruments.

What this does
---------------
  1. Finds every ``instrument_master`` row with ``asset_class != 'STOCK'``.
  2. Invalidates any currently-*active* ``TradeSuggestion`` for those
     instruments — using the exact same select→update→publish→metric
     skeleton as ``worker.expiry_loop`` (batched, ``RETURNING``, publishes to
     the real ``cai:suggestions:expired`` channel so connected frontends
     transition the card exactly as they would for a natural expiry — no
     frontend changes needed), so nobody can act on a mis-scoped suggestion.
  3. Purges ``ml_features`` rows for those symbols via the *existing*
     ``app.ml.features.feature_store.delete_features`` utility (no new
     deletion code) — full date range, current ``FEATURE_VERSION`` only.

What this deliberately does NOT do
------------------------------------
  - Does not touch historical ``EventCorrelation`` rows that never produced a
    suggestion (``consensus_reached=False``) — audit-only, no live/actionable
    footprint, and not cheaply filterable (no symbol column, only JSONB).
  - Does not retrain or redeploy any ML model. The production ensemble may
    have been trained on a manifest that included ETF symbols
    (documentation/backend/docs/training/TRAINING_SYMBOLS.md, separately
    cleaned up) — retraining is a distinct, high-risk initiative requiring
    its own backtest/rollout process, tracked separately, not bundled here.

Usage
-----
  python scripts/cleanup_etf_instruments.py              # dry run (default)
  python scripts/cleanup_etf_instruments.py --execute    # actually writes

Exit codes
----------
  0  success (including "nothing to clean up")
  2  unexpected error
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import select, update  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.core.metrics import suggestion_expiry_total, suggestions_active  # noqa: E402
from app.core.redis import (  # noqa: E402
    RedisChannels,
    close_redis,
    get_pubsub_client,
    init_redis,
)
from app.ml.features.feature_store import delete_features  # noqa: E402
from app.models.trade_suggestions import TradeSuggestion  # noqa: E402
from app.models.upstox_data import InstrumentMaster  # noqa: E402
from app.services.instrument_classifier import AssetClass  # noqa: E402

logger = logging.getLogger("cleanup_etf_instruments")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_EXIT_OK = 0
_EXIT_ERROR = 2


async def _find_non_stock_instruments(session) -> list[InstrumentMaster]:
    stmt = select(InstrumentMaster).where(
        InstrumentMaster.asset_class != AssetClass.STOCK.value
    )
    return list((await session.execute(stmt)).scalars().all())


async def _invalidate_active_suggestions(
    session, instrument_keys: list[str], *, dry_run: bool
) -> list[tuple]:
    """Invalidate active suggestions for the given instrument_keys.

    Mirrors worker.expiry_loop's select -> update -> RETURNING pattern.
    Returns the rows that were (or would be) affected.
    """
    if not instrument_keys:
        return []

    now = datetime.now(timezone.utc)
    select_stmt = select(TradeSuggestion.id).where(
        TradeSuggestion.status == "active",
        TradeSuggestion.instrument_key.in_(instrument_keys),
    )

    if dry_run:
        preview_stmt = select(
            TradeSuggestion.suggestion_id,
            TradeSuggestion.symbol,
            TradeSuggestion.signal_direction,
            TradeSuggestion.confidence_level,
            TradeSuggestion.consensus_score,
        ).where(TradeSuggestion.id.in_(select_stmt))
        return list((await session.execute(preview_stmt)).fetchall())

    stmt = (
        update(TradeSuggestion)
        .where(TradeSuggestion.id.in_(select_stmt))
        .values(status="invalidated", updated_at=now)
        .returning(
            TradeSuggestion.suggestion_id,
            TradeSuggestion.symbol,
            TradeSuggestion.signal_direction,
            TradeSuggestion.confidence_level,
            TradeSuggestion.consensus_score,
        )
    )
    rows = (await session.execute(stmt)).fetchall()
    await session.commit()
    return rows


async def _publish_invalidations(rows: list[tuple]) -> None:
    if not rows:
        return
    pubsub = get_pubsub_client()
    now = datetime.now(timezone.utc)
    for suggestion_id, symbol, direction, confidence, score in rows:
        suggestion_expiry_total.labels(direction=direction, confidence_level=confidence).inc()
        suggestions_active.labels(direction=direction, confidence_level=confidence).dec()
        try:
            await pubsub.publish_json(
                RedisChannels.SUGGESTIONS_EXPIRED,
                {
                    "suggestion_id": str(suggestion_id),
                    "symbol": symbol,
                    "signal_direction": direction,
                    "confidence_level": confidence,
                    "consensus_score": float(score),
                    "expired_at": now.isoformat(),
                    "reason": "ASSET_CLASS_EXCLUDED",
                },
            )
        except Exception as exc:
            logger.warning("Failed to publish invalidation event for %s: %s", suggestion_id, exc)


async def _purge_feature_store(session, symbols: list[str], *, dry_run: bool) -> int:
    total = 0
    for symbol in symbols:
        if dry_run:
            logger.info("  [dry-run] would purge ml_features rows for symbol=%s", symbol)
            continue
        deleted = await delete_features(symbol, None, None, session)
        total += deleted
        if deleted:
            logger.info("  purged %d ml_features row(s) for symbol=%s", deleted, symbol)
    return total


async def _run(*, dry_run: bool) -> int:
    await init_redis()
    try:
        async with AsyncSessionLocal() as session:
            non_stock = await _find_non_stock_instruments(session)
            if not non_stock:
                logger.info("No non-STOCK instruments found; nothing to clean up.")
                return _EXIT_OK

            by_class: dict[str, list[str]] = {}
            for row in non_stock:
                by_class.setdefault(row.asset_class, []).append(row.trading_symbol)
            for asset_class, symbols in by_class.items():
                logger.info("Found %d %s instrument(s): %s", len(symbols), asset_class, symbols)

            instrument_keys = [row.instrument_key for row in non_stock]
            trading_symbols = [row.trading_symbol for row in non_stock]

            invalidated = await _invalidate_active_suggestions(
                session, instrument_keys, dry_run=dry_run
            )
            if dry_run:
                logger.info(
                    "DRY RUN — would invalidate %d active suggestion(s): %s",
                    len(invalidated),
                    [str(r[0]) for r in invalidated],
                )
            else:
                logger.info("Invalidated %d active suggestion(s)", len(invalidated))
                await _publish_invalidations(invalidated)

            purged = await _purge_feature_store(session, trading_symbols, dry_run=dry_run)
            if not dry_run:
                await session.commit()
                logger.info("Purged %d ml_features row(s) total", purged)

        return _EXIT_OK
    except Exception:
        logger.error("Cleanup failed", exc_info=True)
        return _EXIT_ERROR
    finally:
        await close_redis()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Retroactively invalidate trade suggestions and purge feature-store "
        "rows for non-STOCK instruments (ETFs/REITs/InvITs)."
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually write changes. Without this flag, runs as a dry run "
        "(reports what would change, writes nothing).",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(_run(dry_run=not args.execute))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
