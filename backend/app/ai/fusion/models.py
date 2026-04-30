"""
AI Models - ORM models for AI microservice tables.
Import from migration 0005 table definitions.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, LargeBinary, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

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
