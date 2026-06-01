# Custom Financial LLM — Knowledge Base
> Date: 2026-06-01 | Owner: Het Trivedi | Scope: LLM for the Cortex financial/trading platform
> Status: **Research complete → Recommendation issued.** Resources undefined (recommendation is resource-aware).

This knowledge base is the single source of truth for designing and building Cortex's custom financial/trading LLM. It is grounded in 2026 state-of-the-art research (see [`sources.md`](./sources.md)). Read this README first; it carries the decision. The numbered docs are the supporting depth.

---

## 0. How to use this KB

| # | Document | What it answers |
|---|----------|-----------------|
| — | **README** (this file) | The decision, the recommended stack, the from-scratch verdict, phasing |
| 01 | [`approach-and-decision.md`](./01-approach-and-decision.md) | From-scratch vs CPT vs fine-tune vs RAG — framework + finance evidence + resource forks |
| 02 | [`data-strategy.md`](./02-data-strategy.md) | Corpus, curation, synthetic data, quality bar, data licensing/compliance |
| 03 | [`architecture-and-models.md`](./03-architecture-and-models.md) | Base-model selection, sizing, reasoning models, tokenizer, the layered model stack |
| 04 | [`training-pipeline.md`](./04-training-pipeline.md) | CPT → SFT → DPO/GRPO, forgetting mitigation, frameworks & infra |
| 05 | [`retrieval-and-realtime.md`](./05-retrieval-and-realtime.md) | Finance RAG: table-aware chunking, GraphRAG, numerical/time-series, live feeds |
| 06 | [`agentic-orchestration.md`](./06-agentic-orchestration.md) | Multi-agent (Data/Alpha/Risk/Execution), tool use, DAG orchestration |
| 07 | [`evaluation-and-quality.md`](./07-evaluation-and-quality.md) | Benchmarks, custom eval harness, acceptance gates, regression |
| 08 | [`serving-and-infra.md`](./08-serving-and-infra.md) | vLLM, quantization, latency/throughput, cost model, hardware |
| 09 | [`governance-and-compliance.md`](./09-governance-and-compliance.md) | SR 11-7, EU AI Act, NIST AI RMF, guardrails, audit, hallucination control |
| 10 | [`roadmap-and-decision-gates.md`](./10-roadmap-and-decision-gates.md) | Phased plan, milestones, go/no-go gates, resource forks |
| — | [`sources.md`](./sources.md) | All citations, grouped by topic |

---

## 1. Executive summary

**The premise "build an LLM from scratch" should be rejected for this use case.** For a financial/trading product, pretraining a foundation model from scratch is the worst trade on the board: highest cost ($1M–$5M+), longest time (6–18 months), highest risk, and — proven repeatedly in finance — *not even the best-performing option*. The decisive precedent is **BloombergGPT** (50B params, from scratch, ~$2.67M) being matched or beaten on key financial tasks by **FinGPT** (a LoRA fine-tune of an open base costing **< $300**), and both now outclassed by 7B–32B open reasoning models.

Finance has a structural property that settles the architecture debate: **the valuable knowledge is fast-moving** (prices, news, filings change by the second). Fast-moving knowledge must be *retrieved at query time*, not *frozen into weights at training time*. This makes retrieval (RAG) and tool use the backbone, and fine-tuning the skill/format/reasoning layer on top — exactly the opposite of a from-scratch bet.

**Recommendation: build a layered, retrieval-grounded, fine-tuned, agentic system on a strong open-weight base model — not a from-scratch model.** Start where ROI is highest (RAG + evaluation harness), then layer in fine-tuning and verifiable reasoning. Keep from-scratch explicitly *deferred behind a decision gate*, not on the critical path.

---

## 2. The recommended stack (target architecture)

A production financial LLM is a **system of layers**, not a single trained model:

```
┌─────────────────────────────────────────────────────────────────────┐
│  L6  SERVING        vLLM + FP8 quant · continuous batching · latency  │
│                     budgets (interactive vs batch)                    │
├─────────────────────────────────────────────────────────────────────┤
│  L5  AGENTS         Data · Alpha · Risk · Execution pools             │
│                     tool use + function calling, DAG orchestration    │
│                     (Cortex signals/feeds become tools)               │
├─────────────────────────────────────────────────────────────────────┤
│  L4  RETRIEVAL      Finance-aware RAG: table-aware chunking,          │
│      (knowledge)    GraphRAG over tickers/filings, numeric via tools, │
│                     real-time market/news feeds  ← current knowledge  │
├─────────────────────────────────────────────────────────────────────┤
│  L3  ALIGNMENT      DPO (style/safety) + GRPO/RLVR (verifiable        │
│      (reasoning)    numerical reasoning — Fin-R1 recipe)              │
├─────────────────────────────────────────────────────────────────────┤
│  L2  SFT            Instruction tuning on curated + synthetic         │
│      (skill)        financial Q&A / reasoning traces (LoRA/QLoRA)     │
├─────────────────────────────────────────────────────────────────────┤
│  L1  CPT (optional) Domain-adaptive continued pretraining —           │
│                     ONLY behind a decision gate (see §4)              │
├─────────────────────────────────────────────────────────────────────┤
│  L0  BASE MODEL     Open-weight, commercially licensed                │
│                     (Qwen2.5 · Llama 3.x · DeepSeek-R1 distill)       │
└─────────────────────────────────────────────────────────────────────┘
        ▲ CROSS-CUTTING: Governance (SR 11-7, EU AI Act), guardrails,
          audit trails, evaluation harness — mandatory in finance
```

