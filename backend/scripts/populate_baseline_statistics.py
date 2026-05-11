"""
Populate Baseline Prediction Statistics for Drift Detection

Generates ensemble predictions on a representative sample of recent historical
symbols to establish the confidence-score distribution used as the drift baseline.

The baseline is stored in MLModelMetadata.training_prediction_stats for every
active production model.  The drift detector later compares live prediction
distributions against this baseline using a z-score / KS test approach.

Usage:
    python scripts/populate_baseline_statistics.py
    python scripts/populate_baseline_statistics.py --sample-size 2000
    python scripts/populate_baseline_statistics.py --dry-run
"""
from __future__ import annotations

import asyncio
import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.database import AsyncSessionLocal
from app.core.redis import init_redis, close_redis, get_redis
from app.models.ml_data import MLModelMetadata
from app.ml.inference.registry_loader import RegistryModelLoader, LoadedEnsemble
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.ml.inference.feature_loader import FeatureLoader
from app.models.upstox_data import UpstoxOHLCV

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Minimum predictions needed to accept the baseline as valid
_MIN_VALID_PREDICTIONS = 50
# Calendar days to look back when selecting candidate symbols
_SYMBOL_LOOKBACK_DAYS = 90
# Minimum trading-day candles a symbol must have in that window to be eligible
_MIN_CANDLES = 45


