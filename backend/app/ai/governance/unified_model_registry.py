"""
Cortex AI — Unified Model Registry  [TOMBSTONE — do not use in new code]
=========================================================================
This module is retained only so that tests can assert its deprecated mutation
methods (``promote_model``, ``demote_model``) raise ``RegistryDeprecatedError``.
No production code should import or instantiate ``UnifiedModelRegistry``.

Architecture note (R6 — 2026-06-02)
-------------------------------------
The "three-table ambiguity" flagged by the 2026-06-01 audit was resolved by A8
(2026-05-23) and formalised by migration 0040 (2026-06-02):

  * ``ml_model_metadata`` — artifact authority: training artifacts, SHA-256
    checksums, CPCV lineage, calibrator manifests, and the ML lifecycle state
    machine (development → staging → production → archived).

  * ``ai_ml_models`` — governance projection: deployment state
    (shadow/paper/live/retired), drift advisory flags, admin override audit
    trail, and a typed FK (``ml_model_metadata_id``) pointing at the
    authoritative ml_model_metadata record for the current serving version.

  * ``unified_model_registry`` (DB table) — DROPPED in migration 0040.
    It was created in 0007 but was never populated by any application code.

``ModelPromoter`` (``app.ml.model_registry``) is the sole promotion authority.
Every lifecycle transition atomically updates both live tables via
``_project_to_ai_ml_models()``, making structural divergence impossible.
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
        # A8 — retired.  This method operated on ai_ml_models with old accuracy-only
        # gates that are inconsistent with the A6 gate suite, and did NOT atomically
        # update ml_model_metadata.  That split-brain was the root cause A8 fixes.
        #
        # Gated promotions:  ModelPromoter.promote_to_production() enforces all A6
        #   gates and atomically projects state to ai_ml_models in the same transaction.
        #   CLI: python scripts/promote_model.py production --version <ver> --model-name <name>
        #
        # Admin force-transitions: POST /governance/models/{id}/state directly updates
        #   ai_ml_models.deployment_state with transition-graph validation and audit trail.
        from app.ml.model_registry import RegistryDeprecatedError
        raise RegistryDeprecatedError(
            "UnifiedModelRegistry.promote_model() is deprecated (A8 — single authority). "
            "Gated promotions use ModelPromoter.promote_to_production(). "
            "Admin force-transitions use POST /governance/models/{id}/state."
        )

    async def demote_model(
        self,
        db:          AsyncSession,
        pubsub:      PubSubClient,
        model_name:  str,
        reason:      str,
    ) -> AIMLModel:
        # A8 — retired.  Advisory demotion recommendations are now written as drift
        # flags by DriftDetector; human operators action them via promote_model.py.
        from app.ml.model_registry import RegistryDeprecatedError
        raise RegistryDeprecatedError(
            "UnifiedModelRegistry.demote_model() is deprecated (A8 — single authority). "
            "DriftDetector writes advisory flags; operators action via promote_model.py."
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
            "shadow":  ["paper"],
            "paper":   ["live", "shadow"],
            "live":    ["shadow"],
            "retired": ["shadow"],   # restore path — re-enters shadow for re-evaluation
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
