#!/usr/bin/env python3
"""
Load Test Runner for Task 9.9
==============================
Automated runner for production load testing.

Usage:
    python scripts/run_load_test.py [--quick]
    
Options:
    --quick     Run quick test (100 users, 1 minute) for validation
"""
import argparse
import subprocess
import sys
from pathlib import Path


def run_load_test(quick: bool = False):
    """Run load test with appropriate configuration."""
    
    # Configuration
    host = "http://localhost:8000"
    locustfile = Path(__file__).parent / "locustfile.py"
    
    if quick:
        # Quick test for validation
        users = 100
        spawn_rate = 20
        run_time = "1m"
        print("\n🚀 Running QUICK load test (100 users, 1 minute)...\n")
    else:
        # Full production test
        users = 1000
        spawn_rate = 50
        run_time = "5m"
        print("\n🚀 Running FULL load test (1000 users, 5 minutes)...\n")
    
    # Build command
    cmd = [
        "locust",
        "-f", str(locustfile),
        "--host", host,
        "--users", str(users),
        "--spawn-rate", str(spawn_rate),
        "--run-time", run_time,
        "--headless",
        "--only-summary",  # Only show summary, not per-request stats
        "--html", f"load_test_report_{run_time.replace('m', 'min')}.html",
        "--csv", f"load_test_data_{run_time.replace('m', 'min')}",
    ]
    
    print(f"Command: {' '.join(cmd)}\n")
    print("="*80)
    print("LOAD TEST CONFIGURATION")
    print("="*80)
    print(f"Target Host: {host}")
    print(f"Concurrent Users: {users}")
    print(f"Spawn Rate: {spawn_rate} users/second")
    print(f"Duration: {run_time}")
    print("="*80 + "\n")
    
    # Run load test
    try:
        result = subprocess.run(cmd, check=True)
        return result.returncode == 0
    except subprocess.CalledProcessError as e:
        print(f"\n❌ Load test failed with exit code {e.returncode}")
        return False
    except KeyboardInterrupt:
        print("\n\n⚠️  Load test interrupted by user")
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Run Cortex AI load tests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run full production test (1000 users, 5 minutes)
  python scripts/run_load_test.py
  
  # Run quick validation test (100 users, 1 minute)
  python scripts/run_load_test.py --quick
  
  # Run with Web UI (manual control)
  locust -f scripts/locustfile.py --host=http://localhost:8000
  # Then open http://localhost:8089
        """
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Run quick test (100 users, 1 minute)"
    )
    
    args = parser.parse_args()
    
    # Check if API is running
    print("Checking if API server is running...")
    try:
        import httpx
        response = httpx.get("http://localhost:8000/health", timeout=5)
        if response.status_code == 200:
            print("✅ API server is running\n")
        else:
            print(f"⚠️  API server returned status {response.status_code}\n")
    except Exception as e:
        print(f"❌ Cannot connect to API server: {e}")
        print("Please start the API server first: uvicorn app.main:app --reload")
        sys.exit(1)
    
    # Run load test
    success = run_load_test(quick=args.quick)
    
    if success:
        print("\n✅ Load test completed successfully!")
        print("\nReports generated:")
        print("  - HTML report: load_test_report_*.html")
        print("  - CSV data: load_test_data_*.csv")
        print("  - JSON summary: load_test_report_*.json")
        sys.exit(0)
    else:
        print("\n❌ Load test failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
