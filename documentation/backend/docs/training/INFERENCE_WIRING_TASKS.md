# Cortex AI — Model Promotion & Inference Wiring
## Implementation Plan  |  Version 2.0.0  |  2026-04-20

---

## Current State

| Artifact | Location | Status |
|---|---|---|
| XGBoost model | `models/production/models/xgboost_model.json` | ✅ native .json |
| GRU model | `models/production/models/gru_model.keras` | ✅ Keras 3 |
| GRU ONNX | `models/production/onnx/gru_optimized.onnx` | ✅ exported |
| XGBoost ONNX | — | ⚠️ not yet exported — native export available (T0.5) |
| Registry (XGBoost) | DB id=12, status=development, accuracy=0 | ⚠️ metrics missing — fix in T0.3 |
| Registry (GRU) | DB id=13, status=development, accuracy=0 | ⚠️ metrics missing — fix in T0.3 |
| Ensemble weights | XGB=0.75, GRU=0.25 | ✅ optimized on 30k val samples |
| Inference endpoint | `/predict` | ❌ random features, no real model |
| `require_admin_role` dependency | — | ❌ does not exist — create in T3.0 |

## Performance Baseline (evaluation on 30k validation samples)

| Model | Accuracy | F1(UP) | F1(DOWN) |
|---|---|---|---|
| XGBoost | **65.81%** | 0.655 | 0.661 |
| GRU | 53.17% | 0.595 | 0.445 |
| Ensemble (0.75/0.25) | **65.14%** | 0.656 | 0.647 |

XGBoost dominates — expected for tabular financial features.
Sharpe=0.0 is a metrics calculation bug tracked separately (not a model quality issue).

---

## Architecture: Fully-ONNX Inference

**Decision:** Both XGBoost and GRU run through ONNX Runtime.

XGBoost 2.0.3 (installed) has **native ONNX export** via `booster.save_model('model.onnx')`.
This bypasses onnxmltools entirely and eliminates the boolean-type error.
ONNX Runtime 1.19.2 (installed) is fully compatible.

| Model | Format at rest | Loaded as | Load path |
|---|---|---|---|
| XGBoost | `.onnx` (native XGB 2.0.3 export) | `ort.InferenceSession` | raw bytes from registry |
| GRU | `gru_optimized.onnx` | `ort.InferenceSession` | raw bytes from registry |

Both models: decrypted in-process → bytes fed to ONNX Runtime directly → no temp files,
no filesystem paths in business logic, unified inference interface.

---

## Clarifications Resolved

| # | Question | Answer |
|---|---|---|
| 1 | Is `upstox_ohlcv` fresh enough for live features? | ✅ Yes — data ingestion worker backfills all gaps continuously; WebSocket handles live streaming |
| 2 | Rate limiting approach? | `slowapi` + Redis (IP-based) already configured. Add per-user tier in `ml_auth_utils.py`. Details in T3.2. |
| 3 | Does `require_admin_role` exist? | ❌ No — JWT auth only, no RBAC. Must be created (T3.0). |
| 4 | Sharpe=0 — fix now or later? | Track as separate issue. Does not block inference wiring. |
| 5 | XGBoost ONNX — native or hybrid? | ✅ Native export (`booster.save_model('model.onnx')`) in XGBoost 2.0.3. Fully-ONNX path adopted. |

---

## Known Bugs to Fix First (before any phase)

### Bug 1 — `ModelRegistry.load_model_artifact` references non-existent field
```python
# app/ml/model_registry.py line 207 — WRONG:
artifact_path = Path(model.artifact_path)   # MLModelMetadata has no artifact_path

# CORRECT:
artifact_path = Path(model.onnx_path)
```

### Bug 2 — `promote_to_production` / `get_latest_model` filter on non-existent `model_type` column
```python
# WRONG (model_type column does not exist):
stmt.where(MLModel.model_type == model_type)
current_production = await self.get_production_model(model_type=model.model_type)

# CORRECT (use model_name):
stmt.where(MLModel.model_name == model_name)
current_production = await self.get_production_model(model_name=model.model_name)
```

### Bug 3 — Registry records registered with `accuracy=0`
Step 10 calls `register_model(metrics={})` because `self.results` is None at that
point. Fix: pass `evaluation_results` explicitly (T0.4). Backfill existing records
with the real metrics now (T0.3).

### Bug 4 — Prediction endpoint uses random feature vector
```python
# app/api/v1/ml_predictions.py line 52 — placeholder must be replaced:
feature_vector = np.random.randn(60, 42)   # WRONG
# CORRECT: use FeatureLoader (T3.2)
```

---

## Rate Limiting Strategy

Two independent layers — both already in the codebase, both need tuning:

**Layer 1 — `slowapi` (IP-based, Redis-backed, `app/core/limiter.py`)**
- Current: `100000/hour` global default (load-test placeholder)
- Updated: `500/minute` per IP on the predict endpoint
- Admin reload endpoint: no slowapi limit (protected by admin role instead)

**Layer 2 — DB-backed per-user (ml_auth_utils.py `check_user_rate_limit()`)**
- Current: `100 predictions / 60 min` (hardcoded default)
- Adopt tiered model via JWT `tier` claim:
  - `standard`: 100 req/hour (existing default, keep)
  - `premium`: 1000 req/hour (add tier check)
  - `internal`: unlimited (ML team / system accounts)
- The DB-backed layer is the authoritative per-user limit.
  The slowapi layer is a coarse IP-level DoS guard.

---

## Phase 0 — Pre-Requisites & Bug Fixes
**Target: 3–4 hours | Must complete before any other phase**

---

### T0.1 — Set a permanent `ML_MODEL_ENCRYPTION_KEY`

The key generated during training was ephemeral (logged as a warning and discarded).
Models in the registry are encrypted with that key. Without it, every `load_model_artifact()`
call will raise `InvalidToken`.

**If the original key is in the training log, extract it. Otherwise, re-register the
models using the plaintext artifacts on disk under the new permanent key (run T0.5
first to produce the XGBoost ONNX, then re-run `register_model` for both models).**

```bash
# Generate a new permanent Fernet key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Set in .env (permanent — never regenerate unless rotating):
ML_MODEL_ENCRYPTION_KEY=<base64-encoded-32-byte-key>
ML_MODEL_STORAGE_PATH=models/production/models
```

---

### T0.2 — Fix `ModelRegistry` field-name bugs

**File:** `app/ml/model_registry.py`

