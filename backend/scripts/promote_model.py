"""
Cortex AI — Model Promotion CLI
=================================
Production-grade CLI for promoting ML models through lifecycle stages.

Usage:
    # Promote to staging
    python scripts/promote_model.py staging --version 1.0.0_xgboost

    # Promote to production (with quality gates + interactive confirmation)
    python scripts/promote_model.py production --version 1.0.0_xgboost --model-name xgboost

    # Dry-run (preview without changes)
    python scripts/promote_model.py production --version 1.0.0_xgboost --model-name xgboost --dry-run

    # Break-glass: bypass quality gates (requires documented reason + human confirmation)
    python scripts/promote_model.py production --version 1.0.0_xgboost --model-name xgboost \\
        --skip-gates --reason "Hotfix: p99 latency spike, champion model corrupted"

    # Rollback to previous version
    python scripts/promote_model.py rollback --model-name xgboost

BREAK-GLASS PROTOCOL
--------------------
--skip-gates is an audited break-glass mechanism.  It requires:
  1. A non-empty --reason documenting why gates are bypassed.
  2. Explicit interactive confirmation from the operator.
  3. A CRITICAL audit entry emitted to the application log.

--skip-gates is structurally impossible in the scheduled retrain path
(C1 calls ModelPromoter directly without this flag).

Author: Cortex AI Team
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# E2 — durable break-glass audit records land here (survives log rotation).
# Path is fixed relative to the backend root so operators always know where to look.
_AUDIT_DIR = Path(__file__).parent.parent / "audit"

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.ml.model_registry import BreakGlassError, ModelPromoter, QualityGate, QualityGateError
from app.ml.evaluation.promotion_report import (
    BlockedChallengerError,
    BundleChecksumError,
    PromotionStatus,
    load_and_verify_report,
)
from app.models.ml_data import MLModelMetadata


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        description="Cortex AI Model Promotion Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Promote to staging
  %(prog)s staging --version 1.0.0_xgboost
  
  # Promote to production
  %(prog)s production --version 1.0.0_xgboost --model-name xgboost
  
  # Rollback
  %(prog)s rollback --model-name xgboost

  # Demote production model to staging (C3 honest re-eval flow)
  %(prog)s demote --version 1.0.0_xgboost --reason "C3 re-eval failed A6 gates"

  # Dry-run
  %(prog)s production --version 1.0.0_xgboost --model-name xgboost --dry-run
        """,
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Promotion command")
    subparsers.required = True
    
    # Staging promotion
    staging_parser = subparsers.add_parser(
        "staging",
        help="Promote model from development to staging",
    )
    staging_parser.add_argument(
        "--version",
        required=True,
        help="Model version to promote (e.g., 1.0.0_xgboost)",
    )
    staging_parser.add_argument(
        "--skip-gates",
        action="store_true",
        help=(
            "Break-glass: bypass quality gates. "
            "Requires --reason. Action is audited."
        ),
    )
    staging_parser.add_argument(
        "--reason",
        default=None,
        help="Required when --skip-gates is used. Document why gates are bypassed.",
    )
    staging_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them",
    )

    # Production promotion
    prod_parser = subparsers.add_parser(
        "production",
        help="Promote model from staging to production",
    )
    prod_parser.add_argument(
        "--version",
        required=True,
        help="Model version to promote (e.g., 1.0.0_xgboost)",
    )
    prod_parser.add_argument(
        "--model-name",
        required=True,
        help="Model name (xgboost, gru, etc.)",
    )
    prod_parser.add_argument(
        "--skip-gates",
        action="store_true",
        help=(
            "Break-glass: bypass quality gates. "
            "Requires --reason. Action is audited."
        ),
    )
    prod_parser.add_argument(
        "--reason",
        default=None,
        help="Required when --skip-gates is used. Document why gates are bypassed.",
    )
    prod_parser.add_argument(
        "--report-path",
        required=True,
        metavar="PATH",
        help=(
            "Path to the signed C2 Promotion Report generated at the end of "
            "the challenger training run.  The CLI verifies the bundle "
            "checksum and the report status before proceeding.  Required for "
            "all production promotions; use --skip-gates --reason for "
            "break-glass when the report status is BLOCKED."
        ),
    )
    prod_parser.add_argument(
        "--dormant",
        action="store_true",
        help=(
            "Register at production tier but dormant (is_active=False). "
            "The model is auditable, versioned, and checksummed but does not "
            "serve live capital. The current champion stays active. "
            "Used for the Single-Active-Member Ensemble pattern: register a "
            "secondary model without displacing the active one."
        ),
    )
    prod_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them",
    )

    # Rollback
    rollback_parser = subparsers.add_parser(
        "rollback",
        help="Rollback to previous production model",
    )
    rollback_parser.add_argument(
        "--model-name",
        required=True,
        help="Model name (xgboost, gru, etc.)",
    )
    rollback_parser.add_argument(
        "--target-version",
        help="Specific version to rollback to (default: previous)",
    )
    rollback_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them",
    )
    
    # Demote (production → staging) — C3 honest re-eval support
    demote_parser = subparsers.add_parser(
        "demote",
        help=(
            "Demote a production model to staging (production → staging, "
            "live → paper). Atomic dual-write."
        ),
    )
    demote_parser.add_argument(
        "--version",
        required=True,
        help="Model version to demote (e.g., 1.0.0_xgboost)",
    )
    demote_parser.add_argument(
        "--reason",
        required=True,
        help=(
            "Required.  Audit-logged at WARNING.  Document why the model is "
            "being demoted (e.g., 'C3 honest re-eval — fails A6 hard gates')."
        ),
    )
    demote_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without applying them",
    )

    # Status
    status_parser = subparsers.add_parser(
        "status",
        help="Show current model status",
    )
    status_parser.add_argument(
        "--model-name",
        help="Filter by model name (optional)",
    )
    
    return parser


