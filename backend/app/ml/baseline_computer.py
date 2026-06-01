"""
Automated Baseline Statistics Computation

Integrates with model promotion pipeline to automatically compute
baseline prediction statistics when models are promoted to production.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List

import numpy as np
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ml_data import MLModelMetadata
from app.models.upstox_data import UpstoxOHLCV
from app.ml.inference.registry_loader import RegistryModelLoader
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.ml.inference.feature_loader import FeatureLoader

logger = logging.getLogger(__name__)


class BaselineComputer:
    """Computes baseline prediction statistics for drift monitoring."""
    
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
    
    async def compute_and_store(
        self,
        model_id: str,
        model_type: str,
    ) -> Dict[str, Any]:
        """
        Compute baseline statistics and store in model metadata.
        
        Args:
            model_id: Model ID in ml_model_metadata table
            model_type: 'xgboost', 'gru', or 'ensemble'
            
        Returns:
            Computed statistics dictionary
        """
        logger.info(f"Computing baseline statistics for {model_id}")
        
        try:
            # Sample data
            samples = await self._sample_data()
            
            if len(samples) < 100:
                logger.warning(f"Insufficient samples ({len(samples)}), using defaults")
                return self._default_statistics()
            
            # Load model and generate predictions
            predictions = await self._generate_predictions(samples, model_id, model_type)
            
            # Compute statistics
            stats = self._compute_stats(predictions)
            
            # Store in database
            await self._update_metadata(model_id, stats)
            
            logger.info(f"Baseline computed: mean={stats['raw_predictions']['mean']:.4f}, "
                       f"std={stats['raw_predictions']['std']:.4f}, "
                       f"n={stats['raw_predictions']['sample_size']}")
            
            return stats
            
        except Exception as e:
            logger.error(f"Failed to compute baseline for {model_id}: {e}")
            # Return defaults on failure (don't block promotion)
            return self._default_statistics()
    
    async def _sample_data(self) -> List[Dict[str, Any]]:
        """Sample recent OHLCV data."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.lookback_days)
        
        # Get symbols with sufficient data
        stmt = select(
            UpstoxOHLCV.instrument_key,
            func.count(UpstoxOHLCV.timestamp).label('count')
        ).where(
            UpstoxOHLCV.timeframe == '1d',
            UpstoxOHLCV.timestamp >= cutoff
        ).group_by(UpstoxOHLCV.instrument_key).having(
            func.count(UpstoxOHLCV.timestamp) >= 60
        )
        
        result = await self.db.execute(stmt)
        symbol_counts = {row.instrument_key: row.count for row in result.fetchall()}
        
        if not symbol_counts:
            return []
        
        # Sample proportionally
        total = sum(symbol_counts.values())
        samples = []
        
        for symbol, count in list(symbol_counts.items())[:100]:  # Limit to 100 symbols for speed
            n = max(1, int(self.sample_size * count / total))
            
            stmt = select(UpstoxOHLCV.timestamp).where(
                UpstoxOHLCV.instrument_key == symbol,
                UpstoxOHLCV.timeframe == '1d',
                UpstoxOHLCV.timestamp >= cutoff
            ).order_by(func.random()).limit(n)
            
            result = await self.db.execute(stmt)
            
            for row in result.fetchall():
                samples.append({
                    'symbol': symbol,
                    'timestamp': row[0],
                    'timeframe': '1d'
                })
                
            if len(samples) >= self.sample_size:
                break
        
        return samples[:self.sample_size]
    
    async def _generate_predictions(
        self,
        samples: List[Dict[str, Any]],
        model_id: str,
        model_type: str,
    ) -> np.ndarray:
        """Generate predictions on sampled data."""
        loader = RegistryModelLoader(
            session=self.db,
            num_threads=2,
            use_gpu=False,
        )
        
        ensemble = await loader.load_production_ensemble()
        predictor = EnsemblePredictor.from_loaded_ensemble(ensemble)
        
        feature_loader = FeatureLoader(
            db=self.db,
            redis=self.redis,
            sequence_length=60,
            n_features=40,
        )
        
        predictions = []
        
        for sample in samples[:500]:  # Limit to 500 for speed during promotion
            try:
                tabular, sequence, price, vol = await feature_loader.load_features(
                    symbol=sample['symbol'],
                    timeframe=sample['timeframe'],
                )
                
                # Always use the full ensemble predict() — individual model
                # backends are not exposed as public methods.
                result = await predictor.predict(
                    features_tabular=tabular,
                    features_sequence=sequence,
                    symbol=sample['symbol'],
                    current_price=price,
                    volatility=vol,
                    use_cache=False,
                )
                predictions.append(float(result.get('confidence', 0.5)))
                    
            except Exception:
                continue
        
        return np.array(predictions)
    
    def _compute_stats(self, predictions: np.ndarray) -> Dict[str, Any]:
        """Compute statistics from predictions.

        Flat top-level keys (mean, std) are required by the drift detector.
        High-confidence subset stats are included for observability.
        """
        filtered = predictions[predictions >= self.confidence_threshold]

        return {
            # Flat keys — read directly by drift_detector.check_drift()
            'mean':          float(np.mean(predictions)),
            'std':           float(np.std(predictions)),
            'min':           float(np.min(predictions)),
            'max':           float(np.max(predictions)),
            'sample_size':   int(len(predictions)),
            # High-confidence subset
            'high_conf_mean':  float(np.mean(filtered)) if len(filtered) > 0 else 0.0,
            'high_conf_std':   float(np.std(filtered))  if len(filtered) > 0 else 0.0,
            'high_conf_count': int(len(filtered)),
            'confidence_threshold': float(self.confidence_threshold),
            'computed_at':   datetime.now(timezone.utc).isoformat(),
        }
    
    def _default_statistics(self) -> Dict[str, Any]:
        """Return conservative default statistics when computation fails."""
        return {
            'mean':            0.5,
            'std':             0.15,
            'min':             0.0,
            'max':             1.0,
            'sample_size':     0,
            'high_conf_mean':  0.75,
            'high_conf_std':   0.10,
            'high_conf_count': 0,
            'confidence_threshold': float(self.confidence_threshold),
            'computed_at':     datetime.now(timezone.utc).isoformat(),
            'note':            'Default values — computation failed or insufficient data',
        }
    
    async def _update_metadata(self, model_id: str, stats: Dict[str, Any]):
        """Update model metadata with statistics."""
        stmt = select(MLModelMetadata).where(MLModelMetadata.model_id == model_id)
        result = await self.db.execute(stmt)
        model = result.scalar_one_or_none()
        
        if model:
            model.training_prediction_stats = stats
            await self.db.commit()
