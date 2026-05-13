#!/usr/bin/env python3
"""
backfill_confidence_scores.py
==============================
Re-runs ML inference with the corrected pipeline and updates confidence_score
+ ml_predictions for all ai_trading_signals affected by the pre-2026-05-14 bugs.

Why this is needed
------------------
Three compounding bugs suppressed ML confidence to near-random levels (~0.0–0.02):

  1. Inference skipped rolling z-score normalization (window=60) that was applied
     during training.  Raw feature values (RSI≈68, EMA≈₹1450, volume_ratio≈2.3)
     are far outside the [-5, 5] trained range → model probabilities ≈ 0.50–0.53.

  2. Raw probability (floor 0.50 for binary classifier) was stored as "confidence"
     instead of conviction_scale (range 0→1, mapped linearly from threshold→1.0).
     A model returning 0.54 raw probability maps to conviction_scale ≈ 0.0.

  3. NSE trading symbols ("RELIANCE") were passed directly to the OHLCV and
     feature-store queries that expect Upstox instrument_keys
     ("NSE_EQ|INE002A01018"), causing feature loading to silently fail for all
     scheduled universe symbols.

Reconstruction algorithm
------------------------
Surgical replacement of the ML contribution avoids replaying the full
assembly pipeline (which would require re-querying events at historical
timestamps):

  old_fused = w_ev * ev_conf + w_ml * old_ml_conf + w_tech * tech_conf

Solving for ev_conf (the event component, not individually stored):

  ev_conf = (old_fused - w_ml * old_ml_conf - w_tech * tech_conf) / w_ev

New fused confidence with corrected ML conviction_scale:

  new_fused = w_ev_new * ev_conf + w_ml_new * new_conviction + w_tech_new * tech_conf

Weights are the renormalized SignalAssembler weights (base: event=0.35, ml=0.40,
technical=0.25), adjusted for per-signal source availability flags stored in the
ml_predictions and technical_indicators JSONB columns.

Idempotency
-----------
Each updated signal receives a 'backfill' entry in its metadata column:
  {"backfill": {"run_id": "...", "old_confidence": ..., "new_confidence": ..., ...}}
Signals that already carry a 'backfill' key are skipped on subsequent runs unless
--force is given.

Usage
-----
  # Preview (no DB writes):
  python scripts/backfill_confidence_scores.py --dry-run

  # Backfill all signals:
  python scripts/backfill_confidence_scores.py

  # Signals since a specific date (ISO 8601):
  python scripts/backfill_confidence_scores.py --since 2026-01-01T00:00:00Z

  # Specific symbols only:
  python scripts/backfill_confidence_scores.py --symbols RELIANCE,TCS,INFY

  # Re-process signals already backfilled in a prior run:
  python scripts/backfill_confidence_scores.py --force

  # Tune concurrency / batch size:
  python scripts/backfill_confidence_scores.py --batch-size 100 --concurrency 30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import signal as _signal
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

# ── Path bootstrap — allow running from any directory inside the repo ──────────
_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# ── Fusion constants — must mirror SignalAssembler exactly ─────────────────────
_BASE_WEIGHTS: dict[str, float] = {"event": 0.35, "ml": 0.40, "technical": 0.25}
_SINGLE_SOURCE_CAP: float = 0.65   # fuse_signals cap when only 1 source is available
_DP2 = Decimal("0.01")             # Numeric(5, 2) quantisation step

_LOG_INTERVAL = 25                  # log a progress line every N signals

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_confidence_scores")


# ══════════════════════════════════════════════════════════════════════════════
# Data classes
# ══════════════════════════════════════════════════════════════════════════════

@dataclass(slots=True)
class _SignalRecord:
    """Lightweight in-memory representation of a signal row."""
    id:                   int
    symbol:               str
    signal_timestamp:     datetime
    old_confidence:       float          # float(confidence_score)
    ml_predictions:       dict[str, Any]
    technical_indicators: dict[str, Any]
    contributing_events:  list[Any]


@dataclass(slots=True)
class _SignalUpdate:
    """Fully computed update ready to be written to the DB."""
    signal_id:          int
    old_confidence:     float
    new_confidence:     float           # rounded to 4dp before DB write to 2dp
    new_ml_predictions: dict[str, Any]
    meta_patch:         dict[str, Any]  # merged into metadata column


@dataclass
class _RunStats:
    """Mutable statistics accumulated across all batches."""
    total_fetched:               int   = 0
    skipped_already_done:        int   = 0
    skipped_ml_failed:           int   = 0
    skipped_reconstruction_err:  int   = 0
    updated:                     int   = 0
    delta_sum:                   float = 0.0   # Σ(new - old) for updated signals

    @property
    def avg_delta(self) -> float:
        return self.delta_sum / self.updated if self.updated else 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Fusion weight helpers  (mirror of SignalAssembler._renormalize_weights)
# ══════════════════════════════════════════════════════════════════════════════

def _renormalize_weights(
    event_available: bool,
    ml_available:    bool,
    tech_available:  bool,
) -> dict[str, float]:
    """
    Compute renormalized fusion weights given per-source availability.

    Unavailable sources receive weight 0.0 and their share is redistributed
    proportionally among the available ones so weights always sum to 1.0.
    When *all* sources are unavailable the base weights are returned unchanged
    (caller will produce a zero fused signal regardless).
    """
    sources = {
        "event":     (_BASE_WEIGHTS["event"],     event_available),
        "ml":        (_BASE_WEIGHTS["ml"],        ml_available),
        "technical": (_BASE_WEIGHTS["technical"], tech_available),
    }
    denom = sum(w for w, avail in sources.values() if avail)
    if denom == 0.0:
        return {k: w for k, (w, _) in sources.items()}
    return {k: (w / denom if avail else 0.0) for k, (w, avail) in sources.items()}


def _reconstruct_confidence(sig: _SignalRecord, new_conviction: float) -> float:
    """
    Algebraically reconstruct the corrected fused confidence for a signal.

    Steps:
      1. Read old ML/technical state from stored JSONB columns.
      2. Compute the renormalized weights that were used at signal-assembly time.
      3. Solve for ev_conf (event contribution) from the stored fused value.
      4. Recompute fused confidence with new_conviction replacing the ML term.
      5. Apply single-source cap and clamp to [0, 1].

    The confidence_score column is Numeric(5, 2) — 2 decimal places.  The
    recovered ev_conf inherits up to ±0.005 rounding error per component.
    Edge cases (single-source cap, fully-unavailable sources) are handled
    without any division-by-zero risk.
    """
    ml_preds = sig.ml_predictions
    tech     = sig.technical_indicators

    old_ml_conf      = float(ml_preds.get("confidence", 0.0))
    old_ml_available = bool(ml_preds.get("available", False))
    tech_conf        = float(tech.get("confidence", 0.0)) if tech else 0.0
    tech_available   = bool((tech or {}).get("available", False))
    # event_available: gather_event_signals only sets available=True when
    # rows > 0, and stores up to 5 events in contributing_events.
    event_available  = bool(sig.contributing_events)

    old_stored = sig.old_confidence

    # ── Recover ev_conf from the stored fused value ────────────────────────────
    old_w = _renormalize_weights(event_available, old_ml_available, tech_available)

    if old_w["event"] > 1e-8:
        ev_conf_raw = (
            old_stored
            - old_w["ml"]        * old_ml_conf
            - old_w["technical"] * tech_conf
        ) / old_w["event"]

        # Clamp — 2dp rounding in the stored value can push ev_conf slightly
        # outside [0, 1], especially when the single-source cap (0.65) was active.
        ev_conf = max(0.0, min(1.0, ev_conf_raw))

        if abs(ev_conf - ev_conf_raw) > 0.05:
            logger.debug(
                "Signal %d: ev_conf clamped %.4f→%.4f "
                "(2dp precision loss or single-source cap edge case)",
                sig.id, ev_conf_raw, ev_conf,
            )
    else:
        # Events were unavailable — their weight was 0; component is irrelevant.
        ev_conf = 0.0

    # ── Recompute fused confidence with corrected ML ───────────────────────────
    # ML is now available (inference succeeded — otherwise this function is not called).
    new_w = _renormalize_weights(event_available, True, tech_available)

    new_fused = (
        new_w["event"]     * ev_conf
        + new_w["ml"]      * new_conviction
        + new_w["technical"] * tech_conf
    )

    # Apply single-source cap consistently with fuse_signals
    n_available = int(event_available) + 1 + int(tech_available)  # ML always available
    if n_available == 1:
        new_fused = min(new_fused, _SINGLE_SOURCE_CAP)

    return max(0.0, min(1.0, new_fused))


# ══════════════════════════════════════════════════════════════════════════════
# Ensemble loading
# ══════════════════════════════════════════════════════════════════════════════

async def _load_ensemble() -> tuple[Any, int, int, tuple[str, ...]]:
    """
    Load the production XGBoost + GRU ensemble from the model registry.

    Mirrors the main.py lifespan logic — bootstrap_production_models ensures
    the DB record is healthy before RegistryModelLoader attempts the load.

    Redis cache is intentionally disabled (cache=None) for the backfill so that
    fresh inference is always performed instead of returning stale predictions
    from the pre-fix pipeline that may still live in the Redis cache.

    Returns:
        (predictor, n_features, sequence_length, feature_names)
    """
    from app.core.database import AsyncSessionLocal
    from app.ml.inference.ensemble_predictor import EnsemblePredictor
    from app.ml.inference.registry_loader import (
        RegistryModelLoader,
        bootstrap_production_models,
    )

    logger.info("Loading production ML ensemble from registry…")

    async with AsyncSessionLocal() as session:
        await bootstrap_production_models(session)
        loader   = RegistryModelLoader(session=session, num_threads=4, use_gpu=False)
        ensemble = await loader.load_production_ensemble()

    predictor = EnsemblePredictor.from_loaded_ensemble(ensemble, cache=None)

    logger.info(
        "Ensemble ready: XGBoost v%s (%.0f%%) + GRU v%s (%.0f%%)  "
        "n_features=%d  manifest=%s",
        ensemble.xgboost_version, ensemble.xgboost_weight * 100,
        ensemble.gru_version,     ensemble.gru_weight      * 100,
        ensemble.n_features,
        "yes" if ensemble.feature_names else "no — pipeline-level defaults will be used",
    )
    return (
        predictor,
        ensemble.n_features,
        ensemble.sequence_length,
        ensemble.feature_names,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Signal fetching  (keyset pagination — stable under concurrent inserts)
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_signal_batch(
    session:    AsyncSession,
    after_id:   int,
    batch_size: int,
    since:      datetime | None,
    until:      datetime | None,
    symbols:    list[str] | None,
    force:      bool,
) -> list[_SignalRecord]:
    """
    Fetch the next page of signals ordered by id ASC (id > after_id).

    Keyset pagination on the immutable primary key is faster than OFFSET and
    remains correct if new signals are inserted during the backfill run.

    The `force=False` filter excludes signals that already carry a 'backfill'
    key in their metadata column, making the run safely re-entrant.
    """
    conditions: list[str] = ["id > :after_id"]
    params: dict[str, Any] = {"after_id": after_id, "batch_size": batch_size}

    if since:
        conditions.append("signal_timestamp >= :since")
        params["since"] = since
    if until:
        conditions.append("signal_timestamp < :until")
        params["until"] = until
    if symbols:
        conditions.append("symbol = ANY(:symbols)")
        params["symbols"] = symbols
    if not force:
        # Skip signals already processed by a previous run of this script.
        conditions.append("(metadata IS NULL OR NOT (metadata ? 'backfill'))")

    stmt = text(
        "SELECT id, symbol, signal_timestamp, confidence_score, "
        "       ml_predictions, technical_indicators, contributing_events "
        "FROM   ai_trading_signals "
        f"WHERE  {' AND '.join(conditions)} "
        "ORDER  BY id ASC "
        "LIMIT  :batch_size"
    )

    rows = (await session.execute(stmt, params)).fetchall()

    return [
        _SignalRecord(
            id=row[0],
            symbol=row[1],
            signal_timestamp=row[2],
            old_confidence=float(row[3]),
            ml_predictions=row[4] or {},
            technical_indicators=row[5] or {},
            contributing_events=row[6] or [],
        )
        for row in rows
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Feature loading  (bounded concurrency, one DB session per symbol)
# ══════════════════════════════════════════════════════════════════════════════

async def _load_features_for_symbols(
    symbols:         list[str],
    n_features:      int,
    sequence_length: int,
    feature_names:   tuple[str, ...],
    concurrency:     int,
) -> dict[str, dict[str, Any]]:
    """
    Load features for every symbol in parallel, bounded by a semaphore.

    Each symbol gets its own DB session so that the feature loader's
    instrument_key resolution query does not contend with other symbols.
    The module-level _INSTRUMENT_KEY_CACHE in feature_loader.py accumulates
    across symbols within the process lifetime, eliminating repeat DB lookups.

    Redis is not needed by FeatureLoader (it stores the field but never reads it).

    Returns mapping symbol → feature dict.  Symbols that fail are omitted.
    """
    from app.core.database import AsyncSessionLocal
    from app.ml.inference.feature_loader import FeatureLoader

    sem     = asyncio.Semaphore(concurrency)
    results: dict[str, dict[str, Any]] = {}

    async def _load_one(symbol: str) -> None:
        async with sem:
            try:
                async with AsyncSessionLocal() as db:
                    loader = FeatureLoader(
                        db=db,
                        redis=None,               # unused by FeatureLoader internals
                        sequence_length=sequence_length,
                        n_features=n_features,
                        feature_names=feature_names,
                    )
                    tabular, sequence, current_price, volatility = (
                        await loader.load_features(symbol=symbol, timeframe="1d")
                    )
                results[symbol] = {
                    "symbol":        symbol,
                    "tabular":       tabular,
                    "sequence":      sequence,
                    "current_price": current_price,
                    "volatility":    volatility,
                    "timeframe":     "1d",
                }
                logger.debug("Features loaded: %s", symbol)
            except Exception as exc:
                logger.warning("Feature load failed for %s: %s", symbol, exc)

    await asyncio.gather(*[_load_one(sym) for sym in symbols])
    return results


# ══════════════════════════════════════════════════════════════════════════════
# DB update  (executemany — single round-trip per batch)
# ══════════════════════════════════════════════════════════════════════════════

async def _apply_updates(
    session: AsyncSession,
    updates: list[_SignalUpdate],
    dry_run: bool,
) -> int:
    """
    Persist all updates for the current batch in a single transaction.

    Uses SQLAlchemy executemany (list of param dicts) which asyncpg executes
    as a single prepared statement with N parameterized invocations — one
    network round-trip per batch regardless of batch size.

    Returns the number of signals written (0 on dry-run).
    """
    if not updates:
        return 0

    if dry_run:
        delta = sum(u.new_confidence - u.old_confidence for u in updates)
        logger.info(
            "  [dry-run] %d signal(s) would be updated  "
            "avg Δconfidence=%+.3f",
            len(updates),
            delta / len(updates),
        )
        return 0

    params_list = [
        {
            "id": u.signal_id,
            "confidence": float(
                Decimal(str(round(u.new_confidence, 6)))
                .quantize(_DP2, rounding=ROUND_HALF_UP)
            ),
            "ml_predictions": json.dumps(u.new_ml_predictions),
            "meta_patch":     json.dumps({"backfill": u.meta_patch}),
        }
        for u in updates
    ]

    await session.execute(
        text("""
            UPDATE ai_trading_signals
            SET
                confidence_score = :confidence,
                ml_predictions   = CAST(:ml_predictions AS jsonb),
                metadata         = COALESCE(metadata, '{}') || CAST(:meta_patch AS jsonb)
            WHERE id = :id
        """),
        params_list,
    )
    await session.commit()
    return len(updates)


# ══════════════════════════════════════════════════════════════════════════════
# Core orchestration
# ══════════════════════════════════════════════════════════════════════════════

async def run_backfill(
    dry_run:     bool,
    since:       datetime | None,
    until:       datetime | None,
    symbols:     list[str] | None,
    batch_size:  int,
    concurrency: int,
    force:       bool,
) -> _RunStats:
    """
    Main backfill loop.

    Architecture:
      For each page of signals (keyset-paginated):
        Phase A — Parallel feature loading  (I/O, one DB session per symbol)
        Phase B — Single batched ML inference (CPU/GPU)
        Phase C — Algebraic confidence reconstruction per signal
        Phase D — Atomic batch UPDATE + commit

    Handles SIGINT / SIGTERM gracefully: stops after the current batch and
    prints a final report.  Subsequent runs resume from where the previous
    one stopped (signals not yet having the 'backfill' metadata key).
    """
    from app.core.database import AsyncSessionLocal

    predictor, n_features, sequence_length, feature_names = await _load_ensemble()

    stats    = _RunStats()
    run_id   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    after_id = 0
    _interrupted = False

    def _handle_signal(sig, frame):          # noqa: ARG001
        nonlocal _interrupted
        logger.warning(
            "Interrupt received (signal %d) — finishing current batch then stopping.",
            sig,
        )
        _interrupted = True

    _signal.signal(_signal.SIGINT,  _handle_signal)
    _signal.signal(_signal.SIGTERM, _handle_signal)

    logger.info(
        "Run %s  |  dry_run=%s  since=%s  until=%s  symbols=%s  "
        "batch_size=%d  concurrency=%d  force=%s",
        run_id,
        dry_run,
        since.isoformat() if since else "all",
        until.isoformat() if until else "now",
        ",".join(symbols) if symbols else "all",
        batch_size,
        concurrency,
        force,
    )

    batch_num = 0

    while not _interrupted:
        # ── Fetch next page ────────────────────────────────────────────────────
        async with AsyncSessionLocal() as db:
            batch = await _fetch_signal_batch(
                session=db,
                after_id=after_id,
                batch_size=batch_size,
                since=since,
                until=until,
                symbols=symbols,
                force=force,
            )

        if not batch:
            logger.info("No more signals to process — backfill complete.")
            break

        batch_num   += 1
        after_id     = batch[-1].id
        stats.total_fetched += len(batch)

        unique_symbols = list({sig.symbol for sig in batch})
        logger.info(
            "Batch #%d  id_range=[%d, %d]  signals=%d  symbols=%d",
            batch_num,
            batch[0].id, batch[-1].id,
            len(batch),
            len(unique_symbols),
        )

        # ── Phase A: parallel feature loading ─────────────────────────────────
        feature_map = await _load_features_for_symbols(
            symbols=unique_symbols,
            n_features=n_features,
            sequence_length=sequence_length,
            feature_names=feature_names,
            concurrency=concurrency,
        )

        loaded_symbols = set(feature_map)
        if not loaded_symbols:
            logger.warning(
                "Batch #%d: features unavailable for all %d symbol(s) — skipping.",
                batch_num, len(unique_symbols),
            )
            stats.skipped_ml_failed += len(batch)
            continue

        ml_failed_symbols = set(unique_symbols) - loaded_symbols
        if ml_failed_symbols:
            logger.debug(
                "Batch #%d: %d symbol(s) had feature load failures: %s",
                batch_num,
                len(ml_failed_symbols),
                ", ".join(sorted(ml_failed_symbols)),
            )

        # ── Phase B: single batched ML inference ───────────────────────────────
        inference_items = list(feature_map.values())
        try:
            predictions = await predictor.predict_batch(inference_items)
        except Exception as exc:
            logger.error(
                "Batch #%d: ML inference failed (%s) — skipping %d signal(s)",
                batch_num, exc, len(batch),
            )
            stats.skipped_ml_failed += len(batch)
            continue

        pred_map: dict[str, dict[str, Any]] = {
            item["symbol"]: pred
            for item, pred in zip(inference_items, predictions)
            if pred is not None
        }

        # ── Phase C: per-signal confidence reconstruction ──────────────────────
        updates:  list[_SignalUpdate] = []
        now_utc = datetime.now(timezone.utc)

        for i, sig in enumerate(batch, start=1):
            if sig.symbol not in pred_map:
                stats.skipped_ml_failed += 1
                continue

            prediction    = pred_map[sig.symbol]
            new_conviction = float(prediction.get("conviction_scale", 0.0))

            try:
                new_confidence = _reconstruct_confidence(sig, new_conviction)
            except Exception as exc:
                logger.warning(
                    "Confidence reconstruction failed for signal %d (%s): %s",
                    sig.id, sig.symbol, exc,
                )
                stats.skipped_reconstruction_err += 1
                continue

            # Build updated ml_predictions: overwrite ML-specific fields while
            # preserving any other keys (e.g. "model") stored at assembly time.
            new_ml_predictions: dict[str, Any] = {
                **sig.ml_predictions,
                "confidence":       new_conviction,
                "conviction_scale": new_conviction,
                "available":        True,
                "prediction": {
                    "direction":    prediction.get("direction_label", "HOLD"),
                    "entry_price":  prediction.get("entry_price"),
                    "stop_loss":    prediction.get("stop_loss"),
                    "targets": [
                        prediction.get("tp1"),
                        prediction.get("tp2"),
                        prediction.get("tp3"),
                    ],
                    "probabilities": prediction.get("probabilities"),
                },
            }

            meta_patch: dict[str, Any] = {
                "run_id":           run_id,
                "old_confidence":   round(sig.old_confidence, 4),
                "new_confidence":   round(new_confidence, 4),
                "conviction_scale": round(new_conviction, 4),
                "updated_at":       now_utc.isoformat(),
                "script":           "backfill_confidence_scores.py",
            }

            updates.append(_SignalUpdate(
                signal_id=sig.id,
                old_confidence=sig.old_confidence,
                new_confidence=new_confidence,
                new_ml_predictions=new_ml_predictions,
                meta_patch=meta_patch,
            ))

            if i % _LOG_INTERVAL == 0:
                logger.info(
                    "  [%4d/%4d] id=%-8d %-15s  old=%.3f → new=%.3f  "
                    "conviction=%.3f",
                    i, len(batch),
                    sig.id, sig.symbol,
                    sig.old_confidence, new_confidence,
                    new_conviction,
                )

        # ── Phase D: atomic batch UPDATE ───────────────────────────────────────
        async with AsyncSessionLocal() as db:
            n_applied = await _apply_updates(db, updates, dry_run)

        n_written = len(updates) if dry_run else n_applied
        stats.updated   += n_written
        stats.delta_sum += sum(u.new_confidence - u.old_confidence for u in updates)

        delta_avg = (
            sum(u.new_confidence - u.old_confidence for u in updates) / len(updates)
            if updates else 0.0
        )
        logger.info(
            "Batch #%d complete: %d updated  %d ml_failed  avg Δconf=%+.3f",
            batch_num,
            len(updates),
            stats.skipped_ml_failed,
            delta_avg,
        )

    return stats


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Backfill corrected ML confidence scores on ai_trading_signals. "
            "See module docstring for full details."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without writing to the database.",
    )
    p.add_argument(
        "--since",
        metavar="ISO_DATETIME",
        default=None,
        help=(
            "Process only signals generated on or after this UTC datetime "
            "(e.g. 2026-01-01T00:00:00Z).  Defaults to all signals."
        ),
    )
    p.add_argument(
        "--until",
        metavar="ISO_DATETIME",
        default=None,
        help=(
            "Process only signals generated strictly before this UTC datetime.  "
            "Defaults to the current time (all unprocessed signals)."
        ),
    )
    p.add_argument(
        "--symbols",
        metavar="SYM1,SYM2,...",
        default=None,
        help="Comma-separated list of NSE trading symbols to restrict the backfill to.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=50,
        metavar="N",
        help="Number of signals fetched and processed per iteration (default: 50).",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=20,
        metavar="N",
        help=(
            "Maximum parallel feature-load DB sessions per batch (default: 20).  "
            "Each session fetches OHLCV + computes features for one symbol."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Re-process signals that were already backfilled in a previous run.  "
            "Without this flag, signals carrying a 'backfill' metadata key are skipped."
        ),
    )
    p.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Logging verbosity (default: INFO).",
    )
    return p.parse_args()


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S+00:00",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Cannot parse datetime: {value!r}  "
        "Expected ISO 8601, e.g. 2026-05-01T00:00:00Z"
    )


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    args = _parse_args()

    logging.getLogger().setLevel(getattr(logging, args.log_level))

    since_dt = _parse_datetime(args.since)
    until_dt = _parse_datetime(args.until)
    symbols_list: list[str] | None = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else None
    )

    started_at = datetime.now(timezone.utc)
    logger.info(
        "═══ backfill_confidence_scores  started=%s ═══",
        started_at.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )

    try:
        stats = asyncio.run(
            run_backfill(
                dry_run=args.dry_run,
                since=since_dt,
                until=until_dt,
                symbols=symbols_list,
                batch_size=args.batch_size,
                concurrency=args.concurrency,
                force=args.force,
            )
        )
    except Exception:
        logger.exception("Backfill aborted due to an unhandled exception.")
        sys.exit(1)

    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()

    logger.info(
        "\n"
        "══════════════════════════════════════════════════\n"
        "  Backfill %s\n"
        "  Elapsed              : %.1fs\n"
        "  Signals fetched      : %d\n"
        "  Signals updated      : %d%s\n"
        "  Skipped (prev run)   : %d\n"
        "  Skipped (ML failed)  : %d\n"
        "  Skipped (recon err)  : %d\n"
        "  Avg Δconfidence      : %+.4f\n"
        "══════════════════════════════════════════════════",
        "COMPLETE (dry-run — no writes)" if args.dry_run else "COMPLETE",
        elapsed,
        stats.total_fetched,
        stats.updated,
        " (dry-run)" if args.dry_run else "",
        stats.skipped_already_done,
        stats.skipped_ml_failed,
        stats.skipped_reconstruction_err,
        stats.avg_delta,
    )
