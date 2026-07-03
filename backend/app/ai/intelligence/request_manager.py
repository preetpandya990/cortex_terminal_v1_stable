"""
Gemini Request Manager
======================
Central coordinator for all Gemini API access within the Cortex AI layer.

Problem
-------
Multiple independent callers — explanation worker, news forecaster, NLP
sentiment, event classifier, RAG embedder, health check, and others — share a
single Gemini API key with no coordination.  Under market-hours load they burst
simultaneously, hit the per-minute limit, and their retries starve each other:
every retry storm can delay the next legitimate call by 30–60 seconds.  A
process restart loses circuit state, wasting the first post-restart call on a
guaranteed 429.

Solution
--------
Three cooperating mechanisms:

1. **Priority queue** — Five tiers (CRITICAL → BACKGROUND) served in strict
   order with FIFO within each tier.  User-facing explanations are always
   ahead of background RAG embeddings, regardless of arrival order.  See the
   ``Priority`` enum for per-tier assignment guidelines.

2. **Token buckets** — ``generate`` and ``embed`` are tracked separately
   because they have independent quota limits.  ``generate`` has both an RPM
   and a TPM bucket; ``embed`` has RPM only (not TPM-metered by the API).
   Callers suspend asynchronously via ``asyncio.Event``; there is no
   busy-polling or sleep loop on the caller side.

3. **Redis-backed circuit breaker** — When a daily-quota 429 is detected,
   the circuit is written to Redis with a TTL of "seconds until midnight PT".
   On the next app restart ``initialize()`` pre-populates the in-process state
   from Redis so the first post-restart call is a sub-millisecond fast-fail
   rather than a wasted API round-trip.  If Redis is unavailable at startup
   the manager fails open (assumes circuit closed) and will reopen the circuit
   on the next 429 it observes.

Architecture
------------
All Gemini API calls go through::

    permit = await manager.acquire(op, priority, estimated_tokens)
    try:
        response = await <gemini_call>
    finally:
        manager.release(permit, actual_tokens=output_tokens)

A single background ``asyncio.Task`` (the dispatcher) pops the highest-priority
permit, waits for token budget, deducts, then sets ``permit.ready`` — unblocking
the caller.  Because the event loop is single-threaded and all token-bucket
operations are synchronous (no ``await``), no asyncio locks are required.

Failure modes for callers
-------------------------
``GeminiQuotaExhausted``
    Daily quota is exhausted.  The circuit is open until midnight Pacific Time.
    **Do not retry** — treat as non-retryable and degrade gracefully (skip the
    call, return a cached result, or surface a user-facing message).  The
    circuit auto-closes the moment the new quota period begins.

``GeminiBudgetThrottled`` (subclass of ``GeminiRateLimitError``)
    The daily generate budget is within the HIGH-priority reservation band.
    Only MEDIUM / LOW / BACKGROUND callers receive this.  HIGH and CRITICAL
    are always admitted.  Degrade gracefully — identical handling to
    ``GeminiRateLimitError``.  Auto-resolves at midnight Pacific Time.

``GeminiRateLimitError``
    Either the queue is at ``GEMINI_MAX_QUEUE_DEPTH`` capacity (permit rejected
    immediately) or ``GEMINI_PERMIT_TIMEOUT`` elapsed before the dispatcher
    could grant a permit.  The system is under heavy per-minute load.  Skip or
    degrade — the queue drains naturally as the dispatcher works through
    existing permits; a retry within the same request is unlikely to help.

Thread safety
-------------
Pure asyncio — all shared state is mutated only from the event loop thread.
Never call any method from a thread pool (``run_in_executor``) without adding
appropriate synchronization.

Wiring a new caller
-------------------
1. Choose ``Operation.GENERATE`` or ``Operation.EMBED`` based on which Gemini
   API surface you are calling.  They have separate quota tracks and separate
   circuit breakers — never mix them.

2. Choose a ``Priority`` tier.  See the ``Priority`` enum docstring for
   per-tier guidelines.  When in doubt: MEDIUM for live pipeline work,
   BACKGROUND for offline batch jobs.

3. Wrap the call site::

    # FastAPI lifespan startup (after init_redis):
    from app.ai.intelligence.request_manager import GeminiRequestManager
    from app.ai.intelligence.llm_client import _key_id
    key_ids = [_key_id(k) for k in settings.gemini_api_key_pool]
    await GeminiRequestManager.initialize(redis=get_redis(), key_ids=key_ids)

    # Call site:
    from app.ai.intelligence.request_manager import (
        get_request_manager, Priority, Operation,
        GeminiQuotaExhausted, GeminiRateLimitError, GeminiBudgetThrottled,
    )
    manager = get_request_manager()
    permit = await manager.acquire(Operation.GENERATE, Priority.HIGH)
    try:
        result = await <gemini_call>
    except Exception:
        manager.release(permit, outcome="error")
        raise
    else:
        manager.release(permit, actual_tokens=result.output_tokens)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from typing import ClassVar
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)


# ── Public exceptions ──────────────────────────────────────────────────────────

class GeminiQuotaExhausted(Exception):
    """
    Daily Gemini quota exhausted — quota circuit is open.

    Raised immediately (zero network I/O) when ``acquire()`` is called while
    the circuit breaker is open.  Treat as non-retryable until the next quota
    reset (midnight Pacific Time).

    Phase 2 note: the same-named exception in ``llm_client`` will become an
    alias for this class once the manager is wired into ``_acall()``.  Callers
    that catch either import path are forward-compatible.
    """


class GeminiRateLimitError(Exception):
    """
    Per-minute rate-limit backpressure — priority queue is full or timed out.

    Raised when ``GEMINI_MAX_QUEUE_DEPTH`` is already full (queue full) or
    ``GEMINI_PERMIT_TIMEOUT`` elapsed before the dispatcher could grant a
    permit (timeout).  Callers should skip or degrade gracefully; the queue
    drains naturally as the dispatcher works through existing permits.
    """


class GeminiBudgetThrottled(GeminiRateLimitError):
    """
    Daily Gemini generate budget is within the HIGH-priority reservation band.

    Raised on MEDIUM / LOW / BACKGROUND ``acquire()`` calls when the estimated
    remaining daily budget falls below ``GEMINI_HIGH_PRIORITY_RPD_RESERVE``.
    HIGH and CRITICAL priority callers are **never** throttled by the budget
    guard — the reservation exists specifically to guarantee headroom for them.

    Treat identically to ``GeminiRateLimitError``: degrade gracefully and do
    not retry.  The throttle auto-resolves at midnight Pacific Time when the
    Gemini RPD counter resets and the budget guard disengages.

    Inherits from ``GeminiRateLimitError`` so callers that already handle the
    parent exception need no changes.
    """


# ── Enums ──────────────────────────────────────────────────────────────────────

class Operation(str):
    """
    Gemini API operation type.

    Each operation has a separate quota track and token bucket.  Using string
    values directly simplifies Prometheus label assignment.
    """
    GENERATE = "generate"
    EMBED    = "embed"


class Priority(IntEnum):
    """
    Priority tier for the GeminiRequestManager permit queue.

    Lower integer value = higher urgency.  The dispatcher always serves the
    lowest integer (highest urgency) permit that can be satisfied by the
    current token budget.

    Assignment guidelines
    ---------------------
    CRITICAL   — health checks, startup probes (never compete with workloads)
    HIGH       — user-facing, latency-sensitive (trade suggestion explanation)
    MEDIUM     — user-visible pipeline (forecaster, sentiment, instrument context)
    LOW        — background classification (event_classifier Gemini path, ~5–10% of articles)
    BACKGROUND — offline batch work (RAG corpus embeddings, eval harness)
    """
    CRITICAL   = 1
    HIGH       = 2
    MEDIUM     = 3
    LOW        = 4
    BACKGROUND = 5

    @property
    def label(self) -> str:
        """Lowercase name suitable for Prometheus label values."""
        return self.name.lower()


# ── Internal constants ─────────────────────────────────────────────────────────

# All valid operation strings (used for iteration in initialize/aclose).
_ALL_OPERATIONS: tuple[str, ...] = (Operation.GENERATE, Operation.EMBED)


def _circuit_redis_key(op: str, key_id: str) -> str:
    """Redis key for the per-key, per-operation circuit breaker.

    The TTL is set to ``seconds until midnight Pacific Time + GEMINI_QUOTA_RESET_BUFFER_MINUTES``
    so that a Redis expiry acts as a safety net for crash-restart recovery: if the app
    was down when the quota reset watcher would have fired, the expired key causes the
    circuit to load as closed on the next startup — no manual action required.
    """
    return f"cortex:gemini:circuit:{op}:{key_id}"


def _rpd_redis_key() -> str:
    """Redis key for today's (Pacific Time) generate RPD usage counter.

    The PT date suffix scopes the counter to the correct Gemini quota day —
    Gemini resets RPD at midnight Pacific Time.  A process restart mid-day
    loads this key and resumes from the persisted count rather than resetting
    to zero, preventing a post-restart burst from ignoring already-consumed
    quota.  The key carries a 25-hour TTL so it expires naturally one hour
    after the next quota reset, even without an explicit delete.
    """
    date_pt = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    return f"cortex:gemini:rpd:generate:{date_pt}"


def _seconds_until_quota_reset(buffer_secs: int) -> float:
    """Seconds until Gemini's daily RPD quota reset + a safety buffer.

    Gemini resets Requests Per Day counters at **midnight Pacific Time** (a fixed
    wall-clock boundary that shifts with DST: PDT = UTC-7, PST = UTC-8).  Community
    reports confirm a 0–15 minute propagation lag before the counter actually flips,
    so a buffer of at least 15 minutes is recommended before re-enabling a key.

    The returned value is always positive.  It is safe to call immediately after a
    quota trip — at worst the caller sleeps an additional ``buffer_secs`` beyond
    tomorrow's midnight.

    Args:
        buffer_secs: Extra seconds to add after midnight PT (absorbs propagation lag).
    """
    pt = ZoneInfo("America/Los_Angeles")
    now_pt = datetime.now(pt)
    midnight_tomorrow_pt = (now_pt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    reset_utc = midnight_tomorrow_pt.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    return max(1.0, (reset_utc - now_utc).total_seconds() + buffer_secs)


# ── Permit dataclass ───────────────────────────────────────────────────────────

@dataclass(order=True)
class _Permit:
    """
    A slot in the GeminiRequestManager priority queue.

    Sortable by ``(priority, sequence)`` — the asyncio.PriorityQueue dequeues
    the item with the numerically smallest tuple first.  Since CRITICAL=1 <
    HIGH=2 < … < BACKGROUND=5, the most urgent items are served first.

    Callers block on ``ready.wait()`` until the dispatcher sets it, indicating
    that the token budget has been deducted and the caller may proceed.
    ``cancelled`` is set True by ``open_circuit()`` (quota exhaustion) or on
    permit timeout, so the caller raises the appropriate exception on wake-up.
    """

    priority:         int           # sort key 1 — lower = higher urgency
    sequence:         int           # sort key 2 — FIFO within the same tier

    # The remaining fields are excluded from ordering comparisons.
    operation:        str           = field(compare=False)
    estimated_tokens: int           = field(compare=False)
    ready:            asyncio.Event = field(compare=False, default_factory=asyncio.Event)
    cancelled:        bool          = field(compare=False, default=False)
    enqueued_at:      float         = field(compare=False, default_factory=time.monotonic)

    def __hash__(self) -> int:
        # sequence is assigned once from a global monotonic counter and never
        # mutated, making it a stable, collision-free hash key.  This is
        # consistent with the dataclass-generated __eq__, which compares
        # (priority, sequence) — and since sequence is globally unique, that
        # comparison is already effectively identity-based.
        return hash(self.sequence)


# ── Token bucket ───────────────────────────────────────────────────────────────

class _TokenBucket:
    """
    Continuous-refill token bucket for Gemini API rate limiting.

    Tokens refill at a constant rate (``refill_rate`` tokens/second) up to
    ``capacity``.  Setting ``capacity = min(rpm, 10)`` as a burst cap prevents
    a quiet period from banking a full minute's budget and releasing it in a
    single burst — any burst is capped at 10 requests regardless of tier.

    All operations are synchronous.  In the asyncio event loop, synchronous
    code between ``await`` points is atomic — no lock is needed provided the
    dispatcher is the sole caller of ``consume()`` and ``can_satisfy()``.
    ``credit()`` is called by arbitrary coroutines but is also synchronous and
    therefore safe.

    The bucket starts full (``capacity`` tokens available) so a newly
    started app can immediately serve requests.
    """

    __slots__ = ("_capacity", "_tokens", "_refill_rate", "_last_refill")

    def __init__(self, capacity: int, refill_rate: float) -> None:
        self._capacity: float    = float(capacity)
        self._tokens: float      = float(capacity)   # start full
        self._refill_rate: float = max(refill_rate, 1e-9)  # guard div-by-zero
        self._last_refill: float = time.monotonic()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    # ── Dispatcher API (single caller — no lock needed) ────────────────────────

    def can_satisfy(self, tokens: float) -> bool:
        """True if ``tokens`` are available after refill."""
        self._refill()
        return self._tokens >= tokens

    def consume(self, tokens: float) -> None:
        """Deduct ``tokens``.  Must only be called when ``can_satisfy()`` is True."""
        self._refill()
        self._tokens = max(0.0, self._tokens - tokens)

    def time_until_available(self, tokens: float) -> float:
        """Seconds until ``tokens`` will be available at the current refill rate."""
        self._refill()
        deficit = tokens - self._tokens
        if deficit <= 0.0:
            return 0.0
        return deficit / self._refill_rate

    # ── Caller API ─────────────────────────────────────────────────────────────

    def credit(self, tokens: float) -> None:
        """
        Return unused token budget after an API call completes.

        Called by ``release()`` with the difference between estimated and actual
        output tokens.  The adjustment is clamped to ``[0, capacity]`` so a
        wildly inaccurate estimate cannot push the bucket above its capacity.
        """
        if tokens > 0.0:
            self._tokens = min(self._capacity, self._tokens + tokens)

    def recalibrate(self, new_capacity: int, new_refill_rate: float) -> None:
        """
        Adjust rate parameters when the active key count changes.

        On scale-down the token balance is clamped to the new capacity so a
        key dropout cannot release an over-budget burst before the refill rate
        catches up.  On scale-up existing tokens are preserved; the higher
        capacity simply becomes available for natural refill.
        """
        self._capacity = float(new_capacity)
        self._tokens = min(self._tokens, self._capacity)
        self._refill_rate = max(new_refill_rate, 1e-9)

    # ── Metrics ────────────────────────────────────────────────────────────────

    @property
    def utilisation(self) -> float:
        """Fraction of capacity consumed (0.0 = idle, 1.0 = fully saturated)."""
        self._refill()
        return 1.0 - (self._tokens / self._capacity)


# ── Manager ────────────────────────────────────────────────────────────────────

class GeminiRequestManager:
    """
    Singleton coordinator for all Gemini API access.

    Do not instantiate directly.  Acquire the singleton via::

        key_ids = [_key_id(k) for k in settings.gemini_api_key_pool]
        await GeminiRequestManager.initialize(redis=get_redis(), key_ids=key_ids)
        manager = get_request_manager()
    """

    _instance: ClassVar[GeminiRequestManager | None] = None

    def __init__(self) -> None:  # pragma: no cover
        raise RuntimeError(
            "Do not instantiate GeminiRequestManager directly. "
            "Call `await GeminiRequestManager.initialize(redis=...)` at startup, "
            "then use get_request_manager()."
        )

    # ── Startup ────────────────────────────────────────────────────────────────

    @classmethod
    async def initialize(cls, *, redis: Redis, key_ids: list[str]) -> None:
        """
        Create the singleton and start the background dispatcher.

        Pre-populates per-key circuit state from Redis so the first call after a
        quota-exhaustion restart fast-fails rather than wasting an API round-trip.
        Safe to call multiple times — subsequent calls are no-ops.

        Args:
            redis:   The application-level Redis client (must already be connected).
            key_ids: Stable short identifiers for each Gemini API key in the pool
                     (typically ``last8(api_key)``).  Determines how many per-key
                     circuit-breaker slots are allocated.  An empty list is valid
                     and means no quota tracking — used when no keys are configured.
        """
        if cls._instance is not None:
            return

        settings = get_settings()
        inst: GeminiRequestManager = object.__new__(cls)
        inst._settings = settings
        inst._redis = redis
        inst._key_ids: list[str] = list(key_ids)
        inst._quota_reset_buffer_secs: int = settings.GEMINI_QUOTA_RESET_BUFFER_MINUTES * 60

        # Monotonically increasing counter — FIFO ordering within each priority tier.
        inst._next_sequence: int = 0

        # Priority queue serviced by the single dispatcher coroutine.
        inst._queue: asyncio.PriorityQueue[_Permit] = asyncio.PriorityQueue()

        # Tracks per-priority depth for backpressure checks and metrics.
        # Incremented in acquire(), decremented when the dispatcher pops.
        inst._queue_depth: dict[int, int] = {int(p): 0 for p in Priority}

        # All permits currently in the queue (or held in dispatcher's pending slot).
        # Used by open_circuit() to cancel them immediately without waiting for
        # the dispatcher to reach each one.
        inst._active_permits: set[_Permit] = set()

        # ── Token buckets ───────────────────────────────────────────────────────
        # Base RPM values from config — preserved as the scaling baseline so that
        # _recalibrate_rpm_buckets() always computes from the user's intended total
        # budget rather than from a previously-scaled value.
        inst._base_generate_rpm: int = settings.GEMINI_GENERATE_RPM
        inst._base_embed_rpm: int = settings.GEMINI_EMBED_RPM

        # Generate: separate RPM and TPM buckets.
        # burst_cap = min(rpm, 10) — caps tokens that accumulate during a quiet
        # period, preventing a single burst from using a full minute of budget.
        gen_burst_cap = min(settings.GEMINI_GENERATE_RPM, 10)
        inst._generate_rpm = _TokenBucket(
            capacity=gen_burst_cap,
            refill_rate=settings.GEMINI_GENERATE_RPM / 60.0,
        )
        inst._generate_tpm = _TokenBucket(
            capacity=settings.GEMINI_GENERATE_TPM,
            refill_rate=settings.GEMINI_GENERATE_TPM / 60.0,
        )

        # Embed: RPM only (Gemini embed API is not TPM-metered).
        embed_burst_cap = min(settings.GEMINI_EMBED_RPM, 10)
        inst._embed_rpm = _TokenBucket(
            capacity=embed_burst_cap,
            refill_rate=settings.GEMINI_EMBED_RPM / 60.0,
        )

        # ── Per-key circuit breaker ─────────────────────────────────────────────
        # Structure: op → {key_id → is_open: bool}
        #   False → circuit closed (key is healthy and in rotation)
        #   True  → circuit open (key's daily quota is exhausted)
        #
        # Circuits are automatically reset at midnight Pacific Time +
        # GEMINI_QUOTA_RESET_BUFFER_MINUTES by the quota reset watcher task.
        # Redis keys carry a matching TTL so crash-restart recovery works even
        # when the watcher did not fire (expired key → loads as closed at startup).
        inst._circuit_state: dict[str, dict[str, bool]] = {
            op: {kid: False for kid in key_ids}
            for op in _ALL_OPERATIONS
        }

        # ── Budget guard state ──────────────────────────────────────────────────
        # Tracks how many successful generate calls have been made today (PT).
        # Persisted to Redis every N releases (adaptive — see below) so a mid-day
        # restart resumes from the correct count rather than resetting to zero.
        inst._generate_rpd_used: int = 0
        inst._generate_rpd_write_ctr: int = 0

        # Adaptive Redis persistence cadence: flush at most every 5 % of the
        # total daily budget so crash-recovery is accurate within that margin.
        #   Free tier (10 RPD/key × 5 keys =  50 total) → flush every  2 calls
        #   Paid Tier1 (1k RPD/key × 5 keys = 5000 total) → flush every 250 calls
        # Floor of 1 ensures at least every call is persisted on tiny quotas.
        _total_daily_budget = settings.GEMINI_GENERATE_RPD * max(len(key_ids), 1)
        inst._rpd_flush_threshold: int = max(1, _total_daily_budget // 20)

        # ── Startup budget coherence guard ─────────────────────────────────────
        # Detects operator misconfiguration early — before the first API call —
        # so quota exhaustion cannot silently disable explanations for days.
        _reserve = settings.GEMINI_HIGH_PRIORITY_RPD_RESERVE

        if _total_daily_budget <= _reserve:
            logger.critical(
                "request_manager: BUDGET MISCONFIGURATION — total daily budget "
                "(%d calls = GEMINI_GENERATE_RPD %d × %d keys) is ≤ the "
                "HIGH-priority reserve (%d).  MEDIUM/LOW/BACKGROUND callers will "
                "be permanently throttled from the first call.  Fix in backend/.env: "
                "raise GEMINI_GENERATE_RPD or lower GEMINI_HIGH_PRIORITY_RPD_RESERVE "
                "so that reserve < 20 %% of total budget.",
                _total_daily_budget, settings.GEMINI_GENERATE_RPD, max(len(key_ids), 1),
                _reserve,
            )
        elif _total_daily_budget < _reserve * 3:
            logger.warning(
                "request_manager: Budget guard over-reserved — GEMINI_HIGH_PRIORITY_"
                "RPD_RESERVE (%d) exceeds 33 %% of total daily budget (%d).  "
                "Background callers will be throttled very early.  Consider raising "
                "GEMINI_GENERATE_RPD or reducing the reserve in backend/.env.",
                _reserve, _total_daily_budget,
            )

        if settings.GEMINI_GENERATE_RPD == 1_500 and len(key_ids) > 0:
            logger.warning(
                "request_manager: GEMINI_GENERATE_RPD is at the unchecked default "
                "(1 500).  This is correct only for paid-tier deployments.  Free-tier "
                "AI Studio keys have ~10 RPD/key — leaving the default disables the "
                "budget guard entirely (configured budget: %d, likely real budget: %d).  "
                "Override GEMINI_GENERATE_RPD in backend/.env.",
                _total_daily_budget, 10 * max(len(key_ids), 1),
            )

        # Pre-load circuit state from Redis (runs before accepting any calls).
        await inst._load_circuit_state_from_redis()

        # Load today's RPD counter from Redis (after circuit state, so the
        # budget metric is accurate from the very first acquire() call).
        await inst._load_rpd_from_redis()

        # Recalibrate RPM buckets now that circuit state is known — if any keys
        # were already exhausted before this startup, the buckets must reflect
        # only the active key count rather than the full configured budget.
        for op in _ALL_OPERATIONS:
            inst._recalibrate_rpm_buckets(op)

        # ── Background tasks ────────────────────────────────────────────────────
        inst._dispatcher_task: asyncio.Task[None] = asyncio.create_task(
            inst._run_dispatcher(),
            name="gemini_request_manager_dispatcher",
        )
        # Watcher fires at midnight PT + buffer each day, automatically closing
        # all open per-key circuits and restoring exhausted keys to rotation.
        inst._quota_reset_watcher_task: asyncio.Task[None] = asyncio.create_task(
            inst._run_quota_reset_watcher(),
            name="gemini_quota_reset_watcher",
        )

        cls._instance = inst
        logger.info(
            "GeminiRequestManager ready — generate RPM=%d (burst=%d) TPM=%d "
            "embed RPM=%d (burst=%d) queue_depth_cap=%d permit_timeout=%.1fs "
            "key_pool=%d quota_reset_buffer=%dmin | budget: RPD/key=%d "
            "total=%d reserve=%d rpd_flush_every=%d",
            settings.GEMINI_GENERATE_RPM, gen_burst_cap,
            settings.GEMINI_GENERATE_TPM,
            settings.GEMINI_EMBED_RPM, embed_burst_cap,
            settings.GEMINI_MAX_QUEUE_DEPTH,
            settings.GEMINI_PERMIT_TIMEOUT,
            len(key_ids),
            settings.GEMINI_QUOTA_RESET_BUFFER_MINUTES,
            settings.GEMINI_GENERATE_RPD,
            _total_daily_budget,
            _reserve,
            inst._rpd_flush_threshold,
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    async def acquire(
        self,
        operation: str,
        priority: Priority = Priority.MEDIUM,
        estimated_tokens: int = 1_400,
    ) -> _Permit:
        """
        Request a permit to make a Gemini API call.

        Blocks asynchronously until:
        - The token budget allows the call (permit granted), or
        - ``GEMINI_PERMIT_TIMEOUT`` seconds elapse (``GeminiRateLimitError``).

        Must be paired with a ``release()`` call in a ``finally`` block.

        Args:
            operation:        ``Operation.GENERATE`` or ``Operation.EMBED``.
            priority:         Determines queue position relative to other waiters.
            estimated_tokens: Expected token cost of the call.  Used to deduct
                              TPM budget before the call so concurrent large
                              requests cannot collectively exceed the token cap.
                              For embed calls this is informational only (no TPM
                              bucket).  The actual output tokens are passed to
                              ``release()`` for overshoot correction.

        Returns:
            A ``_Permit`` — pass it to ``release()`` in a ``finally`` block.

        Raises:
            GeminiQuotaExhausted:  Daily quota circuit is open.
            GeminiRateLimitError:  Queue is full (``GEMINI_MAX_QUEUE_DEPTH``
                                   reached) or permit timeout elapsed.
        """
        # Fast-path: all keys exhausted — zero queue interaction.
        if self._all_keys_exhausted(operation):
            self._record_outcome(operation, priority, "quota")
            raise GeminiQuotaExhausted(
                f"Gemini {operation} quota circuit is open — all keys exhausted. "
                f"Circuits auto-reset at midnight Pacific Time + buffer. "
                f"For immediate recovery: DEL cortex:gemini:circuit:{operation}:* in Redis."
            )

        # Budget guard: protect the HIGH-priority reservation.
        #
        # Applies only to GENERATE calls at MEDIUM priority or lower.  HIGH and
        # CRITICAL are always admitted — the guard exists specifically to
        # preserve headroom for them.  Checked before the queue depth cap so
        # throttled callers never occupy a queue slot.
        if operation == Operation.GENERATE and self._is_generate_budget_throttled(priority):
            remaining = self._generate_rpd_budget_remaining()
            self._record_outcome(operation, priority, "budget")
            raise GeminiBudgetThrottled(
                f"Gemini daily generate budget is within the HIGH-priority reservation "
                f"({self._settings.GEMINI_HIGH_PRIORITY_RPD_RESERVE} calls reserved, "
                f"~{remaining} estimated remaining).  "
                f"MEDIUM / LOW / BACKGROUND calls are throttled until midnight Pacific "
                f"Time.  HIGH and CRITICAL callers are unaffected.  "
                f"Adjust GEMINI_HIGH_PRIORITY_RPD_RESERVE or GEMINI_GENERATE_RPD in "
                f".env to tune the guard threshold."
            )

        # Backpressure: queue depth cap.
        total_depth = sum(self._queue_depth.values())
        if total_depth >= self._settings.GEMINI_MAX_QUEUE_DEPTH:
            self._record_outcome(operation, priority, "rate")
            raise GeminiRateLimitError(
                f"Gemini request queue is at capacity "
                f"({total_depth}/{self._settings.GEMINI_MAX_QUEUE_DEPTH}). "
                f"Degrade gracefully — queue will drain as permits are granted."
            )

        # Build permit and enqueue.
        seq = self._next_sequence
        self._next_sequence += 1
        permit = _Permit(
            priority=int(priority),
            sequence=seq,
            operation=operation,
            estimated_tokens=estimated_tokens,
        )

        self._queue_depth[int(priority)] += 1
        self._active_permits.add(permit)

        # Metrics — import lazily to avoid circular imports at module load.
        from app.core.metrics import gemini_queue_depth as _qd
        _qd.labels(priority=priority.label).inc()

        await self._queue.put(permit)

        # Block until the dispatcher grants the permit (or timeout fires).
        try:
            await asyncio.wait_for(
                permit.ready.wait(),
                timeout=self._settings.GEMINI_PERMIT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            permit.cancelled = True
            # Keep in _active_permits — the dispatcher will discard it on pop.
            self._record_outcome(operation, priority, "timeout")
            raise GeminiRateLimitError(
                f"Timed out waiting for a Gemini {operation} permit after "
                f"{self._settings.GEMINI_PERMIT_TIMEOUT:.0f}s. "
                f"The system is under heavy Gemini load; try again shortly."
            )

        # Permit was set by the dispatcher.  If the circuit opened while we
        # were queued, the dispatcher marked us cancelled before setting ready.
        if permit.cancelled:
            self._record_outcome(operation, priority, "quota")
            raise GeminiQuotaExhausted(
                f"Gemini {operation} quota circuit opened while permit was queued."
            )

        return permit

    def release(
        self,
        permit: _Permit,
        actual_tokens: int = 0,
        *,
        outcome: str = "success",
    ) -> None:
        """
        Return a permit after the API call completes.

        **Must be called in a ``finally`` block** after every successful
        ``acquire()`` — even when the API call raises an exception.

        Args:
            permit:        The ``_Permit`` returned by ``acquire()``.
            actual_tokens: Actual output/completion tokens consumed.  Used to
                           credit the unused portion of the TPM budget back to
                           the bucket (overshoot correction).  Pass 0 when
                           unknown; the budget will self-correct within seconds.
            outcome:       ``"success"`` or ``"error"`` — recorded in
                           ``gemini_requests_total`` for Grafana / alerting.
        """
        if permit.cancelled:
            # Already accounted for in acquire() (quota/timeout path).
            return

        # Credit back unused TPM budget for generate calls.
        if permit.operation == Operation.GENERATE and actual_tokens > 0:
            unused = max(0, permit.estimated_tokens - actual_tokens)
            if unused > 0:
                self._generate_tpm.credit(float(unused))

        # Budget guard: track actual daily generate usage.
        #
        # Incremented for every non-cancelled GENERATE release — success or
        # error — because the API counts the request regardless of outcome.
        # Persisted to Redis every _rpd_flush_threshold calls (adaptive: ~5% of
        # daily budget) so a mid-day crash restart resumes within that margin.
        if permit.operation == Operation.GENERATE:
            self._generate_rpd_used += 1
            self._generate_rpd_write_ctr += 1
            if self._generate_rpd_write_ctr >= self._rpd_flush_threshold:
                self._generate_rpd_write_ctr = 0
                asyncio.create_task(
                    self._write_rpd_to_redis(),
                    name="gemini_rpd_write",
                )
            self._update_rpd_metric()

        self._record_outcome(permit.operation, Priority(permit.priority), outcome)
        self._update_utilisation_metrics()

    def open_circuit(self, operation: str, *, key_id: str) -> None:
        """
        Mark one specific key's quota circuit as open for ``operation``.

        Called by ``llm_client._mark_key_exhausted()`` when a daily-quota 429 is
        detected for a specific key.  If this is the last available key (all keys
        are now exhausted), immediately cancels all queued permits so callers
        unblock with ``GeminiQuotaExhausted`` rather than waiting for the
        dispatcher to reach them.

        Idempotent — calling again for an already-open key is a no-op.

        Persists the circuit state to Redis asynchronously (fire-and-forget) so
        that the next app restart can skip the first post-quota call.

        Args:
            operation: ``Operation.GENERATE`` or ``Operation.EMBED``.
            key_id:    The short identifier of the exhausted key (``last8(api_key)``).
        """
        op_state = self._circuit_state.get(operation)
        if op_state is None or key_id not in op_state:
            logger.warning(
                "request_manager: open_circuit called for unknown key_id=%s op=%s "
                "— ignored.  Was the manager initialized with this key?",
                key_id, operation,
            )
            return

        if op_state[key_id]:
            return  # Already open — strict idempotent no-op.

        op_state[key_id] = True
        self._recalibrate_rpm_buckets(operation)
        self._emit_circuit_metric(operation, open_=self._any_circuit_open(operation))
        self._emit_all_exhausted_metric(operation)

        # Persist to Redis with a TTL matching the next quota reset + buffer.
        # This acts as a crash-restart safety net: if the watcher task never fired
        # (e.g. the process died), the expired Redis key loads as closed on startup.
        asyncio.create_task(
            self._write_circuit_to_redis(operation, key_id),
            name=f"gemini_circuit_write_{operation}_{key_id}",
        )

        all_exhausted = self._all_keys_exhausted(operation)
        if all_exhausted:
            cancelled_count = self._cancel_queued_permits(operation)
            logger.error(
                "request_manager: ALL Gemini %s keys are now quota-exhausted. "
                "Last key=%s. %d queued permits cancelled. "
                "Auto-reset fires at midnight PT + buffer. "
                "For immediate recovery: DEL cortex:gemini:circuit:%s:%s in Redis.",
                operation, key_id, cancelled_count, operation, key_id,
            )
        else:
            logger.error(
                "request_manager: Gemini %s circuit OPENED for key=%s — "
                "daily quota exhausted on this key. Remaining keys still active. "
                "Auto-reset fires at midnight PT + buffer. "
                "For immediate recovery: DEL cortex:gemini:circuit:%s:%s in Redis.",
                operation, key_id, operation, key_id,
            )

    def circuit_open(self, operation: str) -> bool:
        """Return ``True`` if ALL registered keys' quota circuits are open for ``operation``."""
        return self._all_keys_exhausted(operation)

    def key_circuit_open(self, operation: str, key_id: str) -> bool:
        """Return ``True`` if this specific key's quota circuit is open for ``operation``."""
        return self._circuit_state.get(operation, {}).get(key_id, False)

    async def aclose(self) -> None:
        """
        Cancel the dispatcher and drain the permit queue at shutdown.

        Cancels all pending permits so waiting callers receive
        ``GeminiRateLimitError`` rather than hanging indefinitely.  Call once
        from the FastAPI lifespan shutdown handler.
        """
        for task_attr in ("_dispatcher_task", "_quota_reset_watcher_task"):
            task = getattr(self, task_attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Cancel any permits still tracked as active.
        for permit in list(self._active_permits):
            if not permit.cancelled:
                permit.cancelled = True
                permit.ready.set()
        self._active_permits.clear()

        logger.info("request_manager: GeminiRequestManager shutdown complete.")

    # ── Private — circuit breaker ──────────────────────────────────────────────

    async def _load_circuit_state_from_redis(self) -> None:
        """
        Read per-key, per-operation circuit keys from Redis and populate in-process state.

        Called once during ``initialize()`` so the manager is circuit-aware from
        the very first ``acquire()`` call, even after a crash-restart mid quota-
        exhaustion window.  Keys that are absent or have expired are treated as
        closed (fail-open safety default).
        """
        from app.core.metrics import (
            gemini_circuit_open as _copen,
            gemini_all_keys_exhausted as _all_ex,
        )
        for op in _ALL_OPERATIONS:
            for kid in self._key_ids:
                redis_key = _circuit_redis_key(op, kid)
                try:
                    raw = await self._redis.get(redis_key)
                    is_open = bool(raw)
                    self._circuit_state[op][kid] = is_open
                    # Reflect any-open and all-exhausted metrics after each load.
                    _copen.labels(op=op).set(1 if self._any_circuit_open(op) else 0)
                    _all_ex.labels(op=op).set(1 if self._all_keys_exhausted(op) else 0)
                    if is_open:
                        logger.warning(
                            "request_manager: Gemini %s circuit pre-loaded OPEN for "
                            "key=%s — key was quota-exhausted before last restart. "
                            "Auto-reset watcher will fire at midnight PT + buffer. "
                            "For immediate recovery: DEL %s in Redis.",
                            op, kid, redis_key,
                        )
                except Exception as exc:
                    # Redis unavailable during startup — fail open (assume closed).
                    logger.warning(
                        "request_manager: Redis circuit-state read failed for "
                        "op=%s key=%s (%s) — assuming closed.",
                        op, kid, exc,
                    )
                    self._circuit_state[op][kid] = False

    def _all_keys_exhausted(self, operation: str) -> bool:
        """True only when every registered key's circuit is open for ``operation``.

        Returns ``False`` for an empty key pool (vacuously, all-or-nothing does
        not apply when there are no keys to exhaust).
        """
        states = self._circuit_state.get(operation, {})
        return bool(states) and all(states.values())

    def _any_circuit_open(self, operation: str) -> bool:
        """True when at least one key's circuit is open for ``operation``."""
        return any(self._circuit_state.get(operation, {}).values())

    async def _write_circuit_to_redis(self, operation: str, key_id: str) -> None:
        """Persist a single key's circuit open state to Redis.  Best-effort; called as a Task.

        The key is written with TTL = seconds until the next midnight Pacific Time
        + ``GEMINI_QUOTA_RESET_BUFFER_MINUTES``.  This ensures that a crash-restart
        while the watcher task would have been sleeping still recovers correctly:
        an expired key loads as ``False`` (closed) at startup.
        """
        redis_key = _circuit_redis_key(operation, key_id)
        ttl_secs = max(1, int(_seconds_until_quota_reset(self._quota_reset_buffer_secs)))
        try:
            await self._redis.set(redis_key, "1", ex=ttl_secs)
            logger.info(
                "request_manager: Redis circuit key %s written (TTL=%ds, "
                "auto-expires at midnight PT + buffer). "
                "For immediate recovery: DEL %s in Redis.",
                redis_key, ttl_secs, redis_key,
            )
        except Exception as exc:
            logger.error(
                "request_manager: Failed to write circuit key %s to Redis: %s "
                "(in-process state is authoritative; auto-reset watcher still active).",
                redis_key, exc,
            )

    # ── Private — budget guard ─────────────────────────────────────────────────

    async def _load_rpd_from_redis(self) -> None:
        """Load today's (PT) generate RPD counter from Redis on startup.

        Called once during ``initialize()`` so that a mid-day restart resumes
        from the correct usage count rather than resetting to zero and
        silently bypassing the budget guard.  Missing key (new quota day or
        first run) → start from 0.  Redis failure → fail open (start from 0
        and log a warning; the guard will be conservative from the restart).
        """
        redis_key = _rpd_redis_key()
        try:
            raw = await self._redis.get(redis_key)
            self._generate_rpd_used = int(raw) if raw else 0
            logger.info(
                "request_manager: Budget guard RPD counter loaded — "
                "generate_used=%d (key=%s)",
                self._generate_rpd_used, redis_key,
            )
        except Exception as exc:
            logger.warning(
                "request_manager: Redis RPD counter read failed (%s) — "
                "starting from 0.  Budget guard is conservative from this restart.",
                exc,
            )
            self._generate_rpd_used = 0
        self._update_rpd_metric()

    async def _write_rpd_to_redis(self) -> None:
        """Persist the current generate RPD counter to Redis.

        Best-effort, fire-and-forget.  Uses a 25-hour TTL (90 000 s) so the
        key expires naturally one hour after the next midnight PT reset, acting
        as a crash-recovery safety net without requiring an explicit delete.
        """
        redis_key = _rpd_redis_key()
        try:
            await self._redis.set(redis_key, str(self._generate_rpd_used), ex=90_000)
        except Exception as exc:
            logger.debug(
                "request_manager: RPD counter Redis write failed (non-fatal): %s", exc
            )

    def _generate_rpd_budget_remaining(self) -> int:
        """Estimated remaining daily generate requests before the HIGH-priority reservation.

        Total budget = ``GEMINI_GENERATE_RPD × total_key_count``.  Uses total
        (not active) key count intentionally: the guard triggers slightly early
        as keys circuit-open, which is the correct conservative behaviour for a
        soft protection layer sitting above the hard circuit breaker.
        """
        total = self._settings.GEMINI_GENERATE_RPD * max(len(self._key_ids), 1)
        return max(0, total - self._generate_rpd_used)

    def _is_generate_budget_throttled(self, priority: Priority) -> bool:
        """True when the daily budget is within the HIGH-priority reservation band.

        Only MEDIUM, LOW, and BACKGROUND calls are ever throttled.  HIGH and
        CRITICAL are always admitted — the reservation exists to guarantee
        headroom for them.
        """
        if priority <= Priority.HIGH:
            return False
        return self._generate_rpd_budget_remaining() < self._settings.GEMINI_HIGH_PRIORITY_RPD_RESERVE

    def _update_rpd_metric(self) -> None:
        """Push current budget-remaining estimate to the Prometheus gauge."""
        from app.core.metrics import gemini_rpd_budget_remaining as _rpd_gauge
        _rpd_gauge.set(self._generate_rpd_budget_remaining())

    # ── Private — circuit breaker (cancel / emit) ──────────────────────────────

    def _cancel_queued_permits(self, operation: str) -> int:
        """
        Immediately cancel all queued permits for ``operation``.

        Marks each matching permit as cancelled and sets its ``ready`` event
        so blocked callers wake up and raise ``GeminiQuotaExhausted`` without
        waiting for the dispatcher to reach them.

        Returns the number of permits cancelled.
        """
        count = 0
        for permit in list(self._active_permits):
            if permit.operation == operation and not permit.cancelled:
                permit.cancelled = True
                permit.ready.set()
                count += 1
        return count

    def _emit_circuit_metric(self, operation: str, *, open_: bool) -> None:
        from app.core.metrics import gemini_circuit_open as _copen
        _copen.labels(op=operation).set(1 if open_ else 0)

    def _emit_all_exhausted_metric(self, operation: str) -> None:
        """Update the gemini_all_keys_exhausted gauge for ``operation``.

        Set to 1 only when EVERY registered key is quota-exhausted (i.e. the
        circuit fast-path activates and explanations stop).  This is the CRITICAL
        alert signal — distinct from gemini_circuit_open which fires when ANY key
        is exhausted.
        """
        from app.core.metrics import gemini_all_keys_exhausted as _all_ex
        _all_ex.labels(op=operation).set(1 if self._all_keys_exhausted(operation) else 0)

    # ── Private — quota reset watcher ─────────────────────────────────────────

    async def _run_quota_reset_watcher(self) -> None:
        """Outer loop for the quota reset watcher — handles unexpected crashes.

        Re-schedules itself after a brief delay if the inner loop raises an
        uncaught exception (should never happen in practice; guards against
        transient Redis errors or programming mistakes).
        """
        while True:
            try:
                await self._quota_reset_watcher_loop()
            except asyncio.CancelledError:
                raise  # Propagate shutdown cancellation.
            except Exception as exc:
                logger.critical(
                    "request_manager: Quota reset watcher crashed unexpectedly: %s. "
                    "Restarting in 60 s.",
                    exc, exc_info=True,
                )
                await asyncio.sleep(60.0)

    async def _quota_reset_watcher_loop(self) -> None:
        """Sleep until midnight Pacific Time + buffer, reset all open circuits, repeat.

        Gemini's RPD counter resets at midnight PT.  Community reports show a
        0–15 minute propagation lag before the counter actually flips, so we add
        ``GEMINI_QUOTA_RESET_BUFFER_MINUTES`` (default 15) before re-enabling keys.

        After each reset, the next sleep window is recalculated so DST transitions
        and long-running processes are handled correctly.
        """
        while True:
            delay = _seconds_until_quota_reset(self._quota_reset_buffer_secs)
            buffer_min = self._quota_reset_buffer_secs // 60
            logger.info(
                "request_manager: Quota reset watcher sleeping %.0f s "
                "(fires at midnight PT + %d min buffer).",
                delay, buffer_min,
            )
            await asyncio.sleep(delay)
            await self._reset_all_open_circuits()

    async def _reset_all_open_circuits(self) -> None:
        """Clear every open per-key circuit from both in-process state and Redis.

        Called by the quota reset watcher at midnight PT + buffer.  After this
        method returns, all keys are restored to active rotation and
        ``_circuit_state`` reflects no open circuits for any operation.

        Also emits updated Prometheus metrics so Grafana dashboards reflect the
        reset without requiring a scrape cycle.
        """
        reset_count = 0
        for op in _ALL_OPERATIONS:
            for kid in self._key_ids:
                if not self._circuit_state.get(op, {}).get(kid, False):
                    continue
                self._circuit_state[op][kid] = False
                reset_count += 1
                redis_key = _circuit_redis_key(op, kid)
                try:
                    await self._redis.delete(redis_key)
                except Exception as exc:
                    logger.warning(
                        "request_manager: Failed to delete circuit key %s "
                        "during quota reset: %s (in-process state already cleared).",
                        redis_key, exc,
                    )
            self._emit_circuit_metric(op, open_=self._any_circuit_open(op))
            self._emit_all_exhausted_metric(op)
            self._recalibrate_rpm_buckets(op)

        # Reset the daily generate RPD counter.  The quota watcher fires at
        # midnight PT + buffer, which is exactly the Gemini quota boundary.
        # The old day's Redis key is left to expire naturally (25-hour TTL);
        # the next release() write will create a fresh key for the new day.
        prev_rpd = self._generate_rpd_used
        self._generate_rpd_used = 0
        self._generate_rpd_write_ctr = 0
        self._update_rpd_metric()

        # Signal the explanation worker to auto-requeue any DLQ entries from the
        # previous quota day.  Best-effort — if Redis pub/sub is unavailable, the
        # worker's boot-time DLQ scan on the next restart handles recovery instead.
        try:
            from app.core.redis import RedisChannels as _RC
            payload = json.dumps({
                "reset_at": datetime.now(timezone.utc).isoformat(),
                "keys_reset": reset_count,
            })
            await self._redis.publish(_RC.GEMINI_QUOTA_RESET, payload)
            logger.debug(
                "request_manager: Published quota reset signal to %s "
                "(keys_reset=%d).",
                _RC.GEMINI_QUOTA_RESET, reset_count,
            )
        except Exception as exc:
            logger.warning(
                "request_manager: Failed to publish quota reset signal: %s "
                "— DLQ recovery will occur on next explanation worker restart.",
                exc,
            )

        if reset_count > 0:
            logger.info(
                "request_manager: Gemini quota reset — %d circuit(s) automatically "
                "cleared across %d key(s). All keys restored to active rotation. "
                "Daily generate RPD counter reset (was %d).",
                reset_count, len(self._key_ids), prev_rpd,
            )
        else:
            logger.debug(
                "request_manager: Gemini quota reset fired — no open circuits to clear. "
                "Daily generate RPD counter reset (was %d).",
                prev_rpd,
            )

    # ── Private — token buckets ────────────────────────────────────────────────

    def _recalibrate_rpm_buckets(self, operation: str) -> None:
        """
        Scale the RPM token bucket to reflect the current number of active keys.

        Each Gemini API key has an independent per-minute rate limit set by
        Google (e.g. 10 RPM on the free tier).  The process-level token bucket
        enforces the *total* budget across all keys, assuming round-robin
        distribution.  When a key's daily-quota circuit opens, the bucket must
        be scaled down proportionally so the remaining keys are never asked to
        carry more than their individual per-key limits.

        Formula:  effective_rpm = round(base_rpm * active_keys / total_keys)

        Example (GEMINI_GENERATE_RPM=30, 3 keys, free tier = 10 RPM/key):
          All 3 active → effective_rpm = 30  (10 RPM/key  ✓)
          1 key open   → effective_rpm = 20  (10 RPM/key  ✓)
          2 keys open  → effective_rpm = 10  (10 RPM/key  ✓)

        Called whenever circuit state changes (open_circuit, _reset_all_open_circuits)
        and once at startup after loading Redis-persisted state.

        Note: the TPM bucket is not scaled — token-per-minute budget is an
        aggregate pipeline limit, not a per-key constraint, and remains at the
        configured value regardless of active key count.
        """
        total_keys = len(self._key_ids)
        if total_keys == 0:
            return

        active_keys = sum(
            1 for kid in self._key_ids
            if not self._circuit_state.get(operation, {}).get(kid, False)
        )

        from app.core.metrics import gemini_effective_rpm as _eff_rpm

        if operation == Operation.GENERATE:
            base_rpm = self._base_generate_rpm
            effective_rpm = max(1, round(base_rpm * active_keys / total_keys))
            burst_cap = min(effective_rpm, 10)
            self._generate_rpm.recalibrate(burst_cap, effective_rpm / 60.0)
            _eff_rpm.labels(op=operation).set(effective_rpm)
            logger.info(
                "request_manager: generate RPM recalibrated → %d RPM "
                "(%d/%d keys active, base=%d, burst_cap=%d)",
                effective_rpm, active_keys, total_keys, base_rpm, burst_cap,
            )

        elif operation == Operation.EMBED:
            base_rpm = self._base_embed_rpm
            effective_rpm = max(1, round(base_rpm * active_keys / total_keys))
            burst_cap = min(effective_rpm, 10)
            self._embed_rpm.recalibrate(burst_cap, effective_rpm / 60.0)
            _eff_rpm.labels(op=operation).set(effective_rpm)
            logger.info(
                "request_manager: embed RPM recalibrated → %d RPM "
                "(%d/%d keys active, base=%d, burst_cap=%d)",
                effective_rpm, active_keys, total_keys, base_rpm, burst_cap,
            )

    def _can_satisfy(self, permit: _Permit) -> bool:
        """True if the token buckets have sufficient budget for ``permit``."""
        if permit.operation == Operation.GENERATE:
            return (
                self._generate_rpm.can_satisfy(1.0)
                and self._generate_tpm.can_satisfy(float(permit.estimated_tokens))
            )
        return self._embed_rpm.can_satisfy(1.0)

    def _consume(self, permit: _Permit) -> None:
        """Deduct token budget for ``permit``.  Call only after ``_can_satisfy``."""
        if permit.operation == Operation.GENERATE:
            self._generate_rpm.consume(1.0)
            self._generate_tpm.consume(float(permit.estimated_tokens))
        else:
            self._embed_rpm.consume(1.0)

    def _time_until_can_satisfy(self, permit: _Permit) -> float:
        """
        Seconds until the token buckets can satisfy ``permit``.

        Returns the max of all applicable bucket wait times so the dispatcher
        sleeps exactly as long as needed rather than spinning.
        """
        if permit.operation == Operation.GENERATE:
            rpm_wait = self._generate_rpm.time_until_available(1.0)
            tpm_wait = self._generate_tpm.time_until_available(
                float(permit.estimated_tokens)
            )
            return max(rpm_wait, tpm_wait)
        return self._embed_rpm.time_until_available(1.0)

    # ── Private — metrics helpers ──────────────────────────────────────────────

    @staticmethod
    def _record_outcome(operation: str | None, priority: Priority, status: str) -> None:
        """Increment ``gemini_requests_total`` for the given outcome."""
        from app.core.metrics import gemini_requests_total as _rt
        op_label = operation if operation is not None else "unknown"
        _rt.labels(op=op_label, priority=priority.label, status=status).inc()

    def _update_utilisation_metrics(self) -> None:
        """Push current token-bucket utilisation to Prometheus gauges."""
        from app.core.metrics import (
            gemini_rpm_utilisation as _rpm,
            gemini_tpm_utilisation as _tpm,
        )
        _rpm.labels(op=Operation.GENERATE).set(self._generate_rpm.utilisation)
        _rpm.labels(op=Operation.EMBED).set(self._embed_rpm.utilisation)
        _tpm.set(self._generate_tpm.utilisation)

    # ── Private — dispatcher ───────────────────────────────────────────────────

    async def _run_dispatcher(self) -> None:
        """
        Outer dispatcher loop with automatic restart on unexpected errors.

        Cancellation (``asyncio.CancelledError``) propagates immediately so
        ``aclose()`` can cleanly terminate the task.  Any other exception is
        logged at CRITICAL level and the inner loop restarts after a 1-second
        pause — the manager continues serving permits rather than dying silently.
        """
        while True:
            try:
                await self._dispatch_loop()
            except asyncio.CancelledError:
                raise  # Propagate — initiated by aclose()
            except Exception as exc:
                logger.critical(
                    "request_manager: Dispatcher crashed unexpectedly (%s: %s). "
                    "Restarting in 1s — existing permits will be served.",
                    type(exc).__name__, exc,
                    exc_info=True,
                )
                await asyncio.sleep(1.0)

    async def _dispatch_loop(self) -> None:
        """
        Core dispatch logic.

        Maintains a ``pending`` slot for the highest-priority permit currently
        being evaluated.  This avoids re-queueing and preserves strict priority
        ordering: once a permit is popped as the current candidate, it is
        satisfied before any lower-priority item that arrives later.

        The sleep duration between token-bucket retries is computed from the
        bucket's own refill rate so the loop wakes precisely when budget is
        available — no unnecessary spinning, no excessive sleeping.
        """
        from app.core.metrics import (
            gemini_queue_depth as _qd,
            gemini_permit_wait_seconds as _pws,
        )

        pending: _Permit | None = None

        while True:
            # ── Step 1: Get the next permit if not already holding one ─────────
            if pending is None:
                try:
                    pending = await asyncio.wait_for(
                        self._queue.get(), timeout=1.0
                    )
                    # Accounting: queue depth and active tracking.
                    self._queue_depth[pending.priority] = max(
                        0, self._queue_depth[pending.priority] - 1
                    )
                    _qd.labels(priority=Priority(pending.priority).label).dec()
                    self._active_permits.discard(pending)
                except asyncio.TimeoutError:
                    # No permits waiting — refresh utilisation metrics and loop.
                    self._update_utilisation_metrics()
                    continue

            permit = pending

            # ── Step 2: Skip permits cancelled by timeout or open_circuit() ────
            if permit.cancelled:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass
                pending = None
                continue

            # ── Step 3: Fast-fail if ALL keys exhausted while queued ──────────
            if self._all_keys_exhausted(permit.operation):
                permit.cancelled = True
                permit.ready.set()
                try:
                    self._queue.task_done()
                except ValueError:
                    pass
                self._record_outcome(
                    permit.operation, Priority(permit.priority), "quota"
                )
                pending = None
                continue

            # ── Step 4: Check token budget ────────────────────────────────────
            if self._can_satisfy(permit):
                self._consume(permit)
                # Record permit wait time (enqueued_at → now).
                wait_s = time.monotonic() - permit.enqueued_at
                _pws.labels(
                    op=permit.operation,
                    priority=Priority(permit.priority).label,
                ).observe(wait_s)
                permit.ready.set()
                try:
                    self._queue.task_done()
                except ValueError:
                    pass
                pending = None
                self._update_utilisation_metrics()
            else:
                # ── Step 5: Insufficient tokens — sleep until budget refills ──
                # Cap at 1.0s so we notice circuit changes and new higher-
                # priority permits promptly even during extended token scarcity.
                delay = min(self._time_until_can_satisfy(permit), 1.0)
                await asyncio.sleep(delay)
                # pending stays set — retry satisfying the same permit next iteration.


# ── Singleton accessor ─────────────────────────────────────────────────────────

def get_request_manager() -> GeminiRequestManager:
    """
    Return the ``GeminiRequestManager`` singleton.

    Raises ``RuntimeError`` if ``initialize()`` has not been called — the
    manager must be created in the FastAPI lifespan before any request is served.
    """
    inst = GeminiRequestManager._instance
    if inst is None:
        raise RuntimeError(
            "GeminiRequestManager has not been initialized. "
            "Call `await GeminiRequestManager.initialize(redis=get_redis(), key_ids=...)` "
            "in the FastAPI lifespan before serving requests."
        )
    return inst
