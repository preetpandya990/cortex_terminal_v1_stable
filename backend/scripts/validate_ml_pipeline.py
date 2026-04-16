"""
Cortex AI — ML Pipeline Validation Test
========================================
Tests each module independently with 5 symbols to validate the complete pipeline.

This is a production-grade validation script that:
1. Tests symbol selection
2. Tests feature computation
3. Tests target generation
4. Tests XGBoost training
5. Tests GRU training
6. Tests ensemble creation
7. Validates data flow between modules

Author: Cortex AI Team
Date: 2026-04-13
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np
import pandas as pd

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from app.core.config import get_settings

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)


class PipelineValidator:
    """Validates ML pipeline with 5 symbols"""
    
    def __init__(self, db_session: AsyncSession):
        self.db = db_session
        self.test_symbols = []
        self.features_data = {}
        self.targets_data = {}
        
    async def run_validation(self):
        """Run complete validation"""
        try:
            logger.info("=" * 80)
            logger.info("CORTEX AI — ML PIPELINE VALIDATION")
            logger.info("=" * 80)
            
            # Test 1: Database connectivity
            await self._test_database()
            
            # Test 2: Symbol selection
            await self._test_symbol_selection()
            
            # Test 3: Feature computation
            await self._test_feature_computation()
            
            # Test 4: Target generation
            await self._test_target_generation()
            
            # Test 5: Data alignment
            await self._test_data_alignment()
            
            # Test 6: XGBoost training
            await self._test_xgboost_training()
            
            # Test 7: GRU training
            await self._test_gru_training()
            
            # Test 8: Ensemble creation
            await self._test_ensemble()
            
            logger.info("\n" + "=" * 80)
            logger.info("✓ ALL VALIDATION TESTS PASSED")
            logger.info("=" * 80)
            
            return True
            
        except Exception as e:
            logger.error(f"\n✗ VALIDATION FAILED: {e}", exc_info=True)
            return False
    
    async def _test_database(self):
        """Test 1: Database connectivity"""
        logger.info("\n" + "-" * 80)
        logger.info("TEST 1: DATABASE CONNECTIVITY")
        logger.info("-" * 80)
        
        result = await self.db.execute(text('SELECT COUNT(*) FROM upstox_ohlcv'))
        count = result.scalar()
        
        logger.info(f"✓ Database connected")
        logger.info(f"  OHLCV records: {count:,}")
        
        if count == 0:
            raise ValueError("No OHLCV data in database")
    
    async def _test_symbol_selection(self):
        """Test 2: Symbol selection"""
        logger.info("\n" + "-" * 80)
        logger.info("TEST 2: SYMBOL SELECTION")
        logger.info("-" * 80)
        
        from app.ml.features import get_top_liquid_symbols
        
        # Get top 5 symbols
        self.test_symbols = await get_top_liquid_symbols(
            db=self.db,
            n=5,
            timeframe='1D',
            lookback_days=730
        )
        
        logger.info(f"✓ Symbol selection successful")
        logger.info(f"  Selected symbols: {self.test_symbols}")
        
        if len(self.test_symbols) < 5:
            raise ValueError(f"Expected 5 symbols, got {len(self.test_symbols)}")
    
    async def _test_feature_computation(self):
        """Test 3: Feature computation"""
        logger.info("\n" + "-" * 80)
        logger.info("TEST 3: FEATURE COMPUTATION")
        logger.info("-" * 80)
        
        from app.ml.features import prepare_training_data
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=730)  # 2 years
        
        logger.info(f"Computing features for {len(self.test_symbols)} symbols...")
        logger.info(f"  Date range: {start_date.date()} to {end_date.date()}")
        
        self.features_data = await prepare_training_data(
            symbols=self.test_symbols,
            start_date=start_date,
            end_date=end_date,
            timeframe='1D',
            db=self.db,
            sequence_length=60,
            include_sentiment=False,  # Disable sentiment for validation
            normalize=True
        )
        
        logger.info(f"✓ Feature computation successful")
        logger.info(f"  Symbols with features: {len(self.features_data)}")
        
        for symbol in self.test_symbols:
            if symbol in self.features_data:
                data = self.features_data[symbol]
                logger.info(f"  {symbol}:")
                logger.info(f"    Sequences shape: {data['X'].shape}")
                logger.info(f"    Timestamps: {len(data['timestamps'])}")
                logger.info(f"    Features: {len(data['feature_names'])}")
        
        if len(self.features_data) == 0:
            raise ValueError("No features computed")
    
    async def _test_target_generation(self):
        """Test 4: Target generation"""
        logger.info("\n" + "-" * 80)
        logger.info("TEST 4: TARGET GENERATION")
        logger.info("-" * 80)
        
        from app.ml.features import create_targets_from_db, get_class_weights
        
        logger.info(f"Generating targets for {len(self.test_symbols)} symbols...")
        
        self.targets_data = await create_targets_from_db(
            symbols=self.test_symbols,
            timeframe='1D',
            threshold=0.01,
            horizon=1,
            db=self.db
        )
        
        logger.info(f"✓ Target generation successful")
        logger.info(f"  Symbols with targets: {len(self.targets_data)}")
        
        # Calculate class distribution
        all_targets = []
        for symbol in self.test_symbols:
            if symbol in self.targets_data:
                targets = self.targets_data[symbol]['target']
                all_targets.extend(targets)
                logger.info(f"  {symbol}: {len(targets)} targets")
        
        if len(all_targets) > 0:
            unique, counts = np.unique(all_targets, return_counts=True)
            class_dist = dict(zip(unique, counts))
            
            logger.info(f"\n  Class distribution:")
            logger.info(f"    SELL (-1): {class_dist.get(-1, 0)} ({class_dist.get(-1, 0)/len(all_targets)*100:.1f}%)")
            logger.info(f"    HOLD (0):  {class_dist.get(0, 0)} ({class_dist.get(0, 0)/len(all_targets)*100:.1f}%)")
            logger.info(f"    BUY (1):   {class_dist.get(1, 0)} ({class_dist.get(1, 0)/len(all_targets)*100:.1f}%)")
            
            # Calculate class weights
            class_weights = get_class_weights(np.array(all_targets))
            logger.info(f"  Class weights: {class_weights}")
        
        if len(self.targets_data) == 0:
            raise ValueError("No targets generated")
    
    async def _test_data_alignment(self):
        """Test 5: Data alignment between features and targets"""
        logger.info("\n" + "-" * 80)
        logger.info("TEST 5: DATA ALIGNMENT")
        logger.info("-" * 80)
        
        aligned_count = 0
        
        for symbol in self.test_symbols:
            if symbol in self.features_data and symbol in self.targets_data:
                features = self.features_data[symbol]
                targets = self.targets_data[symbol]
                
                feature_len = len(features['timestamps'])
                target_len = len(targets['target'])
                
                logger.info(f"  {symbol}:")
                logger.info(f"    Features: {feature_len} samples")
                logger.info(f"    Targets:  {target_len} samples")
                
                if feature_len > 0 and target_len > 0:
                    aligned_count += 1
        
        logger.info(f"\n✓ Data alignment check complete")
        logger.info(f"  Aligned symbols: {aligned_count}/{len(self.test_symbols)}")
        
        if aligned_count == 0:
            raise ValueError("No symbols have aligned features and targets")
    
    async def _test_xgboost_training(self):
        """Test 6: XGBoost training"""
        logger.info("\n" + "-" * 80)
        logger.info("TEST 6: XGBOOST TRAINING")
        logger.info("-" * 80)
        
        from app.ml.training import XGBoostTrainer
        
        # Prepare data (use last timestep of sequences as tabular features)
        X_list, y_list = [], []
        
        for symbol in self.test_symbols:
            if symbol in self.features_data and symbol in self.targets_data:
                X_seq = self.features_data[symbol]['X']
                X_tab = X_seq[:, -1, :]  # (n, 47)
                y = self.targets_data[symbol]['target']
                
                # Align lengths
                min_len = min(len(X_tab), len(y))
                if min_len > 0:
                    X_list.append(X_tab[:min_len])
                    y_list.append(y[:min_len])
        
        if len(X_list) == 0:
            raise ValueError("No data available for training")
        
        X_all = np.vstack(X_list)
        y_all = np.concatenate(y_list)
        
        logger.info(f"  Training samples: {len(X_all):,}")
        logger.info(f"  Feature dimension: {X_all.shape[1]}")
        
        # Split train/val (80/20)
        split_idx = int(len(X_all) * 0.8)
        X_train, X_val = X_all[:split_idx], X_all[split_idx:]
        y_train, y_val = y_all[:split_idx], y_all[split_idx:]
        
        logger.info(f"  Train: {len(X_train):,}, Val: {len(X_val):,}")
        
        # Train XGBoost (no tuning for validation)
        logger.info("\n  Training XGBoost model (no hyperparameter tuning)...")
        
        trainer = XGBoostTrainer()
        model = await asyncio.to_thread(
            trainer.train,
            X_train, y_train,
            X_val, y_val,
            params=None,  # Use defaults
            early_stopping_rounds=10,
            verbose=0
        )
        
        # Test prediction
        y_pred, y_proba = trainer.predict(X_val)
        
        accuracy = (y_pred == y_val).mean()
        
        logger.info(f"\n✓ XGBoost training successful")
        logger.info(f"  Validation accuracy: {accuracy:.4f}")
        logger.info(f"  Model type: {type(model)}")
        
        self.xgboost_model = model
        self.xgboost_trainer = trainer
    
    async def _test_gru_training(self):
        """Test 7: GRU training"""
        logger.info("\n" + "-" * 80)
        logger.info("TEST 7: GRU TRAINING")
        logger.info("-" * 80)
        
        from app.ml.training import GRUTrainer
        
        # Prepare sequence data
        X_list, y_list = [], []
        
        for symbol in self.test_symbols:
            if symbol in self.features_data and symbol in self.targets_data:
                X_seq = self.features_data[symbol]['X']  # (n, 60, 47)
                y = self.targets_data[symbol]['target']
                
                # Align lengths
                min_len = min(len(X_seq), len(y))
                if min_len > 0:
                    X_list.append(X_seq[:min_len])
                    y_list.append(y[:min_len])
        
        if len(X_list) == 0:
            raise ValueError("No sequence data available for training")
        
        X_all = np.vstack(X_list)
        y_all = np.concatenate(y_list)
        
        logger.info(f"  Training samples: {len(X_all):,}")
        logger.info(f"  Sequence shape: {X_all.shape}")
        
        # Split train/val (80/20)
        split_idx = int(len(X_all) * 0.8)
        X_train, X_val = X_all[:split_idx], X_all[split_idx:]
        y_train, y_val = y_all[:split_idx], y_all[split_idx:]
        
        logger.info(f"  Train: {len(X_train):,}, Val: {len(X_val):,}")
        
        # Train GRU (no tuning, fewer epochs for validation)
        logger.info("\n  Training GRU model (no hyperparameter tuning, 10 epochs)...")
        
        # Get actual feature count from data
        n_features = X_all.shape[2]
        sequence_length = X_all.shape[1]
        
        trainer = GRUTrainer(input_shape=(sequence_length, n_features))
        model = trainer.build_model(params=None)  # Use defaults
        
        history = await asyncio.to_thread(
            trainer.train,
            X_train, y_train,
            X_val, y_val,
            params=None,
            epochs=10,  # Quick validation
            batch_size=64
        )
        
        # Test prediction
        y_pred_proba = model.predict(X_val, verbose=0)
        y_pred = np.argmax(y_pred_proba, axis=1) - 1  # [0,1,2] -> [-1,0,1]
        
        accuracy = (y_pred == y_val).mean()
        
        logger.info(f"\n✓ GRU training successful")
        logger.info(f"  Validation accuracy: {accuracy:.4f}")
        if hasattr(history, 'history') and hasattr(history.history, '__contains__') and 'val_loss' in history.history:
            logger.info(f"  Final val_loss: {history.history['val_loss'][-1]:.4f}")
        else:
            logger.info("  History object doesn't contain expected validation data")
        logger.info(f"  Model type: {type(model)}")
        
        self.gru_model = model
        self.gru_trainer = trainer
    
    async def _test_ensemble(self):
        """Test 8: Ensemble creation"""
        logger.info("\n" + "-" * 80)
        logger.info("TEST 8: ENSEMBLE CREATION")
        logger.info("-" * 80)
        
        from app.ml.training import create_ensemble
        
        # Create ensemble
        logger.info("  Creating ensemble with weights: XGBoost=0.6, GRU=0.4")
        
        ensemble = create_ensemble(
            self.xgboost_model,
            self.gru_model,
            weights={'xgboost': 0.6, 'gru': 0.4}
        )
        
        # Test prediction
        # Prepare test data
        X_list_tab, X_list_seq, y_list = [], [], []
        
        for symbol in self.test_symbols:
            if symbol in self.features_data and symbol in self.targets_data:
                X_seq = self.features_data[symbol]['X']
                X_tab = X_seq[:, -1, :]
                y = self.targets_data[symbol]['target']
                
                min_len = min(len(X_seq), len(y))
                if min_len > 0:
                    X_list_tab.append(X_tab[:min_len])
                    X_list_seq.append(X_seq[:min_len])
                    y_list.append(y[:min_len])
        
        X_test_tab = np.vstack(X_list_tab)[-100:]  # Last 100 samples
        X_test_seq = np.vstack(X_list_seq)[-100:]
        y_test = np.concatenate(y_list)[-100:]
        
        y_pred, y_proba = ensemble.predict(X_test_tab, X_test_seq)
        
        accuracy = (y_pred == y_test).mean()
        
        logger.info(f"\n✓ Ensemble creation successful")
        logger.info(f"  Test accuracy: {accuracy:.4f}")
        logger.info(f"  Ensemble weights: {ensemble.weights}")
        logger.info(f"  Prediction shape: {y_pred.shape}")
        logger.info(f"  Probability shape: {y_proba.shape}")


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
        validator = PipelineValidator(session)
        success = await validator.run_validation()
        
        if success:
            logger.info("\n✓ Pipeline validation complete. Ready for full training.")
            sys.exit(0)
        else:
            logger.error("\n✗ Pipeline validation failed. Fix issues before training.")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
