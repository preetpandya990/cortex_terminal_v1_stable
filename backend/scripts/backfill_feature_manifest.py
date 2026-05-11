"""
Backfill feature manifest (training_features) for existing production model records.

The feature manifest was introduced after initial model training — this script
retroactively populates the `training_features` JSON column for records that have
an empty or null list, by:

  1. Reading the actual model artifact to determine n_features (ground truth).
  2. Matching against known feature-name lists derived from the feature pipeline.
  3. Writing the resolved list back to the DB record.

Usage:
    python backfill_feature_manifest.py [--dry-run] [--status STATUS]

    --dry-run    Print what would be written without touching the DB.
    --status     Filter by lifecycle status (default: production). Use "all" for every record.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
_SCRIPTS_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_BACKEND_DIR))
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Feature-name resolution
# ---------------------------------------------------------------------------

def _build_candidate_manifests() -> dict[int, list[str]]:
    """
    Return a mapping of {n_features: [feature_name, ...]} for every known
    pipeline configuration.  Add entries here as the feature set evolves.
    """
    from app.ml.features.feature_pipeline import get_all_feature_names
    from app.ml.features.ohlcv_features import get_feature_names  # technical only

    candidates: dict[int, list[str]] = {}

    # 42 technical features (no sentiment)
    tech_only = get_feature_names()
    candidates[len(tech_only)] = tech_only

    # 47 = 42 technical + 5 sentiment (current production model)
    with_sentiment = get_all_feature_names(include_sentiment=True)
    candidates[len(with_sentiment)] = with_sentiment

    # 37 — upcoming model; feature_pipeline may not yet export a 37-name list.
    # When it does, add it here:
    #   from app.ml.features.some_new_module import get_37_feature_names
    #   candidates[37] = get_37_feature_names()

    return candidates


# ---------------------------------------------------------------------------
# Artifact introspection
# ---------------------------------------------------------------------------

def _n_features_from_xgb(path: str | Path) -> int | None:
    """Return num_feature from a Treelite shared-library artifact, or None."""
    path = Path(path)
    if not path.exists() or path.suffix not in {".so", ".dll", ".dylib"}:
        return None
    try:
        import tl2cgen
        pred = tl2cgen.Predictor(str(path))
        return int(pred.num_feature)
    except Exception as exc:
        logger.warning("tl2cgen load failed for %s: %s", path.name, exc)
        return None


def _n_features_from_onnx(path: str | Path) -> int | None:
    """Return n_features from an ONNX model's sequence input shape, or None."""
    path = Path(path)
    if not path.exists() or path.suffix != ".onnx":
        return None
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        shape = sess.get_inputs()[0].shape  # [batch, seq_len, n_features]
        if len(shape) == 3:
            return int(shape[2])
        # tabular model: [batch, n_features]
        if len(shape) == 2:
            return int(shape[1])
        return None
    except Exception as exc:
        logger.warning("ONNX load failed for %s: %s", path.name, exc)
        return None


def _introspect_n_features(record) -> int | None:
    """Best-effort n_features from whichever artifact path is populated.

    Tries every path/loader combination so records where the training artifact
    ended up in the onnx_path column (or vice-versa) are still handled.
    """
    paths = [p for p in (record.model_path, record.onnx_path) if p]
    for path in paths:
        n = _n_features_from_onnx(path)
        if n is not None:
            return n
        n = _n_features_from_xgb(path)
        if n is not None:
            return n
    return None


# ---------------------------------------------------------------------------
# Main async logic
# ---------------------------------------------------------------------------

async def run(dry_run: bool, status_filter: str) -> None:
    from sqlalchemy import select
    from app.core.database import AsyncSessionLocal
    from app.models.ml_data import MLModelMetadata

    candidates = _build_candidate_manifests()
    logger.info("Known feature manifests: %s", {k: len(v) for k, v in candidates.items()})

    async with AsyncSessionLocal() as session:
        stmt = select(MLModelMetadata)
        if status_filter != "all":
            stmt = stmt.where(MLModelMetadata.status == status_filter)
        stmt = stmt.order_by(MLModelMetadata.created_at.desc())

        rows = (await session.execute(stmt)).scalars().all()
        logger.info("Found %d record(s) matching status=%r", len(rows), status_filter)

        updated = 0
        skipped = 0

        for rec in rows:
            existing = rec.training_features
            if existing:  # already populated — skip
                logger.info(
                    "  SKIP  model_id=%-40s  already has %d feature names",
                    rec.model_id, len(existing),
                )
                skipped += 1
                continue

            n = _introspect_n_features(rec)
            if n is None:
                logger.warning(
                    "  WARN  model_id=%-40s  could not determine n_features (artifacts missing?)",
                    rec.model_id,
                )
                skipped += 1
                continue

            manifest = candidates.get(n)
            if manifest is None:
                logger.warning(
                    "  WARN  model_id=%-40s  n_features=%d has no known manifest — "
                    "add it to _build_candidate_manifests()",
                    rec.model_id, n,
                )
                skipped += 1
                continue

            logger.info(
                "  %s  model_id=%-40s  n_features=%d  will write %d-name manifest",
                "DRY-RUN" if dry_run else "UPDATE ",
                rec.model_id, n, len(manifest),
            )

            if not dry_run:
                rec.training_features = manifest
                updated += 1

        if not dry_run and updated:
            await session.commit()
            logger.info("Committed %d update(s).", updated)
        elif dry_run:
            logger.info("Dry-run complete — no changes written.")
        else:
            logger.info("Nothing to update.")

        logger.info(
            "Summary: %d updated, %d skipped, %d total",
            updated, skipped, len(rows),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print planned changes without writing to DB")
    parser.add_argument("--status", default="production", metavar="STATUS",
                        help='Lifecycle status to filter on (default: production). Use "all" for every record.')
    args = parser.parse_args()

    asyncio.run(run(dry_run=args.dry_run, status_filter=args.status))


if __name__ == "__main__":
    main()
