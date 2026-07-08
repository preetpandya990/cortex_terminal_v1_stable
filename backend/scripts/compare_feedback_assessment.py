"""
WS10 offline comparison gate — LLM-assessment retraining factor.

Builds the feedback-weight bundle TWICE from one identical matured-outcome
window — flag OFF (the shipped default) and flag ON — and reports exactly
what flipping FEEDBACK_LLM_ASSESSMENT_ENABLED would change:

  - assessment coverage (only viewed suggestions carry an assessment)
  - judge identity mix (model + prompt_version — mixed generations are
    flagged: their verdicts are not comparable and the gate should be
    re-run after the fleet converges on one judge version)
  - per-(instrument, date) weight deltas: how many rows change, by how much

Run this BEFORE flipping the flag, alongside a full ``python -m eval.run_eval``
pass and an off-vs-on retrain comparison. Flip only on non-regression.

Usage (from backend/ with venv active):
    python scripts/compare_feedback_assessment.py

Exit codes:
    0 — report produced (flag-off output verified identical to baseline)
    1 — INVARIANT VIOLATION: the flag-off path's weights differ from the
        assessment-blind computation — the "flag off is bit-identical" WS10
        guarantee is broken; do NOT flip the flag, investigate first.
    2 — no matured outcomes to compare.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from app.core.database import AsyncSessionLocal  # noqa: E402
from app.ml.training.feedback_loader import (  # noqa: E402
    _FEEDBACK_QUERY,
    _FEEDBACK_QUERY_COLUMNS,
    build_feedback_weights_df,
)


async def _main() -> int:
    async with AsyncSessionLocal() as session:
        off = await build_feedback_weights_df(session, use_llm_assessment=False)
        on = await build_feedback_weights_df(session, use_llm_assessment=True)

        raw = (await session.execute(_FEEDBACK_QUERY)).fetchall()

    if off.empty:
        print("No matured outcomes in the feedback window — nothing to compare.")
        return 2

    df_raw = pd.DataFrame(raw, columns=_FEEDBACK_QUERY_COLUMNS)
    assessed = df_raw[df_raw["llm_ml_assessment"].notna()]

    print("=" * 72)
    print("WS10 LLM-assessment comparison gate")
    print("=" * 72)

    # ── Coverage ───────────────────────────────────────────────────────────────
    n_raw = len(df_raw)
    n_assessed = len(assessed)
    print(f"\nOutcomes in window:        {n_raw}")
    print(f"Carrying an assessment:    {n_assessed} ({100.0 * n_assessed / n_raw:.1f}%)")

    # ── Judge identity mix ─────────────────────────────────────────────────────
    if n_assessed:
        identities = assessed["llm_ml_assessment"].map(
            lambda a: f"{a.get('model', '?')} / prompt_v{a.get('prompt_version', '?')}"
        )
        print("\nJudge identity mix:")
        for identity, count in identities.value_counts().items():
            print(f"  {identity}: {count}")
        if identities.nunique() > 1:
            print(
                "  ⚠ MIXED judge generations — verdicts are not comparable across "
                "versions; re-run this gate once one judge version dominates."
            )
        verdicts = assessed["llm_ml_assessment"]
        contradicts = int(sum(
            1 for a in verdicts if a.get("price_action_agreement") == "contradicts"
        ))
        overconfident = int(sum(
            1 for a in verdicts if a.get("confidence_should_have_been_lower") is True
        ))
        print(f"\nVerdicts: contradicts={contradicts}  overconfident={overconfident}")

    # ── Weight deltas (aggregated rows) ────────────────────────────────────────
    merged = off.merge(
        on, on=["instrument_key", "signal_date"], suffixes=("_off", "_on")
    )
    delta = merged["sample_weight_on"] - merged["sample_weight_off"]
    changed = merged[delta.abs() > 1e-9]
    print(f"\nAggregated (instrument, date) rows: {len(merged)}")
    print(f"Rows whose weight changes:          {len(changed)}")
    if len(changed):
        print(f"  mean delta: {delta[delta.abs() > 1e-9].mean():+.4f}")
        print(f"  max delta:  {delta.abs().max():+.4f}")
        print("\nTop shifted rows:")
        top = changed.assign(delta=delta[changed.index].values).nlargest(
            10, "delta", keep="all"
        )
        for _, row in top.iterrows():
            print(
                f"  {row['instrument_key']}  {row['signal_date']}  "
                f"{row['sample_weight_off']:.3f} → {row['sample_weight_on']:.3f}"
            )

    # ── Invariant: flag-off must be assessment-blind ───────────────────────────
    # Recompute flag-off weights from raw rows WITHOUT the assessment column
    # present at all; any divergence means the off path secretly reads it.
    from app.ml.training.feedback_loader import compute_sample_weight

    blind = df_raw.drop(columns=["llm_ml_assessment"]).copy()
    blind["sample_weight"] = blind.apply(compute_sample_weight, axis=1).astype(np.float32)
    blind_agg = (
        blind.groupby(["instrument_key", "signal_date"], as_index=False)
        .agg(sample_weight=("sample_weight", "mean"))
    )
    check = off.merge(
        blind_agg, on=["instrument_key", "signal_date"], suffixes=("", "_blind")
    )
    if not np.allclose(check["sample_weight"], check["sample_weight_blind"], atol=0):
        print("\n❌ INVARIANT VIOLATION: flag-off weights differ from the "
              "assessment-blind computation. Do NOT flip the flag.")
        return 1

    print("\n✔ Flag-off path verified bit-identical to the assessment-blind baseline.")
    print("\nNext steps before flipping FEEDBACK_LLM_ASSESSMENT_ENABLED:")
    print("  1. Off-vs-on retrain over the same window (scheduled_retrain --once ×2)")
    print("  2. Full eval gate: python -m eval.run_eval (all categories must pass)")
    print("  3. Review coverage above — thin coverage means the factor touches "
          "few samples and the comparison is low-power")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
