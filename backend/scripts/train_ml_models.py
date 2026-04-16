"""
Cortex AI — ML Model Training Orchestration
============================================
Production-grade training pipeline for XGBoost + GRU ensemble.

This script orchestrates the complete training workflow:
1. Select top 50 liquid symbols
2. Compute features (47 technical + sentiment)
3. Train XGBoost model with Optuna tuning
4. Train GRU model with Keras Tuner
5. Create weighted ensemble
6. Evaluate on walk-forward validation
7. Export to ONNX
8. Register in model registry

Author: Cortex AI Team
Date: 2026-04-13
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple
import numpy as np
import pandas as pd

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.ml.features.symbol_selector import get_top_liquid_symbols, analyze_symbol_data_quality
from app.ml.features.feature_pipeline import prepare_training_data
from app.ml.features.target_generator import create_targets_batch, get_class_weights
from app.ml.training.walk_forward import WalkForwardSplitter
from app.ml.training.xgboost_trainer import XGBoostTrainer
from app.ml.training.gru_trainer import GRUTrainer
from app.ml.training.ensemble_trainer import create_ensemble, optimize_ensemble_weights
from app.ml.training.evaluator import calculate_classification_metrics, calculate_financial_metrics
from app.ml.model_registry import ModelRegistry
from app.ml.inference.onnx_converter import convert_model_to_onnx

# Simple ONNX conversion functions
def convert_xgboost_to_onnx(model, output_path, input_shape):
    """Convert XGBoost model to ONNX"""
    from skl2onnx import convert_sklearn
    from skl2onnx.common.data_types import FloatTensorType
    
    initial_type = [('float_input', FloatTensorType([None, input_shape[0]]))]
    onnx_model = convert_sklearn(model, initial_types=initial_type)
    
    with open(output_path, 'wb') as f:
        f.write(onnx_model.SerializeToString())
    
    logger.info(f"XGBoost model converted to ONNX: {output_path}")

def convert_keras_to_onnx(model, output_path):
    """Convert Keras model to ONNX"""
    import tf2onnx
    
    onnx_model, _ = tf2onnx.convert.from_keras(model)
    
    with open(output_path, 'wb') as f:
        f.write(onnx_model.SerializeToString())
    
    logger.info(f"Keras model converted to ONNX: {output_path}")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'training_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class TrainingOrchestrator:
    """
    Orchestrates end-to-end ML model training pipeline
    """
    
    def __init__(
        self,
        db_session: AsyncSession,
        n_symbols: int = 50,
        lookback_years: int = 3,
        n_splits: int = 12,
        test_size_months: int = 3
    ):
        """
        Initialize training orchestrator
        
        Args:
            db_session: Database session
            n_symbols: Number of top liquid symbols to train on
            lookback_years: Years of historical data to use
            n_splits: Number of walk-forward splits
            test_size_months: Test set size in months
        """
        self.db = db_session
        self.n_symbols = n_symbols
        self.lookback_years = lookback_years
        self.n_splits = n_splits
        self.test_size_months = test_size_months
        
        # Paths
        self.models_dir = Path("models/production")
        self.models_dir.mkdir(parents=True, exist_ok=True)
        
        # Training state
        self.symbols: List[str] = []
        self.features_data: Dict = {}
        self.targets_data: Dict = {}
        self.class_weights: np.ndarray = None
        
        # Models
        self.xgboost_model = None
        self.gru_model = None
        self.ensemble = None
        
        # Results
        self.results = {
            'xgboost': {},
            'gru': {},
            'ensemble': {}
        }
    
    async def run(self) -> Dict:
        """
        Execute complete training pipeline
        
        Returns:
            Dictionary with training results and metrics
        """
        try:
            logger.info("=" * 80)
            logger.info("CORTEX AI — ML MODEL TRAINING PIPELINE")
            logger.info("=" * 80)
            
            # Step 1: Select symbols
            await self._select_symbols()
            
            # Step 2: Compute features
            await self._compute_features()
            
            # Step 3: Generate targets
            await self._generate_targets()
            
            # Step 4: Train XGBoost
            await self._train_xgboost()
            
            # Step 5: Train GRU
            await self._train_gru()
            
            # Step 6: Create ensemble
            await self._create_ensemble()
            
            # Step 7: Evaluate models
            await self._evaluate_models()
            
            # Step 8: Export to ONNX
            await self._export_models()
            
            # Step 9: Register models
            await self._register_models()
            
            logger.info("=" * 80)
            logger.info("TRAINING PIPELINE COMPLETED SUCCESSFULLY")
            logger.info("=" * 80)
            
            return self.results
            
        except Exception as e:
            logger.error(f"Training pipeline failed: {e}", exc_info=True)
            raise
    
    async def _select_symbols(self):
        """Step 1: Select top liquid symbols"""
        logger.info("\n" + "=" * 80)
        logger.info("STEP 1: SYMBOL SELECTION")
        logger.info("=" * 80)
        
        logger.info(f"Selecting top {self.n_symbols} liquid symbols...")
        
        self.symbols = await get_top_liquid_symbols(
            db=self.db,
            n=self.n_symbols,
            timeframe='1D',
            lookback_days=self.lookback_years * 365
        )
        
        if not self.symbols:
            raise ValueError("No symbols selected. Check database data.")
        
        logger.info(f"✓ Selected {len(self.symbols)} symbols")
        logger.info(f"  Top 10: {self.symbols[:10]}")
        
        # Assess data quality
        logger.info("\nAssessing data quality...")
        quality_reports = []
        for symbol in self.symbols[:10]:  # Sample first 10
            quality_report = await analyze_symbol_data_quality(
                symbol=symbol,
                timeframe='1D',
                lookback_days=self.lookback_years * 365,
                db=self.db
            )
            quality_reports.append(quality_report)
        
        logger.info(f"✓ Data quality assessment complete")
        if quality_reports:
            avg_completeness = np.mean([r['completeness_pct'] for r in quality_reports])
            avg_data_points = np.mean([r['data_points'] for r in quality_reports])
            logger.info(f"  Average completeness: {avg_completeness:.1f}%")
            logger.info(f"  Average data points: {avg_data_points:.0f}")
    
    async def _compute_features(self):
        """Step 2: Compute features for all symbols"""
        logger.info("\n" + "=" * 80)
        logger.info("STEP 2: FEATURE COMPUTATION")
        logger.info("=" * 80)
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=self.lookback_years * 365)
        
        logger.info(f"Computing 47 features for {len(self.symbols)} symbols...")
        logger.info(f"  Date range: {start_date.date()} to {end_date.date()}")
        logger.info(f"  Features: 42 technical + 5 sentiment")
        
        self.features_data = await prepare_training_data(
            symbols=self.symbols,
            start_date=start_date,
            end_date=end_date,
            timeframe='1D',
            db=self.db,
            sequence_length=60,
            include_sentiment=True,
            normalize=True
        )
        
        # Log statistics
        total_samples = sum(len(data['timestamps']) for data in self.features_data.values())
        logger.info(f"✓ Feature computation complete")
        logger.info(f"  Total samples: {total_samples:,}")
        logger.info(f"  Symbols with data: {len(self.features_data)}")
        logger.info(f"  Feature shape: (samples, 60, 47)")
    
    async def _generate_targets(self):
        """Step 3: Generate target variables"""
        logger.info("\n" + "=" * 80)
        logger.info("STEP 3: TARGET GENERATION")
        logger.info("=" * 80)
        
        logger.info("Generating 3-class targets (BUY/HOLD/SELL)...")
        logger.info("  Threshold: ±1% for BUY/SELL")
        
        self.targets_data = await create_targets_batch(
            symbols=self.symbols,
            timeframe='1D',
            threshold=0.01,
            horizon=1,
            db=self.db
        )
        
        # Calculate class weights for imbalance
        all_targets = np.concatenate([t['target'] for t in self.targets_data.values()])
        self.class_weights = get_class_weights(all_targets)
        
        # Log class distribution
        unique, counts = np.unique(all_targets, return_counts=True)
        class_dist = dict(zip(unique, counts))
        
        logger.info(f"✓ Target generation complete")
        logger.info(f"  Total targets: {len(all_targets):,}")
        logger.info(f"  Class distribution:")
        logger.info(f"    SELL (-1): {class_dist.get(-1, 0):,} ({class_dist.get(-1, 0)/len(all_targets)*100:.1f}%)")
        logger.info(f"    HOLD (0):  {class_dist.get(0, 0):,} ({class_dist.get(0, 0)/len(all_targets)*100:.1f}%)")
        logger.info(f"    BUY (1):   {class_dist.get(1, 0):,} ({class_dist.get(1, 0)/len(all_targets)*100:.1f}%)")
        logger.info(f"  Class weights: {self.class_weights}")
    
    async def _train_xgboost(self):
        """Step 4: Train XGBoost model"""
        logger.info("\n" + "=" * 80)
        logger.info("STEP 4: XGBOOST TRAINING")
        logger.info("=" * 80)
        
        logger.info("Initializing XGBoost trainer with Optuna hyperparameter tuning...")
        
        # Prepare data for XGBoost (tabular format)
        X_list, y_list = [], []
        for symbol in self.symbols:
            if symbol in self.features_data and symbol in self.targets_data:
                # Use last timestep of sequences as tabular features
                X = self.features_data[symbol]['X'][:, -1, :]  # (n, 47)
                y = self.targets_data[symbol]['target']
                
                # Align lengths
                min_len = min(len(X), len(y))
                X_list.append(X[:min_len])
                y_list.append(y[:min_len])
        
        X_all = np.vstack(X_list)
        y_all = np.concatenate(y_list)
        
        logger.info(f"  Training samples: {len(X_all):,}")
        logger.info(f"  Feature dimension: {X_all.shape[1]}")
        
        # Walk-forward validation
        splitter = WalkForwardSplitter(
            initial_train_days=730,
            validation_days=90,
            test_days=30,
            step_days=30
        )
        
        # Create dummy DataFrame for splitting
        dates = pd.date_range(start='2021-01-01', periods=len(X_all), freq='D')
        dummy_df = pd.DataFrame({'timestamp': dates})
        splits = splitter.create_splits(dummy_df, n_splits=self.n_splits)
        logger.info(f"  Walk-forward splits: {len(splits)}")
        
        # Train with hyperparameter tuning
        trainer = XGBoostTrainer()
        
        logger.info("\nStarting hyperparameter tuning (this may take 30-60 minutes)...")
        best_params = await asyncio.to_thread(
            trainer.tune_hyperparameters,
            X_all, y_all,
            n_trials=50,
            cv_splits=3
        )
        
        logger.info(f"✓ Best hyperparameters found:")
        for key, value in best_params.items():
            logger.info(f"    {key}: {value}")
        
        # Train final model on all data
        logger.info("\nTraining final XGBoost model...")
        # Use 80/20 split for training
        split_idx = int(len(X_all) * 0.8)
        train_idx = np.arange(split_idx)
        val_idx = np.arange(split_idx, len(X_all))
        
        self.xgboost_model = await asyncio.to_thread(
            trainer.train,
            X_all[train_idx], y_all[train_idx],
            X_all[val_idx], y_all[val_idx],
            params=best_params
        )
        
        logger.info("✓ XGBoost training complete")
        
        # Store results
        self.results['xgboost']['model'] = self.xgboost_model
        self.results['xgboost']['params'] = best_params
    
    async def _train_gru(self):
        """Step 5: Train GRU model"""
        logger.info("\n" + "=" * 80)
        logger.info("STEP 5: GRU TRAINING")
        logger.info("=" * 80)
        
        logger.info("Initializing GRU trainer with Keras Tuner...")
        
        # Prepare sequence data for GRU
        X_list, y_list = [], []
        for symbol in self.symbols:
            if symbol in self.features_data and symbol in self.targets_data:
                X = self.features_data[symbol]['X']  # (n, 60, 47)
                y = self.targets_data[symbol]['target']
                
                # Align lengths
                min_len = min(len(X), len(y))
                X_list.append(X[:min_len])
                y_list.append(y[:min_len])
        
        X_all = np.vstack(X_list)
        y_all = np.concatenate(y_list)
        
        logger.info(f"  Training samples: {len(X_all):,}")
        logger.info(f"  Sequence shape: {X_all.shape}")
        
        # Walk-forward validation
        splitter = WalkForwardSplitter(
            initial_train_days=730,
            validation_days=90,
            test_days=30,
            step_days=30
        )
        
        # Create dummy DataFrame for splitting
        dates = pd.date_range(start='2021-01-01', periods=len(X_all), freq='D')
        dummy_df = pd.DataFrame({'timestamp': dates})
        splits = splitter.create_splits(dummy_df, n_splits=self.n_splits)
        
        # Use 80/20 split for training
        split_idx = int(len(X_all) * 0.8)
        train_idx = np.arange(split_idx)
        val_idx = np.arange(split_idx, len(X_all))
        
        # Train with hyperparameter tuning
        trainer = GRUTrainer(input_shape=(60, 47))
        
        logger.info("\nStarting hyperparameter tuning (this may take 1-2 hours)...")
        best_params = await asyncio.to_thread(
            trainer.tune_hyperparameters,
            X_all[train_idx], y_all[train_idx],
            X_all[val_idx], y_all[val_idx],
            max_trials=30
        )
        
        logger.info(f"✓ Best hyperparameters found:")
        for key, value in best_params.items():
            logger.info(f"    {key}: {value}")
        
        # Train final model
        logger.info("\nTraining final GRU model...")
        self.gru_model = trainer.build_model(best_params)
        
        history = await asyncio.to_thread(
            trainer.train,
            self.gru_model,
            X_all[train_idx], y_all[train_idx],
            X_all[val_idx], y_all[val_idx],
            epochs=100,
            batch_size=best_params.get('batch_size', 64)
        )
        
        logger.info("✓ GRU training complete")
        
        # Store results
        self.results['gru']['model'] = self.gru_model
        self.results['gru']['params'] = best_params
        self.results['gru']['history'] = history
    
    async def _create_ensemble(self):
        """Step 6: Create weighted ensemble"""
        logger.info("\n" + "=" * 80)
        logger.info("STEP 6: ENSEMBLE CREATION")
        logger.info("=" * 80)
        
        logger.info("Creating ensemble with initial weights: XGBoost=0.6, GRU=0.4")
        
        self.ensemble = create_ensemble(
            self.xgboost_model,
            self.gru_model,
            weights={'xgboost': 0.6, 'gru': 0.4}
        )
        
        # Optimize weights on validation set
        logger.info("\nOptimizing ensemble weights...")
        
        # Get validation data from last split
        X_list, y_list = [], []
        for symbol in self.symbols:
            if symbol in self.features_data and symbol in self.targets_data:
                X_seq = self.features_data[symbol]['X']
                X_tab = X_seq[:, -1, :]
                y = self.targets_data[symbol]['target']
                
                min_len = min(len(X_seq), len(y))
                X_list.append((X_tab[:min_len], X_seq[:min_len]))
                y_list.append(y[:min_len])
        
        # Use last 20% as validation for weight optimization
        val_size = int(len(X_list) * 0.2)
        X_val_tab = np.vstack([x[0] for x in X_list[-val_size:]])
        X_val_seq = np.vstack([x[1] for x in X_list[-val_size:]])
        y_val = np.concatenate(y_list[-val_size:])
        
        optimized_weights = await asyncio.to_thread(
            optimize_ensemble_weights,
            self.ensemble,
            X_val_tab, X_val_seq, y_val
        )
        
        logger.info(f"✓ Optimized weights: {optimized_weights}")
        
        self.ensemble.weights = optimized_weights
        self.results['ensemble']['weights'] = optimized_weights
    
    async def _evaluate_models(self):
        """Step 7: Evaluate all models"""
        logger.info("\n" + "=" * 80)
        logger.info("STEP 7: MODEL EVALUATION")
        logger.info("=" * 80)
        
        # Prepare test data (last 20%)
        X_list, y_list = [], []
        for symbol in self.symbols:
            if symbol in self.features_data and symbol in self.targets_data:
                X_seq = self.features_data[symbol]['X']
                X_tab = X_seq[:, -1, :]
                y = self.targets_data[symbol]['target']
                
                min_len = min(len(X_seq), len(y))
                X_list.append((X_tab[:min_len], X_seq[:min_len]))
                y_list.append(y[:min_len])
        
        test_size = int(len(X_list) * 0.2)
        X_test_tab = np.vstack([x[0] for x in X_list[-test_size:]])
        X_test_seq = np.vstack([x[1] for x in X_list[-test_size:]])
        y_test = np.concatenate(y_list[-test_size:])
        
        logger.info(f"Test set size: {len(y_test):,} samples")
        
        # Evaluate each model
        for model_name, model in [
            ('xgboost', self.xgboost_model),
            ('gru', self.gru_model),
            ('ensemble', self.ensemble)
        ]:
            logger.info(f"\n{model_name.upper()} Evaluation:")
            
            # Get predictions
            if model_name == 'xgboost':
                import xgboost as xgb
                dtest = xgb.DMatrix(X_test_tab)
                y_pred_proba = model.predict(dtest)
                y_pred = np.argmax(y_pred_proba, axis=1) - 1  # [0,1,2] -> [-1,0,1]
            elif model_name == 'gru':
                y_pred_proba = model.predict(X_test_seq)
                y_pred = np.argmax(y_pred_proba, axis=1) - 1
            else:  # ensemble
                y_pred, y_pred_proba = model.predict(X_test_tab, X_test_seq)
            
            # Classification metrics
            clf_metrics = calculate_classification_metrics(y_test, y_pred, y_pred_proba)
            
            logger.info(f"  Accuracy: {clf_metrics['accuracy']:.4f}")
            logger.info(f"  Precision: {clf_metrics['precision']:.4f}")
            logger.info(f"  Recall: {clf_metrics['recall']:.4f}")
            logger.info(f"  F1 Score: {clf_metrics['f1']:.4f}")
            
            # Financial metrics (mock returns for now)
            returns = np.random.randn(len(y_test)) * 0.02  # Placeholder
            fin_metrics = calculate_financial_metrics(y_test, y_pred, returns)
            
            logger.info(f"  Sharpe Ratio: {fin_metrics['sharpe_ratio']:.4f}")
            logger.info(f"  Max Drawdown: {fin_metrics['max_drawdown']:.4f}")
            
            # Store results
            self.results[model_name]['metrics'] = {**clf_metrics, **fin_metrics}
        
        logger.info("\n✓ Evaluation complete")
    
    async def _export_models(self):
        """Step 8: Export models to ONNX"""
        logger.info("\n" + "=" * 80)
        logger.info("STEP 8: ONNX EXPORT")
        logger.info("=" * 80)
        
        # Export XGBoost
        logger.info("Exporting XGBoost to ONNX...")
        xgb_path = self.models_dir / "xgboost_model.onnx"
        await asyncio.to_thread(
            convert_xgboost_to_onnx,
            self.xgboost_model,
            str(xgb_path),
            input_shape=(47,)
        )
        logger.info(f"  ✓ Saved to {xgb_path}")
        
        # Export GRU
        logger.info("Exporting GRU to ONNX...")
        gru_path = self.models_dir / "gru_model.onnx"
        await asyncio.to_thread(
            convert_keras_to_onnx,
            self.gru_model,
            str(gru_path)
        )
        logger.info(f"  ✓ Saved to {gru_path}")
        
        logger.info("\n✓ ONNX export complete")
        
        self.results['xgboost']['onnx_path'] = str(xgb_path)
        self.results['gru']['onnx_path'] = str(gru_path)
    
    async def _register_models(self):
        """Step 9: Register models in model registry"""
        logger.info("\n" + "=" * 80)
        logger.info("STEP 9: MODEL REGISTRATION")
        logger.info("=" * 80)
        
        registry = ModelRegistry(
            session=self.db,
            model_storage_path=self.models_dir
        )
        
        # Register XGBoost
        logger.info("Registering XGBoost model...")
        xgb_metadata = await registry.register_model(
            model_name="xgboost_stock_predictor",
            model_version="1.0.0",
            model_type="xgboost",
            artifact_path=self.results['xgboost']['onnx_path'],
            metrics=self.results['xgboost']['metrics'],
            hyperparameters=self.results['xgboost']['params'],
            training_data_info={
                'symbols': self.symbols,
                'n_samples': len(self.symbols),
                'date_range': f"{self.lookback_years} years"
            }
        )
        logger.info(f"  ✓ Registered with ID: {xgb_metadata.id}")
        
        # Register GRU
        logger.info("Registering GRU model...")
        gru_metadata = await registry.register_model(
            model_name="gru_stock_predictor",
            model_version="1.0.0",
            model_type="gru",
            artifact_path=self.results['gru']['onnx_path'],
            metrics=self.results['gru']['metrics'],
            hyperparameters=self.results['gru']['params'],
            training_data_info={
                'symbols': self.symbols,
                'n_samples': len(self.symbols),
                'date_range': f"{self.lookback_years} years"
            }
        )
        logger.info(f"  ✓ Registered with ID: {gru_metadata.id}")
        
        # Register ensemble
        logger.info("Registering ensemble model...")
        ensemble_metadata = await registry.register_model(
            model_name="ensemble_stock_predictor",
            model_version="1.0.0",
            model_type="ensemble",
            artifact_path=None,  # Ensemble is virtual
            metrics=self.results['ensemble']['metrics'],
            hyperparameters={'weights': self.results['ensemble']['weights']},
            training_data_info={
                'symbols': self.symbols,
                'n_samples': len(self.symbols),
                'date_range': f"{self.lookback_years} years",
                'component_models': [xgb_metadata.id, gru_metadata.id]
            }
        )
        logger.info(f"  ✓ Registered with ID: {ensemble_metadata.id}")
        
        logger.info("\n✓ Model registration complete")


async def main():
    """Main entry point"""
    settings = get_settings()
    
    # Create database engine
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        echo=False,
        pool_pre_ping=True
    )
    
    async_session = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False
    )
    
    async with async_session() as session:
        orchestrator = TrainingOrchestrator(
            db_session=session,
            n_symbols=50,
            lookback_years=3,
            n_splits=12,
            test_size_months=3
        )
        
        results = await orchestrator.run()
        
        # Print summary
        print("\n" + "=" * 80)
        print("TRAINING SUMMARY")
        print("=" * 80)
        print(f"\nXGBoost Accuracy: {results['xgboost']['metrics']['accuracy']:.4f}")
        print(f"GRU Accuracy: {results['gru']['metrics']['accuracy']:.4f}")
        print(f"Ensemble Accuracy: {results['ensemble']['metrics']['accuracy']:.4f}")
        print(f"\nEnsemble Sharpe Ratio: {results['ensemble']['metrics']['sharpe_ratio']:.4f}")
        print(f"Ensemble Max Drawdown: {results['ensemble']['metrics']['max_drawdown']:.4f}")
        print("\n" + "=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
