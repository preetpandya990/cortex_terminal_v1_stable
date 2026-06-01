# 01 — Approach & Decision Framework
> Date: 2026-06-01 | Decides: from-scratch vs CPT vs fine-tune vs RAG for a financial LLM

This is the most important document in the KB. It explains *how* to choose a build path and *why* the recommendation lands where it does.

---

## 1. The four paths (precise definitions)

| Path | What changes | Knowledge source | Cost / time |
|------|--------------|------------------|-------------|
| **Pretraining from scratch** | All weights, random init | Frozen into weights | $1M–$5M+, 6–18 mo |
| **Domain-adaptive continued pretraining (CPT)** | All/most weights, continued from an open base on a domain corpus | Frozen into weights | $5k–$100k, 2–6 wk |
| **Supervised fine-tuning + alignment (SFT/DPO/GRPO)** | A thin adapter (LoRA) or the full weights, on labeled examples | Skill, not facts | $100–$10k, 1–4 wk |
| **Retrieval-augmented generation (RAG) + tools** | Nothing in the model; context injected at inference | Retrieved live | Infra only, 2–8 wk |

These are **composable layers**, not mutually exclusive options. The real question is *which layers you need*, in what order.

---

## 2. The decision framework

Ask these in order. Stop at the first "no" — you rarely need the deeper layer.

1. **Does the answer depend on current/changing facts?** (prices, news, filings, positions)
   → **Yes for finance.** You need **RAG + tools**. This is non-negotiable and comes first.
2. **Does the base model already reason adequately about your domain once grounded?**
   → If yes, you may need *only* RAG. Validate with the eval harness before training anything.
3. **Does it lack domain skill — format, instruction-following, safe financial tone, numerical reasoning?**
   → Add **SFT + DPO/GRPO**. This is where most of the realizable quality lives.
4. **After SFT, does the eval still show a knowledge/representation gap that retrieval can't fill?** (e.g., deep proprietary jargon, a novel instrument class)
   → *Only then* consider **domain-adaptive CPT**.
5. **Do you have a hard requirement that no layer above can satisfy** — full weight ownership for IP/air-gap, a genuinely novel modality, or scale economics where amortized pretraining beats per-token inference?
   → *Only then* consider **from scratch**. For Cortex, none of these hold today.

> **Decision: the answer is "stop after step 3" for the foreseeable roadmap.** RAG + SFT/alignment is the core. CPT is gated on step 4 evidence. From-scratch is gated on step 5 and currently fails every trigger.

---

## 3. Why finance specifically rejects from-scratch

**(a) The freshness argument (structural, not economic).**
A from-scratch model's knowledge is frozen at its data cutoff. In markets, yesterday's knowledge is a liability. Any architecture that bakes facts into weights must be *retrained* to stay current — economically absurd at market speed. RAG updates instantly and for free. This alone disqualifies "knowledge-in-weights" as the primary strategy.

**(b) The cost/performance argument (empirical).**
- BloombergGPT: 50B params, from scratch, **~$2.67M** — and was matched or beaten on many public financial tasks by FinGPT, a **<$300** LoRA fine-tune of an open base.
- Fin-R1: a **7B** model (SFT + GRPO, reasoning distilled from DeepSeek-R1) beats DeepSeek-R1-Distill-**70B** and rivals 32B models on financial reasoning.
- The frontier of open base models (Qwen2.5, Llama 3.x, DeepSeek) advances every few months — for free. A from-scratch model is obsolete the day a better open base ships.

**(c) The risk argument.**
From-scratch concentrates 6–18 months and $1M+ into a single bet that must beat a moving target (open models) you could have simply adopted. Fine-tune + RAG is incremental, observable, and reversible at every step.

**(d) The "no over-engineering" argument.**
Building a foundation model to answer financial questions, when a fine-tuned open model + retrieval does it better and cheaper, *is* the textbook definition of over-engineering — which is explicitly out of bounds for this project.

---

## 4. When from-scratch (or full CPT) *would* be correct — the gates

Keep these written down so the decision is evidence-driven, not aspirational. Trigger = **all** conditions in a row hold.

**CPT gate (Phase 3):**
- A large (≥ billions of tokens), high-quality, *proprietary* financial corpus exists that the base model has never seen, **and**
- After best-effort SFT + RAG, the eval harness still shows a material, reproducible gap on tasks that retrieval cannot close, **and**
- A GPU cluster + ML specialist are available.

**From-scratch gate (out of scope):**
- A regulatory or contractual mandate requires **fully owned weights with no open-model lineage** (rare), **and**
- Eval proves open bases are *structurally* insufficient even after CPT, **and**
- Scale economics make amortized pretraining cheaper than inference at your volume (only true at extreme scale), **and**
- A dedicated research team + $1M+ budget exist.

If any condition is unmet, the gate stays closed. As of 2026-06-01, **every from-scratch condition is unmet.**

---

## 5. Recommendation

> **Build a layered system on a strong open-weight base: RAG + tools for knowledge, SFT + DPO/GRPO for skill and reasoning, agents for orchestration. Defer CPT behind an eval gate. Treat from-scratch as out of scope.** Sequence the work so Phase 0 (RAG + eval harness) ships value in weeks and every later layer is justified by measured gaps.

See [`03-architecture-and-models.md`](./03-architecture-and-models.md) for the model stack and [`10-roadmap-and-decision-gates.md`](./10-roadmap-and-decision-gates.md) for execution.
