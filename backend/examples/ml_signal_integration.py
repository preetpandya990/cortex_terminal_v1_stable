"""
Example: ML Integration with Signal Assembler

Demonstrates how to use the ONNX prediction engine with signal fusion.
"""
import asyncio
import numpy as np
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.signal_assembler import SignalAssembler
from app.core.database import get_async_session
from app.core.redis import PubSubClient, CacheService
from app.ml.inference.prediction_engine import PredictionEngine


async def example_ml_signal_fusion():
    """
    Example: Generate trading signal with ML predictions.
    
    This demonstrates the complete flow:
    1. Load ONNX model
    2. Prepare feature vector
    3. Create signal assembler with prediction engine
    4. Assemble signal (includes ML prediction)
    """
    # 1. Initialize ONNX prediction engine
    cache = CacheService()
    prediction_engine = PredictionEngine(
        model_path="/path/to/model.onnx",
        cache=cache,
        num_threads=4,
    )
    
    # 2. Create signal assembler with prediction engine
    signal_assembler = SignalAssembler(
        prediction_engine=prediction_engine,
        event_weight=0.4,
        ml_weight=0.4,
        technical_weight=0.2,
    )
    
    # 3. Prepare feature vector (normally from feature pipeline)
    # Shape: (sequence_length=60, num_features=50)
    feature_vector = np.random.randn(60, 50).astype(np.float32)
    
    # 4. Assemble signal with ML prediction
    async with get_async_session() as db:
        pubsub = PubSubClient()
        
        signal = await signal_assembler.assemble_signal(
            db=db,
            pubsub=pubsub,
            symbol="NSE_EQ|INE002A01018",
            feature_vector=feature_vector,
            timeframe="1d",
        )
        
        print(f"Signal ID: {signal.id}")
        print(f"Action: {signal.action}")
        print(f"Confidence: {signal.confidence_score}")
        print(f"ML Predictions: {signal.ml_predictions}")
        
        return signal


async def example_ml_signals_only():
    """
    Example: Get ML predictions without full signal assembly.
    
    Useful for testing or when you only need ML predictions.
    """
    # Initialize components
    cache = CacheService()
    prediction_engine = PredictionEngine(
        model_path="/path/to/model.onnx",
        cache=cache,
    )
    
    signal_assembler = SignalAssembler(prediction_engine=prediction_engine)
    
    # Prepare features
    feature_vector = np.random.randn(60, 50).astype(np.float32)
    
    # Get ML signals only
    async with get_async_session() as db:
        ml_signals = await signal_assembler.gather_ml_signals(
            db=db,
            symbol="NSE_EQ|INE002A01018",
            feature_vector=feature_vector,
            timeframe="1d",
        )
        
        print(f"ML Score: {ml_signals['score']}")
        print(f"Confidence: {ml_signals['confidence']}")
        print(f"Direction: {ml_signals['prediction']['direction']}")
        print(f"Entry: {ml_signals['prediction']['entry_price']}")
        print(f"Stop Loss: {ml_signals['prediction']['stop_loss']}")
        print(f"Targets: {ml_signals['prediction']['targets']}")
        
        return ml_signals


async def example_batch_predictions():
    """
    Example: Generate signals for multiple symbols.
    
    Demonstrates batch processing with ML predictions.
    """
    cache = CacheService()
    prediction_engine = PredictionEngine(
        model_path="/path/to/model.onnx",
        cache=cache,
    )
    
    signal_assembler = SignalAssembler(prediction_engine=prediction_engine)
    
    symbols = [
        "NSE_EQ|INE002A01018",  # Reliance
        "NSE_EQ|INE009A01021",  # Infosys
        "NSE_EQ|INE040A01034",  # HDFC Bank
    ]
    
    async with get_async_session() as db:
        pubsub = PubSubClient()
        signals = []
        
        for symbol in symbols:
            # Generate features for each symbol (normally from feature pipeline)
            feature_vector = np.random.randn(60, 50).astype(np.float32)
            
            signal = await signal_assembler.assemble_signal(
                db=db,
                pubsub=pubsub,
                symbol=symbol,
                feature_vector=feature_vector,
                timeframe="1d",
            )
            
            signals.append(signal)
            print(f"{symbol}: {signal.action} (confidence: {signal.confidence_score})")
        
        return signals


if __name__ == "__main__":
    # Run examples
    print("=== Example 1: Full Signal Assembly with ML ===")
    asyncio.run(example_ml_signal_fusion())
    
    print("\n=== Example 2: ML Predictions Only ===")
    asyncio.run(example_ml_signals_only())
    
    print("\n=== Example 3: Batch Predictions ===")
    asyncio.run(example_batch_predictions())
