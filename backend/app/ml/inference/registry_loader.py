"""
Cortex AI — Registry Model Loader
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.metrics import model_state as model_deployment_state
from app.ml.inference.calibrator import ConfidenceCalibrator
from app.ml.monitoring.metrics import model_accuracy_score, model_load_failures_total
from app.models.ml_data import MLModelMetadata

logger = logging.getLogger(__name__)

_RNG = np.random.default_rng(42)


class ModelLoadError(Exception):
    pass


class ModelValidationError(Exception):
    pass


# ── Bootstrap ─────────────────────────────────────────────────────────────────

_MODEL_ROOT = Path(get_settings().ML_MODEL_STORAGE_PATH) / "production"  # single source of truth (config.ML_MODEL_STORAGE_PATH)

_BOOTSTRAP_MODELS = {
    "xgboost": {
        "version": "1.0.0",
        "artifact": _MODEL_ROOT / "treelite/xgboost_model.so",
        "valid_suffixes": {".so", ".dll", ".dylib"},
        "ensemble_weight": 0.75,
    },
    "gru": {
        "version": "1.0.0",
        "artifact": _MODEL_ROOT / "onnx/gru_optimized.onnx",
        "valid_suffixes": {".onnx"},
        "ensemble_weight": 0.25,
    },
}


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_has_valid_onnx_path(record: MLModelMetadata, valid_suffixes: set[str]) -> bool:
    """Return True iff the record's onnx_path points to an existing file with the right extension."""
    if not record.onnx_path:
        return False
    path = Path(record.onnx_path)
    return path.suffix in valid_suffixes and path.exists()


async def bootstrap_production_models(session: AsyncSession) -> None:
    """Ensure every model in ``_BOOTSTRAP_MODELS`` has a healthy production record.

    This function is **idempotent and self-healing**:

    * If no active production record exists for a model → inserts one pointing
      at the on-disk inference artifact.
    * If a record exists but ``onnx_path`` is stale (wrong extension or missing
      file) → repairs it in-place, recomputing the checksum.
    * If the record is healthy → no-op.

    This repairs the common failure mode where training scripts store the native
    training artifact path (.json / .keras) in ``onnx_path`` instead of the
    inference-ready artifact (.so / .onnx), leaving ``RegistryModelLoader``
    unable to start and the service returning HTTP 503.
    """
    for model_name, cfg in _BOOTSTRAP_MODELS.items():
        artifact = Path(cfg["artifact"])
        valid_suffixes: set[str] = cfg["valid_suffixes"]

        stmt = (
            select(MLModelMetadata)
            .where(
                MLModelMetadata.model_name == model_name,
                MLModelMetadata.status == "production",
                MLModelMetadata.is_active == True,
            )
            .order_by(MLModelMetadata.deployed_at.desc())
            .limit(1)
        )
        existing = (await session.execute(stmt)).scalar_one_or_none()

        if existing is not None and _record_has_valid_onnx_path(existing, valid_suffixes):
            logger.debug("Bootstrap: %s record is healthy (%s)", model_name, existing.onnx_path)
            continue

        if not artifact.exists():
            logger.warning(
                "Bootstrap: inference artifact not found for %s at %s — cannot insert/repair record",
                model_name, artifact,
            )
            continue

        checksum = await asyncio.to_thread(_sha256_file, artifact)

        if existing is not None:
            # Repair in-place: update stale onnx_path and recompute checksum.
            old_path = existing.onnx_path
            existing.onnx_path = str(artifact)
            existing.checksum  = checksum
            existing.updated_at = datetime.now(timezone.utc)
            logger.warning(
                "Bootstrap: repaired %s onnx_path  %s → %s",
                model_name, old_path, artifact.name,
            )
        else:
            record = MLModelMetadata(
                model_id        = f"{model_name}_bootstrap_{cfg['version']}",
                model_name      = model_name,
                model_version   = cfg["version"],
                onnx_path       = str(artifact),
                model_path      = str(artifact),
                checksum        = checksum,
                encrypted       = False,
                status          = "production",
                is_active       = True,
                deployed_at     = datetime.now(timezone.utc),
                training_metrics= {"ensemble_weight": cfg["ensemble_weight"]},
            )
            session.add(record)
            logger.info("Bootstrap: inserted %s v%s → %s", model_name, cfg["version"], artifact.name)

    await session.commit()