class BaselineComputer:
    """
    Computes the baseline prediction-confidence distribution for drift monitoring.

    Strategy
    --------
    1. Collect a representative sample of actively-traded symbols.
    2. Run the production ensemble on each symbol's current features.
    3. Record the confidence score (0–1) from each prediction.
    4. Derive mean / std / percentiles from the collected scores.
    5. Persist the statistics in MLModelMetadata.training_prediction_stats
       for every active production model (all models share the same ensemble
       confidence distribution as their baseline).

    The stored format is intentionally flat so the drift detector can read it
    with simple dict.get('mean') / dict.get('std') calls:

        {
            "mean":              float,
            "std":               float,
            "min":               float,
            "max":               float,
            "p25":               float,
            "p50":               float,
            "p75":               float,
            "p90":               float,
            "sample_size":       int,
            "high_conf_mean":    float,   # mean of predictions >= confidence_threshold
            "high_conf_std":     float,
            "high_conf_count":   int,
            "confidence_threshold": float,
            "computed_at":       str,     # ISO-8601 UTC
            "data_window_days":  int,
        }
    """

    def __init__(
        self,
        db: AsyncSession,
        redis: Any,
        sample_size: int = 1000,
        confidence_threshold: float = 0.60,
        dry_run: bool = False,
    ) -> None:
        self.db = db
        self.redis = redis
        self.sample_size = sample_size
        self.confidence_threshold = confidence_threshold
        self.dry_run = dry_run

    # ── public ────────────────────────────────────────────────────────────────

    async def run(self) -> dict[str, Any]:
        """Run the full baseline computation pipeline."""
        logger.info(
            "Baseline computation started  sample_size=%d  dry_run=%s",
            self.sample_size, self.dry_run,
        )

        # 1. Load production ensemble (single load, reused for all symbols)
        loader = RegistryModelLoader(session=self.db, num_threads=4, use_gpu=False)
        ensemble: LoadedEnsemble = await loader.load_production_ensemble()
        predictor = EnsemblePredictor.from_loaded_ensemble(ensemble)
        feature_loader = FeatureLoader(
            db=self.db,
            redis=self.redis,
            sequence_length=ensemble.sequence_length,
            n_features=ensemble.n_features,
            feature_names=ensemble.feature_names,
        )

        logger.info(
            "Ensemble loaded: n_features=%d  sequence_length=%d",
            ensemble.n_features, ensemble.sequence_length,
        )

        # 2. Collect candidate symbols
        symbols = await self._candidate_symbols()
        if not symbols:
            raise RuntimeError(
                f"No eligible symbols found (need >= {_MIN_CANDLES} daily candles "
                f"in the last {_SYMBOL_LOOKBACK_DAYS} days)."
            )
        logger.info("Candidate symbols: %d", len(symbols))

        # 3. Generate predictions (one per unique symbol, no timestamp iteration)
        confidences = await self._predict_symbols(symbols, predictor, feature_loader)

        if len(confidences) < _MIN_VALID_PREDICTIONS:
            raise RuntimeError(
                f"Only {len(confidences)} valid predictions generated "
                f"(minimum {_MIN_VALID_PREDICTIONS} required). "
                "Check feature store data coverage."
            )

        # 4. Compute statistics
        stats = self._compute_statistics(confidences)
        logger.info(
            "Baseline stats: mean=%.4f  std=%.4f  n=%d  "
            "high_conf_mean=%.4f (n=%d)",
            stats["mean"], stats["std"], stats["sample_size"],
            stats["high_conf_mean"], stats["high_conf_count"],
        )

        # 5. Persist to all active production models
        if not self.dry_run:
            await self._persist(stats)
        else:
            logger.info("Dry-run: skipping database write")

        return stats

    # ── private helpers ────────────────────────────────────────────────────────

    async def _candidate_symbols(self) -> list[str]:
        """Return symbols with sufficient recent daily candles, randomly ordered."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=_SYMBOL_LOOKBACK_DAYS)

        stmt = (
            select(UpstoxOHLCV.instrument_key)
            .where(
                UpstoxOHLCV.timeframe == "1D",   # DB stores daily as '1D'
                UpstoxOHLCV.timestamp >= cutoff,
            )
            .group_by(UpstoxOHLCV.instrument_key)
            .having(func.count(UpstoxOHLCV.timestamp) >= _MIN_CANDLES)
            .order_by(func.random())
            # Request 3× the desired sample to account for feature-load failures
            .limit(self.sample_size * 3)
        )

        result = await self.db.execute(stmt)
        return [row[0] for row in result.fetchall()]

    async def _predict_symbols(
        self,
        symbols: list[str],
        predictor: EnsemblePredictor,
        feature_loader: FeatureLoader,
    ) -> np.ndarray:
        """
        Run ensemble inference on each symbol and collect confidence scores.

        One inference call per unique symbol — feature_loader always returns
        current features, so there is no benefit in iterating over timestamps
        for the same symbol.
        """
        confidences: list[float] = []
        failed = 0
        target = min(self.sample_size, len(symbols))

        for i, symbol in enumerate(symbols):
            if len(confidences) >= target:
                break

            try:
                tabular, sequence, price, vol = await feature_loader.load_features(
                    symbol=symbol,
                    timeframe="1d",
                )
                result = await predictor.predict(
                    features_tabular=tabular,
                    features_sequence=sequence,
                    symbol=symbol,
                    current_price=price,
                    volatility=vol,
                    use_cache=False,
                )
                confidences.append(float(result["confidence"]))

            except Exception as exc:
                failed += 1
                if failed <= 10:
                    logger.debug("Skipped %s: %s", symbol, exc)
                continue

            if (i + 1) % 100 == 0 or len(confidences) == target:
                logger.info(
                    "Progress: %d/%d predictions  (%d failed so far)",
                    len(confidences), target, failed,
                )

        logger.info(
            "Prediction complete: %d valid  %d failed  %d symbols attempted",
            len(confidences), failed, i + 1,
        )
        return np.array(confidences, dtype=np.float64)

    def _compute_statistics(self, confidences: np.ndarray) -> dict[str, Any]:
        """Derive flat statistics dict from the confidence array."""
        high_conf = confidences[confidences >= self.confidence_threshold]

        return {
            # Flat top-level keys — read directly by drift_detector.check_drift()
            "mean":           float(np.mean(confidences)),
            "std":            float(np.std(confidences)),
            "min":            float(np.min(confidences)),
            "max":            float(np.max(confidences)),
            "p25":            float(np.percentile(confidences, 25)),
            "p50":            float(np.percentile(confidences, 50)),
            "p75":            float(np.percentile(confidences, 75)),
            "p90":            float(np.percentile(confidences, 90)),
            "sample_size":    int(len(confidences)),
            # High-confidence subset
            "high_conf_mean":  float(np.mean(high_conf)) if len(high_conf) else 0.0,
            "high_conf_std":   float(np.std(high_conf))  if len(high_conf) else 0.0,
            "high_conf_count": int(len(high_conf)),
            "confidence_threshold": float(self.confidence_threshold),
            # Provenance
            "computed_at":     datetime.now(timezone.utc).isoformat(),
            "data_window_days": _SYMBOL_LOOKBACK_DAYS,
        }

    async def _persist(self, stats: dict[str, Any]) -> None:
        """Write baseline stats to every active production MLModelMetadata record."""
        stmt = select(MLModelMetadata).where(
            MLModelMetadata.status == "production",
            MLModelMetadata.is_active == True,
        )
        result = await self.db.execute(stmt)
        models = list(result.scalars().all())

        if not models:
            logger.warning("No active production models found in ml_model_metadata — nothing written.")
            return

        for model in models:
            model.training_prediction_stats = stats
            logger.info(
                "Stored baseline in %s (%s)  mean=%.4f  std=%.4f  n=%d",
                model.model_id, model.model_name,
                stats["mean"], stats["std"], stats["sample_size"],
            )

        await self.db.commit()
        logger.info("Baseline persisted to %d model record(s).", len(models))


async def _run(sample_size: int, dry_run: bool) -> None:
    async with AsyncSessionLocal() as db:
        await init_redis()
        redis = await get_redis()
        try:
            computer = BaselineComputer(
                db=db,
                redis=redis,
                sample_size=sample_size,
                dry_run=dry_run,
            )
            stats = await computer.run()
            print("\n" + "=" * 70)
            print("BASELINE STATISTICS")
            print("=" * 70)
            for k, v in stats.items():
                if isinstance(v, float):
                    print(f"  {k:<26} {v:.6f}")
                else:
                    print(f"  {k:<26} {v}")
            print("=" * 70)
        finally:
            await close_redis()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Populate drift-detection baseline statistics for all production ML models.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/populate_baseline_statistics.py
  python scripts/populate_baseline_statistics.py --sample-size 2000
  python scripts/populate_baseline_statistics.py --dry-run
        """,
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=1000,
        metavar="N",
        help="Number of unique symbols to run inference on (default: 1000)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print statistics without writing to the database",
    )
    args = parser.parse_args()
    asyncio.run(_run(sample_size=args.sample_size, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
