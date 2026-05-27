"""
Drift Detector — ML Model Drift Monitoring (D1)

Monitors three complementary signals for the live production model and surfaces
them as an advisory flag consumed by the C2 Promotion Report.  Autonomous
demotion is explicitly forbidden (A8 single-authority invariant).

Signal stack
------------
1. Prediction distribution shift
   Z-score of recent MLPrediction.prediction values vs the training baseline
   stored in MLModelMetadata.training_prediction_stats.  Catches covariate
   shift early, even before enough trades have closed to compute realised metrics.

2. Realised directional accuracy (from PaperTradeOutcome)
   Fraction of closed paper trades where ml_direction_correct is True, computed
   since the model was deployed (deployed_at).  Directly measures whether the
   model's direction calls are landing correctly on live capital.

3. Realised net-of-cost information ratio (from PaperTradeOutcome)
   Mean / std of per-trade net returns (net_pnl / notional) since deployment.
   A negative IR sustained across a meaningful sample is the strongest economic
   signal that the model is deteriorating.

Trigger logic
-------------
Any single signal exceeding its threshold marks the check as drift_detected and
calls _handle_drift_action(), which writes an advisory flag to
AIMLModel.governance_metadata (challenger_recommended=True, drift_recommendation)
and publishes a Redis alert.  The flag is consumed at the next training run by
_generate_promotion_report() in the orchestrator, which surfaces it inside the
C2 Promotion Report for human review.

Invariants
----------
* _handle_drift_action() never mutates deployment_state.  A8 guarantees that
  only ModelPromoter is allowed to change model state.
* Every check writes an AIDriftReport row with all three signal values in
  distribution_metrics, regardless of whether drift was detected.
* Prometheus gauges are updated on every check so dashboards stay current.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIDriftReport, AIMLModel
from app.core.config import get_settings
from app.core.metrics import drift_detections_total
from app.core.redis import PubSubClient, RedisChannels
from app.models.ml_data import MLPrediction, MLModelMetadata
from app.models.paper_trading import PaperTradeOutcome

logger = logging.getLogger(__name__)
settings = get_settings()

# Minimum closed-trade sample required before realised signals fire.
# Below this threshold the signal is reported but the trigger is skipped
# (too few data points for a reliable estimate).
_MIN_TRADES_FOR_REALISED_METRICS: int = 20

# Realised accuracy must drop more than this many percentage points below the
# training accuracy baseline before the signal triggers (10 pp floor prevents
# noise-driven false positives during the early deployment window).
_ACCURACY_DROP_THRESHOLD_PP: float = 0.10

# Sustained information ratio at or below this value triggers the IR signal.
# -0.5 means the model is delivering meaningfully negative risk-adjusted
# returns on paper capital — a clear economic deterioration signal.
_IR_DETERIORATION_THRESHOLD: float = -0.50


class DriftDetector:
    """
    Multi-signal drift detector that monitors the live ML model.

    Bridges the ML prediction system with the AI governance layer:
    - Monitors MLPrediction data for distribution shift (z-score)
    - Monitors PaperTradeOutcome data for realised directional accuracy
    - Monitors PaperTradeOutcome data for realised net-of-cost information ratio
    - Creates AIDriftReport for governance tracking and audit trail
    - Publishes structured alerts via Redis for real-time dashboard consumption
    - Emits Prometheus gauges for Grafana / oncall observability
    - Writes challenger_recommended advisory flag to AIMLModel.governance_metadata
    """

    async def check_drift(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        model_id: int,
        lookback_hours: int = 24,
    ) -> AIDriftReport:
        """
        Run all three drift signals for a model, write the report, emit metrics.

        Parameters
        ----------
        db:
            AsyncSession — caller is responsible for lifecycle.
        pubsub:
            Redis pub/sub client for alert publication.
        model_id:
            ai_ml_models.id of the governance model to check.
        lookback_hours:
            Window (in hours) for the prediction distribution shift signal.
            The realised signals always use the full deployment window
            (PaperTradeOutcome.created_at >= MLModelMetadata.deployed_at).

        Returns
        -------
        The persisted AIDriftReport row.
        """
        # Ensure a clean read of the governance row.
        await db.commit()
        ai_model = (await db.execute(
            select(AIMLModel).where(AIMLModel.id == model_id)
        )).scalar_one_or_none()

        if not ai_model:
            raise ValueError(f"AI Model {model_id} not found")

        # Resolve the governance model → production ML model metadata record.
        # Naming: governance names follow 'cortex_<type>_<tf>' (e.g. 'cortex_xgboost_1d');
        # ML registry names are short ('xgboost', 'gru').
        production_records: list[MLModelMetadata] = list(
            (await db.execute(
                select(MLModelMetadata).where(
                    MLModelMetadata.status  == "production",
                    MLModelMetadata.is_active == True,  # noqa: E712
                )
            )).scalars().all()
        )

        ml_model: MLModelMetadata | None = None
        ml_model = next(
            (m for m in production_records if m.model_name == ai_model.model_name),
            None,
        )
        if ml_model is None:
            ml_model = next(
                (m for m in production_records if m.model_name in ai_model.model_name),
                None,
            )

        if ml_model is None:
            logger.warning(
                "No production MLModelMetadata found for governance model '%s' — "
                "running baseline check (distribution-shift only)",
                ai_model.model_name,
            )
            return await self._baseline_drift_check(db, pubsub, ai_model)

        # ── Signal 1: prediction distribution shift (z-score) ─────────────────
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        recent_preds = list(
            (await db.execute(
                select(MLPrediction).where(
                    MLPrediction.model_id   == ml_model.model_id,
                    MLPrediction.timestamp  >= cutoff,
                ).order_by(MLPrediction.timestamp.desc()).limit(1000)
            )).scalars().all()
        )

        z_score_signal = self._compute_distribution_shift(recent_preds, ml_model)

        # ── Signal 2 & 3: realised metrics from paper-trading outcomes ─────────
        realised = await self._compute_realised_metrics(db, ml_model)

        # ── Composite drift decision ───────────────────────────────────────────
        threshold_sigma = settings.ML_DRIFT_THRESHOLD_SIGMA

        shift_triggered = z_score_signal["z_score"] > threshold_sigma
        accuracy_triggered = (
            realised["min_samples_met"]
            and realised["realised_accuracy"] is not None
            and z_score_signal["baseline_accuracy"] is not None
            and (z_score_signal["baseline_accuracy"] - realised["realised_accuracy"])
            > _ACCURACY_DROP_THRESHOLD_PP
        )
        ir_triggered = (
            realised["min_samples_met"]
            and realised["realised_information_ratio"] is not None
            and realised["realised_information_ratio"] <= _IR_DETERIORATION_THRESHOLD
        )

        drift_detected = shift_triggered or accuracy_triggered or ir_triggered

        # accuracy_drop: reported in the AIDriftReport row for backward compat —
        # now derived from real outcomes instead of the previous heuristic.
        accuracy_drop: Decimal | None = None
        if (
            realised["realised_accuracy"] is not None
            and z_score_signal["baseline_accuracy"] is not None
        ):
            drop = z_score_signal["baseline_accuracy"] - realised["realised_accuracy"]
            accuracy_drop = Decimal(str(round(max(drop, 0.0), 4)))

        # ── Assemble full distribution_metrics ────────────────────────────────
        distribution_metrics: dict[str, Any] = {
            # Signal 1 — distribution shift
            "distribution_shift": {
                "sample_size":     z_score_signal["sample_size"],
                "current_mean":    z_score_signal["current_mean"],
                "current_std":     z_score_signal["current_std"],
                "baseline_mean":   z_score_signal["baseline_mean"],
                "baseline_std":    z_score_signal["baseline_std"],
                "z_score":         z_score_signal["z_score"],
                "threshold_sigma": threshold_sigma,
                "triggered":       shift_triggered,
                "lookback_hours":  lookback_hours,
            },
            # Signal 2 — realised directional accuracy
            "realised_accuracy": {
                "live":               realised["realised_accuracy"],
                "training_baseline":  z_score_signal["baseline_accuracy"],
                "drop_pp":            float(accuracy_drop) if accuracy_drop is not None else None,
                "trade_count":        realised["realised_trade_count"],
                "min_samples_met":    realised["min_samples_met"],
                "triggered":          accuracy_triggered,
                "since":              realised["since"],
            },
            # Signal 3 — realised information ratio
            "realised_net_ir": {
                "information_ratio":   realised["realised_information_ratio"],
                "mean_return_pct":     realised["mean_return_pct"],
                "std_return_pct":      realised["std_return_pct"],
                "trade_count":         realised["realised_trade_count"],
                "min_samples_met":     realised["min_samples_met"],
                "threshold":           _IR_DETERIORATION_THRESHOLD,
                "triggered":           ir_triggered,
                "since":               realised["since"],
            },
        }

        if z_score_signal["sample_size"] < 10:
            distribution_metrics["distribution_shift"]["note"] = (
                "Insufficient MLPrediction data (minimum 10 required)"
            )

        # ── Advisory flag ─────────────────────────────────────────────────────
        action_taken: str | None = None
        if drift_detected:
            action_taken = await self._handle_drift_action(
                db, ai_model, distribution_metrics
            )
            logger.warning(
                "Drift detected for '%s' (model_id=%s): shift=%s accuracy=%s ir=%s",
                ai_model.model_name, model_id,
                shift_triggered, accuracy_triggered, ir_triggered,
            )

        # ── Persist AIDriftReport ─────────────────────────────────────────────
        drift_score_decimal = Decimal(str(round(z_score_signal["z_score"], 4)))
        report = AIDriftReport(
            model_id=model_id,
            report_timestamp=datetime.now(timezone.utc),
            drift_detected=drift_detected,
            drift_score=drift_score_decimal,
            accuracy_drop=accuracy_drop,
            distribution_metrics=distribution_metrics,
            action_taken=action_taken,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)

        # ── Prometheus gauges ─────────────────────────────────────────────────
        self._emit_prometheus(
            model_name=ai_model.model_name,
            drift_score=z_score_signal["z_score"],
            drift_flagged=drift_detected,
            realised=realised,
        )

        # ── Redis alert ───────────────────────────────────────────────────────
        if drift_detected:
            await pubsub.publish_json(RedisChannels.MODELS_DRIFT_ALERTS, {
                "model_id":          model_id,
                "model_name":        ai_model.model_name,
                "drift_score":       z_score_signal["z_score"],
                "accuracy_drop":     float(accuracy_drop) if accuracy_drop else None,
                "triggered_signals": {
                    "distribution_shift":    shift_triggered,
                    "realised_accuracy_drop": accuracy_triggered,
                    "realised_net_ir":        ir_triggered,
                },
                "action":     action_taken,
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "report_id":  report.id,
            })
            logger.warning("Drift alert published for '%s'", ai_model.model_name)
        else:
            logger.info("No drift detected for '%s'", ai_model.model_name)

        return report

    # ── Private: signal computation ───────────────────────────────────────────

    def _compute_distribution_shift(
        self,
        recent_predictions: list[MLPrediction],
        ml_model: MLModelMetadata,
    ) -> dict[str, Any]:
        """Compute z-score of recent prediction distribution vs training baseline."""
        baseline_stats: dict = ml_model.training_prediction_stats or {}
        raw = baseline_stats.get("raw_predictions", {})
        # Explicit None-checks: 0.0 is a valid baseline value and must not be
        # treated as falsy by the `or` idiom.
        _b_mean = (baseline_stats.get("mean") if baseline_stats.get("mean") is not None
                   else raw.get("mean"))
        _b_std  = (baseline_stats.get("std") if baseline_stats.get("std") is not None
                   else raw.get("std"))
        baseline_mean: float = float(_b_mean) if _b_mean is not None else 0.0
        baseline_std: float  = float(_b_std)  if _b_std  is not None else 1.0

        # Training accuracy for the accuracy-drop signal.
        training_metrics: dict = ml_model.training_metrics or {}
        baseline_accuracy: float | None = (
            float(training_metrics["accuracy"])
            if training_metrics.get("accuracy") is not None
            else None
        )

        n = len(recent_predictions)
        if n < 10:
            return {
                "sample_size":      n,
                "current_mean":     None,
                "current_std":      None,
                "baseline_mean":    baseline_mean,
                "baseline_std":     baseline_std,
                "z_score":          0.0,
                "baseline_accuracy": baseline_accuracy,
            }

        values = [float(p.prediction) for p in recent_predictions]
        current_mean = statistics.mean(values)
        current_std  = statistics.stdev(values) if n > 1 else 0.0

        z_score = (
            min(abs(current_mean - baseline_mean) / baseline_std, 10.0)
            if baseline_std > 0
            else 0.0
        )

        return {
            "sample_size":       n,
            "current_mean":      current_mean,
            "current_std":       current_std,
            "baseline_mean":     baseline_mean,
            "baseline_std":      baseline_std,
            "z_score":           z_score,
            "baseline_accuracy": baseline_accuracy,
        }

    async def _compute_realised_metrics(
        self,
        db: AsyncSession,
        ml_model: MLModelMetadata,
    ) -> dict[str, Any]:
        """
        Query PaperTradeOutcome rows since the model was deployed.

        Returns a dict with:
          realised_accuracy         — fraction of correct direction calls (None if no data)
          realised_information_ratio — mean/std of per-trade net returns (None if std == 0)
          mean_return_pct           — mean net return per trade (%)
          std_return_pct            — std dev of net returns per trade (%)
          realised_trade_count      — number of outcomes with ml_direction_correct non-NULL
          min_samples_met           — True when trade_count >= _MIN_TRADES_FOR_REALISED_METRICS
          since                     — ISO timestamp of the deployment boundary (or "all_time")
        """
        deployed_at = ml_model.deployed_at
        since_label = deployed_at.isoformat() if deployed_at else "all_time"

        stmt = select(
            PaperTradeOutcome.net_pnl,
            PaperTradeOutcome.entry_price,
            PaperTradeOutcome.quantity,
            PaperTradeOutcome.ml_direction_correct,
        ).where(
            PaperTradeOutcome.ml_direction_correct.is_not(None)
        )
        if deployed_at is not None:
            stmt = stmt.where(PaperTradeOutcome.created_at >= deployed_at)

        rows = (await db.execute(stmt)).all()
        n = len(rows)

        _empty: dict[str, Any] = {
            "realised_accuracy":          None,
            "realised_information_ratio": None,
            "mean_return_pct":            None,
            "std_return_pct":             None,
            "realised_trade_count":       n,
            "min_samples_met":            False,
            "since":                      since_label,
        }

        if n == 0:
            return _empty

        # Directional accuracy
        correct_flags = [bool(r.ml_direction_correct) for r in rows]
        realised_accuracy = sum(correct_flags) / n

        # Per-trade net return = net_pnl / (entry_price * quantity)
        returns: list[float] = []
        for r in rows:
            notional = float(r.entry_price) * float(r.quantity)
            if notional > 0:
                returns.append(float(r.net_pnl) / notional)

        if not returns:
            return {
                **_empty,
                "realised_accuracy":    realised_accuracy,
                "realised_trade_count": n,
                "min_samples_met":      n >= _MIN_TRADES_FOR_REALISED_METRICS,
                "since":                since_label,
            }

        mean_ret = statistics.mean(returns)
        std_ret  = statistics.stdev(returns) if len(returns) > 1 else 0.0
        ir = mean_ret / std_ret if std_ret > 0 else None

        return {
            "realised_accuracy":          realised_accuracy,
            "realised_information_ratio": ir,
            "mean_return_pct":            mean_ret * 100.0,
            "std_return_pct":             std_ret  * 100.0,
            "realised_trade_count":       n,
            "min_samples_met":            n >= _MIN_TRADES_FOR_REALISED_METRICS,
            "since":                      since_label,
        }

    # ── Private: advisory flag ─────────────────────────────────────────────────

    async def _handle_drift_action(
        self,
        db: AsyncSession,
        model: AIMLModel,
        distribution_metrics: dict[str, Any] | None = None,
    ) -> str:
        """Write an advisory drift flag to governance_metadata.

        A8 invariant — this method never mutates deployment_state.
        Only ModelPromoter is allowed to change model state.

        Sets governance_metadata["challenger_recommended"] = True and records a
        structured drift_recommendation so the C2 Promotion Report (_generate_
        promotion_report in the orchestrator) can surface it for human review.
        Operators action it via promote_model.py demote.
        """
        _RECOMMENDED_DEMOTION: dict[str, str] = {
            "live":    "paper",
            "paper":   "shadow",
            "shadow":  "shadow",
            "retired": "retired",
        }
        recommended_state = _RECOMMENDED_DEMOTION.get(model.deployment_state, "shadow")

        # Capture which signals triggered so the operator has full context.
        dm = distribution_metrics or {}
        triggered_signals: list[str] = []
        if dm.get("distribution_shift", {}).get("triggered"):
            triggered_signals.append("distribution_shift")
        if dm.get("realised_accuracy", {}).get("triggered"):
            triggered_signals.append("realised_accuracy_drop")
        if dm.get("realised_net_ir", {}).get("triggered"):
            triggered_signals.append("realised_net_ir")

        meta = dict(model.governance_metadata or {})
        meta["challenger_recommended"] = True
        meta["drift_recommendation"] = {
            "current_state":     model.deployment_state,
            "recommended_state": recommended_state,
            "flagged_at":        datetime.now(timezone.utc).isoformat(),
            "reason":            "drift_threshold_exceeded",
            "triggered_signals": triggered_signals,
        }
        model.governance_metadata = meta
        model.updated_at          = datetime.now(timezone.utc)
        await db.commit()

        drift_detections_total.labels(
            model_id=str(model.model_id), action_taken="drift_flagged"
        ).inc()
        logger.warning(
            "Drift advisory flag set for '%s' "
            "(state=%s → recommended=%s, signals=%s) — "
            "human action required via promote_model.py",
            model.model_name, model.deployment_state,
            recommended_state, triggered_signals,
        )
        return "drift_flagged"

    # ── Private: Prometheus emission ───────────────────────────────────────────

    @staticmethod
    def _emit_prometheus(
        *,
        model_name: str,
        drift_score: float,
        drift_flagged: bool,
        realised: dict[str, Any],
    ) -> None:
        """Update all D1 Prometheus gauges.  Silently swallows import errors so
        that environments without prometheus_client do not crash the check."""
        try:
            from app.ml.monitoring.metrics import (
                ml_model_drift_score,
                ml_model_drift_flagged,
                ml_model_live_realised_sharpe,
                ml_model_live_directional_accuracy,
                ml_model_live_trade_count,
            )
            ml_model_drift_score.labels(model_name=model_name).set(drift_score)
            ml_model_drift_flagged.labels(model_name=model_name).set(1 if drift_flagged else 0)

            ir = realised.get("realised_information_ratio")
            if ir is not None:
                ml_model_live_realised_sharpe.labels(model_name=model_name).set(ir)

            acc = realised.get("realised_accuracy")
            if acc is not None:
                ml_model_live_directional_accuracy.labels(model_name=model_name).set(acc)

            count = realised.get("realised_trade_count", 0)
            ml_model_live_trade_count.labels(model_name=model_name).set(count)

        except Exception:  # pragma: no cover — prometheus unavailable in some test envs
            logger.debug("Prometheus metrics unavailable — skipping gauge update for '%s'", model_name)

    # ── Private: baseline fallback ─────────────────────────────────────────────

    async def _baseline_drift_check(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        ai_model: AIMLModel,
    ) -> AIDriftReport:
        """
        Minimal check when no production MLModelMetadata record can be found.

        No realised signals are possible without a known deployment window, so
        only a structural baseline report is written.  drift_detected is always
        False here — we lack the data to make any claim.
        """
        model_age_hours = (
            (datetime.now(timezone.utc) - ai_model.created_at).total_seconds() / 3600
        )
        distribution_metrics: dict[str, Any] = {
            "baseline_only": True,
            "model_age_hours": model_age_hours,
            "note": (
                "No production MLModelMetadata found — only governance metadata available. "
                "No realised signals can be computed."
            ),
        }

        report = AIDriftReport(
            model_id=ai_model.id,
            report_timestamp=datetime.now(timezone.utc),
            drift_detected=False,
            drift_score=Decimal("0.0"),
            accuracy_drop=None,
            distribution_metrics=distribution_metrics,
            action_taken=None,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)
        return report
