"""
Production Latency Benchmark for ML Prediction API

Measures p50, p95, p99, p99.9 latencies under realistic load conditions.
Validates that p99 < 250ms target is met.

Usage:
    python scripts/benchmark_latency.py --duration 300 --users 10
    python scripts/benchmark_latency.py --quick  # 60s test with 5 users

Quality Standard: Billion-Dollar App
- Realistic load patterns
- Accurate percentile calculations
- Comprehensive metrics
- Production-like configuration
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import httpx
import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.upstox_data import UpstoxOHLCV


settings = get_settings()


class LatencyBenchmark:
    """Production-grade latency benchmark tool."""
    
    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        duration_seconds: int = 300,
        concurrent_users: int = 10,
        ramp_up_seconds: int = 30,
    ):
        self.base_url = base_url
        self.duration_seconds = duration_seconds
        self.concurrent_users = concurrent_users
        self.ramp_up_seconds = ramp_up_seconds
        
        # Metrics storage
        self.latencies: List[float] = []
        self.status_codes: Dict[int, int] = defaultdict(int)
        self.errors: List[str] = []
        self.start_time: float = 0
        self.end_time: float = 0
        
        # Auth token
        self.auth_token = create_access_token(
            subject="benchmark_user",
            extra_claims={"role": "trader", "user_id": "benchmark"}
        )
        
    async def get_test_symbols(self, count: int = 10) -> List[str]:
        """Get symbols with recent data for testing."""
        engine = create_async_engine(str(settings.DATABASE_URL))
        async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        
        async with async_session() as session:
            stmt = select(UpstoxOHLCV.instrument_key).where(
                UpstoxOHLCV.timeframe == '1d'
            ).distinct().limit(count)
            
            result = await session.execute(stmt)
            symbols = [row[0] for row in result.fetchall()]
        
        await engine.dispose()
        
        if not symbols:
            print("⚠️  No symbols found in database, using fallback")
            symbols = ["NSE_EQ|INE002A01018"]  # Reliance as fallback
        
        return symbols
    
    async def make_prediction_request(
        self,
        client: httpx.AsyncClient,
        symbol: str,
    ) -> tuple[float, int, str | None]:
        """
        Make a single prediction request.
        
        Returns:
            (latency_ms, status_code, error_message)
        """
        start = time.perf_counter()
        error = None
        status_code = 0
        
        try:
            response = await client.post(
                f"{self.base_url}/api/v1/ml/predict",
                json={"symbol": symbol},
                headers={"Authorization": f"Bearer {self.auth_token}"},
                timeout=10.0,
            )
            status_code = response.status_code
            
            if status_code not in [200, 404, 503]:
                error = f"Unexpected status: {status_code}"
                
        except httpx.TimeoutException:
            error = "Timeout"
            status_code = 0
        except Exception as e:
            error = str(e)
            status_code = 0
        
        latency_ms = (time.perf_counter() - start) * 1000
        
        return latency_ms, status_code, error
    
    async def user_session(
        self,
        user_id: int,
        symbols: List[str],
        stop_event: asyncio.Event,
    ):
        """Simulate a single user making requests."""
        async with httpx.AsyncClient() as client:
            request_count = 0
            
            while not stop_event.is_set():
                # Select random symbol
                symbol = symbols[request_count % len(symbols)]
                
                # Make request
                latency, status, error = await self.make_prediction_request(client, symbol)
                
                # Record metrics
                self.latencies.append(latency)
                self.status_codes[status] += 1
                
                if error:
                    self.errors.append(f"User {user_id}: {error}")
                
                request_count += 1
                
                # Small delay between requests (realistic user behavior)
                await asyncio.sleep(0.1)
    
    async def run_benchmark(self):
        """Run the complete benchmark."""
        print("="*80)
        print("ML PREDICTION API LATENCY BENCHMARK")
        print("="*80)
        print(f"Base URL: {self.base_url}")
        print(f"Duration: {self.duration_seconds}s")
        print(f"Concurrent Users: {self.concurrent_users}")
        print(f"Ramp-up: {self.ramp_up_seconds}s")
        print("="*80)
        print()
        
        # Get test symbols
        print("Loading test symbols...")
        symbols = await self.get_test_symbols(count=20)
        print(f"✓ Loaded {len(symbols)} symbols")
        print()
        
        # Create stop event
        stop_event = asyncio.Event()
        
        # Start user sessions with ramp-up
        print(f"Starting {self.concurrent_users} concurrent users...")
        tasks = []
        
        self.start_time = time.time()
        
        for user_id in range(self.concurrent_users):
            task = asyncio.create_task(
                self.user_session(user_id, symbols, stop_event)
            )
            tasks.append(task)
            
            # Ramp-up delay
            if user_id < self.concurrent_users - 1:
                await asyncio.sleep(self.ramp_up_seconds / self.concurrent_users)
        
        print(f"✓ All users started")
        print()
        
        # Run for specified duration
        print(f"Running benchmark for {self.duration_seconds}s...")
        print("Progress: ", end="", flush=True)
        
        for i in range(self.duration_seconds):
            await asyncio.sleep(1)
            if (i + 1) % 10 == 0:
                print(f"{i+1}s ", end="", flush=True)
        
        print()
        print()
        
        # Stop all users
        print("Stopping users...")
        stop_event.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        
        self.end_time = time.time()
        
        print("✓ Benchmark complete")
        print()
        
        # Calculate and display results
        self.display_results()
    
    def calculate_percentile(self, data: List[float], percentile: float) -> float:
        """Calculate percentile using numpy for accuracy."""
        if not data:
            return 0.0
        return float(np.percentile(data, percentile))
    
    def display_results(self):
        """Display benchmark results."""
        if not self.latencies:
            print("❌ No successful requests recorded")
            return
        
        # Calculate metrics
        total_requests = len(self.latencies)
        duration = self.end_time - self.start_time
        throughput = total_requests / duration
        
        # Latency percentiles
        p50 = self.calculate_percentile(self.latencies, 50)
        p95 = self.calculate_percentile(self.latencies, 95)
        p99 = self.calculate_percentile(self.latencies, 99)
        p99_9 = self.calculate_percentile(self.latencies, 99.9)
        mean = statistics.mean(self.latencies)
        median = statistics.median(self.latencies)
        stdev = statistics.stdev(self.latencies) if len(self.latencies) > 1 else 0
        min_latency = min(self.latencies)
        max_latency = max(self.latencies)
        
        # Success rate
        success_count = self.status_codes.get(200, 0)
        success_rate = (success_count / total_requests * 100) if total_requests > 0 else 0
        
        # Display results
        print("="*80)
        print("BENCHMARK RESULTS")
        print("="*80)
        print()
        
        print("📊 THROUGHPUT")
        print(f"  Total Requests:     {total_requests:,}")
        print(f"  Duration:           {duration:.1f}s")
        print(f"  Requests/Second:    {throughput:.2f}")
        print()
        
        print("⏱️  LATENCY (milliseconds)")
        print(f"  Mean:               {mean:.2f}ms")
        print(f"  Median (p50):       {median:.2f}ms")
        print(f"  p95:                {p95:.2f}ms")
        print(f"  p99:                {p99:.2f}ms")
        print(f"  p99.9:              {p99_9:.2f}ms")
        print(f"  Min:                {min_latency:.2f}ms")
        print(f"  Max:                {max_latency:.2f}ms")
        print(f"  Std Dev:            {stdev:.2f}ms")
        print()
        
        print("✅ SUCCESS RATE")
        print(f"  Successful (200):   {success_count:,} ({success_rate:.1f}%)")
        print(f"  Not Found (404):    {self.status_codes.get(404, 0):,}")
        print(f"  Unavailable (503):  {self.status_codes.get(503, 0):,}")
        print(f"  Errors:             {len(self.errors):,}")
        print()
        
        # Validation against targets
        print("🎯 TARGET VALIDATION")
        print(f"  p99 < 250ms:        {'✅ PASS' if p99 < 250 else '❌ FAIL'} ({p99:.2f}ms)")
        print(f"  p95 < 150ms:        {'✅ PASS' if p95 < 150 else '❌ FAIL'} ({p95:.2f}ms)")
        print(f"  p50 < 50ms:         {'✅ PASS' if p50 < 50 else '⚠️  WARN'} ({p50:.2f}ms)")
        print(f"  Success Rate > 95%: {'✅ PASS' if success_rate > 95 else '❌ FAIL'} ({success_rate:.1f}%)")
        print()
        
        # Overall verdict
        p99_pass = p99 < 250
        p95_pass = p95 < 150
        success_pass = success_rate > 95
        
        if p99_pass and p95_pass and success_pass:
            print("🎉 VERDICT: ✅ PRODUCTION READY")
            print("   All performance targets met!")
        elif p99_pass and success_pass:
            print("⚠️  VERDICT: ⚠️  ACCEPTABLE")
            print("   p99 target met, but p95 needs improvement")
        else:
            print("❌ VERDICT: ❌ NEEDS OPTIMIZATION")
            print("   Performance targets not met")
        
        print("="*80)
        
        # Show sample errors if any
        if self.errors:
            print()
            print("SAMPLE ERRORS (first 5):")
            for error in self.errors[:5]:
                print(f"  - {error}")
            if len(self.errors) > 5:
                print(f"  ... and {len(self.errors) - 5} more")
        
        # Save results to file
        self.save_results({
            "timestamp": datetime.now().isoformat(),
            "config": {
                "base_url": self.base_url,
                "duration_seconds": self.duration_seconds,
                "concurrent_users": self.concurrent_users,
            },
            "metrics": {
                "total_requests": total_requests,
                "duration_seconds": duration,
                "throughput_rps": throughput,
                "latency_ms": {
                    "mean": mean,
                    "median": median,
                    "p50": p50,
                    "p95": p95,
                    "p99": p99,
                    "p99_9": p99_9,
                    "min": min_latency,
                    "max": max_latency,
                    "stdev": stdev,
                },
                "success_rate_percent": success_rate,
                "status_codes": dict(self.status_codes),
                "error_count": len(self.errors),
            },
            "validation": {
                "p99_under_250ms": p99_pass,
                "p95_under_150ms": p95_pass,
                "success_rate_over_95": success_pass,
                "production_ready": p99_pass and p95_pass and success_pass,
            }
        })
    
    def save_results(self, results: Dict[str, Any]):
        """Save results to JSON file."""
        output_dir = Path(__file__).parent.parent / "benchmark_results"
        output_dir.mkdir(exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = output_dir / f"latency_benchmark_{timestamp}.json"
        
        with open(output_file, 'w') as f:
            json.dump(results, f, indent=2)
        
        print()
        print(f"📁 Results saved to: {output_file}")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="ML API Latency Benchmark")
    parser.add_argument(
        "--url",
        default="http://localhost:8000",
        help="Base URL of the API (default: http://localhost:8000)"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=300,
        help="Benchmark duration in seconds (default: 300)"
    )
    parser.add_argument(
        "--users",
        type=int,
        default=10,
        help="Number of concurrent users (default: 10)"
    )
    parser.add_argument(
        "--ramp-up",
        type=int,
        default=30,
        help="Ramp-up time in seconds (default: 30)"
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Quick test: 60s with 5 users"
    )
    
    args = parser.parse_args()
    
    # Quick test mode
    if args.quick:
        args.duration = 60
        args.users = 5
        args.ramp_up = 10
    
    # Create and run benchmark
    benchmark = LatencyBenchmark(
        base_url=args.url,
        duration_seconds=args.duration,
        concurrent_users=args.users,
        ramp_up_seconds=args.ramp_up,
    )
    
    try:
        await benchmark.run_benchmark()
    except KeyboardInterrupt:
        print("\n\n⚠️  Benchmark interrupted by user")
        benchmark.end_time = time.time()
        if benchmark.latencies:
            benchmark.display_results()
    except Exception as e:
        print(f"\n\n❌ Benchmark failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