@dataclass(frozen=True)
class LoadedEnsemble:
    """
    Immutable, thread-safe container for the validated production ensemble.

    Holds pre-loaded ONNX Runtime / Treelite sessions and optional probability
    calibrators.  All fields are plain Python scalars or inference objects —
    no SQLAlchemy ORM objects — so the ensemble can safely outlive the DB
    session used to construct it.

    ``n_features`` and ``sequence_length`` are read directly from the model
    artifacts (Treelite ``num_feature`` / ONNX input shape) at load time —
    never from a config constant.  This makes the ensemble self-describing:
    the model is the single source of truth for its own feature contract,
    regardless of how the feature set evolves between training runs.

    ``feature_names`` carries the ordered feature manifest stored in
    ``MLModelMetadata.training_features`` at registration time.  Downstream
    consumers (``FeatureLoader``) use this to compute exactly the right
    features for each model version without any hard-coded column lists.

    Calibrators are optional: if the calibrator files are absent from the model
    directory, the fields are None and inference falls back to raw probabilities.

    Single-Active-Member Ensemble mode
    -----------------------------------
    ``gru_session``, ``gru_version``, and ``gru_model_id`` are all optional.
    When the GRU is registered at the production tier but dormant
    (``is_active=False``), the loader sets these to None and normalises
    ``gru_weight=0.0`` / ``xgboost_weight=1.0`` so XGBoost serves alone.
    Validation and warmup skip the GRU path.  Flipping the GRU record to
    ``is_active=True`` in the DB and reloading the ensemble automatically
    re-enables the full two-member ensemble without any code change.
    """
    xgboost_predictor: Any                           # tl2cgen.Predictor
    xgboost_weight:    float
    gru_weight:        float
    xgboost_version:   str
    xgboost_model_id:  str
    loaded_at:         datetime
    n_features:        int                           # derived from model — never hardcoded
    # GRU is optional — None when no active production GRU exists (dormant pattern)
    gru_session:       ort.InferenceSession | None = field(default=None)
    gru_version:       str | None                  = field(default=None)
    gru_model_id:      str | None                  = field(default=None)
    sequence_length:   int                         = 60
    feature_names:     tuple[str, ...]             = field(default_factory=tuple)
    xgb_calibrator:    ConfidenceCalibrator | None = field(default=None)
    gru_calibrator:    ConfidenceCalibrator | None = field(default=None)

    @property
    def gru_input_name(self) -> str:
        if self.gru_session is None:
            raise AttributeError("gru_input_name accessed but GRU is not loaded (dormant mode)")
        return self.gru_session.get_inputs()[0].name

    @property
    def gru_output_names(self) -> list[str]:
        if self.gru_session is None:
            raise AttributeError("gru_output_names accessed but GRU is not loaded (dormant mode)")
        return [o.name for o in self.gru_session.get_outputs()]

    def validate(self) -> None:
        """Run deterministic synthetic inference to confirm loaded models accept the right shape."""
        try:
            import tl2cgen
            X_tab = _RNG.standard_normal((1, self.n_features)).astype(np.float32)
            dmat = tl2cgen.DMatrix(X_tab)
            xgb_out = self.xgboost_predictor.predict(dmat)
            if xgb_out.shape[0] != 1:
                raise ModelValidationError(f"XGBoost output shape invalid: {xgb_out.shape}")

            if self.gru_session is not None:
                X_seq = _RNG.standard_normal(
                    (1, self.sequence_length, self.n_features)
                ).astype(np.float32)
                gru_out = self.gru_session.run(
                    self.gru_output_names,
                    {self.gru_input_name: X_seq},
                )[0]
                if gru_out.shape[0] != 1:
                    raise ModelValidationError(f"GRU output shape invalid: {gru_out.shape}")

            logger.info(
                "Model validation passed: n_features=%d sequence_length=%d gru=%s",
                self.n_features, self.sequence_length,
                "yes" if self.gru_session is not None else "dormant",
            )
        except ModelValidationError:
            raise
        except Exception as exc:
            raise ModelValidationError(f"Model validation failed: {exc}") from exc


