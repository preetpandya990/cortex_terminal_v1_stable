"""
B1+B2 Feedback Weight Builder — CLI
=====================================
Standalone script that queries matured paper-trade outcomes, applies the
B1+B2 weight formula, and writes a parquet bundle to feedback_bundles/.

Usage:
    # Full build (writes parquet + meta sidecar):
    python scripts/build_feedback_weights.py

    # Dry-run (prints stats, nothing written):
    python scripts/build_feedback_weights.py --dry-run

    # Custom output directory:
    python scripts/build_feedback_weights.py --output-dir /data/bundles

Exit codes:
    0  — success (or dry-run)
    1  — no matured outcomes found
    2  — database or I/O error
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Make app.* importable when run directly from backend/
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
LOG = logging.getLogger("build_feedback_weights")


async def _run(dry_run: bool, output_dir: Path | None) -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.config import get_settings
    from app.ml.training.feedback_loader import (
        build_feedback_weights_df,
        write_bundle,
    )

    settings = get_settings()
    engine   = create_async_engine(
        str(settings.DATABASE_URL),
        echo=False,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=2,
    )
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session() as session:
            LOG.info("Querying matured paper-trade outcomes…")
            df = await build_feedback_weights_df(session)

        if df.empty:
            LOG.error(
                "No matured outcomes found. "
                "Possible causes: paper trading just started (need ≥1 day of closed trades "
                "with ML fields populated) or no trades match the maturity gate."
            )
            return 1

        LOG.info(
            "Weight distribution:  mean=%.3f  std=%.3f  p5=%.3f  p50=%.3f  p95=%.3f",
            df["sample_weight"].mean(),
            df["sample_weight"].std(),
            df["sample_weight"].quantile(0.05),
            df["sample_weight"].quantile(0.50),
            df["sample_weight"].quantile(0.95),
        )

        value_counts = df["sample_weight"].round(1).value_counts().sort_index()
        LOG.info("Weight bucket distribution:\n%s", value_counts.to_string())

        if dry_run:
            LOG.info("[DRY-RUN] %d rows processed. No bundle written.", len(df))
            return 0

        stats = write_bundle(df, output_dir=output_dir)
        LOG.info("Bundle written: %s", stats.bundle_path)
        LOG.info("  SHA-256  : %s", stats.sha256)
        LOG.info("  Rows     : %d", stats.row_count)
        LOG.info("  Window   : %s → %s", stats.window_start, stats.window_end)
        return 0

    except Exception as exc:
        LOG.exception("Build failed: %s", exc)
        return 2
    finally:
        await engine.dispose()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="B1+B2 Feedback Weight Builder — generates sample_weight parquet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and log stats without writing any files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Override the default feedback_bundles/ output directory.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return asyncio.run(_run(dry_run=args.dry_run, output_dir=args.output_dir))


if __name__ == "__main__":
    sys.exit(main())
