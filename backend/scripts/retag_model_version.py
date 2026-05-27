"""
Cortex AI — Model Version Re-Tag Utility
=========================================
Atomically renames all artifacts and DB records from one model-version tag
to another.  Designed for the specific case where the training orchestrator
was invoked with the wrong ``model_version`` (e.g. ``1.0.0`` instead of
``1.1.0``), causing the new run's registry entries to shadow the previous
champion instead of creating fresh challenger records.

What this script does
---------------------
1. Validates that the source version(s) exist in the DB and the target
   version(s) do NOT (idempotency guard).
2. Copies every physical artifact stored under ``model_storage_path`` that
   carries the old version prefix to a new file with the target prefix.
   The source files are removed only after the DB commit succeeds.
3. Updates the ``ml_model_metadata`` record for each model (model_id,
   model_version, model_path, onnx_path, and all paths inside
   ``lineage.artifact_manifest``).
4. Updates ``ai_ml_models`` (governance projection) to the new
   ``model_version`` string for the affected model types.
5. Patches ``models/production/checkpoints/checkpoint.json`` (the
   ``config.model_version`` field) so that a subsequent resume does not
   re-register under the old tag.
6. Re-generates the C2 Promotion Report from the DB so the report bundle
   SHA, challenger versions, and artifact paths are all consistent.

All DB mutations happen inside a single transaction; the filesystem renames
happen only after commit.  If anything fails before commit the DB is rolled
back and the old files are still in place — no half-renamed state.

Usage
-----
    # Dry-run (print what would change, touch nothing):
    python scripts/retag_model_version.py --from 1.0.0 --to 1.1.0 --dry-run

    # Live run:
    python scripts/retag_model_version.py --from 1.0.0 --to 1.1.0

    # Custom model names (default: xgboost gru):
    python scripts/retag_model_version.py --from 1.0.0 --to 1.1.0 \\
        --models xgboost gru

    # Skip C2 report regeneration (e.g. when the backtest data is unavailable):
    python scripts/retag_model_version.py --from 1.0.0 --to 1.1.0 --no-c2
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

# ── Bootstrap path so ``app.*`` imports resolve ──────────────────────────────
_BACKEND = Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND))

from sqlalchemy import select, and_ as _sa_and, update as _sa_update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.ml_data import MLModelMetadata

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("retag_model_version")

_MODEL_STORAGE   = _BACKEND / "models" / "production" / "models"
_CHECKPOINT_JSON = _BACKEND / "models" / "production" / "checkpoints" / "checkpoint.json"
_INCIDENT_DIR    = _BACKEND / "incident_reports"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _replace_path_prefix(
    path_str: str | None,
    old_ver: str,
    new_ver: str,
) -> str | None:
    """Replace every occurrence of ``old_ver`` inside a path string."""
    if path_str is None:
        return None
    return path_str.replace(old_ver, new_ver)


def _patch_manifest(manifest: dict[str, Any], old_ver: str, new_ver: str) -> dict[str, Any]:
    """Deep-patch paths inside an artifact_manifest dict."""
    patched: dict[str, Any] = {}
    for key, val in manifest.items():
        if isinstance(val, dict):
            patched[key] = _patch_manifest(val, old_ver, new_ver)
        elif isinstance(val, str):
            patched[key] = val.replace(old_ver, new_ver)
        else:
            patched[key] = val
    return patched


async def _load_record(
    session: AsyncSession,
    version: str,
) -> MLModelMetadata | None:
    result = await session.execute(
        select(MLModelMetadata).where(MLModelMetadata.model_version == version)
    )
    return result.scalar_one_or_none()


# ── Core migration ────────────────────────────────────────────────────────────

async def retag(
    session:    AsyncSession,
    old_ver:    str,
    new_ver:    str,
    model_names: list[str],
    *,
    dry_run:    bool,
    regen_c2:   bool,
) -> None:
    """Execute the full re-tag: DB updates + filesystem renames + checkpoint patch."""

    old_versions = [f"{old_ver}_{m}" for m in model_names]
    new_versions = [f"{new_ver}_{m}" for m in model_names]

    # ── 1. Validate: source records must exist, targets must not ─────────────
    logger.info("Validating registry state …")
    records: dict[str, MLModelMetadata] = {}
    for old_v, model_name in zip(old_versions, model_names):
        rec = await _load_record(session, old_v)
        if rec is None:
            raise ValueError(
                f"Source version '{old_v}' not found in ml_model_metadata. "
                "Nothing to re-tag."
            )
        records[model_name] = rec
        logger.info("  Found source: %s  (status=%s)", old_v, rec.status)

    for new_v in new_versions:
        existing = await _load_record(session, new_v)
        if existing is not None:
            raise ValueError(
                f"Target version '{new_v}' already exists in the registry. "
                "Re-tag is not idempotent when the target is already registered. "
                "Either the migration already ran, or you are targeting an occupied slot."
            )
        logger.info("  Target slot %s is free ✓", new_v)

    # ── 2. Resolve physical file pairs to copy ────────────────────────────────
    # Every artifact stored by register_model uses the version string as a
    # filename prefix inside model_storage_path.  We enumerate the directory
    # and copy every file whose stem starts with an old version prefix.
    file_copies: list[tuple[Path, Path]] = []   # (src, dst)
    for old_v, new_v in zip(old_versions, new_versions):
        for src in sorted(_MODEL_STORAGE.glob(f"{old_v}*")):
            dst = src.parent / src.name.replace(old_v, new_v, 1)
            file_copies.append((src, dst))
            logger.info("  Will copy: %s → %s", src.name, dst.name)

    if not file_copies:
        logger.warning(
            "No files found under %s matching old version prefixes %s. "
            "DB records will still be updated.",
            _MODEL_STORAGE,
            old_versions,
        )

    if dry_run:
        logger.info("DRY-RUN: DB and filesystem changes previewed above — nothing written.")
        _preview_db_changes(records, old_versions, new_versions, model_names)
        return

    # ── 3. Copy files (src still intact — only removed after DB commit) ───────
    logger.info("Copying artifact files …")
    copied: list[Path] = []
    try:
        for src, dst in file_copies:
            shutil.copy2(src, dst)
            copied.append(dst)
            logger.info("  Copied %s", dst.name)
    except Exception:
        logger.error("File copy failed — rolling back copied files")
        for f in copied:
            f.unlink(missing_ok=True)
        raise

    # ── 4. Update DB records inside a single transaction ─────────────────────
    logger.info("Updating DB records …")
    try:
        from app.ai.fusion.models import AIMLModel as _GovernanceModel
        for old_v, new_v, model_name in zip(old_versions, new_versions, model_names):
            rec = records[model_name]

            new_model_id   = rec.model_id.replace(old_v, new_v)
            new_model_path = _replace_path_prefix(rec.model_path, old_v, new_v)
            new_onnx_path  = _replace_path_prefix(rec.onnx_path,  old_v, new_v)

            existing_lineage: dict = rec.lineage or {}
            existing_manifest: dict = existing_lineage.get("artifact_manifest", {})
            patched_manifest  = _patch_manifest(existing_manifest, old_v, new_v)
            new_lineage = {**existing_lineage, "artifact_manifest": patched_manifest}

            logger.info(
                "  Updating %s → %s  (model_id: %s → %s)",
                old_v, new_v, rec.model_id, new_model_id,
            )

            await session.execute(
                _sa_update(MLModelMetadata)
                .where(MLModelMetadata.model_version == old_v)
                .values(
                    model_id      = new_model_id,
                    model_version = new_v,
                    model_path    = new_model_path,
                    onnx_path     = new_onnx_path,
                    lineage       = new_lineage,
                )
                .execution_options(synchronize_session=False)
            )

            # Governance projection — ai_ml_models is matched by model_type
            # (== model_name), not by model_version, so only the version
            # string field needs updating.
            gov_result = await session.execute(
                _sa_update(_GovernanceModel)
                .where(_GovernanceModel.model_type == model_name)
                .values(model_version=new_v)
                .execution_options(synchronize_session=False)
            )
            if gov_result.rowcount:
                logger.info(
                    "  Governance projection updated: ai_ml_models.model_version → %s"
                    " for model_type=%s (%d row(s))",
                    new_v, model_name, gov_result.rowcount,
                )

        await session.commit()
        logger.info("DB transaction committed ✓")

    except Exception:
        await session.rollback()
        logger.error("DB update failed — rolling back DB transaction and removing copied files")
        for f in copied:
            f.unlink(missing_ok=True)
        raise

    # ── 5. Remove source files (DB is committed — this is now safe) ──────────
    logger.info("Removing old artifact files …")
    for src, _ in file_copies:
        src.unlink(missing_ok=True)
        logger.info("  Removed %s", src.name)

    # ── 6. Patch checkpoint.json ──────────────────────────────────────────────
    _patch_checkpoint(old_ver, new_ver)

    # ── 7. Regenerate C2 promotion report ─────────────────────────────────────
    if regen_c2:
        # Expire all session objects so the SELECT inside _regen_c2_report
        # fetches fresh DB rows with the newly committed version strings instead
        # of returning stale Python objects from the identity map.
        session.expire_all()
        await _regen_c2_report(session, new_versions, model_names)

    logger.info(
        "Re-tag complete: %s → %s  (%d artifact(s), %d model(s))",
        old_ver, new_ver, len(file_copies), len(model_names),
    )


# ── Supporting functions ──────────────────────────────────────────────────────

def _preview_db_changes(
    records:     dict[str, MLModelMetadata],
    old_versions: list[str],
    new_versions: list[str],
    model_names:  list[str],
) -> None:
    for old_v, new_v, model_name in zip(old_versions, new_versions, model_names):
        rec = records[model_name]
        logger.info(
            "DB change (dry-run): model_version %s → %s | model_id %s → %s",
            old_v, new_v, rec.model_id, rec.model_id.replace(old_v, new_v),
        )
        logger.info(
            "  model_path: %s → %s",
            rec.model_path,
            _replace_path_prefix(rec.model_path, old_v, new_v),
        )
        logger.info(
            "  onnx_path:  %s → %s",
            rec.onnx_path,
            _replace_path_prefix(rec.onnx_path, old_v, new_v),
        )


def _patch_checkpoint(old_ver: str, new_ver: str) -> None:
    if not _CHECKPOINT_JSON.exists():
        logger.warning("Checkpoint not found at %s — skipping patch", _CHECKPOINT_JSON)
        return
    try:
        data = json.loads(_CHECKPOINT_JSON.read_text())
        cfg  = data.get("config", {})
        if cfg.get("model_version") == old_ver:
            cfg["model_version"] = new_ver
            data["config"] = cfg
            _CHECKPOINT_JSON.write_text(json.dumps(data, indent=2, default=str))
            logger.info("Checkpoint patched: model_version %s → %s", old_ver, new_ver)
        elif cfg.get("model_version") == new_ver:
            logger.info("Checkpoint already at target version %s — no patch needed", new_ver)
        else:
            logger.warning(
                "Checkpoint model_version is '%s', expected '%s' — left unchanged",
                cfg.get("model_version"), old_ver,
            )
    except Exception as exc:
        logger.warning("Could not patch checkpoint (%s) — continuing", exc)


async def _regen_c2_report(
    session:      AsyncSession,
    new_versions: list[str],
    model_names:  list[str],
) -> None:
    """Re-generate the C2 promotion report using the re-tagged challenger records."""
    from app.ml.evaluation.promotion_report import build_promotion_report
    from app.ai.fusion.models import AIMLModel as _GovernanceModel, AIDriftReport as _DriftReport

    logger.info("Re-generating C2 promotion report …")

    challengers: dict[str, Any] = {}
    for new_v, model_name in zip(new_versions, model_names):
        rec = await _load_record(session, new_v)
        if rec is not None:
            challengers[model_name] = rec
        else:
            logger.warning("Re-tagged record %s not found — skipping C2", new_v)

    if not challengers:
        logger.warning("No challenger records found after re-tag — C2 report not generated")
        return

    # Current production champions (if any)
    champions: dict[str, Any] = {}
    for model_name in challengers:
        champ = (await session.execute(
            select(MLModelMetadata).where(
                _sa_and(
                    MLModelMetadata.model_name == model_name,
                    MLModelMetadata.status     == "production",
                    MLModelMetadata.is_active  == True,  # noqa: E712
                )
            ).order_by(MLModelMetadata.deployed_at.desc()).limit(1)
        )).scalar_one_or_none()
        champions[model_name] = champ

    # Minimal drift advisory (read-only snapshot from governance tables)
    drift_advisory: dict[str, Any] = {}
    for model_name, champ in champions.items():
        if champ is None:
            drift_advisory[model_name] = None
            continue
        gov_row = (await session.execute(
            select(_GovernanceModel).where(
                _sa_and(
                    _GovernanceModel.model_type       == model_name,
                    _GovernanceModel.deployment_state == "live",
                )
            ).order_by(_GovernanceModel.updated_at.desc()).limit(1)
        )).scalar_one_or_none()
        if gov_row is None:
            drift_advisory[model_name] = {"challenger_recommended": False}
            continue
        gov_meta     = gov_row.governance_metadata or {}
        latest_drift = (await session.execute(
            select(_DriftReport).where(
                _DriftReport.model_id == gov_row.id
            ).order_by(_DriftReport.report_timestamp.desc()).limit(1)
        )).scalar_one_or_none()
        drift_advisory[model_name] = {
            "challenger_recommended": gov_meta.get("challenger_recommended", False),
            "drift_recommendation":  gov_meta.get("drift_recommendation"),
            "last_check": (latest_drift.report_timestamp.isoformat() if latest_drift else None),
            "last_drift_score": (
                float(latest_drift.drift_score)
                if latest_drift and latest_drift.drift_score is not None else None
            ),
            "live_metrics": (latest_drift.distribution_metrics if latest_drift else {}),
        }

    # Load run_id from the (now-patched) checkpoint for traceability
    run_id: str | None = None
    if _CHECKPOINT_JSON.exists():
        try:
            run_id = json.loads(_CHECKPOINT_JSON.read_text()).get("run_id")
        except Exception:
            pass

    _INCIDENT_DIR.mkdir(parents=True, exist_ok=True)
    report = build_promotion_report(
        challengers    = challengers,
        champions      = champions,
        report_dir     = _INCIDENT_DIR,
        run_id         = run_id,
        drift_advisory = drift_advisory,
    )
    logger.info(
        "C2 report written: status=%s  path=%s",
        report.get("status"), report.get("report_path"),
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Cortex AI — re-tag a training run's model version in DB + filesystem",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--from", dest="from_ver", required=True,
        metavar="FROM",
        help="Current (wrong) version tag, e.g. 1.0.0",
    )
    p.add_argument(
        "--to", dest="to_ver", required=True,
        metavar="TO",
        help="Target (correct) version tag, e.g. 1.1.0",
    )
    p.add_argument(
        "--models", nargs="+", default=["xgboost", "gru"],
        metavar="MODEL",
        help="Model names to re-tag (default: xgboost gru)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print planned changes without modifying anything",
    )
    p.add_argument(
        "--no-c2", action="store_true",
        help="Skip C2 promotion report regeneration",
    )
    return p


async def main() -> None:
    args = _build_parser().parse_args()

    if args.from_ver == args.to_ver:
        logger.error("--from and --to are identical (%s) — nothing to do", args.from_ver)
        sys.exit(1)

    settings = get_settings()
    engine   = create_async_engine(str(settings.DATABASE_URL), echo=False)
    Session  = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        await retag(
            session,
            old_ver      = args.from_ver,
            new_ver      = args.to_ver,
            model_names  = args.models,
            dry_run      = args.dry_run,
            regen_c2     = not args.no_c2,
        )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
