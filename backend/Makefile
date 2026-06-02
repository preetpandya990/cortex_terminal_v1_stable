# Cortex AI Backend — Dependency Management
# ==========================================
# All commands assume you are in the backend/ directory with the venv active:
#   source .venv/bin/activate
#
# Targets:
#   make lock              — regenerate all pinned lockfiles (~6 min, no network)
#   make lock-with-hashes  — regenerate lockfiles WITH SHA-256 hashes (~30 min,
#                            requires full network; run in CI after dependency bump)
#   make install           — install production deps from lockfile
#   make install-dev       — install production + dev deps from lockfiles
#   make audit             — CVE scan via OSV database (exits non-zero on findings)
#   make audit-json        — CVE scan with JSON output for CI artifact upload
#   make sync              — sync venv to exactly the locked state (pip-sync)
#   make check-pins        — verify critical ML stack pins have not drifted
#
# Upgrade workflow:
#   1. Edit the relevant .in file
#   2. Run: make lock  (or make lock-with-hashes for full hash coverage in CI)
#   3. Review the lockfile diff — look for unexpected transitive-dep upgrades
#   4. For ML stack changes (numpy/pandas/torch/tensorflow/sklearn/xgboost/onnx):
#      run a backtest diff and confirm TF GPU before committing
#      (see documentation/backend/dependency-management.md)
#   5. Run: make audit
#   6. Commit both .in and .lock files together

PYTHON       := python3.11
PIP_COMPILE  := pip-compile
PIP_AUDIT    := pip-audit

# ── Base compile flags ────────────────────────────────────────────────────────
#   --no-upgrade    never silently upgrade; keep installed versions
#   --allow-unsafe  pin setuptools/pip/wheel (runtime deps on this stack)
#   --annotate      add "via" source-package comments (audit trail in lockfile)
#   --strip-extras  normalise extras → install-portable lockfile
BASE_FLAGS := \
  --no-upgrade \
  --allow-unsafe \
  --annotate \
  --strip-extras

# ── Hash flags (CI / explicit request only) ───────────────────────────────────
#   --generate-hashes  SHA-256 for every wheel/sdist — supply-chain defence.
#   Requires downloading all wheel files (~2–3 GB for the full ML stack).
#   Use: make lock-with-hashes
HASH_FLAGS := --generate-hashes

.PHONY: lock lock-prod lock-training lock-dev \
        lock-with-hashes lock-prod-hashed lock-training-hashed lock-dev-hashed \
        install install-dev install-training \
        audit audit-json sync check-pins help

# ── Pin-only lock targets (fast, ~6 min total) ────────────────────────────────
# Pins ALL transitive dependencies (~250 packages) with == specifiers.
# Protects against: version drift, unexpected transitive upgrades.
# For hash-based supply-chain protection, use: make lock-with-hashes

lock: lock-prod lock-dev lock-training  ## Regenerate all lockfiles (pin-only, ~6 min)

lock-prod:  ## Regenerate requirements.lock
	$(PIP_COMPILE) $(BASE_FLAGS) \
	  --output-file requirements.lock \
	  requirements.in
	@echo "✓  requirements.lock updated ($(shell wc -l < requirements.lock) lines)"

lock-training:  ## Regenerate requirements-ml-training.lock
	$(PIP_COMPILE) $(BASE_FLAGS) \
	  --output-file requirements-ml-training.lock \
	  requirements-ml-training.in
	@echo "✓  requirements-ml-training.lock updated"

lock-dev:  ## Regenerate requirements-dev.lock
	$(PIP_COMPILE) $(BASE_FLAGS) \
	  --output-file requirements-dev.lock \
	  requirements-dev.in
	@echo "✓  requirements-dev.lock updated"

# ── Hashed lock targets (CI / security hardening, ~30 min) ───────────────────
# Adds SHA-256 hashes to every pinned package.
# Protects against: all of the above PLUS supply-chain tampering (PyPI compromise).
# Install with: pip install --require-hashes -r requirements.lock
# Note: torch==2.11.0+cpu is sourced from download.pytorch.org/whl/cpu; its hash
# is resolved from the PyTorch CDN index page (not PyPI).