class RegistryModelLoader:
    """
    Loads and validates the production XGBoost + GRU ensemble from the model registry.

    Thread-safe lazy loading with asyncio lock.  The cached ensemble is
    immutable (frozen dataclass) and safe to share across async handlers.

    Usage:
        loader   = RegistryModelLoader(session)
        ensemble = await loader.load_production_ensemble()
        app.state.ml_ensemble = ensemble
    """

    def __init__(self, session: AsyncSession, num_threads: int = 4, use_gpu: bool = False) -> None:
        self._session     = session
        self._num_threads = num_threads
        self._use_gpu     = use_gpu
        self._lock        = asyncio.Lock()
        self._cached:     LoadedEnsemble | None = None

    async def load_production_ensemble(self, force_reload: bool = False) -> LoadedEnsemble:
        async with self._lock:
            if self._cached and not force_reload:
                return self._cached

            logger.info("Loading production ensemble from registry...")

            # XGBoost is always required.  GRU is optional — absent when the
            # production GRU record is dormant (is_active=False).
            xgb_meta = await self._fetch_production_record("xgboost")
            gru_meta  = await self._fetch_production_record_optional("gru")

            xgb_predictor = await self._load_treelite(xgb_meta)

            if gru_meta is not None:
                gru_session: ort.InferenceSession | None = await self._load_onnx(gru_meta)
            else:
                gru_session = None
                logger.info(
                    "No active production GRU found — loading in XGBoost-only mode. "
                    "Set GRU is_active=True and reload to re-enable the full ensemble."
                )

            # ── Derive n_features from model artifacts ────────────────────────
            xgb_n_features = xgb_predictor.num_feature
            if gru_session is not None:
                gru_n_features = int(gru_session.get_inputs()[0].shape[2])
                if xgb_n_features != gru_n_features:
                    raise ModelLoadError(
                        f"Feature-count mismatch between ensemble members: "
                        f"XGBoost expects {xgb_n_features} features, "
                        f"GRU expects {gru_n_features} features. "
                        f"Both models must be trained on the same feature set."
                    )
            n_features = xgb_n_features
            logger.info("Ensemble feature contract: n_features=%d", n_features)

            # ── Ensemble weights ──────────────────────────────────────────────
            xgb_w = float((xgb_meta.training_metrics or {}).get("ensemble_weight", 0.75))
            if gru_meta is not None:
                gru_w = float((gru_meta.training_metrics or {}).get("ensemble_weight", 0.25))
                total = xgb_w + gru_w
                if not np.isclose(total, 1.0):
                    logger.warning("Ensemble weights sum to %.4f — normalising", total)
                    xgb_w /= total
                    gru_w /= total
            else:
                # XGBoost-only: weight = 1.0, GRU = 0.0.
                xgb_w = 1.0
                gru_w = 0.0

            # ── Load feature manifest from DB ─────────────────────────────────
            raw_feature_names: list[str] = xgb_meta.training_features or []
            if raw_feature_names and len(raw_feature_names) != n_features:
                logger.warning(
                    "Feature manifest length mismatch: DB has %d names but model expects %d. "
                    "Re-run backfill_feature_manifest.py to correct it.",
                    len(raw_feature_names), n_features,
                )
                raw_feature_names = []
            if not raw_feature_names:
                logger.warning(
                    "No feature manifest stored for model '%s' (n_features=%d). "
                    "FeatureLoader will use pipeline-level defaults. "
                    "Run: python scripts/backfill_feature_manifest.py",
                    xgb_meta.model_version, n_features,
                )
            feature_names = tuple(raw_feature_names)

            # ── Calibrators — manifest-aware, SHA-256 verified, gracefully degrade ─
            xgb_cal = self._load_calibrator_from_manifest(xgb_meta, "calibrator_xgb.pkl")
            gru_cal = (
                self._load_calibrator_from_manifest(gru_meta, "calibrator_gru.pkl")
                if gru_meta is not None
                else None
            )

            ensemble = LoadedEnsemble(
                xgboost_predictor = xgb_predictor,
                xgboost_weight    = xgb_w,
                gru_weight        = gru_w,
                xgboost_version   = xgb_meta.model_version,
                xgboost_model_id  = xgb_meta.model_id,
                loaded_at         = datetime.now(timezone.utc),
                n_features        = n_features,
                gru_session       = gru_session,
                gru_version       = gru_meta.model_version if gru_meta is not None else None,
                gru_model_id      = gru_meta.model_id      if gru_meta is not None else None,
                feature_names     = feature_names,
                xgb_calibrator    = xgb_cal,
                gru_calibrator    = gru_cal,
            )
            ensemble.validate()

            # Emit deployment state (3 = live) for both loaded models.
            # model_deployment_state: 0=retired, 1=shadow, 2=paper, 3=live
            model_deployment_state.labels(
                model_id=str(xgb_meta.model_id), model_name="xgboost"
            ).set(3)
            if gru_meta is not None:
                model_deployment_state.labels(
                    model_id=str(gru_meta.model_id), model_name="gru"
                ).set(3 if gru_session is not None else 1)

            # Emit accuracy scores from stored training metrics if available.
            for _metric in ("accuracy", "f1_score", "roc_auc"):
                _val = (xgb_meta.training_metrics or {}).get(_metric)
                if isinstance(_val, (int, float)):
                    model_accuracy_score.labels(
                        model_name="xgboost", metric_type=_metric
                    ).set(float(_val))
            if gru_meta is not None:
                for _metric in ("accuracy", "f1_score", "roc_auc"):
                    _val = (gru_meta.training_metrics or {}).get(_metric)
                    if isinstance(_val, (int, float)):
                        model_accuracy_score.labels(
                            model_name="gru", metric_type=_metric
                        ).set(float(_val))

            self._cached = ensemble
            if gru_session is not None:
                logger.info(
                    "Production ensemble loaded: XGBoost %s (%.0f%%) + GRU %s (%.0f%%)  "
                    "| n_features=%d  manifest=%s",
                    ensemble.xgboost_version, xgb_w * 100,
                    ensemble.gru_version,     gru_w * 100,
                    n_features, "yes" if feature_names else "no",
                )
            else:
                logger.info(
                    "Production ensemble loaded: XGBoost-only %s (100%%)  "
                    "| n_features=%d  manifest=%s  [GRU dormant]",
                    ensemble.xgboost_version, n_features, "yes" if feature_names else "no",
                )
            return ensemble

    async def warmup(self) -> None:
        """Run several synthetic inferences to eliminate JIT cold-start latency."""
        import tl2cgen
        ensemble = await self.load_production_ensemble()
        for _ in range(3):
            X_tab = _RNG.standard_normal((1, ensemble.n_features)).astype(np.float32)
            ensemble.xgboost_predictor.predict(tl2cgen.DMatrix(X_tab))
            if ensemble.gru_session is not None:
                X_seq = _RNG.standard_normal(
                    (1, ensemble.sequence_length, ensemble.n_features)
                ).astype(np.float32)
                ensemble.gru_session.run(
                    ensemble.gru_output_names, {ensemble.gru_input_name: X_seq}
                )
        logger.info(
            "Model warmup complete (3 iterations, n_features=%d, gru=%s)",
            ensemble.n_features,
            "yes" if ensemble.gru_session is not None else "dormant",
        )

    async def health_check(self) -> dict[str, Any]:
        try:
            ensemble = await self.load_production_ensemble()
            ensemble.validate()
            return {
                "status":  "healthy",
                "mode":    "full_ensemble" if ensemble.gru_session is not None else "xgboost_only",
                "xgboost": {"version": ensemble.xgboost_version, "weight": ensemble.xgboost_weight},
                "gru": {
                    "version": ensemble.gru_version,
                    "weight":  ensemble.gru_weight,
                    "active":  ensemble.gru_session is not None,
                },
                "loaded_at": ensemble.loaded_at.isoformat(),
            }
        except Exception as exc:
            logger.error("Health check failed: %s", exc, exc_info=True)
            return {"status": "unhealthy", "error": str(exc)}

    def unload(self) -> None:
        self._cached = None

    # ── private ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_calibrator_from_manifest(
        meta: MLModelMetadata,
        convention_name: str,
    ) -> ConfidenceCalibrator | None:
        """
        Load a calibrator using E2 artifact_manifest for exact path and integrity.

        Resolution order:
        1. E2 manifest: ``meta.lineage["artifact_manifest"]["calibrator"]["path"]``
           — absolute path registered at training time, SHA-256 verified before load.
        2. Convention fallback: sibling of ``onnx_path`` named ``convention_name``
           — for pre-E2 models that lack a lineage manifest.
        3. None — logs a warning and degrades to raw model probabilities.

        SHA == "absent" is a sentinel written when the model was intentionally
        registered without a calibrator artifact.  Returns None immediately.
        """
        lineage: dict = meta.lineage or {}
        manifest: dict = lineage.get("artifact_manifest") or {}
        cal_entry: dict = manifest.get("calibrator") or {}
        manifest_path_str: str | None = cal_entry.get("path")
        expected_sha: str | None = cal_entry.get("sha256")

        # ── E2 manifest path (preferred) ──────────────────────────────────────
        if manifest_path_str:
            if expected_sha == "absent":
                logger.info(
                    "Calibrator absent by design for %s v%s — running uncalibrated.",
                    meta.model_name, meta.model_version,
                )
                return None

            manifest_path = Path(manifest_path_str)
            if manifest_path.exists():
                if expected_sha:
                    actual_sha = _sha256_file(manifest_path)
                    if actual_sha != expected_sha:
                        logger.error(
                            "Calibrator integrity check FAILED for %s v%s: "
                            "expected=%s…  actual=%s…  — running uncalibrated.",
                            meta.model_name, meta.model_version,
                            expected_sha[:12], actual_sha[:12],
                        )
                        return None
                try:
                    cal = ConfidenceCalibrator.load(manifest_path)
                    logger.info(
                        "Calibrator loaded (manifest, SHA-256 verified) for %s v%s: %s",
                        meta.model_name, meta.model_version, manifest_path.name,
                    )
                    return cal
                except Exception as exc:
                    logger.warning(
                        "Calibrator load failed for %s v%s (%s) — running uncalibrated.",
                        meta.model_name, meta.model_version, exc,
                    )
                    return None
            else:
                logger.warning(
                    "Manifest calibrator path not found for %s v%s: %s — "
                    "trying convention fallback.",
                    meta.model_name, meta.model_version, manifest_path,
                )

        # ── Convention fallback: sibling of onnx_path (pre-E2 models) ─────────
        if meta.onnx_path:
            convention_path = Path(meta.onnx_path).parent / convention_name
            if convention_path.exists():
                try:
                    cal = ConfidenceCalibrator.load(convention_path)
                    logger.info(
                        "Calibrator loaded (convention fallback) for %s v%s: %s",
                        meta.model_name, meta.model_version, convention_path.name,
                    )
                    return cal
                except Exception as exc:
                    logger.warning(
                        "Calibrator convention load failed for %s v%s (%s) — running uncalibrated.",
                        meta.model_name, meta.model_version, exc,
                    )
                    return None

        logger.warning(
            "No calibrator found for %s v%s — running uncalibrated.",
            meta.model_name, meta.model_version,
        )
        return None

    @staticmethod
    def _try_load_calibrator(path: Path) -> ConfidenceCalibrator | None:
        """Load a calibrator from an explicit path; return None if absent or unreadable."""
        if not path.exists():
            logger.info("Calibrator not found at %s — running without calibration.", path.name)
            return None
        try:
            return ConfidenceCalibrator.load(path)
        except Exception as exc:
            logger.warning(
                "Failed to load calibrator from %s (%s) — running without calibration.",
                path, exc,
            )
            return None

    async def _fetch_production_record(self, model_name: str) -> MLModelMetadata:
        """Fetch active production record — raises ModelLoadError when absent."""
        record = await self._fetch_production_record_optional(model_name)
        if record is None:
            raise ModelLoadError(
                f"No active production model found for '{model_name}'. "
                f"Run: python scripts/promote_model.py production"
            )
        return record

    async def _fetch_production_record_optional(
        self, model_name: str
    ) -> MLModelMetadata | None:
        """Fetch active production record — returns None when absent (dormant pattern)."""
        stmt = (
            select(MLModelMetadata)
            .where(
                MLModelMetadata.model_name == model_name,
                MLModelMetadata.status     == "production",
                MLModelMetadata.is_active  == True,
            )
            .order_by(MLModelMetadata.deployed_at.desc())
            .limit(1)
        )
        record = (await self._session.execute(stmt)).scalar_one_or_none()
        if record is not None:
            logger.info(
                "Found: %s v%s (deployed %s)", model_name, record.model_version, record.deployed_at
            )
        else:
            logger.info("No active production record for '%s' (dormant or not yet registered)", model_name)
        return record

    async def _load_treelite(self, meta: MLModelMetadata) -> Any:
        try:
            import tl2cgen
        except ImportError as exc:
            raise ModelLoadError("tl2cgen not installed. Run: pip install tl2cgen") from exc

        path = Path(meta.onnx_path)
        if not path.exists():
            raise ModelLoadError(f"XGBoost Treelite library not found: {path}")
        if path.suffix not in {".so", ".dll", ".dylib"}:
            raise ModelLoadError(f"Expected Treelite library (.so/.dll/.dylib), got: {path.suffix}")

        if meta.checksum:
            await self._verify_checksum(path, meta.checksum)

        try:
            predictor = tl2cgen.Predictor(str(path))
            logger.info("XGBoost loaded via Treelite: %s (%d features)", path.name, predictor.num_feature)
            return predictor
        except Exception as exc:
            model_load_failures_total.labels(
                model_name="xgboost", failure_reason=type(exc).__name__
            ).inc()
            raise ModelLoadError(f"Failed to load Treelite model: {exc}") from exc

    async def _load_onnx(self, meta: MLModelMetadata) -> ort.InferenceSession:
        path = Path(meta.onnx_path)
        if not path.exists():
            raise ModelLoadError(f"GRU ONNX model not found: {path}")
        if path.suffix != ".onnx":
            raise ModelLoadError(f"Expected .onnx file, got: {path.suffix}")

        if meta.checksum:
            await self._verify_checksum(path, meta.checksum)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self._num_threads
        opts.inter_op_num_threads = self._num_threads
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

        providers = (
            ["CUDAExecutionProvider", "CPUExecutionProvider"] if self._use_gpu
            else ["CPUExecutionProvider"]
        )

        try:
            session = ort.InferenceSession(str(path), sess_options=opts, providers=providers)
            logger.info("GRU loaded via ONNX Runtime: %s (providers: %s)", path.name, session.get_providers())
            return session
        except Exception as exc:
            model_load_failures_total.labels(
                model_name="gru", failure_reason=type(exc).__name__
            ).inc()
            raise ModelLoadError(f"Failed to load ONNX model: {exc}") from exc

    @staticmethod
    async def _verify_checksum(path: Path, expected: str) -> None:
        def _compute() -> str:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()

        actual = await asyncio.to_thread(_compute)
        if actual != expected:
            raise ModelLoadError(
                f"Checksum mismatch for {path.name}: "
                f"expected {expected[:12]}…, got {actual[:12]}…"
            )
        logger.debug("Checksum verified: %s", path.name)
