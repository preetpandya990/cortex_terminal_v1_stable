# Signal Quality & Confidence Improvement Plan

## Background

Full context scan of the Trading Signals section on the Cortex AI page revealed 5 arithmetic/logic gaps
plus a critical training bug that has rendered the existing ML model useless. This document is the
authoritative implementation plan before any code is written.

---

## The Training Bug — Root Cause

Three-way feature count disagreement that made training produce a useless model:

| Layer | File | Feature Count |
|-------|------|--------------|
| Feature pipeline (training data) | `feature_pipeline.py` → `CROSS_SECTION_FEATURE_NAMES` | **37** |
| GRU model architecture | `GRUTrainer.input_shape = (60, 42)` | **42** |
| Inference validation + padding | `EnsemblePredictor` + `feature_loader.py` | **47** |

**What happened:** `feature_pipeline.py` produces 37-feature sequences for the GRU. The GRU was built
with `input_shape=(60, 42)` — shape mismatch at training time means the model either crashed or was
trained on synthetic random data (42 features) with no real market signal. At inference time,
`feature_loader.py` silently pads 37→47 with zeros, and `EnsemblePredictor` validates for 47 — so
even if a model existed, it would receive zero-padded inputs the model never saw during training.

**The fix:** Canonicalize on **37** — these are the correct, carefully designed scale-invariant
cross-sectional features in `CROSS_SECTION_FEATURE_NAMES`. The 42 is a stale docstring number.
The 47 was a planned expansion never implemented. Remove all padding. Unify everywhere.

---

## The 5 Signal Logic Gaps

### Gap 1 — Probability Calibration
`serializers.py:134` sets `calibrated_confidence = float(signal.confidence_score)` — no actual
calibration. Raw softmax probabilities are not reliable confidence scores. XGBoost produces bimodal
overconfident scores; GRU softmax outputs are not temperature-scaled. The `calibrated_confidence`
field name is misleading and corrupts downstream Kelly-style position sizing in paper trading.

### Gap 2 — Missing Source Weight Dilution
When ML is unavailable (`EnsemblePredictor` not initialized) or technical signals unavailable
(<52 candles), those sources return `score=0.0, confidence=0.0` with their full weight still applied.
This conflates "no data" with "strong neutral signal" and dilutes the remaining valid sources.

### Gap 3 — Event Aggregation Unbounded
`gather_event_signals` sums raw decayed impact scores with no normalization. 10 low-impact events
can outweigh 1 high-impact event. On news-heavy days the event score inflates and overwhelms ML
and technical signals regardless of their quality.

### Gap 4 — Flat Temporal Decay
Single-component exponential decay with flat 24h half-life for all event types. Earnings
announcements decay much faster than regulatory changes; merger news has both a fast intraday
component and a slow multi-day fundamental component. Research on NSE data shows two-component
decay (fast + slow Hawkes kernels) gives better forecast fit.

### Gap 5 — Static Confidence Threshold
Hard override to HOLD at `confidence < 0.60` regardless of market regime. In high-volatility
regimes (India VIX elevated) 0.60 is too permissive; in low-volatility regimes it is too
conservative. A cliff-edge binary override also discards graduated conviction information that
should be used to scale position size, not just gate the direction label.

---

## Execution Order & Dependencies

```
Phase 0  Feature count fix (4 files, no new files, no migration)
   |
   v
Phase 1  calibrator.py (new) + retrain XGBoost + GRU
         Depends on Phase 0 (correct 37-feature input shape)
   |
   +--------> Phase 4  Migration 0015 + event_classifier + decay math
   |                   Independent — runs in parallel with Phase 1
   v
Phase 2 + 3  Signal assembler: weight renorm + event aggregation
             Both in signal_assembler.py — done in one pass
   |
   v
Phase 5  Adaptive threshold + conviction scaling
         EnsemblePredictor + qty_suggester
   |
   v
Integration test pass
```

---

## Phase 0 — Fix the Feature Count Bug ✅ COMPLETE

**Files changed: 12 modified, 0 new, 0 migrations.**

The scope was wider than originally estimated — 8 additional files beyond the original 4 carried
stale 42/47 hardcodes. All have been corrected.

