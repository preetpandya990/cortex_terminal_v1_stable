"""
R7 — Hashed dependency lockfile + upgrade policy enforcement.

Validates:
  R7-1  All three .in files exist and declare the expected critical pins.
  R7-2  pip-audit is in requirements-dev.txt / requirements-dev.in.
  R7-3  Makefile exists with the mandatory targets: lock, audit, check-pins,
        lock-with-hashes, install-locked (hash-verified install target).
  R7-4  All three .lock files exist and pin more packages than the corresponding
        .in files (transitive deps are resolved and pinned, not just direct deps).
  R7-5  Critical ML stack pins are present in requirements.lock and have not
        drifted from the required versions.
  R7-6  requirements-ml-training.in does NOT re-pin the core ML stack
        (the "single source of truth" rule — re-pinning here caused the
        2026-05-18 GPU training breakage).
  R7-7  All packages in requirements.lock use == (exact pin, no range specifiers).
  R7-8  pip-tools is pinned at >=7.5.3 (compatible with pip 26.x; earlier
        versions crash with AttributeError on PackageFinder).
  R7-9  When lockfiles contain hashes (make lock-with-hashes), every pinned
        package has at least one --hash=sha256: entry (guards against partial
        generation that would be silently ignored).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_BACKEND = Path(__file__).parent.parent.parent

# ── Paths ─────────────────────────────────────────────────────────────────────
_REQ_IN   = _BACKEND / "requirements.in"
_REQ_LOCK = _BACKEND / "requirements.lock"
_ML_IN    = _BACKEND / "requirements-ml-training.in"
_ML_LOCK  = _BACKEND / "requirements-ml-training.lock"
_DEV_IN   = _BACKEND / "requirements-dev.in"
_DEV_LOCK = _BACKEND / "requirements-dev.lock"
_DEV_TXT  = _BACKEND / "requirements-dev.txt"
_MAKEFILE = _BACKEND / "Makefile"

# ── Critical ML pins — must never silently drift ──────────────────────────────
_CRITICAL_PINS = {
    "numpy":        "1.26.4",
    "scikit-learn": "1.4.0",
    "tensorflow":   "2.21.0",
    "pandas":       "3.0.2",
    "torch":        "2.11.0",  # accepts 2.11.0+cpu local version suffix
}


# ══════════════════════════════════════════════════════════════════════════════
# R7-1  .in files exist + declare critical pins
# ══════════════════════════════════════════════════════════════════════════════

class TestInFilesExist:

    def test_requirements_in_exists(self) -> None:
        assert _REQ_IN.exists(), "requirements.in must exist (source of truth)"

    def test_ml_training_in_exists(self) -> None:
        assert _ML_IN.exists(), "requirements-ml-training.in must exist"

    def test_dev_in_exists(self) -> None:
        assert _DEV_IN.exists(), "requirements-dev.in must exist"

    def test_requirements_in_pins_numpy(self) -> None:
        assert "numpy==1.26.4" in _REQ_IN.read_text()

    def test_requirements_in_pins_scikit_learn(self) -> None:
        assert "scikit-learn==1.4.0" in _REQ_IN.read_text()

    def test_requirements_in_pins_pandas(self) -> None:
        assert "pandas==3.0.2" in _REQ_IN.read_text()

    def test_requirements_in_pins_torch(self) -> None:
        assert "torch==2.11.0" in _REQ_IN.read_text()

    def test_requirements_in_has_pytorch_extra_index(self) -> None:
        assert "download.pytorch.org/whl/cpu" in _REQ_IN.read_text(), (
            "requirements.in must declare the PyTorch CPU whl index for torch"
        )


# ══════════════════════════════════════════════════════════════════════════════
# R7-2  pip-audit is declared in dev deps
# ══════════════════════════════════════════════════════════════════════════════

class TestPipAuditPresent:

    def test_pip_audit_in_dev_in(self) -> None:
        assert "pip-audit" in _DEV_IN.read_text(), (
            "requirements-dev.in must declare pip-audit"
        )

    def test_pip_audit_in_dev_txt(self) -> None:
        assert "pip-audit" in _DEV_TXT.read_text(), (
            "requirements-dev.txt must declare pip-audit"
        )

    def test_pip_audit_is_pinned_in_dev_in(self) -> None:
        assert re.search(r"pip-audit==\d+\.\d+\.\d+", _DEV_IN.read_text()), (
            "pip-audit in requirements-dev.in must be pinned with =="
        )


# ══════════════════════════════════════════════════════════════════════════════
# R7-3  Makefile exists with mandatory targets
# ══════════════════════════════════════════════════════════════════════════════

class TestMakefileTargets:
    _REQUIRED_TARGETS = [
        "lock", "lock-prod", "lock-dev", "lock-training",
        "lock-with-hashes",   # full hash generation for CI
        "install", "install-locked",  # install-locked uses --require-hashes
        "audit", "check-pins",
    ]

    def test_makefile_exists(self) -> None:
        assert _MAKEFILE.exists(), "backend/Makefile must exist"

    @pytest.mark.parametrize("target", _REQUIRED_TARGETS)
    def test_makefile_has_target(self, target: str) -> None:
        text = _MAKEFILE.read_text()
        assert re.search(rf"^{re.escape(target)}[:\s]", text, re.MULTILINE), (
            f"Makefile must define the '{target}' target"
        )

    def test_install_locked_uses_require_hashes(self) -> None:
        text = _MAKEFILE.read_text()
        # The install-locked target must pass --require-hashes to pip
        lines = text.splitlines()
        in_target = False
        for line in lines:
            if re.match(r"^install-locked[:\s]", line):
                in_target = True
            elif in_target:
                if "--require-hashes" in line:
                    return
                if re.match(r"^[a-zA-Z]", line) and not line.startswith("\t"):
                    break
        pytest.fail("install-locked target must use 'pip install --require-hashes'")

    def test_check_pins_validates_numpy(self) -> None:
        text = _MAKEFILE.read_text()
        assert "numpy" in text and "1.26.4" in text, (
            "check-pins target must validate the numpy==1.26.4 pin"
        )

    def test_makefile_has_lock_with_hashes_target(self) -> None:
        text = _MAKEFILE.read_text()
        assert "--generate-hashes" in text, (
            "Makefile must have a target using --generate-hashes for CI hash generation"
        )


# ══════════════════════════════════════════════════════════════════════════════
# R7-4  Lockfiles exist and cover transitive deps
# ══════════════════════════════════════════════════════════════════════════════

def _count_pinned_packages(path: Path) -> int:
    return sum(
        1 for line in path.read_text().splitlines()
        if re.match(r"^[a-zA-Z0-9_.-]+==[^\s]", line.strip())
    )

def _count_in_deps(path: Path) -> int:
    return sum(
        1 for line in path.read_text().splitlines()
        if line.strip()
           and not line.startswith("#")
           and not line.startswith("-")
           and not line.startswith("--")
    )


class TestLockfilesExist:

    def test_requirements_lock_exists(self) -> None:
        assert _REQ_LOCK.exists(), (
            "requirements.lock does not exist — run: make lock-prod"
        )

    def test_ml_training_lock_exists(self) -> None:
        assert _ML_LOCK.exists(), (
            "requirements-ml-training.lock does not exist — run: make lock-training"
        )

    def test_dev_lock_exists(self) -> None:
        assert _DEV_LOCK.exists(), (
            "requirements-dev.lock does not exist — run: make lock-dev"
        )

    def test_requirements_lock_pins_transitive_deps(self) -> None:
        n_lock = _count_pinned_packages(_REQ_LOCK)
        n_in   = _count_in_deps(_REQ_IN)
        assert n_lock > n_in, (
            f"requirements.lock pins {n_lock} packages but requirements.in only has "
            f"{n_in} direct deps — the lockfile must resolve transitive deps too"
        )

    def test_dev_lock_pins_transitive_deps(self) -> None:
        n_lock = _count_pinned_packages(_DEV_LOCK)
        n_in   = _count_in_deps(_DEV_IN)
        assert n_lock > n_in, (
            f"requirements-dev.lock pins {n_lock} packages vs {n_in} direct deps"
        )

    def test_requirements_lock_is_substantial(self) -> None:
        """A fully resolved lockfile for this stack must have 150+ pinned packages."""
        n = _count_pinned_packages(_REQ_LOCK)
        assert n >= 150, (
            f"requirements.lock only has {n} pinned packages — "
            "expected 150+ including transitive deps for this ML stack"
        )


# ══════════════════════════════════════════════════════════════════════════════
# R7-5  Critical ML pin guard — lockfile must not drift
# ══════════════════════════════════════════════════════════════════════════════

class TestCriticalPinsInLockfile:

    @pytest.mark.parametrize("package,expected_version", list(_CRITICAL_PINS.items()))
    def test_critical_pin_not_drifted(self, package: str, expected_version: str) -> None:
        text = _REQ_LOCK.read_text()
        # Accept local version suffix (e.g. torch==2.11.0+cpu)
        pattern = rf"^{re.escape(package)}=={re.escape(expected_version)}"
        assert re.search(pattern, text, re.MULTILINE | re.IGNORECASE), (
            f"requirements.lock must pin {package}=={expected_version} "
            f"(or {expected_version}+<local>) — see documentation/backend/dependency-management.md"
        )

    def test_lockfile_generated_by_pip_compile(self) -> None:
        """Lockfile header must show it was generated by pip-compile (not hand-edited)."""
        text = _REQ_LOCK.read_text()
        assert "pip-compile" in text.lower(), (
            "requirements.lock must be generated by pip-compile "
            "(run: make lock-prod)"
        )


# ══════════════════════════════════════════════════════════════════════════════
# R7-6  Single-source-of-truth: ml-training.in must not re-pin core stack
# ══════════════════════════════════════════════════════════════════════════════

class TestSingleSourceOfTruth:
    """requirements-ml-training.in must not re-pin the core ML stack.

    Re-pinning here caused the 2026-05-18 GPU training breakage: a
    `pip install -r requirements-ml-training.txt` silently downgraded
    TF 2.21 → 2.15 and force-installed Keras 2, disabling GPU inference.
    """

    _FORBIDDEN_PINS = [
        "tensorflow", "torch", "keras", "onnx", "onnxruntime",
        "xgboost", "optuna", "scikit-learn", "transformers", "optimum",
        "numpy", "pandas",
    ]

    @pytest.mark.parametrize("pkg", _FORBIDDEN_PINS)
    def test_ml_training_in_does_not_repin(self, pkg: str) -> None:
        text = _ML_IN.read_text()
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.match(rf"^{re.escape(pkg)}[=\[<>!@]", stripped, re.IGNORECASE):
                pytest.fail(
                    f"requirements-ml-training.in re-pins '{pkg}' — this violates "
                    "the single-source-of-truth rule and can cause silent stack downgrades. "
                    "Remove it; the pin belongs only in requirements.in."
                )


# ══════════════════════════════════════════════════════════════════════════════
# R7-7  Lockfiles use == for all package pins (no range specifiers)
# ══════════════════════════════════════════════════════════════════════════════

class TestLockfileSpecifiers:

    def _check_exact_pins(self, path: Path) -> None:
        violations: list[str] = []
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                continue
            # Catch range specifiers (~=, >=, <=, !=, <, >) at the start of a dep line
            if re.match(r"^[a-zA-Z0-9_.-]+[~<>!]", stripped):
                violations.append(f"  line {lineno}: {stripped}")
        assert not violations, (
            f"{path.name} contains non-== specifiers (range specifiers must not appear "
            "in lockfiles — all packages must be exactly pinned):\n"
            + "\n".join(violations)
        )

    def test_requirements_lock_uses_exact_pins(self) -> None:
        self._check_exact_pins(_REQ_LOCK)

    def test_dev_lock_uses_exact_pins(self) -> None:
        self._check_exact_pins(_DEV_LOCK)

    def test_ml_lock_uses_exact_pins(self) -> None:
        self._check_exact_pins(_ML_LOCK)


# ══════════════════════════════════════════════════════════════════════════════
# R7-8  pip-tools >=7.5.3 pinned (pip 26.x compatibility)
# ══════════════════════════════════════════════════════════════════════════════

class TestPipToolsVersion:

    def test_pip_tools_version_in_dev_in(self) -> None:
        text = _DEV_IN.read_text()
        m = re.search(r"pip-tools==(\d+\.\d+\.\d+)", text)
        assert m, "pip-tools must be pinned in requirements-dev.in"
        major, minor, patch = map(int, m.group(1).split("."))
        assert (major, minor, patch) >= (7, 5, 3), (
            f"pip-tools=={m.group(1)} is too old — pip 26.x requires >=7.5.3 "
            "(earlier versions crash with AttributeError on PackageFinder)"
        )

    def test_pip_tools_version_in_dev_txt(self) -> None:
        text = _DEV_TXT.read_text()
        m = re.search(r"pip-tools==(\d+\.\d+\.\d+)", text)
        assert m, "pip-tools must be pinned in requirements-dev.txt"
        major, minor, patch = map(int, m.group(1).split("."))
        assert (major, minor, patch) >= (7, 5, 3), (
            f"pip-tools=={m.group(1)} in requirements-dev.txt is too old for pip 26.x"
        )


# ══════════════════════════════════════════════════════════════════════════════
# R7-9  When lockfiles have hashes, every package must have one
# ══════════════════════════════════════════════════════════════════════════════

class TestHashedLockfileIntegrity:
    """Guards against partial hash generation (some packages hashed, some not).

    pip install --require-hashes will fail if any package lacks a hash, but the
    failure can be confusing.  This test catches it at generation time.

    The test is skipped when the lockfile was generated without --generate-hashes
    (the fast `make lock` path), because in that case NO packages have hashes and
    the lockfile is used with `pip install -r` (not --require-hashes).
    """

    def _lockfile_has_any_hashes(self, path: Path) -> bool:
        return "--hash=sha256:" in path.read_text()

    def _packages_missing_hashes(self, path: Path) -> list[str]:
        """Return package names that appear in the lockfile without a following hash."""
        text   = path.read_text()
        lines  = text.splitlines()
        missing: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i].rstrip("\\").strip()
            m = re.match(r"^([a-zA-Z0-9_.-]+)==[^\s]", line)
            if m:
                pkg = m.group(1)
                has_hash = False
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if "--hash=sha256:" in next_line:
                        has_hash = True
                        break
                    if re.match(r"^[a-zA-Z0-9_.-]+==", next_line) or (
                        not next_line and not lines[j - 1].rstrip().endswith("\\")
                    ):
                        break
                    j += 1
                if not has_hash:
                    missing.append(pkg)
            i += 1
        return missing

    def test_requirements_lock_no_partial_hashes(self) -> None:
        if not self._lockfile_has_any_hashes(_REQ_LOCK):
            pytest.skip("requirements.lock has no hashes (pin-only mode); skipping hash completeness check")
        missing = self._packages_missing_hashes(_REQ_LOCK)
        assert not missing, (
            "requirements.lock has hashes for some packages but not others — "
            "re-run 'make lock-prod-hashed' to regenerate consistently:\n"
            + "\n".join(f"  {p}" for p in missing)
        )

    def test_dev_lock_no_partial_hashes(self) -> None:
        if not self._lockfile_has_any_hashes(_DEV_LOCK):
            pytest.skip("requirements-dev.lock has no hashes; skipping hash completeness check")
        missing = self._packages_missing_hashes(_DEV_LOCK)
        assert not missing, (
            "requirements-dev.lock has partial hashes — re-run 'make lock-dev-hashed'"
        )
