# Gemini API — Outgoing Requests Reference

All Gemini traffic routes through a single SDK entry point:
`backend/app/ai/intelligence/llm_client.py` → `client.aio.models.generate_content()`

---

## Shared Request Envelope

Every generate call wraps its payload in:

```python
GenerateContentConfig(
    temperature       = <per-caller>,
    thinking_config   = ThinkingConfig(thinking_budget=0),    # gemini-2.x
                     # ThinkingConfig(thinking_level="minimal") # gemini-3.x
    http_options      = HttpOptions(timeout=30_000),           # ms; env: LLM_REQUEST_TIMEOUT
    safety_settings   = [BLOCK_NONE] × 4,                     # all harm categories disabled
    # + per-caller: system_instruction, max_output_tokens, response_mime_type, response_schema
)
```

SDK call signature:
```python
await genai.aio.models.generate_content(
    model    = settings.GEMINI_MODEL,   # default: "gemini-2.5-flash"
    contents = <user prompt string>,
    config   = <GenerateContentConfig above>,
)
```

---

## 1. Sentiment Analysis

**File:** `backend/app/ai/intelligence/nlp_engine.py:222`  
**Trigger:** Any incoming news event → `NLPEngine.analyze_sentiment(text)`  
**Method:** `generate_structured`  
**Priority:** `MEDIUM`

### System Instruction
```
You are a financial news sentiment analysis tool for the Cortex trading platform.
You are NOT a licensed financial advisor and must not provide investment advice.

Your task: classify the financial sentiment of a news article for NSE-listed Indian equities.

Rules:
1. Base your classification STRICTLY on the factual content of the article provided.
2. Do NOT use general market knowledge or opinions not present in the article.
3. Sentiment scale:
   - "positive": article describes events likely to increase stock price or investor confidence
     (earnings beats, revenue growth, new contracts, promoter buying, upgrades, etc.)
   - "negative": article describes events likely to decrease stock price or investor confidence
     (earnings misses, revenue decline, debt concerns, downgrades, regulatory action, etc.)
   - "neutral":  article is factual/informational with no clear directional price impact
4. Score must be consistent with label:
   - positive → score in (0.0, 1.0]
   - negative → score in [-1.0, 0.0)
   - neutral  → score = 0.0 (± 0.1 tolerance for near-neutral articles)
5. Confidence reflects how clearly the article supports the sentiment direction.
   Ambiguous or mixed articles should have confidence ≤ 0.6.
6. Reasoning: cite one specific fact from the article that drives your classification.
   Do NOT use speculative phrases like "will rise" or "should buy".
```

### User Prompt
```
Classify the financial sentiment of the following news article:

{full_article_text}   ← entire article body, no truncation
```

### Config
| Param | Value |
|---|---|
| `temperature` | `0.1` |
| `max_output_tokens` | `256` |
| `response_mime_type` | `"application/json"` |
| `response_schema` | `SentimentOutput` |

### Response Schema
```python
class SentimentOutput(BaseModel):
    label:      Literal["positive", "negative", "neutral"]
    score:      float   # [-1.0, +1.0]
    confidence: float   # [0.0, 1.0]
    reasoning:  str     # max 300 chars, one sentence citing a specific fact
```

### Notes
- Redis cache: key `nlp:sentiment:<sha256(prompt)>`, TTL 3600s. Cache hit = zero Gemini call.
- Fallback on failure: `{label:"neutral", score:0.0, confidence:0.0, model:"unavailable"}`

---

## 2. Trade Suggestion Explanation

**File:** `backend/app/ai/intelligence/explanation_worker.py:742`  
**Trigger:** Redis pub on `cortex:llm:explanation:pending` → `_generate_explanation()`  
**Method:** `generate_structured_with_usage`  
**Priority:** `HIGH`

