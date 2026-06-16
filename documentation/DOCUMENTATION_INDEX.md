# Cortex AI - Documentation Index

Current navigation index for the `documentation/` tree.

---

## Snapshot

- Total markdown documents: 275
- Top-level directories: 17
- Root-level markdown docs: 4
- Status: mixed set of source-of-truth guides plus historical implementation artifacts

---

## Getting Started

| Document | Description | Location |
|----------|-------------|----------|
| Startup Instructions | Lightweight local startup flow | [documentation/guides/STARTUP_INSTRUCTIONS.md](documentation/guides/STARTUP_INSTRUCTIONS.md) |
| Root README | Full migration and fresh-install guide | [documentation/root/README.md](documentation/root/README.md) |
| Architecture Summary | High-level system overview | [documentation/architecture/ARCHITECTURE_SUMMARY.md](documentation/architecture/ARCHITECTURE_SUMMARY.md) |
| Known Issues | Current remediation backlog | [documentation/troubleshooting/KNOWN_ISSUES_AND_REMEDIATION.md](documentation/troubleshooting/KNOWN_ISSUES_AND_REMEDIATION.md) |

---

## Top-Level Inventory

| Section | Markdown Files | Purpose | Suggested Entry Point |
|--------|----------------|---------|------------------------|
| `architecture/` | 5 | System architecture and major design decisions | [ARCHITECTURE_SUMMARY.md](documentation/architecture/ARCHITECTURE_SUMMARY.md) |
| `api/` | 3 | REST and WebSocket API reference | [API_ENDPOINTS_DOCUMENTATION.md](documentation/api/API_ENDPOINTS_DOCUMENTATION.md) |
| `configuration/` | 2 | Credentials and dev script setup | [UPSTOX_CREDENTIALS_GUIDE.md](documentation/configuration/UPSTOX_CREDENTIALS_GUIDE.md) |
| `guides/` | 6 | Startup, testing, FAQ, handoff | [STARTUP_INSTRUCTIONS.md](documentation/guides/STARTUP_INSTRUCTIONS.md) |
| `implementation/` | 25 | Feature implementation reports and technical notes | [MARKET_FEED_IMPLEMENTATION.md](documentation/implementation/MARKET_FEED_IMPLEMENTATION.md) |
| `phases/` | 15 | Phase progress and completion records | [PHASE_10_COMPLETE_SUMMARY.md](documentation/phases/PHASE_10_COMPLETE_SUMMARY.md) |
| `plans/` | 6 | Forward plans and design proposals | [MARKET_FEED_PLAN.md](documentation/plans/MARKET_FEED_PLAN.md) |
| `root/` | 5 | Root-level operational guides, plans, and task lists | [README.md](documentation/root/README.md) |
| `root-artifacts/` | 61 | Historical analyses, fixes, reports, research, and plans | [research/ML_DL_KNOWLEDGE_COMPACT.md](documentation/root-artifacts/research/ML_DL_KNOWLEDGE_COMPACT.md) |
| `system-status/` | 7 | Status snapshots, audits, and cleanup notes | [ANALYSIS_AND_SOLUTION.md](documentation/system-status/ANALYSIS_AND_SOLUTION.md) |
| `tasks/` | 34 | Task tracking, validations, and hawk-eye workstreams | [TASK_44_FINAL_SYSTEM_VALIDATION.md](documentation/tasks/TASK_44_FINAL_SYSTEM_VALIDATION.md) |
| `testing/` | 5 | Test summaries and verification guides | [ML_SYSTEM_VERIFICATION_REPORT.md](documentation/testing/ML_SYSTEM_VERIFICATION_REPORT.md) |
| `troubleshooting/` | 7 | Issue summaries and remediation guides | [KNOWN_ISSUES_AND_REMEDIATION.md](documentation/troubleshooting/KNOWN_ISSUES_AND_REMEDIATION.md) |
| `backend/` | 86 | Backend-specific docs, guides, references, testing, and ML notes | [docs/architecture/ML_SYSTEM_ARCHITECTURE.md](documentation/backend/docs/architecture/ML_SYSTEM_ARCHITECTURE.md) |
| `frontend/` | 4 | Frontend architecture and loading-state guides | [docs/HEALTH_CHECK_ARCHITECTURE.md](documentation/frontend/docs/HEALTH_CHECK_ARCHITECTURE.md) |
| `tools/` | 0 markdown + PDF assets | External tool setup docs | `documentation/tools/graphify/` |

