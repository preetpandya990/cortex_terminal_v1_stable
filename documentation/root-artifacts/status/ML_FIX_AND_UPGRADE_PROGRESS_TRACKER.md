# ML Fix & Upgrade — Progress Tracker

> Companion to `ML_FIX_IMPLEMENTATION_PLAN.md` (the audited, implementation-ready plan for WS1–WS4)
> and `ML_FIX_AND_UPGRADE_PLAN.md` (the original problem write-up).
> Created **2026-07-16**.
>
> **Hard deadline:** Sat **2026-07-18 20:02 IST** — the systemd timer launches
> `scripts/scheduled_retrain.py` and trains the v1.2.0 challenger on whatever code is on disk.
> All code + validation done before **Sat noon**; no new code after that. All paths under `backend/`.
>
> **How to use:** every task has a checkbox and each WS has a Status line. Statuses: `⬜ NOT STARTED` ·
> `🟡 IN PROGRESS` · `🔵 IN REVIEW/TESTING` · `✅ DONE` · `⛔ BLOCKED (note why)`.
> Update the workstream Status line + the dashboard when a WS changes state. Each WS also has
> a **Verification** block — a WS is not ✅ until its verification items pass.

---

## Dashboard

| WS | Title | Depends on | Status | Started | Completed |
|---|---|---|---|---|---|
| WS1 | Survival fix (session-per-chunk + rollback + retry) | — | ✅ DONE | 2026-07-16 | 2026-07-16 |
| WS2a | Point-in-time fundamental series (17 features) | — | ✅ DONE | 2026-07-17 | 2026-07-17 |
| WS2b | Cross-sectional rank normalization + grids | WS2a | ✅ DONE | 2026-07-17 | 2026-07-17 |
| WS2c | Version-gated inference parity + migration 0056 | WS2b | ✅ DONE (incl. SCHEMA_VERSION 5 + Tier-2 PIT fix) | 2026-07-17 | 2026-07-17 |
| WS3 | Feedback weights in scheduled path | — | ✅ DONE | 2026-07-17 | 2026-07-17 |
| SMOKE | Smoke-run CLI + smoke run + rehearsal | WS1–WS3 | ✅ DONE (smoke passed all criteria; rehearsal wiring proven — A5 gate correctly rejected the degenerate 5-symbol panel, no re-run to protect the 1.2.0 pin) | 2026-07-17 | 2026-07-17 |
| WS4 | Hygiene (projection, purge, fusion threshold) | — (∥-safe) | ✅ DONE (incl. live executes) | 2026-07-16 | 2026-07-17 |

**Feature flag / revert lever:** `ML_FEATURE_SET_VERSION` (default `"2.0.0"`, `app/ml/config.py`) = ✅ created 2026-07-17, wired end-to-end through TrainingConfig → compute_features_batch/for_symbol → get_all_feature_names → zscore exclusion — set to `"1.0.0"` at the Sat ~17:00 decision checkpoint to revert WS2 entirely (WS1/WS3/WS4 still ride)
**Migration:** 0056 (`feature_version` column + `ml_feature_cross_stats` table) = ✅ **APPLIED TO LIVE DB 2026-07-17** (ahead of the Fri-AM slot, deliberately: the ORM in the working tree declares `feature_version`, and the bare-metal backend runs from this tree — a restart before the migration would have broken every ml_model_metadata query. Migration is additive-only, round-trip tested; 15/15 rows backfilled to '1.0.0'; old code verified unaffected)
**Sat timer status:** ARMED (verified 2026-07-17: `cortex-retrain.timer` next fire Sat 20:01:41 IST) — `--fresh` run · checkpoints dir WIPED CLEAN 2026-07-17 (370MB smoke/rehearsal artifacts) · lock verified FREE · fresh bundle present (sha d78463…) · disk 829G free · **model version pinned → 1.2.0** (one-shot, self-healing)

---

## Binding decisions (user-confirmed, locked — do not relitigate)

1. **Hybrid ratio handling:** reconstruct `roe` (= net_profit/net_worth) and `roce` (approximation, verified against real data Thu AM) point-in-time from statements; **drop `pe_ratio, pb_ratio, ev_ebitda`** → v2 set = 44 technical + 5 sentiment + 17 fundamentals = **66 features**.
2. **Pre-2022 history gap** (Upstox statements ~4y deep): NaN → cross-sectional median → rank 0 (neutral). Standard Gu–Kelly–Xiu treatment.
3. **Inference gating source of truth** = the loaded model's persisted `feature_version` (new column), NOT the config flag — live 1.0.0 models keep exact legacy behavior no matter what training does.
4. **Feature store stays raw, `FEATURE_VERSION = "v1.0"` unchanged** — bumping would starve Tier-1 inference of its 60-row history until full backfill.
5. **v2.0.0 promotion is NOT automatic** (locked 2026-07-16) — ablation proof (DSR on purged CV) + 2-week paired live shadow first; see Post-run section.