### System Instruction
```
You are a financial signal analysis tool for the Cortex algorithmic trading platform.
You are NOT a licensed financial advisor and must not provide investment recommendations.

Your task: explain a machine-generated trade signal in plain English — what the ML
ensemble (XGBoost + GRU) and technical scanner observed, how that evidence combined into
a consensus, and what it implies — grounded strictly in the structured signal data and the
retrieved news articles provided in the prompt.

Write full_explanation as Markdown with EXACTLY these five section headers, in order, each
on its own line:
### What the models saw
### Technical picture
### News context
### What this suggests
### Key risks

Be concise — 1–2 short sentences per section, ≤110 words total for full_explanation.
No preamble, no filler, no restating the prompt; an analyst is skimming this. When a
section has nothing substantive (e.g. no news, no per-model split), state it in one short
line rather than padding.

Section guidance:
- "What the models saw": describe the ensemble direction and calibrated confidence, then
  the per-model (XGBoost and GRU) directions, buy/sell/hold probabilities, and how each
  model's conviction compares to its regime-adaptive threshold. Note agreement or
  disagreement between the two models. Use ONLY the numbers given.
- "Technical picture": summarise the scanner readings provided (e.g. RSI, volume ratio,
  price change). If none are present, say so briefly.
- "News context": summarise the retrieved articles and CITE each factual claim inline as
  According to [Source Name, YYYY-MM-DD]... If no articles were provided, state that no
  recent news context was available — never invent sources.
- "What this suggests": a neutral synthesis of direction, confidence band and time horizon.
  Describe; do NOT advise.
- "Key risks": what could invalidate the setup (model disagreement, low conviction, thin
  news corroboration, regime, etc.).

Mandatory rules:
1. GROUND every ML/technical claim in the numbers provided; never invent figures, prices,
   model internals, events, or sources.
2. CITE news claims inline: According to [Source Name, YYYY-MM-DD]...
3. PROHIBITED language (these will be filtered):
   - Price predictions: "will reach ₹X", "target price", "price target"
   - Guarantees: "guaranteed", "certain to", "will definitely"
   - Advisory language: "you should buy/sell", "recommend buying", "buy now"
4. DISCLAIMER: The system automatically appends the required regulatory disclaimer.
   Do NOT add your own disclaimer — it would duplicate the injected one.
5. Output JSON only: {"summary": "...", "full_explanation": "...", "sources_used": [...]}
   The summary is plain text (no headers); full_explanation contains the five sections.
```

### User Prompt (dynamically built from `TradeSuggestion` DB row + RAG chunks)
```
## Trade Signal Summary
Symbol:           RELIANCE
Direction:        BUY
Consensus Score:  78.5/100
Confidence:       HIGH
Entry Price:      ₹2847.50
Stop Loss:        ₹2750.00
Risk/Reward:      2.3x
Time Horizon:     intraday
Market Regime:    trending_bullish
Trigger Pathway:  Ml News Consensus

## ML Ensemble Output
Ensemble Direction:     BUY
Calibrated Confidence:  71%
Conviction (0=threshold,1=max): 45%
Ensemble Probabilities: BUY 71% · SELL 8% · HOLD 21%
Model Versions:         xgboost-1.2.0+gru-1.1.0
Per-model breakdown:
  - XGBoost: BUY, probs BUY 73% · SELL 7% · HOLD 20%, conviction 48%, threshold 35%, weight 0.60
  - GRU: BUY, probs BUY 69% · SELL 9% · HOLD 22%, conviction 42%, threshold 35%, weight 0.40

## Technical Scanner Readings
Signal:         BUY
Direction:      up
Score:          78.5
RSI-14:         62.3
Volume Ratio:   1.84
Price Change %: 1.2
Last Price:     ₹2847.50
Prev Close:     ₹2813.20
Volume:         4521000

## News & Event Signal
News Forecaster Lean: BUY (confidence 68%)
News Forecaster View: RSI and MACD alignment with strong volume corroborates the bullish earnings catalyst.
Sentiment:           positive
Contributing Events: 3
  - RELIANCE Q4 earnings beat estimates (impact +2.3, Economic Times)
  - Reliance Jio subscriber growth hits record (impact +1.8, Mint)
  - RIL signs new O2C expansion deal (impact +1.1, Business Standard)

## Retrieved News Context
(Use these articles as the factual basis for the News context section.
Cite inline as: According to [Source Name, YYYY-MM-DD]...)
{RAG-retrieved chunks}
```
*(If no RAG chunks: "## Retrieved News Context\nNo recent news articles were found for this symbol. State clearly in the News context section that no news context was available…")*

