"""
Feedback-loader telemetry tests
===============================
Regression coverage for the ``build_panel_weights`` match-count log line.

The original implementation inferred coverage from ``weights != 1.0``, which
silently reclassified genuinely-matched rows whose weight legitimately equals
1.0 (``direction_only``/``unknown`` outcome x neutral confidence factor) as
"unmatched" — understating real coverage whenever the outcome mix skews toward
manual/expired exits or sub-70%-confidence misses. The fix counts bundle-key
hits directly; these tests pin that behavior.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from app.ml.training.feedback_loader import build_panel_weights

pytestmark = pytest.mark.unit

_LOGGER_NAME = "app.ml.training.feedback_loader"


def _write_bundle(tmp_path, rows: list[tuple[str, str, float]]):
    """Write a minimal feedback bundle parquet: (instrument_key, date, weight)."""
    df = pd.DataFrame(
        {
            "instrument_key": [r[0] for r in rows],
            "signal_date": [pd.Timestamp(r[1]) for r in rows],
            "sample_weight": [r[2] for r in rows],
        }
    )
    path = tmp_path / "feedback_weights_test.parquet"
    df.to_parquet(path)
    return path


def _panel(symbol_dates: list[tuple[str, str]]) -> tuple[np.ndarray, np.ndarray]:
    """Build (sym_all, ts_all) panel arrays in the shape the trainer passes."""
    syms = np.array([s for s, _ in symbol_dates], dtype=object)
    ts = np.array([np.datetime64(d) for _, d in symbol_dates], dtype="datetime64[ns]")
    return syms, ts


def _matched_log_line(caplog: pytest.LogCaptureFixture) -> str:
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == _LOGGER_NAME and "matched feedback bundle" in r.getMessage()
    ]
    assert len(lines) == 1, f"expected exactly one match-telemetry line, got: {lines}"
    return lines[0]


def test_matched_count_includes_weight_one_matches(tmp_path, caplog):
    """A bundle hit whose weight is exactly 1.0 is still a match (the regression)."""
    bundle = _write_bundle(
        tmp_path,
        [
            ("NSE_EQ|A", "2026-07-01", 1.0),   # direction_only-style neutral weight
            ("NSE_EQ|B", "2026-07-02", 2.5),
        ],
    )
    sym_all, ts_all = _panel(
        [
            ("NSE_EQ|A", "2026-07-01"),  # matched, weight 1.0
            ("NSE_EQ|B", "2026-07-02"),  # matched, weight 2.5
            ("NSE_EQ|C", "2026-07-03"),  # unmatched → default 1.0
        ]
    )

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        weights = build_panel_weights(bundle, sym_all, ts_all)

    np.testing.assert_allclose(weights, [1.0, 2.5, 1.0])
    assert "2 / 3 rows matched feedback bundle (66.7%)" in _matched_log_line(caplog)


def test_full_coverage_of_all_neutral_weights_reports_100_percent(tmp_path, caplog):
    """Coverage reads 100% even when every matched weight is the neutral 1.0."""
    bundle = _write_bundle(
        tmp_path,
        [
            ("NSE_EQ|A", "2026-07-01", 1.0),
            ("NSE_EQ|B", "2026-07-02", 1.0),
        ],
    )
    sym_all, ts_all = _panel([("NSE_EQ|A", "2026-07-01"), ("NSE_EQ|B", "2026-07-02")])

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        weights = build_panel_weights(bundle, sym_all, ts_all)

    np.testing.assert_allclose(weights, [1.0, 1.0])
    assert "2 / 2 rows matched feedback bundle (100.0%)" in _matched_log_line(caplog)


def test_no_matches_reports_zero(tmp_path, caplog):
    """A panel with no bundle overlap reports 0 matched and all-default weights."""
    bundle = _write_bundle(tmp_path, [("NSE_EQ|Z", "2026-01-01", 3.0)])
    sym_all, ts_all = _panel([("NSE_EQ|A", "2026-07-01"), ("NSE_EQ|B", "2026-07-02")])

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        weights = build_panel_weights(bundle, sym_all, ts_all)

    np.testing.assert_allclose(weights, [1.0, 1.0])
    assert "0 / 2 rows matched feedback bundle (0.0%)" in _matched_log_line(caplog)


def test_matched_weights_are_applied_positionally(tmp_path, caplog):
    """Weights land on the correct panel rows, not just in aggregate."""
    bundle = _write_bundle(
        tmp_path,
        [
            ("NSE_EQ|A", "2026-07-01", 0.5),
            ("NSE_EQ|A", "2026-07-02", 3.0),
        ],
    )
    sym_all, ts_all = _panel(
        [
            ("NSE_EQ|A", "2026-07-02"),  # 3.0 — same symbol, different date
            ("NSE_EQ|B", "2026-07-01"),  # unmatched (symbol differs)
            ("NSE_EQ|A", "2026-07-01"),  # 0.5
        ]
    )

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        weights = build_panel_weights(bundle, sym_all, ts_all)

    np.testing.assert_allclose(weights, [3.0, 1.0, 0.5])
    assert "2 / 3 rows matched" in _matched_log_line(caplog)
