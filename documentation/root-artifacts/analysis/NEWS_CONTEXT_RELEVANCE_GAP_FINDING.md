# News Context Relevance Gap — Finding

## Root Cause Analysis

### Problem Summary
**Problem**: The AI instrument-context explanation generated for `COMSYN` cited news about SK Hynix's Nasdaq debut as supporting context. SK Hynix is a Korean memory-semiconductor company; COMSYN (Commercial Syn Bags Ltd) is an Indian industrial packaging manufacturer. The two have no company-level or industry-level relationship.

**User Impact**:
- Explanation's "News context" and "What this suggests" sections reference a news event with no genuine relevance to the instrument
- Reader is led to believe the cited news is instrument-relevant when it is generic market chatter
- Erodes trust in the "News context" section of AI explanations generally

### Confirmed Data

**Record inspected**: `ai_instrument_context`, `symbol = 'COMSYN'`, `instrument_key = NSE_EQ|INE073V01015`
- `model_used`: `gemini/gemini-2.5-flash`
- `generated_at`: `2026-07-11 10:52:32 UTC`

**`source_refs` attached to this explanation** (5 articles, all general market/AI-chip wire pieces, none COMSYN-specific):
- Economic Times Markets — "SK Hynix shares jump 14% in debut on US stock market riding on AI wave"
- LiveMint Markets — "SK Hynix's shares surge 14% in blockbuster US market debut amid AI frenzy"
- Economic Times Markets — "Korea's SK Hynix shares make stellar US market debut, rocket 13% on continued AI optimism"
- Economic Times Markets — "Concurrent gainers: 10 stocks that gained for 5 days in a row"
- LiveMint Markets — "Wall Street mixed ahead of SK Hynix debut on Nasdaq"

**Company identity check** (`instrument_master`):
- `COMSYN` → `COMMERCIAL SYN BAGS LTD`, NSE EQ — synthetic/woven polypropylene industrial bags and packaging manufacturer

### Root Cause
**Root Cause**: The news-context retrieval step in the sentiment/context pipeline is surfacing high-buzz, thematically generic "AI wave" market news as filler when no instrument-specific or sector-specific news exists, rather than returning an explicit "no relevant news found" state. The LLM (`gemini-2.5-flash`) faithfully summarized the articles it was given — the defect is upstream, in what gets retrieved and handed to the prompt, not in the LLM's synthesis.

**Company-level correlation**: None. Different countries (Korea vs. India), different exchanges (Nasdaq vs. NSE), different products (memory semiconductors vs. woven packaging), no supply-chain, ownership, or competitive relationship.

**Industry-level correlation**: None. SK Hynix's news is specific to AI/data-center-driven memory-chip demand. COMSYN's business (polypropylene bags/industrial packaging) shares no raw-material, end-market, or demand-driver overlap with semiconductors. There is no plausible cross-elasticity between the two.

## Evidence Collected

### Database Evidence
- `ai_instrument_context.source_refs` for the COMSYN row contains only broad "US stocks" / "markets" wire content — no article mentions COMSYN, Commercial Syn Bags, or the packaging/polymer industry
- All 5 source articles cluster around a single unrelated event (SK Hynix Nasdaq debut, 2026-07-10/11)

### Explanation Text Evidence
- `context_full` "News context" section states news is relevant to "related sectors" without any sector linkage actually existing between semiconductors and industrial packaging
- `context_full` "What this suggests" section extrapolates "broader market context shows strong investor appetite for AI-linked semiconductor stocks, which could influence related sectors" — an unsupported inferential leap the LLM was allowed to make on top of irrelevant source material

## Deep Investigation — Refined Root Cause (2026-07-11)

The original root cause above is directionally correct but imprecise. Tracing the actual
code path (`backend/app/ai/intelligence/explanation_worker.py` →
`backend/app/ai/rag/pipeline.py` → `backend/app/ai/rag/retriever.py`) surfaces four
concrete, compounding defects rather than a single "filler" behavior:

1. **Generic, symbol-agnostic retrieval query.**
   `explanation_worker.py:1563` builds the RAG query as
   `f"{eff_symbol} market analysis news"` — for COMSYN, literally
   `"COMSYN market analysis news"`. This exact string is what both BM25 and the
   embedding model score every candidate document against.

