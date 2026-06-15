# Cortex Custom LLM — Plain-English Brief
> Date: 2026-06-01 | Owner: Het Trivedi | A one-document summary of the full knowledge base.
> Full detail lives in `documentation/root-artifacts/research/custom-financial-llm/` (README + 10 docs + sources).

This is the short version of the whole plan, written so anyone on the team can follow it — without dumbing down the decisions or the numbers. It ends with concrete recommendations: the model to use, what to run on the dev laptop, and an ideal spec sheet for the production server.

---

## 1. What we're building

An AI "financial analyst" built into Cortex: it reads market data, news, and fundamentals, reasons about them, and explains signals in plain language. **It only analyzes and advises — it never places orders.** It runs on our own server as part of the Cortex system, and it answers interactively (fast, conversational).

---

## 2. The big decision: we are NOT building a model "from scratch"

Think of it like hiring:

- **From scratch** = raising and educating a person from birth just so they can answer finance questions. Costs millions, takes 6–18 months, and you're betting you can out-teach the whole industry.
- **Our approach** = hire an already-educated graduate (a strong, free, open model) and train them on *our* specific job. Cheaper, faster, and — proven repeatedly in finance — usually **better**.

The proof in our own field:

- **BloombergGPT** was built from scratch (50 billion parameters, ~**$2.67M**). It was matched or beaten on many tasks by **FinGPT**, which just fine-tuned an open model for **under $300**.
- A modern 7-billion model (**Fin-R1**) now **beats a 70-billion model** at financial reasoning.

There's also a deeper reason, special to finance: **markets change by the second.** A from-scratch model "memorizes" the world as of its training day and is instantly out of date. We don't want yesterday's prices frozen into the AI. So building from scratch isn't just expensive here — it's the wrong shape for the problem. (Building it would also be textbook over-engineering, which is off the table.)

> **Bottom line:** adapt a strong open model + look facts up live. From-scratch stays "off the roadmap" unless future evidence forces it.

---

## 3. The core idea: separate "skill" from "knowledge"

This one distinction drives the whole design:

- **Skill** = *how to think* — how to reason about finance, follow instructions, explain clearly, stay safe. This we **train into** the model. It changes slowly.
- **Knowledge** = *what's true right now* — today's prices, the latest filing, this morning's news. This we **look up live** and feed to the model at the moment of the question. It changes constantly.

We never memorize live facts into the model. We teach it to think, and we give it an always-current "open book" to read from. Everything below follows from this.

---

## 4. How it's put together (the layers, simply)

Like a capable analyst sitting in a well-equipped office:

