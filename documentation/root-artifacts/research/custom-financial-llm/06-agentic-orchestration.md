# 06 — Agentic Orchestration
> Date: 2026-06-01 | Covers: multi-agent design, tool use, function calling, orchestration

The 2026 frontier for financial AI is **agentic**: systems that reason, call tools, remember, and self-correct — not a single prompt-in/answer-out model. An estimated **89% of global equity trading volume** is already AI-driven; the shift now is from static algorithms to agentic systems.

---

## 1. Reference multi-agent topology

Specialized agent **pools**, each with reasoning + tools + memory, sequenced by an orchestrator:

| Agent pool | Responsibility | Tools (Cortex hooks) |
|------------|----------------|----------------------|
| **Data** | Gather & normalize market/news/fundamentals | RAG retriever, market-feed API, fundamentals API |
| **Alpha** | Generate hypotheses/signals & rationale | Cortex signal engine, pattern/ML models, backtester |
| **Risk** | Constrain, stress-test, check exposure & compliance | Risk models, guardrails, position store |
| **Execution** | (If in scope) translate decisions to orders | Broker API — **gated**, see [`09`](./09-governance-and-compliance.md) |

The **orchestrator** is the "trading-desk manager": it assigns tasks and sequences agents as a **directed acyclic graph (DAG)**, not a hard-coded script. This makes the pipeline observable, testable, and auditable per step.

> **Decision: model the system as a DAG of specialized agents over Cortex's existing engines-as-tools. Do not build a monolithic "do-everything" prompt.**

---

## 2. Tool use / function calling

- The LLM's job is to **decide and call**, not to compute. Exact arithmetic, price lookups, backtests, and DB queries are **tools** with typed schemas.
- This directly fixes the LLM's numeric weakness ([`03`](./03-architecture-and-models.md)) and gives every quantitative claim a verifiable, logged source.
- Prefer **strict, validated function schemas**; reject/repair malformed tool calls; log every call (args + result) for audit.

---

## 3. Memory

- **Short-term**: the working context of a task/DAG run.
- **Long-term**: prior analyses, outcomes, and post-close counterfactuals (Cortex already tracks these) — retrievable so agents learn from realized results.

---

## 4. Existing frameworks to learn from (not necessarily adopt)

- **TradingAgents** (TauricResearch) — open multi-agent LLM trading framework; multi-provider (GPT-5.x, Gemini 3.x, Claude 4.x, Grok 4.x). Good reference topology.
- **NVIDIA multi-agent signal-discovery** patterns; vendor-neutral orchestration layers are the 2026–2028 trend.

Evaluate buy-vs-build per the project's standards; the **topology and tool-boundary discipline** matter more than the specific framework.

---

## 5. Autonomy boundary (critical)

Where the agent sits on the analyze → advise → **act** spectrum defines the entire risk surface:

| Level | Behavior | Governance weight |
|-------|----------|-------------------|
| Analyze | Summarize, explain, retrieve | Lower |
| Advise | Recommend signals/trades, human decides | Medium |
| **Act** | Places orders autonomously | **Maximum** — full SR 11-7 + execution controls, kill-switch |

> **Open decision (for you):** does Cortex's LLM ever *act*, or strictly analyze/advise? This is open decision #3 in the [README](./README.md) and gates [`09`](./09-governance-and-compliance.md). **Recommend starting analyze/advise-only**; promote to execution only behind hard controls and explicit sign-off.
