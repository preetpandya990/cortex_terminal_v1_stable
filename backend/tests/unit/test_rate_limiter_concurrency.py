"""
Regression test for the TokenBucketRateLimiter concurrency bug (2026-07-17).

The wait path slept without advancing ``_last_refill``, so each concurrent
waiter re-credited itself the same sleep interval — effective throughput was
rate × concurrency (observed live: 120/min at a configured 60 with
concurrency=2). Invisible in production because the worker sidecar runs
DATA_INGESTION_CONCURRENCY=1.
"""

import asyncio
import time

import pytest

from app.services.data_ingestion_worker import TokenBucketRateLimiter


async def _hammer(limiter: TokenBucketRateLimiter, n_requests: int, concurrency: int) -> float:
    """N acquires through `concurrency` workers; returns elapsed seconds."""
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(n_requests):
        queue.put_nowait(i)

    async def worker() -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            await limiter.acquire()

    start = time.monotonic()
    await asyncio.gather(*(worker() for _ in range(concurrency)))
    return time.monotonic() - start


class TestTokenBucketConcurrency:
    @pytest.mark.parametrize("concurrency", [1, 2, 4])
    async def test_sustained_rate_independent_of_concurrency(self, concurrency):
        """10 acquires at 600/min (10/s) must take ~1s regardless of workers.

        Before the fix, concurrency=2 finished in ~0.5s and concurrency=4 in
        ~0.25s — the double-credit bug.
        """
        limiter = TokenBucketRateLimiter(requests_per_minute=600)  # 10 tokens/s
        elapsed = await _hammer(limiter, n_requests=10, concurrency=concurrency)

        # Bucket starts empty: 10 tokens take ≥ ~0.9s to mint at 10/s.
        assert elapsed >= 0.85, (
            f"concurrency={concurrency}: 10 acquires at 10/s took {elapsed:.2f}s "
            f"— rate limiter is leaking throughput under concurrency"
        )
        assert elapsed < 2.0  # and it must not be pathologically slow either

    async def test_burst_capacity_is_capped_at_configured_rate(self):
        """After a long idle the bucket may burst at most 1 minute of tokens."""
        limiter = TokenBucketRateLimiter(requests_per_minute=600)
        limiter._tokens = 1e9  # simulate corrupted/over-accrued state
        await limiter.acquire()  # refill path must clamp to capacity
        assert limiter._tokens <= 600.0

    async def test_drain_resets_bucket_and_clock(self):
        limiter = TokenBucketRateLimiter(requests_per_minute=600)
        limiter._tokens = 50.0
        before = time.monotonic()
        await limiter.drain()
        assert limiter._tokens == 0.0
        assert limiter._last_refill >= before