2. **Zero symbol-specific coverage silently falls back to 100% generic pool.**
   `_load_candidates()` (`retriever.py:265-345`) first queries for
   `symbol = 'COMSYN'` docs, gets zero rows, then fills the *entire* 500-candidate
   budget from `symbol IS NULL` general market docs (~3.9k-doc corpus). Every
   candidate considered is generic wire content — none of it is COMSYN- or
   packaging/polymer-industry-related, because none exists.

3. **No relevance floor exists anywhere in the ranking chain:**
   - `_bm25_rank()` (`retriever.py:139-158`) only excludes candidates with an
     exact **zero** BM25 score. The query tokens `"comsyn"`, `"market"`,
     `"analysis"`, `"news"` — "comsyn" matches nothing, but "market" and "news"
     are near-ubiquitous tokens in financial wire content, so unrelated SK Hynix
     "stock market debut" articles get non-zero, competitive BM25 scores purely
     from stopword-like term overlap, not topical relevance.
   - `_cosine_rank()` (`retriever.py:169-183`) has **no similarity threshold at
     all** — it always returns the top-N candidates by cosine similarity
     regardless of how low the absolute similarity is. "COMSYN market analysis
     news" embeds close to *any* generic markets/stocks/investor-sentiment
     article, including SK Hynix's Nasdaq-debut coverage.
   - `_rrf_merge()` (`retriever.py:191-263`) fuses whatever the two rankers
     hand it with no minimum RRF score cutoff before returning results.

4. **Prompt construction only special-cases the fully-empty case.**
   `_build_context_prompt` (~`explanation_worker.py:846-914`) instructs the LLM
   to state "no recent news context was available" only when the retrieved
   `chunks` list is empty. Any non-empty result — even one built entirely from
   topically irrelevant filler — is handed to the LLM as legitimate, citable
   source material with instructions to summarize and cite it inline.

**Net effect**: for any symbol with no dedicated news coverage in the window, the
pipeline is structurally guaranteed to return *something* from the general pool and
present it to the LLM as valid context, because there is no point in the retrieval
→ ranking → prompt-construction chain where "closest available match" is
distinguished from "actually relevant." The SK Hynix/COMSYN mismatch is not an edge
case — it is the expected outcome whenever a low-coverage symbol's context window
overlaps with a high-volume, high-lexical-overlap ("market", "news", "stocks") general
news event.

This is a deeper defect than "filler backfill": a `sector`/`industry` tag on
`instrument_master` would help disambiguate candidates but would **not** by itself
fix the issue, since the pipeline still has no absolute relevance/similarity
threshold at the BM25, cosine, or RRF stage, nor a partial-relevance path in prompt
construction (only fully-empty vs. non-empty is distinguished today).

## Fix Status

**Not yet fixed.** This is a finding, not a completed remediation. No code changes have been made.

### Suggested Direction (not implemented)
- Add a relevance gate between news retrieval and prompt construction: score/filter candidate articles against the instrument's name, symbol, and (if available) sector/industry tags before inclusion
- Introduce a minimum absolute-score threshold at the BM25 and/or cosine ranking stage (or on the final RRF score) so that "closest available" candidates below the threshold are dropped rather than returned
- Make the retrieval query itself more discriminating than generic boilerplate (`"<symbol> market analysis news"`) — e.g. include company name, sector/industry terms — so lexical/semantic scoring has real signal to work with
- When no article clears the relevance threshold, explicitly instruct the LLM to omit or flag the "News context" section rather than backfilling with generic high-buzz market news (extend the existing empty-chunks-only prompt branch to a below-threshold-relevance branch)
- Consider adding a `sector`/`industry` column to `instrument_master` (currently absent) to enable industry-level relevance scoring, not just symbol/name matching — necessary but not sufficient on its own

## Status

- ✅ Root cause identified (news retrieval relevance gap, not an LLM synthesis defect)
- ✅ Deep root cause identified: no relevance/similarity floor at BM25, cosine, or RRF stage; generic retrieval query; empty-vs-nonempty is the only branch the prompt builder distinguishes
- ✅ Confirmed no company-level or industry-level correlation between COMSYN and SK Hynix
- ✅ Evidence collected from `ai_instrument_context` and `instrument_master`
- ✅ Exact code locations identified: `explanation_worker.py:1563` (query), `retriever.py:265-345` (candidate fallback), `retriever.py:139-158` / `169-183` / `191-263` (no-threshold ranking), `explanation_worker.py:846-914` (empty-only prompt branch)
- ⏳ Fix not implemented
- ⏳ `instrument_master` lacks sector/industry metadata needed for a proper relevance gate