async def get_model_info(session: AsyncSession, version: str) -> MLModelMetadata | None:
    """Fetch model metadata."""
    stmt = select(MLModelMetadata).where(MLModelMetadata.model_version == version)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _fmt(v: object, fmt: str = ".4f") -> str:
    """Format a metric value, showing 'n/a' when absent or None."""
    return format(float(v), fmt) if v is not None else "n/a"


async def show_model_status(model: MLModelMetadata) -> None:
    """Display model status including A6 quality-gate metrics."""
    print(f"\nModel: {model.model_version}")
    print(f"  Name:     {model.model_name}")
    print(f"  Status:   {model.status}")
    print(f"  Active:   {model.is_active}")
    print(f"  Deployed: {model.deployed_at or 'Never'}")

    m = model.training_metrics or {}
    print(f"  -- Classification --")
    print(f"  AUC-PR (primary):  {_fmt(m.get('auc_pr'))}")
    print(f"  Accuracy:          {_fmt(m.get('accuracy'), '.2%') if m.get('accuracy') is not None else 'n/a'}")
    print(f"  -- Financial --")
    print(f"  Deflated Sharpe:   {_fmt(m.get('deflated_sharpe'))}")
    print(f"  Ensemble Net DSR:  {_fmt(m.get('ensemble_net_dsr'))}")
    print(f"  PBO:               {_fmt(m.get('pbo'))}")
    print(f"  -- Calibration --")
    print(f"  ECE after:         {_fmt(m.get('ece_after'))}")
    print(f"  -- Data --")
    print(f"  Symbol Coverage:   {_fmt(m.get('symbol_coverage'))}")
    print(f"  Training Samples:  {model.training_samples or 0:,}")


def _validate_break_glass_args(skip_gates: bool, reason: str | None) -> None:
    """Validate that --reason is supplied whenever --skip-gates is used."""
    if skip_gates and not (reason and reason.strip()):
        print(
            "\n❌ --skip-gates requires --reason.\n"
            "   Document why quality gates are being bypassed.\n"
            "   Example: --reason 'Hotfix: champion model artifact corrupted'\n"
        )
        sys.exit(1)


