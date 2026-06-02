# Dependency Management

## Overview

Python dependencies are managed with a two-layer model:

| File | Purpose | Edited by |
|---|---|---|
| `requirements.in` | Abstract spec — direct deps with rationale | Human |
| `requirements.lock` | Concrete lockfile — all transitive deps + SHA-256 hashes | `make lock` |
| `requirements-ml-training.in` | ML training extras (abstract) | Human |
| `requirements-ml-training.lock` | ML training extras (locked + hashed) | `make lock` |
| `requirements-dev.in` | Dev/test deps (abstract) | Human |
| `requirements-dev.lock` | Dev/test deps (locked + hashed) | `make lock` |

**Never edit `.lock` files directly.** They are machine-generated and must be regenerated via `make lock` whenever a `.in` file changes.

---

## Daily workflow

### Install from lockfiles (fresh clone or CI)

```bash
# Production only
pip install --require-hashes -r requirements.lock

# Production + dev
pip install --require-hashes -r requirements.lock \
            --require-hashes -r requirements-dev.lock

# Production + ML training
pip install --require-hashes -r requirements.lock \
            --require-hashes -r requirements-ml-training.lock
```

### Add or update a dependency

1. Edit the relevant `.in` file.
2. Regenerate the lockfile:
   ```bash
   make lock-prod      # or lock-dev, lock-training
   ```
3. Review the lockfile diff carefully — look for unexpected version bumps in transitive deps.
4. Run the audit: `make audit`
5. Commit **both** the `.in` and the `.lock` file together.

### Regenerate all lockfiles

```bash
make lock
```

---

## ML stack upgrade policy

The following packages are **hard-pinned** and must never be upgraded without a full validation cycle:

| Package | Pin | Constraint |
|---|---|---|
| `numpy` | `1.26.4` | numpy ≥ 2.0 breaks TensorFlow 2.21 GPU (ml-dtypes ABI incompatibility). Upgrade path: numpy → 2.x requires TF → 3.x and full ML stack re-validation. |
| `scikit-learn` | `1.4.0` | scikit-learn ≥ 1.5 requires numpy ≥ 2.0. |
| `tensorflow[and-cuda]` | `2.21.0` | Pinned for CUDA 12.x compatibility on the dev box (RTX 3050, WSL2 driver ceiling 12.2). |
| `pandas` | `3.0.2` | Bleeding-edge (copy-on-write default). Monitor for ecosystem compatibility regressions. |
| `torch` | `2.11.0+cpu` | CPU-only wheel from `download.pytorch.org/whl/cpu`. GPU budget allocated to TF. The `+cpu` local version identifier is the CPU build. |
| `onnxruntime-gpu` | `1.19.2` | Paired with onnx 1.21.0. Do not install onnxruntime (CPU) alongside this. |

**Validation checklist for any ML stack upgrade:**

- [ ] `python -c "import tensorflow as tf; print(tf.config.list_physical_devices('GPU'))"` returns `[PhysicalDevice(name='/physical_device:GPU:0', device_type='GPU')]`
- [ ] Training orchestrator smoke run completes without numpy ABI errors
- [ ] Full ML test suite green: `pytest tests/unit/test_ml_*.py tests/ml/`
- [ ] XGBoost inference latency p99 < 5 ms (see `TestInferenceLatencyBudget` in `tests/ml/test_regression_e1.py`)
- [ ] Run a backtest diff against v1.1.x metrics to confirm no silent behavioural change in the feature pipeline

---

## PyTorch — special install note

`torch==2.11.0+cpu` is sourced from the PyTorch wheel index, not PyPI:

```bash
pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cpu
```

Because pip-compile resolves this package from a non-PyPI index, its hash in `requirements.lock` is obtained from the PyTorch CDN index page. If `make lock` cannot resolve the hash (e.g., offline environment), use the fallback:

```bash
# Download the wheel manually, then hash it:
pip download torch==2.11.0 \
  --index-url https://download.pytorch.org/whl/cpu \
  --no-deps -d /tmp/torch-dl/
sha256sum /tmp/torch-dl/torch-*.whl
# Add the result to requirements.lock manually as:
# torch==2.11.0+cpu \
#     --hash=sha256:<computed-hash>
```

---

## Security scanning

Run the CVE audit against the production lockfile:

```bash
make audit
```

This uses [pip-audit](https://pypi.org/project/pip-audit/) against the [OSV vulnerability database](https://osv.dev/). The audit exits non-zero on any unpatched finding so it can be used as a CI gate.

For machine-readable output (CI artifact upload):

```bash
make audit-json   # writes audit-results.json
```

---

## Why hashed lockfiles?

`requirements.txt` with `==` pins protects against **version drift** (a newer broken release being pulled in). Hashed lockfiles additionally protect against **supply chain attacks**: if a package on PyPI is replaced with a tampered version at the same version number, `pip install --require-hashes` will reject it because the SHA-256 hash won't match the recorded value.

The combination means:
- **Version drift**: prevented by `==` pins in `.lock`
- **Transitive dep drift**: prevented by full resolution in `.lock` (all ~250 transitive deps are pinned, not just the ~50 direct ones)
- **Supply chain tampering**: prevented by `--require-hashes` at install time
- **Known CVEs**: caught by `make audit` (OSV database)