### Config
| Param | Value |
|---|---|
| `temperature` | `0.2` |
| `max_output_tokens` | `1400` |
| `response_mime_type` | `"application/json"` |
| `response_schema` | `ExplanationOutput` |

### Response Schema
```python
class ExplanationOutput(BaseModel):
    summary:          str        # 2-3 sentence plain-text for inline display
    full_explanation: str        # Markdown with exactly 5 ### headers
    sources_used:     list[str]  # source names cited in full_explanation
```

### Post-processing
1. Price-prediction regex filter (removes violating sentences)
2. Regulatory disclaimer appended to `full_explanation`
3. Written to `trade_suggestions.llm_summary` + `llm_explanation`
4. Audit row → `ai_llm_audit_log`
5. Redis pub → `cortex:llm:explanation:ready:{suggestion_id}`
6. Max 2 attempts; failure leaves `llm_summary` NULL, no SSE notification

---

## 3. Instrument Market Context

**File:** `backend/app/ai/intelligence/explanation_worker.py:923`  
**Trigger:** Redis pub on `cortex:llm:context:pending` → `_generate_instrument_context()`  
**Method:** `generate_structured_with_usage`  
**Priority:** `LOW`

### System Instruction
```
You are a market context analysis tool for the Cortex algorithmic trading platform.
You are NOT a licensed financial advisor and must not provide investment recommendations.

Your task: explain the current read on a specific NSE-listed stock that has no active trade
signal — what the ML ensemble (XGBoost + GRU) is currently leaning toward and why, and what
recent news is relevant — grounded strictly in the structured model snapshot and the
retrieved news articles provided in the prompt.

Write full_explanation as Markdown with EXACTLY these five section headers, in order, each
on its own line:
### What the models saw
### Technical picture
### News context
### What this suggests
### Key risks

Be concise — 1–2 short sentences per section, ≤110 words total for full_explanation.
No preamble, no filler, no restating the prompt; an analyst is skimming this.

Section guidance:
- "What the models saw": describe the ensemble's current direction and calibrated
  confidence, then the per-model (XGBoost and GRU) directions, buy/sell/hold probabilities,
  and conviction-vs-threshold. Note agreement or disagreement. Use ONLY the numbers given.
  If no model snapshot is provided, state that no live model read was available.
- "Technical picture": summarise volatility / market-regime signals provided, if any.
- "News context": summarise the retrieved articles and CITE each factual claim inline as
  According to [Source Name, YYYY-MM-DD]... If no articles were provided, state that no
  recent news context was available — never invent sources.
- "What this suggests": a neutral synthesis of the current lean. Describe; do NOT advise.
- "Key risks": model disagreement, low conviction, thin news corroboration, volatility, etc.

Mandatory rules:
1. GROUND every ML claim in the numbers provided; never invent figures, prices, model
   internals, events, or sources.
2. CITE news claims inline: According to [Source Name, YYYY-MM-DD]...
3. PROHIBITED: "will reach ₹X", "target price", "guaranteed", "you should buy/sell"
4. DISCLAIMER: Do NOT add your own — system auto-appends it.
5. Output JSON only: {"summary": "...", "full_explanation": "...", "sources_used": [...]}
```

### User Prompt (built from `ml_snapshot` dict + RAG chunks)
```
## Instrument Overview
Instrument Key: NSE_EQ|INE002A01018
Symbol:         RELIANCE

## Current ML Ensemble Snapshot
Ensemble Direction:     BUY
Calibrated Confidence:  58%
Conviction (0=threshold,1=max): 31%
Regime Threshold:       35%
Ensemble Probabilities: BUY 58% · SELL 12% · HOLD 30%
Annualised Volatility:  22%
Timeframe:              intraday
Per-model breakdown:
  - XGBoost: BUY, probs BUY 61% · SELL 10% · HOLD 29%, conviction 35%, threshold 35%, weight 0.60
  - GRU: HOLD, probs BUY 55% · SELL 14% · HOLD 31%, conviction 27%, threshold 35%, weight 0.40

## Retrieved News Context
(Use these articles as the factual basis for the News context section.
Cite inline as: According to [Source Name, YYYY-MM-DD]...)
{RAG chunks}
```

