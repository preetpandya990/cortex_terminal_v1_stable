#!/usr/bin/env python3
"""
Backfill stop_loss and target_price for ai_trading_signals
==========================================================
Populates the previously-null stop_loss and target_price columns for all BUY
signals that have a matching NSE EQ instrument with OHLCV history.

Algorithm
---------
Per unique symbol
  1. Fetch the last ≤64 daily OHLCV closes to compute annualised historical
     volatility using log-returns (same formula as price_target_service.py).
  2. Derive sl_pct = 1.5 × (ann_vol / √252).

Per signal row
  3. Resolve the reference close: the latest OHLCV daily close on or before
     the signal's signal_timestamp date (captures the market price available
     at the time the signal was generated).
  4. Use that close as the entry price proxy.
  5. Compute:
       stop_loss    = entry × (1 − sl_pct)
       target_price = entry × (1 + 1.5 × sl_pct)   [TP1 — primary target]
  6. Round to 2 decimal places (matches Numeric(12,2) column type).

All writes are issued in a single transaction via a PostgreSQL unnest batch-
update — one round-trip regardless of row count.

Safety
------
• Idempotent: only touches rows WHERE stop_loss IS NULL.
• Dry-run mode (--dry-run): prints a preview table, no DB writes.
• Atomic: the entire batch is committed or rolled back together.
• Skipped symbols are reported with their reason.

Usage
-----
  # Preview (no writes):
  python scripts/backfill_signal_price_levels.py --dry-run

  # Execute:
  python scripts/backfill_signal_price_levels.py

  # Specific symbols only:
  python scripts/backfill_signal_price_levels.py --symbols PINELABS,HFCL,TITAN

  # Verbose logging:
  python scripts/backfill_signal_price_levels.py --verbose
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import NamedTuple

# ── Path bootstrap — allows running from anywhere inside the repo ─────────────
_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, BIGINT, NUMERIC
from sqlalchemy.ext.asyncio import AsyncSession

# ── Constants (must match price_target_service.py) ────────────────────────────
_CANDLES_LOOKBACK: int   = 63    # ~3 months of NSE trading days
_MIN_CANDLES: int        = 20    # minimum candles for a meaningful vol estimate
_VOL_FLOOR: float        = 0.05  # 5% annualised floor
_DP2                     = Decimal("0.01")

_BATCH_LOG_INTERVAL: int = 25    # log progress every N signals

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_signal_price_levels")


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class _VolCtx:
    """Per-symbol volatility context computed from OHLCV history."""
    ann_vol: float
    candles_used: int


class _SignalRow(NamedTuple):
    id: int
    symbol: str
    reference_close: Decimal


class _Update(NamedTuple):
    id: int
    stop_loss: Decimal
    target_price: Decimal   # TP1 — primary target stored in the single target_price col


# ── Volatility helpers ────────────────────────────────────────────────────────

def _compute_ann_vol(closes: list[float]) -> float:
    """
    Annualised historical volatility from chronological daily closes.

    Uses the sample standard deviation of log-returns, annualised by √252.
    Returns at least _VOL_FLOOR to prevent zero SL on illiquid tickers.
    """
    if len(closes) < 2:
        return _VOL_FLOOR

    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
        if closes[i - 1] > 0 and closes[i] > 0
    ]
    n = len(log_returns)
    if n < 2:
        return _VOL_FLOOR

    mean_r = sum(log_returns) / n
    variance = sum((r - mean_r) ** 2 for r in log_returns) / (n - 1)  # sample variance
    return max(math.sqrt(variance) * math.sqrt(252), _VOL_FLOOR)


def _compute_levels(entry: Decimal, ann_vol: float) -> tuple[Decimal, Decimal]:
    """Return (stop_loss, take_profit_1) for a BUY signal at ``entry``."""
    ep = float(entry)
    daily_vol = ann_vol / math.sqrt(252)
    sl_pct    = 1.5 * daily_vol

    sl  = ep * (1.0 - sl_pct)
    tp1 = ep * (1.0 + 1.5 * sl_pct)

    def _dec(v: float) -> Decimal:
        return Decimal(str(round(v, 10))).quantize(_DP2, rounding=ROUND_HALF_UP)

    return _dec(sl), _dec(tp1)


# ── Database queries ──────────────────────────────────────────────────────────

async def _fetch_vol_contexts(
    session: AsyncSession,
    symbols: list[str],
) -> dict[str, _VolCtx]:
    """
    For each symbol in ``symbols``, fetch the last (_CANDLES_LOOKBACK + 1)
    daily closes from OHLCV and compute annualised vol.

    Returns a mapping symbol → _VolCtx for every symbol that has at least
    _MIN_CANDLES of history.  Symbols with insufficient data are omitted and
    will be counted as skipped by the caller.
    """
    if not symbols:
        return {}

    # Single query: unnest the symbol list, join instruments + OHLCV, and
    # window-rank by most-recent close.  Pulling at most _CANDLES_LOOKBACK+1
    # rows per symbol keeps memory bounded.
    stmt = text("""
        WITH ranked AS (
            SELECT
                im.trading_symbol,
                o.close,
                ROW_NUMBER() OVER (
                    PARTITION BY im.instrument_key
                    ORDER BY o.timestamp DESC
                ) AS rn
            FROM upstox_ohlcv o
            JOIN instrument_master im
                ON  im.instrument_key  = o.instrument_key
                AND im.exchange        = 'NSE'
                AND im.instrument_type = 'EQ'
            WHERE o.timeframe         = '1D'
              AND im.trading_symbol   = ANY(:symbols)
        ),
        filtered AS (
            SELECT trading_symbol, close
            FROM   ranked
            WHERE  rn <= :lookback
            ORDER  BY trading_symbol, rn DESC   -- ASC within symbol = chronological
        )
        SELECT trading_symbol, ARRAY_AGG(close ORDER BY rn) AS closes_desc
        FROM   ranked
        WHERE  rn <= :lookback
        GROUP  BY trading_symbol
        HAVING COUNT(*) >= :min_candles
    """)

    result = await session.execute(
        stmt,
        {
            "symbols":     symbols,
            "lookback":    _CANDLES_LOOKBACK + 1,
            "min_candles": _MIN_CANDLES + 1,
        },
    )
    rows = result.fetchall()

    ctx_map: dict[str, _VolCtx] = {}
    for row in rows:
        symbol: str = row[0]
        # closes_desc is newest→oldest; reverse to chronological for log-returns
        closes_desc: list[float] = [float(c) for c in row[1]]
        closes_asc = list(reversed(closes_desc))
        ann_vol = _compute_ann_vol(closes_asc)
        ctx_map[symbol] = _VolCtx(ann_vol=ann_vol, candles_used=len(closes_asc) - 1)
        logger.debug("  %s  ann_vol=%.4f  candles=%d", symbol, ann_vol, ctx_map[symbol].candles_used)

    return ctx_map


async def _fetch_signals_with_close(
    session: AsyncSession,
    symbols: list[str],
) -> list[_SignalRow]:
    """
    Fetch all BUY signals with NULL stop_loss for the given symbols, each
    paired with the latest OHLCV close on or before the signal date (LATERAL).

    Returns rows ordered by (symbol, signal_timestamp) for predictable logging.
    """
    stmt = text("""
        SELECT
            s.id,
            s.symbol,
            ref.close   AS reference_close
        FROM ai_trading_signals s
        JOIN instrument_master im
            ON  im.trading_symbol  = s.symbol
            AND im.exchange        = 'NSE'
            AND im.instrument_type = 'EQ'
        JOIN LATERAL (
            SELECT o.close
            FROM   upstox_ohlcv o
            WHERE  o.instrument_key = im.instrument_key
              AND  o.timeframe      = '1D'
              AND  o.timestamp::date <= DATE(s.signal_timestamp)
            ORDER  BY o.timestamp DESC
            LIMIT  1
        ) ref ON true
        WHERE s.action    = 'BUY'
          AND s.stop_loss IS NULL
          AND s.symbol    = ANY(:symbols)
        ORDER BY s.symbol, s.signal_timestamp
    """)

    result = await session.execute(stmt, {"symbols": symbols})
    return [
        _SignalRow(
            id=row[0],
            symbol=row[1],
            reference_close=Decimal(str(row[2])),
        )
        for row in result.fetchall()
    ]


async def _apply_batch_update(
    session: AsyncSession,
    updates: list[_Update],
) -> int:
    """
    Apply all SL/TP values in a single round-trip using PostgreSQL unnest.

    Returns the number of rows actually updated by the DB engine (may be less
    than len(updates) if a row was modified concurrently and is no longer NULL).
    """
    if not updates:
        return 0

    ids         = [u.id                  for u in updates]
    stop_losses = [float(u.stop_loss)    for u in updates]
    targets     = [float(u.target_price) for u in updates]

    # asyncpg requires explicit SQLAlchemy ARRAY types for UNNEST — the
    # PostgreSQL ::type[] cast syntax conflicts with named parameter binding.
    stmt = (
        text("""
            UPDATE ai_trading_signals AS t
            SET
                stop_loss    = v.stop_loss,
                target_price = v.target_price
            FROM (
                SELECT
                    UNNEST(:ids) AS id,
                    UNNEST(:sls) AS stop_loss,
                    UNNEST(:tps) AS target_price
            ) AS v
            WHERE t.id        = v.id
              AND t.stop_loss IS NULL
        """)
        .bindparams(
            bindparam("ids", value=ids,         type_=ARRAY(BIGINT)),
            bindparam("sls", value=stop_losses, type_=ARRAY(NUMERIC)),
            bindparam("tps", value=targets,     type_=ARRAY(NUMERIC)),
        )
    )

    result = await session.execute(stmt)
    return result.rowcount


# ── Core orchestration ────────────────────────────────────────────────────────

async def run_backfill(
    dry_run: bool,
    filter_symbols: list[str] | None,
    verbose: bool,
) -> None:
    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:

        # ── 1. Identify eligible symbols ──────────────────────────────────────
        scope_clause = "AND s.symbol = ANY(:filter_syms)" if filter_symbols else ""
        count_stmt = text(f"""
            SELECT DISTINCT s.symbol
            FROM ai_trading_signals s
            JOIN instrument_master im
                ON  im.trading_symbol  = s.symbol
                AND im.exchange        = 'NSE'
                AND im.instrument_type = 'EQ'
            WHERE s.action    = 'BUY'
              AND s.stop_loss IS NULL
              {scope_clause}
            ORDER BY s.symbol
        """)
        params: dict = {}
        if filter_symbols:
            params["filter_syms"] = filter_symbols

        result = await session.execute(count_stmt, params)
        eligible_symbols: list[str] = [row[0] for row in result.fetchall()]

        if not eligible_symbols:
            logger.info("Nothing to do — no eligible BUY signals with null stop_loss.")
            return

        logger.info(
            "Found %d unique symbol(s) with BUY signals requiring SL/TP.",
            len(eligible_symbols),
        )

        # ── 2. Compute vol context per symbol ─────────────────────────────────
        logger.info("Computing volatility context for %d symbol(s)…", len(eligible_symbols))
        vol_map = await _fetch_vol_contexts(session, eligible_symbols)

        covered   = [s for s in eligible_symbols if s in vol_map]
        no_ohlcv  = [s for s in eligible_symbols if s not in vol_map]

        if no_ohlcv:
            logger.warning(
                "Skipping %d symbol(s) — insufficient OHLCV history (< %d candles): %s",
                len(no_ohlcv), _MIN_CANDLES, ", ".join(no_ohlcv),
            )

        if not covered:
            logger.error("No symbols have sufficient OHLCV data. Nothing to update.")
            return

        logger.info(
            "Volatility resolved for %d/%d symbol(s). Fetching signal rows…",
            len(covered), len(eligible_symbols),
        )

        # ── 3. Fetch signals paired with reference close ───────────────────────
        signal_rows = await _fetch_signals_with_close(session, covered)
        logger.info("Processing %d signal row(s)…", len(signal_rows))

        # ── 4. Compute SL/TP for every signal ─────────────────────────────────
        updates: list[_Update] = []
        skipped_zero: list[int] = []

        for i, sig in enumerate(signal_rows, start=1):
            ctx = vol_map[sig.symbol]

            if sig.reference_close <= 0:
                skipped_zero.append(sig.id)
                logger.debug(
                    "  [skip] id=%-7d %-15s  close=%s (non-positive)",
                    sig.id, sig.symbol, sig.reference_close,
                )
                continue

            sl, tp1 = _compute_levels(sig.reference_close, ctx.ann_vol)
            updates.append(_Update(id=sig.id, stop_loss=sl, target_price=tp1))

            if verbose or i % _BATCH_LOG_INTERVAL == 0:
                logger.info(
                    "  [%4d/%4d] %-15s  close=%-9s  sl=%-9s  tp1=%-9s  vol=%.4f",
                    i, len(signal_rows), sig.symbol,
                    sig.reference_close, sl, tp1, ctx.ann_vol,
                )

        if skipped_zero:
            logger.warning(
                "%d signal(s) skipped due to zero/negative reference close: ids=%s",
                len(skipped_zero), skipped_zero,
            )

        if not updates:
            logger.info("No valid updates to apply.")
            return

        # ── 5. Dry-run preview ────────────────────────────────────────────────
        if dry_run:
            logger.info(
                "\n%s\n  DRY RUN — %d update(s) would be written (no DB changes)\n%s",
                "─" * 70, len(updates), "─" * 70,
            )
            # Print a compact summary table
            sample = updates[:20]
            print(f"\n{'ID':>8}  {'SL':>10}  {'TP1':>10}")
            print("─" * 32)
            for u in sample:
                print(f"{u.id:>8}  {u.stop_loss:>10}  {u.target_price:>10}")
            if len(updates) > 20:
                print(f"  … and {len(updates) - 20} more rows")
            print()
            return

        # ── 6. Apply in a single atomic transaction ────────────────────────────
        logger.info("Applying %d update(s) in a single transaction…", len(updates))
        try:
            rows_updated = await _apply_batch_update(session, updates)
            await session.commit()
            logger.info(
                "✓  Committed.  DB rows updated: %d  (computed: %d)",
                rows_updated, len(updates),
            )
        except Exception:
            await session.rollback()
            logger.exception("Transaction failed — all changes rolled back.")
            sys.exit(1)

        # ── 7. Final report ───────────────────────────────────────────────────
        logger.info(
            "\n%s\n  Backfill complete\n"
            "  Symbols resolved : %d / %d\n"
            "  Signals updated  : %d\n"
            "  Signals skipped  : %d (no OHLCV close)\n"
            "  Symbols skipped  : %d (insufficient history)\n"
            "%s",
            "═" * 60,
            len(covered), len(eligible_symbols),
            rows_updated,
            len(skipped_zero),
            len(no_ohlcv),
            "═" * 60,
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill stop_loss / target_price for ai_trading_signals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to the database.",
    )
    parser.add_argument(
        "--symbols",
        metavar="SYM1,SYM2",
        help="Comma-separated list of symbols to restrict the backfill to.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print every signal row as it is processed.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    filter_symbols: list[str] | None = None
    if args.symbols:
        filter_symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    started_at = datetime.now(timezone.utc)
    logger.info(
        "=== backfill_signal_price_levels  started=%s  dry_run=%s ===",
        started_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
        args.dry_run,
    )

    asyncio.run(
        run_backfill(
            dry_run=args.dry_run,
            filter_symbols=filter_symbols,
            verbose=args.verbose,
        )
    )

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    logger.info("Finished in %.2fs", elapsed)
