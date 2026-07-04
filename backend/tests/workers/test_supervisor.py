"""
Tests for app.workers.supervisor
=================================
Validates PauseToken, TriggerToken, TaskState, and the supervised() restart
wrapper under all key scenarios: pause/resume, early wakeup, back-off retry,
max-failure propagation, and crash-count reset on clean recovery.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.workers.supervisor import (
    PauseToken,
    TaskState,
    TaskStatus,
    TriggerToken,
    create_task_states,
    supervised,
)


# ── PauseToken ─────────────────────────────────────────────────────────────────

class TestPauseToken:
    def test_starts_running(self):
        token = PauseToken()
        assert not token.is_paused

    def test_pause_sets_paused(self):
        token = PauseToken()
        token.pause()
        assert token.is_paused

    def test_resume_clears_paused(self):
        token = PauseToken()
        token.pause()
        token.resume()
        assert not token.is_paused

    def test_pause_is_idempotent(self):
        token = PauseToken()
        token.pause()
        token.pause()
        assert token.is_paused

    def test_resume_is_idempotent(self):
        token = PauseToken()
        token.resume()
        token.resume()
        assert not token.is_paused

    @pytest.mark.asyncio
    async def test_checkpoint_does_not_block_when_running(self):
        token = PauseToken()
        # Should return immediately — no timeout needed
        await asyncio.wait_for(token.checkpoint(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_checkpoint_blocks_when_paused_then_resumes(self):
        token = PauseToken()
        token.pause()
        resume_happened = asyncio.Event()

        async def _wait():
            await token.checkpoint()
            resume_happened.set()

        task = asyncio.create_task(_wait())
        await asyncio.sleep(0.05)
        assert not resume_happened.is_set()   # still blocked

        token.resume()
        await asyncio.wait_for(task, timeout=0.5)
        assert resume_happened.is_set()


# ── TriggerToken ───────────────────────────────────────────────────────────────

class TestTriggerToken:
    @pytest.mark.asyncio
    async def test_falls_back_to_timeout(self):
        token = TriggerToken()
        triggered = await token.wait_or_timeout(0.05)
        assert triggered is False

    @pytest.mark.asyncio
    async def test_wakes_before_timeout_when_fired(self):
        token = TriggerToken()

        async def _fire_after_delay():
            await asyncio.sleep(0.02)
            token.fire()

        asyncio.create_task(_fire_after_delay())
        start = time.monotonic()
        triggered = await token.wait_or_timeout(5.0)   # long timeout — must wake early
        elapsed = time.monotonic() - start

        assert triggered is True
        assert elapsed < 0.5   # woke well before the 5s timeout

    @pytest.mark.asyncio
    async def test_auto_resets_after_fire(self):
        """Second wait_or_timeout call should fall back to natural timeout."""
        token = TriggerToken()
        token.fire()
        await token.wait_or_timeout(0.01)   # consumes the fire

        triggered = await token.wait_or_timeout(0.05)
        assert triggered is False   # event was cleared; no second fire

    @pytest.mark.asyncio
    async def test_fire_is_idempotent(self):
        token = TriggerToken()
        token.fire()
        token.fire()
        triggered = await token.wait_or_timeout(0.1)
        assert triggered is True


# ── TaskState ──────────────────────────────────────────────────────────────────

class TestTaskState:
    def test_initial_state(self):
        state = TaskState(name="test_task")
        assert state.name == "test_task"
        assert state.status == "starting"
        assert state.last_run_at is None
        assert state.last_cycle_at is None
        assert state.crash_count == 0
        assert state.task_handle is None
        assert isinstance(state.pause_token, PauseToken)
        assert isinstance(state.trigger_token, TriggerToken)

    def test_create_task_states(self):
        names = ["task_a", "task_b", "task_c"]
        states = create_task_states(names)
        assert set(states.keys()) == set(names)
        for name, state in states.items():
            assert state.name == name
            assert state.status == "starting"

    def test_create_task_states_fresh_tokens(self):
        """Each state must get its own PauseToken — not a shared instance."""
        states = create_task_states(["a", "b"])
        states["a"].pause_token.pause()
        assert not states["b"].pause_token.is_paused


# ── TaskState.record_cycle() ────────────────────────────────────────────────────

class TestRecordCycle:
    def test_record_cycle_sets_last_cycle_at(self):
        state = TaskState(name="cycle_task")
        assert state.last_cycle_at is None
        state.record_cycle()
        assert state.last_cycle_at is not None
        assert isinstance(state.last_cycle_at, datetime)

    def test_record_cycle_independent_of_last_run_at(self):
        """record_cycle() must never touch last_run_at — distinct signals."""
        state = TaskState(name="cycle_task")
        state.record_cycle()
        assert state.last_run_at is None

    def test_record_cycle_monotonic_across_calls(self):
        state = TaskState(name="cycle_task")
        state.record_cycle()
        first = state.last_cycle_at
        state.record_cycle()
        second = state.last_cycle_at
        assert second >= first


# ── supervised() ───────────────────────────────────────────────────────────────

class TestSupervised:
    @pytest.mark.asyncio
    async def test_runs_factory_once_and_exits_on_shutdown(self):
        """Clean exit: factory runs, then shutdown_event is set."""
        shutdown = asyncio.Event()
        state = TaskState(name="clean_task")
        calls = []

        async def factory():
            calls.append(1)
            shutdown.set()   # signal shutdown so the outer loop exits

        await supervised("clean_task", factory, state, shutdown)

        assert len(calls) == 1
        assert state.crash_count == 0
        assert state.status == "stopped"
        # Regression guard: supervised() must still stamp last_run_at at
        # loop-top (restart recency) — record_cycle()/last_cycle_at are a
        # fully separate concern it never touches.
        assert state.last_run_at is not None
        assert state.last_cycle_at is None

    @pytest.mark.asyncio
    async def test_retries_with_back_off_on_crash(self):
        """Crash once, then succeed — crash_count resets to 0."""
        shutdown = asyncio.Event()
        state = TaskState(name="retry_task")
        attempts = []

        async def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("transient failure")
            shutdown.set()

        # Patch asyncio.sleep to avoid actual delays in tests
        original_sleep = asyncio.sleep
        sleep_calls: list[float] = []

        async def mock_sleep(delay: float):
            sleep_calls.append(delay)
            await original_sleep(0)   # yield control without waiting

        import unittest.mock
        with unittest.mock.patch("asyncio.sleep", mock_sleep):
            await supervised("retry_task", factory, state, shutdown)

        assert len(attempts) == 2
        assert state.crash_count == 0   # reset after clean exit
        assert len(sleep_calls) == 1    # one back-off sleep

    @pytest.mark.asyncio
    async def test_propagates_at_max_failures(self):
        """Five consecutive crashes must propagate out of supervised()."""
        shutdown = asyncio.Event()
        state = TaskState(name="fatal_task")

        async def factory():
            raise RuntimeError("persistent failure")

        import unittest.mock

        async def mock_sleep(_: float):
            await asyncio.sleep(0)

        with unittest.mock.patch("asyncio.sleep", mock_sleep):
            with pytest.raises(RuntimeError, match="persistent failure"):
                await supervised("fatal_task", factory, state, shutdown, max_failures=5)

        assert state.crash_count >= 5

    @pytest.mark.asyncio
    async def test_crash_count_resets_on_recovery(self):
        """After a crash and successful recovery, crash_count goes back to 0."""
        shutdown = asyncio.Event()
        state = TaskState(name="recover_task")
        attempts = []

        async def factory():
            attempts.append(1)
            if len(attempts) < 3:
                raise ValueError("temporary")
            shutdown.set()

        import unittest.mock

        async def mock_sleep(_: float):
            await asyncio.sleep(0)

        with unittest.mock.patch("asyncio.sleep", mock_sleep):
            await supervised("recover_task", factory, state, shutdown)

        assert state.crash_count == 0

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        """CancelledError must never be swallowed by the supervisor."""
        shutdown = asyncio.Event()
        state = TaskState(name="cancel_task")

        async def factory():
            await asyncio.sleep(10)   # waits until cancelled

        task = asyncio.create_task(
            supervised("cancel_task", factory, state, shutdown)
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_shutdown_during_back_off_exits_immediately(self):
        """Setting shutdown during back-off must abort the wait."""
        shutdown = asyncio.Event()
        state = TaskState(name="shutdown_task")

        async def factory():
            raise RuntimeError("crash")

        async def mock_sleep(delay: float):
            # Simulate shutdown arriving while the supervisor sleeps
            shutdown.set()
            await asyncio.sleep(0)

        import unittest.mock
        with unittest.mock.patch("asyncio.sleep", mock_sleep):
            # With shutdown during back-off the supervisor exits cleanly
            # rather than retrying (the while-not-shutdown guard triggers).
            await supervised("shutdown_task", factory, state, shutdown)

        assert state.status == "stopped"
