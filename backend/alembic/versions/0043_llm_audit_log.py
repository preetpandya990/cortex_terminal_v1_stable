"""Intelligence Layer — ai_llm_audit_log table.

Revision ID: 0043
Revises: 0042
Create Date: 2026-06-03

Background
----------
Every LLM inference made by the Cortex Intelligence Layer must be fully
traceable (CORTEX_LLM_UPGRADE_PLAN.md §8.3).  In financial services, model
risk management frameworks (SR 11-7, OCC 2024 GenAI update, EU AI Act) treat
an LLM-based analysis tool as a governed model: given any user complaint or
regulatory enquiry, the operator must be able to reproduce the exact prompt,
model version, retrieved sources, guardrail events, and output for any
historical inference.

This table is the audit trail that makes that reproduction possible.  It is
append-only at the application layer — no UPDATE or DELETE paths exist in
the codebase.  Inserts happen inside every LLM call path via
``CortexIntelligenceClient``, including failures.

Schema notes
------------
  invocation_id
      A UUID generated per inference call, independent of the database row id.
      Stable across retries — if the same logical call is retried, retries share
      the same invocation_id so the full attempt history is visible.

  invocation_type
      Categorical label: ``sentiment``, ``explanation``, ``classification``,
      ``embedding``.  Used for cost breakdowns and per-type monitoring.

  reference_id
      The primary key of the domain object this inference was about
      (suggestion_id, nlp_result_id, etc.).  Typed as BIGINT to match the
      BigInteger PKs used throughout the schema.  The reference_table column
      indicates which table reference_id belongs to.

  prompt_hash
      SHA-256 of the fully-rendered prompt string.  Allows exact prompt
      reproduction without storing the full text (which may be large and contain
      retrieved content).  The full prompt can be reconstructed from the
      retrieved_source_ids + the prompt template version stored in the
      application's template registry.

  retrieved_source_ids
      JSONB array of {table, id, as_of} objects identifying every document
      chunk injected into the prompt context.  This is the provenance record
      required by SR 11-7: "what facts did the model see?"

  guardrail_events
      JSONB array of guardrail actions that fired during this inference
      (input filter, output filter, numeric cross-check, hallucination flag).
      An empty array means all guardrails passed.  NULL means guardrails were
      not evaluated (e.g. the call failed before reaching the guardrail layer).

  output_preview
      First 500 characters of the model output.  Sufficient for spot-checking
      and triage without bloating the audit table with full responses.

  error_message
      NULL on success.  Contains the exception message and type on failure.
      A non-NULL row with NULL output_preview is a failed inference.

Indexes
-------
  * Primary key on id (bigserial) — physical ordering matches insertion order.
  * invocation_id — fast lookup when correlating retries or cross-referencing
    a specific inference from the application logs.
  * (invocation_type, created_at DESC) — efficient per-type monitoring queries
    and cost rollups.

Partitioning
------------
The table is created as a plain table.  If insert volume exceeds ~10M rows/month
it should be converted to a TimescaleDB hypertable partitioned by ``created_at``
(a future migration; the schema is designed to be compatible with that upgrade).

Downgrade
---------
Drops the table and all indexes.  Audit data is lost on downgrade — this is
acceptable because downgrades of a production audit table should never occur
in practice; the downgrade path exists only for development convenience.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_llm_audit_log",
        # ── Identity ──────────────────────────────────────────────────────────
        sa.Column("id",               sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "invocation_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
            comment="Stable UUID per logical inference call; shared across retries",
        ),
        # ── Classification ────────────────────────────────────────────────────
        sa.Column(
            "invocation_type",
            sa.String(50),
            nullable=False,
            comment="sentiment | explanation | classification | embedding",
        ),
        sa.Column(
            "reference_table",
            sa.String(100),
            nullable=True,
            comment="Table the reference_id belongs to (e.g. trade_suggestions)",
        ),
        sa.Column(
            "reference_id",
            sa.BigInteger(),
            nullable=True,
            comment="PK of the domain object this inference was about",
        ),
        # ── Model ─────────────────────────────────────────────────────────────
        sa.Column(
            "model_provider",
            sa.String(50),
            nullable=False,
            comment="nim | ollama",
        ),
        sa.Column(
            "model_id",
            sa.String(100),
            nullable=False,
            comment="Full model identifier e.g. qwen/qwen3.6-35b-a3b",
        ),
        # ── Prompt & context ──────────────────────────────────────────────────
        sa.Column(
            "prompt_hash",
            sa.String(64),
            nullable=False,
            comment="SHA-256 of the fully-rendered prompt for exact reproducibility",
        ),
        sa.Column(
            "retrieved_source_ids",
            postgresql.JSONB(),
            nullable=True,
            comment="[{table, id, as_of}] — provenance of every retrieved chunk",
        ),
        # ── Token accounting ──────────────────────────────────────────────────
        sa.Column("input_tokens",  sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms",    sa.Integer(), nullable=True),
        # ── Guardrails ────────────────────────────────────────────────────────
        sa.Column(
            "guardrail_events",
            postgresql.JSONB(),
            nullable=True,
            comment="[] = all passed; non-empty = list of triggered guardrail actions",
        ),
        # ── Output ────────────────────────────────────────────────────────────
        sa.Column(
            "output_preview",
            sa.Text(),
            nullable=True,
            comment="First 500 chars of model output; NULL on failure",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="Exception message on failure; NULL on success",
        ),
        # ── Timestamp ─────────────────────────────────────────────────────────
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )

    # ── Indexes ───────────────────────────────────────────────────────────────
    op.create_index(
        "idx_llm_audit_invocation_id",
        "ai_llm_audit_log",
        ["invocation_id"],
    )
    op.create_index(
        "idx_llm_audit_type_time",
        "ai_llm_audit_log",
        ["invocation_type", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("idx_llm_audit_type_time",    table_name="ai_llm_audit_log")
    op.drop_index("idx_llm_audit_invocation_id", table_name="ai_llm_audit_log")
    op.drop_table("ai_llm_audit_log")
