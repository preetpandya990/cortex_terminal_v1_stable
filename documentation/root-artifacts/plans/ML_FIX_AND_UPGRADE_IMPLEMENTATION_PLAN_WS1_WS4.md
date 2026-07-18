# ML Fix & Upgrade — Implementation Plan (WS1–WS4)

**Deadline:** Sat 2026-07-18 20:02 IST — systemd timer launches `scripts/scheduled_retrain.py`. All code + validation done before Sat noon. All paths under `backend/`.

## Context

Saturday's timer-launched run must train the v1.2.0 challenger on fixed code. Verified problems:

1. **All 20 fundamental features are dead**: computed once as-of `end_date`, broadcast as a constant per symbol (`feature_pipeline.py:151-160`), and rolling z-score maps any constant column to exactly 0 (`normalize_features`, std<1e-8→1.0). The "69-feature model" is 49 informative + 20 zeros. Fixing normalization alone exposes look-ahead leakage (today's values stamped onto 10y of history), so point-in-time correctness must land together.
2. **The scheduled run crashes on a poisoned session**: forensics on `logs/scheduled_retrain/scheduled_retrain_20260715T153428Z.log` show an asyncpg `ConnectionDoesNotExistError` mid-query (sentiment 3-table JOIN, symbol 803/2234, ~5.5h in), swallowed inside `sentiment_features` without rollback → `PendingRollbackError` cascade for every subsequent symbol (`compute_features_batch` except at `feature_pipeline.py:220-222` has no `rollback()`).
3. **The scheduled path never uses feedback weights**: `scheduled_retrain.py:61-64` passes only `--fresh`; the orchestrator fully supports `--feedback-weights` (CLI :3370-3380, non-fatal load :1428-1454).
4. **Registry projection drift**: `ai_ml_models` has no live rows — the manual 1.1.1 stamp bypassed `ModelPromoter`/`_project_to_ai_ml_models`; serving (reads `ml_model_metadata`) unaffected, governance blind.
5. **Critical discovery**: `feature_version` passed to `register_model` is **silently dropped** — `ml_model_metadata` has no such column (`app/models/ml_data.py:113-159`; `MLModel` ctor `model_registry.py:1044-1059`). Version-gated inference requires new persistence.
6. **Critical discovery**: 5 of the 20 fundamentals (`pe_ratio, pb_ratio, roe, roce, ev_ebitda`) come from `CompanyKeyRatios` — a single upserted current-snapshot row, no `period_date` (Upstox /key-ratios serves current only). They cannot be made point-in-time from existing data.

## User decisions (locked)

- **Hybrid ratio handling**: reconstruct `roe` (= net_profit/net_worth) and `roce` (documented approximation from balance-sheet columns, verified against real data Thu AM) point-in-time from statements; **drop `pe_ratio, pb_ratio, ev_ebitda`** from v2.0.0 → feature set = 44 technical + 5 sentiment + 17 fundamentals = **66 features**.
- **Pre-2022 history gap** (Upstox statements ~4y deep): NaN → cross-sectional median → rank 0 (neutral). Standard Gu–Kelly–Xiu treatment; fundamentals carry full per-date cross-sectional weight wherever data exists.

## Global design

- **`FUNDAMENTAL_FEATURE_NAMES_V2`** (17 names) added alongside the untouched legacy 20-name list in `fundamental_features.py`. A single config switch **`ML_FEATURE_SET_VERSION`** (default `"2.0.0"`) in `app/ml/config.py` selects v2 everywhere in training — this is the WS2 panic-button revert lever.
- **Inference gating source of truth** = the loaded model's persisted `feature_version` (new column), NOT the config flag — live 1.0.0 models keep exact legacy behavior no matter what training does.
- **Feature store stays raw and keeps `FEATURE_VERSION = "v1.0"` unchanged** (`app/ml/features/feature_store.py:24`). Bumping it would starve Tier-1 inference of its 60-row history until a full backfill (loader queries by version). v2 rows simply stop containing pe/pb/ev keys and hold PIT-varying values; JSONB + manifest zero-fill handles the superset/subset both ways.
- **v1.0.0 model gate = hard-zero the 20 fundamental columns** in `FeatureLoader` (post column-selection, excluded from z-score). Their trained distribution is exactly 0; relying on "z-score of a constant → 0" breaks once store rows hold PIT-varying values.
- Rank normalization: per-date cross-sectional percentile rank mapped to [-1, 1] (Gu–Kelly–Xiu); missing → 0. Inference parity via persisted **101-point quantile grids** per (date, feature).
- One migration **`alembic/versions/0056_ml_feature_v2.py`**: `feature_version` column + backfill `'1.0.0'` + new `ml_feature_cross_stats` table.

---

## WS1 — Survival fix (Wed night, first)

**Pattern: session-per-chunk + per-symbol rollback + one fresh-session retry on transient connection errors.** Rollback-only fixes the cascade but a 5.5h loop on one logical session stays fragile; `scripts/compute_production_features.py:process_batch` (226-262) already proves session-isolation survives multi-hour runs.

1. `app/ml/features/feature_pipeline.py` — `compute_features_batch`:
   - New optional params `session_factory: Optional[async_sessionmaker] = None`, `chunk_size: int = 50`.
   - Legacy path (no factory): add `await db.rollback()` (itself wrapped in try/except — rollback can raise on a dead connection) at the top of the except block (:220-222).
   - Factory path (orchestrator): chunk symbols (2234 → ~45 sessions); per-symbol except → rollback; on transient connection error (`_is_transient_connection_error(exc)` helper: asyncpg `ConnectionDoesNotExistError`, SA `OperationalError`/`InterfaceError`/`DBAPIError.connection_invalidated`) abandon the chunk session, open fresh, retry the failed symbol **once**, continue chunk on new session; second failure → skip (existing semantics).
2. `app/ml/features/sentiment_features.py` — the internal broad except that swallowed the root error: `await db.rollback()` before returning defaults, and **re-raise** transient connection errors so the batch-level retry can act.
3. `scripts/production_training_orchestrator.py` step 2 (~:679): pass the app's `async_sessionmaker` as `session_factory`.
4. Confirm `--fresh` initializes cleanly against the now-empty `models/production/checkpoints/`.

## WS2a — Point-in-time fundamental series (Thu AM)

`app/ml/features/fundamental_features.py`:

- `FUNDAMENTAL_FEATURE_NAMES_V2` (17) + `get_fundamental_feature_names(version="1.0.0")` dispatch (legacy default keeps all existing callers byte-identical).
- `FUNDAMENTAL_REPORTING_LAG_DAYS = 90`: a statement becomes known at `period_date + 90d` (`effective_date`). The legacy single-date function's no-lag `period_date <= as_of` is mild lookahead — v2 fixes it; documented divergence.
- **New** `async def compute_fundamental_features_series(instrument_key, start_date, end_date, db) -> pd.DataFrame` (`effective_date` + 17 columns, sorted asc):
  - 4 bulk queries (reuse filters at :160-169, :202-210, :237-245, :258-265 minus the as-of cutoff; no key-ratios query). ~4 queries/symbol instead of 40× per-date calls.
  - Vectorized per-period features in pandas: yoy via `shift(1)` (prior==0 → NaN, reuse `_yoy_growth` semantics), CAGR via shift matching the current implementation's horizon, `operating_margin(_avg)`, `debt_ratio(_trend)` via rolling `_linear_slope`, `net_worth_log/cagr`, shareholding diffs, **`roe = net_profit/net_worth`**, **`roce`** per Thu-AM verified identity (default proxy `op_profit/total_asset`, documented — rank normalization makes only cross-sectional ordering matter).
  - Merge the 4 statement frames on `period_date` (outer, sorted, per-source ffill), set `effective_date = period_date + lag`. No imputation — NaN flows to rank pass.
- `compute_features_for_symbol` (:151-160): under v2, replace the single-shot broadcast with `pd.merge_asof(features_df, series, direction="backward")` on a tz-normalized key (OHLCV timestamps are tz-aware UTC → temp naive normalized key; `effective_date` → datetime64[ns]; both sorted — merge_asof raises otherwise). Rows before first effective_date stay NaN. Legacy NaN-fill exclusion of fundamentals (:164-167) unchanged.

## WS2b — Cross-sectional rank normalization (Thu PM)

**New module `app/ml/features/cross_sectional_stats.py`** (pure core, testable without DB):

- `rank_normalize_panel(results: Dict[str, DataFrame], feature_names) -> Dict[date, Dict[str, CrossStat]]` — long panel, `groupby(date).rank(pct=True)` → `2*pct − 1`, NaN → 0.0, write back in place; returns per-date `CrossStat(quantiles: 101 raw-value floats, median, n_obs)` for persistence. Edges: n_obs ≤ 1 or all-equal → all ranks 0.
- `rank_transform_with_grid(raw, stat) -> float` — `np.interp` over the grid → [-1, 1], clip; NaN/missing stat → 0.0. **The same primitive is unit-tested for parity against `rank_normalize_panel`.**
- `persist_cross_sectional_stats(...)` / `load_cross_sectional_stats(...)` — upsert/read `ml_feature_cross_stats`.

Integration: in `compute_features_batch`, under v2 **replace** the median-imputation block (:232-258) with `rank_normalize_panel(results, FUNDAMENTAL_FEATURE_NAMES_V2)`; legacy block kept verbatim under v1. Docstring updated.

Training-side exclusion: orchestrator helper `_zscore_feature_cols(feature_names)` used at all three `normalize_features` call sites (:1553, :1900, :2005) — under v2 excludes the 17 fundamentals from rolling z-score (they arrive rank-normalized in [-1,1]).

## WS2c — Version-gated inference parity (Thu eve)

**Migration `0056_ml_feature_v2.py`** (+ ORM in `app/models/ml_data.py`):
- `ml_model_metadata.feature_version VARCHAR(16)` nullable + backfill `'1.0.0'`.
- `ml_feature_cross_stats(id, as_of_date DATE, feature_name VARCHAR(64), feature_version VARCHAR(16), quantiles JSONB, median FLOAT, n_obs INT, created_at)`, unique `(as_of_date, feature_name, feature_version)`, index `(feature_name, as_of_date)`.
- Check `alembic/versions/` for a numbering race before Friday's prod upgrade.

**Gating chain:**
1. `app/ml/model_registry.py` `register_model` (:1044-1059): persist the already-accepted `feature_version` param on the `MLModel` row + into lineage.
2. Orchestrator literals `"1.0.0"` (:2908, :2941; also `train_all_timeframes.py:192,254`) → `ML_FEATURE_SET_VERSION` from config.
3. `app/ml/inference/registry_loader.py`: `LoadedEnsemble.feature_version` populated near :314-330 (NULL → `"1.0.0"`); **hard-fail `ModelLoadError` if XGB/GRU versions differ** (same spirit as the n_features check).
4. `app/ml/inference/feature_loader.py`: `FeatureLoader` gains `feature_version` + lazy grid cache.
   - **v1.0.0 models**: after manifest column selection (:316-330), force all 20 legacy fundamental columns to 0.0 and exclude them from `normalize_features` feature_cols — exact trained distribution, deterministic regardless of store contents.
   - **v2.0.0 models**: async pre-step `_apply_rank_transform(features_df, fund_cols)` before the sync `_prepare_features` (called from both Tier-1/Tier-2 call sites): load grids for the frame's date range, map each row's raw value through the nearest grid with `as_of_date ≤ row date` (searchsorted); no grid at all → 0.0 + loud warning. Fundamentals excluded from z-score.

**Grid write path** (daily refresh): new `persist_cross_sectional_pass(as_of)` on `FeatureComputationPipeline` (`scripts/compute_production_features.py`), called after the per-symbol loop — **re-reads** today's raw fundamental values from `ml_features` across the universe (robust to partial-loop crashes / future parallelism), builds and persists grids. Non-fatal on failure (read path degrades to the newest earlier grid).

**Version bumps:** checkpoint `SCHEMA_VERSION` 4 → 5 (`app/ml/training/checkpoint_manager.py:103`); add `feature_version` + `n_symbols` to `_MODEL_AFFECTING_KEYS` (:928-933) so a smoke checkpoint can never resume into a full run. Verify `app/api/v1/admin_training.py:585` handles stale old run dirs gracefully.

## WS3 — Feedback weights in scheduled path (Fri AM)

`scripts/scheduled_retrain.py` — **subprocess** invocation of the existing `scripts/build_feedback_weights.py` (matches `_tee_subprocess` isolation philosophy; builder crash must never kill the retrain; exit codes 0/1/2 already defined):
- `_build_feedback_bundle(cwd, log_dir) -> int` — tee to `feedback_build_<stamp>.log`.
- `_select_newest_bundle(dir, max_age_days=7) -> Path | None` — newest `*.parquet` **with sibling `.meta.json`** (reject orphans), reject stale (prevents silently training on week-old weights when tonight's build exits 1).
- `main()`: if new config `SCHEDULED_RETRAIN["enable_feedback_weights"]` (default True): run builder; regardless of exit code select newest bundle; if found append `("--feedback-weights", path)` to the orchestrator command. Builder failure → WARN + train unweighted; only the retrain's exit code drives 0/1/2.
- `app/ml/config.py` (:324-348): add `enable_feedback_weights`, `feedback_bundle_dir`, `feedback_bundle_max_age_days`.

## Smoke-run CLI (Fri AM)

Orchestrator `main()`: add `--n-symbols INT` (cap after step-1 selection) and `--symbols "A,B,C"` (explicit deterministic list). No other behavior change.

## WS4 — Hygiene (Thu eve / Fri, nothing blocks the run)

1. Harden `_project_to_ai_ml_models` (`model_registry.py:62-106`): `rowcount == 0` → **upsert/INSERT** instead of warning (the warning-only path is how the table went dark).
2. New `scripts/repair_ai_ml_models_projection.py --dry-run/--execute`: upsert `ai_ml_models` rows for every active/production `ml_model_metadata` row. Run dry-run → execute once.
3. New `scripts/purge_test_model_rows.py --dry-run/--execute`: delete `ai_ml_models` rows matching `test_drift_model_%` (+ dead `lstm`); list first, sanity cap. Run **before** the projection repair.
4. Fusion threshold `app/ai/fusion/signal_assembler.py:853-858`: `> 50` → `>= 50`, `< -50` → `<= -50` + boundary test.

---

## Tests (4 gate tests + supporting)

All under `backend/tests/`, asyncio_mode=auto, AsyncMock sessions per existing ML-unit convention (`tests/ml/test_regression_e1.py` patterns for the version gate):

1. **`tests/unit/test_ml_feature_pipeline_resilience.py`** — symbol 2/4 raises `ConnectionDoesNotExistError`: rollback awaited; symbols 1,3,4 survive; factory path retries once on a fresh session (factory call count), second failure skips; sentiment except rolls back and re-raises transient errors.
2. **`tests/unit/test_ml_fundamental_series.py`** — fixture statement rows: PIT correctness (value on `period_date+lag−1` comes from the previous period, flips at `+lag` — mutating a future quarter never changes earlier rows); roe/roce formulas exact; pe/pb/ev absent; yoy prior==0 → NaN; tz-aware input frame merge_asof works.
3. **`tests/unit/test_ml_rank_normalization.py`** (pure) — hand-computed ranks; NaN → 0; all-equal → 0; **grid round-trip parity** (`rank_transform_with_grid` reproduces in-sample panel ranks within tolerance); out-of-range clips to ±1; missing grid → 0. Plus: fundamentals have **non-zero variance in the final training matrix** end-to-end through `normalize_features` exclusion.
4. **`tests/unit/test_ml_feature_version_gate.py`** — `register_model` persists `feature_version`; `LoadedEnsemble` surfaces it (NULL → "1.0.0"); XGB/GRU mismatch → `ModelLoadError`; v1 ensemble → 20 fundamentals hard-zeroed + excluded from z-score; v2 ensemble → rank pre-step invoked, fundamentals excluded from z-score cols.

Supporting: `_select_newest_bundle` cases + command assembly (patch subprocess); fusion ==±50 boundary; purge script dry-run vs execute; `_zscore_feature_cols`; migration up/down against test DB.

## Sequencing & commits (each numbered item = one commit)

| When | What |
|---|---|
| Wed night | WS1 + resilience test (1); fusion threshold + test (2); purge script (3); projection upsert + repair script (4); migration 0056 + ORM (5) |
| Thu AM | roce identity check on real data (read-only, 30 min); `compute_fundamental_features_series` + test (6) |
| Thu PM | `cross_sectional_stats.py` + batch integration + test (7); merge_asof pipeline integration + `ML_FEATURE_SET_VERSION` flag (8) |
| Thu eve | version gating end-to-end + test (9); run purge → repair scripts (dry-run → execute) |
| Fri AM | WS3 + tests (10); `--n-symbols/--symbols` + SCHEMA_VERSION 5 + `_MODEL_AFFECTING_KEYS` (11); apply migration 0056 |
| Fri PM | full ML suite green; **smoke run** `--fresh --n-symbols 20`; verify 16:00 refresh persisted grids (else run post-pass manually); rehearse `scheduled_retrain.py` small-universe end-to-end |
| Sat | buffer to ~14:00, no new code after noon; final checks (lock free, disk, bundles, suite green). Timer fires 20:02 — hands off |

**Smoke-run pass criteria:** both models registered with `feature_version='2.0.0'` and 66-name manifests; fundamentals non-constant post-2022 and 0 pre-2022 in the panel; no PendingRollbackError; steps 1–4 complete.

**WS2-only revert path** (Sat decision checkpoint ~17:00): set `ML_FEATURE_SET_VERSION="1.0.0"` — training reverts to legacy 20-name broadcast + median imputation + 69-feature z-score and registers 1.0.0 models; the inference gate makes this coherent automatically; new column/table are inert. WS1 + WS3 + WS4 still ride. Commits 6–9 are also cleanly revertible as a range if the flag is ever deemed insufficient.

## Post-run: v2.0.0 promotion criteria (locked 2026-07-16, after Sat's run)

Saturday's run produces the v1.2.0 challenger; **promotion to serving is NOT automatic**. Criteria:

1. **Ablation proof**: train a 49-feature control (44 technical + 5 sentiment, no fundamentals) with identical splits/seeds; **DSR on purged CV decides** whether the 17 fundamentals earn their place. Direction accuracy = sanity check only. *Open decision: control in the same Saturday run (~2x runtime, needs a `--feature-subset` flag) vs a separate run — not yet chosen.*
2. **2-week live shadow**: post-close counterfactual tracking, **paired** comparison vs the incumbent on the same symbols/days (paired removes regime luck). Shadow must not *contradict* the ablation; with ~10 trading days the bar is "no red flags", DSR carries the decision if suggestion volume is low.
3. **Scope**: promoted model drives fusion signal + suggestions only.

North-star KPI thereafter: paired, post-close-verified hit rate on gated suggestions, trending up across retrain cycles (selective precision, not overall accuracy).

## Risks

- **roce formula** unresolved until Thu-AM data check (`total_liability` may equal balance-sheet total on Indian statements → `total_asset − total_liability ≈ 0`); default proxy `op_profit/total_asset` is rank-safe and documented.
- **Grid availability**: a promoted 2.0.0 model with no grids serves fundamentals as neutral 0 + loud warning — degraded, never wrong-scale. Add a promotion-time WARN if newest grid > 2 days old.
- **GRU 2022 step-change** (fundamentals 0 → real ranks inside a 60-step window): accepted, standard treatment; purged CV limits straddling sequences. No special-casing.
- **On-demand/new-listing symbols**: not in grid universe → interp still valid; no fundamentals at all → neutral 0.
- **SCHEMA_VERSION bump** invalidates any pre-existing run dir (loud `StaleCheckpointError` — correct); Saturday uses `--fresh`.

## Verification

1. `pytest tests/unit/test_ml_*.py tests/ml/` — full ML suite, zero regressions, from `backend/`.
2. The 4 new gate tests green.
3. Migration: `alembic upgrade head` then `downgrade -1`/re-upgrade on test DB.
4. Smoke run (Fri PM) per pass criteria above; inspect the registered rows and manifest in `ml_model_metadata`.
5. Live parity probe: after grids exist, one symbol's Tier-1 vs Tier-2 fundamental values through `_apply_rank_transform` match within grid tolerance.
6. `scheduled_retrain.py --dry-run` + small-universe rehearsal proves bundle-build → `--feedback-weights` → orchestrator wiring end-to-end.
