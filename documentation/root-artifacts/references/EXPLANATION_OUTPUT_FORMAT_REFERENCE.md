# Cortex AI Explanation — Current Output Format

This document captures the current explanation structure for reference when redesigning the output.

---

## Fields

| Field | Type | Used where |
|---|---|---|
| `summary` | Plain text, 2–3 sentences | Inline on card (no Markdown headers) |
| `full_explanation` | Markdown, 5 fixed sections | Expanded explanation panel |
| `sources_used` | `list[str]` | Internal citation tracking |

---

## `summary` — inline card blurb

Plain English. No Markdown. Leads with what the ML ensemble concluded and why, grounded in signal numbers and news context.

**Example (BUY signal, high conviction):**
> The XGBoost/GRU ensemble converged on a BUY for RELIANCE with 78% calibrated confidence, both models clearing their regime-adaptive thresholds. According to Economic Times (2026-06-28), a large capex announcement drove a positive sentiment lean that corroborates the technical breakout.

**Example (SELL signal, split models):**
> The ensemble leans SELL at 61% confidence, but models are split — XGBoost is decisively bearish while GRU is near-neutral. No recent news context was available to corroborate the setup.

**Example (instrument context, no active signal):**
> The ensemble currently shows a weak BUY lean at 54% confidence, just above the trending-regime threshold. Thin news coverage and below-average volume leave the read uncertain.

---

## `full_explanation` — 5-section Markdown body

The LLM is constrained to ≤110 words total across all 5 sections. Each section is 1–2 short sentences. Sections are always present even if there is nothing to say (one placeholder line).

```
### What the models saw
### Technical picture
### News context
### What this suggests
### Key risks
```

A regulatory disclaimer is **appended automatically** after the fifth section — the LLM does not write it.

---

### Section-by-section rules

| Section | What goes here |
|---|---|
| **What the models saw** | Ensemble direction + calibrated confidence, per-model (XGBoost / GRU) directions, buy/sell/hold probabilities, conviction vs. regime-adaptive threshold, agreement or disagreement |
| **Technical picture** | Scanner readings: RSI-14, volume ratio, price change %, market regime. If none provided: say so briefly |
| **News context** | Summarise retrieved articles. Each factual claim cited inline as `According to [Source Name, YYYY-MM-DD]...`. If no articles: "No recent news context was available." — never invent sources |
| **What this suggests** | Neutral synthesis of direction + confidence band + time horizon. Describe only; no advisory language |
| **Key risks** | What could invalidate the setup: model disagreement, low conviction, thin news, regime shift, etc. |

---

### Full example — suggestion explanation (BUY, strong signal)

```
### What the models saw
The ensemble returned BUY at 78% calibrated confidence (conviction 82%, well above the 45% trending-regime threshold). XGBoost: BUY, probs BUY 71% · SELL 18% · HOLD 11%, conviction 85%; GRU: BUY, probs BUY 68% · SELL 20% · HOLD 12%, conviction 79% — both models in agreement.

### Technical picture
RSI-14 at 58 (neutral-bullish territory), volume ratio 1.8× (above-average participation), price change +1.2% on the session. Market regime: trending.

### News context
According to Economic Times (2026-06-28), Reliance Industries announced a ₹75,000 crore capex plan focused on green energy and retail expansion. According to Moneycontrol (2026-06-27), FII inflows into the broader energy sector hit a 3-month high.

### What this suggests
The ML ensemble and technical picture align on a short-term upward lean with moderate-to-high conviction and a 3–5 day horizon. News sentiment provides corroborating support.

### Key risks
A reversal in FII sentiment or broader market sell-off could invalidate the setup; the signal has no stop-trigger if news tone shifts abruptly.

⚠ This is AI-generated analysis for informational purposes only and does not constitute financial advice. Past signal performance does not guarantee future results. Always conduct your own due diligence.
```

---

### Full example — suggestion explanation (SELL, split models)

```
### What the models saw
Ensemble returned SELL at 63% calibrated confidence (conviction 55%, modestly above the 40% sideways-regime threshold). XGBoost: SELL, probs SELL 68% · BUY 22% · HOLD 10%, conviction 71%; GRU: HOLD, probs SELL 48% · BUY 28% · HOLD 24%, conviction 38% — models disagree; GRU is unconvinced.

### Technical picture
RSI-14 at 67 (approaching overbought), volume ratio 0.9× (below average), price change -0.3%. Market regime: sideways.

### News context
No recent news context was available for this instrument.

### What this suggests
The bearish lean is primarily XGBoost-driven; GRU dissent and sideways regime keep conviction moderate. The 2–3 day time horizon reflects low-certainty conditions.

### Key risks
GRU disagreement is the primary risk — if it reflects a pattern XGBoost is missing, the SELL thesis could fail quickly. Below-average volume weakens the technical read.

⚠ This is AI-generated analysis for informational purposes only and does not constitute financial advice. Past signal performance does not guarantee future results. Always conduct your own due diligence.
```

---

### Full example — instrument context (no active signal)

```
### What the models saw
No active trade signal. Ensemble snapshot shows a weak BUY lean at 54% confidence, just clearing the 51% sideways-regime threshold. XGBoost: BUY at 57% conviction; GRU: HOLD at 44% conviction — no consensus.

### Technical picture
Volatility low (ATR 1.1%), market regime classified as sideways. No scanner breakout readings available.

### News context
According to Bloomberg Quint (2026-06-26), HDFC Bank's management commentary flagged stable NIMs for Q1. No other relevant articles retrieved.

### What this suggests
The instrument is in a wait-and-see state — no clear directional lean from the ensemble. Context is informational only.

### Key risks
Low conviction and sideways regime mean any signal generated here would carry high false-positive risk; thin news coverage limits corroboration.

⚠ This is AI-generated analysis for informational purposes only and does not constitute financial advice. Past signal performance does not guarantee future results. Always conduct your own due diligence.
```

---

## Guardrails (auto-applied, not LLM-written)

| Rule | What happens |
|---|---|
| Price prediction filter | Sentences matching `will reach ₹X`, `price target`, `target price`, `guaranteed return`, `certain to rise/fall`, `will definitely...` are stripped line-by-line |
| Citation check | If RAG context was provided but no `[Source Name]` citation appears in `full_explanation`, a warning is logged |
| Regulatory disclaimer | Appended unconditionally to the end of `full_explanation` |
| Summary length cap | Applied post-hoc (not a Pydantic constraint, to avoid hard generation failures with Gemini structured output) |

---

## Prohibited language (filtered or rejected)

- `"will reach ₹X"` / `"price target"` / `"target price"`
- `"guaranteed"` / `"certain to"` / `"will definitely"`
- `"you should buy/sell"` / `"recommend buying"` / `"buy now"`
