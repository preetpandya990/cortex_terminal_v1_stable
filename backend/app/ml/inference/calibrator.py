"""
Cortex AI — Probability Calibrator
====================================
Post-hoc calibration for production ML models.

XGBoost: Beta calibration — a generalised log-linear model that handles the
bimodal overconfidence typical of boosted tree output.  Unlike Platt/isotonic
scaling, the three-parameter form can fit asymmetric score distributions.
Reference: Kull et al., "Beyond Sigmoids", AISTATS 2017.

GRU: Temperature scaling — single-scalar softmax rescaling.  Zero overfitting
risk, retains sharpness, and is optimal for limited calibration data.
Reference: Guo et al., "On Calibration of Modern Neural Networks", ICML 2017.

Both are fitted on the CPCV out-of-fold (OOF) predictions produced by the
28-combo XGBoost refit loop, NOT on the HPO validation set.  Fitting on the
HPO val set introduces selection leakage: the same data was used to guide
early stopping / model selection, so the calibrator learns to correct for
a biased score distribution that does not reflect the live distribution.
CPCV OOF rows are by construction disjoint from every training run that
produced them — zero selection leakage, zero look-ahead.

ECE is computed with equal-mass (adaptive) binning: equal-width bins produce
high-variance estimates for sparse high-confidence regions; equal-mass bins
guarantee at least N/n_bins samples per bin regardless of score distribution.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import joblib
import numpy as np
from scipy.optimize import minimize

logger = logging.getLogger(__name__)

# ── Numerical constants ───────────────────────────────────────────────────────
_EPS: float = 1e-15        # Clip floor/ceiling for log stability
_LOGIT_CLIP: float = 100.0  # Prevent exp overflow in sigmoid


class ConfidenceCalibrator:
    """
    Post-hoc probability calibrator for binary classification models.

    Supported model types
    ---------------------
    "xgboost"
        Beta calibration:  p_cal = sigmoid(a·log(s) − b·log(1−s) + c)
        Parameters (a, b, c) are fitted via L-BFGS-B on binary cross-entropy.
        Handles bimodal, overconfident score distributions that Platt scaling
        cannot fit (e.g., XGBoost with many trees and high learning rate).

    "gru"
        Temperature scaling:  p_cal = sigmoid(logit(s) / T),  T > 0
        Single scalar T is fitted via L-BFGS-B on NLL.
        Back-calculating logits from binary softmax is exact:
            logit(s) = log(P(UP)) − log(P(DOWN))
        which is numerically stable with proper clipping.

    Usage
    -----
    >>> cal = ConfidenceCalibrator("xgboost")
    >>> cal.fit(y_val, xgb_proba_val)          # (n,) or (n, 2) probabilities
    >>> print(f"ECE after calibration: {cal.ece_after:.4f}")
    >>> p_calibrated = cal.calibrate(raw_proba) # (2,) or (n, 2) → same shape
    >>> cal.save(Path("models/1d/calibrator_xgb.pkl"))
    """

    def __init__(self, model_type: Literal["xgboost", "gru"]) -> None:
        if model_type not in {"xgboost", "gru"}:
            raise ValueError(f"model_type must be 'xgboost' or 'gru', got '{model_type!r}'")

        self.model_type: Literal["xgboost", "gru"] = model_type
        self._is_fitted: bool = False

        # Beta calibration params (XGBoost) — initialised near identity
        self._a: float = 1.0
        self._b: float = 1.0
        self._c: float = 0.0

        # Temperature scaling param (GRU) — T = 1.0 is identity (no scaling)
        self._temperature: float = 1.0

        # Diagnostics populated at fit time
        self.ece_before: float | None = None
        self.ece_after:  float | None = None
        self.n_calibration_samples: int = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def fit(
        self,
        y_true: np.ndarray,
        y_proba: np.ndarray,
    ) -> "ConfidenceCalibrator":
        """
        Fit calibration parameters on a held-out validation set.

        Parameters
        ----------
        y_true  : int array (n,) with binary labels {0, 1}.
        y_proba : float array (n,) with P(class=1), or (n, 2) [P(0), P(1)].

        Returns self — supports chaining.

        Raises
        ------
        ValueError
            If y_true and y_proba have incompatible lengths, or y_true
            contains classes other than {0, 1}.
        """
        p1 = self._extract_p1(y_proba)
        y  = y_true.astype(np.float64)

        if len(p1) != len(y):
            raise ValueError(
                f"y_true length {len(y)} != y_proba length {len(p1)}"
            )
        unique = np.unique(y)
        if not set(unique.tolist()).issubset({0.0, 1.0}):
            raise ValueError(f"y_true must contain only {{0, 1}}, got {unique.tolist()}")

        self.n_calibration_samples = len(p1)
        self.ece_before = self.ece(y_true, p1)

        if self.model_type == "xgboost":
            self._fit_beta(y, p1)
        else:
            self._fit_temperature(y, p1)

        self._is_fitted = True

        p1_cal = self._calibrate_p1(p1)
        self.ece_after = self.ece(y_true, p1_cal)

        logger.info(
            "Calibration fitted [%s] | n=%d | ECE %.4f → %.4f%s",
            self.model_type,
            self.n_calibration_samples,
            self.ece_before,
            self.ece_after,
            "  ⚠ above 0.05 threshold" if self.ece_after >= 0.05 else "",
        )
        return self

    def calibrate(self, proba: np.ndarray) -> np.ndarray:
        """
        Apply calibration to raw model probabilities.

        Accepts (2,) for single-sample inference or (n, 2) for batch.
        Returns the same shape with calibrated probabilities summing to 1.

        Raises
        ------
        RuntimeError  If called before fit().
        """
        if not self._is_fitted:
            raise RuntimeError("Calibrator not fitted. Call fit() before calibrate().")

        proba = np.asarray(proba, dtype=np.float64)
        scalar_input = proba.ndim == 1
        if scalar_input:
            proba = proba[np.newaxis, :]  # (2,) → (1, 2)

        p1     = proba[:, 1]
        p1_cal = self._calibrate_p1(p1)
        result = np.column_stack([1.0 - p1_cal, p1_cal]).astype(np.float32)

        return result[0] if scalar_input else result

    def ece(
        self,
        y_true: np.ndarray,
        y_proba: np.ndarray,
        n_bins: int = 10,
    ) -> float:
        """
        Expected Calibration Error with equal-mass (adaptive) binning.

        Equal-mass bins guarantee at least N/n_bins samples per bin,
        producing stable ECE estimates even when scores cluster near 0 or 1
        (which is common for boosted trees).

        Parameters
        ----------
        y_true  : binary labels (n,)
        y_proba : P(class=1) (n,) or [P(0), P(1)] (n, 2)
        n_bins  : number of equal-mass bins (default: 10)

        Returns
        -------
        ECE ∈ [0, 1] — lower is better; target < 0.05.
        """
        p1 = self._extract_p1(y_proba)
        y  = y_true.astype(np.float64)
        n  = len(p1)

        # Equal-mass bin edges via percentiles
        bin_edges = np.percentile(p1, np.linspace(0.0, 100.0, n_bins + 1))
        bin_edges[-1] += 1e-10  # Include the rightmost point

        ece_val = 0.0
        for lo, hi in zip(bin_edges[:-1], bin_edges[1:]):
            mask = (p1 >= lo) & (p1 < hi)
            if not mask.any():
                continue
            bin_acc  = y[mask].mean()
            bin_conf = p1[mask].mean()
            ece_val += (mask.sum() / n) * abs(bin_acc - bin_conf)

        return float(ece_val)

    def save(self, path: Path) -> None:
        """
        Persist calibrator state to disk via joblib (compress=3).

        joblib with compression is thread-safe for concurrent reads;
        no locking is required when multiple async workers load the same file.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        state = {
            "model_type":            self.model_type,
            "is_fitted":             self._is_fitted,
            "a":                     self._a,
            "b":                     self._b,
            "c":                     self._c,
            "temperature":           self._temperature,
            "ece_before":            self.ece_before,
            "ece_after":             self.ece_after,
            "n_calibration_samples": self.n_calibration_samples,
        }
        joblib.dump(state, path, compress=3)
        logger.info(
            "Calibrator saved: %s  (ECE after=%.4f)",
            path.name,
            self.ece_after or 0.0,
        )

    @classmethod
    def load(cls, path: Path) -> "ConfidenceCalibrator":
        """
        Load a calibrator from disk.

        Safe to call concurrently from multiple async workers — joblib's
        read path is thread-safe (OS-level mmap synchronisation).

        Raises
        ------
        FileNotFoundError  If the file does not exist.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Calibrator file not found: {path}")

        state = joblib.load(path)

        obj = cls(state["model_type"])
        obj._is_fitted             = state["is_fitted"]
        obj._a                     = state.get("a", 1.0)
        obj._b                     = state.get("b", 1.0)
        obj._c                     = state.get("c", 0.0)
        obj._temperature           = state.get("temperature", 1.0)
        obj.ece_before             = state.get("ece_before")
        obj.ece_after              = state.get("ece_after")
        obj.n_calibration_samples  = state.get("n_calibration_samples", 0)

        logger.info(
            "Calibrator loaded: %s [%s]  ECE after=%.4f",
            path.name, obj.model_type, obj.ece_after or 0.0,
        )
        return obj

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_p1(y_proba: np.ndarray) -> np.ndarray:
        """Extract P(class=1) from (n,) or (n, 2) input — never (2,) single."""
        arr = np.asarray(y_proba, dtype=np.float64)
        if arr.ndim == 2:
            return arr[:, 1]
        return arr  # Already (n,) of P(class=1)

    def _calibrate_p1(self, p1: np.ndarray) -> np.ndarray:
        """Apply calibration to a 1-D P(class=1) array and return calibrated P(class=1)."""
        s = np.clip(p1.astype(np.float64), _EPS, 1.0 - _EPS)

        if self.model_type == "xgboost":
            # Beta calibration: sigmoid(a·log(s) − b·log(1−s) + c)
            logits = (
                self._a * np.log(s)
                - self._b * np.log(1.0 - s)
                + self._c
            )
        else:
            # Temperature scaling: sigmoid(logit(s) / T)
            # logit(s) = log(s) − log(1−s) — exact for binary softmax
            logits = (np.log(s) - np.log(1.0 - s)) / max(self._temperature, 1e-4)

        logits = np.clip(logits, -_LOGIT_CLIP, _LOGIT_CLIP)
        return 1.0 / (1.0 + np.exp(-logits))

    def _fit_beta(self, y: np.ndarray, s: np.ndarray) -> None:
        """
        Fit Beta calibration parameters (a, b, c) via L-BFGS-B.

        Objective: minimise binary cross-entropy on the validation set.
        Initialised near identity (a=1, b=1, c=0) to avoid local minima.
        Bounds prevent degenerate solutions while allowing wide flexibility.
        """
        s_clipped = np.clip(s, _EPS, 1.0 - _EPS)
        log_s     = np.log(s_clipped)
        log_1ms   = np.log(1.0 - s_clipped)

        def nll(params: np.ndarray) -> float:
            a, b, c = params
            logits = a * log_s - b * log_1ms + c
            logits = np.clip(logits, -_LOGIT_CLIP, _LOGIT_CLIP)
            p = np.clip(1.0 / (1.0 + np.exp(-logits)), _EPS, 1.0 - _EPS)
            return -float(np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))

        result = minimize(
            nll,
            x0=np.array([1.0, 1.0, 0.0]),
            method="L-BFGS-B",
            bounds=[(-10.0, 10.0), (-10.0, 10.0), (-5.0, 5.0)],
            options={"maxiter": 1000, "ftol": 1e-12, "gtol": 1e-8},
        )

        if not result.success:
            logger.warning(
                "Beta calibration optimisation did not fully converge: %s", result.message
            )

        self._a = float(result.x[0])
        self._b = float(result.x[1])
        self._c = float(result.x[2])

        logger.debug(
            "Beta calibration params: a=%.4f  b=%.4f  c=%.4f  NLL=%.6f",
            self._a, self._b, self._c, result.fun,
        )

    def _fit_temperature(self, y: np.ndarray, s: np.ndarray) -> None:
        """
        Fit temperature T ∈ (0.01, 10] by minimising NLL on the validation set.

        Uses back-calculated logits from binary softmax probabilities:
            logit(s) = log(P(UP)) − log(P(DOWN))
        which is exact for binary classification and numerically stable
        when s is clipped away from {0, 1}.
        """
        s_clipped = np.clip(s, _EPS, 1.0 - _EPS)
        logit_s   = np.log(s_clipped) - np.log(1.0 - s_clipped)

        def nll(params: np.ndarray) -> float:
            T      = max(float(params[0]), 1e-4)
            scaled = np.clip(logit_s / T, -_LOGIT_CLIP, _LOGIT_CLIP)
            p      = np.clip(1.0 / (1.0 + np.exp(-scaled)), _EPS, 1.0 - _EPS)
            return -float(np.mean(y * np.log(p) + (1.0 - y) * np.log(1.0 - p)))

        result = minimize(
            nll,
            x0=np.array([1.0]),
            method="L-BFGS-B",
            bounds=[(0.01, 10.0)],
            options={"maxiter": 500, "ftol": 1e-12, "gtol": 1e-8},
        )

        if not result.success:
            logger.warning(
                "Temperature scaling optimisation did not fully converge: %s", result.message
            )

        self._temperature = max(float(result.x[0]), 1e-4)
        logger.debug("Temperature scaling: T=%.4f  NLL=%.6f", self._temperature, result.fun)

    # ── Diagnostics ───────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        if not self._is_fitted:
            return f"ConfidenceCalibrator(model_type={self.model_type!r}, fitted=False)"

        if self.model_type == "xgboost":
            detail = f"a={self._a:.3f}, b={self._b:.3f}, c={self._c:.3f}"
        else:
            detail = f"T={self._temperature:.4f}"

        return (
            f"ConfidenceCalibrator(model_type={self.model_type!r}, "
            f"{detail}, "
            f"ECE {self.ece_before:.4f}→{self.ece_after:.4f}, "
            f"n={self.n_calibration_samples})"
        )
