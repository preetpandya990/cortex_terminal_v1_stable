"""
Cortex AI — Portfolio-Insight ML-Param Refresher (B2)
======================================================
Worker-sidecar task that keeps the *slow* ML inputs for every open paper
position warm in Redis, so the P&L recompute hot loop (B3) can compute the live
``P(hit TP before SL)`` metric with a single cache read and never touch ML
inference itself.

For each open-position instrument it caches, via ``insight_cache``:

    cai:paper:insight:mlparams:{instrument_key} = {prob_up, sigma, ts}

where ``prob_up`` is the ordinal-expected-value collapse of the ensemble's
calibrated ``{sell, hold, buy}`` probabilities (see ``insight_cache.derive_prob_up``).

Two cooperating drivers under one supervised task
-------------------------------------------------
  Sweep loop
    Every ``INSIGHT_MLPARAMS_SWEEP_INTERVAL_SECONDS`` it lists the distinct
    open-position instruments and scores only those whose cached params are
    missing or expiring within ``INSIGHT_MLPARAMS_REFRESH_MARGIN_SECONDS``
    (a control-plane trigger forces a full re-score).  Because features are
    daily, this is cheap steady-state — a Redis TTL check per instrument, with
    batched GPU inference only for the genuinely stale ones.

  On-demand listener
    Subscribes to ``PAPER_INSIGHT_REFRESH_REQUEST`` and scores a single
    instrument the instant a BUY fill opens a position, so a freshly-opened
    position shows its ML-tilted number almost immediately instead of waiting
    up to one sweep interval.  Idempotent: a request for an instrument whose
    params are still fresh is skipped.

The sweep is the safety net — any open path that does not publish (e.g.
matching-engine fills of pending DAY orders) is still covered within one sweep
interval, and B3 degrades gracefully to the neutral estimate in the meantime.

Design invariants
-----------------
- Scoring uses ``get_prediction_snapshots_batch`` — one batched inference call
  per chunk, never N sequential single-instrument calls (VRAM safety).
- Only *available* snapshots are cached; a degraded snapshot leaves the key
  absent so B3 never reads a fabricated number.
- Isolated from the P&L recompute hot loop: this runs in the sidecar and only
  ever *writes* the cache the hot loop reads.
- Gated by ``INSIGHT_ENABLED``: when off, the task parks responsively on
  shutdown and does no work (staged-rollout / kill switch).
- Pause/trigger-aware and crash-restartable via the standard worker supervisor.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.metrics import (
    insight_mlparams_last_run_timestamp,
    insight_mlparams_refresh_duration_seconds,
    insight_mlparams_refresh_runs_total,
    insight_mlparams_scored_total,
    insight_mlparams_snapshot_unavailable_total,
)
from app.core.redis import RedisChannels
from app.services.paper_trading import insight_cache
from app.services.prediction_snapshot import get_prediction_snapshots_batch
from app.workers.supervisor import PauseToken, TriggerToken

logger = logging.getLogger(__name__)

# Poll cadence while the feature is disabled — stays responsive to shutdown
# without busy-waiting.  Only reached when INSIGHT_ENABLED is False.
_IDLE_POLL_SECS: float = 30.0

_TIMEFRAME = "1d"


class InsightMLParamsRefresher:
    """
    Keep open-position ML params warm in Redis (see module docstring).

    Registered as a native, pause/trigger-aware task under the name
    ``insight_mlparams_refresher``.  Instantiated once at registry build time;
    ``run()`` returns a fresh coroutine on each supervisor restart so no mutable
    state persists across restarts.
    """

    __slots__ = (
        "_session_factory",
        "_redis",
        "_predictor",
        "_shutdown",
        "_pause",
        "_trigger",
        "_on_cycle",
    )

    def __init__(
        self,
        session_factory: async_sessionmaker,
        redis: Any,
        predictor: Any,
        shutdown: asyncio.Event,
        pause: PauseToken,
        trigger: TriggerToken,
        on_cycle: Callable[[], None] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._redis = redis
        self._predictor = predictor
        self._shutdown = shutdown
        self._pause = pause
        self._trigger = trigger
        self._on_cycle = on_cycle

    # ── Entry point ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Launch the sweep loop and the on-demand listener, supervising both.

        If either driver exits unexpectedly, cancel the other and return so the
        worker supervisor restarts the whole task cleanly.
        """
        if not get_settings().INSIGHT_ENABLED:
            logger.info(
                "insight_mlparams_refresher: INSIGHT_ENABLED is false — parking idle"
            )
            insight_mlparams_refresh_runs_total.labels(
                trigger="sweep", status="skipped_disabled"
            ).inc()
            while not self._shutdown.is_set():
                await self._trigger.wait_or_timeout(_IDLE_POLL_SECS)
            return

        logger.info(
            "insight_mlparams_refresher: started — sweep every %ss, ttl %ss",
            get_settings().INSIGHT_MLPARAMS_SWEEP_INTERVAL_SECONDS,
            get_settings().INSIGHT_MLPARAMS_TTL_SECONDS,
        )

        sweep = asyncio.create_task(self._sweep_loop(), name="insight-mlparams-sweep")
        listener = asyncio.create_task(
            self._listener_loop(), name="insight-mlparams-listener"
        )
        drivers = [sweep, listener]
        try:
            done, _ = await asyncio.wait(drivers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and (exc := task.exception()):
                    logger.error(
                        "insight_mlparams_refresher: driver %s raised — restarting: %s",
                        task.get_name(),
                        exc,
                    )
        finally:
            for task in drivers:
                task.cancel()
            await asyncio.gather(*drivers, return_exceptions=True)
        logger.info("insight_mlparams_refresher: exiting (shutdown or driver exit)")

    # ── Sweep driver ─────────────────────────────────────────────────────────

    async def _sweep_loop(self) -> None:
        while not self._shutdown.is_set():
            await self._pause.checkpoint()
            interval = get_settings().INSIGHT_MLPARAMS_SWEEP_INTERVAL_SECONDS
            triggered = await self._trigger.wait_or_timeout(interval)
            if self._shutdown.is_set():
                break
            await self._run_sweep(force=triggered)
            if self._on_cycle is not None:
                self._on_cycle()

    async def _run_sweep(self, *, force: bool) -> None:
        """
        Score open-position instruments whose params are stale (or all, if
        ``force``) and refresh their cache entries.
        """
        t0 = time.monotonic()
        settings = get_settings()
        try:
            all_keys = await self._open_position_instrument_keys()
            if not all_keys:
                insight_mlparams_refresh_runs_total.labels(
                    trigger="sweep", status="skipped_empty"
                ).inc()
                insight_mlparams_last_run_timestamp.set(time.time())
                return

            if force:
                stale = all_keys
            else:
                margin = settings.INSIGHT_MLPARAMS_REFRESH_MARGIN_SECONDS
                stale = [
                    k
                    for k in all_keys
                    if await insight_cache.needs_refresh(
                        self._redis, k, margin_seconds=margin
                    )
                ]

            interrupted = False
            scored = 0
            if stale:
                to_score = stale[: settings.INSIGHT_MLPARAMS_BATCH_CAP]
                if len(stale) > settings.INSIGHT_MLPARAMS_BATCH_CAP:
                    logger.warning(
                        "insight_mlparams_refresher: %d stale instruments exceeds cap "
                        "%d — deferring %d to the next sweep",
                        len(stale),
                        settings.INSIGHT_MLPARAMS_BATCH_CAP,
                        len(stale) - settings.INSIGHT_MLPARAMS_BATCH_CAP,
                    )
                chunk_size = settings.INSIGHT_MLPARAMS_CHUNK_SIZE
                for start in range(0, len(to_score), chunk_size):
                    await self._pause.checkpoint()
                    if self._shutdown.is_set():
                        interrupted = True
                        break
                    chunk = to_score[start : start + chunk_size]
                    scored += await self._score_and_cache(chunk, trigger="sweep")

            duration = time.monotonic() - t0
            insight_mlparams_refresh_runs_total.labels(
                trigger="sweep",
                status="interrupted" if interrupted else "success",
            ).inc()
            insight_mlparams_refresh_duration_seconds.observe(duration)
            insight_mlparams_last_run_timestamp.set(time.time())
            logger.info(
                "insight_mlparams_refresher: sweep %s — open=%d stale=%d scored=%d "
                "duration_ms=%d",
                "interrupted" if interrupted else "complete",
                len(all_keys),
                len(stale),
                scored,
                int(duration * 1000),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a failed sweep must not kill the loop
            insight_mlparams_refresh_runs_total.labels(
                trigger="sweep", status="error"
            ).inc()
            logger.error(
                "insight_mlparams_refresher: sweep failed: %s", exc, exc_info=True
            )

    # ── On-demand driver ───────────────────────────────────────────────────────

    async def _listener_loop(self) -> None:
        """
        Score a single instrument the instant a fill requests it.  Relies on the
        supervisor's CancelledError to break out of the blocking listen on shutdown.
        """
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(RedisChannels.PAPER_INSIGHT_REFRESH_REQUEST)
        logger.info(
            "insight_mlparams_refresher: subscribed to %s",
            RedisChannels.PAPER_INSIGHT_REFRESH_REQUEST,
        )
        try:
            async for message in pubsub.listen():
                if self._shutdown.is_set():
                    break
                if message.get("type") != "message":
                    continue
                await self._pause.checkpoint()
                instrument_key = _parse_instrument_key(message.get("data"))
                if not instrument_key:
                    continue
                await self._handle_ondemand(instrument_key)
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await pubsub.unsubscribe(RedisChannels.PAPER_INSIGHT_REFRESH_REQUEST)
                await pubsub.aclose()
            except Exception:  # noqa: BLE001 — cleanup best-effort
                pass

    async def _handle_ondemand(self, instrument_key: str) -> None:
        """Score one instrument on demand, skipping it if params are still fresh."""
        settings = get_settings()
        try:
            # Dedup repeated opens: only score if not already warm.
            if not await insight_cache.needs_refresh(
                self._redis,
                instrument_key,
                margin_seconds=settings.INSIGHT_MLPARAMS_REFRESH_MARGIN_SECONDS,
            ):
                return
            scored = await self._score_and_cache([instrument_key], trigger="ondemand")
            insight_mlparams_refresh_runs_total.labels(
                trigger="ondemand", status="success"
            ).inc()
            if scored:
                logger.debug(
                    "insight_mlparams_refresher: on-demand scored %s", instrument_key
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — one bad request must not kill the listener
            insight_mlparams_refresh_runs_total.labels(
                trigger="ondemand", status="error"
            ).inc()
            logger.warning(
                "insight_mlparams_refresher: on-demand refresh failed for %s: %s",
                instrument_key,
                exc,
            )

    # ── Shared scoring ─────────────────────────────────────────────────────────

    async def _score_and_cache(self, instrument_keys: list[str], *, trigger: str) -> int:
        """
        Batch-score ``instrument_keys`` and cache params for available snapshots.
        Returns the number of instruments successfully cached.
        """
        if not instrument_keys:
            return 0
        settings = get_settings()
        snapshots = await get_prediction_snapshots_batch(
            instrument_keys=instrument_keys,
            timeframe=_TIMEFRAME,
            predictor=self._predictor,
            session_factory=self._session_factory,
            redis=self._redis,
            feature_concurrency=settings.INSIGHT_MLPARAMS_CHUNK_SIZE,
        )
        ttl = settings.INSIGHT_MLPARAMS_TTL_SECONDS
        now = time.time()
        scored = 0
        for instrument_key in instrument_keys:
            snapshot = snapshots.get(instrument_key)
            if not snapshot or not snapshot.get("available", False):
                reason = (
                    snapshot.get("unavailable_reason", "unknown")
                    if snapshot
                    else "unknown"
                )
                insight_mlparams_snapshot_unavailable_total.labels(reason=reason).inc()
                continue

            prob_up = insight_cache.derive_prob_up(snapshot.get("probabilities"))
            if prob_up is None:
                insight_mlparams_snapshot_unavailable_total.labels(
                    reason="no_probabilities"
                ).inc()
                continue

            sigma = float(snapshot.get("volatility", 0.0) or 0.0)
            await insight_cache.write_mlparams(
                self._redis,
                instrument_key,
                prob_up=prob_up,
                sigma=sigma,
                ttl_seconds=ttl,
                ts=now,
            )
            scored += 1

        if scored:
            insight_mlparams_scored_total.labels(trigger=trigger).inc(scored)
        return scored

    # ── DB access ───────────────────────────────────────────────────────────────

    async def _open_position_instrument_keys(self) -> list[str]:
        """Distinct instrument keys across all OPEN paper positions."""
        from app.models.paper_trading import PaperPosition  # lazy — avoids import cost

        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(PaperPosition.instrument_key)
                    .where(PaperPosition.status == "OPEN")
                    .distinct()
                )
            ).all()
        return [r[0] for r in rows if r[0]]


# ── Helpers ───────────────────────────────────────────────────────────────────────

def _parse_instrument_key(data: Any) -> str | None:
    """Extract ``instrument_key`` from a refresh-request pub/sub payload."""
    if data is None:
        return None
    try:
        payload = json.loads(data)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    key = payload.get("instrument_key")
    return key if isinstance(key, str) and key else None
