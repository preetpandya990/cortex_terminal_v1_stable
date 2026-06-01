"""
C2 — Champion / Challenger Promotion Report
============================================

Produces a signed, immutable Promotion Report for every challenger training
run.  The report is the mandatory human-sign-off artefact consumed by
``promote_model.py production --report-path <path>`` before any production
promotion is allowed.

Architecture
------------
* **Pure computation** — no DB calls, no async.  The orchestrator (async) or a
  standalone caller supplies all ``MLModelMetadata`` objects and drives I/O.
* **Immutable after write** — the bundle SHA-256 covers every model inference
  artefact, every calibrator, the feature manifest, the lineage JSONB, and the
  report body itself (with the hash field set to the sentinel ``""`` to avoid
  circularity).  A single byte change to any component produces a different
  digest.
* **Two-layer integrity** — ``bundle_manifest`` records per-component SHA-256
  hashes; the top-level ``bundle_sha256`` is SHA-256 of the canonical JSON of
  that manifest.  Verification re-derives both layers independently.
* **Report-only** — never mutates registry state.  Promotion is actioned by
  the operator via ``promote_model.py production --report-path <path>``.

Report lifecycle
----------------
1. Orchestrator step-10 writes the report for every challenger run.
2. Operator reads the report, reviews deltas vs the champion.
3. Operator calls ``promote_model.py production --report-path <path>``.
4. The CLI loads + verifies the report (bundle checksum + status gate).
5. Promotion proceeds or is blocked, with an interactive confirmation step.

Status semantics
----------------
``AWAITING_HUMAN_SIGNOFF``
    All challengers passed the A6 QualityGate.  The report is valid for
    production promotion via the standard CLI path.

``BLOCKED``
    One or more challengers failed the A6 QualityGate.  The report is written
    for audit purposes but cannot drive a standard promotion.  The audited
    break-glass path (``--skip-gates --reason``) must be used explicitly.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.ml.model_registry import QualityGate, QualityGateError

logger = logging.getLogger(__name__)

# Compact, sort-key-stable separators make every JSON serialisation
# deterministic regardless of insertion order — critical for reproducible
# SHA-256 hashes.
_CANONICAL_SEP = (",", ":")

# Report and bundle manifest fields injected AFTER the body hash is computed.
# These must be stripped and/or reset to the sentinel when re-deriving the
# body hash during verification.
_POST_HASH_FIELDS = frozenset({"bundle_manifest", "report_path"})


# ── Status enum ────────────────────────────────────────────────────────────────

class PromotionStatus(str, enum.Enum):
    """Overall report verdict consumed by ``promote_model.py``."""
    BLOCKED                = "BLOCKED"
    AWAITING_HUMAN_SIGNOFF = "AWAITING_HUMAN_SIGNOFF"


# ── Exceptions ─────────────────────────────────────────────────────────────────

class BundleChecksumError(ValueError):
    """Raised when bundle integrity verification fails.

    Indicates that one or more artefacts covered by the report's signed
    manifest have been modified since the report was generated.  The
    promotion is blocked; the operator must re-run training to obtain a
    fresh signed report.
    """


class BlockedChallengerError(ValueError):
    """Raised when a BLOCKED promotion report is presented at promotion time.

    The challenger failed one or more A6 quality gates.  The operator must
    retrain with corrective actions or use the audited break-glass path
    (``--skip-gates --reason``).
    """


# ── Hash primitives ────────────────────────────────────────────────────────────

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_canonical(obj: Any) -> str:
    """SHA-256 of the canonical, deterministic JSON serialisation of *obj*.

    Uses ``sort_keys=True`` and compact separators so that key insertion
    order and whitespace never affect the digest.  ``default=str`` converts
    non-JSON-serialisable scalars (e.g. numpy types) to their string
    representation; both sides of the hash computation use the same function,
    so round-trip stability is guaranteed.
    """
    serialised = json.dumps(
        obj, sort_keys=True, separators=_CANONICAL_SEP, default=str
    )
    return _sha256_bytes(serialised.encode())


def _sha256_file(path: Path) -> tuple[str, int]:
    """Return ``(sha256_hex, size_bytes)`` for *path*.

    Returns ``("absent", 0)`` when the file does not exist so that the
    manifest always records a value and the bundle hash changes whenever a
    file disappears between report generation and verification.
    """
    if not path.exists():
        return "absent", 0
    data = path.read_bytes()
    return _sha256_bytes(data), len(data)


def _compute_bundle_sha256(bundle_manifest: dict[str, Any]) -> str:
    """Derive the top-level bundle SHA-256 from the component manifest.

    This is the single, canonical implementation used at both write time
    (``build_promotion_report``) and verify time (``load_and_verify_report``).
    Callers must not reimplement this derivation.
    """
    return _sha256_canonical(bundle_manifest)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _calibrator_path_for(meta: Any) -> Path | None:
    """Derive the A4 calibrator path from an ``MLModelMetadata`` record.

    Mirrors the convention in ``RegistryModelLoader._try_load_calibrator``
    and ``reeval_production_model._expected_calibrator_path``:
    ``{onnx_dir}/calibrator_{first_3_letters_of_model_name}.pkl``.
    """
    if not meta.onnx_path:
        return None
    suffix = (meta.model_name or "")[:3].lower()
    return Path(meta.onnx_path).parent / f"calibrator_{suffix}.pkl"


def _hash_artifact(path: Path | None) -> dict[str, Any]:
    """Build a single artefact entry for the bundle manifest."""
    if path is None:
        return {"path": None, "sha256": "absent", "size_bytes": 0}
    sha, size = _sha256_file(path)
    return {"path": str(path.resolve()), "sha256": sha, "size_bytes": size}


def _build_bundle_manifest(
    *,
    members: dict[str, Any],
    report_body_no_hash: dict[str, Any],
) -> dict[str, Any]:
    """Build the per-component hash manifest for bundle integrity.

    Components
    ----------
    inference_artifacts  — compiled inference files (.so / .onnx) per model.
    calibrators          — Beta calibrator pickles (A4) per model.
    feature_manifest     — sorted feature-name list from the first challenger.
    lineage              — JSONB provenance dict from the first challenger.
    report_body          — the report body with ``bundle_sha256`` = ``""``.

    Missing files are recorded with ``sha256="absent"`` so the top-level
    ``bundle_sha256`` changes whenever any component disappears between report
    generation and verification.
    """
    manifest: dict[str, Any] = {
        "inference_artifacts": {},
        "calibrators":         {},
        "feature_manifest":    {},
        "lineage":             {},
        "report_body":         {},
    }

    for name, meta in members.items():
        # Inference artefact (.so from Treelite or .onnx from ONNX export)
        inf_path = Path(meta.onnx_path) if meta.onnx_path else None
        manifest["inference_artifacts"][name] = _hash_artifact(inf_path)

        # Calibrator pickle (A4 — lives beside the inference artefact)
        cal_path = _calibrator_path_for(meta)
        manifest["calibrators"][name] = _hash_artifact(cal_path)

    # Feature manifest — use the first challenger (all share the same schema)
    first_meta = next(iter(members.values()))
    features = sorted(first_meta.training_features or [])
    manifest["feature_manifest"] = {
        "n_features": len(features),
        "sha256":     _sha256_canonical(features),
    }

    # Lineage JSONB — use the first challenger's provenance record
    lineage = first_meta.lineage or {}
    manifest["lineage"] = {"sha256": _sha256_canonical(lineage)}

    # Report body (sentinel already in place: bundle_sha256 = "")
    manifest["report_body"] = {"sha256": _sha256_canonical(report_body_no_hash)}

    return manifest


def _extract_body_for_hash(report: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the report body dict as it was when the bundle hash was computed.

    Removes post-hash fields (``bundle_manifest``, ``report_path``) and
    resets ``bundle_sha256`` to the sentinel ``""`` so that
    ``_sha256_canonical`` reproduces the original body hash.
    """
    body = {k: v for k, v in report.items() if k not in _POST_HASH_FIELDS}
    body["bundle_sha256"] = ""
    return body


