# 10 — Roadmap & Decision Gates
> Date: 2026-06-01 | Covers: phased plan, milestones, go/no-go gates, resource forks

Staged so each phase ships standalone value, and each next layer is unlocked only by **measured evidence** (an eval gap) plus **available resources**. This is the operational form of "no over-engineering."

---

## 0. Decisions required before Phase 0

| # | Decision | Why it matters | Recommendation |
|---|----------|----------------|----------------|
| 1 | Deployment posture (cloud vs on-prem/air-gap) | Drives licensing, serving, weight-ownership question | Cloud/rented to start |
| 2 | Latency class (interactive vs batch) | Drives model size + serving budget | Both, sized per [`08`](./08-serving-and-infra.md) |
| 3 | Autonomy ceiling (advise vs act) | Drives entire governance surface | **Analyze/advise-only first** |
| 4 | P0 base: hosted API vs open-weight from day one | Speed-to-validate vs control | API to validate, then open-weight |

---

## 1. Phase 0 — Grounding & instrumentation (2–4 wk)
**Goal:** prove retrieval value and stand up the measurement instrument.
- Build the **custom eval harness** (100–500 gold Q&A) — *first* ([`07`](./07-evaluation-and-quality.md)).
- Finance-aware **RAG** over Cortex data (table-aware + provenance; graph optional) ([`05`](./05-retrieval-and-realtime.md)).
- Run on an open base **+ RAG**, zero training.
- Inference tracing + Grafana SLOs from day one ([`08`](./08-serving-and-infra.md), [`09`](./09-governance-and-compliance.md)).

**Exit gate:** RAG+base beats a no-RAG baseline on the gold eval; tracing/guardrail scaffolding live.
**Resources:** 1 engineer, cloud GPU or API.

---

## 2. Phase 1 — Skill (4–8 wk)
**Goal:** a fine-tuned model that beats the base on Cortex tasks.
- Curate gold skill set + distilled, verified synthetic CoT ([`02`](./02-data-strategy.md)).
- **SFT (LoRA) → DPO**; track everything in MLflow ([`04`](./04-training-pipeline.md)).
- Shortlist/lock the base by eval ([`03`](./03-architecture-and-models.md)).

**Exit gate:** fine-tuned model passes all acceptance gates and beats Phase-0 on the gold eval.
**Resources:** 1–2 engineers, rented H100s.

---

## 3. Phase 2 — Reasoning & agents (2–3 mo)
**Goal:** verifiable financial reasoning + orchestrated multi-agent system.
- **GRPO/RLVR** for numerical correctness (Fin-R1 recipe) ([`04`](./04-training-pipeline.md)).
- Multi-agent DAG over Cortex engines-as-tools ([`06`](./06-agentic-orchestration.md)).
- Harden governance to launch bar ([`09`](./09-governance-and-compliance.md)).

**Exit gate:** independent validation sign-off; safety eval 100%; reasoning gates met.
**Resources:** 2–3 engineers.

---

## 4. Phase 3 — Domain-adaptive CPT (GATED, 1–2 mo)
**Enter only if** Phase 1/2 eval shows a material gap retrieval can't close **and** a large proprietary corpus + cluster + ML specialist exist.
- CPT with ≥1% general-text mix + instruction-residual to avoid forgetting ([`04`](./04-training-pipeline.md)).

**Exit gate:** CPT model beats the non-CPT model on the gold eval by a margin that justifies its cost. Else, **abandon CPT** — no sunk-cost continuation.

---

## 5. From scratch — OUT OF SCOPE
Not on the roadmap. Re-opens **only** if every condition in the from-scratch gate ([`01`](./01-approach-and-decision.md) §4) is simultaneously met. As of 2026-06-01, all fail.

---

## 6. Phase → layer → doc map

| Phase | Adds layer | Primary docs |
|-------|-----------|--------------|
| P0 | L4 RAG + L0 base + eval + governance scaffold | 05, 07, 09 |
| P1 | L2 SFT + L3 DPO | 02, 03, 04 |
| P2 | L3 GRPO + L5 agents + governance launch bar | 04, 06, 09 |
| P3 | L1 CPT (gated) | 01, 04 |

> **Bottom line:** value in weeks (P0), a custom model in months (P1–P2), and the expensive layers (CPT/from-scratch) reached *only if data proves they're needed*. That is the world-class, no-shortcut, no-over-engineering path.
