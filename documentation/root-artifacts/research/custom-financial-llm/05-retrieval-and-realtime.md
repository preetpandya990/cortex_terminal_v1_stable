# 05 — Retrieval & Real-Time Data (Finance RAG)
> Date: 2026-06-01 | Covers: finance-aware RAG, table/numeric handling, GraphRAG, live feeds

Retrieval is the **knowledge layer** — where current market truth enters the model. For finance it is also the highest-leverage and most error-prone layer. Industry consensus in 2026: **when RAG fails, the failure is in retrieval, not generation.**

---

## 1. Why generic RAG fails on financial data

| Failure mode | Evidence | Fix |
|--------------|----------|-----|
| **Naive chunking destroys tables** | Accuracy drops **0.91 (text) → 0.44 (tabular reasoning)** | Table-aware / structural chunking |
| **Semantic similarity can't rank numbers** | Cosine over price/time-series *degrades* forecasting | Don't retrieve numbers as text — use tools/structured stores |
| **Generic chunking hallucinates facts** | **14.7%** factual-error rate on financial queries | Structure-preserving chunks + provenance |
| **Flat vector search misses relationships** | Lower precision on entity-linked queries | **GraphRAG** (precision reported up to **99%**) |

> **Decision: do not ship a generic text-RAG. Finance RAG is a specialized build — table-aware, graph-augmented, with numbers handled structurally.**

---

## 2. Reference architecture (three layers)

```
INGESTION  →  real-time quotes · news · filings · fundamentals · transcripts
              standardized + timestamped + provenance-tagged
   │
RETRIEVAL  →  ┌ vector store (semantic text)        ┐
              ├ knowledge graph (tickers↔filings↔    │  hybrid
              │   sectors↔people — GraphRAG)         │  retrieval
              ├ structured store (prices, ratios,    │
              │   time-series → SQL/feature store)   ┘
   │
GENERATION →  grounded answer + citations + freshness stamp + guardrails
```

- **Hybrid retrieval** (vector + keyword/BM25 + graph) outperforms any single method on financial queries.
- **Numbers and time-series live in structured stores** (your existing DB / feature store), reached by **tools**, not embedded as prose. The LLM *calls* for `P/E(TICKER, date)`; it does not "remember" it.

---

## 3. Table-aware & structure-preserving chunking

- Detect and keep tables intact; chunk on document structure (sections, statements), not fixed token windows.
- Attach metadata to every chunk: source, **as-of timestamp**, instrument/ticker, statement type, license tag.
- Tables → either rendered as markdown *with* a structured sidecar, or routed to a text-to-SQL tool for numeric questions.

---

## 4. GraphRAG for finance

Build a knowledge graph over financial entities — **tickers, companies, filings, sectors, people, events, relationships** — and combine graph traversal with vector search. This is what lifts precision toward ~99% on relationship-heavy queries ("who supplies X", "peers of Y", "events affecting Z's sector"). Cortex's fundamentals/competitors data (you already ingest Upstox `/competitors`, share-holdings, corporate-actions) is a ready-made graph seed.

---

## 5. Real-time / freshness

- **Continuous ingestion** via connectors: market-data feeds (real-time prices, volatility, volume), news, filing wires.
- Every retrieved fact carries an **as-of timestamp**; the model must surface it ("as of 14:32 IST"). Stale data in finance is a correctness *and* compliance failure.
- Reuse Cortex's existing market-feed/WebSocket infrastructure as the live source; the RAG layer subscribes rather than re-implements.

---

## 6. Why this beats baking knowledge into weights (again)

Retrieval gives you: instant updates, per-fact citations, auditable provenance, and access control — all mandatory in finance and **none** available from a from-scratch model with frozen weights. This is the operational core of the "adapt, don't originate" thesis in [`01`](./01-approach-and-decision.md).

> **Decision: knowledge = retrieval + tools, always timestamped and cited. The trained model supplies reasoning and language; the retrieval layer supplies truth.**
