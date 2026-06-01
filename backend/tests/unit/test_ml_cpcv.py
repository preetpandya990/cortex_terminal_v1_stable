"""
A2 — PanelPurgedCPCV correctness (López de Prado Combinatorial Purged CV).

These prove the leakage guarantee AT THE CV LEVEL:
  * combinatorics & path counts match the closed forms;
  * López de Prado path assignment is exact (every path covers all groups once);
  * the produced train indices contain NO purge/embargo violation against any
    test block — i.e. no train label window can overlap a test period.

The end-to-end "future-peeking feature collapses to chance" probe lives in the
orchestrator integration test (A2.7) — here we verify the splitter itself.
"""
import numpy as np
import pandas as pd
import pytest

from app.ml.evaluation.cpcv import PanelPurgedCPCV, purged_holdout_split


def _panel(n_days: int, n_symbols: int, start="2016-01-04"):
    """Stacked panel: every trading day repeated once per symbol (shared calendar)."""
    days = pd.bdate_range(start, periods=n_days)
    ts = np.tile(days.values, n_symbols)          # row-aligned timestamps
    sym = np.repeat(np.arange(n_symbols), n_days)  # symbol id per row
    return ts, sym, days


class TestCombinatorics:
    @pytest.mark.parametrize("n,k,splits,paths", [
        (8, 2, 28, 7), (6, 2, 15, 5), (10, 2, 45, 9), (6, 3, 20, 10),
    ])
    def test_closed_forms(self, n, k, splits, paths):
        assert PanelPurgedCPCV.n_splits(n, k) == splits
        assert PanelPurgedCPCV.n_paths(n, k) == paths

    def test_default_regime_is_8_2(self):
        ts, _, _ = _panel(400, 5)
        cv = PanelPurgedCPCV(ts, horizon=5, n_groups=8, n_test=2, embargo=5)
        combos = list(cv.split())
        assert len(combos) == 28
        assert cv.total_paths == 7

    @pytest.mark.parametrize("kw,msg", [
        (dict(n_groups=1), "n_groups"),
        (dict(n_test=0), "n_test"),
        (dict(n_test=8), "n_test"),
        (dict(horizon=-1), "non-negative"),
    ])
    def test_construction_validation(self, kw, msg):
        ts, _, _ = _panel(200, 3)
        with pytest.raises(ValueError, match=msg):
            PanelPurgedCPCV(ts, **kw)

    def test_too_few_unique_timestamps(self):
        ts, _, _ = _panel(5, 3)
        with pytest.raises(ValueError, match="unique timestamps"):
            PanelPurgedCPCV(ts, n_groups=8)


class TestPathAssignment:
    def test_each_group_tested_n_paths_times_and_paths_cover_all_groups(self):
        ts, _, _ = _panel(400, 4)
        cv = PanelPurgedCPCV(ts, n_groups=8, n_test=2, embargo=5)
        per_group_count = {g: 0 for g in range(8)}
        path_groups: dict[int, list[int]] = {}
        for sp in cv.split():
            for g, p in sp.test_paths.items():
                per_group_count[g] += 1
                path_groups.setdefault(p, []).append(g)
        # Each group tested exactly C(N-1,k-1) == n_paths times.
        assert set(per_group_count.values()) == {cv.total_paths}
        # Exactly n_paths paths, each covering all N groups exactly once.
        assert sorted(path_groups) == list(range(cv.total_paths))
        for groups in path_groups.values():
            assert sorted(groups) == list(range(8))


class TestPurgeEmbargoInvariant:
    def test_no_train_row_violates_purge_or_embargo(self):
        ts, _, _ = _panel(260, 3)
        h, emb = 5, 3
        cv = PanelPurgedCPCV(ts, horizon=h, n_groups=6, n_test=2, embargo=emb)
        row_code = cv._row_code  # ordinal on the unique-timestamp axis
        for sp in cv.split():
            train_codes = np.unique(row_code[sp.train_idx])
            test_codes = np.unique(row_code[sp.test_idx])
            assert train_codes.size and test_codes.size
            # train ∩ test must be empty
            assert not np.intersect1d(train_codes, test_codes).size
            # For every maximal test run [s, e]: no train code may lie in
            # [s - h, e + emb] (purge of overlapping label windows + embargo).
            b = np.flatnonzero(np.diff(test_codes) > 1)
            starts = np.r_[test_codes[0], test_codes[b + 1]]
            ends = np.r_[test_codes[b], test_codes[-1]]
            for s, e in zip(starts, ends):
                forbidden = set(range(max(0, s - h), e + 1 + emb))
                assert not (set(train_codes.tolist()) & forbidden)

    def test_embargo_zero_vs_nonzero_shrinks_train(self):
        ts, _, _ = _panel(260, 2)
        big = sum(len(s.train_idx) for s in
                  PanelPurgedCPCV(ts, n_groups=6, n_test=2, embargo=0).split())
        small = sum(len(s.train_idx) for s in
                    PanelPurgedCPCV(ts, n_groups=6, n_test=2, embargo=10).split())
        assert small < big  # a larger embargo strictly removes more train rows


