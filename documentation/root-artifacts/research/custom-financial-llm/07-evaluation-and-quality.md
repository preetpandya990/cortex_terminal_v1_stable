# 07 — Evaluation & Quality
> Date: 2026-06-01 | Covers: benchmarks, custom eval harness, acceptance gates, regression

**Build the eval harness first — before selecting a base, before any training.** It is the instrument that converts every later decision from opinion into evidence. In a regulated financial product, the eval harness is also a governance artifact (SR 11-7 validation).

---

## 1. Two evaluation tracks

| Track | Purpose | Built from |
|-------|---------|------------|
| **Public benchmarks** | Sanity vs the field; base-model shortlisting | Standard financial suites (§2) |
| **Custom gold eval** | The decision authority for *your* product | 100–500 curated Q&A reflecting real Cortex use |

> The custom eval is non-negotiable: **100–500 curated question/answer pairs that mirror actual production use.** Public scores never substitute for it.

---

## 2. Public financial benchmarks (2026)

| Benchmark | Tests |
|-----------|-------|
| **FinBen** | Holistic suite — extraction, reasoning, decision support (powered the FinNLP/IJCAI shared task) |
| **FLARE / FLaME** | Open FinLLM-leaderboard tasks |
| **FinQA / ConvFinQA / TAT-QA** | Numerical reasoning over financial tables & hybrid text |
| **FinanceBench** | Real-world financial QA |
| **FINESSE-Bench** (2026) | 8 hierarchical suites, 3,993 Qs — knowledge → technical analysis |
| **FinMMEval** (CLEF 2026) | First multilingual + multimodal financial eval |

Use these to **shortlist bases** and to detect regressions in general financial competence after training.

---

## 3. The custom gold eval (your decision authority)

Curate, with expert review, sets covering Cortex's real tasks:
- Signal explanation & rationale quality
- Numerical reasoning over your fundamentals/ratios (verifiable answers)
- Sentiment & news interpretation
- Retrieval faithfulness (does the answer match the cited source?)
- **Safety/refusals** (no unlicensed advice, MNPI handling, disclaimer presence)
- **Freshness** (is the as-of timestamp correct and surfaced?)

Scoring: prefer **verifiable/programmatic** checks where possible (numeric correctness, citation match), plus **LLM-as-judge** for open-ended quality, plus periodic **human expert** review on a sample.

---

## 4. Acceptance gates (promotion criteria)

No model version promotes without passing, recorded in MLflow:

| Gate | Threshold (set with domain experts) |
|------|-------------------------------------|
| Custom gold eval | ≥ target score **and** beats the current production model |
| Numerical faithfulness | Verified-answer accuracy ≥ threshold; **0** silent math errors |
| Retrieval faithfulness | Citation-match rate ≥ threshold; factual-error rate below cap |
| Safety/compliance | 100% on refusal/disclaimer/MNPI test set |
| Regression | No drop on public financial benchmarks beyond tolerance |
| Latency/cost | Within the serving budget for its class ([`08`](./08-serving-and-infra.md)) |

> **Decision: promotion is gate-driven and automated in CI. A model that fails any gate does not ship — period.**

---

## 5. Continuous & production evaluation

- **Regression suite** runs on every training run and every retrieval-index change.
- **Online monitoring**: hallucination rate, retrieval-failure rate, refusal rate, latency, drift — wired to your existing Prometheus/Grafana stack.
- **Hallucination control is a compliance requirement, not a quality preference** — track it as a first-class production SLO.