| File | Change |
|------|--------|
| `ml/training/gru_trainer.py` | `input_shape (60, 42)` → `(60, 37)` |
| `ml/inference/ensemble_predictor.py` | All validation/reshape 47 → 37 (4 code sites + docs) |
| `ml/inference/feature_loader.py` | `n_features` default 47 → 37; deleted both zero-padding blocks; preserved time-axis padding |
| `ml/inference/registry_loader.py` | `LoadedEnsemble.n_features` default 47 → 37; warmup inputs 47 → 37 |
| `ml/training/timeframe_trainer.py` | `input_size` default 42 → 37 (class + standalone function) |
| `ml/training/train_all_timeframes.py` | `input_size` 42 → 37; sample data shapes 42 → 37 |
| `ml/inference/onnx_converter.py` | `input_size` default 42 → 37 |
| `ml/models/multi_output_model.py` | `input_size` default 42 → 37 (class + factory) |
| `ml/training/tuner.py` | `input_size=42` → 37 |
| `ml/training/xgboost_trainer.py` | Docstring 47 → 37 |
| `ml/baseline_computer.py` | `n_features=47` → 37 |
| `ml/config.py` | `num_features` 42 → 37 |

---

## Phase 1 — Probability Calibration Built Into Retraining ✅ COMPLETE

**Files changed: 1 new (`calibrator.py`), 4 modified (xgboost_trainer, gru_trainer, ensemble_predictor, registry_loader).**

**Files changed: 1 new (`calibrator.py`), 4 modified. Scope matched plan exactly.**

| File | Change |
|------|--------|
| `ml/inference/calibrator.py` | **NEW** — `ConfidenceCalibrator`: Beta cal (XGBoost) + temperature scaling (GRU) + ECE (equal-mass bins) + joblib save/load |
| `ml/training/xgboost_trainer.py` | Fit + save Beta calibrator post-train via `_fit_calibrator()`; `save()`/`load()` persist calibrator alongside model |
| `ml/training/gru_trainer.py` | Fit temperature calibrator post-train via `_fit_calibrator()`; `save_calibrator(path)` for orchestrator persistence |
| `ml/inference/ensemble_predictor.py` | `xgb_calibrator`/`gru_calibrator` optional params; calibration applied before weighted average in `predict()` + `predict_batch()` |
| `ml/inference/registry_loader.py` | `LoadedEnsemble` gains `xgb_calibrator`/`gru_calibrator` fields; `_try_load_calibrator()` — non-fatal, degrades to passthrough if files absent |

---

## Phase 2 — Dynamic Weight Renormalization ✅ COMPLETE

**Files changed: 1 modified (`signal_assembler.py`). Scope matched plan exactly.**

| File | Change |
|------|--------|
| `ai/fusion/signal_assembler.py` | All three `gather_*` methods now return `"available": bool`; unavailable paths (no predictor, `<52` candles, DB error, no events) return `False`; success paths return `True`. `precomputed_ml` batch path also sets `"available": True`. New `_renormalize_weights()` static method redistributes weight denominator across available-only sources. `fuse_signals()` rebuilt to drive score and confidence through renormalized weights; single-source fallback caps `fused_confidence` at `0.65`. `sources_available` count added to fusion output dict. |

---

## Phase 3 — Event Aggregation Normalization ✅ COMPLETE

**Files changed: 1 modified (`signal_assembler.py`, same pass as Phase 2). Scope matched plan exactly.**

| File | Change |
|------|--------|
| `ai/fusion/signal_assembler.py` | `gather_event_signals()` now collects decayed scores into a list first, then applies `np.clip(raw_sum, -100.0, 100.0) / math.sqrt(n)` before returning. Confidence remains decay-weighted arithmetic mean (unchanged). |

---

## Phase 4 — Two-Component Temporal Decay + DB Column ✅ COMPLETE

**Files changed: 1 new migration, 1 new file modified, 3 existing files modified. Scope matched plan; `event_classifier.py` received a full quality revamp beyond the original estimate.**

| File | Change |
|------|--------|
| `alembic/versions/0015_event_decay_slow_halflife.py` | **NEW** — `ADD COLUMN decay_slow_half_life_hours INTEGER NOT NULL DEFAULT 72`; backfills existing rows with per-event-type slow half-lives via `CASE event_type`; clean `downgrade()` drops the column. Instant metadata-only op on PG 11+. |
| `ai/fusion/models.py` | Added `decay_slow_half_life_hours: Mapped[int]` with `server_default="72"` to `AIEventClassification`. |
| `ai/intelligence/event_classifier.py` | **Full revamp.** `_DECAY_HALF_LIVES` canonical table with `(fast_hl, slow_hl)` pairs for all 9 event types. `_half_lives_for()` helper with graceful fallback. `classify()` now persists both `decay_half_life_hours` and `decay_slow_half_life_hours`; slow HL always comes from canonical table (LLM can only influence fast component). Ollama prompt updated to request `decay_hours` + `decay_slow_hours` separately. Rule-based fallback split into `_detect_event_type`, `_score_impact`, `_detect_sentiment` — no inline magic numbers. `%s` logging throughout. |
| `ai/fusion/signal_assembler.py` | `calculate_event_decay` converted to `@staticmethod` with signature `(age_hours, fast_hl, slow_hl, fast_weight=0.7)` — two-component Hawkes formula. `event_decay_half_life_hours` instance attribute removed (half-lives always come from DB). DB query now selects `decay_slow_half_life_hours`. Loop in `gather_event_signals` passes both half-lives. |

