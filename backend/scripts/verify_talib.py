"""
TA-Lib Installation Verification Script
========================================
Validates TA-Lib installation, performance, and pattern detection.
"""
import time
import numpy as np
import talib

def verify_installation():
    """Verify TA-Lib is properly installed."""
    print("=" * 60)
    print("TA-Lib Installation Verification")
    print("=" * 60)
    
    # 1. Version check
    print(f"\n✓ TA-Lib version: {talib.__version__}")
    
    # 2. Function count
    functions = talib.get_functions()
    print(f"✓ Available functions: {len(functions)}")
    
    # 3. Pattern functions
    pattern_functions = [f for f in functions if f.startswith('CDL')]
    print(f"✓ Candlestick patterns: {len(pattern_functions)}")
    
    return functions, pattern_functions


def smoke_test():
    """Run smoke test with known data."""
    print("\n" + "=" * 60)
    print("Smoke Test - SMA Calculation")
    print("=" * 60)
    
    # Simple deterministic data
    close = np.arange(1, 31, dtype=float)
    
    # 5-period SMA
    sma = talib.SMA(close, timeperiod=5)
    
    print(f"\nInput (last 10): {close[-10:]}")
    print(f"SMA-5 (last 10): {sma[-10:]}")
    
    # Validate (SMA of [21,22,23,24,25] = 23, etc.)
    expected = np.array([19., 20., 21., 22., 23., 24., 25., 26., 27., 28.])
    actual = sma[-10:]
    
    if np.allclose(actual, expected):
        print("✓ SMA calculation correct")
        return True
    else:
        print("✗ SMA calculation incorrect")
        print(f"Expected: {expected}")
        print(f"Actual: {actual}")
        return False


def performance_benchmark():
    """Benchmark pattern detection performance."""
    print("\n" + "=" * 60)
    print("Performance Benchmark")
    print("=" * 60)
    
    # Generate realistic OHLCV data (235 candles, like POC)
    np.random.seed(42)
    n_candles = 235
    
    base_price = 2500.0
    close = base_price + np.cumsum(np.random.randn(n_candles) * 10)
    open_prices = close + np.random.randn(n_candles) * 5
    high = np.maximum(open_prices, close) + np.abs(np.random.randn(n_candles) * 3)
    low = np.minimum(open_prices, close) - np.abs(np.random.randn(n_candles) * 3)
    
    # Test 10 common patterns
    patterns = [
        'CDLDOJI', 'CDLHAMMER', 'CDLENGULFING', 'CDLMORNINGSTAR',
        'CDLEVENINGSTAR', 'CDLSHOOTINGSTAR', 'CDLHARAMI', 'CDLMARUBOZU',
        'CDLSPINNINGTOP', 'CDLDRAGONFLYDOJI'
    ]
    
    start = time.perf_counter()
    
    results = {}
    for pattern_name in patterns:
        pattern_func = getattr(talib, pattern_name)
        result = pattern_func(open_prices, high, low, close)
        detected = np.count_nonzero(result)
        results[pattern_name] = detected
    
    elapsed = (time.perf_counter() - start) * 1000  # ms
    
    print(f"\nCandles analyzed: {n_candles}")
    print(f"Patterns tested: {len(patterns)}")
    print(f"Total time: {elapsed:.2f}ms")
    print(f"Throughput: {(n_candles * len(patterns) / elapsed * 1000):.0f} candles/sec")
    
    print(f"\nDetected patterns:")
    for pattern, count in results.items():
        if count > 0:
            print(f"  {pattern.replace('CDL', '')}: {count}")
    
    return elapsed < 100  # Should be < 100ms


def test_all_patterns():
    """Test all 60+ candlestick patterns."""
    print("\n" + "=" * 60)
    print("All Patterns Test")
    print("=" * 60)
    
    # Generate test data
    np.random.seed(42)
    n_candles = 100
    
    base_price = 2500.0
    close = base_price + np.cumsum(np.random.randn(n_candles) * 10)
    open_prices = close + np.random.randn(n_candles) * 5
    high = np.maximum(open_prices, close) + np.abs(np.random.randn(n_candles) * 3)
    low = np.minimum(open_prices, close) - np.abs(np.random.randn(n_candles) * 3)
    
    # Get all pattern functions
    all_functions = talib.get_functions()
    pattern_functions = [f for f in all_functions if f.startswith('CDL')]
    
    print(f"\nTesting {len(pattern_functions)} patterns...")
    
    working = 0
    failed = []
    
    for pattern_name in pattern_functions:
        try:
            pattern_func = getattr(talib, pattern_name)
            result = pattern_func(open_prices, high, low, close)
            working += 1
        except Exception as e:
            failed.append((pattern_name, str(e)))
    
    print(f"✓ Working patterns: {working}/{len(pattern_functions)}")
    
    if failed:
        print(f"✗ Failed patterns: {len(failed)}")
        for name, error in failed[:5]:  # Show first 5
            print(f"  {name}: {error}")
    
    return len(failed) == 0


def main():
    """Run all verification tests."""
    print("\n" + "=" * 60)
    print("TA-Lib Production Verification")
    print("=" * 60 + "\n")
    
    try:
        # 1. Installation check
        functions, patterns = verify_installation()
        
        # 2. Smoke test
        smoke_ok = smoke_test()
        
        # 3. Performance benchmark
        perf_ok = performance_benchmark()
        
        # 4. All patterns test
        all_ok = test_all_patterns()
        
        # Summary
        print("\n" + "=" * 60)
        print("Verification Summary")
        print("=" * 60)
        
        print(f"\n✓ Installation: PASS")
        print(f"{'✓' if smoke_ok else '✗'} Smoke test: {'PASS' if smoke_ok else 'FAIL'}")
        print(f"{'✓' if perf_ok else '✗'} Performance: {'PASS' if perf_ok else 'FAIL'}")
        print(f"{'✓' if all_ok else '✗'} All patterns: {'PASS' if all_ok else 'FAIL'}")
        
        if smoke_ok and perf_ok and all_ok:
            print("\n🎉 TA-Lib is production-ready!")
            return 0
        else:
            print("\n⚠️  Some tests failed. Review output above.")
            return 1
    
    except Exception as e:
        print(f"\n✗ Verification failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit(main())
