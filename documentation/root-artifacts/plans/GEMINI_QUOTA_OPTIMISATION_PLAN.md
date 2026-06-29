# Gemini Quota Optimisation — Priority Rebalancing + Heuristic Pre-filter

## Background

All four Gemini API keys are circuit-open (daily quota exhausted) as of 2026-06-25.
Root cause: event classification (MEDIUM priority) and watchlist context generation (LOW
priority) are inverted relative to user-facing value. Event classification fires on every
ingested RSS article and drains quota before context generation (the visible watchlist panel)
gets a turn. Additionally, ~90% of articles contain clear category keywords that make a
full Gemini call unnecessary.

---

## Diagnosis

After adding the 4th Gemini key, only 1 explanation was generated before quota exhausted:

| Priority | Operation | Count | Result |
|----------|-----------|-------|--------|
| HIGH | Trade suggestion explanation | 1 | ✅ Written to DB |
| MEDIUM | Event classification + other medium ops | 7 succeeded, 4 failed | ✅ / ❌ quota |
| LOW | Instrument context generation (watchlist) | 33 | ❌ All quota-exhausted |

Event classification at MEDIUM starved watchlist context generation (LOW) of quota entirely.

---

## Constraints & Invariants

- `classify()` in `event_classifier.py` contains a branch at line 369: if `_classify_with_ollama()`
  returns `confidence < 0.7`, the GPT-4o fallback branch runs. GPT-4o is currently a no-op
  (delegates to rule-based), but still executes. The heuristic pre-filter **must return
  `confidence = 0.75`** (not the rule-based 0.60) to bypass this branch cleanly.

- `_classify_rule_based()` must **not be modified** — it is the error-path fallback (called from
  the `except` block in `_classify_with_ollama()`) and correctly signals failure via `confidence = 0.60`.

- The `except Exception` error fallback in `_classify_with_ollama()` must **never be cached** —
  it represents a transient Gemini failure, not a genuine classification result.

- Cache key scheme `cortex:event_class:{sha256(content[:1500])}` must remain unchanged.

- `classify()` symbol-merging logic (pass A + B + C) must remain untouched. The heuristic
  path weakens LLM symbol extraction (pass A) for keyword-matched articles, but pass B
  (content-level ALL-CAPS + corporate-suffix regex extraction) runs unconditionally in
  `classify()` and compensates.

---

## Changes — 3 Files

### File 1 — `backend/app/ai/intelligence/explanation_worker.py`

**Edit 1 — line 1163**

```
- priority=Priority.LOW,
+ priority=Priority.MEDIUM,
```

Watchlist / instrument context generation bumped to MEDIUM. One line, no other changes.

---

### File 2 — `backend/app/core/metrics.py`

**Edit 1 — append after last metric (`watchlist_scheduler_last_run_timestamp`)**

Add a new Counter for full classification pipeline observability:

```python
event_classification_total = Counter(
    'event_classification_total',
    'Event classification outcomes by resolution method',
    ['method'],  # cache | heuristic | llm | rule_based_fallback
)
```

Label semantics:

| Label value | Meaning |
|-------------|---------|
| `cache` | Redis cache hit — zero Gemini cost |
| `heuristic` | Keyword pre-filter matched — Gemini call skipped |
| `llm` | Actual Gemini call made (ambiguous / "general" articles) |
| `rule_based_fallback` | Gemini failed — fell back to deterministic rule-based (error path) |

---

### File 3 — `backend/app/ai/intelligence/event_classifier.py`

**Edit 1 — module docstring**

Update the three-level fallback chain to four levels:

```
1. Redis cache        — zero-cost, 30-min TTL, keyed by content hash
2. Heuristic filter   — keyword pre-filter; skips Gemini for unambiguous event types
3. LLM (Gemini)       — full structured classification for ambiguous / "general" articles
4. Rule-based         — deterministic fallback when Gemini fails (error path only)
```

**Edit 2 — constants block (lines 71–77)**

Remove `_EVENT_CLASS_CACHE_MIN_CONFIDENCE = 0.7` (no longer referenced).
Update cache comment to remove "high-confidence results only" language:

```python
# LLM classification cache — keyed by SHA-256 of event content[:1500].
# Events do not change after ingestion; a 30-min TTL eliminates duplicate
# Gemini calls across worker restarts and pipeline re-runs.
# Heuristic (keyword-matched) and LLM results are both cached unconditionally.
# The error-path fallback (confidence=0.0) is never cached.
_EVENT_CLASS_CACHE_TTL_SECS: int = 1800
```

**Edit 3 — imports**

Add `event_classification_total` to the metrics import.

**Edit 4 — `_classify_with_ollama()` full rewrite**

New execution flow:

```
1. Guard (use_llm / client check)           — unchanged
2. Cache check                              — unchanged; increment event_classification_total[cache]
3. [NEW] Heuristic pre-filter               — if _detect_event_type() != "general":
                                               build result inline with confidence=0.75,
                                               cache unconditionally, increment [heuristic], return
4. Gemini call                              — priority: MEDIUM → LOW; increment [llm]
5. Cache write (unconditional on success)   — removed confidence >= 0.7 gate
6. Return result
7. except block                             — increment [rule_based_fallback]; NOT cached
```

Heuristic result dict structure (built inline, not via `_classify_rule_based`, to avoid
double-computing `_detect_event_type`):

```python
{
    "event_type":       event_type,            # from _detect_event_type()
    "impact_score":     impact_score,          # from _score_impact()
    "confidence":       0.75,                  # honest: reliable type, weaker symbol extraction
    "affected_symbols": (entities.get("companies") or [])[:3],
    "sentiment":        sentiment,             # from _detect_sentiment()
    "reasoning":        f"Heuristic: keyword match for {event_type} — Gemini call skipped",
    "decay_hours":      fast_hl,
    "decay_slow_hours": slow_hl,
}
```

The `confidence = 0.75` is intentional:
- Signals "reliable event_type classification, moderate symbol confidence"
- Keeps result above the `< 0.7` GPT-4o trigger in `classify()` — clean path
- Distinguishable from error fallback (0.60) and full LLM results (0.75–0.95)

Updated `_classify_with_ollama()` docstring to describe the four-path flow.

---

## Priority Table After Implementation

| Priority | Operation | Direction |
|----------|-----------|-----------|
| HIGH | Trade suggestion explanation | Unchanged |
| MEDIUM | Instrument context generation (watchlist + SSE Stage 3) | ↑ from LOW |
| LOW | Event classification — Gemini path only (~10% of articles) | ↓ from MEDIUM |
| — | Event classification — cache + heuristic paths | Zero Gemini cost |

---

## Estimated Quota Impact

| Path | Before | After |
|------|--------|-------|
| Event classification Gemini calls | 100% of articles | ~10% (only "general" type) |
| Context generation priority vs classification | Below (starved) | Above (wins) |
| Explanation priority | Highest | Unchanged |

The ~10% estimate comes from `_detect_event_type()` returning `"general"` only when none
of 7 category keyword groups match. Most financial news articles contain at least one of:
earnings/revenue/profit, merger/acquisition, RBI/FOMC/repo rate, SEBI/regulatory,
GDP/CPI/PMI, Nifty/Sensex/FII, or geopolitical terms.

---

## What Is NOT Changing

- `_classify_rule_based()` — untouched; still the error fallback, still returns `confidence = 0.60`
- `classify()` — no changes; the `0.75` heuristic confidence keeps it on the happy path,
  bypassing the GPT-4o fallback branch
- `_classify_with_gpt4o()` — untouched (reserved stub)
- All other Gemini callers — untouched
- Symbol merging in `classify()` (pass A + B + C) — untouched

---

## Open Questions — Requires Decision Before Implementation

**Q1 — Heuristic confidence value**

The plan uses `0.75`. This is the minimum value that clears the `< 0.7` GPT-4o trigger
in `classify()`. Is `0.75` the right value, or should it be set higher (e.g., `0.80`)
to reflect that a keyword match is genuinely reliable for event_type classification?
The difference is cosmetic (does not affect code flow) but affects the
`classification_confidence` value stored in `ai_event_classifications`.

**Q2 — Low-confidence Gemini results TTL**

Articles that reach the Gemini call (i.e., `_detect_event_type()` returned `"general"`)
and come back with low confidence (e.g., `< 0.6`) are currently not cached (removed by
this plan's Edit 2). Should these get a shorter TTL (e.g., `900s` / 15 min) rather than
the full `1800s`, to allow a retry sooner for genuinely ambiguous content? Or is uniform
`1800s` for all successful Gemini results acceptable?

**Q3 — Keyword list expansion**

`_detect_event_type()` covers 7 categories. Several common Indian market event types
are not covered and will fall through to Gemini unnecessarily:

| Missing category | Example keywords |
|-----------------|-----------------|
| IPO / FPO / listing | "ipo", "fpo", "listing", "subscri", "allotment" |
| Dividend / buyback | "dividend", "buyback", "buy-back", "bonus share" |
| AGM / board meeting | "agm", "egm", "board meeting", "board approved" |
| Credit rating | "credit rating", "downgrade", "upgrade", "moody", "crisil", "icra" |
| Insider trading | "insider trading", "promoter", "bulk deal", "block deal" |

Should these be added to `_detect_event_type()` as part of this work, or deferred to a
separate improvement? Adding them here increases heuristic coverage from ~90% to ~95%+
at no extra cost.
