"""
Forecaster evaluation metrics — binary direction + calibration.
===============================================================
Pure, side-effect-free metrics for evaluating a directional forecaster against
realized outcomes.  Used by the Gemini news-forecaster backtest (Phase 4) to
decide whether the model adds value over the ML/NLP baseline and whether it is
well-calibrated enough to act on.

Honesty principle (shared with the ML evaluator): a metric that cannot be
computed from the data is reported as ``None`` — NEVER fabricated as 0.0.  A
fabricated zero makes an unvalidated model look validated.

Metric choices follow current forecasting-evaluation practice:
  - Direction accuracy + per-class precision/recall (BUY=up, SELL=down).
  - Brier score (mean squared error of P(up) vs realized) — 0 is perfect; it
    penalizes over-confidence.
  - Expected Calibration Error (ECE) + a reliability table — LLM forecasters are
    routinely over-confident, so calibration is evaluated explicitly.

HOLD predictions are abstentions: excluded from accuracy/Brier and reported via
``coverage`` (fraction of points on which the forecaster took a directional view).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sklearn.metrics import precision_score, recall_score


def realized_up(forward_return: float) -> int:
    """Realized binary direction: 1 if the forward return is positive, else 0."""
    return 1 if forward_return > 0 else 0


def p_up_from_forecast(direction: str, confidence: float) -> float | None:
    """Map a (direction, confidence) forecast to P(up), or None for HOLD (abstain).

    BUY → confidence is P(up).  SELL → confidence is P(down), so P(up)=1−confidence.
    HOLD → no directional view → None (excluded from scoring, counted in coverage).
    """
    d = (direction or "").upper()
    try:
        c = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        return None
    if d == "BUY":
        return c
    if d == "SELL":
        return 1.0 - c
    return None


@dataclass
class ReliabilityBin:
    lo: float
    hi: float
    count: int
    mean_confidence: float
    accuracy: float


@dataclass
class DirectionMetrics:
    n_total: int
    n_directional: int
    coverage: float                       # n_directional / n_total
    accuracy: float | None = None         # on directional predictions only
    precision_up: float | None = None
    precision_down: float | None = None
    recall_up: float | None = None
    recall_down: float | None = None
    brier: float | None = None
    ece: float | None = None
    reliability: list[ReliabilityBin] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {
            "n_total": self.n_total,
            "n_directional": self.n_directional,
            "coverage": round(self.coverage, 4),
            "accuracy": _r(self.accuracy),
            "precision_up": _r(self.precision_up),
            "precision_down": _r(self.precision_down),
            "recall_up": _r(self.recall_up),
            "recall_down": _r(self.recall_down),
            "brier": _r(self.brier),
            "ece": _r(self.ece),
            "reliability": [
                {"range": [round(b.lo, 2), round(b.hi, 2)], "n": b.count,
                 "mean_confidence": round(b.mean_confidence, 4),
                 "accuracy": round(b.accuracy, 4)}
                for b in self.reliability
            ],
        }
        return d


def _r(v: float | None, nd: int = 4) -> float | None:
    return None if v is None else round(float(v), nd)


def expected_calibration_error(
    confidences: np.ndarray, correct: np.ndarray, n_bins: int = 10
) -> tuple[float, list[ReliabilityBin]]:
    """Standard ECE over equal-width confidence bins.

    ``confidences`` is the predicted-class confidence (max(P, 1−P)); ``correct``
    is 1 where the predicted class matched the outcome.  ECE is the sample-weighted
    mean gap between confidence and accuracy across bins.
    """
    edges = np.linspace(0.5, 1.0, n_bins + 1)  # predicted-class conf ∈ [0.5, 1]
    n = len(confidences)
    ece = 0.0
    bins: list[ReliabilityBin] = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (confidences >= lo) & (confidences < hi if i < n_bins - 1 else confidences <= hi)
        cnt = int(mask.sum())
        if cnt == 0:
            continue
        conf = float(confidences[mask].mean())
        acc = float(correct[mask].mean())
        ece += (cnt / n) * abs(conf - acc)
        bins.append(ReliabilityBin(lo=float(lo), hi=float(hi), count=cnt,
                                   mean_confidence=conf, accuracy=acc))
    return ece, bins


def evaluate_directions(
    p_ups: list[float | None], realized: list[int], *, n_bins: int = 10
) -> DirectionMetrics:
    """Evaluate directional forecasts (P(up) per point) against realized outcomes.

    ``p_ups[i]`` is P(up) for point i (None = HOLD/abstain).  ``realized[i]`` is
    the realized binary direction (1 up / 0 down).  Points with p_up=None are
    excluded from accuracy/Brier/calibration and counted in ``coverage``.
    """
    if len(p_ups) != len(realized):
        raise ValueError("p_ups and realized must be the same length")
    n_total = len(p_ups)

    pairs = [(p, y) for p, y in zip(p_ups, realized) if p is not None]
    n_dir = len(pairs)
    coverage = (n_dir / n_total) if n_total else 0.0
    m = DirectionMetrics(n_total=n_total, n_directional=n_dir, coverage=coverage)
    if n_dir == 0:
        return m  # nothing directional → all scoring metrics stay None (honest)

    p = np.array([x[0] for x in pairs], dtype=float)
    y = np.array([x[1] for x in pairs], dtype=int)
    pred = (p >= 0.5).astype(int)

    m.accuracy = float((pred == y).mean())
    m.precision_up = float(precision_score(y, pred, pos_label=1, zero_division=0))
    m.precision_down = float(precision_score(y, pred, pos_label=0, zero_division=0))
    m.recall_up = float(recall_score(y, pred, pos_label=1, zero_division=0))
    m.recall_down = float(recall_score(y, pred, pos_label=0, zero_division=0))
    m.brier = float(np.mean((p - y) ** 2))

    pred_class_conf = np.maximum(p, 1.0 - p)   # confidence in the predicted class
    correct = (pred == y).astype(int)
    m.ece, m.reliability = expected_calibration_error(pred_class_conf, correct, n_bins)
    return m
