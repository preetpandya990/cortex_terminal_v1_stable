# 09 — Governance & Compliance
> Date: 2026-06-01 | Covers: model risk, guardrails, audit, hallucination control, regulatory frameworks

In financial services this is **not optional polish — it is a gate to production.** Regulators treat an LLM-based tool as a *model*, and **hallucination control is a compliance requirement, not a product-quality preference.** Design governance in from day one; it is far cheaper than retrofitting.

---

## 1. Regulatory frameworks in scope

| Framework | What it requires of you |
|-----------|-------------------------|
| **SR 11-7** (Fed/OCC) + **OCC 2024 GenAI update** | Treats every deployed ML/LLM tool as a *model*: inventory, validation, ongoing monitoring |
| **EU AI Act** | Risk-tiered obligations; financial decisioning trends high-risk |
| **NIST AI RMF** | Govern/Map/Measure/Manage risk lifecycle |
| **ISO/IEC 42001** | AI management-system certification |

> Applicability depends on jurisdiction and whether you advise vs act. Confirm scope with counsel — but **design to the strictest plausible standard** so you are never blocked at launch.

---

## 2. SR 11-7 applied to an LLM (the mental model)

Every component is a **governed model artifact** to inventory, validate, and monitor:

- Base model **(and version)**
- Fine-tuning **data** (the manifest from [`02`](./02-data-strategy.md))
- Prompt templates
- **Retrieval / knowledge sources** ([`05`](./05-retrieval-and-realtime.md))
- Safety filters / guardrails
- Decision boundaries (the autonomy level from [`06`](./06-agentic-orchestration.md))

**Audit requirement:** given a complaint or examiner request, you must reproduce **the exact prompt, response, model+version, which guardrails fired, retrieved sources, and timestamps** for each step. Architect for this from the start.

> **Decision: every inference is fully traced (prompt, model+version, retrieved sources+as-of, guardrail events, output) to immutable, queryable logs. This is a launch blocker, not a v2.**

---

## 3. Guardrails (runtime)

Runtime policies that inspect, validate, modify, or block requests/responses:

- **Input**: block prompt-injection, MNPI solicitation, out-of-scope requests.
- **Output**: enforce disclaimers, suppress unlicensed financial advice, redact PII/MNPI, require citations + as-of stamps for factual claims.
- **Numeric**: cross-check model numbers against tool results; block unverifiable quantitative claims.
- **Validate for**: task fit, harmful-content suppression, **hallucination rate**, data leakage, bias.

---

## 4. Hallucination control (first-class)

The compounding defenses, in order:
1. **Retrieval grounding** — answer only from retrieved, cited, timestamped sources ([`05`](./05-retrieval-and-realtime.md)).
2. **Tool-verified numbers** — no LLM arithmetic ([`06`](./06-agentic-orchestration.md)).
3. **GRPO/RLVR** — train toward verifiable correctness ([`04`](./04-training-pipeline.md)).
4. **Output guardrails** — block/flag uncited or unverifiable claims.
5. **Monitoring** — hallucination rate as a production SLO ([`07`](./07-evaluation-and-quality.md)).

Recall the baseline risk: generic financial RAG produced **factual errors in 14.7%** of responses. The layered defense is what drives that toward acceptable.

---

## 5. Roles & process

Stand up the GRC roles for LLM deployment: **model owner, independent validator, risk/compliance, monitoring.** Validation must be *independent of development* (SR 11-7). Maintain a living **model card + risk assessment** per model version.

> **Decision: no production launch without (a) full inference tracing, (b) an independent validation sign-off, (c) a documented model card + risk assessment, (d) guardrails proven on the safety eval set. Start analyze/advise-only to keep initial scope below "high-risk autonomous execution."**
