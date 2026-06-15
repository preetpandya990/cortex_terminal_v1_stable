"""
Gemini news-forecaster evaluation harness (Phase 4).
====================================================
A real-data, point-in-time backtest that answers the question gating any future
fine-tuning: does the Gemini news forecaster predict direction better than the
deterministic NLP baseline, does the NEWS actually add value over indicators
alone, and is it well-calibrated?

No synthetic data, no look-ahead:
  - indicators come from the historical feature store (ml_features) as-of the
    decision date — the exact labeled numbers the ML saw;
  - news comes from ai_event_classifications with created_at ≤ the decision date
    (via gather_event_signals(reference_time=...)), which also yields the
    deterministic NLP baseline score;
  - the realized outcome is the H-trading-day forward return from upstox_ohlcv.

For each sampled (symbol, date) point we run the forecaster twice — WITH news and
WITHOUT news (indicators only) — plus the NLP baseline, and score all three
against the realized direction.  Metrics (direction accuracy, Brier, ECE) use
app.ai.evaluation.forecast_metrics; uncomputable metrics are reported as null,
never fabricated.

Quota-aware + resumable: Gemini calls are paced (TokenBucket) and every scored
point is appended to a JSONL cache, so the run can be stopped/resumed and grown
over time or accelerated on a billing-enabled key.

Usage
-----
  cd backend
  .venv/bin/python -m scripts.eval_gemini_forecaster --n-points 40
  .venv/bin/python -m scripts.eval_gemini_forecaster --n-points 200 --rpm 1000   # paid key
  .venv/bin/python -m scripts.eval_gemini_forecaster --report-only                # metrics from cache
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.ai.evaluation.forecast_metrics import (
    DirectionMetrics,
    evaluate_directions,
    p_up_from_forecast,
    realized_up,
)
from app.ai.fusion.news_forecaster import generate_news_forecast
from app.ai.fusion.signal_assembler import SignalAssembler
from app.ai.intelligence.llm_client import CortexIntelligenceClient, close_intelligence_client
from app.ai.rag.backfill_service import TokenBucket
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.redis import close_redis, init_redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("eval_gemini_forecaster")

_CACHE = Path("logs/forecaster_eval_cache.jsonl")
_REPORT = Path("logs/forecaster_eval_report.json")


# ── Eval-set construction (point-in-time, real data) ────────────────────────────

_CANDIDATES_SQL = text("""
    SELECT im.instrument_key, sym, ec_ts
    FROM (
        -- One point per (symbol, day), anchored at the latest news timestamp of
        -- that day so gather_event_signals(reference_time=ec_ts) includes it.
        SELECT s AS sym, max(created_at) AS ec_ts
        FROM ai_event_classifications ec, LATERAL unnest(ec.affected_symbols) AS s
        WHERE ec.affected_symbols IS NOT NULL
          AND ec.created_at <= :cutoff   -- ≥ horizon realized trading bars exist after this
        GROUP BY s, date_trunc('day', created_at)
    ) e
    JOIN instrument_master im
      ON im.trading_symbol = e.sym AND im.exchange='NSE' AND im.instrument_type='EQ'
    WHERE EXISTS (
        SELECT 1 FROM ml_features mf
        WHERE mf.symbol = im.instrument_key AND mf.timestamp <= e.ec_ts
    )
    ORDER BY e.ec_ts DESC
    LIMIT :limit
