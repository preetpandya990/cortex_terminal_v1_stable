"""
E2 — Bundle Integrity, Audited Break-Glass, Key Handling
=========================================================

Validates:
  1. ``register_model`` builds a complete per-model ``artifact_manifest`` (in
     ``lineage["artifact_manifest"]``) covering all artifacts — training,
     inference, calibrator, feature manifest — with SHA-256 for each.

  2. ``load_calibrator_artifact`` loads and integrity-verifies the calibrator
     byte-for-byte; any tamper raises ``ArtifactManifestError``.

  3. ``ModelPromoter._emit_break_glass_audit`` writes a self-signed durable JSON
     audit record to ``audit_dir`` (in addition to the CRITICAL log).  The record
     is tamper-evident: any post-write modification is detectable by recomputing
     the ``audit_sha256`` hash.

  4. The ``key_scheme`` field in every ``artifact_manifest`` formally documents
     the "sha256_checksum_only" integrity decision (Option B); its presence in
     the manifest means it is covered by the C2 bundle SHA-256, so any future
     scheme change produces a different digest.

Design contract
---------------
Each test class is written so that **reverting the corresponding production code
change causes at least one test in that class to fail**.  Assertions probe the
*old broken behaviour* (what would happen if the guard was absent) as well as
the new correct outcome.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_ml_model(
    *,
    name: str = "xgboost",
    version: str = "test_1.0.0_xgboost",
    onnx_path: str | None = None,
    lineage: dict | None = None,
    checksum: str | None = None,
) -> MagicMock:
    m = MagicMock()
    m.model_name    = name
    m.model_version = version
    m.onnx_path     = onnx_path
    m.lineage       = lineage
    m.checksum      = checksum
    m.training_features = []
    m.training_metrics  = {}
    m.training_samples  = 100_000
    m.status        = "development"
    return m


async def _register(
    tmp_path: Path,
    *,
    with_inference: bool = True,
    with_calibrator: bool = True,
    features: list[str] | None = None,
) -> tuple[Any, Path, Path | None, Path | None]:
    """Helper: call register_model and return (model, training_dest, inference_dest, cal_dest)."""
    from app.ml.model_registry import ModelRegistry

    storage = tmp_path / "registry"
    storage.mkdir(parents=True, exist_ok=True)

    training_src  = _write(tmp_path / "xgboost_model.json", b'{"booster":"gbtree"}')
    inference_src = _write(tmp_path / "model.onnx",          b"\x00" * 128) if with_inference else None
    cal_src       = _write(tmp_path / "calibrator_xgb.pkl",  b"\x01" * 64) if with_calibrator else None

    registry = ModelRegistry(session=AsyncMock(), model_storage_path=storage)

    # Patch commit/refresh on the session mock
    registry._session.commit     = AsyncMock()
    registry._session.flush      = AsyncMock()
    registry._session.delete     = AsyncMock()
    registry._session.refresh    = AsyncMock()
    registry._session.execute    = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    )

    added_models: list[Any] = []
    def _add(obj: Any) -> None:
        added_models.append(obj)
    registry._session.add = _add

    async def _refresh(obj: Any) -> None:
        pass
    registry._session.refresh.side_effect = _refresh

    feat = features or [f"f_{i}" for i in range(5)]

    model = await registry.register_model(
        version                 = "test_1.0.0_xgboost",
        model_type              = "xgboost",
        artifact_path           = training_src,
        inference_artifact_path = inference_src,
        calibrator_path         = cal_src,
        metrics                 = {"accuracy": 0.57, "f1_score": 0.55},
        metadata                = {"features": feat, "training_samples": 100_000},
        feature_version         = "1.0.0",
    )
    # The model returned is the MLModel ORM object added to the session
    registered = added_models[0] if added_models else model

    training_dest  = storage / f"test_1.0.0_xgboost.json"
    inference_dest = storage / f"test_1.0.0_xgboost_inference.onnx" if with_inference else None
    cal_dest       = storage / f"test_1.0.0_xgboost_calibrator.pkl" if with_calibrator else None

    return registered, training_dest, inference_dest, cal_dest


# ══════════════════════════════════════════════════════════════════════════════
# 1. Artifact manifest at registration
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestArtifactManifestRegistered:
    """Reverting _build_artifact_manifest or the lineage write would fail these tests."""

    @pytest.mark.asyncio
    async def test_manifest_present_in_lineage(self, tmp_path: Path) -> None:
        """register_model must write artifact_manifest into lineage."""
        model, *_ = await _register(tmp_path)
        lineage = model.lineage or {}
        assert "artifact_manifest" in lineage, (
            "register_model must write 'artifact_manifest' into lineage JSONB. "
            "Reverting the lineage merge would cause this test to fail."
        )

    @pytest.mark.asyncio
    async def test_manifest_contains_all_sections(self, tmp_path: Path) -> None:
        """Manifest must have training, inference, calibrator, feature_manifest, key_scheme."""
        model, *_ = await _register(tmp_path)
        m = model.lineage["artifact_manifest"]
        for section in ("training", "inference", "calibrator", "feature_manifest", "key_scheme"):
            assert section in m, f"artifact_manifest missing section '{section}'"

    @pytest.mark.asyncio
    async def test_training_sha256_is_correct(self, tmp_path: Path) -> None:
        """training.sha256 must match the actual bytes of the copied training artifact."""
        model, training_dest, *_ = await _register(tmp_path)
        stored_sha = model.lineage["artifact_manifest"]["training"]["sha256"]
        actual_sha = _sha256(training_dest.read_bytes())
        assert stored_sha == actual_sha, (
            f"training sha256 mismatch: stored={stored_sha[:16]}… actual={actual_sha[:16]}…"
        )

    @pytest.mark.asyncio
    async def test_inference_sha256_is_correct(self, tmp_path: Path) -> None:
        """inference.sha256 must match the actual bytes of the copied inference artifact."""
        model, _, inference_dest, *_ = await _register(tmp_path)
        stored_sha = model.lineage["artifact_manifest"]["inference"]["sha256"]
        assert inference_dest is not None
        actual_sha = _sha256(inference_dest.read_bytes())
        assert stored_sha == actual_sha

    @pytest.mark.asyncio
    async def test_calibrator_sha256_is_correct(self, tmp_path: Path) -> None:
        """calibrator.sha256 must match the actual bytes of the copied calibrator."""
        model, _, _, cal_dest = await _register(tmp_path)
        stored_sha = model.lineage["artifact_manifest"]["calibrator"]["sha256"]
        assert cal_dest is not None and cal_dest.exists(), (
            "Calibrator must be copied to registry storage at registration time."
        )
        actual_sha = _sha256(cal_dest.read_bytes())
        assert stored_sha == actual_sha, (
            f"calibrator sha256 mismatch: stored={stored_sha[:16]}… actual={actual_sha[:16]}…"
        )

    @pytest.mark.asyncio
    async def test_calibrator_copied_to_registry_storage(self, tmp_path: Path) -> None:
        """The calibrator must be physically copied into model_storage_path."""
        _, _, _, cal_dest = await _register(tmp_path)
        assert cal_dest is not None and cal_dest.exists(), (
            "Calibrator was not copied into registry storage — only in-place checksums "
            "are useless if the source path is cleaned up."
        )

    @pytest.mark.asyncio
    async def test_feature_manifest_sha_is_deterministic(self, tmp_path: Path) -> None:
        """Same feature list must always produce the same feature_manifest.sha256."""
        features = ["alpha", "beta", "gamma"]
        model1, *_ = await _register(tmp_path / "r1", features=features)
        model2, *_ = await _register(tmp_path / "r2", features=features)
        sha1 = model1.lineage["artifact_manifest"]["feature_manifest"]["sha256"]
        sha2 = model2.lineage["artifact_manifest"]["feature_manifest"]["sha256"]
        assert sha1 == sha2, "feature_manifest sha256 must be deterministic for the same feature list."

    @pytest.mark.asyncio
    async def test_absent_calibrator_records_absent_sha(self, tmp_path: Path) -> None:
        """When calibrator_path is None, manifest must record sha256='absent' (not error)."""
        model, *_ = await _register(tmp_path, with_calibrator=False)
        cal = model.lineage["artifact_manifest"]["calibrator"]
        assert cal["sha256"] == "absent", (
            "Absent calibrator must be recorded as sha256='absent' so the manifest "
            "structure is always present and the C2 bundle hash changes if the "
            "calibrator appears or disappears between runs."
        )
        assert cal["path"] is None

    @pytest.mark.asyncio
    async def test_missing_calibrator_file_raises(self, tmp_path: Path) -> None:
        """Providing a calibrator_path that doesn't exist must raise FileNotFoundError."""
        from app.ml.model_registry import ModelRegistry

        storage = tmp_path / "registry"
        storage.mkdir()
        training_src = _write(tmp_path / "model.json", b"{}")

        registry = ModelRegistry(session=AsyncMock(), model_storage_path=storage)
        registry._session.commit  = AsyncMock()
        registry._session.flush   = AsyncMock()
        registry._session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )
        registry._session.add = MagicMock()

        nonexistent = tmp_path / "ghost_calibrator.pkl"  # never created

        with pytest.raises(FileNotFoundError, match="Calibrator artifact not found"):
            await registry.register_model(
                version         = "test_err",
                model_type      = "xgboost",
                artifact_path   = training_src,
                calibrator_path = nonexistent,
                metrics         = {"accuracy": 0.5, "f1_score": 0.5},
                metadata        = {"features": [], "training_samples": 1000},
                feature_version = "1.0.0",
            )

    @pytest.mark.asyncio
    async def test_schema_version_is_one(self, tmp_path: Path) -> None:
        """artifact_manifest.schema_version must be 1 (stable for future migrations)."""
        model, *_ = await _register(tmp_path)
        sv = model.lineage["artifact_manifest"].get("schema_version")
        assert sv == 1, f"Expected schema_version=1, got {sv}"


