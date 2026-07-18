"""
One-off governance-table repair: re-project ml_model_metadata → ai_ml_models.

Context
-------
WS4 of the ML Fix & Upgrade plan (ML_FIX_IMPLEMENTATION_PLAN.md). The manual
1.1.1 model stamp (2026-05-31) bypassed ``ModelPromoter`` and therefore
``_project_to_ai_ml_models``, so the ``ai_ml_models`` governance table still
points at the archived 1.0.0 records (state='paper', stale FK). Serving reads
``ml_model_metadata`` and was never affected — but governance (drift
detection, the governance API) has been blind since.

This script replays the projection for every **authoritative** metadata row
(``status='production'`` OR ``is_active=true``) through the same hardened
``_project_to_ai_ml_models`` used by the live promotion path — update if a
governance row exists for the model type, insert if not. No second write
path to drift out of sync.

Must run **AFTER** ``scripts/purge_test_model_rows.py`` so test debris cannot
be mistaken for repairable rows.

Safety
------
  - Dry run by default; ``--execute`` required to write anything.
  - Dry run shows the current governance row next to the projected target.
  - Rows are replayed oldest-first per model type, so when duplicates exist
    the newest authoritative row wins (and a warning names the duplicates).
  - Single transaction: all projections commit (or roll back) together.

Usage
-----
  python scripts/repair_ai_ml_models_projection.py              # dry run
  python scripts/repair_ai_ml_models_projection.py --execute    # writes

Exit codes
----------
  0  success (including "nothing to repair")
  2  unexpected error
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from sqlalchemy import or_, select  # noqa: E402

from app.ai.fusion.models import AIMLModel  # noqa: E402
from app.core.database import AsyncSessionLocal  # noqa: E402
from app.ml.model_registry import (  # noqa: E402
    _ML_TO_GOVERNANCE_STATE,
    _project_to_ai_ml_models,
)
from app.models.ml_data import MLModelMetadata  # noqa: E402

logger = logging.getLogger("repair_ai_ml_models_projection")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_EXIT_OK = 0
_EXIT_ERROR = 2


async def find_authoritative_rows(session) -> list[MLModelMetadata]:
    """Metadata rows that governance must reflect, oldest-first per type.

    Oldest-first ordering means that when more than one authoritative row
    exists for a model type, the newest is projected last and wins.
    """
    stmt = (
        select(MLModelMetadata)
        .where(
            or_(
                MLModelMetadata.status == "production",
                MLModelMetadata.is_active.is_(True),
            )
        )
        .order_by(MLModelMetadata.model_name, MLModelMetadata.trained_at)
    )
    return list((await session.execute(stmt)).scalars().all())


def warn_on_duplicates(rows: list[MLModelMetadata]) -> None:
    by_type: dict[str, list[MLModelMetadata]] = {}
    for row in rows:
        by_type.setdefault(row.model_name, []).append(row)
    for model_type, group in by_type.items():
        if len(group) > 1:
            logger.warning(
                "model_type=%s has %d authoritative rows (%s) — the newest "
                "(trained_at=%s) will win",
                model_type, len(group),
                [r.model_id for r in group], group[-1].trained_at,
            )


async def load_governance_row(session, model_type: str) -> AIMLModel | None:
    stmt = select(AIMLModel).where(AIMLModel.model_type == model_type)
    return (await session.execute(stmt)).scalar_one_or_none()


async def report_plan(session, rows: list[MLModelMetadata]) -> None:
    """Log current governance state vs the projection target for each row."""
    for row in rows:
        target_state = _ML_TO_GOVERNANCE_STATE.get(row.status, row.status)
        current = await load_governance_row(session, row.model_name)
        if current is None:
            logger.info(
                "  %-8s metadata id=%-4d %-22s → INSERT cortex_%s_1d "
                "(state=%s, version=%s, fk=%d)",
                row.model_name, row.id, row.model_id,
                row.model_name, target_state, row.model_version, row.id,
            )
        else:
            logger.info(
                "  %-8s metadata id=%-4d %-22s → UPDATE %s: "
                "state %s→%s, version %s→%s, fk %s→%d",
                row.model_name, row.id, row.model_id, current.model_name,
                current.deployment_state, target_state,
                current.model_version, row.model_version,
                current.ml_model_metadata_id, row.id,
            )


async def run(*, dry_run: bool) -> int:
    try:
        async with AsyncSessionLocal() as session:
            rows = await find_authoritative_rows(session)
            if not rows:
                logger.info("No production/active ml_model_metadata rows; nothing to repair.")
                return _EXIT_OK

            logger.info("Found %d authoritative metadata row(s) to project:", len(rows))
            warn_on_duplicates(rows)
            await report_plan(session, rows)

            if dry_run:
                logger.info(
                    "DRY RUN — no writes performed. Re-run with --execute to apply."
                )
                return _EXIT_OK

            for row in rows:
                await _project_to_ai_ml_models(session, row, row.status)
            await session.commit()
            logger.info("✓ Projected %d row(s) into ai_ml_models.", len(rows))
            return _EXIT_OK

    except Exception:
        logger.error("Repair failed", exc_info=True)
        return _EXIT_ERROR


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-project every production/active ml_model_metadata row "
        "into the ai_ml_models governance table (update-or-insert)."
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually write. Without this flag, runs as a dry run "
        "(reports the projection plan, writes nothing).",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(run(dry_run=not args.execute))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
