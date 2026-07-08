"""Add trade_suggestions.llm_ml_assessment (WS10/C.B2 structured feedback).

Revision ID: 0053
Revises: 0052
Create Date: 2026-07-06

Context
-------
Retraining feedback was purely quantitative: realized outcomes reweight
training samples, but no structured "here's WHY the model was likely wrong"
signal existed anywhere. Demand-triggered explanations (WS7) already reason
over the actual OHLCV price action, so the same structured Gemini call now
returns an enumerated assessment of the ML call:

  {likely_missed_pattern, confidence_should_have_been_lower,
   price_action_agreement, model, prompt_version, assessed_at}

The judge identity (model + prompt_version) is stored with every verdict:
judge-model changes shift verdict distributions, so weights derived from
different judge generations are not comparable and the offline retrain gate
must be re-run on version bumps (LLM-as-judge practice).

Consumed by ml/training/feedback_loader.compute_sample_weight as an extra
multiplicative factor — gated behind FEEDBACK_LLM_ASSESSMENT_ENABLED
(default False) and an offline non-regression comparison. Only suggestions
whose explanation was actually viewed carry an assessment (coverage is
reported by scripts/compare_feedback_assessment.py).

Downgrade
---------
DROP the column.
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0053"
down_revision: Union[str, None] = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_suggestions",
        sa.Column("llm_ml_assessment", JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trade_suggestions", "llm_ml_assessment")