**Layer ownership of "knowledge" vs "skill":**
- **Knowledge** (what is true *right now*) → **L4 retrieval + L5 tools**. Never bake market facts into weights.
- **Skill** (how to reason, format, follow finance instructions, stay safe) → **L1–L3 training**.

---

## 3. Why NOT from scratch — the evidence

| Approach | Time | Cost (order of magnitude) | Justified when | Verdict for Cortex |
|----------|------|---------------------------|----------------|--------------------|
| **Pretrain from scratch** | 6–18 mo | **$1M–$5M+** | Novel modality, mandated full-weight ownership/air-gap, extreme scale economics | ✗ **Deferred** (behind a gate) |
| **Domain-adaptive CPT** | 2–6 wk | $5k–$100k | Large proprietary corpus *and* an eval gap remains after SFT | ⚠ **Phase 3, gated** |
| **SFT + DPO/GRPO** (LoRA) | 1–4 wk | $100–$10k | Need domain skill, format, safety, verifiable reasoning | ✓ **Core** |
| **RAG + agents** | 2–8 wk | Low (infra only) | Need current/real-time facts, grounding, auditability | ✓ **Core — start here** |

**Finance-specific precedent:**

| Model | Approach | Size | Cost | Outcome |
|-------|----------|------|------|---------|
| BloombergGPT | From scratch | 50B | ~$2.67M | Beaten on many tasks by far cheaper fine-tunes; never released; frozen knowledge |
| FinGPT | LoRA fine-tune | 7B base | **< $300** (some runs ~$65) | Matched/surpassed BloombergGPT on key tasks; cheaply *updatable* |
| Fin-R1 | SFT + GRPO distill | 7B | Low | Beats DeepSeek-R1-Distill-**70B**, rivals 32B models on financial reasoning |

The lesson is consistent and three years deep: **in finance, adaptation beats origination.** A small open model, fine-tuned and retrieval-grounded, wins on cost, speed, freshness, and — increasingly — raw quality.

---

## 4. Resource-forked phasing (summary)

Because budget/team are undefined, the plan is staged so each phase delivers standalone value and the next is unlocked only by evidence (an eval gap) **and** available resources. Full detail in [`10-roadmap-and-decision-gates.md`](./10-roadmap-and-decision-gates.md).

| Phase | Goal | Approx. effort | Min. resources |
|-------|------|----------------|----------------|
| **P0** | RAG over Cortex data on an open base + **eval harness** | 2–4 wk | 1 eng, cloud GPU or API |
| **P1** | SFT/LoRA + DPO on curated+synthetic finance data; beat base on eval | 4–8 wk | 1–2 eng, rented H100s |
| **P2** | Verifiable reasoning (GRPO) + agentic orchestration + governance | 2–3 mo | 2–3 eng |
| **P3** | Domain-adaptive CPT on proprietary corpus — **only if P1/P2 eval shows a gap** | 1–2 mo | GPU cluster, ML specialist |
| **From scratch** | **Out of scope** unless a specific evidenced trigger fires (see gate) | — | Research team, $1M+ |

---

## 5. Open decisions needed from you

These steer the build and are tracked at the top of [`10-roadmap-and-decision-gates.md`](./10-roadmap-and-decision-gates.md):

1. **Deployment posture** — cloud (rent H100s on demand) vs on-prem/air-gapped? (Drives base-model licensing, serving, and whether weight-ownership ever becomes a real from-scratch trigger.)
2. **Latency class** — interactive (<1s, conversational analyst) vs batch (overnight signal/report generation)? (Drives model size + serving budget.)
3. **Autonomy ceiling** — does the LLM ever *place orders*, or strictly analyze/advise? (Drives the entire governance/guardrail surface and regulatory scope.)
4. **Build vs buy for P0 base** — acceptable to start on a hosted API model to validate retrieval, then bring weights in-house? Or open-weight from day one?

---

## 6. Status

- ✅ Research complete (2026-06-01) — landscape, approaches, RAG, evals, infra, governance.
- ✅ Recommendation issued — layered fine-tune + RAG + agents; from-scratch deferred behind a gate.
- ⏭ Next: confirm the four open decisions (§5), then execute **Phase 0**.
