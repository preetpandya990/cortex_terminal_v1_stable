# Scripts Guide

Quick access scripts for Cortex AI development and operations.

---

## Directory Structure

```text
scripts/
├── deployment/             # Existing deployment scripts
├── dev/                    # Development scripts
│   ├── start-dev.sh
│   ├── start-dev-simple.sh
│   └── stop-dev.sh
├── ops/                    # Operations scripts
│   ├── health-check.sh
│   └── quick-ref.sh
├── testing/                # Test and validation scripts
│   ├── ml_visual_demo.py
│   ├── test-auth.sh
│   ├── test-upstox.sh
│   └── test_ml_e2e.py
└── utilities/              # Utility scripts
```

---

## Common Commands

### Development

```bash
./scripts/dev/start-dev.sh
./scripts/dev/start-dev-simple.sh
./scripts/dev/stop-dev.sh
```

### Operations

```bash
./scripts/ops/health-check.sh
./scripts/ops/quick-ref.sh
```

### Testing

```bash
./scripts/testing/test-auth.sh
./scripts/testing/test-upstox.sh
```

---

## Optional Aliases

```bash
alias cortex-start='./scripts/dev/start-dev.sh'
alias cortex-stop='./scripts/dev/stop-dev.sh'
alias cortex-health='./scripts/ops/health-check.sh'
alias cortex-ref='./scripts/ops/quick-ref.sh'
alias cortex-test-auth='./scripts/testing/test-auth.sh'
alias cortex-test-upstox='./scripts/testing/test-upstox.sh'
```

---

## Notes

- The former `root_scripts/` directory has been merged into `scripts/`.
- Script documentation now lives in `documentation/`.
- Existing script behavior was not changed by the move.

---

**Last Updated**: 2026-06-26