### Config
| Param | Value |
|---|---|
| `temperature` | `0.2` |
| `max_output_tokens` | `1400` |
| `response_mime_type` | `"application/json"` |
| `response_schema` | `ExplanationOutput` |

Same `ExplanationOutput` schema as explanation. Persisted to `ai_instrument_context` with 2-hour TTL (upsert on `instrument_key`).

---

## 4. Event Classification

**File:** `backend/app/ai/intelligence/event_classifier.py:529`  
**Trigger:** Every processed news event → `_classify_with_ollama(content, entities)`  
**Method:** `generate_structured`  
**Priority:** `MEDIUM`

### System Instruction
```
You are an expert financial event classifier for Indian equity markets (NSE/BSE).
Always output exact NSE trading symbols — never full company names.
If you are not certain of a symbol, omit it rather than guessing.
```

### User Prompt
```
Classify this Indian financial market event.

Content: {content[:1500]}
Extracted entities — companies: ['RELIANCE', 'TCS']   ← up to 5 companies, if any

Instructions for affected_symbols:
  • List every NSE-listed company mentioned or implicated.
  • Use the exact NSE ticker symbol, not the full company name.
    Examples: RELIANCE (Reliance Industries), TCS (Tata Consultancy),
    HDFCBANK (HDFC Bank), INFY (Infosys), HINDZINC (Hindustan Zinc),
    VEDL (Vedanta), IOC (Indian Oil), ONGC, WIPRO, BAJFINANCE.
  • For sector-wide or macro events with no specific company, return [].
  • For decay_hours: estimate intraday price reaction window (4–48h).
  • For decay_slow_hours: estimate fundamental repricing window (24–168h).
```

### Config
| Param | Value |
|---|---|
| `temperature` | `0.1` |
| `max_output_tokens` | none |
| `response_mime_type` | `"application/json"` |
| `response_schema` | `_ClassificationSchema` |

### Response Schema
```python
class _ClassificationSchema(BaseModel):
    event_type:       str         # "earnings", "regulatory", "merger_acquisition", etc.
    impact_score:     float       # 0–100
    sentiment:        str         # "bullish" / "bearish" / "neutral"
    confidence:       float       # 0.0–1.0
    affected_symbols: list[str]   # exact NSE tickers
    reasoning:        str
    decay_hours:      float       # intraday reaction window in hours
    decay_slow_hours: float       # fundamental repricing window in hours
```

### Notes
- Fallback on failure: `{confidence:0.0, event_type:"general", impact_score:50.0, affected_symbols:[], ...}` → triggers rule-based classification path.

---

## 5. Fake News Detection

**File:** `backend/app/ai/intelligence/fake_news_detector.py:316`  
**Trigger:** Credibility scoring pipeline → `_llm_reasoning(content, source, classification)`  
**Method:** `generate_json` (schema-free — no `response_schema`)  
**Priority:** `MEDIUM` (default)

### System Instruction
```
You are an expert fact-checker analyzing financial news credibility.
```

### User Prompt
```
Analyze this financial news for credibility:

Content: {content}
Source: {source}
Event Type: {classification.event_type}
Impact Score: {classification.impact_score}

Check for:
- Sensationalist language
- Unrealistic claims
- Missing key details
- Logical inconsistencies
- Source reliability indicators

Return JSON with:
- is_credible: boolean
- credibility_score: float 0-1 (1=highly credible, 0=fake)
- red_flags: list of concerns
- reasoning: explanation
```

### Config
| Param | Value |
|---|---|
| `temperature` | `0.3` |
| `max_output_tokens` | none |
| `response_mime_type` | `"application/json"` |
| `response_schema` | none — raw `json.loads()` |