def _verify_artifact_group(
    group: dict[str, Any],
    *,
    group_label: str,
    violations: list[str],
) -> None:
    """Re-hash every artefact in a bundle manifest group, recording mismatches."""
    for name, entry in group.items():
        stored_sha  = entry.get("sha256", "")
        stored_path = entry.get("path")

        if stored_sha == "absent":
            # Artefact was absent when the report was generated; acceptable.
            continue
        if not stored_path:
            continue

        artifact = Path(stored_path)
        if not artifact.exists():
            violations.append(
                f"{group_label} '{name}': file missing at {artifact} "
                f"(expected sha256 {stored_sha[:16]}…)"
            )
            continue

        actual_sha, _ = _sha256_file(artifact)
        if actual_sha != stored_sha:
            violations.append(
                f"{group_label} '{name}': SHA-256 mismatch at {artifact} — "
                f"stored {stored_sha[:16]}…  actual {actual_sha[:16]}… "
                "(artefact has been modified since the report was generated)"
            )


def _build_operator_guidance(
    status: str,
    challenger_sections: dict[str, Any],
    champions: dict[str, Any | None],
    drift_advisory: dict[str, Any] | None = None,
) -> str:
    """Compose copy-pasteable operator instructions for the report."""
    if status == PromotionStatus.BLOCKED.value:
        blocked_items = [
            (name, sec["gate"])
            for name, sec in challenger_sections.items()
            if sec["gate_status"] == PromotionStatus.BLOCKED.value
        ]
        lines = [
            "One or more challengers failed the A6 quality gate.",
            "This report cannot be used for a standard production promotion.",
            "Remediation options:",
            "",
        ]
        for name, gate in blocked_items:
            failed = gate.get("failed_checks") or {}
            lines.append(f"  {name}: failed gates = {', '.join(failed) or '(see gate field)'}")
        lines += [
            "",
            "Options:",
            "  1. Retrain with corrective actions and use the new report.",
            "  2. Audited break-glass (CRITICAL audit entry emitted):",
            "     python scripts/promote_model.py production \\",
            "       --version <ver> --model-name <name> \\",
            "       --report-path <this_report_path> \\",
            "       --skip-gates --reason '<documented justification>'",
        ]
        return "\n".join(lines)

    lines = [
        "All quality gates passed.  Promote via (one command pair per model):",
        "",
    ]
    for name, sec in challenger_sections.items():
        version = sec["model_version"]
        champ   = champions.get(name)
        champ_note = (
            f"  # replaces champion {champ.model_version}" if champ else "  # first production model"
        )
        lines += [
            f"  # ── {name} ──",
            f"  python scripts/promote_model.py staging --version {version}",
            champ_note,
            f"  python scripts/promote_model.py production \\",
            f"    --version {version} --model-name {name} \\",
            f"    --report-path <this_report_path>",
            "",
        ]

    # D1 — Drift advisory warnings appended after promotion commands so the
    # operator sees them even on a gate-passing report.
    if drift_advisory:
        flagged = [
            name for name, info in drift_advisory.items()
            if info and info.get("challenger_recommended")
        ]
        if flagged:
            lines += [
                "── DRIFT ADVISORY ──────────────────────────────────────────",
                "The following live champion model(s) have an active drift flag.",
                "Review the drift_advisory section before promoting the challenger,",
                "and consider demoting the champion first if the signals are severe:",
                "",
            ]
            for name in flagged:
                info = drift_advisory[name]
                rec  = info.get("drift_recommendation", {})
                lines += [
                    f"  {name}:",
                    f"    flagged_at:        {rec.get('flagged_at', 'unknown')}",
                    f"    recommended_state: {rec.get('recommended_state', 'paper')}",
                    f"    triggered_signals: {', '.join(rec.get('triggered_signals', []))}",
                    f"    Demote champion:  python scripts/promote_model.py demote \\",
                    f"      --version <champion_version> \\",
                    f"      --reason 'drift_threshold_exceeded: {', '.join(rec.get('triggered_signals', []))}'",
                    "",
                ]

    return "\n".join(lines)