1. **The brain — a base open model** (the educated graduate we hire).
2. **On-the-job training** — we coach it on finance using curated examples (this is "fine-tuning").
3. **Reasoning practice** — we drill it on problems where the answer is checkable (a ratio, a cash flow), so it learns to be *correct*, not just fluent.
4. **The open book — live retrieval (RAG)** — a fast, organized reference library of current market data, news, and fundamentals it reads from for every answer.
5. **The tools** — a calculator, a database, the Cortex signal engine. The model **doesn't do math in its head**; it uses tools, and every number is logged.
6. **The team of specialists (agents)** — instead of one generalist, a small "desk": a data-gatherer, an idea-generator, a risk-checker, coordinated by a manager. (No "execution" specialist — we don't trade.)
7. **The supervisor — checks & guardrails** — graded exams, safety filters, and audit logs around everything.

---

## 5. The pieces, briefly

**Teaching it (fine-tuning).** We use a lightweight, cheap method (LoRA) that adds a small "skill patch" to the model instead of retraining the whole thing. **Quality beats quantity** — 200 expert-checked examples beat 2,000 sloppy ones. We grow data cheaply by having a strong "teacher" AI generate practice problems, then we **verify every answer** before using it (an unverified finance example teaches confident wrongness — a real risk).

**Live knowledge (RAG) — and why finance makes it tricky.** Generic "look it up" systems break on financial data: naïve handling drops table-reading accuracy from **91% to 44%**, and off-the-shelf setups produce **factual errors ~15% of the time**. So we build it the finance-aware way: keep tables intact, send numbers to tools (not fuzzy text matching), and connect facts in a graph (company ↔ filing ↔ sector ↔ peers), which pushes accuracy up toward **99%**. Every fact carries a timestamp and a source citation.

**The specialist team (agents).** Each step — gather, analyze, risk-check — is a focused agent with the right tools, coordinated like a trading desk. This makes the system testable and auditable step by step, instead of one giant black-box prompt.

**Checking it's right (evaluation).** We **write the exam first**: 100–500 real Cortex questions with known-good answers. Nothing ships unless it passes — beats the previous version, gets the numbers right, cites its sources, and is 100% safe on the compliance tests. Decisions are graded, not guessed.

**Running it fast (serving).** Over **90% of the long-term cost is answering questions**, not training. We shrink the model with "quantization" (cuts memory use 50–75% with negligible quality loss) and serve it with vLLM so it's fast and handles many users at once.

**Keeping it safe & legal (governance).** In finance, regulators treat this AI as a **"model"** with required paperwork: an inventory, independent validation, monitoring, and a full **audit trail** (for any answer, we can reproduce the exact question, model version, sources used, and which safety filters fired). This is built in from **day one**, not bolted on later — and starting "advice-only" keeps us out of the heaviest regulatory tier.

---

## 6. The plan (staged, so value comes early and we never overbuild)

| Phase | What we do | Roughly |
|-------|------------|---------|
| **P0** | Build the exam + the live "open book" (RAG) on an open model. No training yet. | 2–4 weeks |
| **P1** | Fine-tune it on finance; beat the un-trained version on the exam. | 4–8 weeks |
| **P2** | Add reasoning drills + the specialist-agent team + full compliance hardening. | 2–3 months |
| **P3** | *Only if the exam shows a gap we can't otherwise close:* deeper domain training. | gated |
| From scratch | **Not planned.** Re-opens only if very specific conditions all come true. | — |

Each phase delivers something useful on its own, and we only move to the next, more expensive layer if the evidence says we need it.

---

## 7. Recommendations — the models

| Role | Recommended | Why |
|------|-------------|-----|
| **Main model (the brain)** | **Qwen3-14B-Instruct** (free, Apache-2.0 license) | Best balance of finance/number skills, license, and speed; runs interactively on one 24 GB GPU |
| **Reasoning version** | **DeepSeek-R1 distill (14B/32B)**, or our Qwen3 drilled on reasoning | Reasoning-first; this is how a 7B model out-reasons a 70B |
| **Optional higher ceiling** | **Qwen3.6-35B "MoE"** | 35B-level quality but only ~3B "active" at a time → still fast |
| **Keep what works** | **FinBERT** (already in Cortex) | Cheap, fast news-sentiment scoring — no reason to replace it |

> **Pick the final one with the exam (Phase 0/1), not by reputation.** Qwen3-14B is the safe, world-class default to start.

---

## 8. Recommendations — the dev laptop (the current machine)

The current box is an **RTX 3050 laptop: 4 GB GPU (already fully used by Cortex's TensorFlow), 15 GB RAM (~3 GB free), WSL2.** It is fine for **development and wiring things together**, but it **cannot run the real model**. Use it like this:

- **For coding/integration:** run a tiny model — **Phi-4-mini (3.8B)** or **Llama-3.2-3B**, 4-bit, on CPU via Ollama/llama.cpp. Good enough to build and test the plumbing.
- **Keep FinBERT** running as today for sentiment.
- **Do real training elsewhere** — rent a powerful cloud GPU (H100) by the hour for fine-tuning; don't train on the laptop.
- **Don't serve production from here.** Point the app at either the small local model (dev) or a shared GPU endpoint.

---

## 9. Recommendations — ideal production server spec sheet

For a world-class, interactive, self-hosted financial analyst co-located with Cortex (advice-only). The LLM GPU is **dedicated** and separate from whatever GPU Cortex's existing ML stack uses.

| Component | Minimum (works) | **Recommended (world-class)** | Headroom (32B reasoning / high concurrency) |
|-----------|-----------------|-------------------------------|---------------------------------------------|
| **GPU** | 1× 24 GB (RTX 4090 / A10 / L40S) | **1× 48 GB (NVIDIA L40S or RTX 6000 Ada)** | 1× 80 GB (H100) or 2× 48 GB |
| **CPU** | 16 cores | **24–32 cores** (RAG, agents, app glue) | 32+ cores |
| **System RAM** | 64 GB | **128 GB** (vector index + Cortex services + OS) | 256 GB |
| **Storage** | 1 TB NVMe SSD | **2 TB NVMe SSD** (models, vector store, audit logs, MLflow) | 2 TB+ NVMe |
| **OS / stack** | Linux (native, **not** WSL2), NVIDIA driver w/ CUDA 12.4+, Docker, vLLM | same | same |
| **Vector DB** | pgvector on existing Postgres | **Qdrant / Weaviate / Milvus** (dedicated) | clustered vector DB |
| **Networking** | — | Low-latency to market-data feeds; same region/VPC as Cortex if cloud | — |
| **Monitoring** | Prometheus + Grafana (already in place) | same | same |

**Notes:**
- **Interactive = the model must fit entirely in GPU memory.** Spilling to CPU is 10–30× slower and kills the "fast/conversational" requirement.
- **A single 48 GB GPU** comfortably runs the recommended Qwen3-14B (quantized) with room for many simultaneous users and the live-knowledge cache. Drop to 24 GB only if budget demands; step up to 80 GB only for the larger 32B reasoning path.
- **Training is rented, not owned** — pay for H100 time only when fine-tuning, then release it.

---

## 10. The one thing we still need to decide

The exact model + quantization is pinned by the **production server's GPU**. If it's still being chosen, **provision the "Recommended" column above (48 GB GPU)** and **Qwen3-14B** is the world-class default. Tell me the final server GPU/RAM and I'll lock the precise model size, quantization, and concurrency target.

---

*This brief summarizes the full knowledge base at `documentation/root-artifacts/research/custom-financial-llm/`. For any section here, the matching numbered doc there has the depth, the trade-offs, and the cited sources.*
