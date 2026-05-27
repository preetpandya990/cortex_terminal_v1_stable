"""
C3 — Honest re-evaluation of the live production ensemble.

Audits the currently-promoted production models against the post-A6 hard
gates, synthesises additional structural findings the gates cannot express
(obsolete feature schema, missing calibrator, fabricated 0.0 Sharpe from the
A1-fixed downgrade bug), and writes a structured incident report
recommending operator action.

**REPORT-ONLY.**  Never mutates registry state.  When the verdict is
``DEMOTE_RECOMMENDED`` the operator applies the demotion via
``python scripts/promote_model.py demote --version <ver> --reason <reason>``
(the new C3 subcommand wired in this same change set).

Usage:
    python scripts/reeval_production_model.py
    python scripts/reeval_production_model.py --output-dir /tmp/cortex-reports

Exit codes:
    0 — every audited model meets the bar
    1 — operational error (no production models found, DB error, etc.)
    2 — at least one model fails the bar (DEMOTE_RECOMMENDED)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make ``app.*`` importable when this script is run directly (matches the
# convention used by every other script in backend/scripts/).
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.ml.model_registry import QualityGate, QualityGateError
from app.models.ml_data import MLModelMetadata

# ── Constants ─────────────────────────────────────────────────────────────────
# Members of the live ensemble that C3 audits.  These names match the
# ``model_name`` column in ``ml_model_metadata``; any record with these names
# in ``status='production' AND is_active=True`` is in scope.
PRODUCTION_MEMBERS: tuple[str, ...] = ("xgboost", "gru")

# The current feature pipeline produces 69 features (44 technical + 5 sentiment
# + 20 fundamental).  Models trained on fewer features are structurally
# obsolete — they cannot consume the current feature stream without a legacy
# subset extractor, which itself signals an out-of-date governance posture.
EXPECTED_CURRENT_FEATURE_COUNT = 69


def _audit_one_model(meta: MLModelMetadata, qg: QualityGate) -> dict[str, Any]:
    """Build the per-model audit subsection of the incident report.

    Combines two complementary signals:
      1. The A6 ``QualityGate`` verdict against stored metrics.
      2. Structural / lineage findings the gates cannot express
         (obsolete schema, missing calibrator, fabricated 0.0 Sharpe).
    """
    metrics  = meta.training_metrics or {}
    features = meta.training_features or []

    # ── (a) A6 hard-gate verdict against stored metrics ───────────────────────
    try:
        qg.validate(meta, previous_model=None)
        gate_passed = True
        failed: dict[str, str] = {}
    except QualityGateError as exc:
        gate_passed = False
        failed = exc.failed_checks or {}

    # ── (b) Structural / lineage findings ─────────────────────────────────────
    findings: list[dict[str, Any]] = []

    # (b.1) Obsolete feature schema (current pipeline is 69 features).
    n_features_stored = len(features)
    if n_features_stored and n_features_stored < EXPECTED_CURRENT_FEATURE_COUNT:
        findings.append({
            "kind":          "obsolete_feature_schema",
            "stored":        n_features_stored,
            "current":       EXPECTED_CURRENT_FEATURE_COUNT,
            "missing_count": EXPECTED_CURRENT_FEATURE_COUNT - n_features_stored,
            "note": (
                f"Trained on {n_features_stored} features; current pipeline "
                f"produces {EXPECTED_CURRENT_FEATURE_COUNT}.  Re-inference "
                "would require a legacy feature-subset extractor — the "
                "model cannot honestly consume the current feature stream."
            ),
        })

    # (b.2) Calibrator presence (A4 prerequisite for honest ECE).
    cal_path = _expected_calibrator_path(meta)
    if cal_path is None:
        findings.append({
            "kind":          "calibrator_missing",
            "expected_path": None,
            "note": (
                "onnx_path is unset; cannot derive expected calibrator path. "
                "Model predates A4 leakage-safe calibration."
            ),
        })
    elif not cal_path.exists():
        findings.append({
            "kind":          "calibrator_missing",
            "expected_path": str(cal_path),
            "note": (
                "No calibrator file at the expected serving path.  Model "
                "predates A4; ECE cannot be evaluated and serving probabilities "
                "are uncalibrated."
            ),
        })

    # (b.3) Fabricated 0.0 Sharpe (the silent downgrade bug A1 closed).
    stored_sharpe   = metrics.get("sharpe_ratio")
    stored_n_trades = metrics.get("n_trades", 0)
    if stored_sharpe == 0.0 and stored_n_trades == 0:
        findings.append({
            "kind":              "fabricated_zero_sharpe",
            "stored_sharpe":     stored_sharpe,
            "stored_n_trades":   stored_n_trades,
            "note": (
                "sharpe_ratio=0.0 with n_trades=0 matches the A1-fixed "
                "silent Sharpe→accuracy downgrade pattern.  This is the "
                "fabricated zero, not a real measurement."
            ),
        })

    return {
        "model_id":         meta.model_id,
        "model_name":       meta.model_name,
        "model_version":    meta.model_version,
        "status":           meta.status,
        "is_active":        meta.is_active,
        "trained_at":       meta.trained_at.isoformat()  if meta.trained_at  else None,
        "deployed_at":      meta.deployed_at.isoformat() if meta.deployed_at else None,
        "training_samples": meta.training_samples,
        "n_features_stored": n_features_stored,
        "stored_metrics":   metrics,
        "a6_gate": {
            "passed":       gate_passed,
            "failed_gates": failed,
            "n_failed":     len(failed),
        },
        "structural_findings": findings,
    }


def _expected_calibrator_path(meta: MLModelMetadata) -> Path | None:
    """Where ``RegistryModelLoader`` looks for the calibrator at serving time.

    Returns ``None`` when ``onnx_path`` is unset (cannot derive a path).
    """
    if not meta.onnx_path:
        return None
    serving_dir = Path(meta.onnx_path).parent
    # The A4 + A8 serving path uses three-letter model-type prefixes.
    suffix = meta.model_name[:3].lower()
    return serving_dir / f"calibrator_{suffix}.pkl"


def _build_recommendation(audited: list[dict[str, Any]], verdict: str) -> str:
    """Compose the operator next-step block, with concrete CLI invocations."""
    if verdict == "MEETS_BAR":
        return (
            "No action required — every audited production model meets the "
            "current A6 hard-gate spec."
        )

    failing = [a for a in audited if not a["a6_gate"]["passed"]]
    lines: list[str] = [
        "One or more production models fail the current A6 hard-gate spec.",
        "Each must be demoted (production → staging, live → paper) until a "
        "qualified successor exists.",
        "",
        "Failing models:",
    ]
    for a in failing:
        lines.append(
            f"  - {a['model_name']:>8s}  v={a['model_version']}  "
            f"failed_gates={a['a6_gate']['n_failed']}  "
            f"structural_findings={len(a['structural_findings'])}"
        )
    lines += [
        "",
        "Apply the demotion via (each model demoted independently, atomically):",
        "",
        "  source .venv/bin/activate",
    ]
    for a in failing:
        gate_msg = ", ".join(a["a6_gate"]["failed_gates"].keys()) or "see report"
        lines.append(
            "  python scripts/promote_model.py demote "
            f"--version {a['model_version']} "
            f"--reason 'C3 honest re-eval — fails A6 hard gates: {gate_msg}'"
        )
    lines += [
        "",
        "Each `demote` command:",
        "  * is atomic across ml_model_metadata + ai_ml_models (A8 invariant)",
        "  * is audited at WARNING via the dedicated audit logger",
        "  * preserves the model artefact + registry row for future re-promotion",
        "",
        "Restoration path: train a qualified successor + `promote_model.py "
        "production`.  The demoted model can be re-activated only via the "
        "audited break-glass path (--skip-gates --reason).",
    ]
    return "\n".join(lines)


async def _fetch_active_production(
    session: Any,
    model_name: str,
) -> MLModelMetadata | None:
    """Latest active production record for *model_name*, or None."""
    stmt = (
        select(MLModelMetadata)
        .where(
            MLModelMetadata.model_name == model_name,
            MLModelMetadata.status == "production",
            MLModelMetadata.is_active == True,    # noqa: E712 — SQLAlchemy comparator
        )
        .order_by(MLModelMetadata.deployed_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _print_console_summary(
    audited: list[dict[str, Any]],
    verdict: str,
    report_path: Path,
    rec_action: str,
) -> None:
    border = "=" * 78
    print(border)
    print(f"C3 HONEST RE-EVALUATION  ({datetime.now(timezone.utc).isoformat()})")
    print(border)
    for a in audited:
        gate     = a["a6_gate"]
        n_struct = len(a["structural_findings"])
        print(
            f"\n  {a['model_name']:>8s}  v={a['model_version']}"
            f"  status={a['status']}  is_active={a['is_active']}"
        )
        verdict_word = "PASS" if gate["passed"] else "FAIL"
        print(
            f"     A6 gate: {verdict_word}"
            f"  ({gate['n_failed']} hard-gate failure(s))"
        )
        for name, msg in (gate["failed_gates"] or {}).items():
            print(f"        ✗ {name}: {msg}")
        print(f"     Structural findings: {n_struct}")
        for f in a["structural_findings"]:
            print(f"        ! {f['kind']}: {f['note']}")
    print()
    print(f"VERDICT:             {verdict}")
    print(f"REPORT WRITTEN TO:   {report_path}")
    print()
    print("RECOMMENDED OPERATOR ACTION:")
    for line in rec_action.splitlines():
        print(f"   {line}")
    print(border)


async def run_reeval(output_dir: Path) -> int:
    """Run the C3 honest re-evaluation and write an incident report.

    Returns the process exit code (0=meets bar, 1=op error, 2=demote
    recommended) so the CLI shell can react.
    """
    settings = get_settings()
    engine   = create_async_engine(str(settings.DATABASE_URL), echo=False)
    Session  = async_sessionmaker(engine, expire_on_commit=False)
    qg       = QualityGate()
    audited: list[dict[str, Any]] = []

    try:
        async with Session() as ses:
            for model_name in PRODUCTION_MEMBERS:
                row = await _fetch_active_production(ses, model_name)
                if row is None:
                    print(
                        f"  ! no active production record for model_name={model_name}",
                        file=sys.stderr,
                    )
                    continue
                audited.append(_audit_one_model(row, qg))
    finally:
        await engine.dispose()

    if not audited:
        print(
            "✗ No active production models found.  Nothing to re-evaluate.",
            file=sys.stderr,
        )
        return 1

    any_failed = any(not a["a6_gate"]["passed"] for a in audited)
    verdict    = "DEMOTE_RECOMMENDED" if any_failed else "MEETS_BAR"
    rec_action = _build_recommendation(audited, verdict)

    report = {
        "report_kind":   "c3_honest_reeval",
        "generated_at":  datetime.now(timezone.utc).isoformat(),
        "report_only":   True,    # C3 spec: this script never mutates registry state
        "audited_models": audited,
        "verdict":         verdict,
        "recommended_action": rec_action,
        "audit_basis": {
            "a6_quality_gate_class":  "app.ml.model_registry.QualityGate",
            "a6_hard_gates_checked": [
                "auc_pr", "deflated_sharpe", "calibration_ece",
                "symbol_coverage", "training_samples", "accuracy",
                "metadata_completeness",
            ],
            "current_feature_schema_baseline": EXPECTED_CURRENT_FEATURE_COUNT,
        },
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = output_dir / f"reeval_1.0.0_{ts}.json"
    path.write_text(json.dumps(report, indent=2, default=str))

    _print_console_summary(audited, verdict, path, rec_action)
    return 0 if verdict == "MEETS_BAR" else 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="C3 — Honest re-evaluation of the live production ensemble.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            "  0  every audited model meets the A6 bar\n"
            "  1  operational error (no production models, DB unreachable, etc.)\n"
            "  2  at least one model fails the bar → DEMOTE_RECOMMENDED\n"
        ),
    )
    default_out = Path(__file__).parent.parent / "incident_reports"
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_out,
        help=f"Directory for the JSON incident report (default: {default_out})",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    sys.exit(asyncio.run(run_reeval(args.output_dir)))


if __name__ == "__main__":
    main()