# ══════════════════════════════════════════════════════════════════════════════
# 2. Calibrator load + integrity verification
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCalibratorChecksummed:
    """Removing load_calibrator_artifact or its sha256 check would fail these tests."""

    def _model_with_manifest(self, tmp_path: Path, *, cal_bytes: bytes = b"\x01" * 64) -> tuple[Any, Path]:
        """Build a minimal MLModel mock with a valid artifact_manifest."""
        cal_path = tmp_path / "calibrator_xgb.pkl"
        cal_path.write_bytes(cal_bytes)
        sha = _sha256(cal_bytes)

        model = _make_ml_model(
            onnx_path=str(tmp_path / "model.onnx"),
            lineage={
                "artifact_manifest": {
                    "schema_version": 1,
                    "key_scheme": "sha256_checksum_only",
                    "calibrator": {
                        "path":       str(cal_path.resolve()),
                        "sha256":     sha,
                        "size_bytes": len(cal_bytes),
                    },
                }
            },
        )
        return model, cal_path

    @pytest.mark.asyncio
    async def test_load_calibrator_artifact_returns_bytes(self, tmp_path: Path) -> None:
        """load_calibrator_artifact must return the exact bytes on disk when sha256 matches."""
        from app.ml.model_registry import ModelRegistry

        cal_bytes = b"\xDE\xAD\xBE\xEF" * 16
        model, _ = self._model_with_manifest(tmp_path, cal_bytes=cal_bytes)
        registry = ModelRegistry(session=AsyncMock(), model_storage_path=tmp_path)

        result = await registry.load_calibrator_artifact(model)
        assert result == cal_bytes, "load_calibrator_artifact must return unmodified bytes."

    @pytest.mark.asyncio
    async def test_tampered_calibrator_raises_artifact_manifest_error(self, tmp_path: Path) -> None:
        """Any byte change to the calibrator file must raise ArtifactManifestError."""
        from app.ml.model_registry import ArtifactManifestError, ModelRegistry

        model, cal_path = self._model_with_manifest(tmp_path)
        cal_path.write_bytes(b"\xFF" * 64)   # tamper: overwrite with different bytes

        registry = ModelRegistry(session=AsyncMock(), model_storage_path=tmp_path)
        with pytest.raises(ArtifactManifestError, match="integrity check FAILED"):
            await registry.load_calibrator_artifact(model)

    @pytest.mark.asyncio
    async def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        """Model with no artifact_manifest must raise ArtifactManifestError (not KeyError)."""
        from app.ml.model_registry import ArtifactManifestError, ModelRegistry

        model = _make_ml_model(lineage={})  # empty lineage — no artifact_manifest
        registry = ModelRegistry(session=AsyncMock(), model_storage_path=tmp_path)

        with pytest.raises(ArtifactManifestError, match="no artifact_manifest"):
            await registry.load_calibrator_artifact(model)

    @pytest.mark.asyncio
    async def test_none_lineage_raises(self, tmp_path: Path) -> None:
        """None lineage (pre-E2 model) must raise ArtifactManifestError gracefully."""
        from app.ml.model_registry import ArtifactManifestError, ModelRegistry

        model = _make_ml_model(lineage=None)
        registry = ModelRegistry(session=AsyncMock(), model_storage_path=tmp_path)

        with pytest.raises(ArtifactManifestError, match="no artifact_manifest"):
            await registry.load_calibrator_artifact(model)

    @pytest.mark.asyncio
    async def test_absent_sha_in_manifest_raises(self, tmp_path: Path) -> None:
        """sha256='absent' (registered without calibrator) must raise ArtifactManifestError."""
        from app.ml.model_registry import ArtifactManifestError, ModelRegistry

        model = _make_ml_model(
            lineage={
                "artifact_manifest": {
                    "calibrator": {"path": None, "sha256": "absent", "size_bytes": 0}
                }
            }
        )
        registry = ModelRegistry(session=AsyncMock(), model_storage_path=tmp_path)

        with pytest.raises(ArtifactManifestError, match="absent at registration time"):
            await registry.load_calibrator_artifact(model)

    @pytest.mark.asyncio
    async def test_missing_file_on_disk_raises_file_not_found(self, tmp_path: Path) -> None:
        """File listed in manifest but absent on disk must raise FileNotFoundError."""
        from app.ml.model_registry import ModelRegistry

        ghost = tmp_path / "ghost.pkl"   # never written
        model = _make_ml_model(
            lineage={
                "artifact_manifest": {
                    "calibrator": {
                        "path":       str(ghost.resolve()),
                        "sha256":     "a" * 64,
                        "size_bytes": 32,
                    }
                }
            }
        )
        registry = ModelRegistry(session=AsyncMock(), model_storage_path=tmp_path)

        with pytest.raises(FileNotFoundError):
            await registry.load_calibrator_artifact(model)

    def test_artifact_manifest_error_is_runtime_error(self) -> None:
        """ArtifactManifestError must be a RuntimeError subclass — fail-loud contract."""
        from app.ml.model_registry import ArtifactManifestError

        assert issubclass(ArtifactManifestError, RuntimeError), (
            "ArtifactManifestError must inherit from RuntimeError so bare "
            "'except Exception' handlers still surface the failure."
        )

    @pytest.mark.asyncio
    async def test_load_model_artifact_still_works(self, tmp_path: Path) -> None:
        """load_model_artifact (inference artifact) must still function correctly after E2."""
        from app.ml.model_registry import ModelRegistry

        raw = b"\x00\x01\x02\x03" * 32
        onnx_file = tmp_path / "model.onnx"
        onnx_file.write_bytes(raw)

        model = _make_ml_model(
            onnx_path=str(onnx_file),
            checksum=_sha256(raw),
        )
        registry = ModelRegistry(session=AsyncMock(), model_storage_path=tmp_path)
        result = await registry.load_model_artifact(model)
        assert result == raw, "load_model_artifact must still return the inference artifact bytes."