class TestPanelMapping:
    def test_timestamp_pulls_all_symbols_rows(self):
        ts, sym, _ = _panel(200, 4)  # 4 symbols share each timestamp
        cv = PanelPurgedCPCV(ts, n_groups=5, n_test=2, embargo=2)
        row_code = cv._row_code
        sp = next(iter(cv.split()))
        test_codes = set(np.unique(row_code[sp.test_idx]).tolist())
        # Membership is by TIMESTAMP: every panel row whose unique code is a
        # test code must be in test_idx (all 4 symbols), and none in train.
        expected_test = np.flatnonzero(np.isin(row_code, list(test_codes)))
        assert np.array_equal(np.sort(sp.test_idx), np.sort(expected_test))
        assert not set(sp.train_idx.tolist()) & set(sp.test_idx.tolist())
        # Each tested timestamp contributes exactly n_symbols rows.
        _, counts = np.unique(row_code[sp.test_idx], return_counts=True)
        assert set(counts.tolist()) == {4}


class TestOOFRowRouting:
    def test_test_path_of_row_matches_group_paths(self):
        ts, _, _ = _panel(400, 4)
        cv = PanelPurgedCPCV(ts, n_groups=8, n_test=2, embargo=5)
        row_code = cv._row_code
        for sp in cv.split():
            # 1:1 length with test rows; every entry is a valid path id.
            assert sp.test_path_of_row.shape == sp.test_idx.shape
            assert set(sp.test_path_of_row.tolist()) == set(sp.test_paths.values())
            # Each test row's path == the path of the group its timestamp is in.
            for row_pos, gidx in enumerate(sp.test_idx):
                code = row_code[gidx]
                g = int(np.searchsorted(cv._group_starts, code, "right") - 1)
                assert g in sp.test_groups
                assert sp.test_path_of_row[row_pos] == sp.test_paths[g]


class TestPurgedHoldoutSplit:
    def test_disjoint_gap_and_ordering(self):
        ts, _, _ = _panel(260, 3)
        h, emb = 5, 5
        tr, te = purged_holdout_split(ts, horizon=h, embargo=emb, train_frac=0.8)
        _, codes = np.unique(pd.DatetimeIndex(ts).values, return_inverse=True)
        tr_codes, te_codes = codes[tr], codes[te]
        assert not set(tr_codes.tolist()) & set(te_codes.tolist())  # disjoint
        # train strictly precedes test, with a >= h+emb whole-timestamp gap
        assert te_codes.min() - tr_codes.max() >= h + emb

    def test_empty_side_raises_not_degrades(self):
        ts, _, _ = _panel(8, 2)  # too short for the gap
        with pytest.raises(ValueError, match="empty side|unique timestamps"):
            purged_holdout_split(ts, horizon=20, embargo=20, train_frac=0.8)

    @pytest.mark.parametrize("tf", [0.0, 1.0, -0.1, 1.5])
    def test_train_frac_validation(self, tf):
        ts, _, _ = _panel(200, 2)
        with pytest.raises(ValueError, match="train_frac"):
            purged_holdout_split(ts, train_frac=tf)