# ── Public API ─────────────────────────────────────────────────────────────────

def build_promotion_report(
    *,
    challengers:    dict[str, Any],
    champions:      dict[str, Any | None],
    quality_gate:   QualityGate | None = None,
    report_dir:     Path | str,
    run_id:         str | None = None,
    drift_advisory: dict[str, Any] | None = None,
    mlflow_run_id:  str | None = None,
    event_backtest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and write a signed Champion/Challenger Promotion Report.

    This is the C2 deliverable: a single JSON file per training run that an
    operator reviews before running ``promote_model.py production``.

    Parameters
    ----------
    challengers:
        Mapping ``{model_name → MLModelMetadata}`` for each freshly registered
        challenger (``status = 'development'``).  Typically ``{"xgboost": ...,
        "gru": ...}``.
    champions:
        Mapping ``{model_name → MLModelMetadata | None}`` for the current
        production models of the same types.  ``None`` when no champion exists
        yet (first deployment).
    quality_gate:
        ``QualityGate`` instance to run against each challenger.  Defaults to
        ``QualityGate()`` (production thresholds).
    report_dir:
        Directory where the signed JSON report is written.  Created if absent.
    run_id:
        ``CheckpointManager.run_id`` for traceability.  ``None`` is accepted
        (e.g. in tests).
    drift_advisory:
        D1 — Per-model drift flag snapshot for each champion, keyed by model
        name.  Pre-fetched by the async orchestrator from AIMLModel.governance_
        metadata and the latest AIDriftReport.  ``None`` or ``{}`` when no
        champion exists yet or no drift check has been run.  Included verbatim
        in the signed report body so any post-generation tampering is detected
        by the bundle SHA-256.
    mlflow_run_id:
        E3 — MLflow run_id for the training run that produced these challengers.
        Enables operators to correlate the promotion report with the full
        MLflow lineage (params, metrics, step durations, artifacts) in
        ``backend/mlruns/``.  ``None`` when MLflow tracking is unavailable.
        Included in the signed body so any post-generation modification is
        detected by the bundle SHA-256.
    event_backtest:
        F1 — ``EventBacktestReport.as_dict()`` from the event-driven backtest
        engine.  Included verbatim in the signed report body so any
        post-generation tampering is detected by the bundle SHA-256.  ``None``
        or ``{}`` when the F1 engine was unavailable or failed (non-fatal).
        Operators should review ``agreement_status`` and
        ``sharpe_divergence_relative_pct`` before promoting.

    Returns
    -------
    The complete report dict (identical to the written JSON file, parsed back
    to Python primitives).

    Notes
    -----
    The function reads inference artefacts and calibrator files from disk to
    compute the bundle SHA-256 — it is therefore **not** a pure function, but
    it never writes to the database or modifies any model state.
    """
    qg  = quality_gate or QualityGate()
    now = datetime.now(timezone.utc).isoformat()

    _DELTA_KEYS = ("auc_pr", "deflated_sharpe", "pbo", "ece_after")

    challenger_sections: dict[str, Any] = {}
    champion_sections:   dict[str, Any] = {}
    delta_sections:      dict[str, Any] = {}
    dsr_sections:        dict[str, Any] = {}
    lineage_sections:    dict[str, Any] = {}
    any_blocked = False

    for name, chal in challengers.items():
        chal_metrics  = chal.training_metrics  or {}
        champ         = champions.get(name)
        champ_metrics = (champ.training_metrics or {}) if champ else {}

        # ── A6 QualityGate ────────────────────────────────────────────────
        try:
            gate_result = qg.validate(chal, champ)
            gate_status = PromotionStatus.AWAITING_HUMAN_SIGNOFF.value
        except QualityGateError as exc:
            gate_result = {
                "passed":        False,
                "failed_checks": exc.failed_checks,
                "message":       str(exc),
            }
            gate_status = PromotionStatus.BLOCKED.value
            any_blocked = True

        challenger_sections[name] = {
            "model_version":    chal.model_version,
            "model_name":       chal.model_name,
            "status":           chal.status,
            "training_samples": chal.training_samples,
            "metrics":          chal_metrics,
            "gate":             gate_result,
            "gate_status":      gate_status,
        }

        champion_sections[name] = (
            {
                "model_version":    champ.model_version,
                "model_name":       champ.model_name,
                "status":           champ.status,
                "training_samples": champ.training_samples,
                "metrics":          champ_metrics,
            }
            if champ else None
        )

        # ── Deltas (challenger − champion; challenger value if no champion) ─
        delta_sections[name] = {
            key: (
                float(chal_metrics.get(key) or 0.0)
                - float(champ_metrics.get(key) or 0.0)
                if champ_metrics
                else float(chal_metrics.get(key) or 0.0)
            )
            for key in _DELTA_KEYS
        }

        # ── DSR distribution (per-path Sharpe from A3 compute_dsr_and_pbo) ─
        dsr_sections[name] = {
            "path_sharpes":       chal_metrics.get("path_sharpes") or [],
            "deflated_sharpe":    chal_metrics.get("deflated_sharpe"),
            "pbo":                chal_metrics.get("pbo"),
            "ensemble_net_dsr":   chal_metrics.get("ensemble_net_dsr"),
            "ensemble_accretive": chal_metrics.get("ensemble_accretive"),
        }

        lineage_sections[name] = chal.lineage or {}

    # ── Overall run status ─────────────────────────────────────────────────────
    overall_status = (
        PromotionStatus.BLOCKED.value
        if any_blocked
        else PromotionStatus.AWAITING_HUMAN_SIGNOFF.value
    )

    guidance = _build_operator_guidance(
        overall_status, challenger_sections, champions, drift_advisory
    )

    # ── Report body (bundle_sha256 set to the sentinel — hash computed next) ───
    # drift_advisory is always present (empty dict when none) so the field is
    # stable across report versions and covered by the body hash.
    report_body_no_hash: dict[str, Any] = {
        "report_kind":       "c2_promotion_report",
        "generated_at":      now,
        "run_id":            run_id,
        "mlflow_run_id":     mlflow_run_id or "",   # E3: lineage link; "" when unavailable
        "status":            overall_status,
        "challengers":       challenger_sections,
        "champions":         champion_sections,
        "deltas":            delta_sections,
        "dsr_distribution":  dsr_sections,
        "lineage":           lineage_sections,
        "drift_advisory":    drift_advisory or {},
        # F1 — event-driven backtest result; always present ({} when unavailable)
        # so the field is stable across report versions and covered by the SHA-256.
        "event_backtest":    event_backtest or {},
        "operator_guidance": guidance,
        "bundle_sha256":     "",   # sentinel — replaced once the hash is derived
    }

    # ── Bundle integrity ──────────────────────────────────────────────────────
    bundle_manifest = _build_bundle_manifest(
        members=challengers,
        report_body_no_hash=report_body_no_hash,
    )
    bundle_sha256 = _compute_bundle_sha256(bundle_manifest)

    # ── Assemble final report ─────────────────────────────────────────────────
    report: dict[str, Any] = {
        **report_body_no_hash,
        "bundle_manifest": bundle_manifest,
        "bundle_sha256":   bundle_sha256,
        # report_path filled in after the file is written
    }

    # ── Write to disk ─────────────────────────────────────────────────────────
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    ts            = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    versions_slug = "_".join(m.model_version for m in challengers.values())
    report_path   = report_dir / f"promotion_report_{versions_slug}_{ts}.json"

    report["report_path"] = str(report_path.resolve())

    report_path.write_text(
        json.dumps(report, indent=2, default=str),
        encoding="utf-8",
    )

    _log_summary(overall_status, challenger_sections, run_id, report_path)
    return report


def load_and_verify_report(
    report_path: Path | str,
    *,
    challenger_version: str,
    enforce_status: bool = True,
) -> dict[str, Any]:
    """Load and cryptographically verify a C2 Promotion Report.

    Four verification layers are applied in order:

    1. **Kind check** — ``report_kind == "c2_promotion_report"``.
    2. **Version check** — ``challenger_version`` appears as a challenger in
       the report (guards against using the wrong report).
    3. **Bundle integrity** — each artefact file is re-hashed and compared to
       the stored manifest; the top-level ``bundle_sha256`` is re-derived from
       the manifest; the report body is re-hashed with the sentinel.
    4. **Status gate** — if ``enforce_status=True`` (default), a ``BLOCKED``
       report raises ``BlockedChallengerError`` so the standard promotion path
       cannot proceed without explicit break-glass override.

    Parameters
    ----------
    report_path:
        Path to the JSON promotion report.
    challenger_version:
        The model version being promoted (e.g. ``"1.1.0_xgboost"``).  Must
        appear as a challenger version in the report.
    enforce_status:
        When ``True`` (default), a ``BLOCKED`` report raises
        ``BlockedChallengerError`` after bundle verification.  Set to
        ``False`` for the audited break-glass path (``--skip-gates``), where
        the gate failures are explicitly acknowledged in ``--reason``.

    Returns
    -------
    The verified report dict (parsed from the JSON file).

    Raises
    ------
    FileNotFoundError      — report file does not exist.
    ValueError             — schema invalid or version mismatch.
    BundleChecksumError    — any artefact or the report body has been tampered.
    BlockedChallengerError — status is BLOCKED and ``enforce_status=True``.
    """
    report_path = Path(report_path)
    if not report_path.exists():
        raise FileNotFoundError(f"Promotion report not found: {report_path}")

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Promotion report is not valid JSON: {exc}") from exc

    # ── (1) Kind check ────────────────────────────────────────────────────────
    if report.get("report_kind") != "c2_promotion_report":
        raise ValueError(
            f"Not a C2 promotion report "
            f"(report_kind={report.get('report_kind')!r}).  "
            "Only reports generated by build_promotion_report are accepted."
        )

    # ── (2) Version check ─────────────────────────────────────────────────────
    challengers = report.get("challengers", {})
    in_report   = any(
        sec.get("model_version") == challenger_version
        for sec in challengers.values()
    )
    if not in_report:
        found = [sec.get("model_version") for sec in challengers.values()]
        raise ValueError(
            f"Model version '{challenger_version}' is not a challenger in this "
            f"report (found: {found}).  Use the report generated from the same "
            "training run."
        )

    # ── (3) Bundle integrity ──────────────────────────────────────────────────
    stored_manifest   = report.get("bundle_manifest", {})
    stored_bundle_sha = report.get("bundle_sha256", "")
    violations: list[str] = []

    # (3a) Re-hash each artefact file on disk
    _verify_artifact_group(
        stored_manifest.get("inference_artifacts", {}),
        group_label="inference artefact",
        violations=violations,
    )
    _verify_artifact_group(
        stored_manifest.get("calibrators", {}),
        group_label="calibrator",
        violations=violations,
    )

    # (3b) Re-derive top-level bundle_sha256 from the stored manifest
    derived_bundle_sha = _compute_bundle_sha256(stored_manifest)
    if derived_bundle_sha != stored_bundle_sha:
        violations.append(
            f"bundle_sha256 mismatch — "
            f"stored {stored_bundle_sha[:16]}…  "
            f"derived {derived_bundle_sha[:16]}…  "
            "(the bundle manifest has been tampered with)"
        )

    # (3c) Re-hash the report body with the sentinel to verify report integrity
    body_for_hash = _extract_body_for_hash(report)
    expected_body_sha = _sha256_canonical(body_for_hash)
    stored_body_sha   = stored_manifest.get("report_body", {}).get("sha256", "")
    if expected_body_sha != stored_body_sha:
        violations.append(
            f"report body SHA-256 mismatch — "
            f"stored {stored_body_sha[:16]}…  "
            f"recomputed {expected_body_sha[:16]}…  "
            "(the report body has been modified after signing)"
        )

    if violations:
        raise BundleChecksumError(
            f"Promotion report bundle integrity check failed "
            f"({len(violations)} violation(s)):\n"
            + "\n".join(f"  • {v}" for v in violations)
        )

    # ── (4) Status gate ───────────────────────────────────────────────────────
    status = report.get("status")
    if enforce_status and status == PromotionStatus.BLOCKED.value:
        failed_per_model = {
            name: sec.get("gate", {}).get("failed_checks", {})
            for name, sec in challengers.items()
            if sec.get("gate_status") == PromotionStatus.BLOCKED.value
        }
        raise BlockedChallengerError(
            "Promotion report status is BLOCKED — challenger(s) failed the "
            "A6 quality gate.  Standard promotion is disallowed.\n\n"
            f"Failed gates:\n{json.dumps(failed_per_model, indent=2)}\n\n"
            "Options:\n"
            "  1. Retrain and obtain a new report.\n"
            "  2. Audited break-glass: add --skip-gates --reason '<justification>' "
            "(CRITICAL audit entry emitted)."
        )

    return report


# ── Private logging helper ─────────────────────────────────────────────────────

def _log_summary(
    status: str,
    challenger_sections: dict[str, Any],
    run_id: str | None,
    report_path: Path,
) -> None:
    if status == PromotionStatus.BLOCKED.value:
        blocked = [
            n for n, s in challenger_sections.items()
            if s["gate_status"] == PromotionStatus.BLOCKED.value
        ]
        logger.warning(
            "C2 Promotion Report: BLOCKED  run_id=%s  blocked_models=%s  path=%s",
            run_id, blocked, report_path,
        )
        logger.warning(
            "C2: Review gate failures in the report and retrain, "
            "or use audited break-glass (--skip-gates --reason)."
        )
    else:
        logger.info(
            "C2 Promotion Report: AWAITING_HUMAN_SIGNOFF  run_id=%s  path=%s",
            run_id, report_path,
        )
        logger.info(
            "C2: Promote to production via:\n"
            "  python scripts/promote_model.py production "
            "--version <ver> --model-name <name> --report-path %s",
            report_path,
        )
