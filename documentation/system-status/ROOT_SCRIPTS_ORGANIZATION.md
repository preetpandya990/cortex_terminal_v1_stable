# Scripts Organization Summary

**Date**: 2026-06-26  
**Action**: Consolidated former `root_scripts/` into the main `scripts/` directory

---

## Current Layout

```text
scripts/
├── deployment/
├── dev/
├── ops/
├── testing/
└── utilities/
```

## Moved Script Groups

- Development scripts: `scripts/dev/`
  - `start-dev.sh`
  - `start-dev-simple.sh`
  - `stop-dev.sh`
- Operations scripts: `scripts/ops/`
  - `health-check.sh`
  - `quick-ref.sh`
- Root testing shell scripts: `scripts/testing/`
  - `test-auth.sh`
  - `test-upstox.sh`

## Documentation Placement

- Script usage guide: [documentation/configuration/ROOT_SCRIPTS_README.md](../configuration/ROOT_SCRIPTS_README.md)
- The old `root_scripts/README.md` was moved into `documentation/`

## Result

- Removed the loose `root_scripts/` directory from project root
- Consolidated operational scripts under the existing `scripts/` tree
- Moved the embedded script README into the documentation set

## Common Paths

```bash
./scripts/dev/start-dev.sh
./scripts/ops/health-check.sh
./scripts/testing/test-auth.sh
```

---

**Status**: Complete
