# 03 — Architecture & Model Selection
> Date: 2026-06-01 | Covers: base-model choice, sizing, reasoning models, tokenizer

The model is a **decoder-only transformer** — but you are *selecting and adapting* one, not designing one. Architecture innovation is not where your edge is; data, retrieval, and reasoning alignment are.

---

## 1. Base-model selection (2026)

The **7B–14B tier is the enterprise sweet spot** — strong capability at hardware you can actually afford to serve. Reserve 32B–70B for batch/reasoning-heavy paths.

| Model family | Sizes | Why consider it | Notes |
|--------------|-------|-----------------|-------|
| **Qwen 2.5** | 7B / 14B / 32B / 72B | Surprise top performer in finance projects; strong multilingual + numeric | Often the best 7B for finance |
| **Llama 3.1 / 3.3** | 8B / 70B | Mature ecosystem, broad tooling, permissive license | Safe default, huge community |
| **DeepSeek-R1 distills** | 7B / 14B / 32B / 70B | Reasoning-native base; ideal start for a reasoning finance model | Pairs with GRPO well |
| **Mistral** | 7B v0.3 | Lean, fast, efficient | Good when latency-bound |

**Selection method — not vibes:**
1. Build the eval harness *first* (see [`07-evaluation-and-quality.md`](./07-evaluation-and-quality.md)).
2. Run 2–3 candidate bases **zero-shot + RAG** on your eval.
3. Pick the best price/quality point at your latency class, then fine-tune *that*.

> **Decision: shortlist Qwen 2.5 (7B/14B) and a DeepSeek-R1 distill (reasoning path); choose by eval, not reputation. Lock a permissive commercial license (Apache-2.0 / Llama license) — verify before committing.**

---

## 2. Sizing — capability vs cost

- **Too large** → over-fits your data, blows the serving budget, hurts latency.
- **Too small** → under-fits the patterns.
- Reasoning distillation collapses the size requirement: **Fin-R1 (7B) beats a 70B** on financial reasoning. You very likely do **not** need a 70B in production.

| Latency class | Suggested size | Rationale |
|---------------|----------------|-----------|
| Interactive analyst (<1s TTFT) | 7B–14B, quantized | Serving cost + responsiveness dominate |
| Batch signals/reports (overnight) | 14B–32B (or 70B if eval justifies) | Quality over latency; amortized |

Scaling-law reminder for any pretraining/CPT: compute **C ≈ 6 · N · D** (N=params, D=tokens); Chinchilla-optimal is **~20 tokens/param**. Relevant only if a CPT gate opens — not for fine-tuning.

---

## 3. The reasoning path (the real differentiator)

General reasoning gains **do not automatically transfer** to finance — domain-specific reasoning training is required. The proven 2026 recipe (Fin-R1 / Fin-o1):

1. Start from a reasoning-capable base (DeepSeek-R1 distill or a strong instruct model).
2. **SFT** on distilled, verified financial chain-of-thought.
3. **GRPO / RLVR** (RL with verifiable rewards) where the reward checks *numerical correctness* — finance is unusually amenable to verifiable rewards because many answers are checkable (a ratio, a cash flow, a P&L).

This buys frontier-level financial reasoning at 7B cost. Detail in [`04-training-pipeline.md`](./04-training-pipeline.md).

---

## 4. Tokenizer

- For an **adapt-an-open-base** strategy you **keep the base tokenizer** — changing it discards pretrained embeddings.
- A **custom BPE tokenizer** is only worth it under a from-scratch/heavy-CPT path, where tailoring vocabulary to financial jargon (tickers, instrument codes, numeric formats) improves efficiency. Not applicable to the recommended path initially.
- **Numeric tokenization** is a known weak spot: prefer routing exact arithmetic to **tools/code** rather than trusting token-level math (see agents, [`06`](./06-agentic-orchestration.md)).

> **Decision: keep the base tokenizer. Do not hand exact arithmetic to the LLM — route it to a calculator/Python tool.**

---

## 5. The full model stack (recap)

```
L0 Base (Qwen2.5-14B / DeepSeek-R1-distill)   ← selected by eval
   └─ L1 CPT (gated, optional)                ← only if eval gap persists
        └─ L2 SFT (LoRA, curated+synthetic)   ← skill & format
             └─ L3 DPO + GRPO                  ← safety + verifiable reasoning
                  → wrapped by L4 RAG, L5 agents, L6 serving (separate docs)
```

The trained artifact is small and swappable. The **system** around it (retrieval, tools, governance) is where durable value and most engineering live.
