"""
INVESTIGATION ARTIFACT — Workstream B CPU feasibility benchmark.

Measures actual per-batch / per-epoch wall-clock for a representative TFT
on this dev box, then extrapolates to:
  - GRU current scale     (200 symbols, ~263K train rows — real baseline)
  - Plan's full universe  (2551 symbols — implementation-plan target)

Not production. Not a unit test. Delete after the go/no-go is recorded.

Usage:
    python -u scripts/bench_tft_cpu.py
"""
from __future__ import annotations

import json
import resource
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import lightning.pytorch as pl
from pytorch_forecasting import TemporalFusionTransformer, TimeSeriesDataSet

SEED              = 42
N_SYMBOLS_BENCH   = 200       # match current GRU scale to keep panel-build cheap
N_DAYS            = 1500      # ~6 years × 250 trading days
N_FEATURES        = 69        # = TrainingConfig.n_features
SEQ_LEN           = 60        # = TrainingConfig.sequence_length
HIDDEN_SIZE       = 32
ATTENTION_HEADS   = 4
BATCH_SIZE        = 256
N_EPOCHS          = 2

# Ground-truth panel sizes for extrapolation (from step_6_gru/eval_meta.json
# and TrainingConfig.n_symbols, respectively).
GRU_SCALE_TRAIN_ROWS = 263_083
FULL_UNIVERSE_SYMBOLS = 2551


def _print(msg: str) -> None:
    """Force-flush every status line so pytest/CI/tee/tail all show live progress."""
    print(msg, flush=True)


def main() -> None:
    torch.manual_seed(SEED)
    np.random.seed(SEED)
    torch.set_num_threads(8)
    _print(
        f"=== TFT-CPU bench  threads={torch.get_num_threads()} "
        f" torch={torch.__version__}  cuda={torch.cuda.is_available()} ==="
    )

    # ── 1. Synthetic panel (vectorised — random data is fine; this is a compute bench) ──
    t0 = time.time()
    n_rows = N_SYMBOLS_BENCH * N_DAYS
    feature_data = np.random.randn(n_rows, N_FEATURES).astype(np.float32)
    df = pd.DataFrame(feature_data, columns=[f"f{i}" for i in range(N_FEATURES)])
    df["symbol"]   = np.repeat([f"S{i}" for i in range(N_SYMBOLS_BENCH)], N_DAYS)
    df["time_idx"] = np.tile(np.arange(N_DAYS), N_SYMBOLS_BENCH)
    df["target"]   = np.random.randn(n_rows).astype(np.float32)  # regression target — compute is identical
    _print(f"  panel rows={len(df):,}  ({time.time()-t0:.1f}s)")

    # ── 2. TimeSeriesDataSet ──────────────────────────────────────────────────
    t0 = time.time()
    feature_cols = [f"f{i}" for i in range(N_FEATURES)]
    train_ds = TimeSeriesDataSet(
        df,
        time_idx="time_idx",
        target="target",
        group_ids=["symbol"],
        max_encoder_length=SEQ_LEN,
        max_prediction_length=1,
        time_varying_unknown_reals=feature_cols,
    )
    _print(f"  TSD train_len={len(train_ds):,}  ({time.time()-t0:.1f}s)")

    # ── 3. TFT model ──────────────────────────────────────────────────────────
    t0 = time.time()
    tft = TemporalFusionTransformer.from_dataset(
        train_ds,
        learning_rate=1e-3,
        hidden_size=HIDDEN_SIZE,
        attention_head_size=ATTENTION_HEADS,
        dropout=0.1,
        log_interval=0,
    )
    n_params = sum(p.numel() for p in tft.parameters())
    _print(f"  TFT built  params={n_params:,}  ({time.time()-t0:.1f}s)")

    # ── 4. Dataloader + Trainer ───────────────────────────────────────────────
    train_dl = train_ds.to_dataloader(train=True, batch_size=BATCH_SIZE, num_workers=0)
    n_batches = len(train_dl)
    _print(f"  train_batches={n_batches}  batch_size={BATCH_SIZE}")

    trainer = pl.Trainer(
        max_epochs=N_EPOCHS,
        accelerator="cpu",
        devices=1,
        enable_checkpointing=False,
        enable_progress_bar=True,    # live per-batch visibility
        logger=False,
    )

    # ── 5. Train + time ───────────────────────────────────────────────────────
    _print(f"  -> training {N_EPOCHS} epoch(s)...")
    t_start = time.time()
    trainer.fit(tft, train_dataloaders=train_dl)
    train_time   = time.time() - t_start
    epoch_time   = train_time / N_EPOCHS
    per_batch_ms = (epoch_time / n_batches) * 1000
    rss_mb       = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    # ── 6. Extrapolation ──────────────────────────────────────────────────────
    bench_rows = len(train_ds)
    gru_scale  = GRU_SCALE_TRAIN_ROWS / bench_rows
    full_scale = gru_scale * (FULL_UNIVERSE_SYMBOLS / N_SYMBOLS_BENCH)

    report = {
        "hardware": {
            "torch":         torch.__version__,
            "threads":       torch.get_num_threads(),
            "cuda":          torch.cuda.is_available(),
        },
        "spec": {
            "panel": {
                "n_symbols":  N_SYMBOLS_BENCH,
                "n_days":     N_DAYS,
                "n_features": N_FEATURES,
                "seq_len":    SEQ_LEN,
            },
            "tft": {
                "hidden_size":     HIDDEN_SIZE,
                "attention_heads": ATTENTION_HEADS,
                "params":          n_params,
            },
            "batch_size": BATCH_SIZE,
        },
        "measured": {
            "train_rows":   bench_rows,
            "n_batches":    n_batches,
            "epoch_time_s": round(epoch_time, 2),
            "per_batch_ms": round(per_batch_ms, 2),
            "peak_rss_mb":  round(rss_mb, 1),
        },
        "extrap_200sym_gru_scale": {
            "row_scale":               round(gru_scale, 2),
            "epoch_min":               round(epoch_time * gru_scale / 60, 1),
            "50_epochs_h":             round(epoch_time * gru_scale * 50 / 3600, 1),
            "15_trials_x_30_epochs_h": round(epoch_time * gru_scale * 15 * 30 / 3600, 1),
        },
        "extrap_2551sym_full_universe": {
            "row_scale":   round(full_scale, 2),
            "epoch_min":   round(epoch_time * full_scale / 60, 1),
            "50_epochs_h": round(epoch_time * full_scale * 50 / 3600, 1),
        },
        "decision_criteria": {
            "weekly_budget_h":           24,
            "target_200sym_50ep_h":      "<= 12  (leaves HPO headroom)",
            "target_2551sym_50ep_h":     "<= 24  (single retrain only)",
        },
    }

    out = Path(__file__).parent.parent / "bench_tft_cpu_report.json"
    out.write_text(json.dumps(report, indent=2))
    _print(f"\n=== REPORT  (saved to {out}) ===")
    _print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