### Notes
- Only `credibility_score` (float) is consumed. All other returned fields are ignored.
- Fallback on any error: `0.5` (neutral credibility).

---

## 6. News Forecaster

**File:** `backend/app/ai/fusion/news_forecaster.py:276`  
**Trigger:** `SignalAssembler.gather_news_forecast()` during signal assembly  
**Method:** `generate_structured_with_usage`  
**Priority:** `MEDIUM`  
**Hard wall-clock timeout:** `asyncio.wait_for(..., timeout=3.0s)` (env: `GEMINI_FORECAST_TIMEOUT`)

### System Instruction
```
You are the news-aware forecasting module of the Cortex trading platform — the
second forecaster alongside a separate ML ensemble. You are NOT a financial
advisor and must not give investment advice.

You are given the SAME technical indicators the ML model uses, plus recent news
events the ML cannot read. Your job: weigh the indicators and the news together
and output an INDEPENDENT directional view — direction (BUY/SELL/HOLD), a
calibrated confidence (0–1), and a one/two-sentence rationale.

Your edge over the ML is the NEWS: when credible, recent, material news
corroborates or contradicts the technical picture, let it move your direction
and confidence accordingly. With no relevant news, lean on the indicators alone
and keep confidence modest.

Rules:
- GROUND every claim in the specific numbers/news provided. Never invent figures,
  prices, events, or sources.
- No price targets, no guarantees, no advice ("buy now", "you should…").
- HOLD when indicators and news conflict or the signal is weak.
- Confidence is calibrated, not promotional: >0.8 only on strong, corroborated
  agreement; ≤0.5 when evidence is thin, mixed, or news is absent.
```

### User Prompt (built by `build_forecast_prompt()`)
```
Symbol: RELIANCE
Market regime: trending_bullish

Technical indicators (identical to the ML model's inputs):
  Last Close: 2848.
  RSI-14: 62.3
  RSI-21: 58.1
  MACD line: 14.2
  MACD signal: 11.8
  MACD histogram: 2.4
  EMA-20: 2801.
  EMA-50: 2744.
  DEMA-20: 2816.
  ATR-14: 38.4
  ADX-14: 27.1
  OBV: 8.412e+07
  Volume ratio: 1.84
  Bollinger upper: 2911.
  Bollinger lower: 2691.

Recent news / events:
  - RELIANCE Q4 earnings beat estimates (impact +2.3, positive, Economic Times)
  - Reliance Jio subscriber growth hits record (impact +1.8, positive, Mint)
  - RIL signs new O2C expansion deal (impact +1.1, positive, Business Standard)
  ← up to 6 events; missing keys silently skipped
```
*(If no events: "Recent news / events: none in the lookback window — base the call on the indicators and keep confidence modest.")*

### Config
| Param | Value |
|---|---|
| `temperature` | `0.2` |
| `max_output_tokens` | `400` (env: `GEMINI_FORECAST_MAX_TOKENS`) |
| `response_mime_type` | `"application/json"` |
| `response_schema` | `NewsForecastOutput` |

### Response Schema
```python
class NewsForecastOutput(BaseModel):
    rationale:  str                          # ≤45 words, grounded in numbers + news
    direction:  Literal["BUY", "SELL", "HOLD"]
    confidence: float                        # 0.0–1.0, calibrated
```

### Notes
- Output mapped to `−100..+100` score: `score = direction_sign × confidence × 100` → feeds signal fusion consensus.
- Circuit breaker: 4 consecutive Gemini failures → opens for 60s; while open, caller gets deterministic NLP fallback with zero Gemini calls.

---

## 7. RAG Embeddings

**File:** `backend/app/ai/rag/embedder.py:72`  
**Trigger:** Document indexing (corpus) or query-time retrieval  
**Method:** `embed` → `aio.models.embed_content()`  
**Priority:** `BACKGROUND`

