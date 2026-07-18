"""
Re-score registered models' financial metrics on the daily-portfolio basis.

Context (2026-07-18)
--------------------
The evaluation stack computed time-series statistics (DSR, Sharpe, Sortino,
drawdown, total return) over POOLED (symbol × day) panel rows, treating the
cross-section as extra time periods. Consequences observed on the 1.2.0
challenger and 0.49.0 ablation control:

  - DSR printed exactly 1.0000 for all four models (T inflated ~200×,
    √(T−1) saturates Φ) — including a Sharpe-0.338 model;
  - total_return overflowed to ~1e+104 (30k-fold compounding);
  - max_drawdown/annualisation lost meaning.

The fix (aggregate_daily_portfolio + daily-basis DSR/metrics) landed in
``app/ml/evaluation/backtest.py``, ``deflated_sharpe.py`` and
``training/evaluator.py``. This script replays BOTH runs' archived
evaluation artifacts through the corrected stack and updates the four
registered rows — no retraining.

What is recomputed, from what
-----------------------------
  XGBoost DSR                : CPCV OOF paths (proba/fwd_ret/ts per row) —
                               the A3 authority object, timestamps included.
  GRU standalone DSR         : ONNX inference over the archived GRU eval
                               sequences, paths = per-symbol purged windows
                               (mirrors EnsembleTrainer._net_dsr semantics).
  sharpe/sortino/drawdown/
  total_return (both models) : model inference over the archived eval arrays
                               through the corrected calculate_financial_
                               metrics with timestamps.

Classification metrics (accuracy/auc_pr/...) and trade-level stats
(win_rate, n_trades, profit_factor) are NOT touched — the panel bug never
affected them.

Audit trail: prior values are preserved under ``legacy_pooled_basis`` inside
``training_metrics`` and a ``lineage['rescore']`` record is written.

Usage
-----
  python scripts/rescore_financial_metrics.py            # dry run (default)
  python scripts/rescore_financial_metrics.py --execute  # write to registry

Exit codes: 0 success · 2 fatal
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import numpy as np  # noqa: E402

logger = logging.getLogger("rescore_financial_metrics")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_EXIT_OK, _EXIT_ERROR = 0, 2

#: Label horizon: forward_return rows are 5-bar returns (config.cpcv_horizon).
HORIZON_DAYS = 5

#: run label → (checkpoint dir, registered model_version prefix)
RUNS: dict[str, tuple[str, str]] = {
    "1.2.0":  ("models/production/checkpoints_archive_1.2.0_run", "1.2.0"),
    "0.49.0": ("models/production/checkpoints",                   "0.49.0"),
}


def _load_cpcv_paths(run_dir: Path) -> list[dict[str, Any]]:
    """Load CPCV OOF paths (same layout as CheckpointManager.load_cpcv_oof —
    read directly so archived dirs need no CheckpointManager config match)."""
    d = run_dir / "step_5_xgboost" / "cpcv_oof"
    meta = json.loads((d / "meta.json").read_text())
    paths = []
    for p in range(int(meta["n_paths"])):
        tag = f"path_{p:03d}"
        paths.append({
            "proba":          np.load(d / f"{tag}_proba.npy"),
            "forward_return": np.load(d / f"{tag}_fwd_ret.npy"),
            "timestamp":      np.load(d / f"{tag}_ts.npy").view("datetime64[ns]"),
        })
    return paths


def _load_gru_eval(run_dir: Path) -> dict[str, np.ndarray]:
    d = run_dir / "step_6_gru"
    return {
        "X":   np.load(d / "eval_X.npy"),
        "r":   np.load(d / "eval_r.npy"),
        "sym": np.load(d / "eval_sym.npy"),
        "ts":  np.load(d / "eval_ts.npy").view("datetime64[ns]"),
    }


def _gru_proba(onnx_path: str, X: np.ndarray) -> np.ndarray:
    import onnxruntime as ort
    sess = ort.InferenceSession(onnx_path)
    name = sess.get_inputs()[0].name
    outs = []
    for start in range(0, len(X), 4096):
        out = sess.run(None, {name: X[start:start + 4096].astype(np.float32)})[0]
        outs.append(out)
    proba = np.concatenate(outs)
    return proba[:, 1] if proba.ndim == 2 and proba.shape[1] >= 2 else proba.ravel()


def _xgb_proba(model_json_path: str, X_tab: np.ndarray) -> np.ndarray:
    import xgboost as xgb
    booster = xgb.Booster()
    booster.load_model(model_json_path)
    return booster.predict(xgb.DMatrix(X_tab))


def _corrected_financials(pred: np.ndarray, rets: np.ndarray, ts: np.ndarray) -> dict:
    from app.ml.training.evaluator import calculate_financial_metrics
    keys = ("sharpe_ratio", "sortino_ratio", "max_drawdown", "total_return")
    m = calculate_financial_metrics(pred, rets, timestamps=ts, horizon_days=HORIZON_DAYS)
    return {k: float(m[k]) for k in keys}


def _per_symbol_paths(proba, rets, sym, ts) -> list[dict[str, Any]]:
    return [
        {
            "proba":          proba[sym == s],
            "forward_return": rets[sym == s],
            "timestamp":      ts[sym == s],
        }
        for s in np.unique(sym)
        if int((sym == s).sum()) >= 2
    ]


async def run(*, execute: bool) -> int:
    from sqlalchemy import text
    from app.core.database import AsyncSessionLocal
    from app.ml.evaluation.deflated_sharpe import compute_dsr_and_pbo

    updates: list[tuple[str, dict[str, Any]]] = []  # (model_id, corrected)

    async with AsyncSessionLocal() as db:
        for label, (run_dir_s, prefix) in RUNS.items():
            run_dir = Path(run_dir_s)
            rows = (await db.execute(text(
                "SELECT model_id, model_path, onnx_path FROM ml_model_metadata "
                "WHERE model_version LIKE :p ORDER BY model_id"
            ), {"p": f"{prefix}%"})).all()
            models = {mid.split("_")[0]: (mp, op) for mid, mp, op in rows}

            gru_eval = _load_gru_eval(run_dir)
            X_tab = gru_eval["X"][:, -1, :]

            # ── XGBoost: DSR from CPCV OOF (A3 authority) ────────────────────
            xgb_dsr = compute_dsr_and_pbo(_load_cpcv_paths(run_dir), horizon_days=HORIZON_DAYS)
            xgb_p = _xgb_proba(models["xgboost"][0], X_tab)
            xgb_fin = _corrected_financials(
                (xgb_p >= 0.5).astype(np.int8), gru_eval["r"], gru_eval["ts"]
            )
            updates.append((f"xgboost_{prefix}_xgboost", {
                "deflated_sharpe": float(xgb_dsr["deflated_sharpe"]),
                "pbo":             float(xgb_dsr["oos_loss_rate"]),
                "dsr_basis":       xgb_dsr["dsr_basis"],
                "dsr_n_days":      int(xgb_dsr["n_obs_pooled"]),
                "dsr_source":      "cpcv_oof_daily_portfolio",
                **xgb_fin,
            }))

            # ── GRU: standalone DSR from per-symbol purged eval windows ──────
            gru_p = _gru_proba(models["gru"][1], gru_eval["X"])
            gru_dsr = compute_dsr_and_pbo(
                _per_symbol_paths(gru_p, gru_eval["r"], gru_eval["sym"], gru_eval["ts"]),
                horizon_days=HORIZON_DAYS,
            )
            gru_fin = _corrected_financials(
                (gru_p >= 0.5).astype(np.int8), gru_eval["r"], gru_eval["ts"]
            )
            updates.append((f"gru_{prefix}_gru", {
                "deflated_sharpe": float(gru_dsr["deflated_sharpe"]),
                "pbo":             float(gru_dsr["oos_loss_rate"]),
                "dsr_basis":       gru_dsr["dsr_basis"],
                "dsr_n_days":      int(gru_dsr["n_obs_pooled"]),
                "dsr_source":      "per_symbol_eval_daily_portfolio",
                **gru_fin,
            }))

        # ── Report ───────────────────────────────────────────────────────────
        logger.info("%-24s %8s %8s %8s %8s %8s", "model", "DSR", "sharpe",
                    "max_dd", "tot_ret", "days")
        for mid, c in updates:
            logger.info(
                "%-24s %8.4f %8.3f %8.3f %8.3f %8d",
                mid, c["deflated_sharpe"], c["sharpe_ratio"],
                c["max_drawdown"], c["total_return"], c["dsr_n_days"],
            )

        if not execute:
            logger.info("DRY RUN — registry not touched. Re-run with --execute.")
            return _EXIT_OK

        # ── Write back: corrected values + legacy audit + lineage note ───────
        now = datetime.now(timezone.utc).isoformat()
        for mid, corrected in updates:
            row = (await db.execute(text(
                "SELECT training_metrics FROM ml_model_metadata WHERE model_id=:m"
            ), {"m": mid})).scalar_one()
            metrics = dict(row) if isinstance(row, dict) else json.loads(row)

            legacy = {k: metrics.get(k) for k in
                      ("deflated_sharpe", "pbo", "sharpe_ratio", "sortino_ratio",
                       "max_drawdown", "total_return")}
            metrics.update(corrected)
            metrics["legacy_pooled_basis"] = legacy

            await db.execute(text(
                "UPDATE ml_model_metadata SET "
                "training_metrics = :tm, "
                "lineage = COALESCE(lineage, '{}'::jsonb) || :ln "
                "WHERE model_id = :m"
            ), {
                "tm": json.dumps(metrics),
                "ln": json.dumps({"rescore": {
                    "at": now,
                    "reason": "daily-portfolio basis fix — pooled-panel DSR "
                              "saturation + total_return overflow (2026-07-18)",
                }}),
                "m": mid,
            })
        await db.commit()
        logger.info("✓ %d registry rows updated (legacy values preserved in "
                    "training_metrics.legacy_pooled_basis).", len(updates))
    return _EXIT_OK


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--execute", action="store_true",
                        help="Write corrected metrics (default: dry run).")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(run(execute=args.execute)))
    except Exception:
        logger.error("Rescore failed", exc_info=True)
        sys.exit(_EXIT_ERROR)


if __name__ == "__main__":
    main()
