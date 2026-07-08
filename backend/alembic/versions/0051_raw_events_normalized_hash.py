"""Add ai_raw_events.normalized_hash for near-duplicate detection + backfill.

Revision ID: 0051
Revises: 0050
Create Date: 2026-07-06

Context
-------
Ingest dedup was exact-SHA-256 only (content_hash over title+summary), so
syndicated wire copy republished across the 7 configured RSS feeds with even
a one-character difference (byline prefix, whitespace reflow, quote style)
was stored — and embedded — twice. normalized_hash is SHA-256 over a
canonicalized form (casefold, Unicode NFC, leading byline stripped,
punctuation removed, whitespace collapsed); the ingest path checks it
alongside content_hash.

Deliberately NON-unique: near-dup detection is best-effort (a same-cycle
cross-feed race may slip one through; the next cycle's SELECT catches it).
A unique constraint would turn that benign race back into IntegrityError
batch aborts — exactly the failure mode the ingest rework removed.

Steps (upgrade)
---------------
1. ADD COLUMN normalized_hash VARCHAR(64) NULL.
2. Backfill all existing rows in batches (Python-side normalization,
   mirroring app.ai.ingestion.rss_fetcher.normalize_for_hash at the time of
   writing — inlined here so the migration never imports app code).
3. CREATE INDEX ix_ai_raw_events_normalized_hash.

Downgrade
---------
DROP the index and column. Backfill is inherently lost (recomputable).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels = None
depends_on = None

_BATCH_SIZE = 1000

# ── Inlined copy of rss_fetcher.normalize_for_hash (see module docstring) ──────
_BYLINE_PREFIX_RE = re.compile(r"^by\s+[\w .,'&-]{2,80}?[:\-–—|]\s*", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize_for_hash(text: str) -> str:
    t = unicodedata.normalize("NFC", text).casefold()
    t = _BYLINE_PREFIX_RE.sub("", t)
    t = _NON_WORD_RE.sub(" ", t)
    return " ".join(t.split())


def _normalized_content_hash(text: str) -> str:
    return hashlib.sha256(_normalize_for_hash(text).encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.add_column(
        "ai_raw_events",
        sa.Column("normalized_hash", sa.String(64), nullable=True),
    )

    # Backfill in id-ordered batches: bounded memory, resumable on rerun
    # (rows already backfilled are excluded by the IS NULL filter).
    bind = op.get_bind()
    total = 0
    while True:
        rows = bind.execute(
            sa.text(
                "SELECT id, raw_content FROM ai_raw_events "
                "WHERE normalized_hash IS NULL ORDER BY id LIMIT :n"
            ),
            {"n": _BATCH_SIZE},
        ).all()
        if not rows:
            break
        bind.execute(
            sa.text(
                "UPDATE ai_raw_events SET normalized_hash = :h WHERE id = :id"
            ),
            [
                {"id": row.id, "h": _normalized_content_hash(row.raw_content)}
                for row in rows
            ],
        )
        total += len(rows)

    print(f"0051: backfilled normalized_hash for {total} ai_raw_events rows")

    # Index AFTER backfill — one build over final data beats incremental
    # maintenance during the batched UPDATEs.
    op.create_index(
        "ix_ai_raw_events_normalized_hash",
        "ai_raw_events",
        ["normalized_hash"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_raw_events_normalized_hash", table_name="ai_raw_events")
    op.drop_column("ai_raw_events", "normalized_hash")
