"""
Ensemble Trainer — Workstream A5
=================================
Combines the XGBoost and sequence (GRU) members into a weighted ensemble and
selects the blend weights on the **net-of-cost, calibrated, leakage-free
Deflated Sharpe Ratio**, not on raw classification accuracy or an
uncost-adjusted Sharpe (the RC that shipped an unvalidated 1.0.0).

Design (owner-confirmed, 2026-05-19)
------------------------------------
* The weighting operates on **pre-computed calibrated P(UP)** for each member
  on a single leakage-free joint set: the GRU per-symbol purged-val rows
  intersected, by ``(symbol, timestamp)``, with the XGBoost CPCV OOF rows.
  Every probability on both sides is genuinely out-of-fold — no member ever
  saw its own rows in training or HPO.
* "Paths" for the Deflated Sharpe selection-bias correction are the
  **per-symbol purged windows** (one coherent OOS series per symbol).
* **Inner objective** = mean per-period *net* Sharpe across paths (smooth,
  well-posed for a continuous optimiser) with an L2 prior pulling toward
  equal weights. Cost model = the A3 cost-aware backtester (NSE statutory
  charges + slippage), long-only / CNC / 5 bps (the live paper-trading
  product).
* **Promotion bar** = the full Deflated Sharpe Ratio (A3
  ``compute_dsr_and_pbo``, single authority — no duplicated DSR math). The
  ensemble is **rejected unless its net DSR strictly beats the best
  standalone calibrated member**; on rejection the orchestrator ships that
  best standalone member (a designed, audited outcome — never a silent
  metric downgrade).

Rationale for the inner-objective / promotion-bar split: the DSR is a
probability whose selection-bias term is parameterised by a *discrete*
trial count; embedding it inside a *continuous* weight optimiser is
ill-posed and numerically flat. Deflation belongs at the
evaluation/promotion stage — which is exactly where A6 already gates on it.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import joblib
import numpy as np

from app.ml.evaluation import compute_dsr_and_pbo
from app.ml.evaluation.backtest import TRAINING_DECISION_THRESHOLD

logger = logging.getLogger(__name__)

# Accretion must be a *strict* improvement; this guards against ties created
# by float noise being read as "the ensemble helps".
_ACCRETION_EPS: float = 1e-9


class EnsembleNotAccretiveError(RuntimeError):
    """Raised when the optimally-weighted ensemble does not strictly beat the
    best standalone calibrated member on net Deflated Sharpe.

    This is **not** an error condition to suppress — it is the A5 decision
    that the ensemble adds no risk-adjusted value, so the pipeline must ship
    the best standalone member instead. The orchestrator catches this,
    records an audited ``accretive=False`` diagnostic, and one-hot-weights
    the recommended member.
    """

    def __init__(
        self,
        recommended_weights: Dict[str, float],
        ensemble_net_dsr: float,
        best_standalone_net_dsr: float,
        best_standalone: str,
    ) -> None:
        self.recommended_weights = recommended_weights
        self.ensemble_net_dsr = ensemble_net_dsr
        self.best_standalone_net_dsr = best_standalone_net_dsr
        self.best_standalone = best_standalone
        super().__init__(
            f"Ensemble net DSR {ensemble_net_dsr:.6f} does not beat the best "
            f"standalone member '{best_standalone}' (net DSR "
            f"{best_standalone_net_dsr:.6f}). Shipping the standalone member."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Backtest assumptions for the net-DSR objective (locked decision 2026-05-19:
# long-only / CNC / 5 bps — matches the live paper-trading product and the A3
# defaults so the weighting bar equals the promotion bar).
# ──────────────────────────────────────────────────────────────────────────────

_BACKTEST_DEFAULTS: Dict[str, Any] = {
    "mode":                  "long_only",
    "product_type":          "CNC",
    "slippage_bps":          5.0,
    "notional":              100_000.0,
    "entry_price":           1_000.0,
    "periods_per_year":      252,
    "risk_free_rate_annual": 0.05,
}


class EnsembleTrainer:
    """Calibrated, accretion-gated XGBoost + GRU probability ensemble."""

    def __init__(
        self,
        xgboost_model: Any,
        gru_model: Any,
        weights: Optional[Dict[str, float]] = None,
        *,
        xgb_calibrator: Any = None,
        gru_calibrator: Any = None,
    ) -> None:
        self.xgboost_model = xgboost_model
        self.gru_model = gru_model
        self.xgb_calibrator = xgb_calibrator
        self.gru_calibrator = gru_calibrator

        self.weights: Dict[str, float] = weights or {"xgboost": 0.6, "gru": 0.4}
        self.min_confidence = 0.4
        self.optimized_weights: Optional[Dict[str, float]] = None
        self.optimization_diagnostics: Optional[Dict[str, Any]] = None

    # ──────────────────────────────────────────────────────────────────────
    # Weighting on net Deflated Sharpe (the A5 deliverable)
    # ──────────────────────────────────────────────────────────────────────

    def optimize_weights_on_oof(
        self,
        *,
        p_xgb: np.ndarray,
        p_gru: np.ndarray,
        fwd_ret: np.ndarray,
        path_id: np.ndarray,
        l2: float = 0.10,
        grid_points: int = 201,
        backtest: Optional[Dict[str, Any]] = None,
        timestamps: Optional[np.ndarray] = None,
        horizon_days: int = 1,
    ) -> Dict[str, Any]:
        """Select blend weights on the leakage-free joint OOF set.

        Parameters
        ----------
        p_xgb, p_gru : (n,) **calibrated** P(UP) for each member, row-aligned.
        fwd_ret      : (n,) h-bar forward returns, row-aligned.
        path_id      : (n,) integer per-symbol path id (the purged-val window
                       the row belongs to) — the DSR selection-bias paths.
        l2           : strength of the prior pulling weights toward equal.
        grid_points  : resolution of the deterministic 1-D weight scan.
        backtest     : overrides for the cost model (defaults = locked
                       long-only / CNC / 5 bps).

        Returns
        -------
        Diagnostics dict (also stored on ``self``). Sets ``self.weights`` /
        ``self.optimized_weights`` to the accretion-gated result.

        Raises
        ------
        ValueError                  — malformed / insufficient inputs
                                      (fail-loud; never silently degrade).
        EnsembleNotAccretiveError   — ensemble does not beat best standalone.
        """
        p_xgb = np.asarray(p_xgb, dtype=np.float64)
        p_gru = np.asarray(p_gru, dtype=np.float64)
        fwd_ret = np.asarray(fwd_ret, dtype=np.float64)
        path_id = np.asarray(path_id)

        n = len(p_xgb)
        if not (n == len(p_gru) == len(fwd_ret) == len(path_id)):
            raise ValueError(
                f"optimize_weights_on_oof inputs are not row-aligned: "
                f"p_xgb={n} p_gru={len(p_gru)} fwd_ret={len(fwd_ret)} "
                f"path_id={len(path_id)}."
            )
        if n == 0:
            raise ValueError("Empty joint OOF set — cannot optimise weights.")
        for name, p in (("p_xgb", p_xgb), ("p_gru", p_gru)):
            if not np.all(np.isfinite(p)) or p.min() < 0.0 or p.max() > 1.0:
                raise ValueError(
                    f"{name} must be finite probabilities in [0, 1] "
                    "(calibrated P(UP)). Refusing to weight on invalid scores."
                )

        unique_paths = np.unique(path_id)
        if unique_paths.size < 2:
            raise ValueError(
                f"Need ≥ 2 per-symbol paths for a Deflated-Sharpe selection-"
                f"bias correction, got {unique_paths.size}. The joint OOF set "
                "is degenerate — fix the join, do not weaken the bar."
            )

        bt = {**_BACKTEST_DEFAULTS, **(backtest or {})}
        if bt["mode"] not in ("long_only", "long_short"):
            raise ValueError(
                f"Unknown backtest mode {bt['mode']!r}; expected "
                "'long_only' or 'long_short'."
            )
        daily_rfr = (1.0 + bt["risk_free_rate_annual"]) ** (
            1.0 / bt["periods_per_year"]
        ) - 1.0

        # ── Hot-loop cost model: the per-active-bar cost is CONSTANT in w
        # (it depends only on product / notional / entry price / slippage),
        # so we evaluate the Decimal charge calculator exactly ONCE here.
        # The inner objective then becomes a pure numpy expression — A3
        # remains the single authority for the *official* net DSR reported
        # in diagnostics via :meth:`_net_dsr`.
        from decimal import Decimal
        from app.ml.evaluation.backtest import round_trip_charge_fraction

        ep_dec = Decimal(str(bt["entry_price"]))
        qty = max(1, int(bt["notional"] / bt["entry_price"]))
        charge_frac = float(
            round_trip_charge_fraction(bt["product_type"], ep_dec, ep_dec, qty)
        )
        slip_frac = float(bt["slippage_bps"]) * 2.0 / 10_000.0
        unit_cost = charge_frac + slip_frac  # deducted on every active bar

        # Pre-split row indices per path once; cache the per-path forward
        # returns and the per-path mean / std normalisation constants the
        # hot loop reuses.
        path_rows: list[np.ndarray] = [
            np.flatnonzero(path_id == pid) for pid in unique_paths
        ]
        path_fwd:  list[np.ndarray] = [fwd_ret[rows] for rows in path_rows]
        path_xgb:  list[np.ndarray] = [p_xgb[rows]   for rows in path_rows]
        path_gru:  list[np.ndarray] = [p_gru[rows]   for rows in path_rows]

        long_only = bt["mode"] == "long_only"

        def _mean_path_net_sharpe(w: float) -> float:
            """Mean per-period NET-of-cost Sharpe across the per-symbol paths.

            Math matches A3 ``strategy_returns`` + ``per_period_sharpe``
            (verified by the A3 acceptance suite) — but vectorised with
            constants hoisted out of the loop so the optimiser's 200-step
            grid scan stays sub-100 ms even on millions of rows.
            """
            srs: list[float] = []
            w_g = 1.0 - w
            for ps_xgb, ps_gru, r in zip(path_xgb, path_gru, path_fwd):
                p_ens = w * ps_xgb + w_g * ps_gru
                up = p_ens >= TRAINING_DECISION_THRESHOLD
                if long_only:
                    # pos ∈ {0, +1}; active == pos. Costs apply only on
                    # active bars; the risk-free rate is subtracted from
                    # every observation (matching A3 per_period_sharpe).
                    net = np.where(up, r - unit_cost, 0.0)
                else:
                    # long_short: pos ∈ {−1, +1}, always active.
                    pos = np.where(up, 1.0, -1.0)
                    net = pos * r - unit_cost
                excess = net - daily_rfr
                if excess.size < 2:
                    continue
                std = float(excess.std(ddof=1))
                if std == 0.0:
                    srs.append(0.0)
                else:
                    srs.append(float(excess.mean() / std))
            return float(np.mean(srs)) if srs else 0.0

        # w = weight on XGBoost; GRU = 1 − w. Deterministic dense scan +
        # parabolic refine: the thresholded objective is a fine staircase in
        # w, so a gradient optimiser is the wrong tool — a dense scan is
        # robust, reproducible, and free of SLSQP boundary artefacts.
        w_eq = 0.5
        grid = np.linspace(0.0, 1.0, grid_points)

        def _penalised(w: float) -> float:
            reward = _mean_path_net_sharpe(w)
            prior = l2 * ((w - w_eq) ** 2 + ((1.0 - w) - w_eq) ** 2)
            return reward - prior

        curve = np.array([_penalised(float(w)) for w in grid])
        k = int(np.argmax(curve))

        # Parabolic interpolation through the best interior triple for a
        # sub-grid optimum (skipped at the boundary).
        if 0 < k < len(grid) - 1:
            y0, y1, y2 = curve[k - 1], curve[k], curve[k + 1]
            denom = y0 - 2.0 * y1 + y2
            if denom < 0.0:  # concave → real maximum
                delta = 0.5 * (y0 - y2) / denom
                w_star = float(np.clip(grid[k] + delta * (grid[1] - grid[0]),
                                       grid[k - 1], grid[k + 1]))
            else:
                w_star = float(grid[k])
        else:
            w_star = float(grid[k])

        w_xgb = float(np.clip(w_star, 0.0, 1.0))
        w_gru = 1.0 - w_xgb
        weights = {"xgboost": w_xgb, "gru": w_gru}

        # Net DSR (the bar): A3 is the single authority — no duplicated maths.
        ens_p = w_xgb * p_xgb + w_gru * p_gru
        ens_dsr = self._net_dsr(ens_p, fwd_ret, path_id, bt, timestamps, horizon_days)
        solo_xgb = self._net_dsr(p_xgb, fwd_ret, path_id, bt, timestamps, horizon_days)
        solo_gru = self._net_dsr(p_gru, fwd_ret, path_id, bt, timestamps, horizon_days)

        standalone = {"xgboost": solo_xgb, "gru": solo_gru}
        best_name = max(standalone, key=lambda m: standalone[m]["deflated_sharpe"])
        best_solo_dsr = standalone[best_name]["deflated_sharpe"]
        ens_net_dsr = ens_dsr["deflated_sharpe"]

        diagnostics: Dict[str, Any] = {
            "weights":                weights,
            "ensemble_net_dsr":       float(ens_net_dsr),
            "ensemble_pbo":           float(ens_dsr["pbo"]),
            "standalone_net_dsr":     {
                m: float(s["deflated_sharpe"]) for m, s in standalone.items()
            },
            "standalone_pbo":         {
                m: float(s["pbo"]) for m, s in standalone.items()
            },
            "best_standalone":        best_name,
            "best_standalone_net_dsr": float(best_solo_dsr),
            "n_paths":                int(unique_paths.size),
            "n_obs":                  int(n),
            "l2":                     float(l2),
            "backtest":               {k: bt[k] for k in (
                "mode", "product_type", "slippage_bps", "notional",
            )},
            "objective_w_grid":       [float(x) for x in grid],
            "objective_curve":        [float(x) for x in curve],
            "w_star":                 w_xgb,
        }

        if ens_net_dsr <= best_solo_dsr + _ACCRETION_EPS:
            one_hot = {best_name: 1.0,
                       ("gru" if best_name == "xgboost" else "xgboost"): 0.0}
            diagnostics["accretive"] = False
            diagnostics["weights"] = one_hot
            self.optimization_diagnostics = diagnostics
            logger.warning(
                "A5: ensemble NOT accretive — net DSR %.6f ≤ best standalone "
                "'%s' %.6f. Shipping standalone.",
                ens_net_dsr, best_name, best_solo_dsr,
            )
            raise EnsembleNotAccretiveError(
                recommended_weights=one_hot,
                ensemble_net_dsr=float(ens_net_dsr),
                best_standalone_net_dsr=float(best_solo_dsr),
                best_standalone=best_name,
            )

        diagnostics["accretive"] = True
        self.weights = weights
        self.optimized_weights = weights
        self.optimization_diagnostics = diagnostics
        logger.info(
            "A5: ensemble accretive ✓  weights xgb=%.4f gru=%.4f  "
            "net DSR %.6f > best standalone '%s' %.6f  (PBO %.4f, %d paths, n=%d)",
            w_xgb, w_gru, ens_net_dsr, best_name, best_solo_dsr,
            ens_dsr["pbo"], unique_paths.size, n,
        )
        return diagnostics

    @staticmethod
    def _net_dsr(
        proba: np.ndarray,
        fwd_ret: np.ndarray,
        path_id: np.ndarray,
        bt: Dict[str, Any],
        timestamps: Optional[np.ndarray] = None,
        horizon_days: int = 1,
    ) -> Dict[str, Any]:
        """Net Deflated Sharpe + PBO for a probability vector, via the A3
        authority. Paths = per-symbol purged windows.

        ``timestamps`` (row-aligned) enable the daily-portfolio DSR basis in
        compute_dsr_and_pbo; without them the legacy pooled-row basis
        saturates DSR toward 1.0 (see deflated_sharpe.py, 2026-07-18).
        """
        paths = [
            {
                "proba":          proba[path_id == pid],
                "forward_return": fwd_ret[path_id == pid],
                **(
                    {"timestamp": timestamps[path_id == pid]}
                    if timestamps is not None else {}
                ),
            }
            for pid in np.unique(path_id)
        ]
        return compute_dsr_and_pbo(
            paths,
            horizon_days=horizon_days,
            mode=bt["mode"],
            notional=bt["notional"],
            product_type=bt["product_type"],
            entry_price=bt["entry_price"],
            slippage_bps=bt["slippage_bps"],
            periods_per_year=bt["periods_per_year"],
            risk_free_rate_annual=bt["risk_free_rate_annual"],
        )

    # ──────────────────────────────────────────────────────────────────────
    # Calibration-aware inference (matches production EnsemblePredictor:
    # calibrate each member, THEN weight)
    # ──────────────────────────────────────────────────────────────────────

    def predict(
        self,
        X_tabular: np.ndarray,
        X_sequence: np.ndarray,
        apply_confidence_threshold: bool = True,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Calibrated weighted-probability ensemble prediction.

        Returns ``(predictions, probabilities)`` where predictions are binary
        {0, 1}, or −1 (HOLD) where max probability < ``min_confidence`` and
        the threshold is applied.
        """
        _, xgb_proba = self._predict_xgboost(X_tabular)
        _, gru_proba = self._predict_gru(X_sequence)

        xgb_proba = self._apply_calibrator(self.xgb_calibrator, xgb_proba)
        gru_proba = self._apply_calibrator(self.gru_calibrator, gru_proba)

        ensemble_proba = (
            self.weights["xgboost"] * xgb_proba
            + self.weights["gru"] * gru_proba
        )
        max_proba = ensemble_proba.max(axis=1)
        predictions = ensemble_proba.argmax(axis=1)
        if apply_confidence_threshold:
            predictions = np.where(
                max_proba >= self.min_confidence, predictions, -1
            )
        return predictions, ensemble_proba

    def evaluate(
        self,
        X_tabular: np.ndarray,
        X_sequence: np.ndarray,
        y_true: np.ndarray,
        returns: Optional[np.ndarray] = None,
    ) -> Dict[str, Any]:
        """Diagnostic evaluation (informational; the promotion bar is the
        net-DSR diagnostics produced by :meth:`optimize_weights_on_oof`)."""
        from .evaluator import (
            calculate_classification_metrics,
            calculate_financial_metrics,
        )

        predictions, probabilities = self.predict(X_tabular, X_sequence)
        class_metrics = calculate_classification_metrics(
            y_true, predictions, probabilities
        )
        fin_metrics = (
            calculate_financial_metrics(predictions, returns)
            if returns is not None
            else {}
        )
        return {
            **class_metrics,
            **fin_metrics,
            "weights": self.weights,
            "n_samples": len(y_true),
        }

    # ──────────────────────────────────────────────────────────────────────
    # Persistence
    # ──────────────────────────────────────────────────────────────────────

    def save(self, path: str) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "weights":                 self.weights,
                "optimized_weights":       self.optimized_weights,
                "min_confidence":          self.min_confidence,
                "optimization_diagnostics": self.optimization_diagnostics,
            },
            path,
        )
        logger.info("Ensemble config saved to %s", path)

    def load(self, path: str) -> None:
        config = joblib.load(path)
        self.weights = config["weights"]
        self.optimized_weights = config.get("optimized_weights")
        self.min_confidence = config.get("min_confidence", 0.4)
        self.optimization_diagnostics = config.get("optimization_diagnostics")
        logger.info("Ensemble config loaded from %s", path)

    # ──────────────────────────────────────────────────────────────────────
    # Member inference helpers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _apply_calibrator(calibrator: Any, proba: np.ndarray) -> np.ndarray:
        """Apply a fitted ConfidenceCalibrator to (n, 2) probabilities.

        A missing calibrator is logged and the raw scores pass through —
        :meth:`predict` is diagnostic only. The A5 weighting path never
        relies on this: it consumes calibrated probabilities assembled
        upstream in the orchestrator (fail-loud if a calibrator is absent
        there)."""
        if calibrator is None:
            return proba
        return np.asarray(calibrator.calibrate(proba), dtype=proba.dtype)

    def _predict_xgboost(
        self, X: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        import xgboost as xgb

        dtest = xgb.DMatrix(X)
        if hasattr(self.xgboost_model, "num_class"):
            proba = self.xgboost_model.predict(dtest)
            predictions = proba.argmax(axis=1)
        else:
            p1 = self.xgboost_model.predict(dtest)
            proba = np.column_stack([1.0 - p1, p1])
            predictions = (p1 > 0.5).astype(int)
        return predictions, proba

    def _predict_gru(self, X: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        proba = self.gru_model.predict(X, verbose=0)
        return proba.argmax(axis=1), proba


def create_ensemble(
    xgboost_model: Any,
    gru_model: Any,
    weights: Optional[Dict[str, float]] = None,
    *,
    xgb_calibrator: Any = None,
    gru_calibrator: Any = None,
) -> EnsembleTrainer:
    """Factory: build an EnsembleTrainer from trained members."""
    return EnsembleTrainer(
        xgboost_model,
        gru_model,
        weights,
        xgb_calibrator=xgb_calibrator,
        gru_calibrator=gru_calibrator,
    )