# ══════════════════════════════════════════════════════════════════════════════
# 3. Break-glass audited — durable self-signed JSON record
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBreakGlassAudited:
    """Removing the durable audit file write or the self-signing would fail these tests."""

    def _promoter(self, tmp_path: Path) -> Any:
        from app.ml.model_registry import ModelPromoter
        return ModelPromoter(AsyncMock(), audit_dir=tmp_path / "audit")

    def test_break_glass_writes_audit_file(self, tmp_path: Path) -> None:
        """_emit_break_glass_audit must create a JSON file in audit_dir."""
        promoter = self._promoter(tmp_path)
        promoter._emit_break_glass_audit(
            "1.0.0_xgboost", "xgboost", "promote_to_production",
            "Hotfix: champion corrupted"
        )
        audit_dir = tmp_path / "audit"
        files = list(audit_dir.glob("break_glass_*.json"))
        assert len(files) == 1, (
            f"Expected exactly 1 audit file, found {len(files)}. "
            "Reverting the file write removes the durable audit trail."
        )

    def test_audit_file_is_valid_json(self, tmp_path: Path) -> None:
        """The written audit file must be valid, parseable JSON."""
        promoter = self._promoter(tmp_path)
        promoter._emit_break_glass_audit(
            "1.0.0_xgboost", "xgboost", "promote_to_production", "hotfix"
        )
        files = list((tmp_path / "audit").glob("break_glass_*.json"))
        record = json.loads(files[0].read_text(encoding="utf-8"))
        assert isinstance(record, dict), "Audit file must deserialize to a dict."

    def test_audit_file_contains_required_fields(self, tmp_path: Path) -> None:
        """Audit record must carry: event, operation, model_version, model_name, reason, timestamp, audit_sha256."""
        promoter = self._promoter(tmp_path)
        promoter._emit_break_glass_audit(
            "1.0.0_xgboost", "xgboost", "promote_to_production", "integration test"
        )
        files = list((tmp_path / "audit").glob("break_glass_*.json"))
        record = json.loads(files[0].read_text(encoding="utf-8"))

        required = ("event", "operation", "model_version", "model_name",
                    "reason", "timestamp", "audit_sha256")
        for field in required:
            assert field in record, f"Audit record missing required field '{field}'"

        assert record["event"]         == "break_glass"
        assert record["model_version"] == "1.0.0_xgboost"
        assert record["model_name"]    == "xgboost"
        assert record["operation"]     == "promote_to_production"
        assert record["reason"]        == "integration test"

    def test_audit_sha256_is_self_signed(self, tmp_path: Path) -> None:
        """audit_sha256 must equal SHA-256 of the canonical record with sentinel ''."""
        promoter = self._promoter(tmp_path)
        promoter._emit_break_glass_audit(
            "1.0.0_xgboost", "xgboost", "promote_to_staging", "test self-signing"
        )
        files = list((tmp_path / "audit").glob("break_glass_*.json"))
        record = json.loads(files[0].read_text(encoding="utf-8"))

        stored_sha = record["audit_sha256"]

        # Recompute: reset sentinel, re-hash
        verification_record = {**record, "audit_sha256": ""}
        recomputed = hashlib.sha256(
            json.dumps(
                verification_record, sort_keys=True, separators=(",", ":"), default=str
            ).encode()
        ).hexdigest()

        assert recomputed == stored_sha, (
            "audit_sha256 does not match recomputed hash — self-signing is broken. "
            "Any modification of the audit record is undetectable without this guard."
        )

    def test_tampered_audit_reason_detectable(self, tmp_path: Path) -> None:
        """Changing 'reason' in the audit file must produce a sha256 mismatch."""
        promoter = self._promoter(tmp_path)
        promoter._emit_break_glass_audit(
            "1.0.0_xgboost", "xgboost", "promote_to_production", "original reason"
        )
        files = list((tmp_path / "audit").glob("break_glass_*.json"))
        record = json.loads(files[0].read_text(encoding="utf-8"))
        stored_sha = record["audit_sha256"]

        tampered = {**record, "reason": "TAMPERED REASON"}
        recomputed = hashlib.sha256(
            json.dumps(
                {**tampered, "audit_sha256": ""},
                sort_keys=True, separators=(",", ":"), default=str,
            ).encode()
        ).hexdigest()

        assert recomputed != stored_sha, (
            "Tampering with 'reason' should produce a different sha256 — "
            "if this passes, the self-signing is not covering the reason field."
        )

    def test_no_audit_file_without_audit_dir(self, tmp_path: Path) -> None:
        """Without audit_dir, _emit_break_glass_audit must not write any file."""
        from app.ml.model_registry import ModelPromoter

        promoter = ModelPromoter(AsyncMock(), audit_dir=None)
        promoter._emit_break_glass_audit(
            "1.0.0_xgboost", "xgboost", "promote_to_production", "no dir set"
        )
        # No files should exist anywhere in tmp_path
        json_files = list(tmp_path.rglob("break_glass_*.json"))
        assert len(json_files) == 0, (
            f"No audit_dir set — no file should be written. Found: {json_files}"
        )

    def test_critical_log_fires_without_audit_dir(self, tmp_path: Path, caplog: Any) -> None:
        """CRITICAL log must fire even when audit_dir is None."""
        from app.ml.model_registry import ModelPromoter

        promoter = ModelPromoter(AsyncMock(), audit_dir=None)

        with caplog.at_level(logging.CRITICAL, logger="app.ml.model_registry.audit"):
            promoter._emit_break_glass_audit(
                "1.0.0_xgboost", "xgboost", "promote_to_production", "log-only test"
            )

        assert any("BREAK_GLASS" in r.message for r in caplog.records), (
            "CRITICAL 'BREAK_GLASS' log entry must fire regardless of audit_dir. "
            "Log-aggregation alerts depend on this prefix."
        )

    def test_audit_dir_created_if_absent(self, tmp_path: Path) -> None:
        """audit_dir must be created automatically — operator must not pre-create it."""
        audit_dir = tmp_path / "deeply" / "nested" / "audit"
        assert not audit_dir.exists(), "Pre-condition: audit_dir should not exist yet."

        from app.ml.model_registry import ModelPromoter
        promoter = ModelPromoter(AsyncMock(), audit_dir=audit_dir)
        promoter._emit_break_glass_audit(
            "1.0.0_xgboost", "xgboost", "demote_to_staging", "dir creation test"
        )
        assert audit_dir.exists() and any(audit_dir.iterdir()), (
            "audit_dir must be created (including parents) and contain the audit file."
        )


