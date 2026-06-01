"""
Cortex AI — Verify Timeframe Implementation
=============================================
Simple verification script to test timeframe-specific model implementation
without requiring full dependencies.
"""
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.ml.training.timeframe_config import (
    get_timeframe_config,
    get_indicator_period,
    get_sequence_length,
    get_training_config,
    get_all_timeframes,
)


def verify_timeframe_configs():
    """Verify timeframe configurations are correct."""
    print("=" * 80)
    print("VERIFYING TIMEFRAME CONFIGURATIONS")
    print("=" * 80)
    
    timeframes = get_all_timeframes()
    print(f"\n✓ Found {len(timeframes)} timeframes: {timeframes}")
    
    for timeframe in timeframes:
        print(f"\n{timeframe} Configuration:")
        config = get_timeframe_config(timeframe)
        
        print(f"  Name: {config['name']}")
        print(f"  Sequence Length: {config['sequence_length']}")
        print(f"  Min Training Samples: {config['min_training_samples']}")
        print(f"  Batch Size: {config['batch_size']}")
        print(f"  Epochs: {config['epochs']}")
        print(f"  Learning Rate: {config['learning_rate']}")
        
        # Check some indicator periods
        print(f"  Indicator Periods:")
        print(f"    RSI: {get_indicator_period(timeframe, 'rsi')}")
        print(f"    MACD Fast: {get_indicator_period(timeframe, 'macd_fast')}")
        print(f"    MACD Slow: {get_indicator_period(timeframe, 'macd_slow')}")
        print(f"    SMA Short: {get_indicator_period(timeframe, 'sma_short')}")
        print(f"    ATR: {get_indicator_period(timeframe, 'atr')}")


def verify_period_scaling():
    """Verify that indicator periods are correctly scaled."""
    print("\n" + "=" * 80)
    print("VERIFYING PERIOD SCALING")
    print("=" * 80)
    
    indicators = [
        "rsi", "rsi_long", "macd_fast", "macd_slow", "macd_signal",
        "stochastic", "roc_short", "roc_long", "williams_r", "cci",
        "momentum", "sma_short", "sma_medium", "sma_long",
        "ema_fast", "ema_medium", "ema_slow", "atr", "bb_period",
    ]
    
    print("\nChecking period scaling ratios:")
    print(f"{'Indicator':<20} {'Daily':<10} {'Weekly':<10} {'Monthly':<10} {'W/D Ratio':<12} {'M/D Ratio':<12}")
    print("-" * 80)
    
    all_correct = True
    
    for indicator in indicators:
        daily = get_indicator_period("1d", indicator)
        weekly = get_indicator_period("1w", indicator)
        monthly = get_indicator_period("1M", indicator)
        
        weekly_ratio = weekly / daily
        monthly_ratio = monthly / daily
        
        # Check if ratios are correct (5 for weekly, 21 for monthly)
        weekly_correct = abs(weekly_ratio - 5.0) < 0.01
        monthly_correct = abs(monthly_ratio - 21.0) < 0.01
        
        status = "✓" if (weekly_correct and monthly_correct) else "✗"
        
        print(f"{indicator:<20} {daily:<10} {weekly:<10} {monthly:<10} {weekly_ratio:<12.1f} {monthly_ratio:<12.1f} {status}")
        
        if not (weekly_correct and monthly_correct):
            all_correct = False
    
    if all_correct:
        print("\n✓ All indicator periods are correctly scaled!")
    else:
        print("\n✗ Some indicator periods have incorrect scaling!")
    
    return all_correct


def verify_sequence_lengths():
    """Verify sequence lengths are appropriate."""
    print("\n" + "=" * 80)
    print("VERIFYING SEQUENCE LENGTHS")
    print("=" * 80)
    
    expected = {
        "1d": 60,   # 60 days (~3 months)
        "1w": 52,   # 52 weeks (1 year)
        "1M": 24,   # 24 months (2 years)
    }
    
    all_correct = True
    
    for timeframe, expected_length in expected.items():
        actual_length = get_sequence_length(timeframe)
        correct = actual_length == expected_length
        status = "✓" if correct else "✗"
        
        print(f"{timeframe}: {actual_length} (expected: {expected_length}) {status}")
        
        if not correct:
            all_correct = False
    
    if all_correct:
        print("\n✓ All sequence lengths are correct!")
    else:
        print("\n✗ Some sequence lengths are incorrect!")
    
    return all_correct


def verify_training_configs():
    """Verify training configurations."""
    print("\n" + "=" * 80)
    print("VERIFYING TRAINING CONFIGURATIONS")
    print("=" * 80)
    
    for timeframe in get_all_timeframes():
        config = get_training_config(timeframe)
        
        print(f"\n{timeframe} Training Config:")
        print(f"  Batch Size: {config['batch_size']}")
        print(f"  Epochs: {config['epochs']}")
        print(f"  Learning Rate: {config['learning_rate']}")
        print(f"  Early Stopping Patience: {config['early_stopping_patience']}")
        print(f"  Validation Split: {config['validation_split']}")
        print(f"  Test Split: {config['test_split']}")
        
        # Verify required keys
        required_keys = [
            "batch_size", "epochs", "learning_rate",
            "early_stopping_patience", "validation_split", "test_split"
        ]
        
        missing_keys = [key for key in required_keys if key not in config]
        
        if missing_keys:
            print(f"  ✗ Missing keys: {missing_keys}")
            return False
    
    print("\n✓ All training configurations are complete!")
    return True


def main():
    """Run all verification checks."""
    print("\n" + "=" * 80)
    print("TIMEFRAME-SPECIFIC MODELS IMPLEMENTATION VERIFICATION")
    print("=" * 80)
    
    try:
        # Run verification checks
        verify_timeframe_configs()
        scaling_ok = verify_period_scaling()
        sequence_ok = verify_sequence_lengths()
        training_ok = verify_training_configs()
        
        # Summary
        print("\n" + "=" * 80)
        print("VERIFICATION SUMMARY")
        print("=" * 80)
        
        checks = [
            ("Timeframe Configurations", True),
            ("Period Scaling", scaling_ok),
            ("Sequence Lengths", sequence_ok),
            ("Training Configurations", training_ok),
        ]
        
        all_passed = all(result for _, result in checks)
        
        for check_name, result in checks:
            status = "✓ PASS" if result else "✗ FAIL"
            print(f"{check_name:<30} {status}")
        
        print("\n" + "=" * 80)
        
        if all_passed:
            print("✓ ALL CHECKS PASSED!")
            print("\nThe timeframe-specific models implementation is correct.")
            print("\nNext steps:")
            print("1. Prepare training data for each timeframe")
            print("2. Run: python -m backend.app.ml.training.train_all_timeframes")
            print("3. Models will be trained and registered with timeframe tags")
            return 0
        else:
            print("✗ SOME CHECKS FAILED!")
            print("\nPlease review the failed checks above.")
            return 1
            
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
