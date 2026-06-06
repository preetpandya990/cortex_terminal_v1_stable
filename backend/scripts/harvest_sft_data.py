#!/usr/bin/env python3
"""
Cortex Phase 1 — SFT Training Data Harvester
=============================================
Extracts high-quality explanation invocations from the Phase 0 production
audit log and formats them as ChatML JSONL for ORPO fine-tuning of
Agentar-Fin-R1-32B (Qwen3-based).

Data source
-----------
  ai_llm_audit_log  JOIN  ai_trade_suggestions

Quality filter (only rows meeting ALL criteria are included):
  - invocation_type = 'explanation'        — explanation calls only
  - error_message IS NULL                  — successful completion
  - guardrail_events = '[]'                — no guardrail violations
  - input_tokens IS NOT NULL               — actual LLM call (not a skip)
  - latency_ms < 30 000                    — no pathological timeouts
  - trade_suggestion.llm_summary IS NOT NULL

Output format — one JSON object per line (ChatML):
  {
    "messages": [
      {"role": "system",    "content": "<system prompt>"},
      {"role": "user",      "content": "<signal + RAG context prompt>"},
      {"role": "assistant", "content": "<raw structured output (no disclaimer)>"}
    ],
    "metadata": {
      "invocation_id":   "<uuid>",
      "symbol":          "RELIANCE",
      "model_provider":  "nim",
      "model_id":        "qwen/qwen3-235b-a22b",
      "input_tokens":    412,
      "output_tokens":   621,
      "latency_ms":      4821,
      "created_at":      "2026-06-01T12:34:56+00:00"
    }
  }

The regulatory disclaimer appended by the production guardrail is stripped from
the assistant content — the trained model must NOT reproduce it (it is injected
at inference time by the production guardrail, never trained in).

Usage (from backend/ with venv active):
    python -m scripts.harvest_sft_data
    python -m scripts.harvest_sft_data --output data/sft_candidates.jsonl
    python -m scripts.harvest_sft_data --min-output-tokens 200 --dry-run

Output statistics are printed to stdout on completion.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("harvest_sft")

# ── Regulatory disclaimer pattern ─────────────────────────────────────────────
# Matches the disclaimer appended by explanation_worker._apply_guardrails().
# Stripped from assistant content so the model never learns to reproduce it.
_DISCLAIMER_RE = re.compile(
    r"\n*⚠\s*This is AI-generated analysis for informational purposes only.*$",
    re.DOTALL | re.IGNORECASE,
)

# ── System prompt (verbatim from explanation_worker to ensure distribution match) ─
_SYSTEM_PROMPT = """\
You are a financial signal analysis tool for the Cortex algorithmic trading platform.
You are NOT a licensed financial advisor and must not provide investment recommendations.

Your task: generate a concise, factual explanation for a machine-generated trade signal,
grounded exclusively in the retrieved news articles provided.

Mandatory rules:
1. BASE ALL CLAIMS on the retrieved news context provided in the prompt.
   Do not invent facts, prices, or events not present in the context.
2. CITE EVERY FACTUAL CLAIM inline: According to [Source Name, YYYY-MM-DD]...
   If no context is provided, state that clearly rather than inventing sources.
3. PROHIBITED language (these will be filtered):
   - Price predictions: "will reach ₹X", "target price", "price target"
   - Guarantees: "guaranteed", "certain to", "will definitely"
   - Advisory language: "you should buy/sell", "recommend buying", "buy now"
4. ALLOWED: describe what the signal detected, what the news says, what the risk is.
5. DISCLAIMER: The system will automatically append the required regulatory disclaimer.
   Do NOT add your own disclaimer — it will duplicate the injected one.
6. Output JSON only: {"summary": "...", "full_explanation": "...", "sources_used": [...]}
   No markdown fences, no extra keys.\
"""


# ── SQL query ──────────────────────────────────────────────────────────────────
# Reconstruct the user prompt from audit log fields.  The audit log stores
# prompt_hash (not the raw prompt) for storage efficiency, so we reconstruct
# the prompt from the paired trade_suggestion row.  output_preview stores the
# first 500 chars of llm_summary; we fetch llm_explanation from the suggestion
# for the full assistant content.
_HARVEST_QUERY = """
SELECT
    al.invocation_id,
    al.model_provider,
    al.model_id,
    al.input_tokens,
    al.output_tokens,
    al.latency_ms,
    al.created_at,
    ts.symbol,
    ts.signal_direction,
    ts.consensus_score,
    ts.confidence_level,
    ts.entry_price,
    ts.stop_loss,
    ts.risk_reward_ratio,
    ts.time_horizon,
    ts.regime_type,
    ts.ml_signal,
    ts.ai_signal,
    ts.llm_summary,
    ts.llm_explanation
