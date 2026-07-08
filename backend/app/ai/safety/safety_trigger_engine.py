"""
Safety Trigger Engine — Automated Safety Monitoring (WS9: real metrics)
=======================================================================
Monitors live system metrics and takes protective action when thresholds are
breached. Until WS9 every metric was a hardcoded 0 — the loop ran but could
never fire; now all three checks read real data:

  signal_rate  Trade suggestions created in the last hour
               (threshold: SIGNAL_FREQUENCY_THRESHOLD). A runaway correlation
               engine (bug, feedback loop, poisoned upstream data) manifests
               as an abnormal creation rate → GLOBAL KILL SWITCH.

  volatility   High-volatility regime prevalence vs its own 30-day baseline
               (threshold: VOLATILITY_SPIKE_MULTIPLIER, e.g. 3.0 = today's
               high-vol fraction is 3x its norm). Expressed as a RATIO so the
               multiplier threshold semantics hold — an absolute fraction
               (0..1) could never cross a 3.0 threshold, which is how the old
               placeholder wiring was silently unfireable → SIGNAL PAUSE
               (auto-expiring; volatility spikes are transient).

  loss_pct     Today's realized paper-trading loss as a fraction of total
               portfolio value (threshold: LOSS_LIMIT_THRESHOLD, e.g. 0.05 =
               5% session loss). The config key existed but was never read
               (WS9 adds the missing consumer) → GLOBAL KILL SWITCH.

Actions (both have REAL effects — "paused_signals" used to be a DB-row-only
no-op):
  kill_switch_activated  KillSwitchManager switch_type="global"; checked by
                         signal_pipeline before any event processing.
  paused_signals         KillSwitchManager switch_type="signal_pause" with a
                         PAUSE_EXPIRATION_MINUTES auto-expiry; also checked
                         by signal_pipeline.

Anti-latch (WS9): switches activated BY THIS ENGINE auto-release after the
breached condition has stayed clear for RECOVERY_CLEAR_CHECKS consecutive
checks — a transient metric spike can no longer latch the global kill switch
until a human notices. Manually-activated switches are NEVER auto-released.

Anti-spam: a sustained breach re-fires at most once per
TRIGGER_COOLDOWN_SECS per trigger_type (the switch stays active regardless —
this only bounds duplicate audit rows/alerts).
"""
import asyncio
import logging
import time as time_mod
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.fusion.models import AIRegimeDetection, AISafetyTrigger
from app.ai.safety.kill_switch_manager import KillSwitchManager
from app.core.config import get_settings
from app.core.metrics import (
    safety_kill_switch_auto_releases_total,
    safety_metric_value,
    safety_triggers_total,
)
from app.core.redis import PubSubClient, RedisChannels
from app.models.paper_trading import PaperPnlSnapshot, PaperTradeOutcome
from app.models.trade_suggestions import TradeSuggestion

logger = logging.getLogger(__name__)
settings = get_settings()

# Auto-expiry for the signal-pause action: volatility spikes are transient,
# and the pause self-heals even if the engine dies mid-episode.
PAUSE_EXPIRATION_MINUTES = 30

# Consecutive all-clear checks before engine-activated switches are released.
# 10 checks x SAFETY_CHECK_INTERVAL_SECONDS (30s) = 5 minutes of calm.
RECOVERY_CLEAR_CHECKS = 10

# Minimum seconds between duplicate trigger rows for the same trigger_type.
TRIGGER_COOLDOWN_SECS = 300

# Volatility baseline windows and division floor. The floor keeps the ratio
# meaningful when the baseline is near zero (a calm month): today's fraction
# is measured against at least a 5% baseline, so ratio = fraction / 0.05.
_VOLATILITY_CURRENT_WINDOW_HOURS = 24
_VOLATILITY_BASELINE_WINDOW_DAYS = 30
_VOLATILITY_BASELINE_FLOOR = 0.05

# Identity stamped on engine-activated switches — the auto-release ONLY ever
# touches switches carrying it. Manual activations are sacrosanct.
_ENGINE_ACTOR = "safety_trigger_engine"


# ── Metric collectors (real data — WS9) ────────────────────────────────────────

