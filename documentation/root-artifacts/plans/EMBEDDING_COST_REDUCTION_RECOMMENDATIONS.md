# RAG Embedding Cost Reduction — Recommendations

> Generated: 2026-06-19
> Cross-referenced from: GEMINI_API_REQUESTS.md × CostCutting Ideas.md + external research
> Last updated: 2026-06-20 — Quick Wins (#1, #2, #3) COMPLETE and deployed

---

## What Already Exists (Do Not Rebuild)

| Feature | File | Status |
|---|---|---|
| Hybrid BM25 + vector with RRF | `retriever.py:1–347` | Done |
| Anti-join dedup on ingestion (per-source) | `ingester.py:108–116` — `ON CONFLICT DO NOTHING` on `(source_table, source_id)` | Done |
| `content_hash` SHA-256 column computed + stored | `ingester.py:187` | Done |
| HNSW index (m=16, ef_construction=64) | Migration `0041`/`0046` | Done |
| Single-chunk per short article | `ingester.py:7–12` | Done |
| Python-side cosine ranking | `retriever.py:36–41` | Correct at current scale |

---

## Ranked Recommendations by ROI

---

### #1 — Query Embedding Redis Cache ✅ COMPLETE (2026-06-20)

**Files:** `backend/app/ai/rag/embedder.py:92`
**Effort:** 2 hours
**Impact:** 60–80% reduction in query-time Gemini embedding API calls

**Problem:** Every `retrieve()` call re-embeds the query string live via the Gemini API. Both callers in `explanation_worker.py` construct queries deterministically — `symbol + direction` (line 737) and `symbol + "market analysis news"` (line 932) — so the same query string fires many times per day for any actively traded symbol. The Redis SHA-256 cache pattern already used for sentiment (`nlp:sentiment:<sha256>`, TTL 3600s in `nlp_engine.py`) is not replicated for query embeddings.

**Fix:** Wrap `embed_query()` at `embedder.py:92` with a `rag:qembed:<sha256(query)>` Redis cache key, TTL 900s (15 min). Use the existing Redis pool — no new infrastructure required.

```python
# embedder.py — wrap embed_query()
_QUERY_EMBED_TTL = 900  # 15 minutes

async def embed_query(query: str) -> list[float]:
    import hashlib, json
    from app.core.redis_client import get_redis
    cache_key = f"rag:qembed:{hashlib.sha256(query.encode()).hexdigest()}"
    redis = await get_redis()
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)
    vector = (await embed_texts([query], input_type="query"))[0]
    await redis.setex(cache_key, _QUERY_EMBED_TTL, json.dumps(vector))
    return vector
```

---

### #2 — Reduce GEMINI_EMBED_DIM from 1536 to 768

**Files:** `.env` + new Alembic migration `0047`
**Effort:** 3 hours (+ re-embed downtime window)
**Impact:** 50% reduction in pgvector storage; faster Python-side cosine ranking; no API token cost change

**Problem:** `gemini-embedding-001` uses Matryoshka Representation Learning (MRL) — the first N dimensions of a 3072-dim vector are a valid lower-dimensional embedding. Benchmarks (arXiv:2407.20243) show 768-dim retains ~97–99% retrieval quality on monolingual English corpora. The codebase already uses the MRL knob at 1536 (migration `0046` docstring notes this logic) — the same argument applies going 1536 → 768. Gemini charges by input tokens, not output vector size, so there is no API cost change.

**Fix:**
1. `.env` → `GEMINI_EMBED_DIM=768`
2. New migration `0047`: drop HNSW index, TRUNCATE table, `ALTER COLUMN embedding TYPE vector(768)`, recreate HNSW index.
3. Re-embed corpus via existing `scripts/backfill_rag_embeddings.py`.

---

### #3 — Cross-Source Content-Hash Dedup ✅ COMPLETE (2026-06-20)

**Files:** `backend/app/ai/rag/ingester.py:158` + new Alembic migration
**Effort:** 3 hours
**Impact:** 10–30% reduction in corpus ingestion embedding API calls

**Problem:** The existing unique constraint `uq_ai_doc_embeddings_source` is on `(source_table, source_id)` — it deduplicates within one RSS feed but not across feeds. Two different feeds publishing the same article body produce two embedding API calls and two rows with identical `content_hash` values. Research (arXiv:2605.09611) shows 24% byte-level redundancy in enterprise news corpora; multi-feed aggregators skew higher. The `content_hash` SHA-256 column already exists in the table — it just has no unique constraint.

**Fix:**
1. New migration: `ALTER TABLE ai_document_embeddings ADD CONSTRAINT uq_content_hash UNIQUE (content_hash);`
2. Change `ON CONFLICT` target in `ingester.py:158` to use `uq_content_hash`.
3. Rows with duplicate content bodies across sources will auto-skip without calling the embedding API.

---

### #4 — Gemini Batch API for Corpus Ingestion

**Files:** `backend/app/ai/rag/embedder.py:37`, `backend/app/ai/intelligence/llm_client.py`
**Effort:** 8 hours
**Impact:** 50% cost reduction on all corpus ingestion embedding calls ($0.15 → $0.075 per 1M tokens)

**Problem:** Corpus ingestion runs in the BACKGROUND priority queue in the worker sidecar and is not latency-sensitive, making it a perfect fit for the Gemini Batch Embedding API which offers a flat 50% discount for asynchronous jobs. All calls currently use the standard synchronous endpoint.

**Fix:**
1. Add `use_batch_api: bool = False` to `embed_texts()` at `embedder.py:37`.
2. Pass `use_batch_api=True` from `ingester.py:158` when called via the background worker.
3. Add `embed_batch()` method in `llm_client.py` using the Batch API endpoint with a submit-and-poll loop.

Note: The Batch API returns results asynchronously. The current `await embed_texts()` pattern in the ingester needs to become a submit-and-poll loop for batch mode. Do this after quick wins are validated.

---

### #5 — halfvec Storage Quantization

**Files:** New Alembic migration + `backend/app/ai/rag/models.py`
**Effort:** 4 hours
**Impact:** 50% reduction in PostgreSQL storage for `ai_document_embeddings`

**Problem:** pgvector 0.7+ supports `halfvec` — storing each embedding dimension as FP16 instead of FP32. For 1536-dim vectors: 3,076 bytes/vector vs 6,144 bytes. Benchmarks show <0.02 NDCG@5 recall impact and 2–3x faster HNSW index builds. pgvector 0.8.2 is already installed (per migration `0041` comments). The table currently uses `Vector(1536)` (FP32).

**Fix:**
```sql
-- New migration 0047 (or 0048 if combined with dim reduction above)
ALTER TABLE ai_document_embeddings
    ALTER COLUMN embedding TYPE halfvec(1536);

DROP INDEX idx_ai_doc_embeddings_hnsw;
CREATE INDEX idx_ai_doc_embeddings_hnsw
    ON ai_document_embeddings
    USING hnsw ((embedding::halfvec(1536)) halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

Update `models.py`: replace `Vector(1536)` with `HalfVector(1536)` from `pgvector.sqlalchemy`. Python-side cosine ranking in `retriever.py` is unaffected — halfvec columns return `list[float]`.

Note: Combine with #2 (dim reduction) into a single migration to avoid two re-embed cycles.

---

### #6 — Increase Batch Size from 32 to 96 ✅ COMPLETE (2026-06-20)

**Files:** `.env` only
**Effort:** 10 minutes
**Impact:** 66% fewer HTTP round-trips to Gemini during corpus ingestion

**Problem:** The Gemini Embedding API supports batches of up to 100 texts. The current `RAG_EMBED_BATCH_SIZE=32` (`config.py:240`) means 3 API requests where 1 would suffice. The embedder loop at `embedder.py:70–87` already handles arbitrary batch sizes — no code change required.

**Fix:** `.env` → `RAG_EMBED_BATCH_SIZE=96`. Monitor Prometheus for rate limit errors (90 RPM ceiling).

---

### #7 — Corpus TTL Eviction

**Files:** Worker sidecar scheduler
**Effort:** 1 hour
**Impact:** Prevents unbounded table growth; keeps candidate set sizes lean over time

**Problem:** No eviction policy exists. The `window_days=30` anti-join cutoff in `_fetch_unembedded_events()` prevents re-embedding old content but never deletes old rows. Embeddings accumulate indefinitely, inflating candidate set sizes and storage.

**Fix:** Add a daily cleanup task to the worker sidecar periodic task scheduler:
```python
await db.execute(text(
    "DELETE FROM ai_document_embeddings WHERE as_of_timestamp < NOW() - INTERVAL '7 days'"
))
```

---

### #8 — HNSW ef_search Tuning (Future — Monitor First)

**Files:** `backend/app/ai/rag/retriever.py:209`
**Effort:** 4 hours (when triggered)
**Impact:** Latency reduction for large candidate sets; no API cost impact

**Current state:** The HNSW index is not used at query time. `_load_candidates()` at `retriever.py:209–280` issues a plain `SELECT` filtered by `(symbol, as_of_timestamp)` using the B-tree index. The HNSW index only activates with an `ORDER BY embedding <=> $1 LIMIT n` pattern. At ~130 candidates/query this is correct and faster than ANN.

**When to act:** When Prometheus shows `rag.retrieve candidates > 300` on any symbol. At that point add a DB-side ANN pre-filter path before Python-side BM25 ranking.

---

### #9 — Local Embedding Model (BGE-M3 / Nomic / Jina) — Not Recommended Yet

**Effort:** 20 hours + GPU infra
**Impact:** 100% elimination of embedding API cost, but new infra cost

**Why deferred:** Break-even for self-hosting is ~10–50M embeddings/month. Cortex's current corpus (~10K docs, 30-day window) is well below this threshold. `gemini-embedding-001` at $0.15/1M tokens is cheaper than a GPU instance at this scale. Additionally, MRL flexibility and domain-specific quality would need validation against candidates (BGE-M3, `nomic-embed-text-v1.5`, `jina-embeddings-v3`).

**Revisit when:** Embedding volume exceeds 5M tokens/month or corpus expands to 500K+ documents.

---

## Prioritized Action Plan

### Quick Wins — COMPLETE ✅ (2026-06-20, ~5h total)

| # | Action | File | Effort | Status |
|---|---|---|---|---|
| 1 | `RAG_EMBED_BATCH_SIZE=96` in `.env` | `.env` | 10 min | ✅ Deployed |
| 2 | Query embedding Redis cache | `embedder.py:107` | 2h | ✅ Deployed |
| 3 | Cross-source content-hash unique constraint | `ingester.py` + migration `0047` | 3h | ✅ Deployed |

**Implementation notes (vs. original spec):**
- #3 goes beyond the spec: the SQL fetch query (`_fetch_unembedded_events`) now also excludes content via a correlated `NOT EXISTS (sha256)` subquery, so the Gemini API is not called at all for cross-source duplicates (not just skipped at INSERT time).
- Python-side batch dedup added to `ingest_batch()` as a third layer for intra-batch duplicates ingested simultaneously from two feeds.
- `ON CONFLICT DO NOTHING` widened (no constraint specified) to atomically cover both `uq_ai_doc_embeddings_source` and `uq_ai_doc_embeddings_content_hash`.

### Medium Term — This Sprint (~8h)

| # | Action | File | Effort |
|---|---|---|---|
| 4 | `GEMINI_EMBED_DIM=768` + migration `0047` | `.env` + migration | 3h + re-embed |
| 5 | Corpus TTL eviction (7-day DELETE job) | Worker sidecar | 1h |
| 6 | `halfvec` migration (combine with #4) | Migration + `models.py` | 4h |

### Longer Term — Next Quarter

| # | Action | File | Effort |
|---|---|---|---|
| 7 | Gemini Batch API for ingestion | `embedder.py:37`, `llm_client.py` | 8h |
| 8 | HNSW ef_search tuning | `retriever.py:209` | 4h (when triggered) |
| 9 | Local embedding model evaluation | Full infra | 20h (when volume warrants) |

---

## Expected Total Impact (Quick Wins + Medium Term Combined)

| Metric | Current | After Quick Wins + Medium Term | Reduction |
|---|---|---|---|
| Query-time Gemini calls | 1 per retrieve() | ~0.2–0.4 per retrieve() (cache hits) | 60–80% |
| Ingestion Gemini calls | 1 per unique (source, id) | Deduped across sources | 10–30% |
| pgvector storage per vector | 6,144 bytes (FP32, 1536-dim) | 1,538 bytes (FP16, 768-dim) | ~75% |
| HTTP round-trips per batch | 3 (batch=32) | 1 (batch=96) | 66% |

---

## Sources

- arXiv:2407.20243 — Matryoshka-Adaptor: Unsupervised and Supervised Tuning for Smaller Embedding Dimensions
- arXiv:2605.09611 — Byte-Exact Deduplication in RAG: A Three-Regime Empirical Analysis
- arXiv:2510.12474 — SMEC: Rethinking Matryoshka Representation Learning for Retrieval Embedding Compression
- pgvector GitHub — HNSW and IVFFlat documentation
- Neon Blog — halfvec vs vector: 50% Storage Reduction
- Jonathan Katz — Scalar and Binary Quantization for pgvector
- Redis Blog — 10 Techniques for Semantic Cache Optimization
- Google AI — gemini-embedding-001 pricing ($0.15/1M tokens standard, $0.075/1M batch)