def _load_and_display_promotion_report(
    report_path: str,
    *,
    challenger_version: str,
    skip_gates: bool,
) -> dict:
    """Load, verify, and display the C2 Promotion Report.

    Returns the verified report dict.  Exits non-zero on any failure so the
    caller can proceed with confidence that the report is valid.
    """
    from pathlib import Path

    print(f"\n{'─' * 78}")
    print("C2 PROMOTION REPORT VERIFICATION")
    print(f"{'─' * 78}")
    print(f"  Report path: {report_path}")

    try:
        report = load_and_verify_report(
            Path(report_path),
            challenger_version=challenger_version,
            enforce_status=not skip_gates,
        )
    except FileNotFoundError:
        print(f"\n❌ Promotion report not found: {report_path}")
        print(
            "   Generate a report by running the orchestrator, then pass its "
            "path via --report-path."
        )
        sys.exit(1)
    except BundleChecksumError as exc:
        print(f"\n❌ BUNDLE INTEGRITY FAILURE — artefacts have been tampered with:\n")
        for line in str(exc).splitlines():
            print(f"   {line}")
        print(
            "\n   This report cannot be used.  Retrain to obtain a fresh "
            "signed report."
        )
        sys.exit(1)
    except BlockedChallengerError as exc:
        print(f"\n❌ CHALLENGER BLOCKED — quality gate failures:\n")
        for line in str(exc).splitlines():
            print(f"   {line}")
        sys.exit(1)
    except ValueError as exc:
        print(f"\n❌ Invalid promotion report: {exc}")
        sys.exit(1)

    # ── Display a concise summary for the operator ─────────────────────────
    status = report.get("status", "UNKNOWN")
    run_id = report.get("run_id", "n/a")
    gen_at = report.get("generated_at", "n/a")

    status_icon = "✅" if status == PromotionStatus.AWAITING_HUMAN_SIGNOFF.value else "🔴"
    print(f"  Bundle integrity: ✅ verified")
    print(f"  Status:           {status_icon} {status}")
    print(f"  Run ID:           {run_id}")
    print(f"  Generated at:     {gen_at}")

    challengers_in_report = report.get("challengers", {})
    deltas = report.get("deltas", {})

    for name, sec in challengers_in_report.items():
        gate_icon = "✅" if sec.get("gate_status") == PromotionStatus.AWAITING_HUMAN_SIGNOFF.value else "❌"
        print(f"\n  {name.upper()} challenger: {sec.get('model_version')}  {gate_icon}")
        m = sec.get("metrics", {})
        d = deltas.get(name, {})

        def _delta(val: float) -> str:
            return f"+{val:.4f}" if val >= 0 else f"{val:.4f}"

        print(f"    AUC-PR:          {_fmt(m.get('auc_pr'))}  "
              f"(Δ {_delta(d.get('auc_pr', 0.0))} vs champion)")
        print(f"    Deflated Sharpe: {_fmt(m.get('deflated_sharpe'))}  "
              f"(Δ {_delta(d.get('deflated_sharpe', 0.0))})")
        print(f"    PBO:             {_fmt(m.get('pbo'))}  "
              f"(Δ {_delta(d.get('pbo', 0.0))})")
        print(f"    ECE after:       {_fmt(m.get('ece_after'))}  "
              f"(Δ {_delta(d.get('ece_after', 0.0))})")

        if sec.get("gate_status") == PromotionStatus.BLOCKED.value:
            failed = sec.get("gate", {}).get("failed_checks", {})
            print(f"    Gate failures:")
            for gate, msg in failed.items():
                print(f"      ✗ {gate}: {msg}")

    print(f"{'─' * 78}")
    return report


def _prompt_break_glass_confirmation(version: str, reason: str) -> None:
    """Interactively confirm a break-glass promotion.  Cannot be automated."""
    print("\n" + "!" * 70)
    print("  BREAK-GLASS: QUALITY GATES WILL BE BYPASSED")
    print("!" * 70)
    print(f"  Model:  {version}")
    print(f"  Reason: {reason}")
    print(
        "\n  This action is logged as CRITICAL in the application audit trail.\n"
        "  It cannot be reversed without a rollback.\n"
    )
    response = input("  Type CONFIRM to proceed, anything else cancels: ").strip()
    if response != "CONFIRM":
        print("\nCancelled — break-glass promotion aborted.")
        sys.exit(0)