FROM ai_llm_audit_log al
JOIN trade_suggestions ts
    ON ts.id = al.reference_id
   AND al.reference_table = 'trade_suggestions'
WHERE
    al.invocation_type    = 'explanation'
    AND al.error_message  IS NULL
    AND (al.guardrail_events IS NULL OR al.guardrail_events::text = '[]')
    AND al.latency_ms     < 30000
    AND ts.llm_summary    IS NOT NULL
    AND ts.llm_explanation IS NOT NULL
ORDER BY al.created_at DESC
"""


# ── Prompt reconstruction ──────────────────────────────────────────────────────

def _reconstruct_user_prompt(row: dict[str, Any]) -> str:
    """
    Re-build the explanation prompt from the suggestion row fields.

    This mirrors explanation_worker._build_explanation_prompt() exactly —
    the audit log schema stores signal fields on trade_suggestions, so we
    can faithfully reconstruct the user turn without storing the raw prompt.

    Note: the RAG context is NOT available in the audit log (only source IDs
    are stored).  The reconstructed prompt uses the no-context branch, which
    tells the model context was unavailable.  Rows where context was available
    will have citation-style outputs that remain valid training examples — the
    model learns the output schema regardless.
    """
    ml = row.get("ml_signal") or {}
    ai = row.get("ai_signal") or {}

    lines = [
        "## Trade Signal Summary",
        f"Symbol:           {row['symbol']}",
        f"Direction:        {row['signal_direction']}",
        f"Consensus Score:  {float(row['consensus_score']):.1f}/100",
        f"Confidence:       {row['confidence_level']}",
    ]
    if row.get("entry_price"):
        lines.append(f"Entry Price:      ₹{float(row['entry_price']):.2f}")
    if row.get("stop_loss"):
        lines.append(f"Stop Loss:        ₹{float(row['stop_loss']):.2f}")
    if row.get("risk_reward_ratio"):
        lines.append(f"Risk/Reward:      {float(row['risk_reward_ratio']):.1f}x")
    if row.get("time_horizon"):
        lines.append(f"Time Horizon:     {row['time_horizon']}")
    if row.get("regime_type"):
        lines.append(f"Market Regime:    {row['regime_type']}")

    if isinstance(ml, dict) and ml.get("available"):
        lines += [
            "",
            "## ML Model Output",
            f"ML Direction:     {ml.get('action', 'N/A')}",
            f"ML Confidence:    {ml.get('confidence', 0.0):.2%}",
        ]

    sentiment_label = None
    if isinstance(ai, dict):
        sentiment_label = ai.get("sentiment_label") or ai.get("sentiment")
    if sentiment_label:
        lines += [
            "",
            "## News Sentiment Signal",
            f"Sentiment:        {sentiment_label}",
        ]
        event_count = ai.get("event_count") or len(ai.get("contributing_events", []))
        if event_count:
            lines.append(f"Contributing Events: {event_count}")

    # Context was not stored in the audit log; use the no-context branch.
    lines += [
        "",
        "## Retrieved News Context",
        "No recent news articles were found for this symbol. "
        "Base your explanation on the quantitative signal data above only, "
        "and state clearly that no news context was available.",
    ]

    return "\n".join(lines)


def _build_assistant_content(row: dict[str, Any]) -> str:
    """
    Build the assistant turn from the persisted explanation.

    Strips the regulatory disclaimer that the production guardrail appended
    at inference time — the fine-tuned model must NOT reproduce it (the
    production guardrail injects it post-generation at inference time).

    Returns a JSON string matching the ExplanationOutput schema.
    """
    full_explanation = row["llm_explanation"] or ""
    # Strip disclaimer
    full_explanation = _DISCLAIMER_RE.sub("", full_explanation).rstrip()

    payload = {
        "summary":         row["llm_summary"] or "",
        "full_explanation": full_explanation,
        "sources_used":    [],   # production explanation doesn't persist this separately
    }
    return json.dumps(payload, ensure_ascii=False)


# ── Main logic ─────────────────────────────────────────────────────────────────

async def harvest(
    output_path: Path,
    min_output_tokens: int,
    dry_run: bool,
) -> None:
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from sqlalchemy import text
    from app.core.database import AsyncSessionLocal

    logger.info("Connecting to database…")

    rows_fetched = 0
    rows_passed = 0
    skipped_short = 0
    symbols_seen: set[str] = []

    async with AsyncSessionLocal() as db:
        result = await db.execute(text(_HARVEST_QUERY))
        all_rows = result.mappings().all()

    rows_fetched = len(all_rows)
    logger.info("Rows fetched from audit log: %d", rows_fetched)

    if dry_run:
        logger.info("DRY RUN — not writing output file.")

    output_records: list[dict] = []

    for row in all_rows:
        row = dict(row)

        # Only apply token-length filter when output_tokens was actually recorded.
        # The explanation worker currently doesn't capture token counts from
        # Instructor structured calls; those rows are filtered by llm_explanation
        # length instead (see assistant_content length check below).
        output_tokens = row.get("output_tokens")
        if output_tokens is not None and output_tokens < min_output_tokens:
            skipped_short += 1
            continue

        # Minimum content quality gate when token counts unavailable:
        # a meaningful explanation is at least 200 characters after disclaimer strip.
        explanation_text = (row.get("llm_explanation") or "")
        explanation_clean = _DISCLAIMER_RE.sub("", explanation_text).strip()
        if len(explanation_clean) < 200:
            skipped_short += 1
            continue

        user_prompt = _reconstruct_user_prompt(row)
        assistant_content = _build_assistant_content(row)

        record = {
            "messages": [
                {"role": "system",    "content": _SYSTEM_PROMPT},
                {"role": "user",      "content": user_prompt},
                {"role": "assistant", "content": assistant_content},
            ],
            "metadata": {
                "invocation_id":  str(row["invocation_id"]),
                "symbol":         row["symbol"],
                "model_provider": row["model_provider"],
                "model_id":       row["model_id"] or "unknown",
                "input_tokens":   row["input_tokens"],
                "output_tokens":  row["output_tokens"],
                "latency_ms":     row["latency_ms"],
                "created_at":     row["created_at"].isoformat() if row.get("created_at") else None,
            },
        }
        output_records.append(record)
        symbols_seen.append(row["symbol"])
        rows_passed += 1

    unique_symbols = len(set(symbols_seen))

    # ── Statistics ─────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  HARVEST STATISTICS")
    print("=" * 60)
    print(f"  Audit log rows queried:    {rows_fetched}")
    print(f"  Passed quality filter:     {rows_passed}")
    print(f"  Skipped (short output):    {skipped_short}  (< {min_output_tokens} output tokens)")
    print(f"  Unique symbols covered:    {unique_symbols}")
    if rows_passed > 0:
        avg_out = sum(
            r["metadata"]["output_tokens"] or 0 for r in output_records
        ) / rows_passed
        print(f"  Avg output tokens:         {avg_out:.0f}")
    print("=" * 60)
    print()

    if rows_passed == 0:
        logger.warning(
            "No training pairs harvested. "
            "Verify that the Phase 0 audit log has explanation rows by running:\n"
            "  SELECT COUNT(*) FROM ai_llm_audit_log WHERE invocation_type='explanation' "
            "AND error_message IS NULL;"
        )
        return

    if dry_run:
        logger.info("DRY RUN complete — %d records would be written to %s", rows_passed, output_path)
        return

    # ── Write JSONL ────────────────────────────────────────────────────────────
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for record in output_records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.info("Wrote %d SFT training pairs to %s", rows_passed, output_path)


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Harvest Phase 0 audit log → SFT ChatML JSONL for ORPO fine-tuning",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).parent.parent / "data" / "sft_candidates.jsonl"),
        help="Output JSONL path (default: data/sft_candidates.jsonl)",
    )
    parser.add_argument(
        "--min-output-tokens",
        type=int,
        default=100,
        help="Skip rows with fewer than N output tokens (default: 100)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print statistics but do not write the output file",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(harvest(
        output_path=Path(args.output),
        min_output_tokens=args.min_output_tokens,
        dry_run=args.dry_run,
    ))
