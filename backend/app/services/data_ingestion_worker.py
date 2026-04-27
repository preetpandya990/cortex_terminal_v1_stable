"""
Cortex AI — Market Data Ingestion Worker
==========================================
Two-phase automated OHLCV pipeline:

  Phase 1 — Backfill  : fills all historical gaps concurrently, chunking each
                         request to stay within Upstox V3 date-window limits.
  Phase 2 — Maintenance: hourly incremental sync keeps data current.

Upstox V3 historical-candle API constraints (per official documentation):
  minutes 1–15  → 1 month per request   │ available from 2022-01-01
  minutes 16+   → 1 quarter per request │ available from 2022-01-01
  hours 1–5     → 1 quarter per request │ available from 2022-01-01
  days          → 1 decade per request  │ available from 2000-01-01

Rate limit budget: 400 req/min (80% of Upstox's 500/min ceiling).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import text

from app.core.config import get_settings
from app.exceptions import UpstoxAPIError, UpstoxInvalidInstrumentError, UpstoxRateLimitError, DatabaseError
from app.services.data_ingestion import bulk_upsert_ohlcv
from app.services.upstox_client import UpstoxClient

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Upstox V3 API – per-timeframe constraints ──────────────────────────────────

class TimeframeSpec(NamedTuple):
    db_key: str           # Key stored in upstox_ohlcv.timeframe
    unit: str             # Upstox API path segment: minutes / hours / days
    interval: int         # Upstox API path segment: 1, 5, 15, 30, 60 …
    chunk_days: int       # Max date span per single API call
    available_from: date  # Earliest date Upstox holds data for this unit
    target_days: int      # How far back we want to maintain
    priority: int         # Lower = higher priority (processed first)


# Ordered by priority: daily first (fewest calls, most ML value), then lower TFs
TIMEFRAME_SPECS: dict[str, TimeframeSpec] = {
    "1D": TimeframeSpec(
        db_key="1D", unit="days", interval=1,
        chunk_days=3650,                   # 1 decade per request
        available_from=date(2000, 1, 1),
        target_days=3650,                  # 10 years
        priority=1,
    ),
    "1hour": TimeframeSpec(
        db_key="1hour", unit="hours", interval=1,
        chunk_days=89,                     # 1 quarter per request
        available_from=date(2022, 1, 1),
        target_days=1460,                  # Full available history (~4 years)
        priority=2,
    ),
    "30minute": TimeframeSpec(
        db_key="30minute", unit="minutes", interval=30,
        chunk_days=89,                     # 1 quarter (>15 min)
        available_from=date(2022, 1, 1),
        target_days=1460,
        priority=3,
    ),
    "15minute": TimeframeSpec(
        db_key="15minute", unit="minutes", interval=15,
        chunk_days=28,                     # 1 month (≤15 min)
        available_from=date(2022, 1, 1),
        target_days=1460,
        priority=4,
    ),
    "5minute": TimeframeSpec(
        db_key="5minute", unit="minutes", interval=5,
        chunk_days=28,
        available_from=date(2022, 1, 1),
        target_days=1460,
        priority=5,
    ),
    "1minute": TimeframeSpec(
        db_key="1minute", unit="minutes", interval=1,
        chunk_days=28,
        available_from=date(2022, 1, 1),
        target_days=365,                   # 1 year — storage constraint
        priority=6,
    ),
}

# Only ingest these timeframes in automated worker runs.
ENABLED_TIMEFRAMES: set[str] = {"1D", "1hour"}


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass(frozen=True, order=True)
class FetchTask:
    """One discrete API call: a single date-windowed chunk for one instrument."""
    priority: int
    from_date: date
    instrument_key: str = field(compare=False)
    symbol: str = field(compare=False)
    timeframe: str = field(compare=False)
    to_date: date = field(compare=False)

    @property
    def label(self) -> str:
        return f"{self.symbol}|{self.timeframe}|{self.from_date}→{self.to_date}"


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class IngestionStats:
    tasks_total: int = 0
    tasks_completed: int = 0
    tasks_skipped: int = 0
    candles_ingested: int = 0
    api_errors: int = 0
    db_errors: int = 0
    dlq_instruments: int = 0

    def log_summary(self, phase: str, elapsed_s: float) -> None:
        logger.info(
            "%s complete | %.0fs | tasks=%d/%d | candles=%d | "
            "api_err=%d | db_err=%d | dlq=%d",
            phase, elapsed_s,
            self.tasks_completed, self.tasks_total,
            self.candles_ingested,
            self.api_errors, self.db_errors, self.dlq_instruments,
        )


# ── Token-bucket rate limiter ──────────────────────────────────────────────────

class TokenBucketRateLimiter:
    """
    Thread-safe asyncio token bucket.
    Smooths bursts so we never exceed the configured requests-per-minute ceiling.
    """

    def __init__(self, requests_per_minute: int) -> None:
        self._rate = requests_per_minute / 60.0   # tokens per second
        self._tokens = 0.0                        # start empty — no startup burst
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                settings.DATA_INGESTION_REQUESTS_PER_MINUTE,
                self._tokens + elapsed * self._rate,
            )
            self._last_refill = now

            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0

    async def drain(self) -> None:
        """
        Empty the bucket immediately.

        Must be called after a 429 response before the backoff sleep.
        Without this, the bucket accumulates tokens during the 60-second sleep
        and all waiting workers fire in a burst the instant they wake up —
        triggering another round of 429s.
        """
        async with self._lock:
            self._tokens = 0.0
            self._last_refill = time.monotonic()


# ── Circuit breaker ────────────────────────────────────────────────────────────

class CircuitBreaker:
    """
    Opens after `threshold` consecutive failures; allows one probe request
    after `timeout_s` seconds; closes on a successful probe.
    """

    def __init__(self, threshold: int, timeout_s: int) -> None:
        self._threshold = threshold
        self._timeout_s = timeout_s
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        if self._state is CircuitState.OPEN and self._opened_at is not None:
            if time.monotonic() - self._opened_at >= self._timeout_s:
                self._state = CircuitState.HALF_OPEN
                self._failures = 0
                logger.info("Circuit breaker → HALF_OPEN (probing recovery)")
        return self._state

    @property
    def is_open(self) -> bool:
        return self.state is CircuitState.OPEN

    def record_success(self) -> None:
        if self._state is CircuitState.HALF_OPEN:
            logger.info("Circuit breaker → CLOSED (API recovered)")
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._state is CircuitState.CLOSED and self._failures >= self._threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.monotonic()
            logger.error(
                "Circuit breaker → OPEN after %d consecutive failures", self._failures
            )


# ── Core worker ────────────────────────────────────────────────────────────────

class DataIngestionWorker:
    """
    Orchestrates gap detection, date-window chunking, concurrent API fetching,
    OHLCV persistence, and two-phase (backfill → maintenance) lifecycle.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        upstox_client: UpstoxClient,
    ) -> None:
        self._session_factory = session_factory
        self._client = upstox_client
        self._rate_limiter = TokenBucketRateLimiter(settings.DATA_INGESTION_REQUESTS_PER_MINUTE)
        self._circuit = CircuitBreaker(
            threshold=settings.DATA_INGESTION_CIRCUIT_BREAKER_THRESHOLD,
            timeout_s=settings.DATA_INGESTION_CIRCUIT_BREAKER_TIMEOUT,
        )
        self._semaphore = asyncio.Semaphore(settings.DATA_INGESTION_CONCURRENCY)
        self._dead_letter: set[str] = set()           # instrument_key → permanent failure
        self._retry_counts: dict[str, int] = defaultdict(int)
        self._token_refreshed = asyncio.Event()

    # ── Gap detection ──────────────────────────────────────────────────────────

    async def detect_gaps(self, *, backfill: bool) -> list[FetchTask]:
        """
        Returns a sorted list of FetchTask objects — one per date-windowed chunk.

        In backfill mode, targets the full configured history per timeframe.
        In maintenance mode, targets only the last 30 days per timeframe.
        """
        today = date.today()
        tasks: list[FetchTask] = []

        async with self._session_factory() as session:
            instruments = await self._load_instruments(session)
            if not instruments:
                logger.warning("No instruments found in instrument_master — skipping gap detection")
                return []

            logger.info(
                "Gap detection | mode=%s | instruments=%d",
                "backfill" if backfill else "maintenance",
                len(instruments),
            )

            enabled_specs = [
                spec
                for key, spec in TIMEFRAME_SPECS.items()
                if key in ENABLED_TIMEFRAMES
            ]
            for spec in sorted(enabled_specs, key=lambda s: s.priority):
                coverage = await self._load_coverage(session, spec.db_key)

                for instrument_key, symbol in instruments:
                    if instrument_key in self._dead_letter:
                        continue

                    # Determine the desired start date for this instrument/timeframe
                    if backfill:
                        want_from = max(
                            spec.available_from,
                            today - timedelta(days=spec.target_days),
                        )
                    else:
                        want_from = today - timedelta(days=30)

                    want_to = today - timedelta(days=1)   # yesterday (last closed day)

                    if want_from > want_to:
                        continue

                    cov = coverage.get(instrument_key)
                    if cov is None:
                        # No data at all — fetch everything
                        chunks = self._date_chunks(want_from, want_to, spec.chunk_days)
                    else:
                        earliest, latest = cov
                        chunks = []
                        # Historical gap (before earliest known data)
                        if backfill and earliest > want_from:
                            hist_end = earliest - timedelta(days=1)
                            if want_from <= hist_end:
                                chunks.extend(self._date_chunks(want_from, hist_end, spec.chunk_days))
                        # Recent gap (after latest known data)
                        if latest < want_to:
                            rec_start = latest + timedelta(days=1)
                            if rec_start <= want_to:
                                chunks.extend(self._date_chunks(rec_start, want_to, spec.chunk_days))

                    for chunk_from, chunk_to in chunks:
                        tasks.append(FetchTask(
                            priority=spec.priority,
                            from_date=chunk_from,
                            to_date=chunk_to,
                            instrument_key=instrument_key,
                            symbol=symbol,
                            timeframe=spec.db_key,
                        ))

        tasks.sort()
        logger.info("Gap detection complete | chunks=%d", len(tasks))
        return tasks

    @staticmethod
    async def _load_instruments(session: AsyncSession) -> list[tuple[str, str]]:
        result = await session.execute(text(
            "SELECT instrument_key, trading_symbol "
            "FROM instrument_master "
            "WHERE exchange = 'NSE' "
            "ORDER BY trading_symbol"
        ))
        return result.all()  # type: ignore[return-value]

    @staticmethod
    async def _load_coverage(
        session: AsyncSession, timeframe: str
    ) -> dict[str, tuple[date, date]]:
        """Returns {instrument_key: (earliest_date, latest_date)} for one timeframe."""
        result = await session.execute(
            text(
                "SELECT instrument_key, "
                "       MIN(timestamp)::date AS earliest, "
                "       MAX(timestamp)::date AS latest "
                "FROM upstox_ohlcv "
                "WHERE timeframe = :tf "
                "GROUP BY instrument_key"
            ),
            {"tf": timeframe},
        )
        return {row.instrument_key: (row.earliest, row.latest) for row in result}

    @staticmethod
    def _date_chunks(from_date: date, to_date: date, chunk_days: int) -> list[tuple[date, date]]:
        """Split [from_date, to_date] into non-overlapping windows of ≤ chunk_days."""
        chunks: list[tuple[date, date]] = []
        cursor = from_date
        while cursor <= to_date:
            end = min(cursor + timedelta(days=chunk_days - 1), to_date)
            chunks.append((cursor, end))
            cursor = end + timedelta(days=1)
        return chunks

    # ── Fetch & persist ────────────────────────────────────────────────────────

    async def fetch_chunk(self, task: FetchTask) -> int:
        """
        Executes one API call, validates candles, persists to DB.
        Returns the number of candles stored (0 on any non-fatal error).
        """
        if self._circuit.is_open:
            logger.debug("Circuit open — skipping %s", task.label)
            return 0

        spec = TIMEFRAME_SPECS[task.timeframe]
        path = (
            f"/historical-candle/{task.instrument_key}"
            f"/{spec.unit}/{spec.interval}"
            f"/{task.to_date.isoformat()}/{task.from_date.isoformat()}"
        )

        try:
            await self._rate_limiter.acquire()
            data = await self._client.get(path)

            if data.get("message") == "Mock data - Upstox API unavailable":
                return 0

            candles = data.get("data", {}).get("candles", [])
            if not candles:
                self._circuit.record_success()
                self._retry_counts.pop(task.instrument_key, None)
                return 0

            rows = self._validate_candles(candles, task)
            if not rows:
                self._circuit.record_success()
                return 0

            async with self._session_factory() as session:
                await bulk_upsert_ohlcv(session, rows)

            self._circuit.record_success()
            self._retry_counts.pop(task.instrument_key, None)
            logger.debug("Fetched %s | %d candles", task.label, len(rows))
            return len(rows)

        except UpstoxInvalidInstrumentError:
            # Permanent error — instrument delisted or key changed in Upstox.
            # Add to dead letter immediately; no retry, no circuit breaker impact.
            self._dead_letter.add(task.instrument_key)
            logger.warning(
                "Invalid instrument key — permanently skipping: %s (%s)",
                task.symbol, task.instrument_key,
            )
            return 0

        except UpstoxRateLimitError:
            # Transient — Cloudflare / Upstox rate limit. Drain the token bucket
            # BEFORE sleeping so workers don't burst again the moment they wake up.
            logger.warning("Rate-limited by Upstox | draining bucket and backing off 60s")
            await self._rate_limiter.drain()
            await asyncio.sleep(60)
            return 0

        except UpstoxAPIError as exc:
            return await self._handle_api_error(exc, task)

        except DatabaseError as exc:
            logger.error("DB error | %s | %s", task.label, exc)
            return 0

        except Exception as exc:
            logger.exception("Unexpected error | %s | %s", task.label, exc)
            return 0

    @staticmethod
    def _validate_candles(raw: list, task: FetchTask) -> list[dict]:
        rows: list[dict] = []
        for candle in raw:
            if len(candle) < 6:
                continue
            ts_raw, o, h, l, c, vol = candle[0], candle[1], candle[2], candle[3], candle[4], candle[5]
            oi = candle[6] if len(candle) > 6 else 0

            # Parse ISO timestamp string
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw)
            elif isinstance(ts_raw, datetime):
                ts = ts_raw
            else:
                continue

            # Ensure timezone-aware
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            # Reject non-positive prices
            if not all(isinstance(p, (int, float)) and p > 0 for p in (o, h, l, c)):
                continue

            # Enforce OHLC sanity: low ≤ open,close ≤ high
            if not (l <= o <= h and l <= c <= h):
                logger.debug(
                    "OHLC violation skipped | %s | O=%.4f H=%.4f L=%.4f C=%.4f",
                    task.label, o, h, l, c,
                )
                continue

            rows.append({
                "instrument_key": task.instrument_key,
                "timeframe": task.timeframe,
                "timestamp": ts,
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": int(vol),
                "oi": int(oi),
            })
        return rows

    async def _handle_api_error(self, exc: UpstoxAPIError, task: FetchTask) -> int:
        # Only genuine infrastructure failures (5xx, timeouts, etc.) reach here.
        # 400 → UpstoxInvalidInstrumentError (caught above, never reaches here)
        # 429 → UpstoxRateLimitError          (caught above, never reaches here)
        self._circuit.record_failure()

        if exc.upstream_status == 401:
            logger.critical(
                "Upstox token expired | pausing ingestion\n"
                "  → Update UPSTOX_ACCESS_TOKEN in .env; worker will auto-resume."
            )
            await self._wait_for_token_refresh()
            return 0

        self._retry_counts[task.instrument_key] += 1
        if self._retry_counts[task.instrument_key] >= settings.DATA_INGESTION_MAX_RETRIES:
            self._dead_letter.add(task.instrument_key)
            logger.error(
                "DLQ | %s after %d failures",
                task.symbol, self._retry_counts[task.instrument_key],
            )
        else:
            logger.warning(
                "API error | %s | status=%s | retry %d/%d",
                task.label,
                exc.upstream_status,
                self._retry_counts[task.instrument_key],
                settings.DATA_INGESTION_MAX_RETRIES,
            )
        return 0

    async def _wait_for_token_refresh(self) -> None:
        """Polls the .env file every 30 s until a new token is found."""
        env_path = Path(os.getcwd()) / ".env"
        old_token = settings.UPSTOX_ACCESS_TOKEN

        while True:
            await asyncio.sleep(30)
            try:
                new_token = self._read_token_from_env(env_path)
                if new_token and new_token != old_token:
                    self._client.set_access_token(new_token)
                    self._circuit.record_success()
                    logger.info("Token refreshed — resuming ingestion")
                    return
            except Exception:
                pass
            logger.debug("Waiting for UPSTOX_ACCESS_TOKEN update in .env …")

    @staticmethod
    def _read_token_from_env(path: Path) -> str | None:
        if not path.exists():
            return None
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("UPSTOX_ACCESS_TOKEN="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                return val or None
        return None

    # ── Concurrent batch processing ────────────────────────────────────────────

    async def run_phase(self, tasks: list[FetchTask], phase: str) -> IngestionStats:
        """Process a list of FetchTask objects concurrently with bounded parallelism."""
        stats = IngestionStats(tasks_total=len(tasks))
        started = time.monotonic()

        async def _worker(task: FetchTask) -> None:
            async with self._semaphore:
                if task.instrument_key in self._dead_letter:
                    stats.tasks_skipped += 1
                    return
                try:
                    n = await self.fetch_chunk(task)
                    stats.candles_ingested += n
                    stats.tasks_completed += 1
                except Exception:
                    stats.api_errors += 1

        await asyncio.gather(*(_worker(t) for t in tasks))

        stats.dlq_instruments = len(self._dead_letter)
        stats.log_summary(phase, time.monotonic() - started)
        return stats


# ── Worker loop (entry point for worker.py) ────────────────────────────────────

async def data_ingestion_loop(
    session_factory: async_sessionmaker,
    upstox_client: UpstoxClient,
) -> None:
    """
    Main entry point called by worker.py.

    Phase 1 — Backfill: detect and fill all historical gaps. Runs until the
      gap list is empty, logging progress every 500 tasks.

    Phase 2 — Maintenance: every DATA_INGESTION_CHECK_INTERVAL seconds, fill
      any gaps in the last 30 days (new listings, missed days, etc.).
    """
    if not settings.DATA_INGESTION_ENABLED:
        logger.info("Data ingestion worker disabled (DATA_INGESTION_ENABLED=false)")
        return

    await _await_token(upstox_client)

    logger.info(
        "Data ingestion worker starting | concurrency=%d | rate=%d req/min | "
        "circuit_threshold=%d | backfill=%s",
        settings.DATA_INGESTION_CONCURRENCY,
        settings.DATA_INGESTION_REQUESTS_PER_MINUTE,
        settings.DATA_INGESTION_CIRCUIT_BREAKER_THRESHOLD,
        settings.DATA_INGESTION_BACKFILL_ENABLED,
    )

    worker = DataIngestionWorker(session_factory, upstox_client)

    # ── Phase 1: Backfill ──────────────────────────────────────────────────────
    if settings.DATA_INGESTION_BACKFILL_ENABLED:
        logger.info("Phase 1 — Backfill starting")
        iteration = 0
        while True:
            iteration += 1
            tasks = await worker.detect_gaps(backfill=True)
            if not tasks:
                logger.info("Phase 1 — Backfill complete (0 gaps remaining)")
                break

            logger.info("Phase 1 — iteration %d | %d chunks to fetch", iteration, len(tasks))
            await worker.run_phase(tasks, f"Backfill-{iteration}")

            # Brief pause between iterations to let DB writes settle
            await asyncio.sleep(5)

    # ── Phase 2: Maintenance ───────────────────────────────────────────────────
    logger.info(
        "Phase 2 — Maintenance loop starting | check_interval=%ds",
        settings.DATA_INGESTION_CHECK_INTERVAL,
    )
    while True:
        try:
            await asyncio.sleep(settings.DATA_INGESTION_CHECK_INTERVAL)
            tasks = await worker.detect_gaps(backfill=False)
            if tasks:
                logger.info("Phase 2 — %d maintenance chunks", len(tasks))
                await worker.run_phase(tasks, "Maintenance")
            else:
                logger.debug("Phase 2 — data current, no gaps")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Maintenance cycle error — will retry next interval")


async def _await_token(upstox_client: UpstoxClient) -> None:
    """Block until an access token is available, checking every 30 s."""
    if upstox_client.has_token:
        return

    logger.warning(
        "UPSTOX_ACCESS_TOKEN not configured — data ingestion paused. "
        "Set the token in .env to resume."
    )
    while not upstox_client.has_token:
        await asyncio.sleep(30)
        token = DataIngestionWorker._read_token_from_env(Path(os.getcwd()) / ".env")
        if token:
            upstox_client.set_access_token(token)
            logger.info("Access token detected — starting data ingestion")