async def promote_to_staging(
    session:    AsyncSession,
    version:    str,
    skip_gates: bool,
    reason:     str | None,
    dry_run:    bool,
) -> None:
    """Promote model to staging."""
    _validate_break_glass_args(skip_gates, reason)

    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Promoting {version} to staging...")

    model = await get_model_info(session, version)
    if not model:
        print(f"\n❌ Error: Model {version} not found")
        sys.exit(1)

    print("\nCandidate model:")
    await show_model_status(model)

    if model.status != "development":
        print(f"\n❌ Error: Model is {model.status}; can only promote from development to staging")
        sys.exit(1)

    if dry_run:
        print(f"\n[DRY-RUN] Would promote {version} to staging")
        if skip_gates:
            print(f"  Break-glass reason: {reason}")
            print("  Quality gates would be bypassed (audited)")
        else:
            print("  Quality gates would be validated")
        return

    if skip_gates:
        _prompt_break_glass_confirmation(version, reason)

    promoter = ModelPromoter(session, audit_dir=_AUDIT_DIR)
    try:
        promoted = await promoter.promote_to_staging(
            version,
            skip_quality_gates=skip_gates,
            break_glass_reason=reason,
        )
        print(f"\n✅ Successfully promoted {version} to staging")
        await show_model_status(promoted)

    except QualityGateError as e:
        print(f"\n❌ Quality gate validation failed ({len(e.failed_checks)} gate(s)):")
        for gate, msg in e.failed_checks.items():
            print(f"   • {gate}: {msg}")
        print(
            "\n  Use --skip-gates --reason '<documented justification>' "
            "to override (audited break-glass)."
        )
        sys.exit(1)

    except Exception as e:
        print(f"\n❌ Promotion failed: {e}")
        sys.exit(1)


async def promote_to_production(
    session:     AsyncSession,
    version:     str,
    model_name:  str,
    skip_gates:  bool,
    reason:      str | None,
    dry_run:     bool,
    report_path: str,
    dormant:     bool = False,
) -> None:
    """Promote model to production.

    Requires a valid C2 Promotion Report (--report-path).  The report's bundle
    checksum is verified before any other action, then the report status is
    checked (BLOCKED blocks promotion unless --skip-gates is supplied).

    Always requires interactive confirmation.  When --skip-gates is used, the
    confirmation prompt is stricter (requires typing CONFIRM verbatim) and a
    CRITICAL audit entry is written.

    When ``dormant=True``, the model is registered at the production tier with
    ``is_active=False`` (governance: shadow).  The current champion is not
    displaced.  Used for the Single-Active-Member Ensemble pattern.
    """
    _validate_break_glass_args(skip_gates, reason)

    # ── C2: verify the promotion report before touching the DB ────────────────
    _load_and_display_promotion_report(
        report_path,
        challenger_version=version,
        skip_gates=skip_gates,
    )

    mode_label = "DORMANT" if dormant else "ACTIVE"
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Promoting {version} to production ({mode_label})...")

    model = await get_model_info(session, version)
    if not model:
        print(f"\n❌ Error: Model {version} not found")
        sys.exit(1)

    print("\nCandidate model:")
    await show_model_status(model)

    if model.status != "staging":
        print(f"\n❌ Error: Model is {model.status}; can only promote from staging to production")
        sys.exit(1)

    # Fetch current champion
    stmt = (
        select(MLModelMetadata)
        .where(
            MLModelMetadata.model_name == model_name,
            MLModelMetadata.status == "production",
            MLModelMetadata.is_active == True,
        )
        .limit(1)
    )
    current_prod = (await session.execute(stmt)).scalar_one_or_none()

    if current_prod:
        print("\nCurrent production (champion):")
        await show_model_status(current_prod)
    else:
        print(f"\nNo active production model for '{model_name}' — this will be the first.")

    if dry_run:
        print(f"\n[DRY-RUN] Would promote {version} to production/{mode_label.lower()}")
        if dormant:
            print(f"  Mode:     DORMANT — is_active=False, governance=shadow")
            print(f"  Champion: {current_prod.model_version if current_prod else 'none'} stays active")
        else:
            if current_prod:
                print(f"  Would deactivate: {current_prod.model_version}")
        if skip_gates:
            print(f"  Break-glass reason: {reason}")
            print("  Quality gates would be bypassed (audited)")
        else:
            print("  Quality gates would be validated")
        return

    # Interactive confirmation — always required for production; stricter for break-glass.
    if skip_gates:
        _prompt_break_glass_confirmation(version, reason)
    elif dormant:
        print("\n⚠️  Dormant production registration requires confirmation")
        print(f"   {version} will be registered as DORMANT (is_active=False, governance=shadow).")
        if current_prod:
            print(f"   Champion {current_prod.model_version} remains active — NOT displaced.")
        response = input("   Continue? [y/N]: ").strip()
        if response.lower() != "y":
            print("Cancelled.")
            sys.exit(0)
    else:
        print("\n⚠️  Production promotion requires confirmation")
        if current_prod:
            print(f"   Champion {current_prod.model_version} will be deactivated.")
        response = input("   Continue? [y/N]: ").strip()
        if response.lower() != "y":
            print("Cancelled.")
            sys.exit(0)

    promoter = ModelPromoter(session, audit_dir=_AUDIT_DIR)
    try:
        promoted = await promoter.promote_to_production(
            version,
            model_name,
            skip_quality_gates=skip_gates,
            break_glass_reason=reason,
            activate=not dormant,
        )
        if dormant:
            print(f"\n✅ Registered {version} at production tier (DORMANT — is_active=False)")
            print(f"   Governance state: shadow (not serving live capital)")
            print(f"   Activate later:   UPDATE ml_model_metadata SET is_active=true WHERE model_version='{version}';")
        else:
            print(f"\n✅ Successfully promoted {version} to production (ACTIVE)")
        await show_model_status(promoted)

    except QualityGateError as e:
        print(f"\n❌ Quality gate validation failed ({len(e.failed_checks)} gate(s)):")
        for gate, msg in e.failed_checks.items():
            print(f"   • {gate}: {msg}")
        print(
            "\n  Use --skip-gates --reason '<documented justification>' "
            "to override (audited break-glass)."
        )
        sys.exit(1)

    except Exception as e:
        print(f"\n❌ Promotion failed: {e}")
        sys.exit(1)


