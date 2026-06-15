"""
AI Models - ORM models for AI microservice tables.
Import from migration 0005 table definitions.
"""
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.config import get_settings
from app.core.database import Base


class AIRawEvent(Base):
    __tablename__ = "ai_raw_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_url: Mapped[str] = mapped_column(String(500), nullable=False)
    source_name: Mapped[str] = mapped_column(String(200), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(10))
    extra_data: Mapped[dict | None] = mapped_column("metadata", JSONB)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AIProcessedEvent(Base):
    __tablename__ = "ai_processed_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    raw_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    processed_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    translated_content: Mapped[str | None] = mapped_column(Text)
    detected_language: Mapped[str | None] = mapped_column(String(10))
    translation_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    processing_status: Mapped[str] = mapped_column(String(20), server_default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AINLPResult(Base):
    __tablename__ = "ai_nlp_results"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    processed_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    sentiment_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    sentiment_label: Mapped[str | None] = mapped_column(String(20))
    named_entities: Mapped[dict | None] = mapped_column(JSONB)
    keywords: Mapped[dict | None] = mapped_column(JSONB)
    model_used: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AIEventClassification(Base):
    __tablename__ = "ai_event_classifications"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    nlp_result_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    impact_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    affected_symbols: Mapped[list[str] | None] = mapped_column(ARRAY(String(20)), index=True)
    classification_confidence: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text)
    decay_half_life_hours: Mapped[int] = mapped_column(Integer, server_default="24")
    decay_slow_half_life_hours: Mapped[int] = mapped_column(Integer, server_default="72")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AISourceCredibility(Base):
    __tablename__ = "ai_source_credibility"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    credibility_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), server_default="50.0")
    total_events: Mapped[int] = mapped_column(Integer, server_default="0")
    confirmed_events: Mapped[int] = mapped_column(Integer, server_default="0")
    contradicted_events: Mapped[int] = mapped_column(Integer, server_default="0")
    last_updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AIFakeNewsFlag(Base):
    __tablename__ = "ai_fake_news_flags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_classification_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    flag_status: Mapped[str] = mapped_column(String(20), nullable=False)
    detection_layers: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text)
    flagged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AIMLModel(Base):
    __tablename__ = "ai_ml_models"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    model_type: Mapped[str] = mapped_column(String(50), nullable=False)
    deployment_state: Mapped[str] = mapped_column(String(20), server_default="shadow", index=True)
    model_version: Mapped[str] = mapped_column(String(50), nullable=False)
    model_path: Mapped[str | None] = mapped_column(String(500))
    timeframe: Mapped[str | None] = mapped_column(String(20), index=True)
    artifact_encrypted: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    artifact_sha256: Mapped[str | None] = mapped_column(String(64))
    training_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accuracy: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    precision: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    recall: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    f1_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    governance_metadata: Mapped[dict | None] = mapped_column(JSONB)
    # R6 — explicit FK to the authoritative ml_model_metadata record (migration 0040).
    # SET NULL on delete: a retracted ml_model_metadata row must not cascade-delete
    # the governance row, which may still hold drift history and audit trails.
    ml_model_metadata_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("ml_model_metadata.id", ondelete="SET NULL", name="fk_ai_ml_models_ml_model_metadata"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AIDriftReport(Base):
    __tablename__ = "ai_drift_reports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    model_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    report_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    drift_detected: Mapped[bool] = mapped_column(Boolean, server_default="false")
    drift_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    accuracy_drop: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    distribution_metrics: Mapped[dict | None] = mapped_column(JSONB)
    action_taken: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AIRegimeDetection(Base):
    __tablename__ = "ai_regime_detections"
    __table_args__ = (
        Index("idx_regime_symbol", "symbol"),
        Index("idx_regime_symbol_ts", "symbol", "detection_timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    symbol: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    detection_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    regime_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    contributing_factors: Mapped[dict | None] = mapped_column(JSONB)
    volatility_index: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    volume_index: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    trend_strength: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AIActiveStrategy(Base):
    __tablename__ = "ai_active_strategies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    strategy_name: Mapped[str] = mapped_column(String(200), nullable=False)
    regime_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    priority_level: Mapped[int] = mapped_column(Integer, server_default="5")
    status: Mapped[str] = mapped_column(String(20), server_default="active", index=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    configuration: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AITradingSignal(Base):
    __tablename__ = "ai_trading_signals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    signal_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # Denormalized from instrument_master.name — populated at assembly time to
    # avoid a JOIN on every signal read and every serialization/WebSocket publish.
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # False when the symbol is not present in instrument_master as an NSE EQ equity.
    # Such signals are informational only — no trade execution is possible.
    is_nse_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    action: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    strategy_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    regime_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # Derived from prediction timeframe: intraday / swing / positional
    time_horizon: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    # Computed TTL anchored to NSE market hours
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    # Price levels from ML ensemble (TP1 and ATR-based stop loss)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    contributing_events: Mapped[dict | None] = mapped_column(JSONB)
    ml_predictions: Mapped[dict | None] = mapped_column(JSONB)
    technical_indicators: Mapped[dict | None] = mapped_column(JSONB)
    reasoning: Mapped[str | None] = mapped_column(Text)
    extra_data: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AIKillSwitch(Base):
    __tablename__ = "ai_kill_switches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    switch_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), server_default="inactive", index=True)
    activated_by: Mapped[str | None] = mapped_column(String(100))
    activation_reason: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expiration_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AIDocumentEmbedding(Base):
    """
    Finance-aware RAG vector store.

    One row per source document (ai_raw_events).  The embedding dimension is
    owned by GEMINI_EMBED_DIM (gemini-embedding-001) and kept in lock-step with
    the pgvector column by migration 0046.  Changing the embedding model or
    dimension requires a full re-ingestion and a new migration.

    Symbol assignment:
      - Single-symbol events → symbol = that trading symbol.
      - Multi-symbol or unclassified events → symbol = NULL (general market;
        included in every symbol-scoped retrieval pass).
      - All affected symbols are always stored in metadata['affected_symbols'].

    Indexes (created by migration 0041):
      - HNSW on embedding (cosine, m=16, ef_construction=64) for ANN search.
      - B-tree on (symbol, as_of_timestamp DESC) for time-window filtering.
    """

    __tablename__ = "ai_document_embeddings"
    __table_args__ = (
        UniqueConstraint("source_table", "source_id", name="uq_ai_doc_embeddings_source"),
        Index("idx_ai_doc_embeddings_symbol_time", "symbol", text("as_of_timestamp DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # Provenance: which table and which row this embedding was built from.
    source_table: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # NULL = general market event; non-NULL = single-instrument event.
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # SHA-256 of the chunk text; used by the ingester to skip unchanged content.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # First 200 characters of the chunk for debugging / audit.
    content_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Passage embedding (gemini-embedding-001), GEMINI_EMBED_DIM dimensions,
    # L2-normalized for cosine search.  Dimension is pinned by migration 0046.
    embedding: Mapped[list] = mapped_column(
        Vector(get_settings().GEMINI_EMBED_DIM), nullable=False
    )
    # As-of timestamp of the source event (not ingestion time) — used for
    # the time-window freshness filter in retrieval.
    as_of_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # source_name, source_url, event_type, affected_symbols, etc.
    # Column name in DB is "metadata"; "extra_data" avoids the SQLAlchemy
    # reserved-attribute conflict (same pattern used by AIRawEvent / AITradingSignal).
    extra_data: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))


class AILLMAuditLog(Base):
    """
    Append-only audit trail for every LLM inference in the Intelligence Layer.

    Governance requirement (SR 11-7, CORTEX_LLM_UPGRADE_PLAN.md §8.3):
    Given any user complaint or regulatory enquiry, the operator must be able
    to reproduce the exact prompt, model version, retrieved sources, guardrail
    events, and output for any historical inference.

    Invariant: no UPDATE or DELETE paths exist for this table anywhere in the
    application.  Only INSERT is permitted.  Schema changes require a migration.
    """
    __tablename__ = "ai_llm_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Stable per logical inference — shared across retries so the full attempt
    # history is visible under a single invocation_id.
    invocation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        server_default=text("gen_random_uuid()"),
    )

    # "sentiment" | "explanation" | "classification" | "embedding"
    invocation_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Table the reference_id belongs to (e.g. "trade_suggestions", "ai_nlp_results")
    reference_table: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Primary key of the domain object this inference was about
    reference_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # "nim" | "ollama"
    model_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # Full model identifier e.g. "qwen/qwen3.5-122b-a10b"
    model_id: Mapped[str] = mapped_column(String(100), nullable=False)

    # SHA-256 of the fully-rendered prompt — enables exact reproduction without
    # storing the full text.
    prompt_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # [{table, id, as_of}] — provenance of every retrieved document chunk
    retrieved_source_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # [] = all guardrails passed; non-empty = list of triggered guardrail names
    guardrail_events: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # First 500 chars of output for triage; NULL on failure
    output_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Exception message on failure; NULL on success
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )


class AISafetyTrigger(Base):
    __tablename__ = "ai_safety_triggers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trigger_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    trigger_condition: Mapped[str] = mapped_column(Text, nullable=False)
    threshold_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    actual_value: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    action_taken: Mapped[str] = mapped_column(String(50), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_data: Mapped[dict | None] = mapped_column("metadata", JSONB)


class AIInstrumentContext(Base):
    """
    Instrument-scoped market context generated by the Intelligence Layer.

    One row per instrument (UNIQUE on instrument_key).  Context is regenerated
    in-place via INSERT … ON CONFLICT DO UPDATE every time the SSE stream
    triggers a new generation and the worker completes successfully.

    Purpose
    -------
    Enables the AI Explanation Panel to render for Watchlist items that have no
    active trade suggestion.  Instead of a blank panel, the user sees recent
    news analysis plus a summary of what the current ML signal is indicating.

    Lifecycle
    ---------
    generated_at  When the LLM finished writing this context.
    expires_at    generated_at + 2 hours.  The SSE poll path checks this field;
                  an expired row triggers a new generation (same as a missing row).

    Audit trail
    -----------
    Every generation (success or failure) writes one ai_llm_audit_log row with
    invocation_type = 'instrument_context' and reference_table = 'ai_instrument_context'.
    Historical context versions are not retained in this table — only the latest.
    """

    __tablename__ = "ai_instrument_context"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # NSE instrument key: "NSE_EQ|INE002A01018"
    instrument_key: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )
    # NSE trading symbol used for RAG retrieval ("RELIANCE"); may be NULL for
    # instrument keys that cannot be decomposed.
    symbol: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # LLM output — both fields populated on success; NULL on generation failure.
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_full:    Mapped[str | None] = mapped_column(Text, nullable=True)
    model_used:      Mapped[str | None] = mapped_column(String(100), nullable=True)

    # [{source_name, as_of, source_url}] from the RAG retriever.
    # Persisted so the SSE push path can serve citation data without a DB round-trip.
    source_refs: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Lifecycle timestamps
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
