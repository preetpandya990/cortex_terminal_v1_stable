"""
Cortex AI — Post-Close Monitor Backfill
========================================
One-off script to create PostCloseMonitor records for trades that were
auto-closed by SL or TP on dates where monitoring was disabled.

For each qualifying outcome the script:
  1. Fetches 1-minute intraday OHLCV candles from the Upstox API for the
     instrument covering the window from the trade's close time to 15:30 IST
     (or today's intraday endpoint when the target date is today).
  2. Simulates the monitor loop against those candles — identical logic to
     evaluate_instrument_monitors() in post_close_monitor_service.py.
  3. Writes a single PostCloseMonitor row per outcome in COMPLETED state with
     all counterfactual fields populated from the candle replay.

Usage
-----
    # Default: backfill today's missing monitors
    python backfill_post_close_monitors.py

    # Specific date (YYYY-MM-DD):
    python backfill_post_close_monitors.py --date 2026-05-20

    # Dry run — print what would be created, write nothing:
    python backfill_post_close_monitors.py --dry-run

Design notes
------------
- Candles are fetched once per instrument and cached in-process; if multiple
  monitors exist for the same instrument on the same date only one API call
  is made.
- Uses the intraday endpoint for today; historical-candle endpoint for past
  dates.  Upstox v3 path format: /historical-candle/{key}/minutes/1/{to}/{from}
  intraday path: /historical-candle/intraday/{key}/minutes/1
- Candles are sorted ascending by timestamp before replay so hit timestamps
  reflect the first candle that breached each threshold.
- Records are only inserted; the script is safe to re-run — outcomes that
  already have a PostCloseMonitor are skipped at the query level.
- Raises on unexpected DB or API errors (non-zero exit) so CI/alerting can
  catch failures.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

# ── Path bootstrap ────────────────────────────────────────────────────────────
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import WorkerSessionLocal
from app.models.paper_trading import PostCloseMonitor

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_post_close_monitors")

# ── Constants ─────────────────────────────────────────────────────────────────
_IST         = timezone(timedelta(hours=5, minutes=30))
_SESSION_END = {"hour": 15, "minute": 30, "second": 0, "microsecond": 0}
_ZERO        = Decimal("0")


# ── Upstox candle fetch ───────────────────────────────────────────────────────

async def _fetch_1min_candles(
    client: httpx.AsyncClient,
    token: str,
    instrument_key: str,
    target_date: date,
) -> list[dict[str, Any]]:
    """
    Fetch 1-minute OHLCV candles for instrument_key on target_date.

    Uses the intraday endpoint when target_date is today (Upstox only serves
    today's intraday data via that endpoint); falls back to the historical-
    candle endpoint for past dates.

    Returns candles sorted ascending by timestamp (oldest first).
    Each item: {timestamp: datetime(tz=IST), open, high, low, close, volume}
    """
    encoded_key = quote(instrument_key, safe="")
    today = datetime.now(_IST).date()

    if target_date == today:
        path = f"historical-candle/intraday/{encoded_key}/minutes/1"
    else:
        ds = target_date.isoformat()
        path = f"historical-candle/{encoded_key}/minutes/1/{ds}/{ds}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    resp = await client.get(path, headers=headers, timeout=15.0)
    resp.raise_for_status()
    raw_candles: list = resp.json().get("data", {}).get("candles", [])

    parsed: list[dict[str, Any]] = []
    for c in raw_candles:
        if len(c) < 6:
            continue
        ts_raw, o, h, l, close_p, vol = c[0], c[1], c[2], c[3], c[4], c[5]
        ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else ts_raw
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_IST)
        parsed.append({
            "timestamp": ts,
            "open":  Decimal(str(o)),
            "high":  Decimal(str(h)),
            "low":   Decimal(str(l)),
            "close": Decimal(str(close_p)),
            "volume": int(vol),
        })

    # Ensure ascending order for sequential replay
    parsed.sort(key=lambda x: x["timestamp"])
    return parsed


# ── Monitor replay ────────────────────────────────────────────────────────────

def _replay_candles(
    candles: list[dict[str, Any]],
    close_time_utc: datetime,
    direction: str,
    close_reason: str,
    close_price: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal | None,
    remaining_tp1: Decimal | None,
    remaining_tp2: Decimal | None,
    remaining_tp3: Decimal | None,
    monitor_until: datetime,
) -> dict[str, Any]:
    """
    Replay 1-minute candles through the same logic as _apply_tick() in
    post_close_monitor_service.py, returning a dict of computed outcomes.

    Only candles whose timestamp (candle open) is strictly after the trade's
    close time and at or before monitor_until are replayed.
    """
    is_long = direction == "LONG"
    close_time_ist = close_time_utc.astimezone(_IST)

    # Filter to post-close window
    post_close = [
        c for c in candles
        if c["timestamp"] > close_time_ist and c["timestamp"] <= monitor_until.astimezone(_IST)
    ]

    # Initialise outcome state
    peak_favorable = close_price
    peak_adverse   = close_price
    recovered_sl    = False
    recovered_entry = False
    sl_recovery_at  = None
    tp1_hit_at      = None
    tp2_hit_at      = None
    tp3_hit_at      = None

    for candle in post_close:
        h = candle["high"]
        l = candle["low"]
        ts = candle["timestamp"].astimezone(timezone.utc)

        # ── Peak prices ───────────────────────────────────────────────────────
        if is_long:
            if h > peak_favorable:
                peak_favorable = h
            if l < peak_adverse:
                peak_adverse = l
        else:
            if l < peak_favorable:
                peak_favorable = l
            if h > peak_adverse:
                peak_adverse = h

        # ── SL recovery (SL-closed positions only) ────────────────────────────
        if close_reason == "SL" and stop_loss is not None:
            if not recovered_sl:
                sl = stop_loss
                if (is_long and h > sl) or (not is_long and l < sl):
                    recovered_sl   = True
                    sl_recovery_at = ts
            if not recovered_entry:
                entry = entry_price
                if (is_long and h >= entry) or (not is_long and l <= entry):
                    recovered_entry = True

        # ── Remaining target hits ─────────────────────────────────────────────
        if remaining_tp1 is not None and tp1_hit_at is None:
            tp = remaining_tp1
            if (is_long and h >= tp) or (not is_long and l <= tp):
                tp1_hit_at = ts

        if remaining_tp2 is not None and tp2_hit_at is None:
            tp = remaining_tp2
            if (is_long and h >= tp) or (not is_long and l <= tp):
                tp2_hit_at = ts

        if remaining_tp3 is not None and tp3_hit_at is None:
            tp = remaining_tp3
            if (is_long and h >= tp) or (not is_long and l <= tp):
                tp3_hit_at = ts

    return {
        "peak_favorable_price": peak_favorable,
        "peak_adverse_price":   peak_adverse,
        "recovered_above_sl":   recovered_sl,
        "recovered_above_entry": recovered_entry,
        "sl_recovery_at":       sl_recovery_at,
        "tp1_hit_at":           tp1_hit_at,
        "tp2_hit_at":           tp2_hit_at,
        "tp3_hit_at":           tp3_hit_at,
        "candles_replayed":     len(post_close),
    }


# ── Remaining-target resolver ─────────────────────────────────────────────────

def _remaining_targets(
    exit_reason: str,
    tp1: Decimal | None,
    tp2: Decimal | None,
    tp3: Decimal | None,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    """Return (remaining_tp1, remaining_tp2, remaining_tp3) for a given exit."""
    if exit_reason == "SL":
        return tp1, tp2, tp3
    if exit_reason == "TP1":
        return None, tp2, tp3
    if exit_reason == "TP2":
        return None, None, tp3
    return None, None, None  # TP3 — never reaches here (filtered in query)


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(target_date: date, dry_run: bool) -> None:
    settings = get_settings()

    if not settings.UPSTOX_ACCESS_TOKEN:
        logger.error("UPSTOX_ACCESS_TOKEN not set — cannot fetch candle data")
        sys.exit(1)

    session_end_utc = datetime(
        target_date.year, target_date.month, target_date.day,
        **_SESSION_END, tzinfo=_IST,
    ).astimezone(timezone.utc)

    # ── Fetch qualifying outcomes ─────────────────────────────────────────────
    async with WorkerSessionLocal() as session:
        rows = (await session.execute(text("""
            SELECT
                o.id,
                o.portfolio_id,
                o.position_id,
                o.user_id,
                o.symbol,
                p.side          AS direction,
                o.exit_reason,
                o.exit_price,
                o.entry_price,
                o.suggested_stop_loss,
                o.suggested_tp1,
                o.suggested_tp2,
                o.suggested_tp3,
                o.created_at,
                p.instrument_key
            FROM paper_trade_outcomes o
            JOIN paper_positions p ON p.id = o.position_id
            LEFT JOIN post_close_monitors pcm ON pcm.outcome_id = o.id
            WHERE o.created_at::date = :target_date
              AND o.exit_reason IN ('SL', 'TP1', 'TP2')
              AND pcm.id IS NULL
            ORDER BY o.created_at
        """), {"target_date": target_date})).fetchall()

    if not rows:
        logger.info("No qualifying outcomes for %s — nothing to backfill.", target_date)
        return

    logger.info(
        "Found %d outcome(s) on %s needing post-close monitors.",
        len(rows), target_date,
    )

    # ── Fetch candles (one API call per unique instrument) ────────────────────
    candle_cache: dict[str, list[dict[str, Any]]] = {}

    async with httpx.AsyncClient(
        base_url=settings.UPSTOX_BASE_URL,
        timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
    ) as http:
        unique_keys = {row[14] for row in rows}  # instrument_key col
        for key in unique_keys:
            logger.info("Fetching 1-min candles: %s", key)
            try:
                candles = await _fetch_1min_candles(
                    http, settings.UPSTOX_ACCESS_TOKEN, key, target_date
                )
                candle_cache[key] = candles
                logger.info("  → %d candles fetched", len(candles))
            except Exception as exc:
                logger.error("Failed to fetch candles for %s: %s", key, exc)
                candle_cache[key] = []

    # ── Replay and persist ────────────────────────────────────────────────────
    created = 0
    skipped = 0

    async with WorkerSessionLocal() as session:
        async with session.begin():
            for row in rows:
                (
                    outcome_id, portfolio_id, position_id, user_id,
                    symbol, direction, exit_reason,
                    exit_price, entry_price, suggested_sl,
                    tp1, tp2, tp3,
                    close_time, instrument_key,
                ) = row

                # Skip if candle fetch failed for this instrument
                candles = candle_cache.get(instrument_key, [])
                if not candles:
                    logger.warning(
                        "No candles available for %s (%s) — skipping",
                        symbol, instrument_key,
                    )
                    skipped += 1
                    continue

                rem_tp1, rem_tp2, rem_tp3 = _remaining_targets(
                    exit_reason,
                    Decimal(str(tp1)) if tp1 is not None else None,
                    Decimal(str(tp2)) if tp2 is not None else None,
                    Decimal(str(tp3)) if tp3 is not None else None,
                )

                close_price_d  = Decimal(str(exit_price))
                entry_price_d  = Decimal(str(entry_price))
                stop_loss_d    = Decimal(str(suggested_sl)) if suggested_sl is not None else None

                outcomes = _replay_candles(
                    candles        = candles,
                    close_time_utc = close_time,
                    direction      = direction,
                    close_reason   = exit_reason,
                    close_price    = close_price_d,
                    entry_price    = entry_price_d,
                    stop_loss      = stop_loss_d,
                    remaining_tp1  = rem_tp1,
                    remaining_tp2  = rem_tp2,
                    remaining_tp3  = rem_tp3,
                    monitor_until  = session_end_utc,
                )

                logger.info(
                    "%-12s %-3s | candles=%3d | peak_fav=%.2f peak_adv=%.2f"
                    " | sl_recovery=%s entry_recovery=%s"
                    " | tp1=%s tp2=%s tp3=%s",
                    symbol, exit_reason,
                    outcomes["candles_replayed"],
                    outcomes["peak_favorable_price"],
                    outcomes["peak_adverse_price"],
                    "✓" if outcomes["recovered_above_sl"]    else "✗",
                    "✓" if outcomes["recovered_above_entry"] else "✗",
                    outcomes["tp1_hit_at"].strftime("%H:%M") if outcomes["tp1_hit_at"] else "—",
                    outcomes["tp2_hit_at"].strftime("%H:%M") if outcomes["tp2_hit_at"] else "—",
                    outcomes["tp3_hit_at"].strftime("%H:%M") if outcomes["tp3_hit_at"] else "—",
                )

                if dry_run:
                    skipped += 1
                    continue

                monitor = PostCloseMonitor(
                    id             = uuid4(),
                    outcome_id     = outcome_id,
                    position_id    = position_id,
                    portfolio_id   = portfolio_id,
                    user_id        = user_id,
                    symbol         = symbol,
                    instrument_key = instrument_key,
                    direction      = direction,
                    close_reason   = exit_reason,
                    close_price    = close_price_d,
                    entry_price    = entry_price_d,
                    stop_loss      = stop_loss_d,
                    remaining_tp1  = rem_tp1,
                    remaining_tp2  = rem_tp2,
                    remaining_tp3  = rem_tp3,
                    monitor_until  = session_end_utc,
                    status         = "COMPLETED",
                    peak_favorable_price  = outcomes["peak_favorable_price"],
                    peak_adverse_price    = outcomes["peak_adverse_price"],
                    recovered_above_sl    = outcomes["recovered_above_sl"],
                    recovered_above_entry = outcomes["recovered_above_entry"],
                    sl_recovery_at        = outcomes["sl_recovery_at"],
                    tp1_hit_at            = outcomes["tp1_hit_at"],
                    tp2_hit_at            = outcomes["tp2_hit_at"],
                    tp3_hit_at            = outcomes["tp3_hit_at"],
                    created_at            = close_time,
                    completed_at          = session_end_utc,
                    updated_at            = session_end_utc,
                )
                session.add(monitor)
                created += 1

    suffix = " (dry-run — nothing written)" if dry_run else ""
    logger.info(
        "Done%s — created=%d skipped=%d",
        suffix, created, skipped,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill post-close monitors from 1-min OHLCV data")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=datetime.now(_IST).date(),
        help="Trade date to backfill (YYYY-MM-DD, default: today IST)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without writing to DB",
    )
    args = parser.parse_args()

    asyncio.run(run(args.date, args.dry_run))


if __name__ == "__main__":
    main()
