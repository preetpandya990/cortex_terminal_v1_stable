"""
Database models for ML system data.
"""

from datetime import datetime, timezone
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from app.core.database import Base


def _utcnow() -> datetime:
    """Timezone-aware UTC datetime for ORM onupdate callbacks."""
    return datetime.now(timezone.utc)


class MLDriftMetric(Base):
    """
    Stores drift detection metrics.

    Tracks feature and prediction drift over time for monitoring.
    """
    __tablename__ = "ml_drift_metrics"

    id = Column(Integer, primary_key=True, index=True)
    model_id = Column(String(255), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    # Drift detection results
    drift_detected = Column(Boolean, default=False, nullable=False)
    drift_type = Column(String(50))  # 'feature', 'prediction', 'both'

    # Feature drift metrics
    feature_drift_scores = Column(JSON)  # Dict of feature_name -> drift_score
    drifted_features = Column(JSON)  # List of feature names with drift

    # Prediction drift metrics
    prediction_drift_score = Column(Float)
    prediction_z_score = Column(Float)
    prediction_ks_statistic = Column(Float)
    prediction_p_value = Column(Float)

    # Statistical summaries
    current_mean = Column(Float)
    current_std = Column(Float)
    historical_mean = Column(Float)
    historical_std = Column(Float)

    # Alert information
    alerts = Column(JSON)  # List of alert messages
    alert_sent = Column(Boolean, default=False)
    alert_sent_at = Column(DateTime(timezone=True))

    # Metadata
    sample_count = Column(Integer)
    notes = Column(Text)

    def __repr__(self):
        return f"<MLDriftMetric(id={self.id}, model_id={self.model_id}, drift_detected={self.drift_detected})>"


class MLPrediction(Base):
    """
    Stores ML predictions for tracking and analysis.
    """
    __tablename__ = "ml_predictions"

    id = Column(Integer, primary_key=True, index=True)
    model_id = Column(String(255), nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, index=True)
    features = Column(JSON)
    prediction = Column(Float, nullable=False)
    prediction_proba = Column(JSON)
    symbol = Column(String(50), index=True)
    prediction_type = Column(String(50))
    model_version = Column(String(50))
    user_id = Column(String(255), index=True)

    def __repr__(self):
        return f"<MLPrediction(id={self.id}, model_id={self.model_id}, symbol={self.symbol})>"


class MLAuditLog(Base):
    """
    Stores audit logs for ML API access and authentication events.
    """
    __tablename__ = "ml_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    event_type = Column(String(50), nullable=False, index=True)  # 'auth_success', 'auth_failure', 'prediction', etc.

    # User information
    user_id = Column(String(255), index=True)

    # Request information
    endpoint = Column(String(255))
    ip_address = Column(String(50))
    user_agent = Column(String(500))
    request_data = Column(JSON)

    # Response information
    response_status = Column(Integer)
    error_message = Column(Text)

    # Related entities
    prediction_id = Column(Integer)

    def __repr__(self):
        return f"<MLAuditLog(id={self.id}, event_type={self.event_type}, user_id={self.user_id})>"


class MLModelMetadata(Base):
    """
    Stores ML model metadata and training information.
    """
    __tablename__ = "ml_model_metadata"

    id = Column(Integer, primary_key=True, index=True)
    model_id = Column(String(255), unique=True, nullable=False, index=True)
    model_name = Column(String(255), nullable=False)
    model_version = Column(String(50), nullable=False)

    # Training information
    trained_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    training_samples = Column(Integer)
    training_features = Column(JSON)  # List of feature names

    # Training statistics (for drift baseline)
    training_feature_stats = Column(JSON)  # Dict of feature_name -> {mean, std, min, max}
    training_prediction_stats = Column(JSON)  # {mean, std, min, max}

    # Model performance
    training_metrics = Column(JSON)  # Dict of metric_name -> value
    validation_metrics = Column(JSON)  # CPCV/DSR/PBO distribution lives here (ML overhaul A3)

    # Provenance & lineage (ML overhaul A0.2) — end-to-end reproducibility for the
    # gated promotion pipeline. JSONB schema:
    #   {git_commit, data_window:{start,end}, config_hash, feature_manifest_sha,
    #    calibrator_sha, cpcv:{n_folds,n_test_folds,purge,embargo}, dsr, pbo}
    # Nullable: existing rows stay NULL until their next (re)training run.
    lineage = Column(JSONB)

    # Model artifacts
    model_path = Column(String(500))
    onnx_path = Column(String(500))

    # Integrity
    checksum = Column(String(64))  # SHA-256 of the inference artifact
    encrypted = Column(Boolean, default=False)

    # Feature-set contract the model was trained on (migration 0056):
    # '1.0.0' legacy 69-feature, '2.0.0' PIT rank-normalized 66-feature.
    # NULL is read as '1.0.0' by the registry loader. Inference gates on this
    # persisted value — never on the training config flag — so live 1.0.0
    # models keep exact legacy behavior regardless of what training does.
    feature_version = Column(String(16), nullable=True)

    # Status
    is_active = Column(Boolean, default=False)
    deployed_at = Column(DateTime(timezone=True))

    # Lifecycle
    status = Column(String(20), default="development")  # development, staging, production, archived
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=_utcnow)

    def __repr__(self):
        return f"<MLModelMetadata(id={self.id}, model_id={self.model_id}, status={self.status})>"


