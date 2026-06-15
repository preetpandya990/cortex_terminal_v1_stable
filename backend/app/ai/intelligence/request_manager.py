"""
Gemini Request Manager
======================
Central coordinator for all Gemini API access within the Cortex AI layer.

Problem
-------
Six independent callers (explanation worker, news forecaster, NLP sentiment,
event classifier, RAG embedder, health check) share a single Gemini API key
with no coordination.  Under market-hours load they burst simultaneously, hit
the per-minute limit, and their retries starve each other — every retry storm
can delay the next legitimate call by 30–60 seconds.  A process restart loses
circuit state, wasting the first post-restart call on a guaranteed 429.

Solution
--------
Three cooperating mechanisms:

1. **Priority queue** — Five tiers (CRITICAL → BACKGROUND) served in strict
   order with FIFO within each tier.  User-facing explanations are always
   ahead of background RAG embeddings, regardless of arrival order.

2. **Token buckets** — Two independent continuous-refill buckets:
   ``generate`` (RPM + TPM) and ``embed`` (RPM only).  Callers suspend
   asynchronously via ``asyncio.Event``; there is no busy-polling or sleep
   loop on the caller side.

3. **Redis-backed circuit breaker** — When a daily-quota 429 is detected,
   the circuit is written to Redis with a TTL of "seconds until midnight PT".
   On the next app restart ``initialize()`` pre-populates the in-process state
   from Redis so the first post-restart call is a sub-millisecond fast-fail
   rather than a wasted API round-trip.

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

Thread safety
-------------
Pure asyncio — all shared state is mutated only from the event loop thread.
Never call any method from a thread pool (``run_in_executor``) without adding
appropriate synchronization.

Usage
-----
::

    # FastAPI lifespan startup (after init_redis):
    from app.ai.intelligence.request_manager import GeminiRequestManager
    await GeminiRequestManager.initialize(redis=get_redis())

    # All Gemini call sites:
    from app.ai.intelligence.request_manager import (
        get_request_manager, Priority, Operation,
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
    HIGH       — user-facing, latency-sensitive (explanation worker)
    MEDIUM     — live signal pipeline (forecaster, sentiment, event_classifier)
    LOW        — background supplemental data (instrument context)
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

# Redis key pattern for per-operation circuit state.
# TTL is set to the seconds remaining until midnight Pacific Time (daily reset).
_CIRCUIT_REDIS_KEYS: dict[str, str] = {
    Operation.GENERATE: "cortex:gemini:circuit:generate",
    Operation.EMBED:    "cortex:gemini:circuit:embed",
}

# All valid operation strings (used for iteration in initialize/aclose).
_ALL_OPERATIONS: tuple[str, ...] = (Operation.GENERATE, Operation.EMBED)


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

        await GeminiRequestManager.initialize(redis=get_redis())
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
    async def initialize(cls, *, redis: Redis) -> None:
        """
        Create the singleton and start the background dispatcher.

        Pre-populates in-process circuit state from Redis so the first call
        after a quota-exhaustion restart fast-fails rather than wasting an API
        round-trip.  Safe to call multiple times — subsequent calls are no-ops.

        Args:
            redis: The application-level Redis client (must already be connected).
        """
        if cls._instance is not None:
            return

        settings = get_settings()
        inst: GeminiRequestManager = object.__new__(cls)
        inst._settings = settings
        inst._redis = redis

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

        # ── Circuit breaker ─────────────────────────────────────────────────────
        # None  → confirmed closed (no trip, or past reset time)
        # datetime → open until that UTC timestamp
        # Key absent → unknown — will be resolved on first access (only happens
        #               before initialize() populates state, which shouldn't occur
        #               in practice; treat as closed as a fail-open safety default)
        inst._circuit_state: dict[str, datetime | None] = {
            op: None for op in _ALL_OPERATIONS
        }

        # Pre-load circuit state from Redis (runs before accepting any calls).
        await inst._load_circuit_state_from_redis()

        # ── Dispatcher task ─────────────────────────────────────────────────────
        inst._dispatcher_task: asyncio.Task[None] = asyncio.create_task(
            inst._run_dispatcher(),
            name="gemini_request_manager_dispatcher",
        )

        cls._instance = inst
        logger.info(
            "GeminiRequestManager ready — generate RPM=%d (burst=%d) TPM=%d "
            "embed RPM=%d (burst=%d) queue_depth_cap=%d permit_timeout=%.1fs",
            settings.GEMINI_GENERATE_RPM, gen_burst_cap,
            settings.GEMINI_GENERATE_TPM,
            settings.GEMINI_EMBED_RPM, embed_burst_cap,
            settings.GEMINI_MAX_QUEUE_DEPTH,
            settings.GEMINI_PERMIT_TIMEOUT,
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
        # Fast-path: circuit already open — zero queue interaction.
        if self._is_circuit_open(operation):
            self._record_outcome(operation, priority, "quota")
            raise GeminiQuotaExhausted(
                f"Gemini {operation} quota circuit is open — daily limit exhausted, "
                f"resets at midnight Pacific Time."
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

        self._record_outcome(permit.operation, Priority(permit.priority), outcome)
        self._update_utilisation_metrics()

    def open_circuit(self, operation: str) -> None:
        """
        Open the quota circuit for ``operation``.

        Called by ``llm_client._open_quota_circuit()`` (Phase 2) when a
        daily-quota 429 is detected.  Immediately cancels all queued permits
        for this operation so callers unblock with ``GeminiQuotaExhausted``
        rather than waiting for the dispatcher to reach them.

        Idempotent — a second call before the reset is a no-op unless the new
        reset time extends the current one (defensive; normally both are the
        same midnight).

        Writes the circuit state to Redis asynchronously (fire-and-forget) so
        that the next app restart can skip the first post-quota call.

        Args:
            operation: ``Operation.GENERATE`` or ``Operation.EMBED``.
        """
        reset_at = _quota_reset_at()
        current = self._circuit_state.get(operation)
        if current is not None and current >= reset_at:
            return  # Already open at least as long — no-op.

        self._circuit_state[operation] = reset_at
        self._emit_circuit_metric(operation, open_=True)

        # Cancel all queued permits for this operation immediately.
        cancelled_count = self._cancel_queued_permits(operation)

        # Persist to Redis (best-effort; in-process state is the authority).
        asyncio.create_task(
            self._write_circuit_to_redis(operation, reset_at),
            name=f"gemini_circuit_write_{operation}",
        )

        logger.error(
            "request_manager: Gemini %s circuit OPENED — daily quota exhausted, "
            "resets at %s UTC. %d queued permits cancelled immediately.",
            operation,
            reset_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            cancelled_count,
        )

    def circuit_open(self, operation: str) -> bool:
        """Return ``True`` if the quota circuit is currently open for ``operation``."""
        return self._is_circuit_open(operation)

    async def aclose(self) -> None:
        """
        Cancel the dispatcher and drain the permit queue at shutdown.

        Cancels all pending permits so waiting callers receive
        ``GeminiRateLimitError`` rather than hanging indefinitely.  Call once
        from the FastAPI lifespan shutdown handler.
        """
        task = getattr(self, "_dispatcher_task", None)
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
        Read both operation circuit keys from Redis and populate in-process state.

        Called once during ``initialize()`` so the manager is circuit-aware
        from the very first ``acquire()`` call, even after a crash-restart mid
        quota-exhaustion window.
        """
        from app.core.metrics import gemini_circuit_open as _copen
        for op in _ALL_OPERATIONS:
            key = _CIRCUIT_REDIS_KEYS[op]
            try:
                raw = await self._redis.get(key)
                if raw:
                    ttl = await self._redis.ttl(key)
                    if ttl > 0:
                        reset_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
                        self._circuit_state[op] = reset_at
                        _copen.labels(op=op).set(1)
                        logger.warning(
                            "request_manager: Gemini %s circuit pre-loaded from Redis "
                            "— open until ~%s UTC (%ds remaining).",
                            op, reset_at.strftime("%H:%M:%S"), ttl,
                        )
                        continue
                # Key absent or TTL already expired — treat as closed.
                self._circuit_state[op] = None
                _copen.labels(op=op).set(0)
            except Exception as exc:
                # Redis unavailable during startup — fail open (assume closed).
                # The circuit will reopen if a 429 is subsequently received.
                logger.warning(
                    "request_manager: Redis circuit-state read failed for %s (%s) "
                    "— assuming closed.  Circuit will reopen on next quota 429.",
                    op, exc,
                )
                self._circuit_state[op] = None
                _copen.labels(op=op).set(0)

    def _is_circuit_open(self, operation: str) -> bool:
        """
        Check in-process circuit state.  O(1), no I/O.

        Auto-closes the circuit when the reset datetime is reached so callers
        are unblocked the moment the new quota period begins.
        """
        state = self._circuit_state.get(operation)
        if state is None:
            return False
        if datetime.now(timezone.utc) >= state:
            self._circuit_state[operation] = None
            self._emit_circuit_metric(operation, open_=False)
            logger.info(
                "request_manager: Gemini %s circuit auto-closed — quota has reset.",
                operation,
            )
            return False
        return True

    async def _write_circuit_to_redis(
        self, operation: str, reset_at: datetime
    ) -> None:
        """Persist circuit open state to Redis.  Best-effort; called as a Task."""
        ttl_secs = max(1, int((reset_at - datetime.now(timezone.utc)).total_seconds()))
        key = _CIRCUIT_REDIS_KEYS[operation]
        try:
            # SET … EX atomically overwrites a stale TTL from a previous run.
            await self._redis.set(key, "1", ex=ttl_secs)
            logger.info(
                "request_manager: Redis circuit key %s written (TTL %ds).",
                key, ttl_secs,
            )
        except Exception as exc:
            logger.error(
                "request_manager: Failed to write circuit key %s to Redis: %s "
                "(in-process state is authoritative; restart recovery may be impaired).",
                key, exc,
            )

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

    # ── Private — token buckets ────────────────────────────────────────────────

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

            # ── Step 3: Fast-fail if the circuit opened while queued ───────────
            if self._is_circuit_open(permit.operation):
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


# ── Module helpers ─────────────────────────────────────────────────────────────

def _quota_reset_at() -> datetime:
    """
    Return the next Gemini daily quota reset as a UTC datetime.

    Gemini resets daily quotas at midnight Pacific Time.  This function
    returns the next occurrence of midnight PT converted to UTC so the
    Redis TTL and in-process reset check use a consistent reference point.
    """
    pt = ZoneInfo("America/Los_Angeles")
    now_pt = datetime.now(pt)
    midnight_tomorrow_pt = (now_pt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return midnight_tomorrow_pt.astimezone(timezone.utc)


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
            "Call `await GeminiRequestManager.initialize(redis=get_redis())` "
            "in the FastAPI lifespan before serving requests."
        )
    return inst
