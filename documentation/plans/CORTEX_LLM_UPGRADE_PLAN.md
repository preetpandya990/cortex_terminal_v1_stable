# Cortex Intelligence Layer — LLM Upgrade Plan
> **Version:** 1.0 | **Date:** 2026-06-03 | **Owner:** Het Trivedi
> **Classification:** Internal Engineering Specification
> **Status:** Pre-Implementation — Approved for Execution

---

## 0. Preamble

This document is the single authoritative plan for upgrading Cortex's NLP and intelligence layer from a FinBERT-only pipeline to a production-grade, provider-agnostic, retrieval-grounded LLM system. It covers:

- Pre-conditions and system state remediation required before work begins
- Architecture decisions, technology choices with justification and evidence
- A phased implementation plan with concrete deliverables, DB/API/frontend change inventory, and acceptance gates
- Risk register and governance obligations

All recommendations are grounded in 2026 state-of-the-art research and the specific constraints of the Cortex codebase. No shortcuts. No over-engineering. Every layer is justified by evidence and unlocked only when the previous phase passes its gate.

---

## 1. Executive Summary

Cortex currently uses **FinBERT** (ProsusAI/finbert, PyTorch inference) as its sole NLP engine. FinBERT scores news headlines on a three-class sentiment scale and writes a single float to `ai_nlp_results.sentiment_score`. These scores feed five ML features used by the ensemble model, and the same service drives the Sentiment Analysis Card in the Hawk Eye Radar UI. FinBERT has no capacity for natural-language explanation, context-aware reasoning across multiple news items, or producing human-readable trade rationale.

**This upgrade replaces and extends FinBERT with a layered LLM system** that:

1. Produces the same structured sentiment output (backward-compatible with ML features, same DB schema)
2. Reads full article bodies instead of 300-character title truncations
3. Generates plain-English trade explanations — a compact summary on the `TradeSuggestionCard` and a full narrative in the AI Analysis Cards
4. Operates through a **provider-agnostic client** (NVIDIA NIM primary, Ollama fallback) so the system remains operational throughout development
5. Is grounded by a **finance-aware RAG pipeline** over Cortex's existing news/events data — no general financial knowledge base needed

