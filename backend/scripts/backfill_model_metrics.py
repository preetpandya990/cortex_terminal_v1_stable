"""
Cortex AI — Backfill Model Metrics
===================================
One-time script: backfill evaluation metrics into model registry records
that were registered with accuracy=0.

This script reads the training results JSON and updates the database records
for models 12 (XGBoost) and 13 (GRU) with their actual evaluation metrics
and ensemble weights.

Run:
    python scripts/backfill_model_metrics.py

Author: Cortex AI Team
Date: 2026-04-20
"""
import asyncio
import json
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.ml_data import MLModelMetadata

RESULTS_PATH = Path("models/production/training_results_20260420_114151.json")

# Map registry versions to evaluation result keys
METRIC_MAP = {
    "1.0.0_xgboost": "xgboost",
    "1.0.0_gru":     "gru",
}

# Ensemble weights from the optimizer output (XGB=0.75, GRU=0.25)
ENSEMBLE_WEIGHTS = {
    "1.0.0_xgboost": 0.75,
    "1.0.0_gru":     0.25,
}


async def main() -> None:
    """Backfill evaluation metrics into registry records."""
    # Load training results
    if not RESULTS_PATH.exists():
        print(f"ERROR: Training results not found at {RESULTS_PATH}")
        print("Expected path: models/production/training_results_20260420_114151.json")
        sys.exit(1)
    
    with open(RESULTS_PATH) as f:
        results = json.load(f)
    
    eval_results = results["evaluation_results"]
    total_samples = results.get("total_samples", 0)
    
    print(f"Loaded training results from {RESULTS_PATH}")
    print(f"Total training samples: {total_samples:,}")
    print()
    
    # Connect to database
    settings = get_settings()
    engine = create_async_engine(str(settings.DATABASE_URL), echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    
    updated_count = 0
    
    async with Session() as session:
        for version, model_key in METRIC_MAP.items():
            # Find the model record
            stmt = select(MLModelMetadata).where(
                MLModelMetadata.model_version == version
            )
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            
            if not record:
                print(f"WARNING: Model version '{version}' not found in registry — skipping")
                continue
            
            # Get evaluation metrics for this model
            metrics = eval_results.get(model_key, {})
            if not metrics:
                print(f"WARNING: No evaluation metrics found for '{model_key}' — skipping")
                continue
            
            # Add ensemble weight to metrics (for RegistryModelLoader to read later)
            metrics["ensemble_weight"] = ENSEMBLE_WEIGHTS[version]
            
            # Update the record
            record.training_metrics = metrics
            record.validation_metrics = metrics  # Same metrics for both (single validation set)
            record.training_samples = total_samples
            
            acc = metrics.get("accuracy", 0.0)
            f1_up = metrics.get("f1_score", {}).get("up", 0.0)
            f1_down = metrics.get("f1_score", {}).get("down", 0.0)
            
            print(f"✓ Updated {version}:")
            print(f"    accuracy:        {acc:.4f}")
            print(f"    f1_score (UP):   {f1_up:.4f}")
            print(f"    f1_score (DOWN): {f1_down:.4f}")
            print(f"    ensemble_weight: {metrics['ensemble_weight']:.2f}")
            print(f"    training_samples: {total_samples:,}")
            print()
            
            updated_count += 1
        
        # Commit all changes
        await session.commit()
        print(f"✓ Successfully updated {updated_count} model record(s)")
        print("Registry metrics backfill complete.")
    
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
