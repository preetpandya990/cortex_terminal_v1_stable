"""
Feedback Weight Loader — Phase 2 of the ML Training Feedback Formatter.
========================================================================
Computes per-sample weights from realised paper-trade outcomes and merges
them into the production training pipeline.

Weight formula (B1 × B2, clipped to [0.1, 5.0]):

  outcome_factor (B1 — reinforce what paid):
    hit_tp3              → 3.0
    hit_tp2              → 2.0
    hit_tp1              → 1.5
    direction_correct    → 1.0 (no TP, no SL)
    hit_sl               → 0.5

  confidence_interaction_factor (B2 — mine hard negatives):
    confidence ≥ 0.70 AND NOT direction_correct → 2.0
    everything else                              → 1.0

Maturity gate:
  pto.created_at < NOW() - INTERVAL '1 day'
  AND pto.ml_direction_correct IS NOT NULL   ← proxy for "async ML fields computed"

Data path:
  paper_trade_outcomes (pto)
    INNER JOIN paper_positions      (pos)  ON pto.position_id  = pos.id
    LEFT  JOIN trade_suggestions    (ts)   ON pto.suggestion_id = ts.suggestion_id
  → per-outcome weights (instrument_key, signal_date, sample_weight)
  → aggregated by (instrument_key, signal_date) using mean
  → (instrument_key, signal_date, sample_weight, outcome_count) parquet

Trade coverage:
  Suggestion-backed trades:  instrument_key  = ts.instrument_key
                              signal_date     = DATE(ts.generated_at  AT TIME ZONE 'UTC')
                              confidence_score= ts.consensus_score
  Manual / scanner trades:   instrument_key  = pos.instrument_key
                              signal_date     = DATE(pos.opened_at    AT TIME ZONE 'UTC')
                              confidence_score= pto.suggestion_consensus_score (snapshotted
                                               from the matched AITradingSignal, or NULL
                                               → defaults to 50.0, neutral B2 factor)

Why INNER JOIN paper_positions (not trade_suggestions):
  paper_trade_outcomes.position_id is NOT NULL with ON DELETE RESTRICT — it always
  resolves. paper_trade_outcomes.suggestion_id is nullable (SET NULL) and is absent
  for every manually-opened position, making an INNER JOIN on that column permanently
  exclude a large share of the outcome population.

Aggregation:
  Multiple outcomes can exist for the same (instrument_key, signal_date) — e.g.
  two suggestion-backed trades on RELIANCE on 2026-05-01, or one suggestion + one
  manual trade on the same instrument on the same day. Taking the unweighted mean
  across all outcomes for a bar gives a balanced view of that bar's overall signal
  quality, and avoids over-weighting frequently-traded instruments.

  The raw outcome count is preserved in the parquet column `outcome_count` so
  operators can audit how many trades contributed to each bar's weight.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Bundle output directory ────────────────────────────────────────────────────
_DEFAULT_BUNDLES_DIR = Path(__file__).parent.parent.parent.parent / "feedback_bundles"

# ── Weight formula constants ───────────────────────────────────────────────────
_OUTCOME_FACTORS = {
    "tp3":              3.0,
    "tp2":              2.0,
    "tp1":              1.5,
    "direction_only":   1.0,
    "sl":               0.5,
    "unknown":          1.0,
}
_CONFIDENCE_HARD_NEGATIVE_FACTOR = 2.0
_CONFIDENCE_HIGH_THRESHOLD       = 0.70
_WEIGHT_CLIP_MIN = 0.1
_WEIGHT_CLIP_MAX = 5.0


# ── Public data structures ─────────────────────────────────────────────────────

@dataclass
class FeedbackBundleStats:
    """
    Metadata written alongside every parquet bundle.

    row_count          — number of unique (instrument_key, signal_date) rows in the parquet
    total_raw_outcomes — total closed outcomes that were aggregated into those rows
                         (= sum of outcome_count column; always >= row_count)
    """
    bundle_path:        str
    meta_path:          str
    sha256:             str
    row_count:          int     # unique (instrument_key, signal_date) pairs
    total_raw_outcomes: int     # sum of outcome_count across all rows
    window_start:       str     # ISO date — earliest signal_date in the bundle
    window_end:         str     # ISO date — latest  signal_date in the bundle
    created_at:         str     # ISO datetime UTC
    weight_mean:        float
    weight_std:         float
    weight_p5:          float
    weight_p50:         float
    weight_p95:         float
    histogram_bins:     List[float]         # 21 bin edges
    histogram_counts:   List[int]           # 20 bucket counts
    top_symbols:        List[Dict[str, Any]] # top-10 by mean weight

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ── Core weight computation ────────────────────────────────────────────────────

def _outcome_factor(
    hit_tp3: bool,
    hit_tp2: bool,
    hit_tp1: bool,
    hit_sl:  bool,
    direction_correct: Optional[bool],
) -> float:
    if hit_tp3: return _OUTCOME_FACTORS["tp3"]
    if hit_tp2: return _OUTCOME_FACTORS["tp2"]
    if hit_tp1: return _OUTCOME_FACTORS["tp1"]
    if hit_sl:  return _OUTCOME_FACTORS["sl"]
    if direction_correct: return _OUTCOME_FACTORS["direction_only"]
    return _OUTCOME_FACTORS["unknown"]


def _confidence_factor(confidence_01: float, direction_correct: Optional[bool]) -> float:
    if confidence_01 >= _CONFIDENCE_HIGH_THRESHOLD and not direction_correct:
        return _CONFIDENCE_HARD_NEGATIVE_FACTOR
    return 1.0


def compute_sample_weight(row: pd.Series) -> float:
    """Apply the B1×B2 formula to a single outcome row. Returns clipped weight."""
    conf_01 = float(row["confidence_score"]) / 100.0 if row["confidence_score"] is not None else 0.5
    of = _outcome_factor(
        hit_tp3=bool(row["hit_tp3"]),
        hit_tp2=bool(row["hit_tp2"]),
        hit_tp1=bool(row["hit_tp1"]),
        hit_sl =bool(row["hit_sl"]),
        direction_correct=row["ml_direction_correct"],
    )
    cf = _confidence_factor(conf_01, row["ml_direction_correct"])
    return float(np.clip(of * cf, _WEIGHT_CLIP_MIN, _WEIGHT_CLIP_MAX))


# ── DB query ───────────────────────────────────────────────────────────────────
#
# Join strategy
# ─────────────
# paper_trade_outcomes.position_id → paper_positions.id
#   INNER JOIN: position_id is NOT NULL with ON DELETE RESTRICT; always resolves.
#   Provides instrument_key (always set, NOT NULL) and opened_at (position open time).
#
# paper_trade_outcomes.suggestion_id → trade_suggestions.suggestion_id
#   LEFT JOIN: suggestion_id is nullable; absent for every manually-opened position.
#   When present: provides the canonical instrument_key + generated_at (suggestion
#   creation timestamp) + consensus_score.
#   When absent: we fall back to pos.instrument_key, pos.opened_at, and the
#   snapshotted pto.suggestion_consensus_score (populated from the matched
#   AITradingSignal by position_service._write_outcome if one was found).
#
# COALESCE priority
# ─────────────────
#   instrument_key : ts.instrument_key   → pos.instrument_key
#   signal_date    : ts.generated_at     → pos.opened_at        (UTC calendar date)
#   confidence     : ts.consensus_score  → pto.suggestion_consensus_score
#
# pos.opened_at is the correct fallback for signal_date because it is when the
# operator decided to act on the signal — the same calendar date as the OHLCV bar
# the signal was derived from. pos.closed_at would be WRONG (that is after the
# outcome is known, not when the signal fired).

_FEEDBACK_QUERY = text("""
SELECT
    COALESCE(ts.instrument_key,  pos.instrument_key)                     AS instrument_key,
    DATE(
        COALESCE(ts.generated_at, pos.opened_at) AT TIME ZONE 'UTC'
    )                                                                     AS signal_date,
    pto.ml_direction_correct,
    pto.hit_tp1,
    pto.hit_tp2,
    pto.hit_tp3,
    pto.hit_sl,
    COALESCE(ts.consensus_score, pto.suggestion_consensus_score)          AS confidence_score,
    pto.created_at                                                        AS closed_at
