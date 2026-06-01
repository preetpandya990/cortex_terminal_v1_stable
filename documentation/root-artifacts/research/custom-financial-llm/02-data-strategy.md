# 02 — Data Strategy
> Date: 2026-06-01 | Covers: training corpus, curation, synthetic data, quality bar, data compliance

Data quality — not model size — is the dominant lever on final quality. Recent scaling-law work is explicit: **the pretraining data and tokenizer determine the scaling trend; model size and even architecture have comparatively limited impact.** Treat data as the product.

---

## 1. Two distinct data needs (do not conflate)

| Need | Used by | Volume | Bar |
|------|---------|--------|-----|
| **Skill data** — instructions, Q&A, reasoning traces | SFT, DPO, GRPO | Hundreds → low thousands | Expert-validated, exact |
| **Knowledge data** — filings, news, prices, fundamentals | RAG index (not training) | As large as available | Fresh, well-structured, deduplicated |

The single biggest data mistake in finance LLMs is trying to push **knowledge** into **skill** data (training). Knowledge belongs in the retrieval index; training data teaches *how to think*, not *what is true today*.

---

## 2. Quality beats quantity — the governing principle

- **200 expert-validated examples beat 2,000 hastily collected ones.** Curate ruthlessly.
- Simple task adaptation: often **hundreds** of examples. Complex multi-step financial reasoning: **1,000–5,000**.
- Measured effect: a LoRA adapter took one finance task from **41% → 78%** accuracy — specialization yields disproportionate returns *when the data is clean*.

> **Decision: invest in a small, expert-reviewed gold set before scaling volume. Every training row is reviewed or generated against a verified reference.**

---

## 3. Skill-data sources for a financial LLM

1. **Public financial instruction sets** — FinGPT datasets, PIXIU/FLARE/FinBen task data, financial QA (FinQA, ConvFinQA, TAT-QA) for numerical reasoning over tables.
2. **Cortex proprietary signal** — your own labeled outcomes: signals + realized results, analyst notes, post-close counterfactuals (you already track these). This is your moat; competitors cannot replicate it.
3. **Synthetic data** (see §4) — to scale coverage cheaply once the gold set defines the shape.

---

## 4. Synthetic data — the 2026 method

Synthetic data is now standard and is **shaped for the specific fine-tune target**: SFT rows, DPO preference pairs, or RL prompts — each structured to match the loss function and schema of its stage.

- **Reasoning traces via distillation:** prompt a strong reasoning teacher (e.g., DeepSeek-R1 / a frontier model) to produce chain-of-thought solutions to financial problems, then **filter with an LLM-as-judge** and against verified numerical answers. This is exactly the Fin-R1 recipe (60,091 curated CoT samples).
- **Reinforcement/selector prompting for finance:** a policy network selects prompts; an LLM executor produces synthetic financial rows — improves coverage of rare scenarios.
- **Non-negotiable hygiene:** synthetic data must pass the *same* quality bar as real data — **dedup, PII redaction, and verification** against ground truth. Unverified synthetic reasoning is worse than no data (it teaches confident wrongness — a compliance risk in finance).

> **Decision: bootstrap reasoning data by distilling a strong teacher into verified CoT traces, judge-filtered and numerically checked. Never ship unverified synthetic reasoning into training.**

---

## 5. Knowledge-data (RAG corpus) curation

Even though this feeds retrieval, not training, curation rigor still governs accuracy. Pipeline (industry standard — RefinedWeb/DCLM/NeMo-Curator lineage):

1. **Acquire** from diverse sources (filings, news, fundamentals, transcripts, your market feeds).
2. **Filter** — language + heuristic + a quality classifier (fastText-style) keeping the high-quality top slice.
3. **Deduplicate** — exact (n-gram) + fuzzy (MinHash) + temporal (across data dumps). Dedup is standard and improves both efficiency and quality.
4. **Redact PII** and tag provenance (source + timestamp) for auditability.
5. **Structure-preserve** — keep tables/numbers intact for table-aware chunking (see [`05-retrieval-and-realtime.md`](./05-retrieval-and-realtime.md)).

---

## 6. Data licensing & compliance (finance-specific)

- **Market-data licensing:** exchange/vendor feeds (e.g., Upstox, NSE/BSE) carry redistribution and derived-data terms. Confirm that indexing into a RAG store and surfacing in model outputs is within license.
- **Training-data provenance:** under SR 11-7 / EU AI Act, *fine-tuning data is a governed model component* — you must be able to state what data trained the model. Keep an immutable manifest (source, license, date, transform) for every training set.
- **PII / MNPI:** never index material non-public information or customer PII into a shared retrieval store. Segregate by access scope.

> **Decision: maintain a versioned data manifest (DVC or LakeFS) from day one. Every model version pins the exact data snapshots that produced it — a hard governance requirement, not a nicety.**

See [`09-governance-and-compliance.md`](./09-governance-and-compliance.md).
