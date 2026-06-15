"""
Capture a live Pathway-1 end-to-end emission (verification tool).
================================================================
Watches for brand-new scanner-triggered (TECHNICAL_FIRST) trade suggestions
created by the live correlation worker during an NSE session, and prints the full
evidence that the rebuilt Gemini layer flowed end to end:

    real scanner anomaly  →  ML ensemble  →  Gemini news forecast (AI slot)
        →  consensus ≥60  →  suggestion  →  Gemini explanation

For each new suggestion it shows the scanner signal that triggered it, the ML
ensemble read, the Gemini forecast in ai_signal (direction + rationale +
forecast_source), and then waits briefly for the async explanation worker to
write the Gemini explanation, printing the model + summary + sections.

This is a read-only observer — it makes NO LLM calls and creates nothing; it only
reports what the live system produced.  Run it during market hours with the
correlation worker + explanation worker running.

Usage
-----
  cd backend
  .venv/bin/python -m scripts.capture_live_emission                 # capture 1
  .venv/bin/python -m scripts.capture_live_emission --count 3       # capture 3
  .venv/bin/python -m scripts.capture_live_emission --since-existing # include the latest existing one (demo)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.database import AsyncSessionLocal

_COLS = (
    "id, suggestion_id, symbol, signal_direction, consensus_score, confidence_level, "
    "created_at, scanner_signal, ml_signal, ai_signal, llm_summary, llm_explanation, explanation_model"
)
_SELECT = text(
    f"SELECT {_COLS} FROM trade_suggestions "
    "WHERE trigger_pathway = 'TECHNICAL_FIRST' AND created_at >= :since ORDER BY created_at ASC"
)
_BY_ID = text(f"SELECT {_COLS} FROM trade_suggestions WHERE suggestion_id = :sid")


def _g(d, *path, default=None):
    cur = d or {}
    for k in path:
        cur = (cur or {}).get(k) if isinstance(cur, dict) else None
        if cur is None:
            return default
    return cur


def _report(row) -> None:
    (sid_i, sid, symbol, direction, consensus, conf_level, created_at,
     scanner, ml, ai, summary, explanation, model) = row
    print("\n" + "=" * 78)
    print(f"  LIVE PATHWAY-1 EMISSION  {symbol}  {direction}  "
          f"consensus={float(consensus):.1f}  conf={conf_level}  @ {str(created_at)[:19]}")
    print("-" * 78)
    print(f"  Scanner anomaly : signal={scanner.get('signal') or scanner.get('direction')} "
          f"score={scanner.get('score')} rsi={scanner.get('rsi')} vol_ratio={scanner.get('volume_ratio')}")
    print(f"  ML ensemble     : dir={_g(ml,'prediction','direction')} conf={ml.get('confidence')} "
          f"model={ml.get('model')}")
    print(f"  Gemini forecast : dir={ai.get('direction')} conf={ai.get('confidence')} "
          f"source={ai.get('forecast_source')}")
    if ai.get("rationale"):
        print(f"      rationale   : {ai['rationale']}")
    # Gemini-layer verdict
    fc_ok = ai.get("forecast_source") == "gemini"
    ex_ok = bool(model) and str(model).startswith("gemini/")
    print(f"  GEMINI LAYER    : forecast={'gemini' if fc_ok else ai.get('forecast_source')} "
          f"| explanation={model or 'pending'}")
    if explanation:
        print(f"  Explanation ({model}):")
        print("    SUMMARY: " + (summary or "")[:300])
        head = "\n".join("    " + ln for ln in (explanation or "").splitlines()[:8])
        print(head)
    else:
        print("  Explanation     : not yet written (explanation worker still generating)")


async def _await_explanation(db, suggestion_id, wait_secs: int) -> tuple | None:
    """Re-read a captured suggestion until its explanation is written (or timeout)."""
    deadline = asyncio.get_event_loop().time() + wait_secs
    last = None
    while asyncio.get_event_loop().time() < deadline:
        row = (await db.execute(_BY_ID, {"sid": suggestion_id})).first()
        last = row
        if row and row[11]:  # llm_explanation populated
            return row
        await asyncio.sleep(3)
    return last


async def _run(args) -> int:
    since = (datetime.min if args.since_existing else datetime.now(timezone.utc)).replace(tzinfo=timezone.utc)
    if args.since_existing:
        # Only the single most-recent existing one, for a demo capture.
        async with AsyncSessionLocal() as db:
            r = (await db.execute(text(
                "SELECT created_at FROM trade_suggestions WHERE trigger_pathway='TECHNICAL_FIRST' "
                "ORDER BY created_at DESC LIMIT 1"))).scalar()
        since = (r - timedelta(seconds=1)) if r else since

    print(f"Watching for new TECHNICAL_FIRST emissions since {str(since)[:19]} "
          f"(target {args.count}, up to {args.max_minutes} min)…")
    seen: set[int] = set()
    captured = 0
    deadline = asyncio.get_event_loop().time() + args.max_minutes * 60

    while captured < args.count and asyncio.get_event_loop().time() < deadline:
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(_SELECT, {"since": since})).all()
            for row in rows:
                if row[0] in seen:
                    continue
                seen.add(row[0])
                full = await _await_explanation(db, row[1], args.explanation_wait_secs)
                _report(full or row)
                captured += 1
                if captured >= args.count:
                    break
        if captured < args.count:
            await asyncio.sleep(args.poll_secs)

    print("\n" + "=" * 78)
    print(f"Captured {captured}/{args.count} live Pathway-1 emission(s).")
    return 0 if captured else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Capture live Pathway-1 end-to-end emissions.")
    p.add_argument("--count", type=int, default=1, help="How many emissions to capture before exiting.")
    p.add_argument("--poll-secs", type=int, default=15, help="DB poll interval.")
    p.add_argument("--max-minutes", type=int, default=420, help="Max watch duration (one session).")
    p.add_argument("--explanation-wait-secs", type=int, default=60,
                   help="Per-suggestion wait for the async Gemini explanation to be written.")
    p.add_argument("--since-existing", action="store_true",
                   help="Capture the latest EXISTING emission immediately (demo, no waiting).")
    args = p.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
