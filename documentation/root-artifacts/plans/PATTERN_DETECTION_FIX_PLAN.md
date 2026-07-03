# Pattern Detection Fix Plan

## Problem

The pattern detection system always reports **HIKKAKE** regardless of what is actually forming on the chart.

## Root Causes (4)

1. **HIKKAKE/HIKKAKEMOD are TA-Lib's most promiscuous patterns.** They fire every 2–5 candles because the criteria (failed inside bar) is extremely common price action. Across 365 days of 1D data they are near-guaranteed to have a recent signal.

2. **Selection algorithm is naive.** `max(patterns, key=lambda p: (p["confidence"], p["timestamp"]))` has no concept of pattern reliability — it treats HIKKAKE identically to Morning Star or Three White Soldiers. Since HIKKAKE fires more recently than rare-but-meaningful patterns, it almost always wins.

3. **Volume is fetched but silently discarded.** `_fetch_ohlcv` runs `SELECT ... volume` but the returned dict never includes it. Volume confirmation is the single most important signal quality filter and is completely wasted.

4. **No recency gate.** A pattern from 6 months ago can beat a 2-day-old pattern. The 365-day lookback exists to give TA-Lib sufficient warmup data, not to define signal relevance. These two concerns are conflated.

---

## Solution Architecture

### Layer 1 — Pattern Reliability Registry

Add `PATTERN_RELIABILITY: dict[str, float]` class constant on `PatternDetectionService` mapping all 61 CDL patterns to empirically-calibrated weights (0.0–1.0), grounded in academic win-rate research and trading literature.

| Tier | Score | Examples |
|---|---|---|
| High | 0.70–1.0 | `THREE_WHITE_SOLDIERS`, `THREE_BLACK_CROWS`, `MORNING_STAR`, `EVENING_STAR`, `ABANDONED_BABY`, `KICKING`, `KICKING_BY_LENGTH`, `THREE_STARS_IN_SOUTH`, `CONCEALING_BABY_SWALLOW` |
| Medium-High | 0.50–0.70 | `ENGULFING`, `PIERCING`, `DARK_CLOUD_COVER`, `HAMMER`, `SHOOTING_STAR`, `DRAGONFLY_DOJI`, `GRAVESTONE_DOJI`, `HARAMI`, `BREAKAWAY`, `LADDER_BOTTOM`, `MAT_HOLD` |
| Medium | 0.35–0.50 | `DOJI`, `CLOSING_MARUBOZU`, `MARUBOZU`, `HANGING_MAN`, `HARAMI_CROSS`, `ADVANCE_BLOCK`, `IN_NECK`, `ON_NECK`, `THRUSTING`, `SEPARATING_LINES` |
| Low (noise) | 0.10–0.25 | `HIKKAKE` (0.15), `HIKKAKE_MOD` (0.20), `SHORT_LINE` (0.15), `LONG_LINE` (0.20), `SPINNING_TOP` (0.25), `HIGH_WAVE` (0.25), `RICKSHAW_MAN` (0.25) |

### Layer 2 — Recency Gate (per timeframe)

Add `RECENCY_CANDLES: dict[str, int]` — patterns older than this window are excluded from selection entirely. TA-Lib still uses the full 365-day history for correct warmup; only the *selection* is gated.

| Timeframe | Window | Rationale |
|---|---|---|
| `1D` | 10 candles | ~2 trading weeks — balanced for swing trading |
| `1hour` | 20 candles | ~20 hours — intraday relevance |

### Layer 3 — Volume Integration

Fix `_fetch_ohlcv` to include `volume` in the returned dict (already fetched in SQL, currently discarded). Pass it through to `_detect_sync` as `np.ndarray`. Compute a rolling 20-period average volume to derive a `volume_factor`:

```
volume_factor = min(1.5, max(1.0, candle_volume / avg_vol_20))
```

Baseline 1.0 when volume is average or below; up to 1.5 when ≥1.5× the 20-period average.

### Layer 4 — Composite Scoring Formula

Replace the naive `max(confidence, timestamp)` comparator with:

```
composite_score = reliability × signal_strength × recency_decay × volume_factor
```

| Component | Values | Notes |
|---|---|---|
| `reliability` | 0.0–1.0 | From the registry |
| `signal_strength` | 0.5 or 1.0 | talib=100 → 0.5, talib=200 → 1.0 |
| `recency_decay` | 0.0–1.0 | `exp(-age_in_candles / half_life)` where `half_life = RECENCY_CANDLES / 2` |
| `volume_factor` | 1.0–1.5 | 1.0 baseline, capped at 1.5 |

**Effect on HIKKAKE:** max possible score = `0.15 × 1.0 × 1.0 × 1.5 = 0.225`. A Hammer with average volume = `0.60 × 0.5 × 1.0 × 1.0 = 0.300`. Morning Star with vol confirmation = `0.75 × 1.0 × 1.0 × 1.5 = 1.125`. HIKKAKE cannot compete against any meaningful pattern.

**Recency decay behaviour:** a pattern at candle age 0 scores `exp(0) = 1.0`; at age = half_life it scores `exp(-1) ≈ 0.368`; at age = RECENCY_CANDLES it scores `exp(-2) ≈ 0.135`, after which it is gated out entirely.

### Layer 5 — Schema + Type Update

Add `composite_score: float` to `PatternDetection` in the Pydantic schema and the TypeScript interface. This surfaces the score in the API response for transparency and debugging. No other API shape changes — `best_pattern` structure is unchanged.

---

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| 1D recency window | 10 candles (~2 weeks) | Balanced for swing trading — enough candidates without going stale |
| HIKKAKE disposition | Keep at low weight (0.15 / 0.20) | Transparent — can surface only if literally nothing else exists within the recency window |

---

## Files Touched

| File | Change |
|---|---|
| `backend/app/services/pattern_detection_service.py` | `PATTERN_RELIABILITY` registry, `RECENCY_CANDLES` config, volume in `_fetch_ohlcv`, composite scoring in `_detect_sync`, selection updated in `detect_strongest_signal` |
| `backend/app/schemas/pattern_analysis.py` | Add `composite_score: float` to `PatternDetection` |
| `frontend/src/types/analysis.ts` | Add `composite_score: number` to `PatternDetection` interface |

**No DB migrations. No API shape changes. No frontend component changes. Backward compatible.**

---

## References

- [The Predictive Power of Candlestick Patterns — Lund University](https://lup.lub.lu.se/luur/download?func=downloadFile&recordOId=8877738&fileOId=8877838)
- [Study on Bullish Reversal Candlestick Profitability — SSRN](https://papers.ssrn.com/sol3/Delivery.cfm/5755102.pdf?abstractid=5755102&mirid=1)
- [Candlestick Patterns and Volume Analysis — Wayland Quant](https://waylandz.com/quant-book-en/Candlestick-Patterns-and-Volume-Analysis/)
- [Candlestick Confirmation: Key Techniques — LuxAlgo](https://www.luxalgo.com/blog/candlestick-confirmation-key-techniques/)
