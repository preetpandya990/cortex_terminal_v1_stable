"""
Cortex AI — Model Promotion CLI
=================================
Production-grade CLI for promoting ML models through lifecycle stages.

Features:
- Promote models: development → staging → production
- Automated quality gate validation
- Rollback support
- Dry-run mode for safety
- Comprehensive status reporting

Usage:
    # Promote to staging
    python scripts/promote_model.py staging --version 1.0.0_xgboost
    
    # Promote to production (with quality gates)
    python scripts/promote_model.py production --version 1.0.0_xgboost --model-name xgboost
    
    # Rollback to previous version
    python scripts/promote_model.py rollback --model-name xgboost
    
    # Dry-run (preview without changes)
    python scripts/promote_model.py production --version 1.0.0_xgboost --model-name xgboost --dry-run
    
    # Skip quality gates (not recommended)
    python scripts/promote_model.py production --version 1.0.0_xgboost --model-name xgboost --skip-gates

Author: Cortex AI Team
Date: 2026-04-20
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.ml.model_registry import ModelPromoter, QualityGate, QualityGateError
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
        help="Skip quality gate validation (not recommended)",
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
        help="Skip quality gate validation (not recommended)",
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


async def show_model_status(model: MLModelMetadata) -> None:
    """Display model status."""
    print(f"\nModel: {model.model_version}")
    print(f"  Name: {model.model_name}")
    print(f"  Status: {model.status}")
    print(f"  Active: {model.is_active}")
    print(f"  Deployed: {model.deployed_at or 'Never'}")
    
    metrics = model.training_metrics or {}
    print(f"  Accuracy: {metrics.get('accuracy', 0):.2%}")
    print(f"  Sharpe Ratio: {metrics.get('sharpe_ratio', 0):.2f}")
    print(f"  Training Samples: {model.training_samples or 0:,}")


async def promote_to_staging(
    session: AsyncSession,
    version: str,
    skip_gates: bool,
    dry_run: bool,
) -> None:
    """Promote model to staging."""
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Promoting {version} to staging...")
    
    # Fetch model
    model = await get_model_info(session, version)
    if not model:
        print(f"❌ Error: Model {version} not found")
        sys.exit(1)
    
    print(f"\nCurrent status:")
    await show_model_status(model)
    
    if model.status != "development":
        print(f"\n❌ Error: Model is already {model.status}")
        print("   Can only promote from development to staging")
        sys.exit(1)
    
    if dry_run:
        print(f"\n✓ Dry-run: Would promote {version} to staging")
        if not skip_gates:
            print("  Quality gates would be validated")
        return
    
    # Promote
    promoter = ModelPromoter(session)
    
    try:
        promoted = await promoter.promote_to_staging(version, skip_gates)
        print(f"\n✅ Successfully promoted {version} to staging")
        await show_model_status(promoted)
        
    except QualityGateError as e:
        print(f"\n❌ Quality gate validation failed:")
        for check, reason in e.failed_checks.items():
            print(f"   • {check}: {reason}")
        print("\nUse --skip-gates to bypass (not recommended)")
        sys.exit(1)
        
    except Exception as e:
        print(f"\n❌ Promotion failed: {e}")
        sys.exit(1)


async def promote_to_production(
    session: AsyncSession,
    version: str,
    model_name: str,
    skip_gates: bool,
    dry_run: bool,
) -> None:
    """Promote model to production."""
    print(f"\n{'[DRY-RUN] ' if dry_run else ''}Promoting {version} to production...")
    
    # Fetch model
    model = await get_model_info(session, version)
    if not model:
        print(f"❌ Error: Model {version} not found")
        sys.exit(1)
    
    print(f"\nCandidate model:")
    await show_model_status(model)
    
    if model.status != "staging":
        print(f"\n❌ Error: Model is {model.status}")
        print("   Can only promote from staging to production")
        sys.exit(1)
    
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
    current_prod = result.scalar_one_or_none()
    
    if current_prod:
        print(f"\nCurrent production model:")
        await show_model_status(current_prod)
    else:
        print(f"\nNo current production model for {model_name}")
    
    if dry_run:
        print(f"\n✓ Dry-run: Would promote {version} to production")
        if current_prod:
            print(f"  Would deactivate: {current_prod.model_version}")
        if not skip_gates:
            print("  Quality gates would be validated")
        return
    
    # Confirm production promotion
    if not skip_gates:
        print("\n⚠️  Production promotion requires confirmation")
        response = input("Continue? [y/N]: ")
        if response.lower() != 'y':
            print("Cancelled")
            sys.exit(0)
    
    # Promote
    promoter = ModelPromoter(session)
    
    try:
        promoted = await promoter.promote_to_production(version, model_name, skip_gates)
        print(f"\n✅ Successfully promoted {version} to production")
        await show_model_status(promoted)
        
    except QualityGateError as e:
        print(f"\n❌ Quality gate validation failed:")
        for check, reason in e.failed_checks.items():
            print(f"   • {check}: {reason}")
        print("\nUse --skip-gates to bypass (not recommended)")
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
    promoter = ModelPromoter(session)
    
    try:
        rolled_back = await promoter.rollback(model_name, target_version)
        print(f"\n✅ Successfully rolled back to {rolled_back.model_version}")
        await show_model_status(rolled_back)
        
    except Exception as e:
        print(f"\n❌ Rollback failed: {e}")
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
            
            metrics = model.training_metrics or {}
            acc = metrics.get("accuracy", 0)
            sharpe = metrics.get("sharpe_ratio", 0)
            print(f"acc={acc:.2%} sharpe={sharpe:.2f} ", end="")
            
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
                    args.dry_run,
                )
            
            elif args.command == "production":
                await promote_to_production(
                    session,
                    args.version,
                    args.model_name,
                    args.skip_gates,
                    args.dry_run,
                )
            
            elif args.command == "rollback":
                await rollback_model(
                    session,
                    args.model_name,
                    args.target_version,
                    args.dry_run,
                )
            
            elif args.command == "status":
                await show_status(session, args.model_name)
    
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