class MLFeatureCrossStats(Base):
    """
    Per-(date, feature, version) cross-sectional rank grid (migration 0056).

    Written by training's rank-normalization pass (WS2b) and the daily
    feature-computation post-pass; read by inference (FeatureLoader) to map a
    raw fundamental value through the newest grid with as_of_date <= row date,
    reproducing the training-time rank transform exactly.

    ``quantiles`` holds 101 raw-value floats (p0..p100) of that day's
    cross-section; inference interpolates rank = np.interp(raw, quantiles,
    linspace(-1, 1, 101)), clipped to [-1, 1].
    """
    __tablename__ = "ml_feature_cross_stats"
    __table_args__ = (
        UniqueConstraint(
            "as_of_date", "feature_name", "feature_version",
            name="uq_ml_feature_cross_stats_date_feature_version",
        ),
        Index(
            "idx_ml_feature_cross_stats_feature_date",
            "feature_name", "as_of_date",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    as_of_date = Column(Date, nullable=False)
    feature_name = Column(String(64), nullable=False)
    feature_version = Column(String(16), nullable=False)
    quantiles = Column(JSONB, nullable=False)  # 101 raw-value floats, p0..p100
    median = Column(Float, nullable=False)
    n_obs = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self):
        return (
            f"<MLFeatureCrossStats(as_of_date={self.as_of_date}, "
            f"feature={self.feature_name}, version={self.feature_version}, "
            f"n_obs={self.n_obs})>"
        )


class MLPredictionOutcome(Base):
    """
    Unified tracking for ALL ML predictions (pattern detection, sentiment, ensemble).
    
    Purpose:
    - ML governance and accuracy measurement
    - Track predictions whether traded or not
    - Measure directional accuracy and TP/SL hit rates
    - Enable continuous model performance monitoring
    - Link to paper trading outcomes when executed
    
    Key Design:
    - Tracks ALL predictions (not just traded ones)
    - Measures outcomes for non-traded predictions too
    - Links to paper_positions via position_id (optional)
    - Supports pattern-specific accuracy analysis
    """
    __tablename__ = "ml_prediction_outcomes"
    
    # Primary Key & Instrument
    id = Column(PGUUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid())
    symbol = Column(String(50), nullable=False, index=True)
    instrument_key = Column(String(100), nullable=False)
    predicted_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    
    # ML Prediction Data
    model_version = Column(String(50), nullable=False)
    prediction_type = Column(String(50), nullable=False)  # 'PATTERN', 'SENTIMENT', 'ENSEMBLE'
    pattern_name = Column(String(50), index=True)
    pattern_timeframe = Column(String(10))
    signal_direction = Column(String(4), nullable=False)  # 'BUY', 'SELL'
    confidence_score = Column(Numeric(5, 4), nullable=False)
    confidence_level = Column(String(10), index=True)  # 'HIGH', 'MEDIUM', 'LOW'
    
    # Price Targets
    predicted_entry_price = Column(Numeric(12, 4), nullable=False)
    predicted_stop_loss = Column(Numeric(12, 4))
    predicted_tp1 = Column(Numeric(12, 4))
    predicted_tp2 = Column(Numeric(12, 4))
    predicted_tp3 = Column(Numeric(12, 4))
    
    # Execution Data (NULL if not traded)
    was_traded = Column(Boolean, nullable=False, server_default=func.false())
    portfolio_id = Column(PGUUID(as_uuid=True))
    position_id = Column(PGUUID(as_uuid=True))
    actual_entry_price = Column(Numeric(12, 4))
    actual_exit_price = Column(Numeric(12, 4))
    gross_pnl = Column(Numeric(14, 4))
    net_pnl = Column(Numeric(14, 4))
    
    # Outcome Measurement (for ALL predictions)
    outcome_status = Column(String(20), nullable=False, server_default="PENDING", index=True)
    ml_direction_correct = Column(Boolean, index=True)
    hit_predicted_tp1 = Column(Boolean, server_default=func.false())
    hit_predicted_tp2 = Column(Boolean, server_default=func.false())
    hit_predicted_tp3 = Column(Boolean, server_default=func.false())
    hit_predicted_sl = Column(Boolean, server_default=func.false())
    max_favorable_move_pct = Column(Numeric(8, 4))
    max_adverse_move_pct = Column(Numeric(8, 4))
    final_move_pct = Column(Numeric(8, 4))
    measurement_window_days = Column(Integer, server_default="5")
    measured_at = Column(DateTime(timezone=True))
    
    # Metadata
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=_utcnow)
    
    def __repr__(self):
        return (
            f"<MLPredictionOutcome(id={self.id}, symbol={self.symbol}, "
            f"type={self.prediction_type}, direction={self.signal_direction}, "
            f"status={self.outcome_status})>"
        )


    def __repr__(self):
        return f"<MLModelMetadata(model_id={self.model_id}, version={self.model_version}, status={self.status})>"


class MLFeature(Base):
    """Stores computed feature values for ML training and inference."""
    __tablename__ = "ml_features"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(50), nullable=False, index=True)
    timeframe = Column(String(50), nullable=False, index=True)
    feature_name = Column(String(255), nullable=False, index=True)
    feature_value = Column(Float)
    version = Column(String(50), nullable=False, index=True)
    computed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    def __repr__(self):
        return f"<MLFeature(symbol={self.symbol}, feature={self.feature_name}, version={self.version})>"


class MLExperiment(Base):
    """Stores ML training experiment metadata and results."""
    __tablename__ = "ml_experiments"

    id = Column(Integer, primary_key=True, index=True)
    experiment_name = Column(String(255), nullable=False, index=True)
    hyperparameters = Column(JSON)
    metrics = Column(JSON)
    artifacts = Column(JSON)
    status = Column(String(50), nullable=False, default="running")
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True))
    error_message = Column(Text)

    def __repr__(self):
        return f"<MLExperiment(id={self.id}, name={self.experiment_name}, status={self.status})>"
