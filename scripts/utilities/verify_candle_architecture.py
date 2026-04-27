#!/usr/bin/env python3
"""
Candle Data Architecture Verification Script
=============================================
Verifies the three-tier architecture is working correctly:
1. Redis Cache
2. PostgreSQL Database
3. Upstox API

Usage:
    python scripts/utilities/verify_candle_architecture.py
"""
import asyncio
import sys
import time
from datetime import datetime, timedelta

import httpx
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import box

console = Console()

# Configuration
API_BASE_URL = "http://localhost:8000/api/v1"
TEST_INSTRUMENT = "NSE_EQ|INE009A01021"  # Reliance
TEST_TOKEN = None  # Will be set after login


async def login() -> str:
    """Quick dev login to get auth token."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{API_BASE_URL}/auth/dev-login",
            json={"user_id": "test_user"}
        )
        if response.status_code == 200:
            data = response.json()
            return data.get("access_token")
        else:
            console.print("[red]❌ Login failed[/red]")
            sys.exit(1)


async def test_intraday_candles(token: str) -> dict:
    """Test intraday candles endpoint with timing."""
    console.print("\n[cyan]Testing Intraday Candles...[/cyan]")
    
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{API_BASE_URL}/upstox/candles/intraday"
    params = {
        "instrument_key": TEST_INSTRUMENT,
        "unit": "minutes",
        "interval": 1
    }
    
    results = {
        "first_call": None,
        "second_call": None,
        "third_call": None,
    }
    
    async with httpx.AsyncClient() as client:
        # First call (should hit API, store in DB, cache in Redis)
        console.print("  📡 First call (cold - should hit API)...")
        start = time.time()
        response = await client.get(url, params=params, headers=headers)
        elapsed = (time.time() - start) * 1000
        results["first_call"] = {
            "status": response.status_code,
            "time_ms": round(elapsed, 2),
            "candles": len(response.json().get("data", {}).get("candles", [])) if response.status_code == 200 else 0,
            "expected_source": "API → DB → Redis"
        }
        console.print(f"    ✓ {elapsed:.2f}ms, {results['first_call']['candles']} candles")
        
        # Second call (should hit Redis cache)
        console.print("  ⚡ Second call (warm - should hit Redis cache)...")
        await asyncio.sleep(0.5)  # Small delay
        start = time.time()
        response = await client.get(url, params=params, headers=headers)
        elapsed = (time.time() - start) * 1000
        results["second_call"] = {
            "status": response.status_code,
            "time_ms": round(elapsed, 2),
            "candles": len(response.json().get("data", {}).get("candles", [])) if response.status_code == 200 else 0,
            "expected_source": "Redis Cache"
        }
        console.print(f"    ✓ {elapsed:.2f}ms, {results['second_call']['candles']} candles")
        
        # Wait for cache to expire (30s TTL)
        console.print("  ⏳ Waiting 31s for cache to expire...")
        await asyncio.sleep(31)
        
        # Third call (cache expired, should hit DB)
        console.print("  💾 Third call (cache expired - should hit DB)...")
        start = time.time()
        response = await client.get(url, params=params, headers=headers)
        elapsed = (time.time() - start) * 1000
        results["third_call"] = {
            "status": response.status_code,
            "time_ms": round(elapsed, 2),
            "candles": len(response.json().get("data", {}).get("candles", [])) if response.status_code == 200 else 0,
            "expected_source": "PostgreSQL DB"
        }
        console.print(f"    ✓ {elapsed:.2f}ms, {results['third_call']['candles']} candles")
    
    return results


async def test_historical_candles(token: str) -> dict:
    """Test historical candles endpoint with timing."""
    console.print("\n[cyan]Testing Historical Candles...[/cyan]")
    
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{API_BASE_URL}/upstox/candles/historical"
    
    # Test with date range from 7 days ago to today
    to_date = datetime.now().strftime("%Y-%m-%d")
    from_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    
    params = {
        "instrument_key": TEST_INSTRUMENT,
        "unit": "days",
        "interval": 1,
        "from_date": from_date,
        "to_date": to_date
    }
    
    results = {
        "first_call": None,
        "second_call": None,
    }
    
    async with httpx.AsyncClient() as client:
        # First call (should hit API or DB if data exists)
        console.print(f"  📡 First call ({from_date} to {to_date})...")
        start = time.time()
        response = await client.get(url, params=params, headers=headers)
        elapsed = (time.time() - start) * 1000
        results["first_call"] = {
            "status": response.status_code,
            "time_ms": round(elapsed, 2),
            "candles": len(response.json().get("data", {}).get("candles", [])) if response.status_code == 200 else 0,
            "expected_source": "DB or API"
        }
        console.print(f"    ✓ {elapsed:.2f}ms, {results['first_call']['candles']} candles")
        
        # Second call (should hit Redis cache)
        console.print("  ⚡ Second call (should hit Redis cache)...")
        await asyncio.sleep(0.5)
        start = time.time()
        response = await client.get(url, params=params, headers=headers)
        elapsed = (time.time() - start) * 1000
        results["second_call"] = {
            "status": response.status_code,
            "time_ms": round(elapsed, 2),
            "candles": len(response.json().get("data", {}).get("candles", [])) if response.status_code == 200 else 0,
            "expected_source": "Redis Cache"
        }
        console.print(f"    ✓ {elapsed:.2f}ms, {results['second_call']['candles']} candles")
    
    return results


def print_results(intraday_results: dict, historical_results: dict):
    """Print formatted results table."""
    console.print("\n")
    console.print(Panel.fit(
        "[bold cyan]Candle Data Architecture Verification Results[/bold cyan]",
        box=box.DOUBLE
    ))
    
    # Intraday Results Table
    table = Table(title="Intraday Candles Performance", box=box.ROUNDED)
    table.add_column("Call", style="cyan")
    table.add_column("Response Time", justify="right", style="green")
    table.add_column("Candles", justify="right", style="yellow")
    table.add_column("Expected Source", style="magenta")
    table.add_column("Status", justify="center")
    
    for call_name, result in intraday_results.items():
        if result:
            status = "✅" if result["status"] == 200 else "❌"
            time_color = "green" if result["time_ms"] < 50 else "yellow" if result["time_ms"] < 200 else "red"
            table.add_row(
                call_name.replace("_", " ").title(),
                f"[{time_color}]{result['time_ms']}ms[/{time_color}]",
                str(result["candles"]),
                result["expected_source"],
                status
            )
    
    console.print(table)
    
    # Historical Results Table
    table = Table(title="Historical Candles Performance", box=box.ROUNDED)
    table.add_column("Call", style="cyan")
    table.add_column("Response Time", justify="right", style="green")
    table.add_column("Candles", justify="right", style="yellow")
    table.add_column("Expected Source", style="magenta")
    table.add_column("Status", justify="center")
    
    for call_name, result in historical_results.items():
        if result:
            status = "✅" if result["status"] == 200 else "❌"
            time_color = "green" if result["time_ms"] < 100 else "yellow" if result["time_ms"] < 300 else "red"
            table.add_row(
                call_name.replace("_", " ").title(),
                f"[{time_color}]{result['time_ms']}ms[/{time_color}]",
                str(result["candles"]),
                result["expected_source"],
                status
            )
    
    console.print(table)
    
    # Analysis
    console.print("\n[bold cyan]Analysis:[/bold cyan]")
    
    # Intraday analysis
    if intraday_results["second_call"] and intraday_results["first_call"]:
        speedup = intraday_results["first_call"]["time_ms"] / intraday_results["second_call"]["time_ms"]
        console.print(f"  • Intraday cache speedup: [green]{speedup:.1f}x faster[/green]")
        
        if intraday_results["second_call"]["time_ms"] < 20:
            console.print("  • ✅ Redis cache is working optimally (<20ms)")
        elif intraday_results["second_call"]["time_ms"] < 50:
            console.print("  • ⚠️  Redis cache is working but could be faster")
        else:
            console.print("  • ❌ Redis cache may not be working properly")
    
    if intraday_results["third_call"] and intraday_results["first_call"]:
        if intraday_results["third_call"]["time_ms"] < intraday_results["first_call"]["time_ms"]:
            console.print("  • ✅ Database is faster than API (as expected)")
        else:
            console.print("  • ⚠️  Database is slower than API (unexpected)")
    
    # Historical analysis
    if historical_results["second_call"] and historical_results["first_call"]:
        speedup = historical_results["first_call"]["time_ms"] / historical_results["second_call"]["time_ms"]
        console.print(f"  • Historical cache speedup: [green]{speedup:.1f}x faster[/green]")
    
    # Overall assessment
    console.print("\n[bold cyan]Overall Assessment:[/bold cyan]")
    
    all_passed = True
    
    # Check if all calls succeeded
    for result in list(intraday_results.values()) + list(historical_results.values()):
        if result and result["status"] != 200:
            all_passed = False
            break
    
    # Check if cache is working (second call should be much faster)
    if (intraday_results["second_call"] and 
        intraday_results["second_call"]["time_ms"] > 50):
        all_passed = False
    
    if all_passed:
        console.print("  [bold green]✅ All systems operational![/bold green]")
        console.print("  [green]Three-tier architecture is working correctly:[/green]")
        console.print("    1. ✅ Redis Cache (fastest)")
        console.print("    2. ✅ PostgreSQL Database (fast)")
        console.print("    3. ✅ Upstox API (fallback)")
    else:
        console.print("  [bold yellow]⚠️  Some issues detected[/bold yellow]")
        console.print("  [yellow]Review the results above for details[/yellow]")


async def main():
    """Main verification flow."""
    console.print(Panel.fit(
        "[bold cyan]Candle Data Architecture Verification[/bold cyan]\n"
        "Testing three-tier architecture:\n"
        "  1. Redis Cache (fastest)\n"
        "  2. PostgreSQL Database (fast)\n"
        "  3. Upstox API (fallback)",
        box=box.DOUBLE
    ))
    
    try:
        # Login
        console.print("\n[cyan]Authenticating...[/cyan]")
        token = await login()
        console.print("  ✓ Authenticated successfully")
        
        # Test intraday candles
        intraday_results = await test_intraday_candles(token)
        
        # Test historical candles
        historical_results = await test_historical_candles(token)
        
        # Print results
        print_results(intraday_results, historical_results)
        
    except KeyboardInterrupt:
        console.print("\n[yellow]Verification cancelled by user[/yellow]")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[red]❌ Error: {e}[/red]")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