class TestNoLabelLeakage:
    """A2 acceptance test — empirical leakage probe.

    The structural invariants (purge/embargo guarantee) are proven by
    ``TestPurgeEmbargoInvariant``.  This class adds an *empirical* end-to-end
    probe that verifies PanelPurgedCPCV eliminates spurious predictive signal
    that a naive shuffled k-fold would carry.

    Setup
    -----
    * AR(1) daily return series (rho=0.7) → h-step forward return is
      mildly predictable from current return (weak real signal).
    * Binary label ``y[t] = 1 if fwd_ret[t] > 0 else 0``.
    * Feature ``X[t] = ret[t]`` (causal: only today's return, no look-ahead).

    Why naive k-fold inflates
    -------------------------
    With overlapping h-bar label windows, a test row at time t shares price
    data ``price[t : t+h]`` with up to h adjacent train rows. The AR(1)
    autocorrelation propagates this overlap into spurious feature/label
    correlation → the shuffled-k-fold OOF AUC-PR overstates true
    out-of-sample performance.

    Why CPCV deflates
    -----------------
    PanelPurgedCPCV removes every train timestamp whose label window
    ``[t, t+h]`` overlaps any test timestamp, plus an embargo buffer
    afterwards. This severs the shared-price-data channel, leaving only the
    genuine h-step AR(1) signal (≈ rho^h ≈ 0.7^5 ≈ 0.17), which is too weak
    to materially inflate AUC-PR.
    """

    def test_no_label_leakage(self):
        """Naive shuffled CV > CPCV OOF AUC-PR when label-horizon windows overlap."""
        import sklearn.linear_model as sklm
        import sklearn.metrics as skm
        import sklearn.model_selection as skms

        n_days, h, emb = 700, 5, 5
        ts, _, _ = _panel(n_days, 1)  # single symbol → clean sequential time series
        n = len(ts)
        rng = np.random.default_rng(2024)

        # AR(1) return series (rho = 0.7)
        ret = np.empty(n, dtype=np.float64)
        ret[0] = rng.normal(0, 0.01)
        for i in range(1, n):
            ret[i] = 0.7 * ret[i - 1] + rng.normal(0, 0.01)

        # h-bar forward return (label horizon)
        fwd = np.zeros(n, dtype=np.float32)
        for i in range(n - h):
            fwd[i] = float(ret[i : i + h].sum())

        y = (fwd > 0).astype(np.int8)
        X = ret.reshape(-1, 1).astype(np.float32)  # causal feature — no look-ahead

        # ── Naive shuffled 6-fold (no time structure, no purge) ───────────────
        oof_naive  = np.full(n, float(y.mean()))
        mask_naive = np.zeros(n, dtype=bool)
        kf = skms.KFold(n_splits=6, shuffle=True, random_state=0)
        for tr, te in kf.split(X):
            clf = sklm.LogisticRegression(random_state=0, max_iter=400)
            clf.fit(X[tr], y[tr])
            oof_naive[te] = clf.predict_proba(X[te])[:, 1]
            mask_naive[te] = True
        naive_ap = skm.average_precision_score(y[mask_naive], oof_naive[mask_naive])

        # ── CPCV OOF (purge=h, embargo=emb) ───────────────────────────────────
        cv = PanelPurgedCPCV(ts, horizon=h, n_groups=6, n_test=2, embargo=emb)
        oof_cpcv  = np.full(n, float(y.mean()))
        mask_cpcv = np.zeros(n, dtype=bool)
        for sp in cv.split():
            if sp.train_idx.size < 40:
                continue  # skip degenerate combos (rare edge case for tiny h)
            clf2 = sklm.LogisticRegression(random_state=0, max_iter=400)
            clf2.fit(X[sp.train_idx], y[sp.train_idx])
            oof_cpcv[sp.test_idx] = clf2.predict_proba(X[sp.test_idx])[:, 1]
            mask_cpcv[sp.test_idx] = True
        cpcv_ap = skm.average_precision_score(y[mask_cpcv], oof_cpcv[mask_cpcv])

        # Naive CV is inflated because h-step overlapping label windows create
        # spurious temporal correlation; CPCV purge severs it.
        assert naive_ap > cpcv_ap, (
            f"Expected naive AP ({naive_ap:.4f}) > CPCV OOF AP ({cpcv_ap:.4f}). "
            "Shuffled k-fold should be more optimistic than time-purged CPCV "
            "when label-horizon windows overlap across train/test boundaries. "
            "Check purge/embargo logic."
        )

        # CPCV deflation must be material, not a rounding artefact.
        # With rho=0.7, h=5: h-step correlation ≈ 0.7^5 ≈ 0.17 → weak residual
        # signal after purge; naive inflation should dominate by > 0.5 %.
        assert (naive_ap - cpcv_ap) > 0.005, (
            f"CPCV deflation {(naive_ap - cpcv_ap):.4f} is smaller than 0.5 pp — "
            "purge may not be eliminating the expected volume of leaky rows."
        )

    def test_property_no_train_ts_in_purge_window(self):
        """Property: ∀ train row t, t ∉ [test_lo − h, test_hi + emb] (exact integer codes)."""
        ts, _, _ = _panel(300, 4)
        h, emb = 7, 4
        cv = PanelPurgedCPCV(ts, horizon=h, n_groups=6, n_test=2, embargo=emb)
        row_code = cv._row_code
        for sp in cv.split():
            train_codes = set(row_code[sp.train_idx].tolist())
            test_codes  = np.unique(row_code[sp.test_idx])
            # Build forbidden set from maximal contiguous test runs.
            boundaries  = np.flatnonzero(np.diff(test_codes) > 1)
            run_starts  = np.r_[test_codes[0], test_codes[boundaries + 1]]
            run_ends    = np.r_[test_codes[boundaries], test_codes[-1]]
            for s, e in zip(run_starts.tolist(), run_ends.tolist()):
                forbidden = set(range(max(0, s - h), e + 1 + emb))
                assert not (train_codes & forbidden), (
                    f"combo {sp.combo_id}: train codes {train_codes & forbidden} "
                    f"fall in forbidden window [{s-h}, {e+emb}] — purge/embargo violated."
                )


class TestAxisFingerprint:
    def test_fingerprint_deterministic_and_axis_sensitive(self):
        ts_a, _, _ = _panel(300, 4)
        ts_b, _, _ = _panel(300, 7)  # more symbols, SAME calendar → same axis
        ts_c, _, _ = _panel(301, 4)  # different calendar → different axis
        fp = lambda t: PanelPurgedCPCV(t, n_groups=6, n_test=2).axis_fingerprint
        assert fp(ts_a) == fp(ts_a)        # deterministic
        assert fp(ts_a) == fp(ts_b)        # symbol count irrelevant; axis identical
        assert fp(ts_a) != fp(ts_c)        # axis change detected (resume guard)