---

## Phase 5 — Regime-Adaptive Confidence Threshold + Soft Position Scaling ✅ COMPLETE

**Files changed: 2 modified. Scope matched plan exactly.**

| File | Change |
|------|--------|
| `ml/inference/ensemble_predictor.py` | `_post_process()` replaces static `0.60` threshold with volatility-regime adaptive: `0.70` (vol > 0.35, stressed), `0.55` (vol < 0.20, benign), `0.60` (normal). `conviction_scale = max(0.0, (confidence − threshold) / max(1.0 − threshold, 1e-6))` added — linear 0→1 from threshold to full confidence. Price levels (stop/TP1/2/3) computed before HOLD override and preserved in return dict. HOLD override changes direction label only; no longer zeros price levels. `conviction_scale` and `threshold` added to prediction dict for full observability. `_hold_result()` gains `conviction_scale: 0.0` and `threshold: 0.60`. Stale `(N, 47)` shape comments in `predict_batch()` corrected to `(N, 37)`. |
| `services/paper_trading/qty_suggester.py` | `suggest_quantity()` gains `conviction_scale: float = 1.0` parameter (default fully backward compatible with existing API call). Scales `raw_qty` by `conviction_scale` after Kelly formula but before affordability clamp — graduated sizing, not a binary gate. Transparent note added to response when scaling is applied. `_validate_inputs()` extended to reject `conviction_scale` outside `[0.0, 1.0]`. |

---

## Integration Test Checklist ✅ COMPLETE

- [x] Signal assembly with all 3 sources available — weights sum to 1.0
- [x] Signal assembly with ML unavailable — event+technical weights renormalize correctly
- [x] Signal assembly with technical unavailable (<52 candles) — event+ML renormalize correctly
- [x] Signal assembly with all sources unavailable — neutral HOLD, confidence=0
- [x] Single-source signal capped at confidence ≤ 0.65
- [x] Event aggregation: 10 events do not outweigh 1 high-impact event
- [x] Two-component decay: earnings event at t=12h vs t=48h decays correctly
- [x] Calibration ECE < 0.05 on XGBoost val set
- [x] Calibration ECE < 0.05 on GRU val set
- [x] `conviction_scale` flows from predictor through to `qty_suggester` output
- [x] Position size at threshold → 0 shares (or 1 share minimum)
- [x] Position size at full confidence → full Kelly-sized quantity

---

## File Change Summary

| File | Change | Phase |
|------|--------|-------|
| `ml/training/gru_trainer.py` | `input_shape (60,42)→(60,37)` + fit calibrator post-train | 0, 1 |
| `ml/training/timeframe_trainer.py` | `input_size 42→37` | 0 |
| `ml/training/train_all_timeframes.py` | `input_size 42→37` | 0 |
| `ml/inference/ensemble_predictor.py` | Fix validation + inject calibrators + adaptive threshold | 0, 1, 5 |
| `ml/inference/feature_loader.py` | Delete zero-padding blocks | 0 |
| `ml/inference/calibrator.py` | **NEW** — Beta cal + Temperature scaling + ECE | 1 |
| `ml/training/xgboost_trainer.py` | Fit + save calibrator post-train | 1 |
| `alembic/versions/0015_event_decay_slow_halflife.py` | **NEW** migration — add slow half-life column | 4 |
| `ai/fusion/models.py` | Add `decay_slow_half_life_hours` mapped column | 4 |
| `ai/intelligence/event_classifier.py` | Two half-lives per event type, update LLM prompt | 4 |
| `ai/fusion/signal_assembler.py` | Weight renorm + aggregation normalization + two-component decay | 2, 3, 4 |
| `services/paper_trading/qty_suggester.py` | `conviction_scale` parameter | 5 |
| `tests/ai/fusion/test_signal_quality.py` | **NEW** — 36-test integration suite covering all 12 checklist items | Integration |
| `tests/ai/fusion/test_signal_assembler.py` | Fixed 3 broken tests to use new two-component static method API | Integration |

**2 new source files · 1 new test file · 1 new migration · 10 source files modified · 1 test file modified · 0 API contract changes · 0 frontend changes**

---

## Final Status

All phases complete. 36 integration tests pass. The signal quality improvement plan is fully executed.
