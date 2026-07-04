"""
Cortex AI — Worker Supervisor Infrastructure
=============================================

Three asyncio primitives that give the worker sidecar control-plane cooperative
control over every background task:

  PauseToken    Asyncio-Event gate.  pause() blocks the task at its next
                checkpoint() call without spinning; resume() unblocks it.

  TriggerToken  Interrupt-style early wakeup.  fire() causes the task's
                wait_or_timeout(N) call to return True immediately instead of
                sleeping the full N seconds.  Replaces the legacy
                ``asyncio.wait_for(shutdown.wait(), timeout=N)`` pattern used
                throughout worker.py so tasks respond to remote trigger commands.

  TaskState     Bookkeeping dataclass: name, live status, last_run_at,
                crash_count, token handles, and the asyncio.Task reference.

  supervised()  Restart wrapper with exponential back-off.  A task that exits
                cleanly resets the crash counter.  Five consecutive crashes
                propagate out of the TaskGroup → ExceptionGroup unwinds the
                worker process → Docker restart: unless-stopped brings it back.

Usage (inside worker_app.py lifespan):
    states   = create_task_states(list(TASK_NAMES))
    registry = build_task_registry(session_factory, redis, ml, upstox,
                                   shutdown_event, states)

    async with asyncio.TaskGroup() as tg:
        for name, factory in registry.items():
            task = tg.create_task(
                supervised(name, factory, states[name], shutdown_event)
            )
            states[name].task_handle = task
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Literal

logger = logging.getLogger(__name__)

# ── Type alias ─────────────────────────────────────────────────────────────────
TaskStatus = Literal["starting", "running", "paused", "crashed", "stopped"]


# ─────────────────────────────── PauseToken ───────────────────────────────────

class PauseToken:
    """
    Cooperative pause gate backed by ``asyncio.Event``.

    The event starts SET (running).  When the control-plane calls pause(),
    the event is CLEARED.  The task's loop body starts with
    ``await pause.checkpoint()``, which blocks until resume() sets it again.
    No CPU is consumed while waiting — the event loop continues normally.

    Thread-safety note: all callers must run in the same event loop.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._event.set()           # starts in "running" state

    async def checkpoint(self) -> None:
        """Return immediately if running; block until resumed if paused."""
        await self._event.wait()

    def pause(self) -> None:
        """Signal the task to pause at its next checkpoint (idempotent)."""
        self._event.clear()

    def resume(self) -> None:
        """Allow the task to continue past its checkpoint (idempotent)."""
        self._event.set()

    @property
    def is_paused(self) -> bool:
        """True when the token is in the paused state."""
        return not self._event.is_set()


# ─────────────────────────────── TriggerToken ─────────────────────────────────

class TriggerToken:
    """
    Interrupt-style early-wakeup backed by ``asyncio.Event``.

    Tasks sleep between cycles by calling ``await trigger.wait_or_timeout(N)``
    instead of ``asyncio.sleep(N)``.  The control-plane can call fire() to
    wake the task immediately (e.g. to force a cycle without waiting for the
    next natural interval).  The event auto-resets after each wakeup so the
    next wait_or_timeout call sleeps normally.
    """

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = asyncio.Event()

    async def wait_or_timeout(self, secs: float) -> bool:
        """
        Sleep for up to *secs* seconds.  Wakes early when fire() is called.

        Args:
            secs: Maximum sleep duration in seconds.

        Returns:
            True  — fire() was called before the timeout; caller may choose to
                    run an immediate cycle.
            False — natural timeout elapsed; normal cadence continues.
        """
        try:
            await asyncio.wait_for(self._event.wait(), timeout=secs)
            self._event.clear()     # auto-reset so next call sleeps normally
            return True
        except asyncio.TimeoutError:
            return False

    def wait(self) -> "asyncio.coroutines":
        """
        Return an awaitable that resolves when fire() is called.

        Unlike wait_or_timeout(), this does NOT auto-reset the event — the
        caller must call consume() after waking to prevent the next waiter
        from returning immediately.  Intended for use with asyncio.wait()
        when racing against multiple events (e.g. shutdown + trigger).
        """
        return self._event.wait()

    def consume(self) -> None:
        """
        Consume (clear) a pending trigger signal.

        Call this after waking via wait() to reset the event so the next
        wait() / wait_or_timeout() call sleeps normally.  Idempotent.
        """
        self._event.clear()

    def fire(self) -> None:
        """Wake up the task immediately (idempotent; safe to call mid-cycle)."""
        self._event.set()


# ─────────────────────────────── TaskState ────────────────────────────────────

