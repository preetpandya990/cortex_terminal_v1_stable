"""
Populate Baseline Prediction Statistics for Drift Detection

Generates predictions on recent historical data to establish baseline statistics
for production drift monitoring. Computes mean, std, min, max for both raw and
filtered (high-confidence) predictions.

Usage:
    python scripts/populate_baseline_statistics.py --model-id xgboost_1.0.0_xgboost
    python scripts/populate_baseline_statistics.py --all-production
"""
import asyncio
import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List
import json

import numpy as np
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.database import AsyncSessionLocal
from app.core.redis import init_redis, close_redis, get_redis
from app.models.ml_data import MLModelMetadata
from app.ml.inference.registry_loader import RegistryModelLoader
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.ml.inference.feature_loader import FeatureLoader
from app.models.upstox_data import UpstoxOHLCV

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BaselineStatisticsComputer:
    """Computes baseline prediction statistics for drift detection."""
    
    def __init__(
        self,
        db: AsyncSession,
        redis,
        sample_size: int = 5000,
        lookback_days: int = 60,
        confidence_threshold: float = 0.6,
    ):
        self.db = db
        self.redis = redis
        self.sample_size = sample_size
        self.lookback_days = lookback_days
        self.confidence_threshold = confidence_threshold
        
    async def get_training_symbols(self) -> List[str]:
        """Get all symbols used in training from training results."""
        training_results_path = Path(__file__).parent.parent / "models/production/training_results_20260420_114151.json"
        
        if not training_results_path.exists():
            logger.warning("Training results not found, sampling from database")
            return await self._sample_symbols_from_db()
            
        with open(training_results_path) as f:
            results = json.load(f)
            
        symbols = results.get("symbols", [])
        logger.info(f"Loaded {len(symbols)} training symbols")
        return symbols
    
    async def _sample_symbols_from_db(self) -> List[str]:
        """Fallback: sample symbols from database."""
        stmt = select(UpstoxOHLCV.instrument_key).distinct().limit(1000)
        result = await self.db.execute(stmt)
        symbols = [row[0] for row in result.fetchall()]
        logger.info(f"Sampled {len(symbols)} symbols from database")
        return symbols
    
    async def sample_recent_data(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """Sample recent OHLCV data for baseline computation."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        
        # Get available data points per symbol
        stmt = select(
            UpstoxOHLCV.instrument_key,
            func.count(UpstoxOHLCV.timestamp).label('count')
        ).where(
            UpstoxOHLCV.instrument_key.in_(symbols),
            UpstoxOHLCV.timeframe == '1d',
            UpstoxOHLCV.timestamp >= cutoff
        ).group_by(UpstoxOHLCV.instrument_key).having(
            func.count(UpstoxOHLCV.timestamp) >= 60  # Need 60 days for sequence
        )
        
        result = await self.db.execute(stmt)
        symbol_counts = {row.instrument_key: row.count for row in result.fetchall()}
        
        if not symbol_counts:
            raise ValueError(f"No symbols with sufficient data in last {self.lookback_days} days")
        
        logger.info(f"Found {len(symbol_counts)} symbols with sufficient data")
        
        # Calculate samples per symbol
        total_available = sum(symbol_counts.values())
        samples = []
        
        for symbol, count in symbol_counts.items():
            # Proportional sampling
            n_samples = max(1, int(self.sample_size * count / total_available))
            
            # Get random timestamps for this symbol
            stmt = select(UpstoxOHLCV.timestamp).where(
                UpstoxOHLCV.instrument_key == symbol,
                UpstoxOHLCV.timeframe == '1d',
                UpstoxOHLCV.timestamp >= cutoff
            ).order_by(func.random()).limit(n_samples)
            
            result = await self.db.execute(stmt)
            timestamps = [row[0] for row in result.fetchall()]
            
            for ts in timestamps:
                samples.append({
                    'symbol': symbol,
                    'timestamp': ts,
                    'timeframe': '1d'
                })
                
            if len(samples) >= self.sample_size:
                break
        
        logger.info(f"Sampled {len(samples)} data points across {len(set(s['symbol'] for s in samples))} symbols")
        return samples[:self.sample_size]
    
    async def generate_predictions(
        self,
        samples: List[Dict[str, Any]],
        predictor: EnsemblePredictor,
        model_type: str,
    ) -> np.ndarray:
        """Generate predictions for sampled data."""
        feature_loader = FeatureLoader(
            db=self.db,
            redis=self.redis,
            sequence_length=60,
            n_features=47,
        )
        
        predictions = []
        failed = 0
        
        logger.info(f"Generating {len(samples)} predictions for {model_type}...")
        
        for i, sample in enumerate(samples):
            try:
                # Load features
                tabular, sequence, price, vol = await feature_loader.load_features(
                    symbol=sample['symbol'],
                    timeframe=sample['timeframe'],
                )
                
                # Generate prediction
                if model_type == 'ensemble':
                    result = await predictor.predict(
                        features_tabular=tabular,
                        features_sequence=sequence,
                        symbol=sample['symbol'],
                        current_price=price,
                        volatility=vol,
                        timeframe=sample['timeframe'],
                        use_cache=False,
                    )
                    pred_value = result.get('confidence', 0.5)
                elif model_type == 'xgboost':
                    result = await predictor._predict_xgboost(tabular)
                    pred_value = float(result[0])
                elif model_type == 'gru':
                    result = await predictor._predict_gru(sequence)
                    pred_value = float(result[0])
                else:
                    raise ValueError(f"Unknown model type: {model_type}")
                
                predictions.append(pred_value)
                
                if (i + 1) % 100 == 0:
                    logger.info(f"Progress: {i + 1}/{len(samples)} ({(i+1)/len(samples)*100:.1f}%)")
                    
            except Exception as e:
                failed += 1
                if failed <= 5:  # Log first 5 failures
                    logger.warning(f"Failed to generate prediction for {sample['symbol']}: {e}")
                continue
        
        logger.info(f"Generated {len(predictions)} predictions ({failed} failed)")
        
        if len(predictions) < 100:
            raise ValueError(f"Insufficient predictions generated: {len(predictions)}")
        
        return np.array(predictions)
    
    def compute_statistics(
        self,
        predictions: np.ndarray,
        start_date: datetime,
        end_date: datetime,
    ) -> Dict[str, Any]:
        """Compute baseline statistics from predictions."""
        # Raw predictions
        raw_stats = {
            'mean': float(np.mean(predictions)),
            'std': float(np.std(predictions)),
            'min': float(np.min(predictions)),
            'max': float(np.max(predictions)),
            'sample_size': len(predictions),
        }
        
        # Filtered predictions (high confidence)
        filtered = predictions[predictions >= self.confidence_threshold]
        
        if len(filtered) > 0:
            filtered_stats = {
                'mean': float(np.mean(filtered)),
                'std': float(np.std(filtered)),
                'min': float(np.min(filtered)),
                'max': float(np.max(filtered)),
                'sample_size': len(filtered),
                'confidence_threshold': self.confidence_threshold,
            }
        else:
            filtered_stats = {
                'mean': 0.0,
                'std': 0.0,
                'min': 0.0,
                'max': 0.0,
                'sample_size': 0,
                'confidence_threshold': self.confidence_threshold,
            }
        
        return {
            'raw_predictions': raw_stats,
            'filtered_predictions': filtered_stats,
            'computed_at': datetime.now(timezone.utc).isoformat(),
            'data_period': f"{start_date.date()} to {end_date.date()}",
        }
    
    async def populate_model_baseline(
        self,
        model_id: str,
        model_type: str,
    ) -> Dict[str, Any]:
        """Populate baseline statistics for a single model."""
        logger.info(f"Computing baseline statistics for {model_id} ({model_type})")
        
        # Get training symbols
        symbols = await self.get_training_symbols()
        
        # Sample recent data
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=self.lookback_days)
        samples = await self.sample_recent_data(symbols)
        
        # Load model
        loader = RegistryModelLoader(
            session=self.db,
            num_threads=2,
            use_gpu=False,
        )
        
        if model_type == 'ensemble':
            ensemble = await loader.load_production_ensemble()
            predictor = EnsemblePredictor.from_loaded_ensemble(ensemble)
        else:
            # Load individual model
            ensemble = await loader.load_production_ensemble()
            predictor = EnsemblePredictor.from_loaded_ensemble(ensemble)
        
        # Generate predictions
        predictions = await self.generate_predictions(samples, predictor, model_type)
        
        # Compute statistics
        stats = self.compute_statistics(predictions, start_date, end_date)
        
        logger.info(f"Baseline statistics for {model_id}:")
        logger.info(f"  Raw: mean={stats['raw_predictions']['mean']:.4f}, "
                   f"std={stats['raw_predictions']['std']:.4f}, "
                   f"n={stats['raw_predictions']['sample_size']}")
        logger.info(f"  Filtered: mean={stats['filtered_predictions']['mean']:.4f}, "
                   f"std={stats['filtered_predictions']['std']:.4f}, "
                   f"n={stats['filtered_predictions']['sample_size']}")
        
        return stats
    
    async def update_model_metadata(
        self,
        model_id: str,
        statistics: Dict[str, Any],
    ):
        """Update model metadata with baseline statistics."""
        stmt = select(MLModelMetadata).where(MLModelMetadata.model_id == model_id)
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        
        if not model:
            raise ValueError(f"Model {model_id} not found")
        
        model.training_prediction_stats = statistics
        await self.db.commit()
        
        logger.info(f"Updated baseline statistics for {model_id}")


async def populate_all_production_models():
    """Populate baseline statistics for all production models."""
    async with AsyncSessionLocal() as db:
        await init_redis()
        redis = await get_redis()
        
        try:
            # Get production models
            stmt = select(MLModelMetadata).where(
                MLModelMetadata.status == 'production',
                MLModelMetadata.is_active == True
            )
            result = await db.execute(stmt)
            models = result.scalars().all()
            
            if not models:
                logger.error("No production models found")
                return
            
            logger.info(f"Found {len(models)} production models")
            
            computer = BaselineStatisticsComputer(
                db=db,
                redis=redis,
                sample_size=5000,
                lookback_days=60,
                confidence_threshold=0.6,
            )
            
            # Process each model
            for model in models:
                try:
                    # Determine model type
                    if 'xgboost' in model.model_name.lower():
                        model_type = 'xgboost'
                    elif 'gru' in model.model_name.lower():
                        model_type = 'gru'
                    else:
                        logger.warning(f"Unknown model type for {model.model_id}, skipping")
                        continue
                    
                    # Compute statistics
                    stats = await computer.populate_model_baseline(
                        model_id=model.model_id,
                        model_type=model_type,
                    )
                    
                    # Update database
                    await computer.update_model_metadata(model.model_id, stats)
                    
                except Exception as e:
                    logger.error(f"Failed to process {model.model_id}: {e}", exc_info=True)
                    continue
            
            # Also compute ensemble baseline
            logger.info("Computing ensemble baseline statistics")
            try:
                stats = await computer.populate_model_baseline(
                    model_id='ensemble',
                    model_type='ensemble',
                )
                logger.info(f"Ensemble baseline: {json.dumps(stats, indent=2)}")
            except Exception as e:
                logger.error(f"Failed to compute ensemble baseline: {e}", exc_info=True)
            
            logger.info("Baseline statistics population complete")
            
        finally:
            await close_redis()


async def populate_single_model(model_id: str):
    """Populate baseline statistics for a single model."""
    async with AsyncSessionLocal() as db:
        await init_redis()
        redis = await get_redis()
        
        try:
            # Get model
            stmt = select(MLModelMetadata).where(MLModelMetadata.model_id == model_id)
            result = await db.execute(stmt)
            model = result.scalar_one_or_none()
            
            if not model:
                logger.error(f"Model {model_id} not found")
                return
            
            # Determine model type
            if 'xgboost' in model.model_name.lower():
                model_type = 'xgboost'
            elif 'gru' in model.model_name.lower():
                model_type = 'gru'
            else:
                logger.error(f"Unknown model type for {model_id}")
                return
            
            computer = BaselineStatisticsComputer(
                db=db,
                redis=redis,
                sample_size=5000,
                lookback_days=60,
                confidence_threshold=0.6,
            )
            
            # Compute statistics
            stats = await computer.populate_model_baseline(
                model_id=model.model_id,
                model_type=model_type,
            )
            
            # Update database
            await computer.update_model_metadata(model.model_id, stats)
            
            logger.info("Baseline statistics population complete")
            
        finally:
            await close_redis()


def main():
    parser = argparse.ArgumentParser(description='Populate baseline prediction statistics')
    parser.add_argument('--model-id', help='Specific model ID to process')
    parser.add_argument('--all-production', action='store_true', help='Process all production models')
    
    args = parser.parse_args()
    
    if args.all_production:
        asyncio.run(populate_all_production_models())
    elif args.model_id:
        asyncio.run(populate_single_model(args.model_id))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
