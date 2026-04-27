"""
Cortex AI — Unified Model Registry
=====================================
Manages ML model lifecycle with governance gates and state machine.

State machine:  shadow → paper → live
                any    → shadow  (demotion / rollback)

Promotion gates are calibrated for financial binary-classification on
noisy equity data — not generic ML benchmarks.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIMLModel
from app.core.redis import PubSubClient, RedisChannels

logger = logging.getLogger(__name__)

# ── Promotion gates ───────────────────────────────────────────────────────────
# Financial binary-classification on equity price data is inherently noisy.
# 65% accuracy is strong; 85%+ thresholds belong to curated benchmark datasets,
# not live market data.  These thresholds reflect domain reality.

_SHADOW_TO_PAPER_MIN_ACCURACY  = Decimal("0.55")   # meaningfully above random
_PAPER_TO_LIVE_MIN_ACCURACY    = Decimal("0.58")
_PAPER_TO_LIVE_MIN_PRECISION   = Decimal("0.53")
_PAPER_TO_LIVE_MIN_RECALL      = Decimal("0.50")


class UnifiedModelRegistry:
    """
    Manages ML model lifecycle in the governance table.

    Responsibilities:
    - Register models (initial state: shadow)
    - Enforce promotion gates (shadow → paper → live)
    - Publish state-change events to Redis
    - Support demotion and rollback
    """

    # ── registration ──────────────────────────────────────────────────────────

    async def register_model(
        self,
        db:              AsyncSession,
        model_name:      str,
        model_type:      str,
        model_version:   str,
        timeframe:       str,
        artifact_bytes:  bytes | None,
        metrics:         dict[str, Any],
        metadata:        dict[str, Any] | None = None,
        initial_state:   str = "shadow",
    ) -> AIMLModel:
        """
        Register a model in the governance registry.

        Args:
            artifact_bytes: Raw model bytes for checksum.  Pass None when the
                            artifact lives on disk (Option-B storage) and only
                            the checksum is needed from governance_metadata.
            initial_state:  Starting deployment state.  'shadow' for new models;
                            'live' when promoting an already-validated model.
        """
        checksum = hashlib.sha256(artifact_bytes).hexdigest() if artifact_bytes else None

        model = AIMLModel(
            model_name         = model_name,
            model_type         = model_type,
            deployment_state   = initial_state,
            model_version      = model_version,
            timeframe          = timeframe,
            artifact_sha256    = checksum,
            artifact_encrypted = None,   # Option-B: no DB-stored artifact
            training_date      = datetime.now(timezone.utc),
            accuracy           = Decimal(str(round(metrics.get("accuracy",  0.0), 4))),
            precision          = Decimal(str(round(metrics.get("precision", 0.0), 4))),
            recall             = Decimal(str(round(metrics.get("recall",    0.0), 4))),
            f1_score           = Decimal(str(round(metrics.get("f1_score",  0.0), 4))),
            governance_metadata = metadata or {},
        )
        db.add(model)
        await db.commit()
        await db.refresh(model)

        logger.info(
            "Registered model: name=%s version=%s state=%s accuracy=%.4f",
            model_name, model_version, initial_state, float(model.accuracy or 0),
        )
        return model

    # ── promotion ─────────────────────────────────────────────────────────────

    async def promote_model(
        self,
        db:                  AsyncSession,
        pubsub:              PubSubClient,
        model_name:          str,
        target_state:        str,
        evaluation_results:  dict[str, Any] | None = None,
        bypass_gates:        bool = False,
    ) -> AIMLModel:
        """
        Promote (or demote) a model with governance gate enforcement.

        Valid transitions:
            shadow → paper
            paper  → live
            any    → shadow  (demotion)

        Args:
            bypass_gates: Skip quality thresholds.  Use only when the model
                          has already passed an external quality gate (e.g.
                          ml_model_registry promotion pipeline).
        """
        stmt = select(AIMLModel).where(AIMLModel.model_name == model_name)
        model = (await db.execute(stmt)).scalar_one_or_none()
        if model is None:
            raise ValueError(f"Model '{model_name}' not found in governance registry")

        from_state = model.deployment_state
        self._assert_valid_transition(from_state, target_state)

        if not bypass_gates:
            self._check_gates(model, target_state)

        model.deployment_state = target_state
        model.updated_at       = datetime.now(timezone.utc)

        if evaluation_results:
            meta = dict(model.governance_metadata or {})
            meta["evaluation_results"] = evaluation_results
            meta["promoted_at"]        = datetime.now(timezone.utc).isoformat()
            model.governance_metadata  = meta

        await db.commit()
        await db.refresh(model)

        await pubsub.publish_json(RedisChannels.MODELS_STATE_CHANGES, {
            "action":        "model_state_changed",
            "model_name":    model_name,
            "from_state":    from_state,
            "to_state":      target_state,
            "model_version": model.model_version,
            "timeframe":     model.timeframe,
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        })

        logger.info("Model state changed: %s  %s → %s", model_name, from_state, target_state)
        return model

    async def demote_model(
        self,
        db:          AsyncSession,
        pubsub:      PubSubClient,
        model_name:  str,
        reason:      str,
    ) -> AIMLModel:
        return await self.promote_model(
            db=db,
            pubsub=pubsub,
            model_name=model_name,
            target_state="shadow",
            evaluation_results={"demotion_reason": reason},
            bypass_gates=True,
        )

    # ── queries ───────────────────────────────────────────────────────────────

    async def get_active_models(
        self,
        db:        AsyncSession,
        state:     str = "live",
        timeframe: str | None = None,
    ) -> list[AIMLModel]:
        stmt = select(AIMLModel).where(AIMLModel.deployment_state == state)
        if timeframe:
            stmt = stmt.where(AIMLModel.timeframe == timeframe)
        stmt = stmt.order_by(AIMLModel.updated_at.desc())
        return list((await db.execute(stmt)).scalars().all())

    async def get_model(self, db: AsyncSession, model_name: str) -> AIMLModel | None:
        return (await db.execute(
            select(AIMLModel).where(AIMLModel.model_name == model_name)
        )).scalar_one_or_none()

    # ── private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _assert_valid_transition(from_state: str, to_state: str) -> None:
        valid: dict[str, list[str]] = {
            "shadow": ["paper"],
            "paper":  ["live", "shadow"],
            "live":   ["shadow"],
        }
        if to_state not in valid.get(from_state, []):
            raise ValueError(
                f"Invalid state transition: {from_state} → {to_state}. "
                f"Valid from '{from_state}': {valid.get(from_state, [])}"
            )

    @staticmethod
    def _check_gates(model: AIMLModel, target_state: str) -> None:
        acc  = model.accuracy  or Decimal("0")
        prec = model.precision or Decimal("0")
        rec  = model.recall    or Decimal("0")

        if target_state == "paper":
            if acc < _SHADOW_TO_PAPER_MIN_ACCURACY:
                raise ValueError(
                    f"shadow→paper gate failed: accuracy {acc} < {_SHADOW_TO_PAPER_MIN_ACCURACY}"
                )

        elif target_state == "live":
            failures = []
            if acc  < _PAPER_TO_LIVE_MIN_ACCURACY:
                failures.append(f"accuracy {acc} < {_PAPER_TO_LIVE_MIN_ACCURACY}")
            if prec < _PAPER_TO_LIVE_MIN_PRECISION:
                failures.append(f"precision {prec} < {_PAPER_TO_LIVE_MIN_PRECISION}")
            if rec  < _PAPER_TO_LIVE_MIN_RECALL:
                failures.append(f"recall {rec} < {_PAPER_TO_LIVE_MIN_RECALL}")
            if failures:
                raise ValueError("paper→live gate failed: " + "; ".join(failures))