---

## Core References

### Architecture
- [documentation/architecture/ARCHITECTURE.md](documentation/architecture/ARCHITECTURE.md) - full architecture document
- [documentation/architecture/ARCHITECTURE_SUMMARY.md](documentation/architecture/ARCHITECTURE_SUMMARY.md) - quick reference
- [documentation/architecture/MARKET_FEED_ARCHITECTURE.md](documentation/architecture/MARKET_FEED_ARCHITECTURE.md) - unified market feed architecture

### APIs and runtime
- [documentation/api/API_ENDPOINTS_DOCUMENTATION.md](documentation/api/API_ENDPOINTS_DOCUMENTATION.md) - main API surface
- [documentation/api/MARKET_FEED_WEBSOCKET_API.md](documentation/api/MARKET_FEED_WEBSOCKET_API.md) - market feed WS protocol
- [documentation/root/README.md](documentation/root/README.md) - actual install and migration checklist

### Operations
- [documentation/guides/STARTUP_INSTRUCTIONS.md](documentation/guides/STARTUP_INSTRUCTIONS.md) - quick local startup
- [documentation/guides/TESTING_WORKFLOW.md](documentation/guides/TESTING_WORKFLOW.md) - test workflow
- [documentation/troubleshooting/KNOWN_ISSUES_AND_REMEDIATION.md](documentation/troubleshooting/KNOWN_ISSUES_AND_REMEDIATION.md) - current known issues

---

## Major Subtrees

### `documentation/backend/`
- `docs/` - architecture, security, training, APIs, task summaries
- `guides/` - observability, health checks, realtime, caching, pagination
- `reference/` - quick-reference sheets
- `ml/` - monitoring and ensemble docs
- `testing/` - backend integration test documentation

### `documentation/root-artifacts/`
- `analysis/` - investigation reports
- `enhancements/` - enhancement completion records
- `fixes/` - fix summaries and verification notes
- `plans/` - implementation and remediation plans
- `references/` - context and API references
- `reports/` - audit reports
- `research/` - ML, LLM, and fundamentals research
- `status/` - status and recovery notes
- `tasks/` - older task lists

### `documentation/tasks/hawk-eye/`
- `completion-reports/` - task-by-task completion docs
- `market-feed/` - market feed change log
- `planning/` - task planning
- `tracking/` - task tracker

---

## Root-Level Markdown Files

| File | Purpose |
|------|---------|
| [documentation/README.md](documentation/README.md) | Documentation landing page |
| [documentation/DOCUMENTATION_INDEX.md](documentation/DOCUMENTATION_INDEX.md) | Master index |
| [documentation/DOCUMENTATION_ORGANIZATION_SUMMARY.md](documentation/DOCUMENTATION_ORGANIZATION_SUMMARY.md) | Organization snapshot |
| [documentation/TASKS_3_8_IMPLEMENTATION_COMPLETE.md](documentation/TASKS_3_8_IMPLEMENTATION_COMPLETE.md) | Standalone implementation report |

---

## Notes

- The top-level `documentation/` tree is no longer limited to the original 10-11 categories.
- `root/` and `root-artifacts/` hold a large amount of historical context.
- For current operational work, prefer `root/README.md`, `guides/`, `architecture/`, `api/`, and `troubleshooting/` before historical completion reports.

---

**Last Updated**: 2026-06-15  
**Documentation Version**: 1.3  
**Project Status**: Production-ready documentation set with historical artifacts retained