FROM  paper_trade_outcomes  pto
JOIN  paper_positions        pos ON pto.position_id   = pos.id
LEFT  JOIN trade_suggestions ts  ON pto.suggestion_id = ts.suggestion_id
WHERE pto.created_at                                   < NOW() - INTERVAL '1 day'
  AND pto.ml_direction_correct                         IS NOT NULL
  AND COALESCE(ts.instrument_key, pos.instrument_key)  IS NOT NULL
  AND COALESCE(ts.instrument_key, pos.instrument_key)  <> ''
ORDER BY instrument_key, signal_date
""")

# Column names that map 1-to-1 onto the SELECT list above.
_FEEDBACK_QUERY_COLUMNS = [
    "instrument_key", "signal_date", "ml_direction_correct",
    "hit_tp1", "hit_tp2", "hit_tp3", "hit_sl",
    "confidence_score", "closed_at",
]


async def build_feedback_weights_df(
    session: AsyncSession,
) -> pd.DataFrame:
    """
    Query matured outcomes, apply the B1×B2 weight formula, aggregate by
    (instrument_key, signal_date), and return a DataFrame with columns:
      instrument_key  str       — NSE instrument key (NSE_EQ|INE...)
      signal_date     date      — UTC calendar date of the originating bar
      sample_weight   float32   — mean B1×B2 weight for all outcomes on that bar
      outcome_count   int32     — number of raw outcomes aggregated into this row

    Aggregation rationale
    ─────────────────────
    Multiple closed positions can share the same (instrument_key, signal_date).
    Taking the unweighted mean of their individual weights gives a balanced view
    of that bar's overall signal quality and prevents high-frequency instruments
    from dominating the gradient signal relative to less-traded ones.

    Returns an empty DataFrame (with the correct column schema) when no matured
    outcomes pass the query filters.
    """
    result = await session.execute(_FEEDBACK_QUERY)
    rows = result.fetchall()

    if not rows:
        logger.warning(
            "build_feedback_weights: query returned 0 rows. "
            "Possible causes: (1) no closed positions older than 1 day with "
            "ml_direction_correct populated; (2) instrument_key absent from both "
            "trade_suggestions and paper_positions (data integrity issue). "
            "Check that compute_ml_feedback background tasks are completing "
            "successfully (look for MLFeedbackError records)."
        )
        return pd.DataFrame(
            columns=["instrument_key", "signal_date", "sample_weight", "outcome_count"]
        )

    df_raw = pd.DataFrame(rows, columns=_FEEDBACK_QUERY_COLUMNS)

    # ── Per-outcome weight (B1 × B2, clipped) ─────────────────────────────────
    df_raw["sample_weight"] = df_raw.apply(compute_sample_weight, axis=1).astype(np.float32)

    # ── Aggregate by (instrument_key, signal_date) ────────────────────────────
    # mean: balanced view of that bar's signal quality across all trades
    # count: number of raw outcomes (audit trail for each aggregated weight)
    df = (
        df_raw
        .groupby(["instrument_key", "signal_date"], as_index=False)
        .agg(
            sample_weight=("sample_weight", "mean"),
            outcome_count=("sample_weight", "count"),
        )
    )
    df["sample_weight"] = df["sample_weight"].astype(np.float32)
    df["outcome_count"]  = df["outcome_count"].astype(np.int32)

    n_raw        = len(df_raw)
    n_aggregated = len(df)
    logger.info(
        "Feedback weights built: %d raw outcomes → %d unique (instrument_key, signal_date) rows  "
        "window=[%s, %s]  weight_mean=%.3f  weight_std=%.3f",
        n_raw, n_aggregated,
        df["signal_date"].min(),
        df["signal_date"].max(),
        df["sample_weight"].mean(),
        df["sample_weight"].std(),
    )

    return df


# ── Bundle I/O ─────────────────────────────────────────────────────────────────

def write_bundle(
    df: pd.DataFrame,
    output_dir: Optional[Path] = None,
) -> FeedbackBundleStats:
    """
    Write the aggregated feedback weight DataFrame to a parquet file and an
    accompanying .meta.json sidecar. Returns FeedbackBundleStats.

    Expected DataFrame columns (produced by build_feedback_weights_df):
      instrument_key  str    — NSE instrument key
      signal_date     date   — UTC calendar bar date
      sample_weight   float32— mean B1×B2 weight for this bar
      outcome_count   int32  — number of raw outcomes aggregated

    The parquet filename encodes the UTC creation timestamp so bundles sort
    chronologically by name. zstd compression gives ~3× size reduction at
    negligible CPU cost for this column-oriented numeric data.
    """
    required_cols = {"instrument_key", "signal_date", "sample_weight", "outcome_count"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"write_bundle: DataFrame missing required columns: {missing}")

    out_dir = Path(output_dir or _DEFAULT_BUNDLES_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp        = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parquet_path = out_dir / f"feedback_weights_{stamp}.parquet"
    meta_path    = out_dir / f"feedback_weights_{stamp}.meta.json"

    df.to_parquet(parquet_path, compression="zstd", index=False)
    raw_bytes = parquet_path.read_bytes()
    sha256    = hashlib.sha256(raw_bytes).hexdigest()

    weights            = df["sample_weight"].values.astype(np.float32)
    total_raw_outcomes = int(df["outcome_count"].sum())

    hist_counts, hist_edges = np.histogram(weights, bins=20, range=(0.0, 5.5))

    # Top-10 instruments by mean weight.
    # n_outcomes here represents the number of UNIQUE (signal_date) bars for
    # that instrument (not raw trades), which is what operators care about.
    top_symbols = (
        df.groupby("instrument_key")
        .agg(mean_weight=("sample_weight", "mean"), n_bars=("sample_weight", "count"))
        .sort_values("mean_weight", ascending=False)
        .head(10)
        .reset_index()
        .rename(columns={"instrument_key": "symbol"})
        .to_dict(orient="records")
    )
    top_symbols = [
        {
            "symbol":      r["symbol"],
            "mean_weight": round(float(r["mean_weight"]), 4),
            "n_outcomes":  int(r["n_bars"]),  # unique bars; keep key name for UI compat
        }
        for r in top_symbols
    ]

    stats = FeedbackBundleStats(
        bundle_path        = str(parquet_path),
        meta_path          = str(meta_path),
        sha256             = sha256,
        row_count          = len(df),
        total_raw_outcomes = total_raw_outcomes,
        window_start       = str(df["signal_date"].min()),
        window_end         = str(df["signal_date"].max()),
        created_at         = datetime.now(timezone.utc).isoformat(),
        weight_mean        = round(float(weights.mean()), 4),
        weight_std         = round(float(weights.std()),  4),
        weight_p5          = round(float(np.percentile(weights,  5)), 4),
        weight_p50         = round(float(np.percentile(weights, 50)), 4),
        weight_p95         = round(float(np.percentile(weights, 95)), 4),
        histogram_bins     = [round(float(e), 3) for e in hist_edges.tolist()],
        histogram_counts   = hist_counts.tolist(),
        top_symbols        = top_symbols,
    )

    meta_path.write_text(json.dumps(stats.to_dict(), indent=2), encoding="utf-8")
    logger.info(
        "Feedback bundle written: %s  sha256=%s…  unique_rows=%d  raw_outcomes=%d",
        parquet_path.name, sha256[:12], len(df), total_raw_outcomes,
    )
    return stats


def load_bundle_lookup(
    bundle_path: str | Path,
) -> Tuple[Dict[Tuple[str, date], np.float32], "FeedbackBundleStats | None"]:
    """
    Load a feedback bundle parquet and return:
      - weight_lookup: dict[(instrument_key, signal_date), float32]
        Fast O(1) lookup used by the orchestrator's per-row weight assignment.
      - stats: FeedbackBundleStats from the .meta.json sidecar (or None if absent).

    Backward compatible with bundles written before the outcome_count column was
    added — the column is simply ignored if absent.

    Performance note: building the lookup dict via zip over two numpy arrays
    (vectorised column extraction) is ~10× faster than iterrows() for bundles
    with thousands of rows.
    """
    p = Path(bundle_path)
    if not p.exists():
        raise FileNotFoundError(f"Feedback bundle not found: {p}")

    df = pd.read_parquet(p)
    df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.date

    # Vectorised dict build — avoids iterrows() O(n) Python overhead.
    keys   = list(zip(df["instrument_key"].tolist(), df["signal_date"].tolist()))
    values = df["sample_weight"].astype(np.float32).tolist()
    weight_lookup: Dict[Tuple[str, date], np.float32] = dict(zip(keys, values))

    # Load sidecar metadata — resolve path whether suffix is .parquet or not.
    meta_path: Optional[Path] = None
    if p.suffix == ".parquet":
        candidate = p.with_suffix(".meta.json")
        if candidate.exists():
            meta_path = candidate

    stats: "FeedbackBundleStats | None" = None
    if meta_path:
        try:
            raw = json.loads(meta_path.read_text())
            # Forward-compat: inject missing fields introduced after bundle was written
            raw.setdefault("total_raw_outcomes", raw.get("row_count", len(df)))
            stats = FeedbackBundleStats(**raw)
        except Exception as exc:
            logger.warning("Could not parse bundle meta sidecar %s: %s", meta_path.name, exc)

    logger.info(
        "Feedback bundle loaded: %d entries  path=%s",
        len(weight_lookup), p.name,
    )
    return weight_lookup, stats


def list_bundles(output_dir: Optional[Path] = None) -> List[FeedbackBundleStats]:
    """
    List all available feedback bundles in output_dir, newest first.
    Returns stats parsed from their .meta.json sidecars.
    Old meta files missing `total_raw_outcomes` are handled gracefully.
    """
    out_dir = Path(output_dir or _DEFAULT_BUNDLES_DIR)
    if not out_dir.exists():
        return []

    bundles: List[FeedbackBundleStats] = []
    for meta_file in sorted(out_dir.glob("*.meta.json"), reverse=True):
        try:
            raw = json.loads(meta_file.read_text())
            # Forward-compat: inject missing fields added in later schema versions.
            raw.setdefault("total_raw_outcomes", raw.get("row_count", 0))
            bundles.append(FeedbackBundleStats(**raw))
        except Exception as exc:
            logger.warning("Skipping malformed meta file %s: %s", meta_file.name, exc)

    return bundles


def get_latest_bundle_stats(output_dir: Optional[Path] = None) -> FeedbackBundleStats | None:
    """Return stats for the most recently created bundle, or None if empty."""
    bundles = list_bundles(output_dir)
    return bundles[0] if bundles else None


def delete_bundle(
    bundle_name: str,
    output_dir: Optional[Path] = None,
) -> FeedbackBundleStats:
    """
    Delete a feedback bundle (parquet + meta sidecar) by its stem name.

    Args:
        bundle_name: Filename stem, e.g. ``feedback_weights_20260528T160638Z``.
                     Must not contain path separators or traversal sequences —
                     callers must validate this before calling (the API layer
                     enforces the regex guard).
        output_dir:  Override the default bundles directory (testing only).

    Returns:
        FeedbackBundleStats of the deleted bundle (for audit logging).

    Raises:
        FileNotFoundError: if the parquet file does not exist.
        ValueError:        if ``bundle_name`` contains unsafe characters.
    """
    if "/" in bundle_name or "\\" in bundle_name or ".." in bundle_name:
        raise ValueError(f"Unsafe bundle_name: {bundle_name!r}")

    out_dir = Path(output_dir or _DEFAULT_BUNDLES_DIR)
    parquet_path = out_dir / f"{bundle_name}.parquet"
    meta_path    = out_dir / f"{bundle_name}.meta.json"

    if not parquet_path.exists():
        raise FileNotFoundError(f"Feedback bundle not found: {parquet_path}")

    # Read stats before deletion for the audit record.
    stats: Optional[FeedbackBundleStats] = None
    if meta_path.exists():
        try:
            raw = json.loads(meta_path.read_text())
            raw.setdefault("total_raw_outcomes", raw.get("row_count", 0))
            stats = FeedbackBundleStats(**raw)
        except Exception as exc:
            logger.warning("Could not parse meta sidecar before deletion %s: %s", meta_path.name, exc)

    # Delete parquet first; meta is non-critical.
    parquet_path.unlink()
    logger.info("Deleted feedback bundle parquet: %s", parquet_path.name)

    if meta_path.exists():
        meta_path.unlink()
        logger.info("Deleted feedback bundle meta: %s", meta_path.name)

    if stats is None:
        # Construct a minimal stats object if meta was absent/corrupt.
        stats = FeedbackBundleStats(
            bundle_path        = str(parquet_path),
            meta_path          = str(meta_path),
            sha256             = "",
            row_count          = 0,
            total_raw_outcomes = 0,
            window_start       = "",
            window_end         = "",
            created_at         = "",
            weight_mean        = 0.0,
            weight_std         = 0.0,
            weight_p5          = 0.0,
            weight_p50         = 0.0,
            weight_p95         = 0.0,
            histogram_bins     = [],
            histogram_counts   = [],
            top_symbols        = [],
        )

    return stats


# ── Training integration helpers ───────────────────────────────────────────────

def build_panel_weights(
    bundle_path: str | Path,
    sym_all: np.ndarray,
    ts_all: np.ndarray,
    class_weights: Optional[Dict[int, float]] = None,
) -> np.ndarray:
    """
    Build a per-row sample weight array for the XGBoost tabular panel.

    Args:
        bundle_path:   Path to feedback_weights_*.parquet
        sym_all:       shape (n,) string array of instrument_keys (same order as panel)
        ts_all:        shape (n,) datetime64[ns] array of bar timestamps (same order as panel)
        class_weights: Optional {0: w0, 1: w1} — when provided, the class weight for
                       each row is multiplied into the feedback weight so that a single
                       combined weight covers both objectives.

    Returns:
        shape (n,) float32 array. Rows without feedback data default to the
        class weight (if provided) or 1.0.
    """
    weight_lookup, _ = load_bundle_lookup(bundle_path)

    n = len(sym_all)
    weights = np.ones(n, dtype=np.float32)

    for i in range(n):
        sym = str(sym_all[i])
        d   = pd.Timestamp(ts_all[i]).tz_localize("UTC").date()
        fb_w = float(weight_lookup.get((sym, d), 1.0))
        if class_weights is not None:
            pass  # caller combines class_weights separately
        weights[i] = fb_w

    matched = int(np.sum(weights != 1.0))
    logger.info(
        "XGBoost panel weights: %d / %d rows matched feedback bundle (%.1f%%)",
        matched, n, 100.0 * matched / max(n, 1),
    )
    return weights


def build_sequence_weights_for_symbol(
    bundle_path: str | Path,
    weight_lookup: Optional[Dict[Tuple[str, date], np.float32]],
    symbol: str,
    seq_timestamps: "pd.DatetimeIndex",
    n_take: int,
    class_weights: Optional[Dict[int, float]],
    y_sym: np.ndarray,
    y_start: int,
) -> np.ndarray:
    """
    Build per-sequence weights for a single symbol's GRU training slice.

    The per-sequence weight = feedback_weight(symbol, bar_date) × class_weight(label).
    This combined weight replaces the Keras `class_weight` dict when feedback is present,
    since Keras cannot use both `class_weight` and `sample_weight` simultaneously.

    Args:
        weight_lookup:   Pre-loaded lookup dict. Pass None to skip (returns class-weight-only).
        symbol:          Instrument key string.
        seq_timestamps:  pd.DatetimeIndex of bar timestamps for each sequence (last bar).
        n_take:          Number of train sequences to include.
        class_weights:   {0: w0, 1: w1} or None.
        y_sym:           Per-row label array for the symbol.
        y_start:         Start offset into y_sym.

    Returns:
        float32 array of shape (n_take,)
    """
    weights = np.ones(n_take, dtype=np.float32)

    for i in range(n_take):
        label = int(y_sym[y_start + i])
        cw = float(class_weights[label]) if class_weights else 1.0

        if weight_lookup is not None:
            try:
                ts = seq_timestamps[i]
                d = pd.Timestamp(ts).tz_localize("UTC").date() if ts.tzinfo is None else pd.Timestamp(ts).date()
                fb_w = float(weight_lookup.get((symbol, d), 1.0))
            except Exception:
                fb_w = 1.0
        else:
            fb_w = 1.0

        weights[i] = float(np.clip(cw * fb_w, _WEIGHT_CLIP_MIN, _WEIGHT_CLIP_MAX))

    return weights
