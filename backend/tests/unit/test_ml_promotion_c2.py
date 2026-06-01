"""
C2 — Champion/Challenger Promotion Report acceptance suite.

Validates:
  1. ``build_promotion_report`` builds a well-formed, signed report for passing
     and failing challengers, writes it to disk, and populates all required
     fields.
  2. ``load_and_verify_report`` accepts valid reports and raises on every
     category of tampering (body, artefact, manifest, missing file).
  3. A BLOCKED challenger cannot promote via the standard path
     (``BlockedChallengerError``).
  4. Any byte-level tamper to any bundle component is detected
     (``BundleChecksumError``).
  5. The bundle SHA-256 is deterministic across identical inputs.
  6. Report status is ``AWAITING_HUMAN_SIGNOFF`` only when ALL challengers pass
     the A6 gate, and ``BLOCKED`` as soon as any one fails.

Design notes
------------
* No DB, no async — every test is pure unit I/O using ``tmp_path``.
* Fake artefact files are created in ``tmp_path`` so bundle checksum tests
  exercise real file I/O without requiring a trained model on disk.
* Mocked ``MLModelMetadata`` objects follow the exact same shape used by the
  C3 test suite (``_make_model`` helper).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.ml.evaluation.promotion_report import (
    BlockedChallengerError,
    BundleChecksumError,
    PromotionStatus,
    _build_bundle_manifest,
    _compute_bundle_sha256,
    _extract_body_for_hash,
    _sha256_canonical,
    build_promotion_report,
    load_and_verify_report,
)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures & helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_model(
    *,
    name:     str  = "xgboost",
    version:  str  = "1.1.0_xgboost",
    status:   str  = "development",
    active:   bool = False,
    metrics:  dict | None = None,
    features: list | None = None,
    onnx:     str  | None = None,
    samples:  int  = 3_000_000,
    lineage:  dict | None = None,
) -> MagicMock:
    """Return a MagicMock shaped like ``MLModelMetadata``."""
    m = MagicMock()
    m.model_id          = f"{name}_{version}"
    m.model_name        = name
    m.model_version     = version
    m.status            = status
    m.is_active         = active
    m.training_metrics  = metrics  if metrics  is not None else {}
    m.training_features = features if features is not None else []
    m.onnx_path         = onnx
    m.training_samples  = samples
    m.lineage           = lineage
    m.trained_at        = datetime(2026, 5, 24, tzinfo=timezone.utc)
    m.deployed_at       = None
    return m


def _passing_metrics(model_name: str = "xgboost") -> dict:
    """A6-passing metrics for the given model type."""
    ap_floor = {"xgboost": 0.60, "gru": 0.55}.get(model_name, 0.60)
    return {
        "accuracy":          0.65,
        "f1_score":          0.62,
        "auc_pr":            ap_floor + 0.05,
        "deflated_sharpe":   1.35,
        "ece_after":         0.03,
        "symbol_coverage":   0.92,
        "pbo":               0.14,
        "path_sharpes":      [0.8, 1.1, 0.9, 1.3, 1.0, 0.7, 1.2],
        "ensemble_net_dsr":  1.20,
        "ensemble_accretive": True,
    }


def _failing_metrics() -> dict:
    """Metrics that fail gates 1/2/3 (auc_pr / deflated_sharpe / ece_after)."""
    return {
        "accuracy":   0.65,
        "f1_score":   0.62,
        # auc_pr absent   → 0.0 < floor
        # deflated_sharpe → -1.0 (fail-safe) not > 0
        # ece_after       → 1.0 (fail-safe) > 0.05
    }


def _write_fake_artefact(path: Path, content: bytes = b"fake-model-bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.fixture()
def artefact_dir(tmp_path: Path) -> Path:
    """A directory containing fake inference artefacts + calibrators."""
    d = tmp_path / "artefacts"
    d.mkdir()
    _write_fake_artefact(d / "1.1.0_xgboost_inference.so",  b"xgb-so-bytes")
    _write_fake_artefact(d / "calibrator_xgb.pkl",           b"xgb-cal-bytes")
    _write_fake_artefact(d / "1.1.0_gru_inference.onnx",    b"gru-onnx-bytes")
    _write_fake_artefact(d / "calibrator_gru.pkl",           b"gru-cal-bytes")
    return d


@pytest.fixture()
def challengers(artefact_dir: Path) -> dict[str, MagicMock]:
    return {
        "xgboost": _make_model(
            name="xgboost", version="1.1.0_xgboost",
            metrics=_passing_metrics("xgboost"),
            features=[f"f{i}" for i in range(69)],
            onnx=str(artefact_dir / "1.1.0_xgboost_inference.so"),
            lineage={"git_commit": "abc123", "data_window": {"start": "2016-01-01"}},
        ),
        "gru": _make_model(
            name="gru", version="1.1.0_gru",
            metrics=_passing_metrics("gru"),
            features=[f"f{i}" for i in range(69)],
            onnx=str(artefact_dir / "1.1.0_gru_inference.onnx"),
        ),
    }


@pytest.fixture()
def champions() -> dict[str, MagicMock | None]:
    return {
        "xgboost": _make_model(
            name="xgboost", version="1.0.0_xgboost",
            status="production", active=True,
            metrics={
                "accuracy": 0.60, "f1_score": 0.58,
                "auc_pr": 0.57, "deflated_sharpe": 1.10,
                "ece_after": 0.04,
            },
        ),
        "gru": None,  # no prior champion for GRU
    }


# ══════════════════════════════════════════════════════════════════════════════
# TestBuildPromotionReport — core report construction
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildPromotionReport:

    def test_passing_challengers_status_awaiting(
        self, challengers, champions, tmp_path
    ) -> None:
        report = build_promotion_report(
            challengers=challengers,
            champions=champions,
            report_dir=tmp_path,
            run_id="20260524_120000",
        )
        assert report["status"] == PromotionStatus.AWAITING_HUMAN_SIGNOFF.value

    def test_failing_challenger_status_blocked(
        self, artefact_dir, champions, tmp_path
    ) -> None:
        blocked = {
            "xgboost": _make_model(
                name="xgboost", version="1.1.0_xgboost",
                metrics=_failing_metrics(),
                features=[f"f{i}" for i in range(69)],
                onnx=str(artefact_dir / "1.1.0_xgboost_inference.so"),
            ),
            "gru": _make_model(
                name="gru", version="1.1.0_gru",
                metrics=_passing_metrics("gru"),
                features=[f"f{i}" for i in range(69)],
                onnx=str(artefact_dir / "1.1.0_gru_inference.onnx"),
            ),
        }
        report = build_promotion_report(
            challengers=blocked,
            champions=champions,
            report_dir=tmp_path,
        )
        assert report["status"] == PromotionStatus.BLOCKED.value
        assert report["challengers"]["xgboost"]["gate_status"] == PromotionStatus.BLOCKED.value
        assert report["challengers"]["gru"]["gate_status"]     == PromotionStatus.AWAITING_HUMAN_SIGNOFF.value

    def test_report_file_is_written(self, challengers, champions, tmp_path) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        report_path = Path(report["report_path"])
        assert report_path.exists()
        on_disk = json.loads(report_path.read_text())
        assert on_disk["report_kind"] == "c2_promotion_report"

    def test_required_top_level_fields(self, challengers, champions, tmp_path) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions,
            report_dir=tmp_path, run_id="test-run",
        )
        for field in (
            "report_kind", "generated_at", "run_id", "status",
            "challengers", "champions", "deltas", "dsr_distribution",
            "lineage", "operator_guidance", "bundle_manifest",
            "bundle_sha256", "report_path",
        ):
            assert field in report, f"Missing field: {field}"

    def test_challenger_sections_populated(self, challengers, champions, tmp_path) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        for name in ("xgboost", "gru"):
            sec = report["challengers"][name]
            assert sec["model_version"] == challengers[name].model_version
            assert sec["model_name"]    == name
            assert "gate"       in sec
            assert "gate_status" in sec
            assert "metrics"    in sec

    def test_champion_section_present_and_absent(
        self, challengers, champions, tmp_path
    ) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        assert report["champions"]["xgboost"] is not None
        assert report["champions"]["xgboost"]["model_version"] == "1.0.0_xgboost"
        assert report["champions"]["gru"] is None

    def test_deltas_computed_vs_champion(self, challengers, champions, tmp_path) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        xgb_delta = report["deltas"]["xgboost"]
        chal_ap   = challengers["xgboost"].training_metrics["auc_pr"]
        champ_ap  = champions["xgboost"].training_metrics["auc_pr"]
        assert abs(xgb_delta["auc_pr"] - (chal_ap - champ_ap)) < 1e-9

    def test_deltas_challenger_value_when_no_champion(
        self, challengers, champions, tmp_path
    ) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        gru_delta = report["deltas"]["gru"]
        # No champion → delta equals the challenger metric value itself
        assert abs(gru_delta["auc_pr"] - challengers["gru"].training_metrics["auc_pr"]) < 1e-9

    def test_dsr_distribution_populated(self, challengers, champions, tmp_path) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        dsr = report["dsr_distribution"]["xgboost"]
        assert "path_sharpes"     in dsr
        assert "deflated_sharpe"  in dsr
        assert "pbo"              in dsr

    def test_lineage_captured(self, challengers, champions, tmp_path) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        assert report["lineage"]["xgboost"]["git_commit"] == "abc123"

    def test_run_id_in_report(self, challengers, champions, tmp_path) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions,
            report_dir=tmp_path, run_id="20260524_run",
        )
        assert report["run_id"] == "20260524_run"

    def test_no_champion_first_deploy(self, artefact_dir, tmp_path) -> None:
        chal = {
            "xgboost": _make_model(
                name="xgboost", version="1.0.0_xgboost",
                metrics=_passing_metrics("xgboost"),
                onnx=str(artefact_dir / "1.1.0_xgboost_inference.so"),
            ),
        }
        report = build_promotion_report(
            challengers=chal, champions={"xgboost": None}, report_dir=tmp_path
        )
        assert report["status"] == PromotionStatus.AWAITING_HUMAN_SIGNOFF.value
        assert report["champions"]["xgboost"] is None

    def test_operator_guidance_contains_promote_command(
        self, challengers, champions, tmp_path
    ) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        guidance = report["operator_guidance"]
        assert "promote_model.py production" in guidance
        assert "1.1.0_xgboost" in guidance

    def test_operator_guidance_blocked_contains_retrain_hint(
        self, artefact_dir, tmp_path
    ) -> None:
        blocked = {
            "xgboost": _make_model(
                name="xgboost", version="1.1.0_xgboost",
                metrics=_failing_metrics(),
                onnx=str(artefact_dir / "1.1.0_xgboost_inference.so"),
            ),
        }
        report = build_promotion_report(
            challengers=blocked, champions={"xgboost": None}, report_dir=tmp_path
        )
        assert "BLOCKED" in report["status"]
        assert "break-glass" in report["operator_guidance"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# TestBundleManifest — manifest construction
# ══════════════════════════════════════════════════════════════════════════════

class TestBundleManifest:

    def test_manifest_has_all_required_keys(
        self, challengers, champions, tmp_path
    ) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        manifest = report["bundle_manifest"]
        assert "inference_artifacts" in manifest
        assert "calibrators"         in manifest
        assert "feature_manifest"    in manifest
        assert "lineage"             in manifest
        assert "report_body"         in manifest

    def test_inference_artefact_entry_has_path_and_sha(
        self, challengers, champions, tmp_path
    ) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        xgb_entry = report["bundle_manifest"]["inference_artifacts"]["xgboost"]
        assert xgb_entry["path"] is not None
        assert len(xgb_entry["sha256"]) == 64   # SHA-256 hex
        assert xgb_entry["size_bytes"] > 0

    def test_missing_onnx_path_recorded_as_absent(self, tmp_path) -> None:
        chal = {
            "xgboost": _make_model(
                name="xgboost", version="1.0.0_xgboost",
                metrics=_passing_metrics("xgboost"),
                onnx=None,  # no path
            ),
        }
        report = build_promotion_report(
            challengers=chal, champions={"xgboost": None}, report_dir=tmp_path
        )
        entry = report["bundle_manifest"]["inference_artifacts"]["xgboost"]
        assert entry["sha256"] == "absent"

    def test_feature_manifest_n_features_correct(
        self, challengers, champions, tmp_path
    ) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        assert report["bundle_manifest"]["feature_manifest"]["n_features"] == 69

    def test_bundle_sha256_is_hex_string(self, challengers, champions, tmp_path) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        sha = report["bundle_sha256"]
        assert isinstance(sha, str)
        assert len(sha) == 64
        int(sha, 16)  # raises ValueError if not valid hex


# ══════════════════════════════════════════════════════════════════════════════
# TestBundleChecksum — determinism and sensitivity
# ══════════════════════════════════════════════════════════════════════════════

class TestBundleChecksum:

    def test_deterministic_same_inputs(
        self, challengers, champions, tmp_path
    ) -> None:
        r1 = build_promotion_report(
            challengers=challengers, champions=champions,
            report_dir=tmp_path / "r1", run_id="same-run",
        )
        r2 = build_promotion_report(
            challengers=challengers, champions=champions,
            report_dir=tmp_path / "r2", run_id="same-run",
        )
        # Manifests (excluding report_body — timestamps differ) should match
        m1 = r1["bundle_manifest"]
        m2 = r2["bundle_manifest"]
        assert (
            m1["inference_artifacts"] == m2["inference_artifacts"]
        ), "Inference artefact hashes should be identical for same files"
        assert (
            m1["calibrators"] == m2["calibrators"]
        ), "Calibrator hashes should be identical for same files"

    def test_compute_bundle_sha256_stable(self) -> None:
        manifest = {"a": {"sha256": "aabbcc"}, "b": {"sha256": "ddeeff"}}
        h1 = _compute_bundle_sha256(manifest)
        h2 = _compute_bundle_sha256(manifest)
        assert h1 == h2

    def test_different_manifest_different_hash(self) -> None:
        m1 = {"a": {"sha256": "aabbcc"}}
        m2 = {"a": {"sha256": "112233"}}
        assert _compute_bundle_sha256(m1) != _compute_bundle_sha256(m2)

    def test_sha256_canonical_sort_keys_stable(self) -> None:
        d1 = {"z": 1, "a": 2}
        d2 = {"a": 2, "z": 1}
        assert _sha256_canonical(d1) == _sha256_canonical(d2)

    def test_extract_body_for_hash_removes_post_fields(
        self, challengers, champions, tmp_path
    ) -> None:
        report = build_promotion_report(
            challengers=challengers, champions=champions, report_dir=tmp_path
        )
        body = _extract_body_for_hash(report)
        assert "bundle_manifest" not in body
        assert "report_path"     not in body
        assert body["bundle_sha256"] == ""


# ══════════════════════════════════════════════════════════════════════════════
# TestLoadAndVerifyReport — verification happy path
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadAndVerifyReport:

    def _write_report(
        self, challengers, champions, tmp_path, **kwargs
    ) -> Path:
        report = build_promotion_report(
            challengers=challengers, champions=champions,
            report_dir=tmp_path, **kwargs,
        )
        return Path(report["report_path"])

    def test_valid_report_returns_dict(
        self, challengers, champions, tmp_path
    ) -> None:
        path = self._write_report(challengers, champions, tmp_path)
        result = load_and_verify_report(path, challenger_version="1.1.0_xgboost")
        assert result["report_kind"] == "c2_promotion_report"
        assert result["status"] == PromotionStatus.AWAITING_HUMAN_SIGNOFF.value

    def test_verify_with_gru_version(self, challengers, champions, tmp_path) -> None:
        path = self._write_report(challengers, champions, tmp_path)
        result = load_and_verify_report(path, challenger_version="1.1.0_gru")
        assert result is not None

    def test_missing_report_raises_file_not_found(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError):
            load_and_verify_report(
                tmp_path / "nonexistent.json",
                challenger_version="1.0.0_xgboost",
            )

    def test_wrong_report_kind_raises_value_error(self, tmp_path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"report_kind": "not_c2"}))
        with pytest.raises(ValueError, match="Not a C2 promotion report"):
            load_and_verify_report(bad, challenger_version="1.0.0_xgboost")

    def test_version_not_in_report_raises_value_error(
        self, challengers, champions, tmp_path
    ) -> None:
        path = self._write_report(challengers, champions, tmp_path)
        with pytest.raises(ValueError, match="not a challenger in this report"):
            load_and_verify_report(path, challenger_version="9.9.9_xgboost")

    def test_invalid_json_raises_value_error(self, tmp_path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not valid json {{{")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_and_verify_report(bad, challenger_version="1.0.0_xgboost")


# ══════════════════════════════════════════════════════════════════════════════
# TestBlockedChallengerCannotPromote — C2 acceptance test #1
# ══════════════════════════════════════════════════════════════════════════════

class TestBlockedChallengerCannotPromote:
    """
    A BLOCKED report must raise ``BlockedChallengerError`` on the standard
    promotion path (``enforce_status=True``).

    The audited break-glass path (``enforce_status=False``) must succeed even
    on a BLOCKED report — the gate bypass is explicit and documented via
    ``--skip-gates --reason``.
    """

    @pytest.fixture()
    def blocked_report_path(self, artefact_dir, tmp_path) -> Path:
        blocked_challengers = {
            "xgboost": _make_model(
                name="xgboost", version="1.1.0_xgboost",
                metrics=_failing_metrics(),
                features=[f"f{i}" for i in range(69)],
                onnx=str(artefact_dir / "1.1.0_xgboost_inference.so"),
            ),
        }
        report = build_promotion_report(
            challengers=blocked_challengers,
            champions={"xgboost": None},
            report_dir=tmp_path,
        )
        assert report["status"] == PromotionStatus.BLOCKED.value
        return Path(report["report_path"])

    def test_blocked_report_raises_on_standard_path(
        self, blocked_report_path
    ) -> None:
        with pytest.raises(BlockedChallengerError) as exc_info:
            load_and_verify_report(
                blocked_report_path,
                challenger_version="1.1.0_xgboost",
                enforce_status=True,
            )
        msg = str(exc_info.value)
        assert "BLOCKED" in msg
        assert "quality gate" in msg.lower()

    def test_blocked_error_contains_gate_failures(
        self, blocked_report_path
    ) -> None:
        with pytest.raises(BlockedChallengerError) as exc_info:
            load_and_verify_report(
                blocked_report_path,
                challenger_version="1.1.0_xgboost",
            )
        # At least one of the three failing gates should be named
        msg = str(exc_info.value)
        assert any(g in msg for g in ("auc_pr", "deflated_sharpe", "calibration_ece"))

    def test_blocked_report_succeeds_with_break_glass(
        self, blocked_report_path
    ) -> None:
        result = load_and_verify_report(
            blocked_report_path,
            challenger_version="1.1.0_xgboost",
            enforce_status=False,
        )
        assert result["status"] == PromotionStatus.BLOCKED.value

    def test_blocked_error_is_value_error_subclass(
        self, blocked_report_path
    ) -> None:
        with pytest.raises(ValueError):
            load_and_verify_report(
                blocked_report_path,
                challenger_version="1.1.0_xgboost",
            )


# ══════════════════════════════════════════════════════════════════════════════
# TestBundleIntegrityEndToEnd — C2 acceptance test #2
# ══════════════════════════════════════════════════════════════════════════════

class TestBundleIntegrityEndToEnd:
    """
    Tampering with any single byte of any bundle component must be detected
    by ``load_and_verify_report``.

    Components tested: inference artefact, calibrator, report body JSON.
    """

    @pytest.fixture()
    def valid_report_path(self, challengers, champions, tmp_path) -> Path:
        report = build_promotion_report(
            challengers=challengers, champions=champions,
            report_dir=tmp_path / "reports",
        )
        return Path(report["report_path"])

    def test_valid_report_passes_verification(self, valid_report_path) -> None:
        result = load_and_verify_report(
            valid_report_path, challenger_version="1.1.0_xgboost"
        )
        assert result["status"] == PromotionStatus.AWAITING_HUMAN_SIGNOFF.value

    def test_tamper_inference_artefact_detected(self, valid_report_path) -> None:
        report = json.loads(valid_report_path.read_text())
        artefact_path = Path(
            report["bundle_manifest"]["inference_artifacts"]["xgboost"]["path"]
        )
        artefact_path.write_bytes(b"tampered-model-bytes")

        with pytest.raises(BundleChecksumError) as exc_info:
            load_and_verify_report(
                valid_report_path, challenger_version="1.1.0_xgboost"
            )
        assert "inference artefact" in str(exc_info.value).lower()
        assert "xgboost" in str(exc_info.value)

    def test_tamper_calibrator_detected(self, valid_report_path) -> None:
        report = json.loads(valid_report_path.read_text())
        cal_path = Path(
            report["bundle_manifest"]["calibrators"]["xgboost"]["path"]
        )
        cal_path.write_bytes(b"tampered-calibrator")

        with pytest.raises(BundleChecksumError) as exc_info:
            load_and_verify_report(
                valid_report_path, challenger_version="1.1.0_xgboost"
            )
        assert "calibrator" in str(exc_info.value).lower()

    def test_tamper_report_body_detected(self, valid_report_path) -> None:
        report = json.loads(valid_report_path.read_text())
        # Modify a metric in the report body to simulate tampering
        report["challengers"]["xgboost"]["metrics"]["auc_pr"] = 0.99
        valid_report_path.write_text(json.dumps(report, indent=2))

        with pytest.raises(BundleChecksumError) as exc_info:
            load_and_verify_report(
                valid_report_path, challenger_version="1.1.0_xgboost"
            )
        assert "report body" in str(exc_info.value).lower()

    def test_tamper_bundle_sha256_detected(self, valid_report_path) -> None:
        report = json.loads(valid_report_path.read_text())
        report["bundle_sha256"] = "a" * 64   # plausible-looking but wrong hash
        valid_report_path.write_text(json.dumps(report, indent=2))

        with pytest.raises(BundleChecksumError) as exc_info:
            load_and_verify_report(
                valid_report_path, challenger_version="1.1.0_xgboost"
            )
        assert "bundle_sha256" in str(exc_info.value)

    def test_missing_inference_artefact_detected(self, valid_report_path) -> None:
        report = json.loads(valid_report_path.read_text())
        artefact_path = Path(
            report["bundle_manifest"]["inference_artifacts"]["xgboost"]["path"]
        )
        artefact_path.unlink()

        with pytest.raises(BundleChecksumError) as exc_info:
            load_and_verify_report(
                valid_report_path, challenger_version="1.1.0_xgboost"
            )
        assert "missing" in str(exc_info.value).lower()

    def test_bundle_checksum_error_is_value_error_subclass(
        self, valid_report_path
    ) -> None:
        report = json.loads(valid_report_path.read_text())
        report["bundle_sha256"] = "0" * 64
        valid_report_path.write_text(json.dumps(report, indent=2))

        with pytest.raises(ValueError):
            load_and_verify_report(
                valid_report_path, challenger_version="1.1.0_xgboost"
            )

    def test_multiple_violations_all_reported(self, valid_report_path) -> None:
        """All violations must be surfaced in a single exception, not just the first."""
        report = json.loads(valid_report_path.read_text())

        # Tamper both inference artefact and calibrator
        xgb_inf = Path(
            report["bundle_manifest"]["inference_artifacts"]["xgboost"]["path"]
        )
        xgb_cal = Path(
            report["bundle_manifest"]["calibrators"]["xgboost"]["path"]
        )
        xgb_inf.write_bytes(b"tampered-inf")
        xgb_cal.write_bytes(b"tampered-cal")

        with pytest.raises(BundleChecksumError) as exc_info:
            load_and_verify_report(
                valid_report_path, challenger_version="1.1.0_xgboost"
            )
        msg = str(exc_info.value)
        assert "inference artefact" in msg.lower()
        assert "calibrator"          in msg.lower()
