"""Intelligence Layer — ai_instrument_context table.

Revision ID: 0044
Revises: 0043
Create Date: 2026-06-06

Background
----------
The Cortex Intelligence Layer can generate plain-English market context
summaries for any instrument regardless of whether an active trade suggestion
exists.  This enables the AI Explanation Panel to populate for Watchlist items
that have no current signal — the user sees recent news analysis and ML signal
context rather than a blank panel.

This is distinct from the suggestion-scoped explanation stored in
``trade_suggestions.llm_explanation``.  That column explains a specific ML
signal; this table explains the instrument's current market state.

Design decisions
----------------
  One row per instrument (UNIQUE on instrument_key)
      Context is regenerated in-place via INSERT ... ON CONFLICT DO UPDATE.
      Historical versions are NOT retained in this table — the full audit trail
      lives in ai_llm_audit_log (reference_table = 'ai_instrument_context').

  expires_at column
      Context becomes stale after 2 hours.  The SSE stream checks this field
      before serving cached context; expired rows trigger regeneration.

  source_refs JSONB
      The structured source list from the RAG retriever, persisted so the SSE
      push path can serve source citations without an additional DB query.

  Audit trail
      Every generation (success and failure) is logged in ai_llm_audit_log
      with invocation_type = 'instrument_context' and
      reference_table = 'ai_instrument_context'.

Indexes
-------
  * Primary key on id (bigserial).
  * Unique index on instrument_key — enforces one active context per instrument.
  * (instrument_key, expires_at) — fast staleness check in the SSE poll path.

Downgrade
---------
Drops the table and all indexes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_instrument_context",
        # ── Identity ──────────────────────────────────────────────────────────
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "instrument_key",
            sa.String(100),
            nullable=False,
            comment="NSE instrument key (e.g. NSE_EQ|INE002A01018)",
        ),
        sa.Column(
            "symbol",
            sa.String(20),
            nullable=True,
            comment="NSE trading symbol (e.g. RELIANCE) — used for RAG retrieval",
        ),
        # ── LLM output ────────────────────────────────────────────────────────
        sa.Column(
            "context_summary",
            sa.Text(),
            nullable=True,
            comment="2–3 sentence market context summary (inline display)",
        ),
        sa.Column(
            "context_full",
            sa.Text(),
            nullable=True,
            comment="Full market context narrative with inline citations",
        ),
        sa.Column(
            "model_used",
            sa.String(100),
            nullable=True,
            comment="Model that generated this context (e.g. nim/qwen/qwen3.5-122b-a10b)",
        ),
        # ── Source provenance ─────────────────────────────────────────────────
        sa.Column(
            "source_refs",
            postgresql.JSONB(),
            nullable=True,
            comment="[{source_name, as_of, source_url}] — for SSE push path citations",
        ),
        # ── Lifecycle ─────────────────────────────────────────────────────────
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="When this context was generated",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Context is stale after this timestamp; triggers regeneration",
        ),
    )

    # ── Constraints ───────────────────────────────────────────────────────────
    op.create_index(
        "uq_ai_instrument_context_key",
        "ai_instrument_context",
        ["instrument_key"],
        unique=True,
    )

    # Staleness lookup: SSE poll path filters WHERE instrument_key = ? AND expires_at > NOW()
    op.create_index(
        "idx_ai_instrument_context_lookup",
        "ai_instrument_context",
        ["instrument_key", sa.text("expires_at")],
    )


def downgrade() -> None:
    op.drop_index("idx_ai_instrument_context_lookup", table_name="ai_instrument_context")
    op.drop_index("uq_ai_instrument_context_key",     table_name="ai_instrument_context")
    op.drop_table("ai_instrument_context")