lock-with-hashes: lock-prod-hashed lock-dev-hashed lock-training-hashed  ## Regenerate all lockfiles WITH SHA-256 hashes (~30 min)

lock-prod-hashed:  ## Regenerate requirements.lock with hashes
	$(PIP_COMPILE) $(BASE_FLAGS) $(HASH_FLAGS) \
	  --output-file requirements.lock \
	  requirements.in
	@echo "✓  requirements.lock updated with hashes"

lock-training-hashed:  ## Regenerate requirements-ml-training.lock with hashes
	$(PIP_COMPILE) $(BASE_FLAGS) $(HASH_FLAGS) \
	  --output-file requirements-ml-training.lock \
	  requirements-ml-training.in
	@echo "✓  requirements-ml-training.lock updated with hashes"

lock-dev-hashed:  ## Regenerate requirements-dev.lock with hashes
	$(PIP_COMPILE) $(BASE_FLAGS) $(HASH_FLAGS) \
	  --output-file requirements-dev.lock \
	  requirements-dev.in
	@echo "✓  requirements-dev.lock updated with hashes"

# ── Install targets ───────────────────────────────────────────────────────────

install:  ## Install production deps from requirements.lock
	pip install -r requirements.lock
	@echo "✓  Production dependencies installed"

install-training: install  ## Install production + ML training deps
	pip install -r requirements-ml-training.lock
	@echo "✓  ML training dependencies installed"

install-dev: install  ## Install production + dev deps
	pip install -r requirements-dev.lock
	@echo "✓  Dev dependencies installed"

install-locked:  ## Install with hash verification (requires hashed lockfile)
	pip install --require-hashes -r requirements.lock
	@echo "✓  Production dependencies installed (hash-verified)"

sync:  ## Sync venv to exactly the locked state (removes unlocked extras)
	pip-sync requirements.lock requirements-dev.lock

# ── Security targets ──────────────────────────────────────────────────────────

audit:  ## Scan installed packages for known CVEs (OSV database)
	$(PIP_AUDIT) \
	  --vulnerability-service osv \
	  --fix-source none \
	  --strict
	@echo "✓  No known CVEs found"

audit-req:  ## Scan requirements.lock for CVEs without installing
	$(PIP_AUDIT) \
	  -r requirements.lock \
	  --vulnerability-service osv \
	  --fix-source none \
	  --no-deps \
	  --strict
	@echo "✓  No known CVEs in requirements.lock"

audit-json:  ## CVE scan with JSON output (for CI artifact upload)
	$(PIP_AUDIT) \
	  --vulnerability-service osv \
	  --fix-source none \
	  --format json \
	  -o audit-results.json
	@echo "✓  Audit results written to audit-results.json"

# ── Validation targets ────────────────────────────────────────────────────────

check-pins:  ## Verify critical ML stack pins have not drifted in requirements.lock
	@echo "Checking critical ML stack pins in requirements.lock ..."
	@grep -E "^numpy==" requirements.lock | grep -q "1\.26\.4" \
	  || (echo "ERROR: numpy pin has drifted from 1.26.4 — see dependency-management.md" && exit 1)
	@grep -E "^scikit-learn==" requirements.lock | grep -q "1\.4\.0" \
	  || (echo "ERROR: scikit-learn pin has drifted from 1.4.0 — see dependency-management.md" && exit 1)
	@grep -E "^tensorflow==" requirements.lock | grep -q "2\.21\.0" \
	  || (echo "ERROR: tensorflow pin has drifted from 2.21.0 — see dependency-management.md" && exit 1)
	@grep -E "^pandas==" requirements.lock | grep -q "3\.0\.2" \
	  || (echo "ERROR: pandas pin has drifted from 3.0.2 — see dependency-management.md" && exit 1)
	@grep -E "^torch==" requirements.lock | grep -q "2\.11\.0" \
	  || (echo "ERROR: torch pin has drifted from 2.11.0 — see dependency-management.md" && exit 1)
	@echo "✓  All critical ML stack pins are intact"

help:  ## Show available targets
	@echo ""
	@echo "  Cortex AI — Dependency Management"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'
	@echo ""