@dataclass
class TaskState:
    """
    Per-task runtime bookkeeping.

    Instances are created at worker startup by create_task_states() and stored
    at ``app.state.task_states``.  The control-plane routes (worker_control.py)
    read and mutate these objects directly.

    Attributes:
        name:          Canonical task identifier (matches registry key).
        status:        Current lifecycle status (set by the supervisor).
        last_run_at:   UTC timestamp supervised() last (re)entered its outer
                        loop for this task — i.e. restart recency, NOT work
                        recency. Only changes on process start or a crash
                        restart; frozen for the entire lifetime of a healthy,
                        long-running task loop.
        last_cycle_at: UTC timestamp the task itself last completed a real
                        work cycle, set via record_cycle() from inside the
                        loop body. This is the genuine liveness signal —
                        distinct from last_run_at — and is None until the
                        task's first cycle completes.
        crash_count:   Consecutive crash count; reset to 0 on clean exit.
        pause_token:   Cooperative pause gate for this task.
        trigger_token: Early-wakeup trigger for this task.
        task_handle:   The asyncio.Task wrapping supervised(); set after creation.
    """

    name: str
    status: TaskStatus = "starting"
    last_run_at: datetime | None = None
    last_cycle_at: datetime | None = None
    crash_count: int = 0
    pause_token: PauseToken = field(default_factory=PauseToken)
    trigger_token: TriggerToken = field(default_factory=TriggerToken)
    task_handle: asyncio.Task | None = None

    def record_cycle(self) -> None:
        """
        Mark that this task completed a real work cycle just now.

        Synchronous, no await — safe to call from inside a plain loop body
        right after a cycle's work has finished, before the loop's
        sleep/wait call. Distinct from last_run_at, which only reflects
        supervisor (re)start time and never changes for a healthy
        long-running task.
        """
        self.last_cycle_at = datetime.now(timezone.utc)


def create_task_states(names: list[str]) -> dict[str, TaskState]:
    """
    Create a fresh TaskState for each task name.

    Args:
        names: Ordered list of task identifiers (must match the registry keys).

    Returns:
        Mapping of task name → TaskState with fresh tokens for each.
    """
    return {name: TaskState(name=name) for name in names}


# ─────────────────────────────── supervised() ─────────────────────────────────

async def supervised(
    name: str,
    coro_factory: Callable[[], Coroutine],
    state: TaskState,
    shutdown: asyncio.Event,
    *,
    max_failures: int = 5,
) -> None:
    """
    Wrap *coro_factory* with restart logic, back-off, and state tracking.

    Calling convention:
        Each iteration calls ``coro_factory()`` to obtain a *fresh* coroutine
        so all closure-captured dependencies are re-evaluated on restart
        (avoids stale DB sessions, closed Redis connections, etc.).

    Two-timestamp split (do not conflate these):
        ``state.last_run_at`` is set below, once per outer-loop iteration —
        i.e. once per process start or crash-restart. Every task's
        coro_factory() is itself a long-running loop that never returns
        under normal operation, so last_run_at freezes at start time for the
        entire healthy lifetime of the task. It answers "when did this task
        last restart," not "is this task still doing work." Real per-cycle
        liveness is ``state.last_cycle_at``, updated independently by the
        task body itself via ``state.record_cycle()`` — supervised() never
        touches it.

    Back-off schedule (consecutive crashes):
        1st:  2s   2nd:  4s   3rd:  8s   4th: 16s   5th+: max 60s

    Propagation:
        If ``crash_count >= max_failures``, the exception is re-raised inside
        the TaskGroup.  Python 3.11 TaskGroup collects an ExceptionGroup and
        cancels all siblings → ``worker_app.py`` lifespan exits → Docker's
        ``restart: unless-stopped`` policy brings the container back.  This is
        intentional — five consecutive crashes indicate a hard failure that
        warrants a fresh process rather than an infinite retry loop.

    Shutdown:
        When ``shutdown.is_set()``, the outer loop exits cleanly.  If the
        shutdown is signalled during the back-off sleep, the supervisor exits
        immediately rather than waiting out the full delay.

    Args:
        name:          Task name for logging and state updates.
        coro_factory:  Zero-argument callable returning a coroutine each call.
        state:         Mutable TaskState updated throughout the lifecycle.
        shutdown:      Shared shutdown event; when set, the supervisor exits.
        max_failures:  Consecutive crash threshold before propagating upstream.
    """
    logger.info("Supervisor starting: %s", name)

    while not shutdown.is_set():
        state.status = "running"
        state.last_run_at = datetime.now(timezone.utc)

        try:
            await coro_factory()

            # The factory returned without raising — treat as a clean exit
            # (e.g. the task detected shutdown internally and returned).
            state.crash_count = 0
            state.status = "stopped"
            logger.info("Task %s exited cleanly", name)

        except asyncio.CancelledError:
            # CancelledError must never be swallowed — it signals that the
            # TaskGroup or caller has cancelled this coroutine.
            state.status = "stopped"
            logger.info("Task %s cancelled", name)
            raise

        except Exception as exc:
            state.crash_count += 1
            state.status = "crashed"
            delay = min(60.0, 2.0 ** state.crash_count)

            if state.crash_count >= max_failures:
                logger.critical(
                    "Task %s hit max failures (%d/%d) — propagating to TaskGroup. "
                    "Docker will restart the worker process.",
                    name, state.crash_count, max_failures,
                    exc_info=True,
                )
                raise   # ExceptionGroup → worker container restart

            logger.error(
                "Task %s crashed (#%d/%d): %s — retrying in %.1fs",
                name, state.crash_count, max_failures, exc, delay,
                exc_info=True,
            )

            # Respect shutdown during back-off: if the shutdown event fires
            # while we're waiting, exit cleanly instead of retrying.
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=delay)
                state.status = "stopped"
                logger.info("Task %s back-off interrupted by shutdown", name)
                return
            except asyncio.TimeoutError:
                pass    # back-off elapsed; loop continues to next attempt

    state.status = "stopped"
    logger.info("Supervisor exiting cleanly: %s", name)