### SDK Call
```python
await genai.aio.models.embed_content(
    model    = "gemini-embedding-001",         # env: GEMINI_EMBED_MODEL
    contents = ["text a", "text b", ...],      # batch; size env: RAG_EMBED_BATCH_SIZE (default 32)
    config   = EmbedContentConfig(
        task_type            = "RETRIEVAL_DOCUMENT",  # corpus indexing
                            # "RETRIEVAL_QUERY"       # query-time retrieval
        output_dimensionality = 1536,                 # env: GEMINI_EMBED_DIM (range 128–3072)
        http_options         = HttpOptions(timeout=30_000),
    ),
)
```

No system prompt, no temperature, no schema. Returns `resp.embeddings` → list of float vectors. L2-normalized post-hoc when `embed_dim < 3072` (Gemini doesn't pre-normalize truncated vectors; pgvector search assumes unit norm).

---

## 8. Health Check

**File:** `backend/app/ai/intelligence/llm_client.py:652`  
**Trigger:** App startup / `/health` endpoint

```python
await genai.aio.models.generate_content(
    model    = "gemini-2.5-flash",
    contents = "ping",
    config   = GenerateContentConfig(
        max_output_tokens = 1,
        thinking_config   = ThinkingConfig(thinking_budget=0),
        http_options      = HttpOptions(timeout=30_000),
    ),
)
```

No system prompt, no schema. Response discarded — only checks that the call succeeds.

---

## Summary

| Caller | File | Method | Temp | Max tokens | Schema | Priority | Timeout |
|---|---|---|---|---|---|---|---|
| Sentiment | `nlp_engine.py:222` | `generate_structured` | 0.1 | 256 | `SentimentOutput` | MEDIUM | 30s |
| Trade explanation | `explanation_worker.py:742` | `generate_structured_with_usage` | 0.2 | 1400 | `ExplanationOutput` | HIGH | 30s |
| Instrument context | `explanation_worker.py:923` | `generate_structured_with_usage` | 0.2 | 1400 | `ExplanationOutput` | LOW | 30s |
| Event classification | `event_classifier.py:529` | `generate_structured` | 0.1 | none | `_ClassificationSchema` | MEDIUM | 30s |
| Fake news | `fake_news_detector.py:316` | `generate_json` | 0.3 | none | none (raw JSON) | MEDIUM | 30s |
| News forecaster | `news_forecaster.py:276` | `generate_structured_with_usage` | 0.2 | 400 | `NewsForecastOutput` | MEDIUM | **3s** |
| RAG embeddings | `rag/embedder.py:72` | `embed` | — | — | — | BACKGROUND | 30s |
| Health check | `llm_client.py:652` | direct SDK | — | 1 | — | — | 30s |

---

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | none (required) | API key; `None` disables all Gemini calls |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Generation model |
| `GEMINI_THINKING_LEVEL` | `minimal` | Maps to `thinking_budget=0` (2.x) or `thinking_level="minimal"` (3.x) |
| `GEMINI_EMBED_MODEL` | `gemini-embedding-001` | Embedding model |
| `GEMINI_EMBED_DIM` | `1536` | Output embedding dimensions (128–3072) |
| `GEMINI_GENERATE_RPM` | `150` | Rate limit: requests/minute for generate |
| `GEMINI_GENERATE_TPM` | `1_000_000` | Rate limit: tokens/minute for generate |
| `GEMINI_EMBED_RPM` | `90` | Rate limit: requests/minute for embed |
| `GEMINI_MAX_QUEUE_DEPTH` | `50` | Max queued permits before `GeminiRateLimitError` |
| `GEMINI_PERMIT_TIMEOUT` | `30.0` | Seconds to wait for a rate-limit permit |
| `GEMINI_FORECAST_TIMEOUT` | `3.0` | Hard asyncio wall-clock timeout for forecaster call |
| `GEMINI_FORECAST_MAX_TOKENS` | `400` | Max output tokens for news forecaster |
| `LLM_MAX_RETRIES` | `3` | Tenacity retry attempts on transient errors |
| `LLM_REQUEST_TIMEOUT` | `30.0` | HTTP timeout in seconds (sent as ms to SDK) |