# ══════════════════════════════════════════════════════════════════════════════
# 4. Key handling — scheme documented, covered by manifest hash
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestKeyHandlingVerification:
    """Guards the formal Option B decision: SHA-256 checksum-only integrity."""

    @pytest.mark.asyncio
    async def test_key_scheme_is_sha256_checksum_only(self, tmp_path: Path) -> None:
        """artifact_manifest.key_scheme must be exactly 'sha256_checksum_only'."""
        model, *_ = await _register(tmp_path)
        scheme = model.lineage["artifact_manifest"].get("key_scheme")
        assert scheme == "sha256_checksum_only", (
            f"Expected key_scheme='sha256_checksum_only', got {scheme!r}. "
            "Changing the scheme without updating this value would silently break "
            "the integrity audit trail."
        )

    def test_key_scheme_constant_exported(self) -> None:
        """The module-level _KEY_SCHEME constant must equal the scheme in every manifest."""
        from app.ml.model_registry import _KEY_SCHEME
        assert _KEY_SCHEME == "sha256_checksum_only"

    @pytest.mark.asyncio
    async def test_encrypted_flag_is_false(self, tmp_path: Path) -> None:
        """MLModelMetadata.encrypted must be False — Option B explicitly chooses no encryption."""
        model, *_ = await _register(tmp_path)
        assert model.encrypted is False, (
            "encrypted must be False (Option B — checksum-only). "
            "If this field ever becomes True without matching key-management, "
            "the served probabilities are undecryptable."
        )

    def test_encryption_key_param_accepted_but_ignored(self, tmp_path: Path) -> None:
        """ModelRegistry must accept encryption_key without raising (backward compat)."""
        from app.ml.model_registry import ModelRegistry
        registry = ModelRegistry(
            session=AsyncMock(),
            model_storage_path=tmp_path,
            encryption_key="some-key-that-is-intentionally-ignored",
        )
        # Should not raise; key is accepted and documented as Option B
        assert registry is not None

    @pytest.mark.asyncio
    async def test_different_features_produce_different_feature_manifest_sha(self, tmp_path: Path) -> None:
        """Different feature sets must produce different feature_manifest.sha256 values.

        This guards against a SHA-256 collision masking a feature schema change,
        which would mean a model trained on a different feature set passes integrity
        checks silently.  (This is a structural sanity check, not a cryptographic proof.)
        """
        model_a, *_ = await _register(tmp_path / "ra", features=["alpha", "beta"])
        model_b, *_ = await _register(tmp_path / "rb", features=["gamma", "delta"])
        sha_a = model_a.lineage["artifact_manifest"]["feature_manifest"]["sha256"]
        sha_b = model_b.lineage["artifact_manifest"]["feature_manifest"]["sha256"]
        assert sha_a != sha_b, (
            "Different feature sets must produce different SHA-256 hashes. "
            "A collision here would allow a feature schema mismatch to go undetected."
        )

    @pytest.mark.asyncio
    async def test_manifest_schema_version_field_present(self, tmp_path: Path) -> None:
        """artifact_manifest.schema_version must exist for future migration support."""
        model, *_ = await _register(tmp_path)
        assert "schema_version" in model.lineage["artifact_manifest"], (
            "schema_version is required so a future integrity scheme change can be "
            "detected by loading code and trigger a re-registration prompt."
        )

    @pytest.mark.asyncio
    async def test_registered_at_is_parseable_iso_datetime(self, tmp_path: Path) -> None:
        """artifact_manifest.registered_at must be a parseable ISO-8601 datetime string."""
        model, *_ = await _register(tmp_path)
        ts_str = model.lineage["artifact_manifest"].get("registered_at", "")
        try:
            dt = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError) as exc:
            pytest.fail(f"registered_at={ts_str!r} is not parseable as ISO datetime: {exc}")
        assert dt.tzinfo is not None, "registered_at must be timezone-aware (UTC)."
