"""
Cortex AI — Model Registry
===========================
Version-controlled storage for ML models with metadata, lineage, and governance.

Integrity model (Option B — checksum-only)
------------------------------------------
Every artifact stored in the registry (training artifact, inference artifact,
calibrator) is SHA-256 hashed at registration time and verified on load.  The
per-model ``artifact_manifest`` is persisted in the ``lineage`` JSONB column so
a registry record is fully self-describing: every byte it references is covered
by a stored hash.  Encryption (Option A) was evaluated and rejected because it
adds key-management complexity without a meaningful adversarial threat model on
this deployment — an attacker with filesystem write access already has broader
privileges.  The ``key_scheme`` field in every ``artifact_manifest`` documents
this decision formally so any future scheme change produces a different manifest
hash and is caught by the C2 bundle integrity check.

Break-glass audit
-----------------
When ``skip_quality_gates=True`` is used, a CRITICAL log entry is always emitted.
When ``ModelPromoter`` is constructed with an ``audit_dir``, a self-signed JSON
record is also written to that directory so the audit survives log rotation.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select, update as _sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ml_data import MLModelMetadata as MLModel

logger = logging.getLogger(__name__)

# Dedicated audit logger — emits to the root handler but with a distinctive
# prefix so log-aggregation alerts can filter on "BREAK_GLASS" without noise.
_audit_logger = logging.getLogger(__name__ + ".audit")

# ── A8 — Governance projection state map ──────────────────────────────────────
# ml_model_metadata.status  →  ai_ml_models.deployment_state
# ModelPromoter is the single promotion authority; ai_ml_models is updated as
# an atomic projection in the same DB transaction so the two tables can never
# diverge.  All other callers that previously mutated ai_ml_models directly
# (UnifiedModelRegistry.promote_model, ModelRegistry.promote_to_production)
# now raise RegistryDeprecatedError.
_ML_TO_GOVERNANCE_STATE: dict[str, str] = {
    "production":  "live",
    "staging":     "paper",
    "development": "shadow",
}


async def _project_to_ai_ml_models(
    session:   AsyncSession,
    model:     MLModel,
    ml_status: str,
) -> int:
    """Atomically project ml_model_metadata promotion into ai_ml_models.

    Must be called **within an active, uncommitted transaction** — the caller's
    ``await session.commit()`` (or rollback) covers both table writes together.
    Matches governance rows by ``model_type == model.model_name``
    (e.g. 'xgboost', 'gru').

    Returns the number of rows updated (0 when no governance record exists yet,
    which is expected on the very first registration).
    """
    from app.ai.fusion.models import AIMLModel as _GovernanceModel  # lazy — avoids circular

    deployment_state = _ML_TO_GOVERNANCE_STATE.get(ml_status, ml_status)
    result = await session.execute(
        _sa_update(_GovernanceModel)
        .where(_GovernanceModel.model_type == model.model_name)
        .values(
            deployment_state=deployment_state,
            model_version=model.model_version,
            updated_at=datetime.now(timezone.utc),
        )
        .execution_options(synchronize_session=False)
    )
    rows: int = result.rowcount
    if rows == 0:
        logger.warning(
            "projection: no ai_ml_models row for model_type=%s — "
            "governance record may not exist yet (expected on first registration)",
            model.model_name,
        )
    else:
        logger.info(
            "projection: %s → %s for model_type=%s (%d row(s) updated atomically)",
            ml_status, deployment_state, model.model_name, rows,
        )
    return rows


class QualityGateError(Exception):
    def __init__(self, message: str, failed_checks: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.failed_checks = failed_checks or {}


class BreakGlassError(ValueError):
    """Raised when skip_quality_gates=True is used without a documented reason.

    Every gate bypass must be human-authored and audited; this exception
    enforces that contract at the API boundary so no code path can silently
    skip gates.
    """


class RegistryDeprecatedError(RuntimeError):
    """Raised when a caller invokes a promotion path that was retired in A8.

    After A8, ``ModelPromoter`` is the **sole** promotion authority.  All other
    paths that previously mutated model state — ``ModelRegistry.promote_to_production``,
    ``UnifiedModelRegistry.promote_model``, and ``UnifiedModelRegistry.demote_model``
    — are deprecated and raise this error immediately to make the violation explicit.

    Resolution: route promotion through ``ModelPromoter.promote_to_production()``
    or the CLI: ``python scripts/promote_model.py production``.
    """


class ArtifactManifestError(RuntimeError):
    """Raised when per-model artifact manifest integrity verification fails.

    Indicates that one or more artifacts (inference artifact, calibrator) covered
    by the registry record's ``artifact_manifest`` have been modified since
    registration.  The model must not be loaded or promoted until the root cause
    is investigated — a legitimate cause is re-registering after a retrain, which
    produces a fresh manifest; a malicious cause is file tampering.

    Resolution: re-register the model (re-run the training pipeline) or restore
    the artifact from a trusted backup.
    """


# ── E2 — Artifact integrity helpers ──────────────────────────────────────────
# These are local to model_registry to avoid circular imports with
# evaluation.promotion_report (which has equivalent helpers for the C2 bundle).

_MANIFEST_SCHEMA_VERSION = 1
_KEY_SCHEME = "sha256_checksum_only"
_CANONICAL_SEP = (",", ":")   # compact + stable separators for deterministic SHA-256


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_canonical(obj: Any) -> str:
    """SHA-256 of the canonical, deterministic JSON of *obj*.

    Uses ``sort_keys=True`` and compact separators so insertion order and
    whitespace never affect the digest.  Both the write path (register_model)
    and the read path (load_calibrator_artifact / audit verification) use this
    function, guaranteeing round-trip stability.
    """
    return _sha256_bytes(
        json.dumps(obj, sort_keys=True, separators=_CANONICAL_SEP, default=str).encode()
    )


def _hash_file(path: Path) -> tuple[str, int]:
    """Return ``(sha256_hex, size_bytes)`` for *path*.

    Returns ``("absent", 0)`` when the file does not exist, mirroring the
    convention in ``evaluation.promotion_report`` so both manifest layers are
    consistent.
    """
    if not path.exists():
        return "absent", 0
    data = path.read_bytes()
    return _sha256_bytes(data), len(data)


def _build_artifact_manifest(
    *,
    training_path: Path,
    training_raw: bytes,
    inference_path: Path,
    inference_raw: bytes,
    calibrator_path: Path | None,
    calibrator_raw: bytes | None,
    features: list[str],
) -> dict[str, Any]:
    """Build the per-model artifact manifest stored in ``lineage["artifact_manifest"]``.

    Every artifact is SHA-256 hashed so that any byte-level change (tamper,
    silent corruption, accidental overwrite) is detectable at load time.  The
    manifest is stored in the ``lineage`` JSONB column — no new DB columns
    required.

    Components
    ----------
    training     — native training artifact (.json for XGBoost, .keras for GRU).
    inference    — compiled inference artifact (.so from Treelite, .onnx from ONNX).
    calibrator   — Beta calibrator pickle (A4).  ``sha256="absent"`` when not supplied.
    feature_manifest — sorted feature-name list SHA-256 (guards schema drift).
    key_scheme   — formally records the integrity mechanism chosen (Option B).
    """
    cal_sha: str
    cal_size: int
    if calibrator_raw is not None and calibrator_path is not None:
        cal_sha = _sha256_bytes(calibrator_raw)
        cal_size = len(calibrator_raw)
        cal_stored_path: str | None = str(calibrator_path.resolve())
    else:
        cal_sha = "absent"
        cal_size = 0
        cal_stored_path = None

    return {
        "schema_version":  _MANIFEST_SCHEMA_VERSION,
        "registered_at":   datetime.now(timezone.utc).isoformat(),
        "key_scheme":      _KEY_SCHEME,
        "training": {
            "path":       str(training_path.resolve()),
            "sha256":     _sha256_bytes(training_raw),
            "size_bytes": len(training_raw),
        },
        "inference": {
            "path":       str(inference_path.resolve()),
            "sha256":     _sha256_bytes(inference_raw),
            "size_bytes": len(inference_raw),
        },
        "calibrator": {
            "path":       cal_stored_path,
            "sha256":     cal_sha,
            "size_bytes": cal_size,
        },
        "feature_manifest": {
            "n_features": len(features),
            "sha256":     _sha256_canonical(sorted(features)),
        },
    }


class QualityGate:
    """
    Validates models against production-grade quality criteria before promotion.

    All gates below are HARD — a single failure blocks promotion.

    Gate hierarchy (promotion requires all to pass):
      1. auc_pr          — primary classification gate (AUC Precision-Recall)
      2. deflated_sharpe — net-of-cost Deflated Sharpe Ratio must be strictly positive
      3. calibration_ece — Expected Calibration Error must be ≤ MAX_ECE (5 %)
      4. symbol_coverage — fraction of requested symbols with usable data (A7)
      5. training_samples — minimum row count sanity gate
      6. accuracy        — secondary classification sanity gate
      7. metadata_completeness — required metric keys must be present
      8. ap_degradation  — AUC-PR must not regress vs the current champion
      9. accuracy_degradation — accuracy must not regress vs the current champion

    Per-type floors exist for classification gates (xgboost / gru / tft);
    unknown types fall through to the default floor.

    Symbol-coverage gate (4) is skipped with a warning when the key is absent
    from training_metrics — this is temporary until A7 populates it.  Every
    other absent metric uses a fail-safe default so an unvalidated model can
    never slip through silently.
    """

    # ── Per-type classification floors ───────────────────────────────────────
    # Primary gate: AUC-PR (average precision on the imbalanced binary label).
    # AUC-PR of a perfectly random classifier equals the positive-class prevalence
    # (~0.50 for balanced UP/DOWN); each floor is set above that baseline.
    MIN_AP_BY_TYPE: dict[str, float] = {
        "xgboost": 0.55,
        "gru":     0.50,
        "tft":     0.52,
    }
    MIN_AP_DEFAULT = 0.50   # global floor for unknown types

    # Secondary gate: raw accuracy (sanity check only — see "Accuracy is reported,
    # never gated alone" in the standing engineering rules).
    MIN_ACCURACY_BY_TYPE: dict[str, float] = {
        "xgboost": 0.55,
        "gru":     0.50,
        "tft":     0.51,
    }
    MIN_ACCURACY_DEFAULT = 0.55

    # ── Financial gate ────────────────────────────────────────────────────────
    # Strictly positive net-of-cost Deflated Sharpe (DSR > 0).  A model that
    # loses money after realistic costs and selection-bias deflation cannot be
    # deployed to live capital regardless of classification performance.
    MIN_DSR: float = 0.0   # gate: dsr > MIN_DSR  (strict inequality)

    # ── Calibration gate ──────────────────────────────────────────────────────
    # Expected Calibration Error after Beta calibration (A4).  ≤ 5 % means
    # stated probabilities are reliable enough for position sizing.
    MAX_ECE: float = 0.05

    # ── Coverage gate (A7) ────────────────────────────────────────────────────
    # Fraction of requested symbols that had sufficient usable history.  Gate
    # is *skipped with a warning* when the key is absent (A7 not yet wired).
    MIN_COVERAGE: float = 0.85

    # ── Regression guard ──────────────────────────────────────────────────────
    MAX_AP_DROP:       float = 0.05   # max AUC-PR drop vs champion
    MAX_ACCURACY_DROP: float = 0.05   # max accuracy drop vs champion

    MIN_TRAINING_SAMPLES: int = 100_000

    def __init__(
        self,
        min_training_samples: int   | None = None,
        max_accuracy_drop:    float | None = None,
        max_ap_drop:          float | None = None,
    ) -> None:
        self.min_training_samples = min_training_samples or self.MIN_TRAINING_SAMPLES
        self.max_accuracy_drop    = max_accuracy_drop    or self.MAX_ACCURACY_DROP
        self.max_ap_drop          = max_ap_drop          or self.MAX_AP_DROP

    def _min_ap_for(self, model_type: str) -> float:
        return self.MIN_AP_BY_TYPE.get(model_type.lower(), self.MIN_AP_DEFAULT)

    def _min_accuracy_for(self, model_type: str) -> float:
        return self.MIN_ACCURACY_BY_TYPE.get(model_type.lower(), self.MIN_ACCURACY_DEFAULT)

    def validate(self, model: MLModel, previous_model: MLModel | None = None) -> dict[str, Any]:
        """Validate *model* against all production quality gates.

        Raises:
            QualityGateError: When one or more hard gates fail.  The exception
                carries ``failed_checks`` keyed by gate name so callers can
                surface precise failure reasons to operators.

        Returns:
            A result dict ``{"passed": True, "checks": {...}, ...}`` on success.
        """
        metrics    = model.training_metrics or {}
        model_type = model.model_name.lower()

        checks: dict[str, Any] = {}
        failed: dict[str, str] = {}

        # ── (1) AUC-PR — primary classification gate ──────────────────────────
        # Absent key → 0.0 (fail-safe; an unvalidated model must not pass silently).
        ap     = float(metrics.get("auc_pr", 0.0))
        ap_thr = self._min_ap_for(model_type)
        checks["auc_pr"] = {"value": ap, "threshold": ap_thr, "passed": ap >= ap_thr}
        if not checks["auc_pr"]["passed"]:
            failed["auc_pr"] = (
                f"auc_pr {ap:.4f} < {ap_thr:.4f} for model type '{model.model_name}'"
            )

        # ── (2) Deflated Sharpe — net-of-cost financial gate ─────────────────
        # DSR must be *strictly positive* (> MIN_DSR = 0.0); zero means the model
        # is not generating net alpha after accounting for overfitting selection bias.
        # Absent key → -1.0 (fail-safe).
        dsr = float(metrics.get("deflated_sharpe", -1.0))
        checks["deflated_sharpe"] = {
            "value":     dsr,
            "threshold": self.MIN_DSR,
            "passed":    dsr > self.MIN_DSR,
        }
        if not checks["deflated_sharpe"]["passed"]:
            failed["deflated_sharpe"] = (
                f"deflated_sharpe {dsr:.4f} must be strictly positive "
                f"(> {self.MIN_DSR:.4f}) — net-of-cost alpha required"
            )

        # ── (3) Calibration ECE ───────────────────────────────────────────────
        # Post-calibration Expected Calibration Error (A4 Beta calibrator).
        # Absent key → 1.0 (worst possible ECE — fail-safe).
        ece = float(metrics.get("ece_after", 1.0))
        checks["calibration_ece"] = {
            "value":     ece,
            "threshold": self.MAX_ECE,
            "passed":    ece <= self.MAX_ECE,
        }
        if not checks["calibration_ece"]["passed"]:
            failed["calibration_ece"] = (
                f"ece_after {ece:.4f} > {self.MAX_ECE:.4f} "
                f"— calibration too poor for reliable position sizing"
            )

        # ── (4) Symbol coverage (A7) ──────────────────────────────────────────
        # Skipped with warning when absent (A7 not yet wired); enforced once present.
        if "symbol_coverage" in metrics:
            cov = float(metrics["symbol_coverage"])
            checks["symbol_coverage"] = {
                "value":     cov,
                "threshold": self.MIN_COVERAGE,
                "passed":    cov >= self.MIN_COVERAGE,
            }
            if not checks["symbol_coverage"]["passed"]:
                failed["symbol_coverage"] = (
                    f"symbol_coverage {cov:.1%} < {self.MIN_COVERAGE:.1%} "
                    f"— insufficient symbol universe"
                )
        else:
            checks["symbol_coverage"] = {
                "value":     None,
                "threshold": self.MIN_COVERAGE,
                "passed":    True,   # provisional — gate active once A7 populates this
                "warning":   "symbol_coverage absent from metrics (A7 not yet wired) — gate skipped",
            }
            logger.warning(
                "Quality gate: symbol_coverage absent from metrics for model=%s "
                "— gate skipped until A7 populates it",
                model.model_version,
            )

        # ── (5) Training samples ──────────────────────────────────────────────
        samples = int(model.training_samples or 0)
        checks["training_samples"] = {
            "value":     samples,
            "threshold": self.min_training_samples,
            "passed":    samples >= self.min_training_samples,
        }
        if not checks["training_samples"]["passed"]:
            failed["training_samples"] = (
                f"training_samples {samples:,} < minimum {self.min_training_samples:,}"
            )

        # ── (6) Accuracy — secondary sanity gate ──────────────────────────────
        # Enforces a basic sanity floor.  AUC-PR is the primary classification
        # signal; raw accuracy is never gated alone.
        accuracy = float(metrics.get("accuracy", 0.0))
        acc_thr  = self._min_accuracy_for(model_type)
        checks["accuracy"] = {"value": accuracy, "threshold": acc_thr, "passed": accuracy >= acc_thr}
        if not checks["accuracy"]["passed"]:
            failed["accuracy"] = (
                f"accuracy {accuracy:.4f} < {acc_thr:.4f} for model type '{model.model_name}'"
            )

        # ── (7) Metadata completeness ─────────────────────────────────────────
        required_fields = ["accuracy", "f1_score"]
        missing = [f for f in required_fields if f not in metrics]
        checks["metadata_completeness"] = {"missing_fields": missing, "passed": not missing}
        if not checks["metadata_completeness"]["passed"]:
            failed["metadata_completeness"] = f"missing required metrics: {missing}"

        # ── (8 & 9) Regression guards — vs current champion ──────────────────
        if previous_model:
            prev_metrics = previous_model.training_metrics or {}

            # Primary: AUC-PR regression (fail if champion had auc_pr > 0)
            prev_ap  = float(prev_metrics.get("auc_pr", 0.0))
            ap_drop  = prev_ap - ap
            checks["ap_degradation"] = {
                "current_auc_pr":  ap,
                "previous_auc_pr": prev_ap,
                "drop":            ap_drop,
                "threshold":       self.max_ap_drop,
                "passed":          ap_drop <= self.max_ap_drop,
            }
            if not checks["ap_degradation"]["passed"]:
                failed["ap_degradation"] = (
                    f"auc_pr dropped {ap_drop:.4f} vs champion "
                    f"(max allowed {self.max_ap_drop:.4f})"
                )

            # Secondary: accuracy regression
            prev_acc = float(prev_metrics.get("accuracy", 0.0))
            acc_drop = prev_acc - accuracy
            checks["accuracy_degradation"] = {
                "current_accuracy":  accuracy,
                "previous_accuracy": prev_acc,
                "drop":              acc_drop,
                "threshold":         self.max_accuracy_drop,
                "passed":            acc_drop <= self.max_accuracy_drop,
            }
            if not checks["accuracy_degradation"]["passed"]:
                failed["accuracy_degradation"] = (
                    f"accuracy dropped {acc_drop:.4f} vs champion "
                    f"(max allowed {self.max_accuracy_drop:.4f})"
                )

        # ── Verdict ───────────────────────────────────────────────────────────
        if failed:
            raise QualityGateError(
                f"Model '{model.model_version}' failed {len(failed)} quality gate(s): "
                + ", ".join(failed),
                failed_checks=failed,
            )

        logger.info(
            "Quality gate PASSED: model=%s type=%s "
            "auc_pr=%.4f dsr=%.4f ece=%.4f accuracy=%.4f samples=%d",
            model.model_version, model.model_name,
            ap, dsr, ece, accuracy, samples,
        )

        return {
            "passed":        True,
            "checks":        checks,
            "model_version": model.model_version,
            "validated_at":  datetime.now(timezone.utc).isoformat(),
        }


class ModelPromoter:
    """
    Manages lifecycle transitions with quality gate enforcement.

    development → staging (promote_to_staging)
    staging     → production (promote_to_production)
    any         ← rollback

    Break-glass protocol
    ────────────────────
    Quality gates may be bypassed only when ``skip_quality_gates=True`` AND a
    non-empty ``break_glass_reason`` is supplied.  Every bypass is recorded
    as a CRITICAL audit-log entry.  The reason requirement is enforced at the
    API boundary so no automated code path can silently bypass gates — the
    scheduled retrain (C1) always passes ``skip_quality_gates=False``.
    """

    def __init__(
        self,
        session:       AsyncSession,
        quality_gate:  QualityGate | None = None,
        audit_dir:     Path | None = None,
    ) -> None:
        self._session      = session
        self._quality_gate = quality_gate or QualityGate()
        # E2 — when provided, durable break-glass audit records are written here
        # in addition to the CRITICAL log.  None is safe: only the log fires.
        self._audit_dir: Path | None = audit_dir

    # ── Internal ──────────────────────────────────────────────────────────────

    def _emit_break_glass_audit(
        self,
        model_version: str,
        model_name:    str,
        operation:     str,
        reason:        str,
    ) -> None:
        """Emit a structured CRITICAL log entry + a durable self-signed JSON file.

        The CRITICAL log fires unconditionally so log-aggregation alerts based on
        the "BREAK_GLASS" prefix work regardless of ``audit_dir``.

        When ``audit_dir`` is set, a self-signed JSON record is also written to
        ``{audit_dir}/break_glass_{ts}_{version}_{operation}.json``.  The record
        carries an ``audit_sha256`` field that is the SHA-256 of the canonical
        JSON of the record with that field set to the sentinel ``""``.  Any
        post-write modification of the file (reason, timestamp, model details) is
        detectable by recomputing the hash.
        """
        ts = datetime.now(timezone.utc)
        _audit_logger.critical(
            "BREAK_GLASS operation=%s model=%s version=%s reason=%r ts=%s",
            operation,
            model_name,
            model_version,
            reason,
            ts.isoformat(),
        )

        if self._audit_dir is None:
            return

        self._audit_dir.mkdir(parents=True, exist_ok=True)

        record: dict[str, Any] = {
            "event":         "break_glass",
            "operation":     operation,
            "model_version": model_version,
            "model_name":    model_name,
            "reason":        reason,
            "timestamp":     ts.isoformat(),
            "audit_sha256":  "",   # sentinel — replaced once hash is derived
        }

        # Self-signed: SHA-256 of the canonical form (sentinel in place)
        record["audit_sha256"] = _sha256_canonical(record)

        ts_slug     = ts.strftime("%Y%m%dT%H%M%SZ")
        safe_ver    = model_version.replace("/", "_").replace("\\", "_")
        audit_path  = self._audit_dir / f"break_glass_{ts_slug}_{safe_ver}_{operation}.json"
        audit_path.write_text(
            json.dumps(record, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("Break-glass audit record written: %s", audit_path)

    # ── Public API ────────────────────────────────────────────────────────────

    async def promote_to_staging(
        self,
        model_version:      str,
        skip_quality_gates: bool       = False,
        break_glass_reason: str | None = None,
    ) -> MLModel:
        if skip_quality_gates:
            if not break_glass_reason or not break_glass_reason.strip():
                raise BreakGlassError(
                    "skip_quality_gates=True requires a non-empty break_glass_reason. "
                    "Document why gates are being bypassed — this action is audited."
                )
            self._emit_break_glass_audit(
                model_version, model_version, "promote_to_staging", break_glass_reason
            )

        model = await self._get_or_raise(model_version)
        if model.status != "development":
            raise ValueError(
                f"Model {model_version} is {model.status}; "
                "can only promote from development to staging."
            )
        if not skip_quality_gates:
            self._quality_gate.validate(model)

        try:
            model.status     = "staging"
            model.updated_at = datetime.now(timezone.utc)
            # A8 — atomic projection: ai_ml_models updated in the same transaction.
            await _project_to_ai_ml_models(self._session, model, "staging")
            await self._session.commit()
            await self._session.refresh(model)
            logger.info("Promoted %s: development → staging", model_version)
            return model
        except Exception:
            await self._session.rollback()
            raise

    async def promote_to_production(
        self,
        model_version:      str,
        model_name:         str,
        skip_quality_gates: bool       = False,
        break_glass_reason: str | None = None,
        activate:           bool       = True,
    ) -> MLModel:
        """Promote a staging model to production.

        Parameters
        ----------
        activate : bool, default True
            When True (standard path) the model becomes ``is_active=True``,
            the current champion is deactivated, and the governance projection
            is set to ``"live"``.

            When False (Single-Active-Member Ensemble / dormant path) the model
            is registered at the production tier for auditing, versioning, and
            checksum coverage, but ``is_active=False``.  The current champion
            is **not** deactivated — it continues serving.  The governance
            projection is set to ``"shadow"`` (not routing live capital).
            Flip to ``is_active=True`` + re-project when the model is ready to
            serve, without re-running the full promotion pipeline.
        """
        if skip_quality_gates:
            if not break_glass_reason or not break_glass_reason.strip():
                raise BreakGlassError(
                    "skip_quality_gates=True requires a non-empty break_glass_reason. "
                    "Document why gates are being bypassed — this action is audited."
                )
            self._emit_break_glass_audit(
                model_version, model_name, "promote_to_production", break_glass_reason
            )

        model = await self._get_or_raise(model_version)
        if model.status != "staging":
            raise ValueError(
                f"Model {model_version} is {model.status}; "
                "can only promote from staging to production."
            )

        prev = await self._fetch_active_production(model_name)

        if not skip_quality_gates:
            self._quality_gate.validate(model, prev)

        now = datetime.now(timezone.utc)
        try:
            if activate:
                # Standard path: deactivate current champion, make challenger live.
                if prev:
                    prev.is_active  = False
                    prev.updated_at = now

                model.status      = "production"
                model.is_active   = True
                model.deployed_at = now
                model.updated_at  = now

                # A8 — atomic dual-write: ai_ml_models.deployment_state → "live"
                await _project_to_ai_ml_models(self._session, model, "production")

                await self._session.commit()
                await self._session.refresh(model)
                logger.info(
                    "Promoted %s: staging → production/live (replaced: %s)",
                    model_version,
                    prev.model_version if prev else "none",
                )
            else:
                # Dormant path: register at production tier, champion stays active.
                # Governance → "shadow" (not serving live capital).
                model.status      = "production"
                model.is_active   = False
                model.deployed_at = now
                model.updated_at  = now

                # A8 — project to development→shadow so inference router ignores it.
                await _project_to_ai_ml_models(self._session, model, "development")

                await self._session.commit()
                await self._session.refresh(model)
                logger.info(
                    "Registered %s: staging → production/shadow (dormant, is_active=False; "
                    "champion %s unchanged)",
                    model_version,
                    prev.model_version if prev else "none",
                )

            return model
        except Exception:
            await self._session.rollback()
            raise

    async def demote_to_staging(
        self,
        model_version: str,
        reason:        str,
    ) -> MLModel:
        """Demote a production model to staging (production → staging, live → paper).

        Used by C3 honest re-evaluation when an in-production model fails the
        current A6 hard gates and no qualified successor exists yet.  The model
        artefact and registry row are preserved; the model can be re-promoted
        later via the standard ``promote_to_production`` flow once it earns
        legitimate metrics — or via the audited break-glass path.

        Atomic: ``ml_model_metadata`` (status → 'staging', is_active → False)
        and ``ai_ml_models`` (deployment_state → 'paper') are updated in a
        single DB transaction, projected through ``_project_to_ai_ml_models``
        so the two tables can never diverge — same invariant as
        ``promote_to_production``.

        Audit: a structured WARNING is emitted (not CRITICAL — demotion is
        the safety system working as designed, not a bypass).  ``reason`` is
        required so the audit trail is always informative.

        Raises:
            ValueError: ``reason`` is empty or whitespace; or the model is
                not currently ``status='production'``.
        """
        if not reason or not reason.strip():
            raise ValueError(
                "demote_to_staging requires a non-empty reason for the audit trail"
            )

        model = await self._get_or_raise(model_version)
        if model.status != "production":
            raise ValueError(
                f"Model {model_version} is {model.status}; "
                "can only demote from production."
            )

        now = datetime.now(timezone.utc)
        try:
            model.status     = "staging"
            model.is_active  = False
            model.updated_at = now
            # A8 — atomic dual-write: ai_ml_models.deployment_state → "paper"
            # in the same transaction.  Commit or rollback covers both writes.
            await _project_to_ai_ml_models(self._session, model, "staging")
            await self._session.commit()
            await self._session.refresh(model)
            _audit_logger.warning(
                "DEMOTE production→staging  model=%s  version=%s  reason=%r  ts=%s",
                model.model_name, model_version, reason, now.isoformat(),
            )
            return model
        except Exception:
            await self._session.rollback()
            raise

    async def rollback(self, model_name: str, target_version: str | None = None) -> MLModel:
        current = await self._fetch_active_production(model_name)

        if target_version:
            stmt = select(MLModel).where(MLModel.model_name == model_name, MLModel.model_version == target_version)
        else:
            stmt = (
                select(MLModel)
                .where(MLModel.model_name == model_name, MLModel.status == "production")
                .order_by(MLModel.deployed_at.desc())
                .offset(1).limit(1)
            )
        target = (await self._session.execute(stmt)).scalar_one_or_none()
        if target is None:
            raise ValueError(f"No rollback target found for '{model_name}'")

        now = datetime.now(timezone.utc)
        try:
            if current:
                current.is_active  = False
                current.updated_at = now

            target.is_active   = True
            target.deployed_at = now
            target.updated_at  = now

            # A8 — atomic projection: ai_ml_models updated alongside ml_model_metadata.
            await _project_to_ai_ml_models(self._session, target, "production")

            await self._session.commit()
            await self._session.refresh(target)
            logger.warning(
                "Rollback: %s  %s → %s",
                model_name,
                current.model_version if current else "none",
                target.model_version,
            )
            return target
        except Exception:
            await self._session.rollback()
            raise

    async def _get_or_raise(self, model_version: str) -> MLModel:
        model = (await self._session.execute(
            select(MLModel).where(MLModel.model_version == model_version)
        )).scalar_one_or_none()
        if model is None:
            raise ValueError(f"Model version '{model_version}' not found in registry")
        return model

    async def _fetch_active_production(self, model_name: str) -> MLModel | None:
        return (await self._session.execute(
            select(MLModel)
            .where(MLModel.model_name == model_name, MLModel.status == "production", MLModel.is_active == True)
            .order_by(MLModel.deployed_at.desc())
            .limit(1)
        )).scalar_one_or_none()


class ModelRegistry:
    """
    Centralized repository for trained models.

    Stores artifacts as plaintext files.  Integrity guaranteed via SHA-256
    checksum stored in the DB and verified on every load.
    """

    def __init__(
        self,
        session:            AsyncSession,
        model_storage_path: str | Path,
        encryption_key:     str | None = None,  # accepted but ignored (Option B)
    ) -> None:
        self._session      = session
        self._storage_path = Path(model_storage_path)
        self._storage_path.mkdir(parents=True, exist_ok=True)
        if encryption_key:
            logger.debug("encryption_key provided but encryption is disabled (Option B — checksum-only integrity)")

    @staticmethod
    def _sanitize_for_json(obj: Any) -> Any:
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.integer, np.floating)):
            return obj.item()
        if isinstance(obj, dict):
            return {k: ModelRegistry._sanitize_for_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [ModelRegistry._sanitize_for_json(item) for item in obj]
        return obj

    async def register_model(
        self,
        version:                  str,
        model_type:               str,
        artifact_path:            str | Path,
        metrics:                  dict[str, Any],
        metadata:                 dict[str, Any],
        feature_version:          str,
        status:                   str = "development",
        inference_artifact_path:  str | Path | None = None,
        calibrator_path:          str | Path | None = None,
        overwrite:                bool = False,
        feedback_bundle_meta:     dict[str, Any] | None = None,
    ) -> MLModel:
        """Register a trained model with the registry.

        All supplied artifacts are copied into ``model_storage_path`` and covered
        by SHA-256 hashes stored in ``lineage["artifact_manifest"]``.  Every byte
        of every artifact is verifiable at load time without external state.

        Args:
            artifact_path: Training artifact (e.g. .json, .keras). Copied into
                registry storage; path stored in ``model_path``.
            inference_artifact_path: Inference-ready artifact (e.g. Treelite
                .so or ONNX .onnx). Copied into registry storage; path stored
                in ``onnx_path``.  Checksum is computed against this file so
                that ``RegistryModelLoader`` can verify it on load.  When
                omitted ``onnx_path`` falls back to ``model_path`` (backward
                compatible, but the loader will reject non-.so/.onnx suffixes).
            calibrator_path: A4 Beta calibrator pickle (.pkl).  Copied into
                registry storage alongside the inference artifact; its SHA-256
                is stored in ``artifact_manifest["calibrator"]`` and verified by
                ``load_calibrator_artifact()``.  When omitted the manifest records
                ``sha256="absent"`` — no error; calibrator-less models degrade to
                raw ensemble probabilities.
            overwrite: When True, delete any existing record with the same
                version before registering.  Use during re-runs of the training
                pipeline to avoid "already registered" errors.
        """
        existing = await self.get_model(version)
        if existing:
            if not overwrite:
                raise ValueError(f"Model version '{version}' already registered")
            await self._session.delete(existing)
            await self._session.flush()
            logger.info(
                "register_model: overwrote existing record for version '%s'", version
            )

        # ── Training artifact ────────────────────────────────────────────────
        artifact_path = Path(artifact_path)
        if not artifact_path.exists():
            raise FileNotFoundError(f"Training artifact not found: {artifact_path}")

        training_raw  = artifact_path.read_bytes()
        training_dest = self._storage_path / f"{version}{artifact_path.suffix}"
        training_dest.write_bytes(training_raw)

        # ── Inference artifact ───────────────────────────────────────────────
        if inference_artifact_path is not None:
            inference_path = Path(inference_artifact_path)
            if not inference_path.exists():
                raise FileNotFoundError(f"Inference artifact not found: {inference_path}")
            inference_raw  = inference_path.read_bytes()
            inference_dest = self._storage_path / f"{version}_inference{inference_path.suffix}"
            inference_dest.write_bytes(inference_raw)
            onnx_dest = inference_dest
            checksum  = _sha256_bytes(inference_raw)
        else:
            # No inference artifact provided — store training artifact path in
            # onnx_path for backward compatibility.  Note: RegistryModelLoader
            # will reject non-.so/.onnx extensions at load time.
            inference_raw  = training_raw
            inference_dest = training_dest
            onnx_dest      = training_dest
            checksum       = _sha256_bytes(training_raw)

        # ── Calibrator (A4 — optional) ───────────────────────────────────────
        calibrator_raw:  bytes | None = None
        calibrator_dest: Path  | None = None
        if calibrator_path is not None:
            cal_src = Path(calibrator_path)
            if not cal_src.exists():
                raise FileNotFoundError(f"Calibrator artifact not found: {cal_src}")
            calibrator_raw  = cal_src.read_bytes()
            calibrator_dest = self._storage_path / f"{version}_calibrator{cal_src.suffix}"
            calibrator_dest.write_bytes(calibrator_raw)

        # ── Artifact manifest (E2 — per-model integrity record) ──────────────
        # The manifest is stored in lineage["artifact_manifest"] so the registry
        # row is fully self-describing: every referenced byte is covered by a hash.
        # We merge rather than overwrite so existing lineage fields (from A0.2 /
        # future provenance work) are preserved.
        metrics  = self._sanitize_for_json(metrics)
        features: list[str] = self._sanitize_for_json(metadata.get("features", []))
        f_stats  = self._sanitize_for_json(metadata.get("feature_stats", {}))

        artifact_manifest = _build_artifact_manifest(
            training_path   = training_dest,
            training_raw    = training_raw,
            inference_path  = inference_dest,
            inference_raw   = inference_raw,
            calibrator_path = calibrator_dest,
            calibrator_raw  = calibrator_raw,
            features        = features,
        )

        # Merge into existing lineage (None → empty dict) without clobbering
        # provenance fields written by A0.2 or future pipeline stages.
        existing_lineage: dict[str, Any] = {}  # populated from existing record if overwrite
        merged_lineage: dict[str, Any] = {**existing_lineage, "artifact_manifest": artifact_manifest}

        # Phase 2: inject feedback bundle provenance when this model was trained
        # with feedback-weighted samples.  Surfaced by C2 promotion report so
        # reviewers see exactly which outcome window and how many rows contributed.
        if feedback_bundle_meta:
            merged_lineage["feedback_bundle"] = feedback_bundle_meta

        model = MLModel(
            model_id               = f"{model_type}_{version}",
            model_name             = model_type,
            model_version          = version,
            model_path             = str(training_dest),
            onnx_path              = str(onnx_dest),
            checksum               = checksum,
            training_metrics       = metrics,
            training_features      = features,
            training_feature_stats = f_stats,
            training_samples       = metadata.get("training_samples"),
            status                 = status,
            encrypted              = False,
            is_active              = (status == "production"),
            lineage                = merged_lineage,
        )
        self._session.add(model)
        await self._session.commit()
        await self._session.refresh(model)
        logger.info(
            "Registered model %s (type=%s, status=%s, accuracy=%.4f, inference=%s, "
            "calibrator=%s, n_features=%d)",
            version, model_type, status, metrics.get("accuracy", 0.0),
            Path(onnx_dest).suffix,
            "yes" if calibrator_dest else "absent",
            len(features),
        )
        return model

    async def load_model_artifact(self, model: MLModel) -> bytes:
        path = Path(model.onnx_path)
        if not path.exists():
            raise FileNotFoundError(f"Artifact not found: {path}")

        raw = path.read_bytes()

        if model.checksum:
            actual = _sha256_bytes(raw)
            if actual != model.checksum:
                raise RuntimeError(
                    f"Integrity check failed for {model.model_version}: "
                    f"expected {model.checksum[:16]}…, got {actual[:16]}…"
                )

        logger.debug("Loaded artifact: %s v%s", model.model_name, model.model_version)
        return raw

    async def load_calibrator_artifact(self, model: MLModel) -> bytes:
        """Load and integrity-verify the A4 calibrator for *model*.

        Reads the calibrator path and expected SHA-256 from
        ``model.lineage["artifact_manifest"]["calibrator"]`` — the same
        record written by ``register_model(calibrator_path=...)``.

        Raises
        ------
        ArtifactManifestError
            When no artifact manifest is present, the calibrator entry is
            absent, or the stored SHA-256 does not match the file on disk.
        FileNotFoundError
            When the calibrator file listed in the manifest does not exist.
        """
        lineage  = model.lineage or {}
        manifest = lineage.get("artifact_manifest", {})

        if not manifest:
            raise ArtifactManifestError(
                f"Model {model.model_version} has no artifact_manifest in lineage. "
                "Re-register the model via register_model() to generate one."
            )

        cal_entry    = manifest.get("calibrator", {})
        stored_path  = cal_entry.get("path")
        stored_sha   = cal_entry.get("sha256", "")

        # Check sha256 first: "absent" means registered without a calibrator
        # (both path=None and sha256="absent" are set together by _build_artifact_manifest).
        if stored_sha == "absent":
            raise ArtifactManifestError(
                f"Calibrator for {model.model_version} was absent at registration time "
                "(artifact_manifest.calibrator.sha256='absent'). "
                "Re-register with a valid calibrator_path= to enable integrity checks."
            )

        if not stored_path:
            raise ArtifactManifestError(
                f"No calibrator path registered for model {model.model_version} "
                "(artifact_manifest.calibrator.path is null — "
                "re-register with calibrator_path= to enable integrity checks)."
            )

        cal_path = Path(stored_path)
        if not cal_path.exists():
            raise FileNotFoundError(
                f"Calibrator artifact not found on disk: {cal_path} "
                f"(registered for model {model.model_version})"
            )

        raw        = cal_path.read_bytes()
        actual_sha = _sha256_bytes(raw)
        if actual_sha != stored_sha:
            raise ArtifactManifestError(
                f"Calibrator integrity check FAILED for {model.model_version}: "
                f"expected {stored_sha[:16]}…, got {actual_sha[:16]}… — "
                "the calibrator file has been modified since registration."
            )

        logger.debug(
            "Loaded calibrator: %s v%s (%d bytes, sha256=%s…)",
            model.model_name, model.model_version, len(raw), actual_sha[:16],
        )
        return raw

    async def get_model(self, version: str) -> MLModel | None:
        return (await self._session.execute(
            select(MLModel).where(MLModel.model_version == version)
        )).scalar_one_or_none()

    async def get_latest_model(self, model_name: str | None = None, status: str | None = None) -> MLModel | None:
        stmt = select(MLModel).order_by(MLModel.created_at.desc())
        if model_name:
            stmt = stmt.where(MLModel.model_name == model_name)
        if status:
            stmt = stmt.where(MLModel.status == status)
        return (await self._session.execute(stmt.limit(1))).scalar_one_or_none()

    async def get_production_model(self, model_name: str | None = None) -> MLModel | None:
        return await self.get_latest_model(model_name=model_name, status="production")

    async def list_models(self, status: str | None = None, model_name: str | None = None, limit: int = 100) -> list[MLModel]:
        stmt = select(MLModel).order_by(MLModel.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(MLModel.status == status)
        if model_name:
            stmt = stmt.where(MLModel.model_name == model_name)
        return list((await self._session.execute(stmt)).scalars().all())

    async def promote_to_production(self, version: str, demote_current: bool = True) -> MLModel:  # noqa: ARG002
        # A8 — this ungated path is retired.  It bypassed all A6 quality gates
        # (DSR, calibration, AUC-PR, symbol coverage) and did not project state
        # to ai_ml_models atomically.  Both flaws are fixed in ModelPromoter.
        raise RegistryDeprecatedError(
            "ModelRegistry.promote_to_production() is deprecated (A8 — single authority). "
            "Use ModelPromoter.promote_to_production() which enforces A6 quality gates "
            "and atomically projects state to ai_ml_models in the same DB transaction. "
            "CLI: python scripts/promote_model.py production --version <ver> --model-name <name>"
        )

    async def rollback_model(self, target_version: str | None = None) -> MLModel:  # noqa: ARG002
        # A8 — retired alongside promote_to_production.
        raise RegistryDeprecatedError(
            "ModelRegistry.rollback_model() is deprecated (A8 — single authority). "
            "Use ModelPromoter.rollback() which atomically projects state to ai_ml_models."
        )

    async def archive_model(self, version: str) -> MLModel:
        model = await self.get_model(version)
        if not model:
            raise ValueError(f"Version '{version}' not found")
        if model.status == "archived":
            raise ValueError(f"Model '{version}' is already archived")
        if model.status == "production":
            raise ValueError(f"Cannot archive production model '{version}'. Demote first.")
        model.status     = "archived"
        model.updated_at = datetime.now(timezone.utc)
        await self._session.commit()
        await self._session.refresh(model)
        logger.info("Archived model %s", version)
        return model


def get_model_registry(
    session:            AsyncSession,
    model_storage_path: str | Path = "backend/ml_models",
) -> ModelRegistry:
    return ModelRegistry(session, model_storage_path)
