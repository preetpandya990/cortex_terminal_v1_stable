"""
Cortex AI — Combinatorial Purged Cross-Validation (in-house)
=============================================================
Leakage-safe cross-validation for panel (multi-symbol) financial data,
implementing López de Prado's Combinatorial Purged Cross-Validation (CPCV).

Why in-house (not skfolio / mlfinlab)
-------------------------------------
`skfolio` forces `numpy>=2 / scikit-learn>=1.6`, which cascades into a
TensorFlow-2.21-GPU break on this environment; `mlfinlab` is paid-tier. CPCV
is ~exact, well-specified index logic — a focused dependency-free
implementation is both safer (zero env risk) and fully under our control.
See `ML_IMPLEMENTATION_PLAN.md` §3 + Execution Amendments.

Method (López de Prado, 2018 — *Advances in Financial Machine Learning*, ch. 7/12)
----------------------------------------------------------------------------------
* The **sorted unique timestamp axis** is partitioned into ``n_groups``
  contiguous, temporally-ordered blocks.
* Every combination of ``n_test`` groups (``C(n_groups, n_test)`` of them) is
  used once as the test set; the remaining groups form the train set.
* **Purge**: a train timestamp ``t`` is dropped if its label horizon window
  ``[t, t+h]`` overlaps any test block — the label at ``t`` is realised using
  information inside the test period, so keeping it leaks.
* **Embargo**: train timestamps within ``embargo`` steps *after* a test block
  are additionally dropped to neutralise serial-correlation leakage.
* The combinatorial scheme yields ``φ = n_test · C(n_groups, n_test) /
  n_groups`` independent **backtest paths** (each group contributes its
  out-of-sample prediction to exactly one path per path-index), giving a
  *distribution* of performance for Deflated-Sharpe / PBO instead of a single
  fragile point estimate.

Panel adaptation
----------------
Rows are a *stack* of many symbols sharing the NSE trading calendar. Grouping
and purge/embargo are therefore computed on the **global unique-timestamp
axis**, then mapped back to the (many) panel rows that carry each timestamp.
All membership tests run on integer timestamp *codes* (not datetimes) so a
2.5 M-row panel × 28 combinations stays fast (vectorised boolean masks).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from itertools import combinations
from math import comb
from typing import Iterator

import numpy as np
import pandas as pd


@dataclass(frozen=True, slots=True)
class CPCVSplit:
    """One CPCV train/test combination, as panel row indices.

    ``test_paths`` maps each tested group id → the backtest path index its
    out-of-sample predictions belong to (López de Prado path assignment), so a
    downstream backtester can assemble ``n_paths`` coherent OOS equity curves.
    """

    combo_id: int
    test_groups: tuple[int, ...]
    train_idx: np.ndarray  # row indices into the panel arrays
    test_idx: np.ndarray
    test_paths: dict[int, int]  # group id -> path id
    test_path_of_row: np.ndarray  # path id per element of test_idx (OOF routing)


class PanelPurgedCPCV:
    """Combinatorial Purged CV over a panel's global unique-timestamp axis.

    Parameters
    ----------
    timestamps:
        Per-row timestamps aligned 1:1 with the panel feature/label arrays
        (length == n_rows). Many rows share a timestamp (one per symbol).
    horizon:
        Label look-ahead in trading-day steps (h). A row's label uses data
        through ``t + horizon``; this defines the purge window.
    n_groups, n_test:
        CPCV parameters. ``n_test`` must be ≥ 2 for a combinatorial path
        distribution (``n_test == 1`` degenerates to plain purged K-fold).
    embargo:
        Extra trading-day steps removed from train *after* each test block.
    """

    def __init__(
        self,
        timestamps: np.ndarray | pd.DatetimeIndex | pd.Series,
        *,
        horizon: int = 5,
        n_groups: int = 8,
        n_test: int = 2,
        embargo: int = 5,
    ) -> None:
        if n_groups < 2:
            raise ValueError(f"n_groups must be ≥ 2, got {n_groups}")
        if not 1 <= n_test < n_groups:
            raise ValueError(f"n_test must be in [1, n_groups), got {n_test}")
        if horizon < 0 or embargo < 0:
            raise ValueError("horizon and embargo must be non-negative")

        # Normalise to UTC-naive nanosecond precision before building the axis.
        #
        # Timestamps arrive from two code paths in the orchestrator:
        #   • step-4 (_build_cv_plan): parquet column is datetime64[us, UTC];
        #     .values strips TZ but preserves microsecond precision →
        #     datetime64[us] numpy array.
        #   • step-5 (_train_xgboost): same column cast explicitly via
        #     np.asarray(ts, dtype="datetime64[ns]") → datetime64[ns].
        #
        # pandas 3.x DatetimeIndex.asi8 returns the raw internal integer,
        # which is in MICROSECONDS for datetime64[us] and NANOSECONDS for
        # datetime64[ns] — a 1000× difference for the same instant.
        # axis_fingerprint hashes those integers, so us vs ns produces a
        # guaranteed mismatch even when both axes represent the same
        # timestamps.  Normalising here makes the fingerprint invariant to
        # the caller's precision or timezone representation.
        _idx = pd.DatetimeIndex(pd.to_datetime(np.asarray(timestamps)))
        if _idx.tz is not None:
            _idx = _idx.tz_convert("UTC").tz_localize(None)
        ts: pd.DatetimeIndex = _idx.astype("datetime64[ns]")

        if len(ts) == 0:
            raise ValueError("timestamps is empty")

        # Integer-code every row by its position on the sorted UNIQUE axis.
        # `codes[i]` ∈ [0, n_unique) is the unique-timestamp ordinal of row i;
        # all subsequent purge/embargo math is integer-fast.
        unique_ts, codes = np.unique(ts.values, return_inverse=True)
        self._row_code: np.ndarray = codes.astype(np.int64)
        self._n_unique: int = len(unique_ts)
        self.unique_ts: pd.DatetimeIndex = pd.DatetimeIndex(unique_ts)

        self.horizon = int(horizon)
        self.embargo = int(embargo)
        self.n_groups = int(n_groups)
        self.n_test = int(n_test)

        if self._n_unique < n_groups:
            raise ValueError(
                f"only {self._n_unique} unique timestamps for n_groups={n_groups}"
            )

        # Contiguous, temporally-ordered, near-equal group boundaries on the
        # unique axis: group g owns unique codes [starts[g], starts[g+1]).
        self._group_starts: np.ndarray = np.linspace(
            0, self._n_unique, n_groups + 1, dtype=np.int64
        )
        self._combos: list[tuple[int, ...]] = list(
            combinations(range(n_groups), n_test)
        )
        self._path_of: dict[tuple[int, int], int] = self._assign_paths()

    # ── Static combinatorics ──────────────────────────────────────────────────

    @staticmethod
    def n_splits(n_groups: int, n_test: int) -> int:
        """Number of train/test combinations = C(n_groups, n_test)."""
        return comb(n_groups, n_test)

    @staticmethod
    def n_paths(n_groups: int, n_test: int) -> int:
        """Number of backtest paths φ = n_test · C(n_groups, n_test) / n_groups."""
        return n_test * comb(n_groups, n_test) // n_groups

    # ── Path assignment (López de Prado) ──────────────────────────────────────

    def _assign_paths(self) -> dict[tuple[int, int], int]:
        """Assign each (combo, tested-group) to a path.

        Each group is tested in exactly ``C(n_groups-1, n_test-1)`` combos
        (== ``n_paths``). The j-th time a group appears as a test group (in
        deterministic combo order) its OOS predictions feed path ``j``. By
        construction every path then covers all ``n_groups`` groups exactly
        once.
        """
        seen: dict[int, int] = {g: 0 for g in range(self.n_groups)}
        mapping: dict[tuple[int, int], int] = {}
        for combo_id, groups in enumerate(self._combos):
            for g in groups:
                mapping[(combo_id, g)] = seen[g]
                seen[g] += 1
        return mapping

    # ── Group → unique-code range ─────────────────────────────────────────────

    def _group_code_range(self, g: int) -> tuple[int, int]:
        return int(self._group_starts[g]), int(self._group_starts[g + 1])

    # ── Iteration ─────────────────────────────────────────────────────────────

    def split(self) -> Iterator[CPCVSplit]:
        """Yield one :class:`CPCVSplit` per combination (purged + embargoed)."""
        row_code = self._row_code
        for combo_id, test_groups in enumerate(self._combos):
            # Test mask over the UNIQUE axis (union of the test groups' ranges).
            test_unit = np.zeros(self._n_unique, dtype=bool)
            for g in test_groups:
                lo, hi = self._group_code_range(g)
                test_unit[lo:hi] = True

            # Forbidden = test ∪ purge ∪ embargo, all on the unique axis.
            forbidden = test_unit.copy()
            test_codes = np.flatnonzero(test_unit)
            if test_codes.size:
                # PURGE: a train code c leaks if its label window [c, c+h]
                # reaches into any test code → forbid [test_lo - h, test_hi].
                # EMBARGO: also forbid (test_hi, test_hi + embargo].
                # Contiguity is not assumed — operate on every maximal test run.
                boundaries = np.flatnonzero(np.diff(test_codes) > 1)
                run_starts = np.r_[test_codes[0], test_codes[boundaries + 1]]
                run_ends = np.r_[test_codes[boundaries], test_codes[-1]]
                for s, e in zip(run_starts, run_ends):
                    plo = max(0, s - self.horizon)
                    ehi = min(self._n_unique, e + 1 + self.embargo)
                    forbidden[plo:ehi] = True

            train_unit = ~forbidden  # everything not test/purged/embargoed

            # Per-unique-timestamp → path id for THIS combo (-1 outside test
            # groups). Lets us route every test row to its backtest path in
            # one vectorised gather (OOF assembly for the φ paths).
            path_of_unit = np.full(self._n_unique, -1, dtype=np.int64)
            for g in test_groups:
                lo, hi = self._group_code_range(g)
                path_of_unit[lo:hi] = self._path_of[(combo_id, g)]

            test_idx = np.flatnonzero(test_unit[row_code])
            train_idx = np.flatnonzero(train_unit[row_code])
            test_paths = {g: self._path_of[(combo_id, g)] for g in test_groups}
            test_path_of_row = path_of_unit[row_code[test_idx]]

            yield CPCVSplit(
                combo_id=combo_id,
                test_groups=tuple(test_groups),
                train_idx=train_idx,
                test_idx=test_idx,
                test_paths=test_paths,
                test_path_of_row=test_path_of_row,
            )

    # ── Introspection / persistence helpers ───────────────────────────────────

    @property
    def total_paths(self) -> int:
        return self.n_paths(self.n_groups, self.n_test)

    @property
    def axis_fingerprint(self) -> str:
        """Stable hash of the unique-timestamp axis.

        Persisted at step-4 so that if the panel is rebuilt on resume and its
        timestamp axis differs (non-deterministic data), the run fails loudly
        instead of silently producing different CPCV folds (A1 philosophy).
        """
        h = hashlib.sha256()
        h.update(self.unique_ts.asi8.tobytes())
        return h.hexdigest()

    def plan_summary(self) -> dict:
        """JSON-serialisable description of the CV plan (for the checkpoint)."""
        return {
            "scheme": "PanelPurgedCPCV",
            "n_groups": self.n_groups,
            "n_test": self.n_test,
            "horizon": self.horizon,
            "embargo": self.embargo,
            "n_unique_timestamps": self._n_unique,
            "n_splits": self.n_splits(self.n_groups, self.n_test),
            "n_paths": self.total_paths,
            "group_starts": self._group_starts.tolist(),
            "first_ts": str(self.unique_ts[0]),
            "last_ts": str(self.unique_ts[-1]),
            "axis_fingerprint": self.axis_fingerprint,
        }


def purged_holdout_split(
    timestamps: np.ndarray | pd.DatetimeIndex | pd.Series,
    *,
    horizon: int = 5,
    embargo: int = 5,
    train_frac: float = 0.8,
) -> tuple[np.ndarray, np.ndarray]:
    """Single time-ordered, purged + embargoed train/test split for a panel.

    Used where the combinatorial path distribution is *not* required — the
    one-time hyperparameter-tuning holdout (A2.4) and the sequence-model split
    (A2.5, combinatorial CPCV deferred to Workstream B). Leakage-safe by the
    same rule as :class:`PanelPurgedCPCV`: a ``horizon + embargo`` gap of whole
    timestamps is removed between the (earlier) train block and the (later)
    test block, so no train label window ``[t, t+horizon]`` can reach the test
    period.

    Returns ``(train_idx, test_idx)`` panel row indices — disjoint, with train
    strictly preceding test in time. Raises if either side is empty (the data
    window is too short for the requested gap, rather than silently degrading).
    """
    if not 0.0 < train_frac < 1.0:
        raise ValueError(f"train_frac must be in (0, 1), got {train_frac}")
    if horizon < 0 or embargo < 0:
        raise ValueError("horizon and embargo must be non-negative")

    ts = pd.DatetimeIndex(pd.to_datetime(np.asarray(timestamps)))
    if len(ts) == 0:
        raise ValueError("timestamps is empty")
    _, codes = np.unique(ts.values, return_inverse=True)
    n_unique = int(codes.max()) + 1
    if n_unique < 3:
        raise ValueError(f"need ≥ 3 unique timestamps, got {n_unique}")

    cut = int(n_unique * train_frac)
    test_start = min(cut + horizon + embargo, n_unique)

    train_idx = np.flatnonzero(codes < cut)
    test_idx = np.flatnonzero(codes >= test_start)
    if train_idx.size == 0 or test_idx.size == 0:
        raise ValueError(
            f"purged holdout produced an empty side (n_unique={n_unique}, "
            f"cut={cut}, gap={horizon + embargo}); widen the data window or "
            f"reduce horizon/embargo — refusing to emit a degenerate split."
        )
    return train_idx, test_idx
