# 04 — Training Pipeline
> Date: 2026-06-01 | Covers: CPT → SFT → DPO/GRPO, forgetting mitigation, frameworks, infra

The modern post-training stack is **modular**: SFT for instruction-following, preference optimization (DPO/SimPO/KTO) for alignment, and RL with verifiable rewards (GRPO/DAPO/RLVR) for reasoning. Apply only the stages your eval demands.

---

## 1. Stage map

| Stage | Objective | Data | Output |
|-------|-----------|------|--------|
| **(Optional) CPT** | Next-token on domain corpus | Large unlabeled finance text | Domain-shifted base |
| **SFT** | Next-token on completions | Curated + synthetic instructions/CoT | Instruction-following finance model |
| **DPO** (or SimPO/KTO) | Preference optimization, no reward model | Chosen/rejected pairs | Aligned to preferred style/safety |
| **GRPO / RLVR** | RL with verifiable reward | Prompts + a checkable reward | Strong, *correct* reasoning |

**GRPO** is the current favorite for reasoning: it **drops the critic model**, samples a group (8–64) of responses per prompt, and computes advantage by normalizing each reward against the group mean/std — cheaper and stable. Finance suits it because correctness is often verifiable (numbers check out or they don't).

> **Decision: default pipeline = SFT → DPO → GRPO on a LoRA adapter. Add CPT only behind the gate in [`01`](./01-approach-and-decision.md).**

---

## 2. Catastrophic forgetting (the CPT trap)

CPT on a narrow domain corpus **erodes general ability and instruction-following** ("catastrophic forgetting"). Mitigations, in order of preference:

1. **Mix in general text** — as little as **~1%** general data during CPT substantially mitigates forgetting. Curate the mix deliberately.
2. **Instruction-residual / instruction-aware CPT** — restores instruction-following post-adaptation without a full re-SFT.
3. **Dual-expert / routing** — keep a general expert and a domain expert, route per task, instead of forcing one model to be both.

> This is the central reason CPT is gated, not default: done naively it makes the model *worse* at following instructions — unacceptable for a product.

---

## 3. Parameter-efficient fine-tuning (PEFT)

- **LoRA / QLoRA** is the default. QLoRA fits 7B–8B fine-tuning on a single 12–24 GB GPU; adapters are tiny, swappable, and cheap to retrain as data refreshes.
- Tune rank, learning rate, and target modules deliberately; PEFT quality is sensitive to these.
- Keep adapters **versioned and composable** — you may run different adapters per task (e.g., sentiment vs reasoning) behind the same base.

---

## 4. Frameworks & infra

**Fine-tuning / post-training:**
| Tool | Use |
|------|-----|
| **Unsloth** | Fastest single-GPU QLoRA SFT; great for P0/P1 |
| **LLaMA-Factory / Axolotl** | Config-driven SFT + DPO + GRPO across recipes |
| **TRL** | Reference DPO/GRPO implementations |

**Distributed (only if going big / CPT):**
| Tool | Use |
|------|-----|
| **PyTorch FSDP** | Right default for 7B–70B on 2–8 GPUs |
| **DeepSpeed (ZeRO / ZeRO-Infinity)** | Max VRAM savings + offload when memory-bound |
| **Megatron-LM** | Tensor/pipeline parallelism at true scale (NVIDIA) |
| **NVIDIA NeMo Curator** | Domain-adaptive data prep at scale |

**Experiment tracking:** you already run **MLflow** (`backend/mlruns/`) — extend it to log datasets, base-model hash, adapter, hyperparameters, and eval scores per run. This doubles as your SR 11-7 model-inventory record.

---

## 5. Reproducibility & governance hooks

Every training run must record, immutably:
- Base model + exact hash/version
- Training data snapshot ID (from the data manifest, [`02`](./02-data-strategy.md))
- Hyperparameters + framework versions
- Eval scores against the harness ([`07`](./07-evaluation-and-quality.md))
- Resulting adapter/model artifact hash

> **Decision: no model is promotable without a complete, reproducible MLflow record. This is both engineering hygiene and the SR 11-7 audit trail.**
