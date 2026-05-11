"""
Cortex AI — Model Registry Repair Script
=========================================
One-time (and safely idempotent) repair for production MLModelMetadata records
whose ``onnx_path`` points to a training artifact (.json / .keras / .enc) instead
of the inference-ready artifact (.so for XGBoost Treelite, .onnx for GRU).

This situation arises when training scripts registered models before the
``inference_artifact_path`` parameter was added to ``ModelRegistry.register_model()``.
The same repair logic runs automatically on every server startup via
``bootstrap_production_models()``, but this script lets you trigger it manually
or verify the DB state without restarting the service.

Usage
-----
    cd backend
    python scripts/repair_model_registry.py [--dry-run] [--status]

Options
-------
    --dry-run   Show what would change without committing anything.
    --status    Print current onnx_path for all production records and exit.

Author: Cortex AI Team
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Bootstrap path ─────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.ml_data import MLModelMetadata

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── Inference artifact locations ───────────────────────────────────────────────
_BACKEND_ROOT = Path(__file__).parent.parent

_EXPECTED_INFERENCE: dict[str, dict] = {
    "xgboost": {
        "artifact": _BACKEND_ROOT / "models/production/treelite/xgboost_model.so",
        "valid_suffixes": {".so", ".dll", ".dylib"},
    },
    "gru": {
        "artifact": _BACKEND_ROOT / "models/production/onnx/gru_optimized.onnx",
        "valid_suffixes": {".onnx"},
    },
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_valid(onnx_path: str | None, valid_suffixes: set[str]) -> bool:
    if not onnx_path:
        return False
    p = Path(onnx_path)
    return p.suffix in valid_suffixes and p.exists()


async def show_status(session: AsyncSession) -> None:
    print("\n" + "=" * 90)
    print("PRODUCTION MODEL REGISTRY STATUS")
    print("=" * 90)

    for model_name, cfg in _EXPECTED_INFERENCE.items():
        stmt = (
            select(MLModelMetadata)
            .where(
                MLModelMetadata.model_name == model_name,
                MLModelMetadata.status == "production",
                MLModelMetadata.is_active == True,
            )
            .order_by(MLModelMetadata.deployed_at.desc())
            .limit(1)
        )
        record = (await session.execute(stmt)).scalar_one_or_none()

        print(f"\n  {model_name.upper()}")
        if record is None:
            print("    ❌  No active production record found")
            continue

        valid = _is_valid(record.onnx_path, cfg["valid_suffixes"])
        icon = "✅" if valid else "❌"
        print(f"    model_id   : {record.model_id}")
        print(f"    model_path : {record.model_path or '(null)'}")
        print(f"    onnx_path  : {record.onnx_path}")
        print(f"    checksum   : {record.checksum[:16] if record.checksum else '(null)'}…")
        print(f"    status     : {icon}  {'healthy' if valid else 'STALE — needs repair'}")
        print(f"    expected   : {cfg['artifact']}")

    print("\n" + "=" * 90)


async def repair(session: AsyncSession, dry_run: bool) -> None:
    any_repaired = False

    for model_name, cfg in _EXPECTED_INFERENCE.items():
        artifact: Path = cfg["artifact"]
        valid_suffixes: set[str] = cfg["valid_suffixes"]

        stmt = (
            select(MLModelMetadata)
            .where(
                MLModelMetadata.model_name == model_name,
                MLModelMetadata.status == "production",
                MLModelMetadata.is_active == True,
            )
            .order_by(MLModelMetadata.deployed_at.desc())
            .limit(1)
        )
        record = (await session.execute(stmt)).scalar_one_or_none()

        if record is None:
            logger.info(
                "%s: no active production record — bootstrap will insert one on next startup",
                model_name,
            )
            continue

        if _is_valid(record.onnx_path, valid_suffixes):
            logger.info("%s: onnx_path is healthy (%s)", model_name, record.onnx_path)
            continue

        if not artifact.exists():
            logger.error(
                "%s: inference artifact not found at %s — cannot repair. "
                "Re-run the training pipeline (Step 9 ONNX/Treelite export).",
                model_name, artifact,
            )
            continue

        new_checksum = _sha256(artifact)
        old_path = record.onnx_path

        if dry_run:
            logger.info(
                "[DRY-RUN] %s: would update onnx_path\n"
                "  from : %s\n"
                "  to   : %s\n"
                "  csum : %s…",
                model_name, old_path, artifact, new_checksum[:16],
            )
        else:
            record.onnx_path  = str(artifact)
            record.checksum   = new_checksum
            record.updated_at = datetime.now(timezone.utc)
            logger.warning(
                "%s: repaired onnx_path\n  from : %s\n  to   : %s",
                model_name, old_path, artifact,
            )

        any_repaired = True

    if not dry_run and any_repaired:
        await session.commit()
        logger.info("All repairs committed.")
    elif not any_repaired:
        logger.info("Nothing to repair — all production records are healthy.")
    else:
        logger.info("[DRY-RUN] No changes committed.")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Repair ML model registry onnx_path fields")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without committing")
    parser.add_argument("--status", action="store_true", help="Print current state and exit")
    args = parser.parse_args()

    settings = get_settings()
    engine = create_async_engine(str(settings.DATABASE_URL), echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with Session() as session:
            if args.status:
                await show_status(session)
            else:
                if args.dry_run:
                    logger.info("Running in DRY-RUN mode — no DB changes will be made")
                await repair(session, dry_run=args.dry_run)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