---

## WS1 — Survival fix (Wed night, FIRST)

**Status: ✅ DONE (2026-07-16)** · Files: `app/ml/features/feature_pipeline.py`, `app/ml/features/sentiment_features.py`, `scripts/production_training_orchestrator.py`, **new** `app/ml/features/db_errors.py`

Pattern: session-per-chunk + per-symbol rollback + one fresh-session retry on transient connection errors (proven by `scripts/compute_production_features.py:process_batch`).

- [x] `compute_features_batch`: new optional params `session_factory: Optional[async_sessionmaker] = None`, `chunk_size: int = 50` (keyword-only; `db` now Optional, exactly one of db/factory required)
- [x] Legacy path (no factory): `await safe_rollback()` in except block, rollback itself guarded (can raise on a dead connection)
- [x] Transient classifier: **placed in new shared module `app/ml/features/db_errors.py`** as `is_transient_connection_error` (not inside feature_pipeline as planned — sentiment_features needs it too and feature_pipeline imports sentiment_features; same behavior, clean dependency direction). Walks `orig`/`__cause__`/`__context__` chain: asyncpg `ConnectionDoesNotExistError`, SA `OperationalError`/`InterfaceError`/`DBAPIError.connection_invalidated`
- [x] Factory path: chunk symbols (2234 → ~45 sessions); per-symbol except → rollback; transient error → abandon chunk session, open fresh, retry failed symbol **once**, continue chunk on new session; second failure → skip (+ replace session again if the retry died transiently, so the rest of the chunk isn't poisoned)
- [x] `sentiment_features.py`: both broad excepts (`extract_features` + `_fetch_sentiment_data`) → guarded rollback before returning defaults + **re-raise** transient connection errors so batch-level retry can act
- [x] Orchestrator: `__init__` gains keyword-only `session_factory`; step 2 passes it to `compute_features_batch`; `main()` upgraded `sessionmaker(class_=AsyncSession)` → `async_sessionmaker` and passes it in
- [x] Confirmed `models/production/checkpoints/` is empty; `--fresh` path clean (runtime confirmation lands with Fri smoke run)

**Verification**
- [x] `tests/unit/test_ml_feature_pipeline_resilience.py` green — **18/18 passed**: classification (8 cases incl. wrapped orig/cause chains), legacy rollback + rollback-failure survival, factory retry-once (factory call count == 2), second-failure skip (count == 3), non-transient no-retry, chunking closes all sessions, sentiment rollback + re-raise + non-transient defaults
- [x] Full ML suite: **609 passed, 0 regressions** (10 failures verified pre-existing on clean tree via stash: 6 in `test_ml_auth.py`, 3 in `test_ml_scheduled_c1.py::TestRealInvocation`, 1 in `test_regression_e1.py::test_training_state_round_trips_correctly`)

> ⚠️ Pre-existing failures block the Fri-PM "full suite green" gate — the `test_ml_scheduled_c1.py` ones touch `scheduled_retrain.py` (WS3's file, revisit there); auth + regression_e1 need a separate decision.

---

## WS2a — Point-in-time fundamental series (Thu AM)

**Status: ✅ DONE (2026-07-17)** · Files: `app/ml/features/fundamental_features.py`

- [x] **roce identity check on real data (2026-07-17, read-only)** — RESOLVED: `total_liability` does NOT include equity (median rel. diff 0.59, not ≈0); `net_worth == total_asset − total_liability` holds EXACTLY on all 9,503 balance-sheet rows, so every capital-employed variant constructible from available columns reduces to `total_asset` → proxy `roce = op_profit/total_asset` is the only choice, not an approximation. Validation vs Upstox key-ratio snapshots (2,218 companies): Spearman roe 0.73, roce 0.78; roe median scale ratio 1.05. Documented in the V2 names docstring
- [x] `FUNDAMENTAL_FEATURE_NAMES_V2` (17 names) + `get_fundamental_feature_names(version="1.0.0")` dispatch — legacy default byte-identical (tested)
- [x] `FUNDAMENTAL_REPORTING_LAG_DAYS = 90` with SEBI LODR rationale (45d quarterly/60d annual + headroom); legacy no-lag divergence documented
- [x] `compute_fundamental_features_series()` — 4 bulk queries (income/balance/cashflow/holdings, legacy filters minus as-of cutoff, NO key-ratios), `effective_date` + 17 columns sorted asc
- [x] Vectorized per-period features: `_yoy_series` (prior==0 → NaN), `_expanding_cagr` (matches legacy n_years semantics), `_expanding_mean`/`_expanding_slope` (margin avg, debt trend, legacy skip-invalid semantics), shareholding diffs, roe/roce post-merge from ffilled inputs with non-positive denominators → NaN
- [x] Outer merge on `period_date`, per-source ffill, no imputation — NaN flows to rank pass
- [x] `merge_fundamentals_asof()` helper (tz-normalized key, backward, rows before first effective_date stay NaN) — **written + tested now; pipeline wiring lands in commit 8** per sequencing

**Verification**
- [x] `tests/unit/test_ml_fundamental_series.py` green — **14/14**: PIT flip at `+lag` (day-before carries previous period), future-quarter mutation never changes earlier rows (frame-equal assert), roe/roce exact, pe/pb/ev absent + legacy list byte-identical, yoy prior==0 → NaN, tz-aware UTC merge works, empty-series → all-NaN columns
- [x] **Live smoke on real symbol** (2026-07-17, read-only): 8 PIT rows, per-source publication cadence visible, roe 13–17%, effective dates = period+90d, 78.7% non-null across 17 features
- [x] Full ML suite: 579 passed / 0 regressions with auth excluded (auth tests leak un-awaited AsyncMock coroutines and poison random bystander tests via PytestUnraisableExceptionWarning — pre-existing, verified both directions)

---

## WS2b — Cross-sectional rank normalization (Thu PM)

**Status: ✅ DONE (2026-07-17)** · Files: new `app/ml/features/cross_sectional_stats.py`, `app/ml/features/feature_pipeline.py`, `scripts/production_training_orchestrator.py`

- [x] New module `cross_sectional_stats.py` (pure math core, DB only in persist/load):
  - [x] `rank_normalize_panel(results, feature_names) -> Dict[date, Dict[str, CrossStat]]` — per-date `groupby.rank(pct=True)` → `2*pct − 1`, NaN → 0.0, in-place write-back via contiguous panel slices; `CrossStat(quantiles 101, median, n_obs)` frozen dataclass with `.degenerate` property. Edges: n_obs ≤ 1 or all-equal → 0; all-NaN date → no CrossStat. **Perf verified**: 2,200 sym × 250 days × 17 feats = 15.2s → ~2.5 min extrapolated for the full 10y panel (negligible vs training)
  - [x] `rank_transform_with_grid(raw, stat)` — `np.interp` over grid (endpoint clamp = ±1 clip); NaN/missing/degenerate → 0.0
  - [x] `persist_cross_sectional_stats` (chunked `ON CONFLICT` upsert on the 0056 unique key, idempotent) / `load_cross_sectional_stats`
- [x] `compute_features_batch` gains `feature_set_version: str = "1.0.0"` (keyword-only; config wiring = commit 8): under `"2.0.0"` the median-imputation block is replaced by `rank_normalize_panel(results, FUNDAMENTAL_FEATURE_NAMES_V2)`; legacy block verbatim under v1; docstring documents both contracts
- [x] z-score exclusion helper — implemented as **pure `zscore_feature_cols(feature_names, version)` in `feature_pipeline.py`** (not an orchestrator method: the identical exclusion is needed by `feature_loader` in WS2c — one function, no drift) and used at all three orchestrator `normalize_features` call sites + `prepare_training_data`. Verified: v2 → 49 z-scored cols (66−17), v1 → identity
- [x] `ML_FEATURE_SET_VERSION` config switch (default `"2.0.0"`) in `app/ml/config.py` with full revert-lever documentation; `TrainingConfig.feature_set_version` defaults to it, and `TrainingConfig.n_features` now auto-derives (66 v2 / 69 v1 / 49 no-fundamentals, explicit override honored). `compute_features_for_symbol` v2 path merges the PIT series via `merge_fundamentals_asof` (legacy broadcast verbatim under v1); version threaded through batch + `prepare_training_data` + `get_all_feature_names`. DONE 2026-07-17 (commit 8)

**Verification**
- [x] `tests/unit/test_ml_rank_normalization.py` green — **18/18** (2026-07-17): hand-computed ranks; NaN → 0; all-equal → 0; single-obs → 0; per-date independence; **grid round-trip parity on 500-symbol panel** (tolerance = one grid cell 0.02 + 2/n rank discretization — the grid can't beat its own resolution); out-of-range clips ±1; missing/NaN/degenerate → 0; median → ~0; persistence upsert (ON CONFLICT asserted) + load round-trip; v2 batch integration ranks in place while v1 stays byte-identical
- [x] End-to-end: fundamentals have **non-zero variance in the final training matrix** through `normalize_features` exclusion (rank column passes through untouched — asserted frame-equal)

---

## WS2c — Version-gated inference parity (Thu eve)

**Status: ✅ DONE (2026-07-17)** · Files: new `alembic/versions/0056_ml_feature_v2.py`, `app/models/ml_data.py`, `app/ml/model_registry.py`, `scripts/production_training_orchestrator.py`, ~~`scripts/train_all_timeframes.py`~~ (no longer exists — moot), `app/ml/inference/registry_loader.py`, `app/ml/inference/ensemble_predictor.py`, `app/ml/inference/feature_loader.py`, `scripts/compute_production_features.py`, `app/ml/training/checkpoint_manager.py` (+9 FeatureLoader construction sites)

- [x] **Migration 0056** (`alembic/versions/0056_ml_feature_v2.py`) + ORM (`MLFeatureCrossStats` class + `MLModelMetadata.feature_version` in `app/models/ml_data.py`): `feature_version VARCHAR(16)` nullable + backfill `'1.0.0'`; `ml_feature_cross_stats` with unique `(as_of_date, feature_name, feature_version)` + index `(feature_name, as_of_date)`. Verified 2026-07-17: offline SQL compile both directions + real upgrade→downgrade→re-upgrade round-trip on scratch DB cloned from live schema (constraints + column confirmed, scratch dropped). Prod apply = Fri AM
- [x] Checked `alembic/versions/` numbering race (2026-07-17): 0055 is the latest file AND the live DB head — no race
- [x] `register_model`: persists `feature_version` on the row **and** in lineage (was silently dropped). DONE 2026-07-17
- [x] Orchestrator literals `"1.0.0"` (both register calls) → `self.config.feature_set_version`. **`train_all_timeframes.py` no longer exists in the repo — plan item moot**
- [x] `registry_loader.py`: `LoadedEnsemble.feature_version` field + new pure `_resolve_ensemble_feature_version(xgb_meta, gru_meta)` (NULL → `"1.0.0"`; XGB/GRU mismatch → hard `ModelLoadError`); logged alongside n_features. Also threaded through `EnsemblePredictor` (ctor + `from_loaded_ensemble` + reload path) and the worker's `ml_components` dict
- [x] `feature_loader.py`: `feature_version` param + lazy per-instance grid cache; **all 9 construction sites wired** (worker ×1, event_processor, fusion API, signal_scheduler ×2, prediction_snapshot ×2, price_target_service, + `create_feature_loader` factory); baseline_computer left at v1 default deliberately
  - [x] **v1.0.0 models**: legacy 20 fundamentals hard-zeroed after manifest column selection (post-snapshot so Gemini still sees raw indicators) + excluded from z-score — exact trained distribution regardless of store contents
  - [x] **v2.0.0 models**: async `_apply_rank_transform` before `_prepare_features` at both Tier-1/Tier-2 call sites; newest grid `as_of_date ≤ row date` via searchsorted (14d lookback buffer); no grid → 0.0 + loud warning; rank cols excluded from z-score via shared `zscore_feature_cols`
- [x] Grid write path: `persist_cross_sectional_pass(as_of)` on `FeatureComputationPipeline` — re-reads the day's raw vectors (`DISTINCT ON (symbol)`) from `ml_features`, builds grids via `rank_normalize_panel`, upserts for `"2.0.0"`; called after the batch loop, non-fatal, only when the flag is v2. Daily compute now passes `feature_set_version=ML_FEATURE_SET_VERSION` so store rows carry raw PIT fundamentals
- [x] Checkpoint `SCHEMA_VERSION` 4 → 5 (changelog entry added) + `_MODEL_AFFECTING_KEYS` += `feature_set_version` (the config-dict key name) + `n_symbols`. Schema-pin tests updated (`ensemble_a5`, `lineage_e3` → 5 with documented-bump wording); `failloud_a1` non-affecting test moved to `xgboost_trials` + NEW test asserting n_symbols drift aborts. DONE 2026-07-17 (commit 11)
- [x] Tier-2 exactness fix found by live parity probe: `_compute_on_demand` now passes `feature_set_version=self.feature_version` — a v2 model's 60-step sequence tail gets real PIT steps, not flattened broadcast values
- [x] Verified `app/api/v1/admin_training.py` handles stale run dirs: `schema_ok = (schema_version == SCHEMA_VERSION)` → old dirs simply report non-resumable

**Verification**
- [x] `tests/unit/test_ml_feature_version_gate.py` green — **14/14** (2026-07-17): `register_model` persists to row+lineage; NULL → "1.0.0"; XGB/GRU mismatch → `ModelLoadError` (both directions); xgb-only mode; v1 hard-zero on PIT-varying store values (the crucial case) + snapshot still raw + technicals still z-scored; v2 grid mapping exact (2·raw−1 on uniform grid), rows before first grid → 0, no-grids → 0 + warning, rank values pass z-score untouched, Tier-1 invokes pre-step for v2 and skips for v1. Full sweep vs clean baseline: zero regressions
- [x] Migration: `alembic upgrade head` → `downgrade -1` → re-upgrade round-trip green on scratch DB cloned from the live schema (2026-07-17)

---

## WS3 — Feedback weights in scheduled path (Fri AM)

**Status: ✅ DONE (2026-07-17)** · Files: `scripts/scheduled_retrain.py`, `app/ml/config.py`

**Subprocess** invocation of existing `scripts/build_feedback_weights.py` (matches `_tee_subprocess` isolation philosophy — builder crash must never kill the retrain; exit codes 0/1/2 already defined).

- [x] `_build_feedback_bundle(cwd, log_dir) -> int` — `_tee_subprocess` to `feedback_build_<stamp>.log`, crash-isolated
- [x] `_select_newest_bundle(dir, max_age_days=7)` — newest `*.parquet` with sibling `.meta.json` (orphans skipped → next-newest), staleness checked on the newest complete bundle only (no fallback to staler ones — documented rationale)
- [x] `_assemble_orchestrator_cmd()` — builder failure → WARN + newest existing bundle regardless of exit code; no usable bundle → unweighted; disabled by config → base cmd; unexpected exception in the whole feedback path → belt-and-braces catch, unweighted. Builder runs UNDER the flock. Only the retrain's exit code drives 0/1/2
- [x] `app/ml/config.py` SCHEDULED_RETRAIN: `enable_feedback_weights` (True), `feedback_bundle_dir` ("feedback_bundles", matches `_DEFAULT_BUNDLES_DIR`), `feedback_bundle_max_age_days` (7)

**Verification**
- [x] 12/12 in new `tests/unit/test_ml_scheduled_feedback.py`: newest-wins, orphan fallback, stale→None (never an older bundle), fresh accepted, empty/missing dir, flag appended with correct path, builder-failure fallback, unweighted path, config-disabled skips builder, dry-run never launches builder, base cmd never mutated (2026-07-17)
- [x] `scheduled_retrain.py --dry-run` live-verified: prints feedback plan + assembled cmd, no lock, no subprocess
- [x] **Bonus:** fixed the 3 long-pre-existing `TestRealInvocation` failures — they patched `subprocess.call` which the Popen-based `_tee_subprocess` never uses, silently launching REAL orchestrator subprocesses during test runs; now patch the actual seam
- [ ] Small-universe rehearsal end-to-end (Fri PM item — after smoke run completes)

---

## SMOKE — Smoke-run CLI + Fri PM smoke run

**Status: ✅ DONE (2026-07-17)** · Files: `scripts/production_training_orchestrator.py`, `scripts/scheduled_retrain.py`, `app/ml/config.py`

- [x] Orchestrator `main()`: `--n-symbols INT` (config n_symbols set to cap for honest coverage + checkpoint identity; cap applied after step-1 quality ranking) + `--symbols "A,B,C"` (replaces selection, coverage=1.0); mutually exclusive; `--help` verified. DONE 2026-07-17
- [x] Full sweep green vs clean baseline: **1,074 passed, 3 pre-existing failures FIXED net**, only the roaming hawk_eye flake differs (2026-07-17)
- [x] **Smoke run `--fresh --n-symbols 20` COMPLETE** (2026-07-17, 79 min, exit 0, all 10 steps). **Caught 1 real bug**: step-10 manifest built without the version → 69 names on 66-feature artifacts. Fixed the call + added a hard registration guard (manifest length ≠ config.n_features → refuse to register); artifacts verified truly 66-feature (Treelite num_feature=66, ONNX [*,60,66]); the two 1.1.2 rows' manifests corrected to the real 66-name contract
- [x] Grids seeded manually via `persist_cross_sectional_pass` (16:00 refresh predates v2 code): 17 grid rows from the newest store snapshot (2026-07-13, 3,162-symbol cross-section) — within the loader's 14-day buffer
- [x] Rehearsal `scheduled_retrain.py` small-universe (5 symbols, real lock + real builder + real orchestrator subprocess) — **bundle-build → `--feedback-weights` → orchestrator chain PROVEN live** (2026-07-17): builder wrote bundle sha=d78463afce04… 32 rows; orchestrator loaded the identical bundle (same sha); 0/8617 row match expected at 5 symbols (no overlap with outcome instruments — full universe will match). Runs as challenger 1.1.3
- [x] Live parity probe (2026-07-17): Tier-1 vs Tier-2 through `_apply_rank_transform` on a live symbol — **max|diff| = 0.0000** across roe/debt_ratio/operating_margin/promoter_holding_pct over 30 overlapping days. Probe also exposed the Tier-2 broadcast gap → fixed (`_compute_on_demand` now version-aware)

**Smoke-run pass criteria (all must hold)**
- [x] Both models registered with `feature_version='2.0.0'` (column + lineage) and 66-name manifests — after the step-10 fix above; verified in `ml_model_metadata` (2026-07-17)
- [x] Fundamentals non-constant post-2022 (mean per-symbol std 0.21 on rank scale, 98/100 cells varying) and exactly 0 pre-2022 across all 20 symbols; pe/pb/ev absent from the panel
- [x] No `PendingRollbackError` anywhere in the log (grep count: 0 over 79 min)
- [x] Orchestrator steps 1–10 ALL complete, exit 0

---

## WS4 — Hygiene (Thu eve / Fri — nothing here blocks the run)

**Status: ✅ DONE (2026-07-17 — all code, tests, and live executes complete)** · Files: `app/ml/model_registry.py`, new `scripts/repair_ai_ml_models_projection.py`, new `scripts/purge_test_model_rows.py`, `app/ai/fusion/signal_assembler.py`

- [x] Hardened `_project_to_ai_ml_models`: `rowcount == 0` → INSERT with `ON CONFLICT (model_name) DO UPDATE` (race-safe), named `cortex_<type>_1d` per governance convention, metrics flattened via new `_metric_scalar` (per-class dicts → macro mean), `governance_metadata.auto_created_by` provenance. Existing UPDATE path unchanged; all 43 registry-A8 tests still green. DONE 2026-07-17
- [x] New `scripts/purge_test_model_rows.py` (dry-run default, `--execute` to write): deletes `ai_ml_models` rows matching `test_drift_model_%` OR `model_type='lstm'`; lists every candidate first; hard-aborts on protected types (xgboost/gru) or >20 matches (`--cap`). **Scope addition:** also deletes linked `ai_drift_reports` rows (no FK — 1,714 rows reference the 5 test models; deleting models alone would orphan them) in the same transaction. Live dry-run verified 2026-07-16: exactly 5 model rows + 1,714 reports matched, production rows untouched. **`--execute` RAN 2026-07-17**: 5 model rows + 1,714 reports deleted, before projection repair
- [x] New `scripts/repair_ai_ml_models_projection.py` (dry-run default, `--execute` to write): replays the **same hardened projection** (no second write path) for every `status='production'` OR `is_active` metadata row, oldest-first per type so the newest wins (+ duplicate warning); dry-run prints current-vs-target plan. Live dry-run verified 2026-07-17: both rows → state paper→live, version →1.1.1, FK 5→164 / 6→165. **`--execute` RAN 2026-07-17** (after purge): both governance rows live/1.1.1/correct FKs, zero orphans verified
- [x] Ran in order 2026-07-17 (Thu eve): purge `--execute` (5 model rows + 1,714 drift reports deleted) **then** repair `--execute` (both governance rows → live / 1.1.1 / FK 164·165)
- [x] Fusion threshold `signal_assembler.py` (:850-859 after WS1 edits): `> 50` → `>= 50`, `< -50` → `<= -50`, with rationale comment (technical scoring emits exactly ±50). Verified single-site: no other code re-derives action from `fused_score` (backend + frontend grep). DONE 2026-07-16

**Verification**
- [x] Fusion `==±50` boundary tests green — 3 new tests in `tests/ai/fusion/test_signal_assembler.py` (+50→BUY, −50→SELL, ±49.99→HOLD); full fusion suite 79/79 passed (2026-07-16)
- [x] Purge script dry-run vs execute tests green — 10/10 in `tests/unit/test_purge_test_model_rows.py` (dry-run never deletes, execute deletes+commits, guards abort with exit 2 and no writes, empty-table noop, unexpected-error exit) (2026-07-16)
- [x] `ai_ml_models` verified post-repair 2026-07-17: exactly 2 rows (cortex_xgboost_1d, cortex_gru_1d), both `live`, versions `1.1.1_*`, FKs → metadata 164/165; all 18,503 remaining drift reports reference only those 2 models (zero orphans). Serving reads ml_model_metadata — unaffected throughout

---

## Sequencing & commits (each numbered item = one commit)

| # | When | Commit | Status |
|---|---|---|---|
| 1 | Wed night | WS1 + resilience test | ✅ code+tests done 2026-07-16 (commit pending, user-owned) |
| 2 | Wed night | Fusion threshold + boundary test | ✅ code+tests done 2026-07-16 (commit pending, user-owned) |
| 3 | Wed night | Purge script | ✅ code+tests+live dry-run done 2026-07-16 (commit pending, user-owned; --execute RAN 2026-07-17) |
| 4 | Wed night | Projection upsert + repair script | ✅ code+tests+live dry-run done 2026-07-17 (commit pending, user-owned; --execute RAN 2026-07-17 after purge) |
| 5 | Wed night | Migration 0056 + ORM | ✅ written + scratch-DB round-trip verified 2026-07-17 (commit pending, user-owned; APPLIED to live DB 2026-07-17) |
| 6 | Thu AM | roce identity check (read-only) → `compute_fundamental_features_series` + test | ✅ done 2026-07-17 incl. live-data smoke (commit pending, user-owned) |
| 7 | Thu PM | `cross_sectional_stats.py` + batch integration + test | ✅ done 2026-07-17 incl. perf benchmark (commit pending, user-owned) |
| 8 | Thu PM | merge_asof pipeline integration + `ML_FEATURE_SET_VERSION` flag | ✅ done 2026-07-17 (commit pending, user-owned; a7/e3 test fixtures pinned to v1 where they encode 69-feature checkpoints) |
| 9 | Thu eve | Version gating end-to-end + test; run purge → repair scripts | ✅ FULLY done 2026-07-17: code+tests + purge/repair executed on live DB + post-state verified (commit pending, user-owned) |
| 10 | Fri AM | WS3 + tests | ✅ done 2026-07-17 + fixed 3 pre-existing TestRealInvocation failures (commit pending, user-owned) |
| 11 | Fri AM | `--n-symbols/--symbols` + SCHEMA_VERSION 5 + `_MODEL_AFFECTING_KEYS`; apply migration 0056 | ✅ done 2026-07-17 (migration was already applied Thu; commit pending, user-owned) |
| — | Fri PM | Full suite green; smoke run; grid check; rehearsal | ✅ ALL DONE 2026-07-17: suite green (1,074 passed, net 3 pre-existing fixed) · grids seeded · parity probe exact · smoke PASSED all 4 criteria (caught+fixed step-10 manifest bug) · rehearsal wiring proven (A5 gate correctly rejected 5-symbol panel; no re-run, protects 1.2.0 pin) |
| + | Fri eve | Model-version pin → 1.2.0 (`--model-version` flag + `model_version_override` config, one-shot self-healing) | ✅ done 2026-07-17, live-verified: pin→1.2.0, auto→1.1.3 (commit pending, user-owned) |
| ✔ | Fri 20:42 → Sat 12:32 | **FULL TRAINING RUN — COMPLETE, exit 0** (launched a day early, monitored): STOCK-only universe 1,997 (selector asset-class gate added live after 229 ETFs found in first launch); 3.65M samples × 66 features; steps 1–10 clean, zero PendingRollbackError/OOM; A5 join coverage 1.0000; registered `1.2.0_xgboost` (acc 0.7346, weight 1.0) + `1.2.0_gru` (acc 0.6006, weight 0.0, non-accretive) — both feature_version=2.0.0, 66-name manifests, development status. DSR saturated at 1.0000 for both → validate in ablation before trusting. GRU non-accretive → XGB-only serving if promoted | ✅ |
| — | Sat | Buffer / final checks / 20:02 timer | ✅ OVERTAKEN BY EVENTS: full run completed 12:32 Sat (launched Fri night), timer STOPPED by operator, revert lever not needed (ablation passed) |

---

## Revert path (Sat ~17:00 decision checkpoint)

- **WS2-only revert:** set `ML_FEATURE_SET_VERSION="1.0.0"` → training reverts to legacy 20-name broadcast + median imputation + 69-feature z-score and registers 1.0.0 models; inference gate makes this coherent automatically; new column/table inert. WS1 + WS3 + WS4 still ride.
- Commits 6–9 are also cleanly revertible as a range if the flag is ever deemed insufficient.

## Post-run: v2.0.0 promotion criteria (locked 2026-07-16 — NOT automatic)

- [x] **Ablation RUN 2026-07-18** (decision: separate run, `--no-fundamentals` flag added; control `0.49.0_*`, same universe/seeds/bundle): fundamentals deliver uniform held-out lift — xgb acc +9.8pp / auc_pr +13.3pp / Sharpe 1.34 vs 0.96 / win-rate +12.1pp; PBO unchanged (~0.48). GRU below-random without fundamentals; non-accretive both runs. **⛔ BUT the locked criterion (DSR) is NON-DISCRIMINATING: all 4 models print exactly 1.0000 incl. a Sharpe-0.338 model → evaluator's DSR computation is saturated/broken (suspect total_return overflow). Fix required before the criterion can be formally satisfied.**
- [ ] ~~**Ablation proof**~~ (superseded above): 49-feature control (44 technical + 5 sentiment, no fundamentals), identical splits/seeds; **DSR on purged CV decides**; direction accuracy = sanity check only. *Open decision: control in the same Saturday run (~2× runtime, needs `--feature-subset` flag) vs separate run — NOT YET CHOSEN*
- [x] **DSR evaluator fixed + both runs re-scored (2026-07-18 late):** panel→daily-portfolio collapse + horizon de-overlap (`horizon_days=5`); `rescore_financial_metrics.py --execute` updated all 4 rows (legacy preserved). **Corrected criterion verdict: challenger XGB OOF SR 0.2236 vs control 0.1915 (+17%, ann ≈1.59 vs ≈1.36); DSR(N=1)≈1.0 both (real significance, gauge discriminates now); N_upper band 0.194 vs 0.057; oos_loss_rate 0 both. FUNDAMENTALS PASS THE ABLATION GATE.** Eval-window Sharpe tension (control 4.03 vs 2.90) noted as ~1.6 se noise at T=101
- [x] **1.2.0 PROMOTED to the serving tier 2026-07-18** (operator-directed; audited break-glass on the stale coverage denominator + moot dormant-GRU ECE): xgb production-active / gru production-ACTIVE at weight 0.0 (user-directed: full prediction telemetry, zero signal influence) / 1.1.1 stepped back (xgb=rollback anchor, gru=staging). Loader pre-flight: XGB-only, 66 feats, fv=2.0.0 ✓. Services were down — evaluation clock starts on next API/worker start
- [ ] **2-week live evaluation** (was: shadow — role inverted by operator: challenger takes the paper flow, incumbent benched): post-close counterfactual tracking, **paired** vs incumbent on same symbols/days; must not contradict the ablation; ~10 trading days → bar is "no red flags", DSR carries if suggestion volume is low
- [ ] Scope on promotion: model drives fusion signal + suggestions only
- North-star KPI thereafter: paired, post-close-verified hit rate on gated suggestions, trending up across retrain cycles

## Risks (watch list)

- ~~**roce formula** unresolved until Thu-AM data check~~ **RESOLVED 2026-07-17**: feared degeneracy doesn't exist (`total_liability` excludes equity; `net_worth == total_asset − total_liability` exact on all rows); proxy `op_profit/total_asset` adopted + validated (Spearman 0.78 vs snapshots)
- **Grid availability**: promoted 2.0.0 model with no grids serves fundamentals as neutral 0 + loud warning — degraded, never wrong-scale. Add promotion-time WARN if newest grid > 2 days old
- **GRU 2022 step-change** (fundamentals 0 → real ranks inside a 60-step window): accepted, standard treatment; purged CV limits straddling sequences
- **On-demand/new-listing symbols**: not in grid universe → interp still valid; no fundamentals at all → neutral 0
- **SCHEMA_VERSION bump** invalidates any pre-existing run dir (loud `StaleCheckpointError` — correct); Saturday uses `--fresh`
- **Alembic numbering race**: 0055 exists uncommitted — verify before writing 0056 and before Friday's prod upgrade


---

## FINAL STATE (2026-07-18 ~23:50 IST — initiative complete)

- **Serving tier**: `1.2.0_xgboost` production-ACTIVE (weight 1.0) + `1.2.0_gru` production-ACTIVE at weight 0.0 (operator-directed: full per-member prediction telemetry, zero signal influence). Governance: both `live v1.2.0`. Rollback anchor: `1.1.1_xgboost` production-inactive (`promote_model.py rollback --model-name xgboost`); `1.1.1_gru` staging.
- **Loader pre-flight PASSED** (offline): both members load, 66 features, `feature_version=2.0.0`, grid transform armed (grids seeded; daily refresh maintains).
- **Containers REBUILT** 2026-07-18 23:47 (api + worker) — July-11 images lacked the version-gated inference layer and would have silently mis-served the v2 model (zeroed fundamentals). Build ≠ start: **nothing is serving; `docker compose up -d api worker` is the deliberate 2-week-evaluation kickoff.**
- **Timer**: stopped by operator (would otherwise train a redundant 1.2.1). Re-enable after clearing `SCHEDULED_RETRAIN["model_version_override"]`.
- **Break-glass audit**: promotions bypassed A6 `symbol_coverage` (78.3% < 85% — stale denominator: n_symbols=2551 predates the STOCK-only universe of 2,936) and dormant-GRU `calibration_ece` 0.054. **Follow-ups filed**: recalibrate n_symbols/coverage gate before the next scheduled run; optionally regenerate the C2 promotion report from corrected metrics.
- **Post-cutover science**: 2-week live evaluation (paired per-member snapshots + post-close outcomes) → next retrain absorbs outcomes via feedback weights; GRU re-earns weight automatically if A5 finds it accretive.
- **STILL UNCOMMITTED**: the entire initiative (~45 files) — commit before starting services so images/runs correspond to a revision.