async def rollback_model(
    session: AsyncSession,
    model_name: str,
    target_version: str | None,
    dry_run: bool,
) -> None:
    """Rollback to previous production model."""
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Rolling back {model_name}...")
    
    # Fetch current production model
    stmt = (
        select(MLModelMetadata)
        .where(
            MLModelMetadata.model_name == model_name,
            MLModelMetadata.status == "production",
            MLModelMetadata.is_active == True,
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    current = result.scalar_one_or_none()
    
    if current:
        print(f"\nCurrent production model:")
        await show_model_status(current)
    else:
        print(f"\n❌ Error: No active production model for {model_name}")
        sys.exit(1)
    
    # Fetch target model
    if target_version:
        target = await get_model_info(session, target_version)
        if not target:
            print(f"\n❌ Error: Target version {target_version} not found")
            sys.exit(1)
    else:
        # Get previous production model
        stmt = (
            select(MLModelMetadata)
            .where(
                MLModelMetadata.model_name == model_name,
                MLModelMetadata.status == "production",
            )
            .order_by(MLModelMetadata.deployed_at.desc())
            .offset(1)
            .limit(1)
        )
        result = await session.execute(stmt)
        target = result.scalar_one_or_none()
        
        if not target:
            print(f"\n❌ Error: No previous production model found for {model_name}")
            sys.exit(1)
    
    print(f"\nTarget model:")
    await show_model_status(target)
    
    if dry_run:
        print(f"\n✓ Dry-run: Would rollback to {target.model_version}")
        print(f"  Would deactivate: {current.model_version}")
        return
    
    # Confirm rollback
    print("\n⚠️  Rollback requires confirmation")
    response = input("Continue? [y/N]: ")
    if response.lower() != 'y':
        print("Cancelled")
        sys.exit(0)
    
    # Rollback
    promoter = ModelPromoter(session, audit_dir=_AUDIT_DIR)
    
    try:
        rolled_back = await promoter.rollback(model_name, target_version)
        print(f"\n✅ Successfully rolled back to {rolled_back.model_version}")
        await show_model_status(rolled_back)
        
    except Exception as e:
        print(f"\n❌ Rollback failed: {e}")
        sys.exit(1)


async def demote_model(
    session:    AsyncSession,
    version:    str,
    reason:     str,
    dry_run:    bool,
) -> None:
    """Demote a production model to staging (production → staging, live → paper).

    Used by the C3 honest re-eval flow: when a production model fails the
    current A6 hard gates, the operator runs this command to atomically
    pull the model off live capital while preserving the registry record
    and artefact for future re-promotion.

    Atomic dual-write is performed by `ModelPromoter.demote_to_staging()`
    (mirror of `promote_to_production`); both `ml_model_metadata` and
    `ai_ml_models` are updated in the same DB transaction.
    """
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Demoting {version} to staging...")

    if not reason or not reason.strip():
        print("\n❌ --reason is required for demote (audit trail).")
        sys.exit(1)

    current = await get_model_info(session, version)
    if not current:
        print(f"\n❌ Error: Version {version} not found")
        sys.exit(1)

    if current.status != "production":
        print(
            f"\n❌ Error: Model {version} is in status='{current.status}'.  "
            "Demote only applies to production models."
        )
        sys.exit(1)

    print(f"\nCurrent (production) model:")
    await show_model_status(current)

    print(f"\nDemote target state:")
    print(f"  ml_model_metadata.status         → 'staging'")
    print(f"  ml_model_metadata.is_active      → False")
    print(f"  ai_ml_models.deployment_state    → 'paper'  (atomic projection)")
    print(f"  Reason (audit-logged):             {reason!r}")

    if dry_run:
        print(f"\n✓ Dry-run: would demote {version} to staging.  No state changed.")
        return

    print("\n⚠️  Demotion will pull this model off live capital.")
    response = input("Continue? [y/N]: ").strip().lower()
    if response != "y":
        print("Cancelled")
        sys.exit(0)

    promoter = ModelPromoter(session, audit_dir=_AUDIT_DIR)
    try:
        demoted = await promoter.demote_to_staging(version, reason=reason)
        print(f"\n✅ Demoted {demoted.model_version}: production → staging")
        await show_model_status(demoted)
    except Exception as e:
        print(f"\n❌ Demote failed: {e}")
        sys.exit(1)


async def show_status(session: AsyncSession, model_name: str | None) -> None:
    """Show current model status."""
    print("\n" + "=" * 80)
    print("MODEL STATUS")
    print("=" * 80)
    
    # Build query
    stmt = select(MLModelMetadata).order_by(
        MLModelMetadata.model_name,
        MLModelMetadata.deployed_at.desc().nullslast(),
    )
    
    if model_name:
        stmt = stmt.where(MLModelMetadata.model_name == model_name)
    
    result = await session.execute(stmt)
    models = result.scalars().all()
    
    if not models:
        print("No models found")
        return
    
    # Group by model name
    by_name = {}
    for model in models:
        if model.model_name not in by_name:
            by_name[model.model_name] = []
        by_name[model.model_name].append(model)
    
    # Display
    for name, model_list in by_name.items():
        print(f"\n{name.upper()}")
        print("-" * 80)
        
        for model in model_list:
            status_icon = "🟢" if model.is_active else "⚪"
            print(f"{status_icon} {model.model_version:20} {model.status:12} ", end="")

            m = model.training_metrics or {}
            ap  = m.get("auc_pr")
            dsr = m.get("deflated_sharpe")
            ece = m.get("ece_after")
            acc = m.get("accuracy")
            print(
                f"auc_pr={_fmt(ap) if ap is not None else 'n/a':6}  "
                f"dsr={_fmt(dsr) if dsr is not None else 'n/a':6}  "
                f"ece={_fmt(ece) if ece is not None else 'n/a':6}  "
                f"acc={f'{acc:.2%}' if acc is not None else 'n/a':7}  ",
                end="",
            )

            if model.deployed_at:
                print(f"deployed={model.deployed_at.strftime('%Y-%m-%d %H:%M')}")
            else:
                print()
    
    print("\n" + "=" * 80)


async def main() -> None:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args()
    
    # Connect to database
    settings = get_settings()
    engine = create_async_engine(str(settings.DATABASE_URL), echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    try:
        async with Session() as session:
            if args.command == "staging":
                await promote_to_staging(
                    session,
                    args.version,
                    args.skip_gates,
                    getattr(args, "reason", None),
                    args.dry_run,
                )

            elif args.command == "production":
                await promote_to_production(
                    session,
                    args.version,
                    args.model_name,
                    args.skip_gates,
                    getattr(args, "reason", None),
                    args.dry_run,
                    args.report_path,
                    getattr(args, "dormant", False),
                )
            
            elif args.command == "rollback":
                await rollback_model(
                    session,
                    args.model_name,
                    args.target_version,
                    args.dry_run,
                )

            elif args.command == "demote":
                await demote_model(
                    session,
                    args.version,
                    args.reason,
                    args.dry_run,
                )

            elif args.command == "status":
                await show_status(session, args.model_name)
    
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