""")


async def _eval_cutoff(db, horizon: int) -> datetime:
    """Latest decision time that already has ``horizon`` realized daily bars after it.

    Anchored to the actual OHLCV **data frontier** (the (horizon+1)-th most recent
    distinct 1D bar), so it is robust to the daily sync lagging wall-clock time.
    Re-running the eval after the sync advances moves this cutoff forward and the
    resumable cache then scores the newly-closed points ("check back later").
    """
    rows = (await db.execute(text(
        "SELECT DISTINCT timestamp FROM upstox_ohlcv WHERE timeframe='1D' "
        "ORDER BY timestamp DESC LIMIT :h"
    ), {"h": horizon + 1})).all()
    if len(rows) < horizon + 1:
        raise RuntimeError("Not enough daily OHLCV history to evaluate this horizon.")
    return rows[-1][0]


async def _indicators_asof(db, instrument_key: str, asof: datetime) -> dict | None:
    row = (await db.execute(text(
        """SELECT feature_vector FROM ml_features
           WHERE symbol = :k AND timestamp <= :t ORDER BY timestamp DESC LIMIT 1"""
    ), {"k": instrument_key, "t": asof})).scalar()
    if row is None:
        return None
    fv = json.loads(row) if isinstance(row, str) else row
    return {k: v for k, v in fv.items() if isinstance(v, (int, float))}


async def _forward_return(db, instrument_key: str, asof: datetime, horizon: int) -> float | None:
    """H-trading-day forward return from daily OHLCV, point-in-time (no look-ahead)."""
    entry = (await db.execute(text(
        """SELECT close FROM upstox_ohlcv WHERE instrument_key=:k AND timeframe='1D'
           AND timestamp <= :t ORDER BY timestamp DESC LIMIT 1"""
    ), {"k": instrument_key, "t": asof})).scalar()
    exit_rows = (await db.execute(text(
        """SELECT close FROM upstox_ohlcv WHERE instrument_key=:k AND timeframe='1D'
           AND timestamp > :t ORDER BY timestamp ASC LIMIT :h"""
    ), {"k": instrument_key, "t": asof, "h": horizon})).all()
    if entry is None or len(exit_rows) < horizon:
        return None
    entry_c, exit_c = float(entry), float(exit_rows[-1][0])
    if entry_c <= 0:
        return None
    return exit_c / entry_c - 1.0


def _nlp_p_up(event_signals: dict) -> float | None:
    """Map the deterministic NLP event score (−100..100) to a pseudo-P(up).

    The NLP baseline is not a calibrated probability; this linear map lets it be
    scored on the same axis (score 100 → 1.0, −100 → 0.0, 0/unavailable → abstain).
    """
    if not event_signals.get("available") or not event_signals.get("events"):
        return None
    score = float(event_signals.get("score", 0.0))
    if score == 0.0:
        return None
    return max(0.0, min(1.0, 0.5 + score / 200.0))


# ── Per-point scoring ───────────────────────────────────────────────────────────

async def _prepare_point(
    db, assembler: SignalAssembler, *,
    instrument_key: str, symbol: str, asof: datetime, horizon: int,
) -> dict | None:
    """DB-only point construction (no Gemini).  Returns None when not usable."""
    indicators = await _indicators_asof(db, instrument_key, asof)
    if not indicators:
        return None
    fwd = await _forward_return(db, instrument_key, asof, horizon)
    if fwd is None:
        return None
    event_signals = await assembler.gather_event_signals(db, symbol, reference_time=asof)
    events = event_signals.get("events") or []
    if not events:
        return None  # the forecaster only acts on news — skip no-news points
    return {
        "instrument_key": instrument_key,
        "symbol": symbol,
        "asof": asof,
        "indicators": indicators,
        "events": events,
        "forward_return": fwd,
        "nlp_p_up": _nlp_p_up(event_signals),
    }


async def _forecast_point(client, prepared: dict, *, timeout: float, max_tokens: int) -> dict:
    """Run the two Gemini forecasts (with / without news) and assemble the record."""
    async def _fc(evs):
        out, _ = await generate_news_forecast(
            client, symbol=prepared["symbol"], indicators=prepared["indicators"],
            events=evs, regime=None, timeout=timeout, max_tokens=max_tokens,
        )
        return out.direction, float(out.confidence)

    dir_w, conf_w = await _fc(prepared["events"])   # with news
    dir_wo, conf_wo = await _fc([])                 # without news (indicators only)
    fwd = prepared["forward_return"]
    return {
        "instrument_key": prepared["instrument_key"],
        "symbol": prepared["symbol"],
        "asof": prepared["asof"].isoformat(),
        "forward_return": fwd,
        "realized_up": realized_up(fwd),
        "with_news":    {"direction": dir_w,  "confidence": conf_w},
        "without_news": {"direction": dir_wo, "confidence": conf_wo},
        "nlp_p_up": prepared["nlp_p_up"],
        "scored_at": datetime.utcnow().isoformat(),
    }


# ── Cache ────────────────────────────────────────────────────────────────────────

def _load_cache() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if _CACHE.exists():
        for line in _CACHE.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                out[f"{r['instrument_key']}@{r['asof']}"] = r
    return out


def _append_cache(rec: dict) -> None:
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    with _CACHE.open("a") as f:
        f.write(json.dumps(rec) + "\n")


# ── Metrics assembly ──────────────────────────────────────────────────────────────

def _metrics(records: list[dict]) -> dict[str, Any]:
    realized = [r["realized_up"] for r in records]
    with_p   = [p_up_from_forecast(r["with_news"]["direction"], r["with_news"]["confidence"]) for r in records]
    without_p = [p_up_from_forecast(r["without_news"]["direction"], r["without_news"]["confidence"]) for r in records]
    nlp_p    = [r.get("nlp_p_up") for r in records]

    m_with: DirectionMetrics = evaluate_directions(with_p, realized)
    m_without: DirectionMetrics = evaluate_directions(without_p, realized)
    m_nlp: DirectionMetrics = evaluate_directions(nlp_p, realized)

    def _edge(a: float | None, b: float | None) -> float | None:
        return None if a is None or b is None else round(a - b, 4)

    return {
        "n_points": len(records),
        "gemini_with_news": m_with.as_dict(),
        "gemini_without_news": m_without.as_dict(),
        "nlp_baseline": m_nlp.as_dict(),
        "edges": {
            "news_value_add_accuracy": _edge(m_with.accuracy, m_without.accuracy),
            "gemini_vs_nlp_accuracy":  _edge(m_with.accuracy, m_nlp.accuracy),
            "news_value_add_brier":    _edge(m_without.brier, m_with.brier),  # lower is better
        },
    }


async def _explanation_quality(db) -> dict[str, Any]:
    """Explanation guardrail/citation quality from the audit log — no new LLM calls."""
    row = (await db.execute(text(
        """SELECT
             COUNT(*) AS n,
             COUNT(*) FILTER (WHERE guardrail_events @> '["price_prediction_filter"]'::jsonb) AS price_filtered,
             COUNT(*) FILTER (WHERE retrieved_source_ids IS NOT NULL) AS with_context,
             COUNT(*) FILTER (WHERE guardrail_events @> '["citation_missing"]'::jsonb) AS citation_missing,
             COUNT(*) FILTER (WHERE error_message IS NOT NULL) AS errors
           FROM ai_llm_audit_log WHERE invocation_type='explanation'"""
    ))).first()
    n, price_filtered, with_ctx, cite_missing, errors = (row or (0, 0, 0, 0, 0))
    n = n or 0
    return {
        "n_explanations": n,
        "price_prediction_filter_rate": round(price_filtered / n, 4) if n else None,
        "error_rate": round(errors / n, 4) if n else None,
        "n_with_retrieved_context": with_ctx,
        "citation_present_rate_when_context": (
            round(1 - cite_missing / with_ctx, 4) if with_ctx else None
        ),
    }


# ── Orchestration ──────────────────────────────────────────────────────────────────

async def _run(args) -> int:
    s = get_settings()
    if not s.GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY not set — aborting.")
        return 2
    from app.ai.intelligence.llm_client import get_intelligence_client

    await init_redis()
    await CortexIntelligenceClient.initialize()
    assembler = SignalAssembler()
    client = get_intelligence_client()

    cache = _load_cache()
    bucket = TokenBucket(args.rpm, capacity=2)  # 2 calls/point; empty-start

    try:
        if not args.report_only:
            async with AsyncSessionLocal() as db:
                cutoff = await _eval_cutoff(db, args.horizon_days)
                cands = (await db.execute(_CANDIDATES_SQL, {
                    "cutoff": cutoff, "limit": max(args.n_points * 6, 200),
                })).all()
            logger.info(
                "eval cutoff (OHLCV frontier − %d trading days) = %s | candidates: %d (target %d)",
                args.horizon_days, str(cutoff)[:16], len(cands), args.n_points,
            )

            scored = 0
            for ik, sym, ec_ts in cands:
                if scored >= args.n_points:
                    break
                key = f"{ik}@{ec_ts.isoformat()}"
                if key in cache:
                    continue
                async with AsyncSessionLocal() as db:
                    # Build the point first (DB only); only pay Gemini if it's usable.
                    try:
                        prepared = await _prepare_point(
                            db, assembler, instrument_key=ik, symbol=sym,
                            asof=ec_ts, horizon=args.horizon_days,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("point %s prep failed: %s", key, str(exc)[:120])
                        continue
                if prepared is None:
                    continue
                await bucket.acquire(2)
                try:
                    rec = await _forecast_point(client, prepared,
                                                timeout=args.timeout, max_tokens=args.max_tokens)
                except Exception as exc:  # noqa: BLE001 — skip a bad point, keep going
                    logger.warning("point %s forecast failed: %s", key, str(exc)[:120])
                    continue
                _append_cache(rec)
                cache[key] = rec
                scored += 1
                logger.info("scored %d/%d  %s %s  fwd=%.3f", scored, args.n_points,
                            sym, str(ec_ts)[:10], rec["forward_return"])

        records = list(cache.values())
        report: dict[str, Any] = {
            "generated_at": datetime.utcnow().isoformat(),
            "model": client.model_id,
            "horizon_days": args.horizon_days,
            "forecaster": _metrics(records) if records else {"n_points": 0},
        }
        async with AsyncSessionLocal() as db:
            report["explanations"] = await _explanation_quality(db)

        _REPORT.parent.mkdir(parents=True, exist_ok=True)
        _REPORT.write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
        return 0
    finally:
        await close_intelligence_client()
        await close_redis()


def main() -> None:
    p = argparse.ArgumentParser(description="Gemini news-forecaster evaluation backtest.")
    p.add_argument("--n-points", type=int, default=40, help="Target number of scored points.")
    p.add_argument("--horizon-days", type=int, default=5, help="Forward-return horizon (trading days).")
    p.add_argument("--rpm", type=int, default=60, help="Gemini calls/min pace (2 per point).")
    p.add_argument("--timeout", type=float, default=20.0, help="Per-forecast timeout (s); generous for eval.")
    p.add_argument("--max-tokens", type=int, default=400)
    p.add_argument("--report-only", action="store_true", help="Recompute metrics from cache, no new calls.")
    args = p.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