Three method signatures / field references must be corrected. All use `model_type`
(doesn't exist) instead of `model_name`, and `artifact_path` instead of `onnx_path`.

```python
# 1. load_model_artifact — line ~207
async def load_model_artifact(self, model: MLModel) -> bytes:
    artifact_path = Path(model.onnx_path)       # was: model.artifact_path
    if not artifact_path.exists():
        raise FileNotFoundError(f"Model artifact not found: {artifact_path}")
    ...

# 2. get_latest_model — rename parameter model_type → model_name
async def get_latest_model(
    self,
    model_name: str | None = None,              # was: model_type
    status: str | None = None,
) -> MLModel | None:
    stmt = select(MLModel).order_by(MLModel.created_at.desc())
    if model_name:
        stmt = stmt.where(MLModel.model_name == model_name)   # was: MLModel.model_type
    if status:
        stmt = stmt.where(MLModel.status == status)
    result = await self._session.execute(stmt.limit(1))
    return result.scalar_one_or_none()

# 3. get_production_model — update parameter name
async def get_production_model(
    self,
    model_name: str | None = None,              # was: model_type
) -> MLModel | None:
    return await self.get_latest_model(model_name=model_name, status="production")

# 4. promote_to_production — use model_name in get_production_model call
async def promote_to_production(self, version: str, demote_current: bool = True) -> MLModel:
    model = await self.get_model(version)
    if not model:
        raise ValueError(f"Model version {version} not found")
    if model.status == "production":
        raise ValueError(f"Model {version} is already in production")
    if demote_current:
        current_production = await self.get_production_model(
            model_name=model.model_name         # was: model_type=model.model_type
        )
        if current_production:
            current_production.status = "staging"
            current_production.updated_at = datetime.now(timezone.utc)
    model.status = "production"
    model.is_active = True
    model.updated_at = datetime.now(timezone.utc)
    model.deployed_at = datetime.now(timezone.utc)
    await self._session.commit()
    await self._session.refresh(model)
    logger.info("Promoted model %s to production", version)
    return model
```

---

### T0.3 — Backfill evaluation metrics into registry records

The training results JSON at `models/production/training_results_20260420_114151.json`
has the correct metrics. This script updates the two existing DB records.

**File:** `scripts/backfill_model_metrics.py` *(new — run once)*

```python
"""
One-time script: backfill evaluation metrics into model registry records
that were registered with accuracy=0.

Run:
    python scripts/backfill_model_metrics.py
"""
import asyncio, json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.ml_data import MLModelMetadata

RESULTS_PATH = Path("models/production/training_results_20260420_114151.json")

METRIC_MAP = {
    "1.0.0_xgboost": "xgboost",
    "1.0.0_gru":     "gru",
}

# Ensemble weights from the optimizer output (XGB=0.75, GRU=0.25)
ENSEMBLE_WEIGHTS = {
    "1.0.0_xgboost": 0.75,
    "1.0.0_gru":     0.25,
}

async def main() -> None:
    with open(RESULTS_PATH) as f:
        results = json.load(f)
    eval_results = results["evaluation_results"]

    settings = get_settings()
    engine   = create_async_engine(str(settings.DATABASE_URL), echo=False)
    Session  = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        for version, model_key in METRIC_MAP.items():
            stmt   = select(MLModelMetadata).where(MLModelMetadata.model_version == version)
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            if not record:
                print(f"WARNING: {version} not found in DB — skipping")
                continue

            metrics = eval_results.get(model_key, {})
            # Store ensemble_weight so RegistryModelLoader can read it later
            metrics["ensemble_weight"] = ENSEMBLE_WEIGHTS[version]

            record.training_metrics   = metrics
            record.validation_metrics = metrics
            # Total training samples (from training_results)
            record.training_samples   = results.get("total_samples", 0)

            acc = metrics.get("accuracy", 0.0)
            print(f"Updated {version}: accuracy={acc:.4f}  ensemble_weight={metrics['ensemble_weight']}")

        await session.commit()
        print("Done — registry metrics updated.")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
```

---

### T0.4 — Fix orchestrator: pass evaluation metrics to `register_model`

Prevents the accuracy=0 bug from recurring on future training runs.

**File:** `scripts/production_training_orchestrator.py`

Root cause: `self.results` is `None` at step 10 — it is only assigned after all 10
steps complete. Fix: pass `evaluation_results` as an explicit parameter.

```python
# In run(), replace the step 10 call:
model_paths = await self._register_models_in_registry(evaluation_results)

# Update the method signature:
async def _register_models_in_registry(
    self,
    evaluation_results: Dict[str, EvaluationResults],
) -> Dict[str, str]:
    ...
    xgb_meta = await registry.register_model(
        version       = f"{self.config.model_version}_xgboost",
        model_type    = "xgboost",
        artifact_path = model_paths['xgboost'],
        metrics       = {                               # was: {} if self.results else {}
            **asdict(evaluation_results['xgboost']),
            "ensemble_weight": self.ensemble_trainer.weights.get('xgboost', 0.75),
        },
        ...
    )
    gru_meta = await registry.register_model(
        version       = f"{self.config.model_version}_gru",
        model_type    = "gru",
        artifact_path = model_paths['gru'],
        metrics       = {
            **asdict(evaluation_results['gru']),
            "ensemble_weight": self.ensemble_trainer.weights.get('gru', 0.25),
        },
        ...
    )
```

---

### T0.5 — Export XGBoost to ONNX (one-time, using native XGBoost 2.0.3)

XGBoost 2.0.3 supports `booster.save_model('model.onnx')` natively — no onnxmltools
required. This produces a valid ONNX file that ONNX Runtime 1.19.2 reads directly.

**File:** `scripts/export_xgboost_onnx.py` *(new — run once)*

```python
"""
One-time script: export the trained XGBoost Booster to ONNX using
XGBoost 2.0.3's native export.  Registers the ONNX file in the registry.

Run:
    python scripts/export_xgboost_onnx.py

XGBoost 2.0.3 native export produces ONNX opset 15, compatible with
ONNX Runtime 1.19.2.  No onnxmltools or skl2onnx required.
"""
import asyncio, logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import onnxruntime as ort
import xgboost as xgb

from app.core.config import get_settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

XGB_JSON_PATH = Path("models/production/models/xgboost_model.json")
XGB_ONNX_PATH = Path("models/production/onnx/xgboost_model.onnx")
N_FEATURES    = 47


def export() -> None:
    # Load existing trained Booster
    booster = xgb.Booster()
    booster.load_model(str(XGB_JSON_PATH))
    logger.info("Loaded XGBoost Booster from %s", XGB_JSON_PATH)
    logger.info("Features: %d", booster.num_features())

    assert booster.num_features() == N_FEATURES, (
        f"Expected {N_FEATURES} features, got {booster.num_features()}"
    )

    # Native ONNX export — XGBoost 2.0+ detects .onnx extension automatically
    XGB_ONNX_PATH.parent.mkdir(parents=True, exist_ok=True)
    booster.save_model(str(XGB_ONNX_PATH))
    logger.info("ONNX model saved → %s", XGB_ONNX_PATH)

    # Validate with ONNX Runtime
    session    = ort.InferenceSession(str(XGB_ONNX_PATH))
    input_name = session.get_inputs()[0].name
    test_data  = np.random.default_rng(42).standard_normal(
        (5, N_FEATURES)
    ).astype(np.float32)

    outputs = session.run(None, {input_name: test_data})
    proba   = outputs[0]
    logger.info("Validation passed  output_shape=%s  sample=%s", proba.shape, proba[:2])

    assert proba.shape[0] == 5,          "Wrong batch size in output"
    assert not np.any(np.isnan(proba)),  "NaN in ONNX output"
    logger.info("✓ XGBoost ONNX export validated successfully")


if __name__ == "__main__":
    export()
```

After running this script, update the registry record to point to the ONNX file:

```python
# Append to backfill_model_metrics.py or run separately:
# Update onnx_path on the xgboost registry record
record.onnx_path = str(XGB_ONNX_PATH)
```

---

### T0.6 — Fix orchestrator ONNX export for future training runs

Replace the broken `onnxmltools` path with native XGBoost export.

**File:** `scripts/production_training_orchestrator.py`, method `_export_models_to_onnx`

```python
async def _export_models_to_onnx(self) -> Dict[str, str]:
    onnx_paths: Dict[str, str] = {}

    # ── XGBoost: native ONNX export (XGBoost 2.0+) ──────────────────
    logger.info("Exporting XGBoost to ONNX (native)...")
    try:
        xgb_onnx_path = self.onnx_dir / "xgboost_model.onnx"
        self.xgboost_trainer.model.save_model(str(xgb_onnx_path))

        # Validate with ONNX Runtime before accepting
        import onnxruntime as ort
        session    = ort.InferenceSession(str(xgb_onnx_path))
        input_name = session.get_inputs()[0].name
        test_data  = np.zeros((1, self.config.n_features), dtype=np.float32)
        session.run(None, {input_name: test_data})

        onnx_paths['xgboost'] = str(xgb_onnx_path)
        logger.info("  ✓ XGBoost → %s", xgb_onnx_path)
    except Exception as e:
        logger.error("XGBoost ONNX export failed: %s", e)

    # ── GRU: tf2onnx (unchanged) ────────────────────────────────────
    logger.info("Exporting GRU to ONNX...")
    try:
        import tf2onnx
        gru_path   = self.onnx_dir / "gru_model.onnx"
        onnx_model, _ = tf2onnx.convert.from_keras(self.gru_trainer.model, opset=11)
        with open(gru_path, 'wb') as fh:
            fh.write(onnx_model.SerializeToString())
        onnx_paths['gru'] = str(gru_path)
        logger.info("  ✓ GRU → %s", gru_path)
    except Exception as e:
        logger.error("GRU ONNX export failed: %s", e)

    # ── Optimise both ───────────────────────────────────────────────
    for model_name, onnx_path in list(onnx_paths.items()):
        try:
            opt_path = self.onnx_dir / f"{model_name}_optimized.onnx"
            opt_str  = await asyncio.to_thread(
                self.onnx_converter.optimize_onnx, onnx_path, str(opt_path)
            )
            onnx_paths[f"{model_name}_optimized"] = opt_str
        except Exception as e:
            logger.warning("ONNX optimisation failed for %s: %s", model_name, e)

    logger.info("✓ ONNX export complete")
    return onnx_paths
```

---

## Phase 1 — Registry-Aware Model Loader
**Target: 1 day**

The core abstraction: `RegistryModelLoader` decrypts artifacts from the registry and
returns a `LoadedEnsemble` — an immutable frozen dataclass holding two live ONNX Runtime
sessions. All downstream code (predictor, API) operates on this object.
No filesystem paths leak into business logic after startup.

---

### T1.1 — Create `RegistryModelLoader` + `LoadedEnsemble`

**File:** `app/ml/inference/registry_model_loader.py` *(new)*

```python
"""
Cortex AI — Registry-Aware Model Loader
========================================
Decrypts model artifacts from the registry, loads them into ONNX Runtime
sessions, and assembles an immutable LoadedEnsemble for inference.

Architecture (fully-ONNX):
    XGBoost  →  xgboost_model.onnx  →  ort.InferenceSession
    GRU      →  gru_optimized.onnx  →  ort.InferenceSession

Both artifacts are decrypted in-process.  The raw bytes are fed directly
into ONNX Runtime — nothing is written back to disk.
SHA-256 checksum verified on every load (integrity gate).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import onnxruntime as ort
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.model_registry import ModelRegistry
from app.models.ml_data import MLModelMetadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedEnsemble:
    """
    Immutable, thread-safe container for a validated production ensemble.

    Both models are already-loaded ONNX Runtime sessions.  Once constructed
    this object is safe to share across async request handlers without locking.

    Attributes:
        xgboost_session:  ONNX Runtime session for XGBoost (tabular input)
        gru_session:      ONNX Runtime session for GRU (sequence input)
        xgboost_weight:   Ensemble weight for XGBoost  (e.g. 0.75)
        gru_weight:       Ensemble weight for GRU      (e.g. 0.25)
        xgboost_version:  Registry version string
        gru_version:      Registry version string
        loaded_at:        UTC timestamp of this load event
        n_features:       Number of input features (47)
        sequence_length:  GRU lookback length (60)
    """
    xgboost_session:  ort.InferenceSession
    gru_session:      ort.InferenceSession
    xgboost_weight:   float
    gru_weight:       float
    xgboost_version:  str
    gru_version:      str
    loaded_at:        datetime
    n_features:       int = 47
    sequence_length:  int = 60

    def predict_xgboost(self, X_tab: np.ndarray) -> np.ndarray:
        """
        Run XGBoost inference.

        Args:
            X_tab: shape (n_samples, n_features) float32

        Returns:
            P(class=1 / UP), shape (n_samples,)
        """
        input_name = self.xgboost_session.get_inputs()[0].name
        result     = self.xgboost_session.run(None, {input_name: X_tab.astype(np.float32)})
        # XGBoost native ONNX export: output is probabilities array [P(DOWN), P(UP)]
        # or a flat probability depending on export mode.
        out = result[0]
        if out.ndim == 2:
            return out[:, 1]    # P(UP) from [P(DOWN), P(UP)]
        return out              # flat P(UP) for binary:logistic

    def predict_gru(self, X_seq: np.ndarray) -> np.ndarray:
        """
        Run GRU sequence inference.

        Args:
            X_seq: shape (n_samples, sequence_length, n_features) float32

        Returns:
            Softmax probabilities [P(DOWN), P(UP)], shape (n_samples, 2)
        """
        input_name = self.gru_session.get_inputs()[0].name
        result     = self.gru_session.run(None, {input_name: X_seq.astype(np.float32)})
        return result[0]    # shape (n, 2)

    def predict_ensemble(
        self,
        X_tab: np.ndarray,
        X_seq: np.ndarray,
    ) -> np.ndarray:
        """
        Weighted ensemble of XGBoost + GRU predictions.

        Args:
            X_tab: Tabular features  (n, n_features)
            X_seq: Sequence features (n, sequence_length, n_features)

        Returns:
            Ensemble P(UP), shape (n,) — values in [0, 1]
        """
        p_xgb = self.predict_xgboost(X_tab)        # (n,)
        p_gru = self.predict_gru(X_seq)[:, 1]      # (n,) — P(UP) column
        return self.xgboost_weight * p_xgb + self.gru_weight * p_gru


class RegistryModelLoader:
    """
    Decrypts and loads the XGBoost + GRU model pair from the model registry.

    Typical usage (application startup):
        loader   = RegistryModelLoader(session, storage_path, encryption_key)
        ensemble = await loader.load_production_ensemble()
        app.state.ensemble = ensemble   # immutable — safe to share

    Hot-reload after model promotion:
        new_ensemble = await loader.load_production_ensemble()
        app.state.ensemble = new_ensemble   # atomic reference swap
    """

    def __init__(
        self,
        session:        AsyncSession,
        storage_path:   str,
        encryption_key: str,
        num_threads:    int = 4,
    ) -> None:
        self._registry    = ModelRegistry(session, storage_path, encryption_key)
        self._num_threads = num_threads

    @staticmethod
    def _sess_options(num_threads: int) -> ort.SessionOptions:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = num_threads
        opts.inter_op_num_threads = num_threads
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        return opts

    async def load_production_ensemble(self) -> LoadedEnsemble:
        """
        Locate, decrypt, and load the current production XGBoost + GRU pair.

        Raises:
            RuntimeError: No production model found, or checksum / decryption failure.
        """
        logger.info("Loading production ensemble from registry...")

        xgb_record = await self._registry.get_production_model(model_name="xgboost")
        gru_record  = await self._registry.get_production_model(model_name="gru")

        if xgb_record is None:
            raise RuntimeError(
                "No production XGBoost model found. "
                "Run: python scripts/promote_models.py --to production"
            )
        if gru_record is None:
            raise RuntimeError(
                "No production GRU model found. "
                "Run: python scripts/promote_models.py --to production"
            )

        return await self._build_ensemble(xgb_record, gru_record)

    async def load_staging_ensemble(self) -> LoadedEnsemble:
        """Load the current staging pair (for smoke tests before promotion)."""
        xgb_record = await self._registry.get_latest_model(model_name="xgboost", status="staging")
        gru_record  = await self._registry.get_latest_model(model_name="gru",     status="staging")
        if not xgb_record or not gru_record:
            raise RuntimeError(
                "No staging models found. "
                "Run: python scripts/promote_models.py --to staging"
            )
        return await self._build_ensemble(xgb_record, gru_record)

    async def _build_ensemble(
        self,
        xgb_record: MLModelMetadata,
        gru_record:  MLModelMetadata,
    ) -> LoadedEnsemble:
        xgb_session = await self._load_onnx_session(xgb_record, "XGBoost")
        gru_session = await self._load_onnx_session(gru_record,  "GRU")
        xgb_w, gru_w = self._extract_weights(xgb_record)

        ensemble = LoadedEnsemble(
            xgboost_session = xgb_session,
            gru_session     = gru_session,
            xgboost_weight  = xgb_w,
            gru_weight      = gru_w,
            xgboost_version = xgb_record.model_version,
            gru_version     = gru_record.model_version,
            loaded_at       = datetime.now(timezone.utc),
        )
        logger.info(
            "Ensemble loaded  XGB=%s (w=%.2f)  GRU=%s (w=%.2f)",
            ensemble.xgboost_version, xgb_w,
            ensemble.gru_version,     gru_w,
        )
        return ensemble

    async def _load_onnx_session(
        self,
        record: MLModelMetadata,
        label:  str,
    ) -> ort.InferenceSession:
        """
        Decrypt the registry artifact and load it as an ONNX Runtime session.
        ONNX Runtime accepts raw bytes directly — no temp file needed.
        """
        raw_bytes = await self._registry.load_model_artifact(record)
        session   = ort.InferenceSession(
            raw_bytes,
            sess_options = self._sess_options(self._num_threads),
            providers    = ["CPUExecutionProvider"],
        )
        logger.debug("%s ONNX session loaded  version=%s", label, record.model_version)
        return session

    @staticmethod
    def _extract_weights(xgb_record: MLModelMetadata) -> tuple[float, float]:
        """
        Read the ensemble weight stored by the orchestrator in `training_metrics`.
        Falls back to the optimizer result (0.75 / 0.25) if not present.
        """
        metrics = xgb_record.training_metrics or {}
        xgb_w   = float(metrics.get("ensemble_weight", 0.75))
        return xgb_w, 1.0 - xgb_w
```

---

### T1.2 — Add `EnsemblePredictor.from_loaded_ensemble()` factory

**File:** `app/ml/inference/ensemble_predictor.py`

Add a class method factory so the API can instantiate the predictor from a
`LoadedEnsemble` without any filesystem access.

```python
@classmethod
def from_loaded_ensemble(
    cls,
    ensemble:             "LoadedEnsemble",
    cache:                CacheService | None = None,
    confidence_threshold: float               = 0.60,
) -> "EnsemblePredictor":
    """
    Production path: create predictor from a registry-loaded ensemble.
    Models are already decrypted and loaded — no paths, no I/O.
    """
    predictor                       = cls.__new__(cls)
    predictor.cache                 = cache
    predictor.confidence_threshold  = confidence_threshold
    predictor._ensemble             = ensemble
    predictor.xgboost_weight        = ensemble.xgboost_weight
    predictor.gru_weight            = ensemble.gru_weight
    return predictor

async def predict(
    self,
    features_tabular:  np.ndarray,
    features_sequence: np.ndarray,
    symbol:            str,
    current_price:     float,
    volatility:        float | None = None,
    timeframe:         str          = "1D",
) -> dict[str, Any]:
    """
    Generate ensemble prediction.
    Accepts single-sample (1D tabular, 2D sequence) or batched inputs.
    """
    if features_tabular.ndim == 1:
        features_tabular  = features_tabular[np.newaxis, :]   # (1, n_features)
    if features_sequence.ndim == 2:
        features_sequence = features_sequence[np.newaxis, :]  # (1, seq, feat)

    cache_key = f"pred:{symbol}:{timeframe}:{self._ensemble.xgboost_version}"
    if self.cache:
        cached = await self._get_cached_prediction(cache_key)
        if cached:
            return cached

    p_up   = float(self._ensemble.predict_ensemble(features_tabular, features_sequence)[0])
    p_down = 1.0 - p_up

    confidence = max(p_up, p_down)
    direction  = (
        "HOLD" if confidence < self.confidence_threshold
        else ("UP" if p_up > p_down else "DOWN")
    )

    atr    = volatility or self._estimate_volatility(features_tabular[0])
    result = self._build_signal(direction, confidence, current_price, atr, p_up, p_down)

    if self.cache:
        await self._cache_prediction(cache_key, result, ttl=300)

    return result
```

---

### T1.3 — Smoke test for loaded models

**File:** `scripts/test_inference.py` *(new)*

```python
"""
Smoke test — load staging or production ensemble, run synthetic inference,
assert shape + value constraints.

Usage:
    python scripts/test_inference.py            # tests production
    python scripts/test_inference.py --staging  # tests staging
"""
import argparse, asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.ml.inference.registry_model_loader import RegistryModelLoader

N_FEATURES = 47
SEQ_LEN    = 60

async def run(staging: bool) -> None:
    settings = get_settings()
    if not settings.ML_MODEL_ENCRYPTION_KEY:
        raise SystemExit("ML_MODEL_ENCRYPTION_KEY not set in .env")

    engine  = create_async_engine(str(settings.DATABASE_URL), echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        loader = RegistryModelLoader(
            session        = session,
            storage_path   = settings.ML_MODEL_STORAGE_PATH,
            encryption_key = settings.ML_MODEL_ENCRYPTION_KEY,
        )
        ensemble = (
            await loader.load_staging_ensemble()    if staging
            else await loader.load_production_ensemble()
        )

    await engine.dispose()

    rng   = np.random.default_rng(42)
    X_tab = rng.standard_normal((10, N_FEATURES)).astype(np.float32)
    X_seq = rng.standard_normal((10, SEQ_LEN, N_FEATURES)).astype(np.float32)

    p_up = ensemble.predict_ensemble(X_tab, X_seq)

    assert p_up.shape  == (10,),           f"Wrong shape: {p_up.shape}"
    assert np.all(p_up >= 0),              "Negative probabilities"
    assert np.all(p_up <= 1),              "Probabilities > 1"
    assert not np.any(np.isnan(p_up)),     "NaN in output"

    stage = "staging" if staging else "production"
    print(f"✓ Smoke test passed [{stage}]")
    print(f"  XGBoost : {ensemble.xgboost_version}  (weight={ensemble.xgboost_weight:.2f})")
    print(f"  GRU     : {ensemble.gru_version}       (weight={ensemble.gru_weight:.2f})")
    print(f"  P(UP) range : [{p_up.min():.3f}, {p_up.max():.3f}]  mean={p_up.mean():.3f}")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.staging))

if __name__ == "__main__":
    main()
```

---

## Phase 2 — Quality Gates & Model Promotion
**Target: 4–6 hours**

---

### T2.1 — Add `ModelPromoter` + `QualityGate` to `model_registry.py`

**File:** `app/ml/model_registry.py` — append at end of file

```python
from datetime import datetime, timezone  # already imported above


class QualityGate:
    """Minimum thresholds for model promotion."""
    MIN_ACCURACY:         float = 0.52    # above random for noisy financial data
    MIN_F1_UP:            float = 0.50
    MIN_F1_DOWN:          float = 0.40
    MIN_TRAINING_SAMPLES: int   = 100_000


class QualityGateError(ValueError):
    """Raised when a model fails a quality gate check."""


class ModelPromoter:
    """
    Validates quality gates and executes lifecycle transitions atomically.

    Transitions:
        development → staging     (promote_to_staging)
        staging     → production  (promote_to_production)
        any         ← rollback    (rollback_to_previous)
    """

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    async def promote_to_staging(self, version: str) -> MLModel:
        """development → staging (quality gate must pass)."""
        model = await self._registry.get_model(version)
        if not model:
            raise ValueError(f"Version '{version}' not found in registry")
        if model.status != "development":
            raise ValueError(
                f"Expected status=development, got status={model.status}. "
                f"Only development models can be promoted to staging."
            )
        self._assert_quality(model)
        model.status     = "staging"
        model.updated_at = datetime.now(timezone.utc)
        await self._registry._session.commit()
        await self._registry._session.refresh(model)
        logger.info("✓ Promoted %s  development → staging", version)
        return model

    async def promote_to_production(
        self,
        version:            str,
        skip_staging_check: bool = False,
    ) -> MLModel:
        """
        staging → production.

        Args:
            version:             Registry version to promote.
            skip_staging_check:  Bypass staging requirement (emergency only — document why).
        """
        model = await self._registry.get_model(version)
        if not model:
            raise ValueError(f"Version '{version}' not found in registry")
        if not skip_staging_check and model.status != "staging":
            raise ValueError(
                f"Expected status=staging, got status={model.status}. "
                f"Promote to staging first, or use --skip-staging (document the reason)."
            )
        self._assert_quality(model)
        return await self._registry.promote_to_production(version, demote_current=True)

    @staticmethod
    def _assert_quality(model: MLModel) -> None:
        metrics = model.training_metrics or {}
        acc     = float(metrics.get("accuracy", 0.0))
        f1      = metrics.get("f1_score", {})
        f1_up   = float(f1.get("up",   0.0)) if isinstance(f1, dict) else 0.0
        f1_dn   = float(f1.get("down", 0.0)) if isinstance(f1, dict) else 0.0
        samples = int(model.training_samples or 0)

        failures = []
        if acc     < QualityGate.MIN_ACCURACY:
            failures.append(f"accuracy {acc:.4f} < minimum {QualityGate.MIN_ACCURACY}")
        if f1_up   < QualityGate.MIN_F1_UP:
            failures.append(f"F1(UP) {f1_up:.4f} < minimum {QualityGate.MIN_F1_UP}")
        if f1_dn   < QualityGate.MIN_F1_DOWN:
            failures.append(f"F1(DOWN) {f1_dn:.4f} < minimum {QualityGate.MIN_F1_DOWN}")
        if samples < QualityGate.MIN_TRAINING_SAMPLES:
            failures.append(
                f"training_samples {samples:,} < minimum {QualityGate.MIN_TRAINING_SAMPLES:,}"
            )

        if failures:
            raise QualityGateError(
                f"Quality gate FAILED for '{model.model_version}':\n"
                + "\n".join(f"  • {f}" for f in failures)
            )
        logger.info(
            "Quality gate PASSED  version=%s  acc=%.4f  F1(UP)=%.4f  F1(DOWN)=%.4f",
            model.model_version, acc, f1_up, f1_dn,
        )
```

---

### T2.2 — Create promotion CLI

**File:** `scripts/promote_models.py` *(new)*

```python
"""
Cortex AI — Model Promotion CLI
==================================
Promotes the XGBoost + GRU pair through the lifecycle.

Usage:
    python scripts/promote_models.py --to staging
    python scripts/promote_models.py --to production
    python scripts/promote_models.py --to production --skip-staging
    python scripts/promote_models.py --rollback
"""
import argparse, asyncio, logging, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.ml.model_registry import ModelRegistry, ModelPromoter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

XGBOOST_VERSION = "1.0.0_xgboost"
GRU_VERSION     = "1.0.0_gru"


async def run(target: str | None, skip_staging: bool, rollback: bool) -> None:
    settings = get_settings()
    if not settings.ML_MODEL_ENCRYPTION_KEY:
        raise SystemExit("ML_MODEL_ENCRYPTION_KEY is not set. Add it to .env first.")

    engine  = create_async_engine(str(settings.DATABASE_URL), echo=False)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        registry = ModelRegistry(
            session,
            model_storage_path = settings.ML_MODEL_STORAGE_PATH,
            encryption_key     = settings.ML_MODEL_ENCRYPTION_KEY,
        )
        promoter = ModelPromoter(registry)

        if rollback:
            xgb = await registry.rollback_model()
            gru = await registry.rollback_model()
            logger.info("Rollback complete  XGBoost→%s  GRU→%s", xgb.model_version, gru.model_version)

        elif target == "staging":
            xgb = await promoter.promote_to_staging(XGBOOST_VERSION)
            gru = await promoter.promote_to_staging(GRU_VERSION)
            logger.info("✓ Staged: %s  %s", xgb.model_version, gru.model_version)
            logger.info("Next: python scripts/test_inference.py --staging")

        elif target == "production":
            xgb = await promoter.promote_to_production(XGBOOST_VERSION, skip_staging_check=skip_staging)
            gru = await promoter.promote_to_production(GRU_VERSION,     skip_staging_check=skip_staging)
            logger.info("✓ Production: %s  %s", xgb.model_version, gru.model_version)
            logger.info("Hot-reload running server: POST /api/v1/admin/reload")

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Cortex AI Model Promotion")
    parser.add_argument("--to",           choices=["staging", "production"])
    parser.add_argument("--skip-staging", action="store_true",
                        help="Emergency: bypass staging requirement (document the reason)")
    parser.add_argument("--rollback",     action="store_true",
                        help="Rollback production to previous staging version")
    args = parser.parse_args()
    if not args.rollback and not args.to:
        parser.error("Provide --to <stage> or --rollback")
    asyncio.run(run(args.to, args.skip_staging, args.rollback))


if __name__ == "__main__":
    main()
```

---

## Phase 3 — API Wiring
**Target: 1 day**

---

### T3.0 — Create `require_admin_role` dependency  *(PREREQUISITE for T3.3)*

`require_admin_role` does not exist. The JWT infrastructure is in place (`get_current_user`
returns `{"user_id": str}`). The admin check reads an `is_admin` boolean from the JWT
payload via `get_current_user_id` in `app/core/security.py`.

**File:** `app/api/deps.py` — add below `get_current_user`

```python
from fastapi import Depends, HTTPException, status
from app.core.security import get_current_user_id   # already imported


async def require_admin_role(
    user: dict = Depends(get_current_user),
) -> dict:
    """
    FastAPI dependency: requires the authenticated user to have admin privileges.

    The `is_admin` flag is embedded in the JWT payload by the auth service at
    token issuance time.  This dependency rejects any token where is_admin is
    absent or False, returning HTTP 403.

    Usage:
        @router.post("/admin/reload")
        async def reload(current_user: dict = Depends(require_admin_role)):
            ...
    """
    if not user.get("is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error":   "forbidden",
                "message": "Admin privileges required for this endpoint.",
            },
        )
    return user
```

**Note:** `get_current_user_id` (in `app/core/security.py`) must decode the JWT and
include `is_admin` in the returned payload. Verify this is the case before wiring.
If the JWT doesn't carry `is_admin`, add it to the token issuance logic.

---

### T3.1 — Wire model loading into application lifespan

**File:** `app/main.py`

Add `RegistryModelLoader` to the startup sequence. If no production model exists
(first deploy before promotion), the API starts in degraded mode (503 on `/predict`)
rather than refusing to start — this prevents deployment failures from blocking
non-ML parts of the application.

```python
from contextlib import asynccontextmanager
from app.ml.inference.registry_model_loader import RegistryModelLoader
from app.ml.inference.ensemble_predictor import EnsemblePredictor

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────
    settings = get_settings()

    # ML inference — gracefully degraded if no production model is promoted yet
    app.state.ensemble           = None
    app.state.ensemble_predictor = None

    if settings.ML_MODEL_ENCRYPTION_KEY:
        try:
            async with async_session_factory() as session:
                loader = RegistryModelLoader(
                    session        = session,
                    storage_path   = settings.ML_MODEL_STORAGE_PATH,
                    encryption_key = settings.ML_MODEL_ENCRYPTION_KEY,
                    num_threads    = 4,
                )
                ensemble = await loader.load_production_ensemble()

            app.state.ensemble = ensemble
            app.state.ensemble_predictor = EnsemblePredictor.from_loaded_ensemble(
                ensemble             = ensemble,
                cache                = getattr(app.state, "cache", None),
                confidence_threshold = 0.60,
            )
            logger.info(
                "✓ Production ensemble active  XGB=%s  GRU=%s",
                ensemble.xgboost_version, ensemble.gru_version,
            )
        except RuntimeError as exc:
            # No production model yet — API starts in degraded mode
            logger.warning(
                "No production model available: %s  "
                "Inference endpoint will return 503 until a model is promoted.",
                exc,
            )
    else:
        logger.warning(
            "ML_MODEL_ENCRYPTION_KEY not set — inference disabled. "
            "Set this in .env to enable predictions."
        )

    yield   # ── Application runs here ──────────────────────────────────

    # ── Shutdown ──────────────────────────────────────────────────────────
    app.state.ensemble_predictor = None
    app.state.ensemble           = None
    logger.info("Inference engine shut down cleanly")
```

---

### T3.2 — Rewrite prediction endpoint

**File:** `app/api/v1/ml_predictions.py`

Replace random features + fallback response with real `FeatureLoader` + `EnsemblePredictor`.
Rate limits updated: slowapi 500/minute per IP (DoS guard) + DB per-user check (authoritative).

```python
"""
Cortex AI — ML Prediction Endpoints
======================================
Production inference using the registry-loaded ensemble.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_admin_role
from app.api.v1.ml_auth_utils import check_user_rate_limit
from app.core.limiter import limiter
from app.ml.inference.feature_loader import FeatureLoader
from app.ml.inference.registry_model_loader import RegistryModelLoader
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.schemas.ml_predictions import PredictionRequest, PredictionResponse
from app.core.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ML Predictions"])


@router.post(
    "/predict",
    response_model=PredictionResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate ML prediction for a symbol",
)
@limiter.limit("500/minute")   # coarse IP-level DoS guard; per-user limit is below
async def predict_single(
    request:            Request,
    prediction_request: PredictionRequest,
    db:                 AsyncSession = Depends(get_db),
    current_user:       dict         = Depends(get_current_user),
) -> PredictionResponse:
    """
    Generate a live ensemble prediction for a single NSE instrument.

    Returns direction (UP / DOWN / HOLD), confidence score, entry price,
    stop-loss, and three take-profit levels calibrated to 1.5×/2.5×/4× risk.

    503 — no production model loaded (promote a model first)
    422 — symbol not found or insufficient historical data
    429 — per-user rate limit exceeded
    """
    # ── Inference engine availability check ───────────────────────────
    predictor: EnsemblePredictor | None = getattr(
        request.app.state, "ensemble_predictor", None
    )
    if predictor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error":   "inference_unavailable",
                "message": (
                    "No production model is loaded. "
                    "Promote a model via: python scripts/promote_models.py --to production"
                ),
            },
        )

    # ── Per-user rate limit (DB-backed, authoritative) ────────────────
    user_id     = current_user["user_id"]
    tier        = current_user.get("tier", "standard")
    limit_map   = {"standard": 100, "premium": 1000, "internal": 999_999}
    user_limit  = limit_map.get(tier, 100)

    allowed, count = await check_user_rate_limit(
        db=db, user_id=user_id, limit=user_limit, time_window_minutes=60
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error":       "rate_limit_exceeded",
                "limit":       user_limit,
                "used":        count,
                "window":      "60 minutes",
                "tier":        tier,
            },
        )

    symbol    = prediction_request.symbol
    timeframe = prediction_request.timeframe or "1D"

    # ── Feature loading (Redis → DB → on-demand) ──────────────────────
    feature_loader = FeatureLoader(
        db              = db,
        redis           = getattr(request.app.state, "cache", None),
        sequence_length = 60,
        n_features      = 47,
    )
    try:
        tab_features, seq_features, current_price, volatility = (
            await feature_loader.load_features(symbol=symbol, timeframe=timeframe)
        )
    except Exception as exc:
        logger.error("Feature loading failed  symbol=%s  error=%s", symbol, exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "feature_loading_failed", "message": str(exc)},
        )

    # ── Inference ────────────────────────────────────────────────────
    result = await predictor.predict(
        features_tabular  = tab_features,
        features_sequence = seq_features,
        symbol            = symbol,
        current_price     = current_price,
        volatility        = volatility,
        timeframe         = timeframe,
    )
    return PredictionResponse(**result)


@router.get("/models", summary="List all registered models and their lifecycle status")
async def list_models(
    db:           AsyncSession = Depends(get_db),
    current_user: dict         = Depends(get_current_user),
):
    """Returns all models grouped by status (development/staging/production/archived)."""
    from app.models.ml_data import MLModelMetadata
    from sqlalchemy import select

    rows = (await db.execute(
        select(MLModelMetadata).order_by(MLModelMetadata.created_at.desc())
    )).scalars().all()

    return {
        "models": [
            {
                "id":            m.id,
                "model_id":      m.model_id,
                "model_name":    m.model_name,
                "model_version": m.model_version,
                "status":        m.status,
                "accuracy":      (m.training_metrics or {}).get("accuracy"),
                "f1_up":         (m.training_metrics or {}).get("f1_score", {}).get("up"),
                "f1_down":       (m.training_metrics or {}).get("f1_score", {}).get("down"),
                "is_active":     m.is_active,
                "trained_at":    m.trained_at.isoformat()  if m.trained_at  else None,
                "deployed_at":   m.deployed_at.isoformat() if m.deployed_at else None,
            }
            for m in rows
        ],
        "count": len(rows),
    }


@router.get("/health", summary="Model inference health check — no auth required")
async def model_health(request: Request):
    """Returns loaded model versions, weights, and inference availability."""
    ensemble  = getattr(request.app.state, "ensemble",           None)
    predictor = getattr(request.app.state, "ensemble_predictor", None)

    if ensemble is None or predictor is None:
        return {"status": "unavailable", "detail": "No production model loaded"}

    return {
        "status":               "healthy",
        "xgboost_version":      ensemble.xgboost_version,
        "gru_version":          ensemble.gru_version,
        "xgboost_weight":       ensemble.xgboost_weight,
        "gru_weight":           ensemble.gru_weight,
        "loaded_at":            ensemble.loaded_at.isoformat(),
        "confidence_threshold": predictor.confidence_threshold,
    }


@router.post(
    "/admin/reload",
    summary="Hot-reload production models without server restart (admin only)",
    include_in_schema=False,   # hidden from public OpenAPI docs
)
async def admin_reload(
    request:      Request,
    db:           AsyncSession = Depends(get_db),
    current_user: dict         = Depends(require_admin_role),
):
    """
    Atomically swap the loaded ensemble with the current production models.

    Called by ops after running: python scripts/promote_models.py --to production
    Allows zero-downtime model updates without restarting the API process.
    """
    settings = get_settings()
    loader   = RegistryModelLoader(
        session        = db,
        storage_path   = settings.ML_MODEL_STORAGE_PATH,
        encryption_key = settings.ML_MODEL_ENCRYPTION_KEY,
    )
    new_ensemble = await loader.load_production_ensemble()
    new_predictor = EnsemblePredictor.from_loaded_ensemble(
        ensemble             = new_ensemble,
        cache                = getattr(request.app.state, "cache", None),
        confidence_threshold = 0.60,
    )
    # Atomic reference swap — old sessions dereferenced, Python GC handles cleanup
    request.app.state.ensemble           = new_ensemble
    request.app.state.ensemble_predictor = new_predictor

    logger.info(
        "Hot-reload complete  XGB=%s  GRU=%s  user=%s",
        new_ensemble.xgboost_version, new_ensemble.gru_version,
        current_user["user_id"],
    )
    return {
        "status":          "reloaded",
        "xgboost_version": new_ensemble.xgboost_version,
        "gru_version":     new_ensemble.gru_version,
        "loaded_at":       new_ensemble.loaded_at.isoformat(),
    }
```

---

## Phase 4 — Drift Baseline & Monitoring
**Target: 4–6 hours**

---

### T4.1 — Populate training prediction statistics (drift baseline)

`DriftDetector` computes KS-test drift against `training_prediction_stats` on
`MLModelMetadata`. This field is currently empty. The eval arrays saved at
`models/production/checkpoints/step_6_gru/eval_X.npy` + `eval_y.npy` are the
validation set — run the trained ensemble on them and store the P(UP) distribution
as the baseline.

**File:** `scripts/store_drift_baseline.py` *(new)*

```python
"""
Compute and store the training prediction distribution as the drift baseline.
Run once after models are promoted to production.

Run:
    python scripts/store_drift_baseline.py
"""
import asyncio, json
import numpy as np
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.ml.inference.registry_model_loader import RegistryModelLoader
from app.models.ml_data import MLModelMetadata

EVAL_X_PATH = Path("models/production/checkpoints/step_6_gru/eval_X.npy")
EVAL_Y_PATH = Path("models/production/checkpoints/step_6_gru/eval_y.npy")

async def main() -> None:
    settings = get_settings()
    engine   = create_async_engine(str(settings.DATABASE_URL), echo=False)
    Session  = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        loader   = RegistryModelLoader(session, settings.ML_MODEL_STORAGE_PATH,
                                        settings.ML_MODEL_ENCRYPTION_KEY)
        ensemble = await loader.load_production_ensemble()

        X_val = np.load(EVAL_X_PATH)              # (30000, 60, 47)
        X_tab = X_val[:, -1, :]                   # last timestep for XGBoost (30000, 47)

        p_up = ensemble.predict_ensemble(X_tab, X_val)   # (30000,)

        baseline = {
            "mean":  float(p_up.mean()),
            "std":   float(p_up.std()),
            "min":   float(p_up.min()),
            "max":   float(p_up.max()),
            "p5":    float(np.percentile(p_up, 5)),
            "p95":   float(np.percentile(p_up, 95)),
            "n_samples": len(p_up),
        }
        print("Drift baseline:", json.dumps(baseline, indent=2))

        # Update both registry records
        for version in ["1.0.0_xgboost", "1.0.0_gru"]:
            stmt   = select(MLModelMetadata).where(MLModelMetadata.model_version == version)
            result = await session.execute(stmt)
            record = result.scalar_one_or_none()
            if record:
                record.training_prediction_stats = baseline
                print(f"Updated {version}")

        await session.commit()
        print("Done — drift baseline stored.")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
```

---

### T4.2 — Verify drift monitoring background task is active

In `app/worker.py` the `drift_detection_loop` is already wired. Confirm these
`.env` values are set before the worker starts:

```bash
DRIFT_CHECK_INTERVAL_SECONDS=3600   # 1 hour in production, 300 in dev
ML_DRIFT_THRESHOLD_SIGMA=2.0
SLACK_WEBHOOK_URL=https://hooks.slack.com/...   # optional
```

---

## Phase 5 — Testing & Hardening
**Target: 1 day**

---

### T5.1 — Integration test: end-to-end prediction pipeline

**File:** `tests/integration/test_production_inference.py` *(new)*

```python
import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_predict_returns_valid_signal(client: AsyncClient):
    resp = await client.post(
        "/api/v1/predict",
        json={"symbol": "NSE_EQ|INE002A01018", "timeframe": "1D"},
        headers={"Authorization": "Bearer <test_token>"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["direction"] in ("UP", "DOWN", "HOLD")
    assert 0.0 <= body["confidence"] <= 1.0
    assert body["entry_price"] > 0

@pytest.mark.asyncio
async def test_health_endpoint_shows_loaded_versions(client: AsyncClient):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert "xgboost_version" in body
    assert "gru_version"     in body

@pytest.mark.asyncio
async def test_503_when_no_model_loaded(client_no_model: AsyncClient):
    resp = await client_no_model.post(
        "/api/v1/predict",
        json={"symbol": "NSE_EQ|INE002A01018"},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "inference_unavailable"

@pytest.mark.asyncio
async def test_429_when_rate_limit_exceeded(client: AsyncClient):
    # Send 101 requests as a standard-tier user (limit=100/hour)
    for _ in range(101):
        resp = await client.post("/api/v1/predict",
                                  json={"symbol": "NSE_EQ|INE002A01018"})
    assert resp.status_code == 429
    assert resp.json()["detail"]["error"] == "rate_limit_exceeded"
```

---

### T5.2 — Latency benchmark

Target: **p99 < 250ms** (`ML_LATENCY_TARGET_MS=250`)

| Path | Expected latency |
|---|---|
| Redis cache hit | < 5ms |
| DB feature load + XGBoost ONNX + GRU ONNX | < 150ms |
| Full cold path (DB + both models + serialisation) | < 200ms |

Both models now go through ONNX Runtime:
- XGBoost ONNX: ~1–3ms
- GRU ONNX: ~10–30ms
- Feature load from DB: ~20–50ms

```bash
sudo apt-get install wrk
wrk -t4 -c20 -d30s http://localhost:8000/api/v1/predict
# p99 target: < 250ms
```

---

### T5.3 — Key rotation runbook

```bash
# 1. Generate new key
NEW_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 2. Run rotation script (maintenance window — put API in 503 mode first)
python scripts/rotate_encryption_key.py --old-key "$ML_MODEL_ENCRYPTION_KEY" --new-key "$NEW_KEY"

# 3. Update .env
sed -i "s/ML_MODEL_ENCRYPTION_KEY=.*/ML_MODEL_ENCRYPTION_KEY=$NEW_KEY/" .env

# 4. Restart API
```

`scripts/rotate_encryption_key.py` (to be implemented): load all registry records,
decrypt with old key, re-encrypt with new key, overwrite `.onnx.enc` files, update
checksums in DB.

---

## Execution Checklist

```
Phase 0 — Pre-requisites (in order, nothing else starts until done)
  [ ] T0.1  Set permanent ML_MODEL_ENCRYPTION_KEY in .env
  [ ] T0.2  Fix ModelRegistry field-name bugs (artifact_path, model_type)
  [ ] T0.3  python scripts/backfill_model_metrics.py
  [ ] T0.4  Fix orchestrator _register_models_in_registry to pass evaluation_results
  [ ] T0.5  python scripts/export_xgboost_onnx.py  → models/production/onnx/xgboost_model.onnx
  [ ] T0.6  Fix _export_models_to_onnx in orchestrator to use native XGB export

  Verify:
  [ ]   DB has 2 records with accuracy > 0.50
  [ ]   xgboost_model.onnx exists and ort.InferenceSession loads it cleanly
  [ ]   ML_MODEL_ENCRYPTION_KEY can decrypt both registry .onnx.enc artifacts

Phase 1 — Inference Engine
  [ ] T1.1  Create app/ml/inference/registry_model_loader.py (LoadedEnsemble + RegistryModelLoader)
  [ ] T1.2  Add EnsemblePredictor.from_loaded_ensemble() factory method
  [ ] T1.3  python scripts/test_inference.py --staging  (after T2.3)

Phase 2 — Promotion
  [ ] T2.1  Add ModelPromoter + QualityGate + QualityGateError to model_registry.py
  [ ] T2.2  Create scripts/promote_models.py
  [ ] T2.3  python scripts/promote_models.py --to staging
  [ ] T2.4  python scripts/test_inference.py --staging  (smoke test)
  [ ] T2.5  python scripts/promote_models.py --to production

Phase 3 — API Wiring
  [ ] T3.0  Add require_admin_role to app/api/deps.py
            Verify get_current_user_id in security.py returns is_admin in payload
  [ ] T3.1  Wire RegistryModelLoader into lifespan in app/main.py
  [ ] T3.2  Rewrite app/api/v1/ml_predictions.py (full replacement)
  [ ] T3.3  Add /admin/reload endpoint (in T3.2 already)

  Verify:
  [ ]   curl localhost:8000/api/v1/health  →  {"status":"healthy", ...}
  [ ]   POST /api/v1/predict with real symbol returns direction != null

Phase 4 — Monitoring
  [ ] T4.1  python scripts/store_drift_baseline.py
  [ ] T4.2  Confirm .env has DRIFT_CHECK_INTERVAL_SECONDS + ML_DRIFT_THRESHOLD_SIGMA
  [ ] T4.3  Manually trigger DriftDetector.detect_drift() in a Python shell; verify DB write

Phase 5 — Hardening
  [ ] T5.1  Run integration test suite: pytest tests/integration/test_production_inference.py
  [ ] T5.2  Latency benchmark: wrk → confirm p99 < 250ms
  [ ] T5.3  Document key rotation procedure; test rotation script in staging

  DONE: API serving live predictions from the production ensemble.
```

---

## Separate Issues (tracked, not blocking)

| Issue | Location | Impact |
|---|---|---|
| Sharpe ratio = 0.0 in evaluation | `app/ml/training/evaluator.py` | Metrics display only — models are correct |
| `is_admin` claim in JWT | `app/core/security.py` | Must be present for `/admin/reload` to work |
| `training_samples` not set on registry records | `scripts/backfill_model_metrics.py` | QualityGate `MIN_TRAINING_SAMPLES` check will fail if not backfilled |
One thing to verify before starting: the get_current_user_id function in app/core/security.py needs to return is_admin in its payload. If   it doesn't, T3.0 requires a small addition there too. Check it before wiring T3.3.