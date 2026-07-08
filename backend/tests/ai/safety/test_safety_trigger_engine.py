"""
Safety trigger engine tests (WS9)
=================================
Every metric was a hardcoded 0 before WS9 — the loop ran but could never
fire. These tests pin: each threshold fires its trigger with a REAL action
(pause included), the anti-spam cooldown, and the anti-latch auto-release
that never touches manually-activated switches.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.ai.safety.safety_trigger_engine as ste
from app.ai.safety.safety_trigger_engine import (
    RECOVERY_CLEAR_CHECKS,
    SafetyTriggerEngine,
    _collect_volatility_ratio,
)

pytestmark = pytest.mark.unit


def _engine() -> SafetyTriggerEngine:
    engine = SafetyTriggerEngine(AsyncMock())
    engine.kill_switch_manager = AsyncMock()
    return engine


def _db() -> AsyncMock:
    db = AsyncMock()
    db.add = MagicMock()
    return db


_CALM = {"signal_rate": 10.0, "volatility": 1.0, "loss_pct": 0.01}


def _settings_patch():
    settings = MagicMock()
    settings.SIGNAL_FREQUENCY_THRESHOLD = 100
    settings.VOLATILITY_SPIKE_MULTIPLIER = 3.0
    settings.LOSS_LIMIT_THRESHOLD = 0.05
    settings.SAFETY_CHECK_INTERVAL_SECONDS = 30
    return patch.object(ste, "settings", settings)


class TestTriggerFiring:
    @pytest.mark.asyncio
    async def test_calm_metrics_fire_nothing(self):
        engine = _engine()
        with _settings_patch():
            triggers = await engine.check_safety_conditions(_db(), AsyncMock(), _CALM)
        assert triggers == []
        engine.kill_switch_manager.activate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_high_signal_rate_activates_global_kill_switch(self):
        engine = _engine()
        with _settings_patch():
            triggers = await engine.check_safety_conditions(
                _db(), AsyncMock(), {**_CALM, "signal_rate": 250.0}
            )
        assert len(triggers) == 1
        args = engine.kill_switch_manager.activate.await_args.args
        assert args[2] == "global"
        assert args[4] == "safety_trigger_engine"

    @pytest.mark.asyncio
    async def test_volatility_spike_activates_real_signal_pause(self):
        """The old 'paused_signals' action was a DB-row no-op — it must now
        activate an auto-expiring signal_pause switch."""
        engine = _engine()
        with _settings_patch():
            triggers = await engine.check_safety_conditions(
                _db(), AsyncMock(), {**_CALM, "volatility": 4.2}
            )
        assert len(triggers) == 1
        call = engine.kill_switch_manager.activate.await_args
        assert call.args[2] == "signal_pause"
        assert call.kwargs["expiration_minutes"] == ste.PAUSE_EXPIRATION_MINUTES

    @pytest.mark.asyncio
    async def test_session_loss_activates_global_kill_switch_in_percent(self):
        engine = _engine()
        db = _db()
        with _settings_patch():
            triggers = await engine.check_safety_conditions(
                db, AsyncMock(), {**_CALM, "loss_pct": 0.0634}
            )
        assert len(triggers) == 1
        row = db.add.call_args.args[0]
        assert row.trigger_type == "session_loss_limit"
        # Stored as percentages — Numeric(10,2) would crush the fraction.
        assert row.threshold_value == Decimal("5.0")
        assert row.actual_value == Decimal("6.34")
        assert engine.kill_switch_manager.activate.await_args.args[2] == "global"

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_duplicate_trigger_rows(self):
        engine = _engine()
        breach = {**_CALM, "signal_rate": 250.0}
        with _settings_patch():
            first = await engine.check_safety_conditions(_db(), AsyncMock(), breach)
            second = await engine.check_safety_conditions(_db(), AsyncMock(), breach)
        assert len(first) == 1
        assert second == []  # suppressed — the first switch is still active
        assert engine.kill_switch_manager.activate.await_count == 1


class TestAntiLatchRecovery:
    @pytest.mark.asyncio
    async def test_release_after_sustained_calm_only(self):
        engine = _engine()
        engine._release_engine_switches = AsyncMock()
        with _settings_patch():
            # A breach resets the streak…
            await engine.check_safety_conditions(
                _db(), AsyncMock(), {**_CALM, "signal_rate": 250.0}
            )
            # …then calm checks accumulate; release fires exactly at N.
            for i in range(RECOVERY_CLEAR_CHECKS - 1):
                await engine.check_safety_conditions(_db(), AsyncMock(), _CALM)
                engine._release_engine_switches.assert_not_awaited()
            await engine.check_safety_conditions(_db(), AsyncMock(), _CALM)
        engine._release_engine_switches.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_breach_mid_recovery_resets_the_streak(self):
        engine = _engine()
        engine._release_engine_switches = AsyncMock()
        with _settings_patch():
            for _ in range(RECOVERY_CLEAR_CHECKS - 1):
                await engine.check_safety_conditions(_db(), AsyncMock(), _CALM)
            await engine.check_safety_conditions(
                _db(), AsyncMock(), {**_CALM, "loss_pct": 0.09}
            )
            for _ in range(RECOVERY_CLEAR_CHECKS - 1):
                await engine.check_safety_conditions(_db(), AsyncMock(), _CALM)
        engine._release_engine_switches.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_release_only_touches_engine_activated_switches(self):
        engine = _engine()
        manual = MagicMock(id=1, activated_by="admin_user", status="active")
        engine_owned = MagicMock(
            id=2, activated_by="safety_trigger_engine", status="active",
            switch_type="global",
        )
        db = _db()
        result = MagicMock()
        # The query itself filters on activated_by — simulate its result.
        result.scalars.return_value.all.return_value = [engine_owned]
        db.execute = AsyncMock(return_value=result)

        with _settings_patch():
            await engine._release_engine_switches(db, AsyncMock())

        engine.kill_switch_manager.deactivate.assert_awaited_once()
        assert engine.kill_switch_manager.deactivate.await_args.args[2] == 2
        # And the SQL filter targets only engine-activated switches.
        compiled = str(db.execute.await_args.args[0].compile())
        assert "activated_by" in compiled
        assert manual.id != engine_owned.id  # guard the fixture's intent


class TestVolatilityRatio:
    def _db_with_counts(self, counts: list[int]) -> AsyncMock:
        db = AsyncMock()
        db.scalar = AsyncMock(side_effect=counts)
        return db

    @pytest.mark.asyncio
    async def test_ratio_vs_baseline(self):
        # current window: 40 total / 20 high (0.5) · baseline: 100 total / 10 high (0.1)
        db = self._db_with_counts([40, 20, 100, 10])
        assert await _collect_volatility_ratio(db) == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_no_detections_is_no_signal(self):
        db = self._db_with_counts([0])
        assert await _collect_volatility_ratio(db) == 0.0

    @pytest.mark.asyncio
    async def test_near_zero_baseline_uses_floor(self):
        # current: 10/10 high (1.0) · baseline: 100/0 high (0.0) → floor 0.05
        db = self._db_with_counts([10, 10, 100, 0])
        assert await _collect_volatility_ratio(db) == pytest.approx(20.0)