The upgrade is staged across four phases. Phase 0 (this document's primary focus) delivers a working, production-scaffolded system on a hosted base model with no training. Later phases add fine-tuning, reasoning alignment, and multi-agent orchestration — each gated on measured evidence from the previous phase.

---

## 2. System State Assessment

### 2.1 Current Architecture

```
RSS/News Feeds
     │
     ▼
ai_raw_events (AIRawEvent)
     │
     ▼
ai_processed_events (AIProcessedEvent)
     │
     ▼
NLPEngine (FinBERT PyTorch)  ←── reads title only (≤300 chars)
     │
     ▼
ai_nlp_results (AINLPResult)
 - sentiment_score  Numeric(5,2)  [-1.0, +1.0]
 - sentiment_label  String(20)    "positive"|"negative"|"neutral"
 - model_used       String(50)    "finbert-pt-gpu"
 - confidence_score Numeric(5,2)  [0.0, 1.0]
     │
     ▼
ai_event_classifications (AIEventClassification)
 - event_type
 - impact_score        Numeric(5,2)
 - affected_symbols    ARRAY(String)
 - reasoning           Text          ← exists but populated minimally
     │
     ├─▶ SentimentFeatureExtractor → 5 ML features → Ensemble model
     │
     └─▶ SentimentAnalysisService → sentiment card (SSE stream)

OllamaClient (llm_client.py)
 - Singleton, Ollama-specific, no provider abstraction
 - Used by: event_classifier.py, fake_news_detector.py
 - Config: OLLAMA_BASE_URL, OLLAMA_MODEL (llama3.1:8b), OLLAMA_TIMEOUT

TradeSuggestionCard (frontend)
 - Displays: ticker, direction, confidence, consensus, price grid, expiry
 - No explanation field anywhere in backend schema or frontend component
```

### 2.2 Known Issues and Gaps

| ID | Location | Issue | Severity |
|----|----------|-------|----------|
| **G1** | `nlp_engine.py:181` | FinBERT reads title only (`[:300]`), not full article body | High — degrades sentiment quality |
| **G2** | `llm_client.py` | OllamaClient is Ollama-specific; no provider fallback | High — system inoperable if Ollama is down |
| **G3** | `trade_suggestions` table | No explanation/narrative field exists | High — feature blocker |
| **G4** | DB migrations | pgvector extension not installed | High — RAG pipeline blocker |
| **G5** | `TradeSuggestionCard.tsx` | No `llm_summary` field rendered | Medium — UI blocker |
| **G6** | `ai_stream.py` | SSE analysis_update has no explanation payload | Medium — AI Analysis Card blocker |
| **G7** | `nlp_engine.py` | FinBERT loaded at startup, consuming 4GB GPU VRAM shared with GRU training (Audit R9) | Medium — hardware contention |
| **G8** | `ai_nlp_results` | `model_used` String(50) — must hold new LLM model identifiers | Low — verify length fits |

### 2.3 Outstanding Audit Items Affected by This Work

| Audit Ref | Status | Interaction |
|-----------|--------|-------------|
| **R2** — CI pipeline | Deferred (user decision) | LLM code ships without automated CI; manual test runs required |
| **R9** — GPU contention | Pending | Migrating FinBERT to NIM frees GPU VRAM at startup; R9 is substantially resolved by Phase 0 completion |
| **R10** — Latency benchmarks | Pending | Phase 0 establishes Grafana SLOs for the LLM pipeline, providing the baseline R10 requires |
| **R12** — Frontend typing | Pending | New `llm_summary` and explanation fields must conform to correct TypeScript types from day one |

---

## 3. Pre-Conditions (Must Be True Before Any Code is Written)

The following items must be resolved before Phase 0 begins. They are not features — they are prerequisites.

### PC-1: NVIDIA NIM API Key
**Action:** Sign up at `build.nvidia.com`, generate an API key (prefix `nvapi-`).
**Add to environment:** `NVIDIA_NIM_API_KEY=nvapi-...`
**Add to `config.py`:**
```python
NVIDIA_NIM_API_KEY: str | None = None
NVIDIA_NIM_BASE_URL: str = "https://integrate.api.nvidia.com/v1"
NVIDIA_NIM_MODEL: str = "qwen/qwen3.6-235b-a22b"  # confirmed on build.nvidia.com
NVIDIA_NIM_EMBED_MODEL: str = "nvidia/nv-embedqa-e5-v5"
```
**Free tier limits:** 1,000 inference credits on signup; 40 requests/minute. Sufficient for dev-pace RAG and explanation generation.

### PC-2: pgvector Extension
**Action:** Add Alembic migration `0041_add_pgvector.py` that runs:
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```
Postgres must have the `vector` extension available in its binary. Verify: `SELECT * FROM pg_available_extensions WHERE name = 'vector';` If not present, install `postgresql-16-pgvector` (or equivalent for your Postgres version) on the host before running the migration.

### PC-3: Python Dependencies
**Add to `requirements.in`:**
```
litellm>=1.40.0          # provider-agnostic LLM client
instructor>=1.5.0        # structured output via Pydantic
pgvector>=0.3.0          # SQLAlchemy + psycopg2 vector type support
llama-index-core>=0.12.0 # RAG ingestion pipeline
llama-index-vector-stores-postgres>=0.4.0
rank-bm25>=0.2.2         # BM25 for hybrid retrieval
```
**Regenerate lockfile:** `make lock`

### PC-4: Verify `model_used` Field Capacity
`AINLPResult.model_used` is `String(50)`. Longest anticipated value: `nim-qwen3.6-35b-a3b` (20 chars). **No migration needed.** Verify before choosing a model with a longer identifier.

---

## 4. Architecture — Target State

### 4.1 Design Principles

1. **Schema compatibility first.** The LLM must write to `AINLPResult` using the same field names and numeric scale as FinBERT. The ML feature pipeline reads from these fields; any scale change silently degrades ML predictions. No ML retraining happens until Phase 1.
2. **Provider agnosticism with explicit logging.** Every inference call logs which backend served it. Silent fallback is prohibited — the system must always know which model produced which output.
3. **Explanation never blocks suggestions.** Trade suggestions are created and returned immediately. LLM explanation is generated asynchronously and delivered via SSE when ready. A failed explanation never prevents a suggestion from being seen.
4. **RAG is symbol-scoped and timestamped.** There is no general financial knowledge base. Retrieval is restricted to news/events for the specific symbol within a time window. Every retrieved fact carries an `as_of` timestamp and a source reference.
5. **Audit trail is a first-class schema citizen.** Every LLM inference is logged to `ai_llm_audit_log` with the prompt hash, retrieved source IDs, model+version, output, latency, and token count. This is a launch blocker, not a post-launch addition.
6. **Dual-write during FinBERT migration.** During the transition, both FinBERT and the LLM score each event. This produces a comparison dataset that validates score calibration before FinBERT is removed.

### 4.2 Target Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  CORTEX INTELLIGENCE LAYER — POST-UPGRADE                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  NEWS / RSS FEEDS                                                            │
│       │                                                                      │
│       ▼                                                                      │
│  ai_raw_events → ai_processed_events                                        │
│       │                                                                      │
│       ▼                                                                      │
│  ┌────────────────────────────────────┐                                      │
│  │  INTELLIGENCE CLIENT (LiteLLM)     │                                      │
│  │  Primary:  NVIDIA NIM              │  ← structured output via Instructor  │
│  │  Fallback: Ollama (llama3.1:8b)   │                                      │
│  │  Logs:     ai_llm_audit_log        │                                      │
│  └────────────────────────────────────┘                                      │
│       │                                                                      │
│       ├─▶ SENTIMENT SCORING ──────────────────────────────────────────────┐ │
│       │    - full article body (not title only)                            │ │
│       │    - writes: AINLPResult.sentiment_score  [-1.0, +1.0]            │ │
│       │    - writes: AINLPResult.sentiment_label                          │ │
│       │    - writes: AINLPResult.confidence_score                         │ │
│       │    - writes: AINLPResult.model_used  (e.g. "nim-qwen3.6-35b")    │ │
│       │                   │                                                │ │
│       │                   ▼                                                │ │
│       │         SentimentFeatureExtractor → 5 ML features → Ensemble      │ │
│       │                                                                    │ │
│       └─▶ EVENT CLASSIFICATION ─────────────────────────────────────────┐ │ │
│            - enriches: AIEventClassification.reasoning  (Text)           │ │ │
│            - enriches: AIEventClassification.impact_score                │ │ │
│                                                                           │ │ │
│  RAG PIPELINE (LlamaIndex)                                                │ │ │
│  ┌────────────────────────────────────────────────────────────┐           │ │ │
│  │  INGESTION                                                  │           │ │ │
│  │  ai_raw_events → chunk (structure-preserving, 512 tokens)  │           │ │ │
│  │  → embed (nvidia/nv-embedqa-e5-v5, 1024-dim)               │           │ │ │
│  │  → store (pgvector, HNSW index, symbol+time metadata)      │           │ │ │
│  │                                                             │           │ │ │
│  │  RETRIEVAL (hybrid)                                         │           │ │ │
│  │  query → BM25 + vector search → RRF fusion → top-k chunks  │           │ │ │
│  │  → rerank → inject into LLM context with as_of timestamps  │           │ │ │
│  └────────────────────────────────────────────────────────────┘           │ │ │
│                                                                            │ │ │
│  EXPLANATION PIPELINE (async)                                              │ │ │
│  ┌─────────────────────────────────────────────────────────────┐          │ │ │
│  │  Trigger: trade suggestion created → Redis pub              │          │ │ │
│  │  Worker:  fetch suggestion + RAG-retrieved news             │          │ │ │
│  │           → LLM (NIM) → {summary (2-3 sentences),          │          │ │ │
│  │                           full_explanation (narrative)}     │          │ │ │
│  │  Write:   ai_trade_suggestions.llm_summary                  │          │ │ │
│  │           ai_trade_suggestions.llm_explanation              │          │ │ │
│  │  Publish: cortex:llm:explanation:ready:{suggestion_id}      │          │ │ │
│  └─────────────────────────────────────────────────────────────┘          │ │ │
│                                                                            │ │ │
│  SSE STREAM (ai_stream.py)                                      ◄─────────┘ │ │
│  - analysis_update now carries: prediction + pattern + sentiment + explanation  │
│                                                                              │
│  FRONTEND                                                                    │
│  - TradeSuggestionCard: llm_summary (2-3 sentences, skeleton while null)    │
│  - AI Analysis Card: full llm_explanation (narrative, sourced, timestamped) │
│                                                                              │
│  OBSERVABILITY (Prometheus + Grafana — existing stack)                      │
│  - LLM TTFT p50/p95/p99, tokens/request, cost/request, error rate          │
│  - RAG retrieval latency, k-recall, embedding queue depth                   │
│  - Explanation generation latency, success rate, fallback rate              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 4.3 Technology Choices — Justified

| Concern | Choice | Evidence / Reasoning |
|---------|--------|---------------------|
| **Provider abstraction** | **LiteLLM** | Supports 100+ providers (NIM, Ollama, vLLM, OpenAI) via unified interface. Built-in fallback chains, cost tracking, rate-limit handling. Powers 1B+ production requests. At Cortex's request volume, Python GIL is not a constraint — the bottleneck is network I/O to NIM. |
| **Structured output** | **Instructor** | Pydantic-based, works with any OpenAI-compatible API including NIM. Production-ready at this throughput class. Simpler than grammar-guided constrained decoding (XGrammar/Outlines) which is only warranted at vLLM-served volumes. |
| **Vector store** | **pgvector** (existing Postgres) | May 2025 benchmarks: pgvector + pgvectorscale achieves 471 QPS at 99% recall on 50M vectors vs. Qdrant's 41 QPS at the same recall threshold. Cortex's news corpus is well under 10M vectors. Zero new infra — same Postgres instance. HNSW index for ANN search. |
| **RAG framework** | **LlamaIndex** (ingestion) | Achieved 92% retrieval accuracy on financial documents in 2025 benchmarks due to structure-preserving parsers. Outperforms LangChain for pure retrieval. |
| **Agentic orchestration** | **LangGraph** (Phase 2) | Top recommendation for agentic RAG in 2026 — checkpointing, human-in-the-loop controls, DAG-native. Not needed in Phase 0/1. |
| **Hybrid retrieval** | BM25 + vector + **RRF** | Universal production pattern for finance RAG. BM25 captures exact tickers and financial terms; vectors handle semantics. RRF merges non-comparable scores without tuning parameters. |
| **Embedding model** | `nvidia/nv-embedqa-e5-v5` (1024-dim) | Finance-adapted embeddings outperform general models by 18-23% recall on FinMTEB benchmark. Available on NVIDIA NIM (same API key). QA-optimized. |
| **Primary LLM** | **NVIDIA NIM** (Qwen3 family) | Free tier (40 RPM, 1K credits), OpenAI-compatible endpoint, no infra. Sufficient for dev-pace explanation generation and sentiment scoring. |
| **LLM fallback** | **Ollama** (existing) | Already integrated, zero-cost local fallback. Operationally validates the provider-agnostic client without cloud dependency. |
| **Inference tracing** | New `ai_llm_audit_log` table | SR 11-7 compliance requires full reproducibility: prompt hash, model version, retrieved source IDs, output, latency, guardrail events. Table is immutable (append-only). |

---

## 5. Database Changes

All schema changes require Alembic migrations. Each migration is a discrete, reversible unit. No DDL runs outside of migrations.

### 5.1 Migration 0041 — pgvector Extension + Document Embedding Store

```sql
-- Enable vector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Document embedding store (for RAG)
CREATE TABLE ai_document_embeddings (
    id                 BIGSERIAL PRIMARY KEY,
    source_table       VARCHAR(50)  NOT NULL,          -- 'ai_raw_events'
    source_id          BIGINT       NOT NULL,
    symbol             VARCHAR(20),                    -- NULL = general market
    content_hash       VARCHAR(64)  NOT NULL,
    content_preview    TEXT,                           -- first 200 chars (debug/audit)
    embedding          VECTOR(1024) NOT NULL,
    as_of_timestamp    TIMESTAMPTZ  NOT NULL,
    metadata           JSONB,                          -- source_name, source_url, event_type
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    UNIQUE (source_table, source_id)
);

-- HNSW index for approximate nearest-neighbour search
-- m=16, ef_construction=64: standard production settings for recall/speed balance
CREATE INDEX idx_ai_doc_embeddings_hnsw
    ON ai_document_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Filtered retrieval by symbol (B-tree on symbol + created_at)
CREATE INDEX idx_ai_doc_embeddings_symbol_time
    ON ai_document_embeddings (symbol, as_of_timestamp DESC);
```

### 5.2 Migration 0042 — LLM Explanation Fields on Trade Suggestions

```sql
-- Brief summary for TradeSuggestionCard inline display
ALTER TABLE ai_trade_suggestions
    ADD COLUMN llm_summary           TEXT,
    ADD COLUMN llm_explanation       TEXT,            -- full narrative for analysis card
    ADD COLUMN explanation_model     VARCHAR(100),    -- model that generated it
    ADD COLUMN explanation_generated_at TIMESTAMPTZ;

-- Partial index: allows efficient "where explanation is still pending"
CREATE INDEX idx_suggestions_explanation_pending
    ON ai_trade_suggestions (created_at DESC)
    WHERE llm_summary IS NULL AND status = 'active';
```

### 5.3 Migration 0043 — LLM Audit Log

```sql
CREATE TABLE ai_llm_audit_log (
    id                 BIGSERIAL PRIMARY KEY,
    invocation_id      UUID         NOT NULL DEFAULT gen_random_uuid(),
    invocation_type    VARCHAR(50)  NOT NULL,   -- 'sentiment', 'explanation', 'classification'
    reference_id       BIGINT,                  -- suggestion_id, nlp_result_id, etc.
    model_provider     VARCHAR(50)  NOT NULL,   -- 'nim', 'ollama'
    model_id           VARCHAR(100) NOT NULL,   -- e.g. 'qwen/qwen3.6-235b-a22b'
    prompt_hash        VARCHAR(64)  NOT NULL,   -- SHA-256 of rendered prompt
    retrieved_source_ids JSONB,                 -- [{table, id, as_of}]
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    latency_ms         INTEGER,
    guardrail_events   JSONB,                   -- list of triggered guardrails
    output_preview     TEXT,                    -- first 500 chars of output
    error_message      TEXT,                    -- NULL if successful
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Append-only: no updates, no deletes (enforced at application layer)
-- Partition by month if volume exceeds 10M rows/month

CREATE INDEX idx_llm_audit_invocation ON ai_llm_audit_log (invocation_id);
CREATE INDEX idx_llm_audit_type_time  ON ai_llm_audit_log (invocation_type, created_at DESC);
```

### 5.4 `AINLPResult` — No Schema Change Required

The existing schema is compatible:
- `sentiment_score Numeric(5,2)` → LLM outputs float in `[-1.0, +1.0]` ✓
- `sentiment_label String(20)` → "positive" | "negative" | "neutral" ✓
- `confidence_score Numeric(5,2)` → LLM outputs confidence in `[0.0, 1.0]` ✓
- `model_used String(50)` → "nim-qwen3.6-35b" (14 chars) ✓

The ML feature pipeline (`SentimentFeatureExtractor`) reads these fields unchanged.

---

## 6. Backend Changes — File-by-File

### 6.1 New: `backend/app/ai/intelligence/llm_client.py` (full replacement)

Replace the existing `OllamaClient` with a `CortexIntelligenceClient` backed by **LiteLLM**. The existing `get_ollama_client()` function is retained as a compatibility alias pointing to the new client.

**Key design:**
```python
# Provider resolution order (from config):
# 1. NVIDIA NIM  — if NVIDIA_NIM_API_KEY is set and model is reachable
# 2. Ollama      — fallback, always available locally

# Startup log (mandatory — never silent):
# "LLM backend: nim/qwen3.6-35b-a3b [primary]"
# "LLM backend: ollama/llama3.1:8b [fallback — NIM unreachable]"
```

**Interface (backward-compatible):**
- `async generate(prompt, system, temperature, max_tokens) -> dict`
- `async generate_structured(prompt, response_model: Type[BaseModel], system, temperature) -> BaseModel`  ← new
- `async embed(texts: list[str]) -> list[list[float]]`  ← new
- `async health_check() -> dict[str, str]`  ← returns `{"provider": "nim", "model": "..."}`

**Callers that must not break:**
- `backend/app/ai/intelligence/event_classifier.py`
- `backend/app/ai/intelligence/fake_news_detector.py`
These call `generate()` — the interface is unchanged.

### 6.2 New: `backend/app/ai/intelligence/nlp_engine.py` (full replacement)

Remove FinBERT PyTorch. Replace with LLM-backed sentiment analysis.

**Phase 0 design (dual-write):**
```python
class NLPEngine:
    async def analyze_sentiment(self, text: str) -> dict:
        # Calls LLM via CortexIntelligenceClient
        # Structured output: SentimentOutput(label, score, confidence)
        # ALSO runs FinBERT in background thread (dual-write comparison)
        # Logs both to ai_llm_audit_log with invocation_type='sentiment_compare'
        # Returns LLM output; FinBERT output stored for calibration analysis
        ...

    async def process_event(self, db, processed_event_id, content):
        # Now passes FULL content to analyze_sentiment (not title-truncated)
        # Writes AINLPResult with same field names, same numeric scale
        ...
```

**Phase 0 exit:** When score calibration analysis confirms LLM scores are statistically comparable to FinBERT (Pearson r ≥ 0.80 on the comparison dataset, or human eval confirms LLM is superior), FinBERT dual-write is disabled. FinBERT model is no longer loaded at startup — **R9 GPU contention is resolved.**

### 6.3 New: `backend/app/ai/rag/` (new module)

```
backend/app/ai/rag/
├── __init__.py
├── embedder.py          # Embedding calls via CortexIntelligenceClient.embed()
├── ingester.py          # LlamaIndex-based ingestion: chunk → embed → pgvector upsert
├── retriever.py         # Hybrid retrieval: BM25 + vector → RRF → rerank → top-k
└── pipeline.py          # RAG pipeline: query string + symbol + time window → context string
```

**`retriever.py` design:**
```python
async def retrieve(
    query: str,
    symbol: str,
    window_hours: int = 24,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    # 1. BM25 over recent events for symbol (rank_bm25)
    # 2. Vector similarity search on pgvector HNSW index
    # 3. RRF score fusion
    # 4. Return top-k with: content, source_name, as_of_timestamp, source_url
```

Every returned chunk carries `as_of_timestamp` and `source_name`. These are injected verbatim into the LLM prompt context.

### 6.4 New: `backend/app/ai/intelligence/explanation_worker.py`

Async background worker that:
1. Subscribes to Redis channel `cortex:llm:explanation:pending`
2. For each message `{suggestion_id}`:
   a. Loads suggestion from DB
   b. Calls RAG pipeline: `retrieve(query=f"trade signal {symbol} {direction}", symbol=symbol, window_hours=24)`
   c. Builds prompt: ML signal data + retrieved news context
   d. Calls `CortexIntelligenceClient.generate_structured()` → `ExplanationOutput(summary, full_explanation)`
   e. Writes `llm_summary` and `llm_explanation` to `ai_trade_suggestions`
   f. Writes to `ai_llm_audit_log`
   g. Publishes to `cortex:llm:explanation:ready:{suggestion_id}`
3. On failure: writes error to audit log; does not retry more than 2 times; publishes a `failed` event

**Registered in `main.py` lifespan** alongside the existing `cai_redis_listener` task.

### 6.5 Modified: `backend/app/api/v1/trade_suggestions.py`

After creating a trade suggestion, publish to `cortex:llm:explanation:pending`:
```python
await redis.publish("cortex:llm:explanation:pending", suggestion_id)
```

### 6.6 Modified: `backend/app/schemas/trade_suggestions.py`

Add to `TradeSuggestionResponse`:
```python
llm_summary:              str | None = Field(None, description="LLM-generated 2-3 sentence plain-English summary")
llm_explanation:          str | None = Field(None, description="Full LLM narrative explanation (for Analysis Cards)")
explanation_model:        str | None = Field(None, description="Model that generated the explanation")
explanation_generated_at: datetime | None = Field(None, description="When explanation was generated")
```

### 6.7 Modified: `backend/app/api/v1/ai_stream.py`

The SSE `analysis_update` event is assembled from `prediction + pattern + sentiment`. Add a fourth component:

```python
_EXPLANATION_REFRESH_SECS = 300  # refresh every 5 minutes

# In event_generator():
# - Subscribe to cortex:llm:explanation:ready:{instrument_key_based_suggestion}
# - On ready event: emit updated analysis_update immediately (not waiting for next refresh cycle)
# - Include explanation data in the combined payload:
payload_dict = {
    "prediction":   prediction_data,
    "pattern":      pattern_data,
    "sentiment":    sentiment_data,
    "explanation":  explanation_data,   # {summary, full_explanation, model, generated_at, sources}
    "instrument_key": instrument_key,
    "emitted_at":   datetime.now(timezone.utc).isoformat(),
}
```

The `sources` field in `explanation_data` lists the news articles used (title, source_name, as_of_timestamp, source_url) — directly visible to the user in the Analysis Card for full transparency.

### 6.8 Modified: `backend/app/core/config.py`

```python
# LLM Provider Configuration
NVIDIA_NIM_API_KEY:     str | None = None
NVIDIA_NIM_BASE_URL:    str        = "https://integrate.api.nvidia.com/v1"
NVIDIA_NIM_MODEL:       str        = "qwen/qwen3.6-235b-a22b"
NVIDIA_NIM_EMBED_MODEL: str        = "nvidia/nv-embedqa-e5-v5"

# Ollama (fallback — existing settings preserved)
OLLAMA_BASE_URL:        str = "http://localhost:11434"
OLLAMA_MODEL:           str = "llama3.1:8b"
OLLAMA_TIMEOUT:         int = Field(30, ge=5, le=120)

# LLM Behaviour
LLM_MAX_RETRIES:        int   = Field(3, ge=1, le=5)
LLM_REQUEST_TIMEOUT:    float = Field(30.0, ge=5.0, le=120.0)

# RAG Configuration
RAG_TOP_K:              int = Field(5, ge=1, le=20)
RAG_WINDOW_HOURS:       int = Field(24, ge=1, le=168)
RAG_EMBED_BATCH_SIZE:   int = Field(32, ge=1, le=128)
```

---

## 7. Frontend Changes

### 7.1 TypeScript Type Update: `TradeSuggestion`

Locate and update the `TradeSuggestion` interface (likely in `frontend/src/types/trade_suggestions.ts`):

```typescript
export interface TradeSuggestion {
  // ... existing fields unchanged ...

  // LLM Explanation (nullable — populated asynchronously)
  llm_summary:              string | null;
  llm_explanation:          string | null;
  explanation_model:        string | null;
  explanation_generated_at: string | null;
}
```

### 7.2 Modified: `TradeSuggestionCard.tsx`

Add an `LLMSummarySection` component below `<PriceGrid />` inside `<CardContent>`:

```tsx
function LLMSummarySection({ summary }: { summary: string | null }) {
  if (summary === undefined) return null;  // field not yet in API response

  if (summary === null) {
    return (
      <div className="space-y-1.5 pt-1">
        <div className="h-3 w-full animate-pulse rounded bg-slate-100" />
        <div className="h-3 w-4/5 animate-pulse rounded bg-slate-100" />
      </div>
    );
  }

  return (
    <p className="text-xs text-slate-600 leading-relaxed pt-1 border-t border-slate-100">
      {summary}
    </p>
  );
}
```

**Behaviour:**
- `null` → loading skeleton (explanation is being generated)
- Non-null string → renders the 2-3 sentence summary
- The skeleton auto-disappears when the parent component receives a refreshed suggestion with `llm_summary` populated

### 7.3 Modified: AI Analysis Cards (SSE Consumer)

The AI Analysis Cards are driven by the `analysis_update` SSE event. When the `explanation` payload arrives:

- **Sentiment Card** (existing): retains its current behaviour unchanged
- **New Explanation Panel** (within the Analysis Cards section): renders `full_explanation` as a readable narrative with a source citation list at the bottom

```tsx
// Explanation panel within the Analysis Cards
function ExplanationPanel({ explanation }: { explanation: ExplanationData | null }) {
  if (!explanation) {
    return <ExplanationSkeleton />;
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-700 leading-relaxed">
        {explanation.full_explanation}
      </p>
      <div className="text-xs text-slate-400 space-y-0.5">
        {explanation.sources.map((src) => (
          <div key={src.source_url} className="flex items-center gap-1">
            <span className="font-medium">{src.source_name}</span>
            <span>·</span>
            <span>{formatRelativeTime(src.as_of_timestamp)}</span>
          </div>
        ))}
      </div>
      <p className="text-[10px] text-slate-300">
        Generated by {explanation.model} · {formatRelativeTime(explanation.generated_at)}
      </p>
    </div>
  );
}
```

---

## 8. Guardrails and Governance (Phase 0 Minimum Bar)

These are not optional. A system that produces financial analysis without these controls does not ship.

### 8.1 Output Guardrails (applied to every LLM response)

| Guardrail | Implementation | Trigger |
|-----------|---------------|---------|
| **Disclaimer injection** | Append to every `full_explanation`: *"This is AI-generated analysis for informational purposes only and does not constitute financial advice."* | Always |
| **No price predictions** | Output validation: reject responses containing phrases like "will reach ₹X", "price target", "guaranteed return" | Programmatic regex + LLM self-check |
| **Citation requirement** | Every factual claim in `full_explanation` must be attributable to a retrieved source. Explanations without citations are rejected and flagged in audit log. | Structural validation |
| **Hallucination circuit breaker** | If the LLM output references a company, event, or figure not present in the retrieved context, the response is demoted to `llm_summary = null` and the failure is logged. | RAG context cross-check |
| **Numeric cross-check** | Any numbers in the explanation (price, P/E, ratio) must match the structured ML signal data passed in the prompt. | Programmatic |

### 8.2 Prompt Design (non-negotiable constraints)

The system prompt for every LLM call must:
1. State that the model is an analysis tool for the Cortex trading platform, not a licensed financial advisor
2. Instruct the model to base all claims strictly on the provided context
3. Require citations inline (e.g., "According to [Economic Times, 2026-06-03]...")
4. Prohibit speculative or advisory language ("you should buy", "this will rise")
5. Specify the exact output format (JSON with `summary`, `full_explanation`, `sources_used`)

### 8.3 `ai_llm_audit_log` — Operational Requirement

Every LLM inference — including failures — writes a row. The audit log enables:
- **Complaint resolution:** given a user query, reproduce the exact prompt, model, context, and output
- **Drift detection:** compare score distributions across model versions over time
- **Cost tracking:** token counts per invocation_type, per model
- **Guardrail monitoring:** query `WHERE guardrail_events != '[]'` to see all triggered guardrails

**Access control:** audit log is read-only to application code. Only migrations can alter its schema.

### 8.4 Grafana SLOs (add to existing stack on Day 1)

| SLO | Target | Alert threshold |
|-----|--------|----------------|
| Explanation generation latency p95 | < 5s | > 8s |
| Explanation success rate | ≥ 95% | < 90% |
| NIM fallback rate (to Ollama) | < 5% | > 15% |
| Sentiment scoring latency p95 | < 3s | > 5s |
| RAG retrieval latency p95 | < 500ms | > 1s |
| Audit log write success rate | 100% | < 99.9% |

---

## 9. Eval Harness — Phase 0 Gate Instrument

**Build this first.** The eval harness is the measurement instrument. Without it, every quality claim is opinion. With it, Phase 0 → Phase 1 → Phase 2 transitions are evidence-driven.

### 9.1 Gold Set Structure (50 Questions Minimum)

| Category | Count | Judge |
|----------|-------|-------|
| Signal explanation quality | 15 | Human expert review (score 1-5) |
| Sentiment label accuracy | 10 | Programmatic (label must match ground truth) |
| Sentiment score calibration vs FinBERT | 10 | Programmatic (Pearson r ≥ 0.80) |
| News narrative faithfulness | 10 | Retrieval cross-check (every claim in context) |
| Safety / refusal / disclaimer | 5 | Programmatic (100% must pass) |

### 9.2 Acceptance Gate for Phase 0 → Phase 1

| Criterion | Threshold |
|-----------|-----------|
| Signal explanation quality (human) | Mean ≥ 3.5/5.0 |
| Sentiment label accuracy | ≥ 85% |
| Sentiment score calibration | Pearson r ≥ 0.80 vs FinBERT on comparison dataset |
| Retrieval faithfulness | ≥ 90% of factual claims traceable to retrieved source |
| Safety tests | 100% pass (zero failures acceptable) |
| Explanation generation success rate (7-day prod average) | ≥ 95% |
| Explanation latency p95 | < 5s |

**No phase promotion without a recorded, version-stamped eval run in MLflow.**

---

## 10. Phased Execution Plan

### Phase 0 — Foundation (Estimated: 3–5 weeks)

**Goal:** Provider-agnostic LLM client operating in production. FinBERT replaced in dual-write mode. RAG pipeline live. Explanation generated async and surfaced in UI. Eval harness instrumented. Governance scaffolding live.

| Week | Deliverable | Files changed |
|------|-------------|---------------|
| 1 | PC-1 through PC-4 resolved. `llm_client.py` replaced with LiteLLM-backed `CortexIntelligenceClient`. Startup log shows active backend. Existing callers (`event_classifier`, `fake_news_detector`) tested. | `llm_client.py`, `config.py`, `requirements.in` |
| 1 | Migration 0041 (pgvector). Migration 0043 (audit log). | `alembic/versions/0041_*`, `0043_*` |
| 2 | RAG module: embedder, ingester, retriever, pipeline. Basic ingestion of `ai_raw_events` for last 30 days. Hybrid BM25+vector retrieval tested manually. | `app/ai/rag/` (new module) |
| 2 | Eval harness: 50 gold Q&A pairs written, eval runner script. | `backend/eval/gold_set.jsonl`, `backend/eval/run_eval.py` |
| 3 | `nlp_engine.py` replaced: LLM sentiment scoring with dual-write comparison. First eval run recorded. | `nlp_engine.py` |
| 3 | Migration 0042 (explanation fields). `explanation_worker.py` registered in lifespan. `trade_suggestions.py` publishes on creation. | `app/ai/intelligence/explanation_worker.py`, `alembic/versions/0042_*`, `app/api/v1/trade_suggestions.py` |
| 4 | `ai_stream.py` extended with explanation payload. Schema and TypeScript types updated. `TradeSuggestionCard` renders summary + skeleton. AI Analysis Card renders full explanation with sources. | `ai_stream.py`, `schemas/trade_suggestions.py`, `TradeSuggestionCard.tsx`, AI Analysis Card component |
| 4 | Grafana dashboards for LLM SLOs added to existing stack. All guardrails active. | Grafana dashboard JSON |
| 5 | Calibration analysis: compare LLM vs FinBERT score distributions on dual-write dataset. If Pearson r ≥ 0.80: disable FinBERT. R9 resolved. Phase 0 eval run recorded and gate criteria reviewed. | `nlp_engine.py` (FinBERT removal), eval run artifact |

**Exit gate:** All Phase 0 acceptance criteria in §9.2 met and recorded.

---

### Phase 1 — Skill / Fine-Tuning (Estimated: 4–8 weeks after Phase 0 gate)

**Goal:** A LoRA fine-tuned model on curated Cortex-specific examples that measurably beats the base model on the gold eval.

- Curate 200+ expert-verified SFT examples (signal explanations, sentiment labels with rationale, refusals)
- Synthetic CoT generation via the Phase 0 base model, verified by human expert before use
- SFT → LoRA adapter → DPO for style and safety alignment
- Host fine-tuned checkpoint on a rented GPU pod (RunPod A40, vLLM)
- Point `CortexIntelligenceClient` at the fine-tuned endpoint as primary; NIM as secondary; Ollama as tertiary
- **Exit gate:** fine-tuned model beats Phase 0 base on all gold eval categories by a measurable margin

---

### Phase 2 — Reasoning & Agents (Estimated: 2–3 months after Phase 1 gate)

**Goal:** Verifiable financial reasoning + orchestrated multi-agent system over Cortex's engines.

- GRPO/RLVR training for numerical correctness (Fin-R1 recipe)
- LangGraph-based multi-agent DAG: Data agent (RAG + market feed) → Alpha agent (Cortex signal engine) → Risk agent (guardrails + exposure check)
- No execution agent: Cortex remains advise-only
- Full governance launch bar: independent validation sign-off, documented model card per version, safety eval 100%
- **Exit gate:** independent validation sign-off; safety eval 100%; reasoning gates met; all SR 11-7 required artifacts produced

---

### Phase 3 — Domain-Adaptive CPT (Gated)

**Enter only if:** Phase 1/2 gold eval shows a persistent, material gap that retrieval cannot close **and** a proprietary corpus ≥ 1B tokens exists **and** a GPU cluster + ML specialist are available.

If these conditions are not simultaneously met, this phase does not execute. CPT without evidence is expensive over-engineering.

---

## 11. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| **NIM free tier rate limit (40 RPM) hit during dev** | Medium | Low | Queue explanation requests; batch sentiment scoring; upgrade to paid tier if needed |
| **LLM sentiment scores not calibrated to FinBERT scale** | Medium | High | Dual-write period with Pearson r gate before FinBERT removal; ML retraining in Phase 1 if correlation is insufficient |
| **Explanation latency exceeds 5s p95** | Medium | Medium | Reduce top-k retrieval; cache RAG context for same symbol+window; reduce prompt length |
| **pgvector not available in Postgres binary** | Low | High | Verify `pg_available_extensions` before migration; install `postgresql-16-pgvector` if needed |
| **NIM model availability changes** | Low | Medium | Provider-agnostic client means Ollama fallback is always operational; no single point of failure |
| **Hallucination in explanation reaches user** | Low | High | Guardrails (§8.1) + audit log monitoring; citation requirement prevents ungrounded claims |
| **FinBERT dual-write period extends too long** | Low | Medium | Hard deadline: FinBERT must be removed within 2 weeks of Pearson r ≥ 0.80 being confirmed |
| **R2 (no CI) causes regression** | Medium | Medium | Manually run `pytest backend/` and frontend Vitest on every PR during this period; no exceptions |

---

## 12. Decision Log (Decisions Made)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Provider abstraction | LiteLLM | Unified interface, fallback chains, 100+ providers, production-proven |
| Primary LLM provider | NVIDIA NIM | Free tier, OpenAI-compatible, Qwen3 family |
| Fallback LLM provider | Ollama (existing) | Zero-cost, already integrated, operationally validates provider abstraction |
| Vector store | pgvector (existing Postgres) | Outperforms Qdrant at Cortex's scale, zero new infra |
| Embedding model | nvidia/nv-embedqa-e5-v5 | Finance-adapted, 18-23% better recall, same NIM API key |
| RAG scope | Symbol-scoped news/events only | No general financial KB needed; Cortex already has the data |
| Explanation timing | Async background job | Preserves existing suggestion latency; resilient to LLM failure |
| Explanation placement | Brief on card + full in Analysis Cards | Maximum visibility without blocking the primary action |
| FinBERT migration | Dual-write comparison period | Validates score calibration before removing ML feature dependency |
| Autonomy ceiling | Analyze/advise only | Confirmed; no execution agent in any phase |
| CI (R2) | Deferred | Manual test runs required throughout; no automated gate |

---

## 13. Open Items (Require Action Before Phase 0 Kickoff)

| Item | Owner | Deadline |
|------|-------|----------|
| Obtain NVIDIA NIM API key (PC-1) | Het Trivedi | Before Week 1 |
| Verify pgvector available in Postgres binary (PC-2) | Het Trivedi | Before Week 1 |
| Confirm exact NIM model availability on `build.nvidia.com` and pin in config | Het Trivedi | Before Week 1 |
| Write first 10 gold eval Q&A pairs (establishes the quality bar for everything that follows) | Het Trivedi | End of Week 1 |

---

*This document supersedes all prior architecture discussions and verbal agreements regarding the Cortex LLM upgrade. All implementation decisions not covered here that arise during execution must be recorded in this document before code is written.*

*Full research knowledge base: `documentation/root-artifacts/research/custom-financial-llm/` (10 documents + sources.md)*
