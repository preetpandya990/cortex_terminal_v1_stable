"""
One-off governance-table cleanup: purge test/dead rows from ``ai_ml_models``.

Context
-------
WS4 of the ML Fix & Upgrade plan (ML_FIX_IMPLEMENTATION_PLAN.md). The
``ai_ml_models`` governance table contains leftovers from the Task-9.7 drift
E2E test (``scripts/test_task_9_7_drift_detection.py``): rows named
``test_drift_model_<epoch>`` with ``model_type='lstm'`` — a model type that no
longer exists in the ensemble (serving is xgboost + gru only). Each test row
also accumulated ~340 ``ai_drift_reports`` rows (no FK — deleting the models
alone would orphan them), so linked drift reports are purged in the same
transaction.

Must run **BEFORE** ``scripts/repair_ai_ml_models_projection.py`` so the
projection repair never has to disambiguate real rows from test debris.

Safety
------
  - Dry run by default; ``--execute`` required to write anything.
  - Every candidate row is listed before deletion (dry-run and execute).
  - Hard abort if any matched row has a protected ``model_type``
    ('xgboost', 'gru') — the serving ensemble is untouchable here.
  - Hard abort if more than ``--cap`` model rows match (default 20): a match
    count that large means the pattern caught something it shouldn't have.
  - Single transaction: models + their drift reports commit (or roll back)
    together.

Usage
-----
  python scripts/purge_test_model_rows.py              # dry run (default)
  python scripts/purge_test_model_rows.py --execute    # actually deletes

Exit codes
----------
  0  success (including "nothing to purge")
  2  unexpected error, or a safety guard aborted the run
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

from sqlalchemy import delete, func, or_, select  # noqa: E402

from app.ai.fusion.models import AIDriftReport, AIMLModel  # noqa: E402
from app.core.database import AsyncSessionLocal  # noqa: E402

logger = logging.getLogger("purge_test_model_rows")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_EXIT_OK = 0
_EXIT_ERROR = 2

#: Naming pattern the drift E2E test used for its throwaway governance rows.
TEST_MODEL_NAME_PATTERN = "test_drift_model_%"

#: Model type that no longer exists anywhere in the serving ensemble.
DEAD_MODEL_TYPE = "lstm"

#: Model types that serve production traffic — matching one of these means the
#: selection criteria are wrong, and the run must abort rather than delete.
PROTECTED_MODEL_TYPES = frozenset({"xgboost", "gru"})

#: Default upper bound on deletable model rows; more matches than this means
#: the pattern caught something unexpected.
DEFAULT_SANITY_CAP = 20


class PurgeAborted(Exception):
    """A safety guard refused the purge; nothing was written."""


async def find_purge_candidates(session) -> list[AIMLModel]:
    """Return governance rows that are test debris or a dead model type."""
    stmt = (
        select(AIMLModel)
        .where(
            or_(
                AIMLModel.model_name.like(TEST_MODEL_NAME_PATTERN),
                AIMLModel.model_type == DEAD_MODEL_TYPE,
            )
        )
        .order_by(AIMLModel.id)
    )
    return list((await session.execute(stmt)).scalars().all())


def assert_candidates_safe(candidates: list[AIMLModel], cap: int) -> None:
    """Abort (raise PurgeAborted) if the selection looks wrong."""
    protected = [m for m in candidates if m.model_type in PROTECTED_MODEL_TYPES]
    if protected:
        raise PurgeAborted(
            f"Selection matched {len(protected)} protected row(s) "
            f"({[m.model_name for m in protected]}) — criteria are wrong, aborting."
        )
    if len(candidates) > cap:
        raise PurgeAborted(
            f"Selection matched {len(candidates)} rows, above the sanity cap of "
            f"{cap} — refusing to mass-delete. Review the criteria (or raise "
            f"--cap deliberately) and re-run."
        )


async def count_linked_drift_reports(session, model_ids: list[int]) -> dict[int, int]:
    """Per-model count of ai_drift_reports rows referencing the candidates."""
    if not model_ids:
        return {}
    stmt = (
        select(AIDriftReport.model_id, func.count())
        .where(AIDriftReport.model_id.in_(model_ids))
        .group_by(AIDriftReport.model_id)
    )
    return dict((await session.execute(stmt)).all())


def log_candidates(candidates: list[AIMLModel], report_counts: dict[int, int]) -> None:
    logger.info("Matched %d ai_ml_models row(s) for purge:", len(candidates))
    for m in candidates:
        logger.info(
            "  id=%-4d name=%-30s type=%-8s state=%-8s version=%-20s "
            "drift_reports=%d",
            m.id, m.model_name, m.model_type, m.deployment_state,
            m.model_version, report_counts.get(m.id, 0),
        )


async def purge(session, model_ids: list[int]) -> tuple[int, int]:
    """Delete drift reports then model rows; returns (n_reports, n_models).

    Caller owns the transaction (commit/rollback).
    """
    reports_result = await session.execute(
        delete(AIDriftReport).where(AIDriftReport.model_id.in_(model_ids))
    )
    models_result = await session.execute(
        delete(AIMLModel).where(AIMLModel.id.in_(model_ids))
    )
    return reports_result.rowcount, models_result.rowcount


async def run(*, dry_run: bool, cap: int = DEFAULT_SANITY_CAP) -> int:
    try:
        async with AsyncSessionLocal() as session:
            candidates = await find_purge_candidates(session)
            if not candidates:
                logger.info("No test/dead model rows found; nothing to purge.")
                return _EXIT_OK

            assert_candidates_safe(candidates, cap)

            model_ids = [m.id for m in candidates]
            report_counts = await count_linked_drift_reports(session, model_ids)
            log_candidates(candidates, report_counts)
            total_reports = sum(report_counts.values())

            if dry_run:
                logger.info(
                    "DRY RUN — would delete %d ai_ml_models row(s) and %d linked "
                    "ai_drift_reports row(s). Re-run with --execute to apply.",
                    len(candidates), total_reports,
                )
                return _EXIT_OK

            n_reports, n_models = await purge(session, model_ids)
            await session.commit()
            logger.info(
                "✓ Purged %d ai_ml_models row(s) and %d ai_drift_reports row(s).",
                n_models, n_reports,
            )
            return _EXIT_OK

    except PurgeAborted as guard:
        logger.error("ABORTED: %s", guard)
        return _EXIT_ERROR
    except Exception:
        logger.error("Purge failed", exc_info=True)
        return _EXIT_ERROR


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Purge test/dead rows (test_drift_model_%%, model_type='lstm') "
        "and their linked drift reports from the ai_ml_models governance table."
    )
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually delete. Without this flag, runs as a dry run "
        "(lists what would be deleted, writes nothing).",
    )
    parser.add_argument(
        "--cap", type=int, default=DEFAULT_SANITY_CAP,
        help=f"Abort if more than this many model rows match "
        f"(default {DEFAULT_SANITY_CAP}).",
    )
    args = parser.parse_args()

    exit_code = asyncio.run(run(dry_run=not args.execute, cap=args.cap))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