async def _collect_signal_rate(db: AsyncSession) -> float:
    """Trade suggestions created in the last hour."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    count = await db.scalar(
        select(func.count())
        .select_from(TradeSuggestion)
        .where(TradeSuggestion.created_at >= cutoff)
    )
    return float(count or 0)


async def _high_vol_fraction(db: AsyncSession, since: datetime) -> float | None:
    """Fraction of regime detections since ``since`` labeled high_volatility.

    Returns None when there are no detections in the window (no signal —
    distinct from a genuine 0% high-vol reading).
    """
    total = await db.scalar(
        select(func.count())
        .select_from(AIRegimeDetection)
        .where(AIRegimeDetection.detection_timestamp >= since)
    )
    if not total:
        return None
    high = await db.scalar(
        select(func.count())
        .select_from(AIRegimeDetection)
        .where(
            AIRegimeDetection.detection_timestamp >= since,
            AIRegimeDetection.regime_type == "high_volatility",
        )
    )
    return float(high or 0) / float(total)


async def _collect_volatility_ratio(db: AsyncSession) -> float:
    """Current high-volatility regime prevalence vs its 30-day baseline."""
    now = datetime.now(timezone.utc)
    current = await _high_vol_fraction(
        db, now - timedelta(hours=_VOLATILITY_CURRENT_WINDOW_HOURS)
    )
    if current is None:
        return 0.0  # no detections → no volatility signal, never a spike
    baseline = await _high_vol_fraction(
        db, now - timedelta(days=_VOLATILITY_BASELINE_WINDOW_DAYS)
    )
    denominator = max(baseline or 0.0, _VOLATILITY_BASELINE_FLOOR)
    return current / denominator


async def _collect_session_loss_pct(db: AsyncSession) -> float:
    """
    Today's realized paper-trading LOSS as a fraction of total portfolio value.

    Numerator: sum of net_pnl over outcomes closed since UTC midnight —
    positive P&L yields 0.0 (this metric only measures drawdown).
    Denominator: total portfolio value from each portfolio's most recent EOD
    snapshot. No snapshots → 0.0 (no basis to compute a fraction).
    """
    today_start = datetime.combine(
        date.today(), datetime.min.time(), tzinfo=timezone.utc
    )
    # Outcome rows are append-only and written exactly once at position close,
    # so created_at IS the close time (the feedback loader aliases it the same
    # way; PaperPosition.closed_at is a different table's column).
    realized = await db.scalar(
        select(func.coalesce(func.sum(PaperTradeOutcome.net_pnl), 0))
        .where(PaperTradeOutcome.created_at >= today_start)
    )

    latest_per_portfolio = (
        select(
            PaperPnlSnapshot.portfolio_id,
            func.max(PaperPnlSnapshot.snapshot_date).label("latest_date"),
        )
        .group_by(PaperPnlSnapshot.portfolio_id)
        .subquery()
    )
    total_value = await db.scalar(
        select(func.coalesce(func.sum(PaperPnlSnapshot.portfolio_value), 0))
        .join(
            latest_per_portfolio,
            (PaperPnlSnapshot.portfolio_id == latest_per_portfolio.c.portfolio_id)
            & (PaperPnlSnapshot.snapshot_date == latest_per_portfolio.c.latest_date),
        )
    )

    total_value_f = float(total_value or 0)
    if total_value_f <= 0:
        return 0.0
    loss = max(0.0, -float(realized or 0))
    return loss / total_value_f


async def collect_safety_metrics(db: AsyncSession) -> dict[str, float]:
    """Gather all live safety metrics (replaces the hardcoded placeholders)."""
    metrics = {
        "signal_rate": await _collect_signal_rate(db),
        "volatility": await _collect_volatility_ratio(db),
        "loss_pct": await _collect_session_loss_pct(db),
    }
    for name, value in metrics.items():
        safety_metric_value.labels(metric=name).set(value)
    return metrics


# ── Engine ─────────────────────────────────────────────────────────────────────

class SafetyTriggerEngine:
    """Monitors safety conditions, takes protective actions, and self-heals."""

    def __init__(self, redis: Redis) -> None:
        self.kill_switch_manager = KillSwitchManager(redis)
        # Per-type wall-clock of the last created trigger row (anti-spam).
        self._last_trigger_at: dict[str, float] = {}
        # Consecutive checks with every metric below threshold (anti-latch).
        self._clear_streak = 0

    async def check_safety_conditions(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        metrics: dict,
    ) -> list[AISafetyTrigger]:
        """Check safety conditions and trigger actions if needed."""
        triggers = []

        # Check signal frequency (suggestions/hour — see module docstring)
        signal_rate = metrics.get("signal_rate", 0)
        if signal_rate > settings.SIGNAL_FREQUENCY_THRESHOLD:
            trigger = await self._create_trigger(
                db,
                pubsub,
                "high_signal_rate",
                f"Signal rate > {settings.SIGNAL_FREQUENCY_THRESHOLD}/hour",
                Decimal(str(settings.SIGNAL_FREQUENCY_THRESHOLD)),
                Decimal(str(signal_rate)),
                "kill_switch_activated",
            )
            if trigger is not None:
                triggers.append(trigger)

        # Check volatility spike (ratio vs 30-day baseline)
        volatility = metrics.get("volatility", 0)
        threshold = settings.VOLATILITY_SPIKE_MULTIPLIER
        if volatility > threshold:
            trigger = await self._create_trigger(
                db,
                pubsub,
                "high_volatility",
                f"High-volatility regime prevalence > {threshold}x its 30-day baseline",
                Decimal(str(threshold)),
                Decimal(str(volatility)),
                "paused_signals",
            )
            if trigger is not None:
                triggers.append(trigger)

        # Check session loss (WS9 — LOSS_LIMIT_THRESHOLD finally has a consumer).
        # Stored as PERCENTAGES: the audit columns are Numeric(10,2), which
        # would crush a fraction like 0.054 into 0.05.
        loss_pct = metrics.get("loss_pct", 0)
        if loss_pct > settings.LOSS_LIMIT_THRESHOLD:
            trigger = await self._create_trigger(
                db,
                pubsub,
                "session_loss_limit",
                f"Realized session loss > {settings.LOSS_LIMIT_THRESHOLD:.0%} of portfolio value",
                Decimal(str(round(settings.LOSS_LIMIT_THRESHOLD * 100, 2))),
                Decimal(str(round(loss_pct * 100, 2))),
                "kill_switch_activated",
            )
            if trigger is not None:
                triggers.append(trigger)

        # ── Anti-latch recovery (WS9) ─────────────────────────────────────────
        breached = (
            signal_rate > settings.SIGNAL_FREQUENCY_THRESHOLD
            or volatility > settings.VOLATILITY_SPIKE_MULTIPLIER
            or loss_pct > settings.LOSS_LIMIT_THRESHOLD
        )
        if breached:
            self._clear_streak = 0
        else:
            self._clear_streak += 1
            if self._clear_streak == RECOVERY_CLEAR_CHECKS:
                await self._release_engine_switches(db, pubsub)

        return triggers

    async def _release_engine_switches(
        self, db: AsyncSession, pubsub: PubSubClient
    ) -> None:
        """
        Deactivate switches THIS ENGINE activated, after sustained recovery.

        Scoped strictly to activated_by == _ENGINE_ACTOR — a human's manual
        kill switch is a deliberate decision and is never auto-released.
        """
        from app.ai.fusion.models import AIKillSwitch

        result = await db.execute(
            select(AIKillSwitch).where(
                AIKillSwitch.status == "active",
                AIKillSwitch.activated_by == _ENGINE_ACTOR,
            )
        )
        switches = result.scalars().all()
        for switch in switches:
            try:
                await self.kill_switch_manager.deactivate(db, pubsub, switch.id)
                safety_kill_switch_auto_releases_total.labels(
                    switch_type=switch.switch_type
                ).inc()
                logger.warning(
                    "Safety engine auto-released %s switch %d after %d consecutive "
                    "clear checks (%.0fs of calm)",
                    switch.switch_type,
                    switch.id,
                    RECOVERY_CLEAR_CHECKS,
                    RECOVERY_CLEAR_CHECKS * settings.SAFETY_CHECK_INTERVAL_SECONDS,
                )
            except Exception as exc:
                logger.error(
                    "Safety engine failed to auto-release switch %d: %s",
                    switch.id, exc,
                )

    async def _create_trigger(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        trigger_type: str,
        condition: str,
        threshold: Decimal,
        actual: Decimal,
        action: str,
    ) -> AISafetyTrigger | None:
        """Create a safety trigger row and take its action.

        Returns None when suppressed by the per-type cooldown (the protective
        switch from the FIRST firing is still active — this only prevents a
        sustained breach from writing an audit row every 30 seconds).
        """
        now_mono = time_mod.monotonic()
        last = self._last_trigger_at.get(trigger_type)
        if last is not None and now_mono - last < TRIGGER_COOLDOWN_SECS:
            logger.debug(
                "Safety trigger %s suppressed by cooldown (%.0fs remaining)",
                trigger_type, TRIGGER_COOLDOWN_SECS - (now_mono - last),
            )
            return None
        self._last_trigger_at[trigger_type] = now_mono

        trigger = AISafetyTrigger(
            trigger_type=trigger_type,
            trigger_condition=condition,
            threshold_value=threshold,
            actual_value=actual,
            action_taken=action,
            triggered_at=datetime.utcnow(),
        )

        db.add(trigger)
        await db.commit()
        await db.refresh(trigger)
        safety_triggers_total.labels(trigger_type=trigger_type).inc()

        # Publish to Redis
        await pubsub.publish_json(RedisChannels.SAFETY_TRIGGERS, {
            "trigger_id": trigger.id,
            "type": trigger_type,
            "action": action,
            "timestamp": trigger.triggered_at.isoformat(),
        })

        # Take the protective action — BOTH actions are real (WS9):
        # "paused_signals" used to write the row and do nothing.
        if action == "kill_switch_activated":
            await self.kill_switch_manager.activate(
                db,
                pubsub,
                "global",
                None,
                _ENGINE_ACTOR,
                f"Safety trigger: {condition}",
            )
        elif action == "paused_signals":
            await self.kill_switch_manager.activate(
                db,
                pubsub,
                "signal_pause",
                None,
                _ENGINE_ACTOR,
                f"Safety trigger: {condition}",
                expiration_minutes=PAUSE_EXPIRATION_MINUTES,
            )

        logger.warning(f"Safety trigger: {trigger_type} - {action}")
        return trigger


async def safety_monitoring_loop(
    session_factory: async_sessionmaker,
    redis: Redis,
    *,
    on_cycle: Callable[[], None] | None = None,
) -> None:
    """
    Safety monitoring background loop.

    Gathers REAL metrics each cycle (WS9 — previously hardcoded zeros meant
    no trigger could ever fire), checks thresholds, takes protective action,
    and auto-releases engine-activated switches after sustained recovery.
    Runs until cancelled via asyncio.CancelledError.

    Args:
        session_factory: SQLAlchemy async session factory (WorkerSessionLocal).
        redis: Raw asyncio Redis client — passed to KillSwitchManager for
               <1ms cache reads and to PubSubClient for event publishing.
    """
    logger.info("Safety monitoring loop started")
    engine = SafetyTriggerEngine(redis)
    pubsub = PubSubClient(redis)

    try:
        while True:
            try:
                async with session_factory() as db:
                    metrics = await collect_safety_metrics(db)
                    triggers = await engine.check_safety_conditions(db, pubsub, metrics)

                    if triggers:
                        logger.warning(f"Safety check triggered {len(triggers)} actions")
                    else:
                        logger.debug(
                            "Safety check passed: signal_rate=%.0f/h "
                            "volatility=%.2fx loss=%.2f%%",
                            metrics["signal_rate"],
                            metrics["volatility"],
                            metrics["loss_pct"] * 100,
                        )

            except Exception as e:
                logger.error(f"Safety monitoring error: {e}", exc_info=True)

            if on_cycle is not None:
                on_cycle()

            # Sleep between iterations
            logger.debug(f"Sleeping for {settings.SAFETY_CHECK_INTERVAL_SECONDS}s")
            await asyncio.sleep(settings.SAFETY_CHECK_INTERVAL_SECONDS)

    except asyncio.CancelledError:
        logger.info("Safety monitoring loop cancelled")
        raise
    finally:
        logger.info("Safety monitoring loop stopped")
