**IMPLEMENTATION PLAN: Bidirectional Multi-Agent Trade Suggestion System**                                                                           
                                                                                                                                                       
  Project: Hawk-Eye Radar - Trade Suggestions Landing Page                                                                                             
  Date: April 21, 2026                                                                                                                                 
  Version: 1.0                                                                                                                                         
  Status: Planning Phase Complete - Awaiting Approval                                                                                                  
                                                                                                                                                       
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
                                                                                                                                                       
  **Executive Summary**                                                                                                                                
                                                                                                                                                       
  Building a world-class event correlation engine that orchestrates bidirectional signal validation between Market Scanner, AI Intelligence, and ML    
  Predictor agents. This system will generate high-confidence trade suggestions displayed on the Hawk-Eye Radar landing page, with detailed analysis   
  accessible via dynamic routes.                                                                                                                       
                                                                                                                                                       
  Key Principles:                                                                                                                                      
                                                                                                                                                       
  - ✅ Production-grade event-driven architecture (EDA)                                                                                                
  - ✅ Sub-100ms consensus latency target                                                                                                              
  - ✅ TimescaleDB hypertable optimization for time-series queries                                                                                     
  - ✅ Redis Streams for guaranteed message delivery                                                                                                   
  - ✅ Circuit breakers and graceful degradation                                                                                                       
  - ✅ Comprehensive observability and monitoring                                                                                                      
  - ✅ Zero-downtime deployments                                                                                                                       
                                                                                                                                                       
  Architecture Overview:                                                                                                                               
                                                                                                                                                       
  ┌─────────────────────────────────────────────────────────────┐                                                                                      
  │                    Redis Pub/Sub + Streams                   │                                                                                     
  ├─────────────────────────────────────────────────────────────┤                                                                                      
  │  • scanner:anomaly:{symbol}                                  │                                                                                     
  │  • intelligence:event:{event_id}                             │                                                                                     
  │  • ml:prediction:{symbol}                                    │                                                                                     
  │  • suggestions:new                                           │                                                                                     
  └─────────────────────────────────────────────────────────────┘                                                                                      
           ↑                    ↑                    ↑                                                                                                 
           │                    │                    │                                                                                                 
      ┌────┴────┐         ┌─────┴─────┐       ┌─────┴─────┐                                                                                            
      │ Market  │         │    AI     │       │    ML     │                                                                                            
      │ Scanner │←───────→│Intelligence│←─────→│ Predictor │                                                                                           
      │ Agent   │         │   Agent   │       │   Agent   │                                                                                            
      └─────────┘         └───────────┘       └───────────┘                                                                                            
           │                    │                    │                                                                                                 
           └────────────────────┼────────────────────┘                                                                                                 
                                ↓                                                                                                                      
                      ┌──────────────────┐                                                                                                             
                      │   Correlation    │                                                                                                             
                      │     Engine       │                                                                                                             
                      │  (Consensus)     │                                                                                                             
                      └──────────────────┘                                                                                                             
                                ↓                                                                                                                      
                      ┌──────────────────┐                                                                                                             
                      │ Trade Suggestion │                                                                                                             
                      │   Generator      │                                                                                                             
                      └──────────────────┘                                                                                                             
                                ↓                                                                                                                      
                      ┌──────────────────┐                                                                                                             
                      │  PostgreSQL +    │                                                                                                             
                      │  TimescaleDB     │                                                                                                             
                      └──────────────────┘                                                                                                             
                                ↓                                                                                                                      
                      ┌──────────────────┐                                                                                                             
                      │  Next.js ISR     │                                                                                                             
                      │  Landing Page    │                                                                                                             
                      └──────────────────┘                                                                                                             
                                                                                                                                                       
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
                                                                                                                                                       
  **Phase 1: Database Schema & Migrations**                                                                                                            
                                                                                                                                                       
  **Task 1.1: Create Trade Suggestions Table**                                                                                                         
                                                                                                                                                       
  File: backend/migrations/0009_trade_suggestions.sql                                                                                                  
  Objective: Design production-grade schema with TimescaleDB optimization                                                                              
                                                                                                                                                       
  -- Migration: 0009_trade_suggestions.sql                                                                                                             
  CREATE TABLE trade_suggestions (                                                                                                                     
      id BIGSERIAL PRIMARY KEY,                                                                                                                        
      suggestion_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid(),                                                                                    
                                                                                                                                                       
      -- Instrument identification                                                                                                                     
      symbol VARCHAR(50) NOT NULL,                                                                                                                     
      instrument_key VARCHAR(100) NOT NULL,                                                                                                            
      trading_symbol VARCHAR(50),                                                                                                                      
                                                                                                                                                       
      -- Consensus metadata                                                                                                                            
      consensus_score NUMERIC(5,2) NOT NULL CHECK (consensus_score >= 0 AND consensus_score <= 100),                                                   
      confidence_level VARCHAR(20) NOT NULL CHECK (confidence_level IN ('HIGH', 'MEDIUM', 'LOW')),                                                     
      signal_direction VARCHAR(10) NOT NULL CHECK (signal_direction IN ('BUY', 'SELL')),                                                               
                                                                                                                                                       
      -- Pathway tracking (which triggered first)                                                                                                      
      trigger_pathway VARCHAR(20) NOT NULL CHECK (trigger_pathway IN ('TECHNICAL_FIRST', 'FUNDAMENTAL_FIRST')),                                        
                                                                                                                                                       
      -- Contributing signals (JSONB for flexibility)                                                                                                  
      scanner_signal JSONB NOT NULL,                                                                                                                   
      ai_signal JSONB NOT NULL,                                                                                                                        
      ml_signal JSONB NOT NULL,                                                                                                                        
                                                                                                                                                       
      -- Trade parameters                                                                                                                              
      entry_price NUMERIC(12,2),                                                                                                                       
      stop_loss NUMERIC(12,2),                                                                                                                         
      risk_reward_ratio NUMERIC(5,2),                                                                                                                  
      take_profit_1 NUMERIC(12,2),                                                                                                                     
      take_profit_2 NUMERIC(12,2),                                                                                                                     
      take_profit_3 NUMERIC(12,2),                                                                                                                     
                                                                                                                                                       
      -- Temporal metadata                                                                                                                             
      generated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),                                                                                    
      expires_at TIMESTAMP WITH TIME ZONE NOT NULL,                                                                                                    
      status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'expired', 'executed', 'invalidated')),                                          
                                                                                                                                                       
      -- Audit trail                                                                                                                                   
      created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),                                                                                               
      updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()                                                                                                
  );                                                                                                                                                   
                                                                                                                                                       
  -- Convert to TimescaleDB hypertable for time-series optimization                                                                                    
  SELECT create_hypertable('trade_suggestions', 'generated_at',                                                                                        
      chunk_time_interval => INTERVAL '1 day',                                                                                                         
      if_not_exists => TRUE                                                                                                                            
  );                                                                                                                                                   
                                                                                                                                                       
  -- Indexes for high-performance queries                                                                                                              
  CREATE INDEX idx_suggestions_symbol_status ON trade_suggestions(symbol, status) WHERE status = 'active';                                             
  CREATE INDEX idx_suggestions_generated_at_desc ON trade_suggestions(generated_at DESC);                                                              
  CREATE INDEX idx_suggestions_consensus_score ON trade_suggestions(consensus_score DESC) WHERE status = 'active';                                     
  CREATE INDEX idx_suggestions_confidence_level ON trade_suggestions(confidence_level) WHERE status = 'active';                                        
  CREATE INDEX idx_suggestions_direction ON trade_suggestions(signal_direction) WHERE status = 'active';                                               
                                                                                                                                                       
  -- Composite index for landing page query optimization                                                                                               
  CREATE INDEX idx_suggestions_landing_page ON trade_suggestions(status, consensus_score DESC, generated_at DESC)                                      
      WHERE status = 'active';                                                                                                                         
                                                                                                                                                       
  -- Retention policy: Auto-delete suggestions older than 7 days                                                                                       
  SELECT add_retention_policy('trade_suggestions', INTERVAL '7 days');                                                                                 
                                                                                                                                                       
  Performance Targets:                                                                                                                                 
                                                                                                                                                       
  - Query latency: <10ms for landing page (50 suggestions)                                                                                             
  - Write throughput: 1000+ suggestions/second                                                                                                         
  - Index size: <100MB for 1M suggestions                                                                                                              
                                                                                                                                                       
  Test Cases:                                                                                                                                          
                                                                                                                                                       
  - ✅ Verify hypertable creation                                                                                                                      
  - ✅ Benchmark index performance (EXPLAIN ANALYZE)                                                                                                   
  - ✅ Test retention policy execution                                                                                                                 
  - ✅ Validate constraint enforcement                                                                                                                 
                                                                                                                                                       
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
                                                                                                                                                       
  **Task 1.2: Create Event Correlation Tracking Table**                                                                                                
                                                                                                                                                       
  File: backend/migrations/0009_trade_suggestions.sql (continued)                                                                                      
  Objective: Audit trail for debugging and performance analysis                                                                                        
                                                                                                                                                       
  CREATE TABLE event_correlations (                                                                                                                    
      id BIGSERIAL PRIMARY KEY,                                                                                                                        
      correlation_id UUID UNIQUE NOT NULL DEFAULT gen_random_uuid(),                                                                                   
      suggestion_id UUID REFERENCES trade_suggestions(suggestion_id),                                                                                  
                                                                                                                                                       
      -- Trigger metadata                                                                                                                              
      trigger_type VARCHAR(20) NOT NULL CHECK (trigger_type IN ('SCANNER_ANOMALY', 'NEWS_EVENT')),                                                     
      trigger_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,                                                                                             
                                                                                                                                                       
      -- Agent response times (for latency monitoring)                                                                                                 
      scanner_response_ms INTEGER,                                                                                                                     
      ai_response_ms INTEGER,                                                                                                                          
      ml_response_ms INTEGER,                                                                                                                          
      total_latency_ms INTEGER,                                                                                                                        
                                                                                                                                                       
      -- Consensus decision                                                                                                                            
      consensus_reached BOOLEAN NOT NULL,                                                                                                              
      rejection_reason VARCHAR(200),                                                                                                                   
                                                                                                                                                       
      -- Raw agent outputs (for debugging)                                                                                                             
      scanner_output JSONB,                                                                                                                            
      ai_output JSONB,                                                                                                                                 
      ml_output JSONB,                                                                                                                                 
                                                                                                                                                       
      created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()                                                                                                
  );                                                                                                                                                   
                                                                                                                                                       
  SELECT create_hypertable('event_correlations', 'trigger_timestamp',                                                                                  
      chunk_time_interval => INTERVAL '1 day',                                                                                                         
      if_not_exists => TRUE                                                                                                                            
  );                                                                                                                                                   
                                                                                                                                                       
  CREATE INDEX idx_correlations_suggestion ON event_correlations(suggestion_id);                                                                       
  CREATE INDEX idx_correlations_latency ON event_correlations(total_latency_ms) WHERE consensus_reached = TRUE;                                        
  CREATE INDEX idx_correlations_rejection ON event_correlations(rejection_reason) WHERE consensus_reached = FALSE;                                     
                                                                                                                                                       
  Monitoring Queries:                                                                                                                                  
                                                                                                                                                       
  -- Average consensus latency (target: <100ms)                                                                                                        
  SELECT AVG(total_latency_ms) as avg_latency_ms                                                                                                       
  FROM event_correlations                                                                                                                              
  WHERE consensus_reached = TRUE                                                                                                                       
    AND trigger_timestamp >= NOW() - INTERVAL '1 hour';                                                                                                
                                                                                                                                                       
  -- Rejection rate by reason                                                                                                                          
  SELECT rejection_reason, COUNT(*) as count                                                                                                           
  FROM event_correlations                                                                                                                              
  WHERE consensus_reached = FALSE                                                                                                                      
    AND trigger_timestamp >= NOW() - INTERVAL '1 day'                                                                                                  
  GROUP BY rejection_reason                                                                                                                            
  ORDER BY count DESC;                                                                                                                                 
                                                                                                                                                       
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
                                                                                                                                                       
  **Task 1.3: Create ORM Models**                                                                                                                      
                                                                                                                                                       
  File: backend/app/models/trade_suggestions.py                                                                                                        
                                                                                                                                                       
  """                                                                                                                                                  
  Trade Suggestions ORM Models                                                                                                                         
  =============================                                                                                                                        
  SQLAlchemy models for trade suggestions and event correlations.                                                                                      
  """                                                                                                                                                  
  from datetime import datetime                                                                                                                        
  from decimal import Decimal                                                                                                                          
  from uuid import UUID                                                                                                                                
                                                                                                                                                       
  from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Numeric, String, Text                                                                 
  from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID                                                                                    
  from sqlalchemy.orm import Mapped, mapped_column                                                                                                     
                                                                                                                                                       
  from app.core.database import Base                                                                                                                   
                                                                                                                                                       
                                                                                                                                                       
  class TradeSuggestion(Base):                                                                                                                         
      __tablename__ = "trade_suggestions"                                                                                                              
                                                                                                                                                       
      id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)                                                                
      suggestion_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True, nullable=False)                                                  
                                                                                                                                                       
      # Instrument                                                                                                                                     
      symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)                                                                      
      instrument_key: Mapped[str] = mapped_column(String(100), nullable=False)                                                                         
      trading_symbol: Mapped[str | None] = mapped_column(String(50))                                                                                   
                                                                                                                                                       
      # Consensus                                                                                                                                      
      consensus_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)                                                                  
      confidence_level: Mapped[str] = mapped_column(String(20), nullable=False)                                                                        
      signal_direction: Mapped[str] = mapped_column(String(10), nullable=False)                                                                        
      trigger_pathway: Mapped[str] = mapped_column(String(20), nullable=False)                                                                         
                                                                                                                                                       
      # Signals                                                                                                                                        
      scanner_signal: Mapped[dict] = mapped_column(JSONB, nullable=False)                                                                              
      ai_signal: Mapped[dict] = mapped_column(JSONB, nullable=False)                                                                                   
      ml_signal: Mapped[dict] = mapped_column(JSONB, nullable=False)                                                                                   
                                                                                                                                                       
      # Trade params                                                                                                                                   
      entry_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))                                                                              
      stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))                                                                                
      risk_reward_ratio: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))                                                                         
      take_profit_1: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))                                                                            
      take_profit_2: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))                                                                            
      take_profit_3: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))                                                                            
                                                                                                                                                       
      # Temporal                                                                                                                                       
      generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)                                                          
      expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)                                                            
      status: Mapped[str] = mapped_column(String(20), default="active")                                                                                
                                                                                                                                                       
      created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))                                              
      updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))                                              
                                                                                                                                                       
                                                                                                                                                       
  class EventCorrelation(Base):                                                                                                                        
      __tablename__ = "event_correlations"                                                                                                             
                                                                                                                                                       
      id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)                                                                
      correlation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), unique=True, nullable=False)                                                 
      suggestion_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))                                                                        
                                                                                                                                                       
      trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)                                                                            
      trigger_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)                                                     
                                                                                                                                                       
      scanner_response_ms: Mapped[int | None] = mapped_column(Integer)                                                                                 
      ai_response_ms: Mapped[int | None] = mapped_column(Integer)                                                                                      
      ml_response_ms: Mapped[int | None] = mapped_column(Integer)                                                                                      
      total_latency_ms: Mapped[int | None] = mapped_column(Integer)                                                                                    
                                                                                                                                                       
      consensus_reached: Mapped[bool] = mapped_column(Boolean, nullable=False)                                                                         
      rejection_reason: Mapped[str | None] = mapped_column(String(200))                                                                                
                                                                                                                                                       
      scanner_output: Mapped[dict | None] = mapped_column(JSONB)                                                                                       
      ai_output: Mapped[dict | None] = mapped_column(JSONB)                                                                                            
      ml_output: Mapped[dict | None] = mapped_column(JSONB)                                                                                            
                                                                                                                                                       
      created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("NOW()"))                                              
                                                                                                                                                       
  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
                                                                                                                                                       
  **Phase 2: Event Correlation Engine (Core Service)**                                                                                                 
                                                                                                                                                       
  **Task 2.1: Create Correlation Engine Service**                                                                                                      
                                                                                                                                                       
  File: backend/app/ai/correlation/engine.py                                                                                                           
  Lines of Code: ~500                                                                                                                                  
  Complexity: High                                                                                                                                     
                                                                                                                                                       
  """                                                                                                                                                  
  Event Correlation Engine - Bidirectional Signal Orchestration                                                                                        
  ==============================================================                                                                                       
  Orchestrates cross-agent validation using Redis Streams for guaranteed delivery.                                                                     
  Implements sub-100ms consensus with circuit breakers and graceful degradation.                                                                       
                                                                                                                                                       
  Architecture:                                                                                                                                        
  - Pathway 1: Scanner Anomaly → AI Search + ML Prediction → Consensus                                                                                 
  - Pathway 2: News Event → Scanner Check + ML Prediction → Consensus                                                                                  
                                                                                                                                                       
  Performance Targets:                                                                                                                                 
  - Consensus latency: <100ms (p95)                                                                                                                    
  - Throughput: 1000+ correlations/second                                                                                                              
  - Availability: 99.9%                                                                                                                                
  """                                                                                                                                                  
  import asyncio                                                                                                                                       
  import json                                                                                                                                          
  import logging                                                                                                                                       
  import uuid                                                                                                                                          
  from datetime import datetime, timedelta, timezone                                                                                                   
  from decimal import Decimal                                                                                                                          
  from typing import Literal                                                                                                                           
                                                                                                                                                       
  from sqlalchemy import select                                                                                                                        
  from sqlalchemy.ext.asyncio import AsyncSession                                                                                                      
  from redis.asyncio import Redis                                                                                                                      
                                                                                                                                                       
  from app.ai.fusion.models import AIEventClassification                                                                                               
  from app.ai.fusion.signal_assembler import SignalAssembler                                                                                           
  from app.core.redis import RedisChannels, get_redis                                                                                                  
  from app.models.trade_suggestions import TradeSuggestion, EventCorrelation                                                                           
  from app.schemas.scanner import ScanResult                                                                                                           
  from app.services.market_scanner import MarketScannerService                                                                                         
  from app.ml.inference.ensemble_predictor import EnsemblePredictor                                                                                    
                                                                                                                                                       
  logger = logging.getLogger(__name__)                                                                                                                 
                                                                                                                                                       
  # Consensus configuration                                                                                                                            
  CONSENSUS_HIGH_THRESHOLD = 80.0                                                                                                                      
  CONSENSUS_MEDIUM_THRESHOLD = 60.0                                                                                                                    
  TEMPORAL_ALIGNMENT_WINDOW_SECONDS = 300  # 5 minutes                                                                                                 
  SUGGESTION_EXPIRY_HOURS = 24                                                                                                                         
                                                                                                                                                       
  # Agent weights (must sum to 1.0)                                                                                                                    
  SCANNER_WEIGHT = 0.30                                                                                                                                
  AI_WEIGHT = 0.40                                                                                                                                     
  ML_WEIGHT = 0.30                                                                                                                                     
                                                                                                                                                       
                                                                                                                                                       
  class CircuitBreaker:                                                                                                                                
      """Circuit breaker for fault tolerance."""                                                                                                       
                                                                                                                                                       
      def __init__(self, failure_threshold: int = 5, timeout_seconds: int = 60):                                                                       
          self.failure_threshold = failure_threshold                                                                                                   
          self.timeout_seconds = timeout_seconds                                                                                                       
          self.failure_count = 0                                                                                                                       
          self.last_failure_time: datetime | None = None                                                                                               
          self.state = "closed"  # closed, open, half_open                                                                                             
                                                                                                                                                       
      def record_success(self):                                                                                                                        
          if self.state == "half_open":                                                                                                                
              self.state = "closed"                                                                                                                    
              self.failure_count = 0                                                                                                                   
              logger.info("Circuit breaker closed")                                                                                                    
                                                                                                                                                       
      def record_failure(self):                                                                                                                        
          self.failure_count += 1                                                                                                                      
          self.last_failure_time = datetime.now(timezone.utc)                                                                                          
          if self.failure_count >= self.failure_threshold:                                                                                             
              self.state = "open"                                                                                                                      
              logger.warning(f"Circuit breaker opened (failures: {self.failure_count})")                                                               
                                                                                                                                                       
      def can_attempt(self) -> bool:                                                                                                                   
          if self.state == "closed":                                                                                                                   
              return True                                                                                                                              
          if self.state == "open" and self.last_failure_time:                                                                                          
              elapsed = (datetime.now(timezone.utc) - self.last_failure_time).total_seconds()                                                          
              if elapsed >= self.timeout_seconds:                                                                                                      
                  self.state = "half_open"                                                                                                             
                  logger.info("Circuit breaker half-open")                                                                                             
                  return True                                                                                                                          
              return False                                                                                                                             
          return True                                                                                                                                  
                                                                                                                                                       
                                                                                                                                                       
  class EventCorrelationEngine:                                                                                                                        
      """                                                                                                                                              
      Orchestrates bidirectional signal validation between agents.                                                                                     
      Uses Redis Streams for guaranteed message delivery.                                                                                              
      """                                                                                                                                              
                                                                                                                                                       
      def __init__(                                                                                                                                    
          self,                                                                                                                                        
          scanner_service: MarketScannerService,                                                                                                       
          signal_assembler: SignalAssembler,                                                                                                           
          ensemble_predictor: EnsemblePredictor,                                                                                                       
          redis: Redis,                                                                                                                                
      ):                                                                                                                                               
          self.scanner = scanner_service                                                                                                               
          self.assembler = signal_assembler                                                                                                            
          self.predictor = ensemble_predictor                                                                                                          
          self.redis = redis                                                                                                                           
                                                                                                                                                       
          # Circuit breakers per agent                                                                                                                 
          self.circuit_breakers = {                                                                                                                    
              "scanner": CircuitBreaker(failure_threshold=5, timeout_seconds=60),                                                                      
              "ai": CircuitBreaker(failure_threshold=5, timeout_seconds=60),                                                                           
              "ml": CircuitBreaker(failure_threshold=5, timeout_seconds=60),                                                                           
          }                                                                                                                                            
                                                                                                                                                       
      async def on_scanner_anomaly(                                                                                                                    
          self,                                                                                                                                        
          db: AsyncSession,                                                                                                                            
          scan_result: ScanResult,                                                                                                                     
      ) -> TradeSuggestion | None:                                                                                                                     
          """                                                                                                                                          
          Pathway 1: Technical anomaly triggers fundamental validation.                                                                                
                                                                                                                                                       
          Flow:                                                                                                                                        
          1. Scanner detects anomaly (high gainer/loser, volume spike, breakout)                                                                       
          2. Trigger AI Intelligence to search news                                                                                                    
          3. Trigger ML Predictor to generate forecast                                                                                                 
          4. Compute consensus and generate suggestion if aligned                                                                                      
                                                                                                                                                       
          Args:                                                                                                                                        
              db: Database session                                                                                                                     
              scan_result: Scanner detection result                                                                                                    
                                                                                                                                                       
          Returns:                                                                                                                                     
              TradeSuggestion if consensus reached, None otherwise                                                                                     
          """                                                                                                                                          
          correlation_id = str(uuid.uuid4())                                                                                                           
          trigger_timestamp = datetime.now(timezone.utc)                                                                                               
                                                                                                                                                       
          logger.info(f"[{correlation_id}] Pathway 1: Scanner anomaly for {scan_result.instrument_key}")                                               
                                                                                                                                                       
          # Publish to Redis Stream for AI Intelligence                                                                                                
          await self.redis.xadd(                                                                                                                       
              "cai:stream:scanner_anomalies",                                                                                                          
              {                                                                                                                                        
                  "correlation_id": correlation_id,                                                                                                    
                  "symbol": scan_result.instrument_key,                                                                                                
                  "anomaly_type": scan_result.signal,                                                                                                  
                  "price_change_pct": str(scan_result.price_change_pct),                                                                               
                  "volume_ratio": str(scan_result.volume_ratio),                                                                                       
                  "timestamp": trigger_timestamp.isoformat(),                                                                                          
              }                                                                                                                                        
          )                                                                                                                                            
                                                                                                                                                       
          # Gather signals with timeout                                                                                                                
          try:                                                                                                                                         
              scanner_signal, ai_signal, ml_signal, latencies = await asyncio.wait_for(                                                                
                  self._gather_signals_pathway1(db, scan_result),                                                                                      
                  timeout=5.0  # 5 second timeout                                                                                                      
              )                                                                                                                                        
          except asyncio.TimeoutError:                                                                                                                 
              logger.warning(f"[{correlation_id}] Timeout gathering signals")                                                                          
              await self._record_correlation(db, correlation_id, trigger_timestamp, None, "TIMEOUT")                                                   
              return None                                                                                                                              
          except Exception as e:                                                                                                                       
              logger.error(f"[{correlation_id}] Error gathering signals: {e}", exc_info=True)                                                          
              await self._record_correlation(db, correlation_id, trigger_timestamp, None, f"ERROR: {str(e)}")                                          
              return None                                                                                                                              
                                                                                                                                                       
          # Compute consensus                                                                                                                          
          suggestion = await self._compute_consensus(                                                                                                  
              db=db,                                                                                                                                   
              correlation_id=correlation_id,                                                                                                           
              trigger_type="SCANNER_ANOMALY",                                                                                                          
              trigger_timestamp=trigger_timestamp,                                                                                                     
              scanner_signal=scanner_signal,                                                                                                           
              ai_signal=ai_signal,                                                                                                                     
              ml_signal=ml_signal,                                                                                                                     
              latencies=latencies,                                                                                                                     
          )                                                                                                                                            
                                                                                                                                                       
          if suggestion:                                                                                                                               
              logger.info(f"[{correlation_id}] HIGH CONFIDENCE {suggestion.signal_direction} suggestion generated")                                    
                                                                                                                                                       
          return suggestion                                                                                                                            
                                                                                                                                                       
      async def on_news_event(                                                                                                                         
          self,                                                                                                                                        
          db: AsyncSession,                                                                                                                            
          event: AIEventClassification,                                                                                                                
      ) -> list[TradeSuggestion]:                                                                                                                      
          """                                                                                                                                          
          Pathway 2: Fundamental event triggers technical validation.                                                                                  
                                                                                                                                                       
          Flow:                                                                                                                                        
          1. AI Intelligence detects breaking news                                                                                                     
          2. Extract affected symbols                                                                                                                  
          3. For each symbol, query Scanner + ML Predictor                                                                                             
          4. Compute consensus and generate suggestions                                                                                                
                                                                                                                                                       
          Args:                                                                                                                                        
              db: Database session                                                                                                                     
              event: Classified news event                                                                                                             
                                                                                                                                                       
          Returns:                                                                                                                                     
              List of TradeSuggestions for affected symbols                                                                                            
          """                                                                                                                                          
          correlation_id = str(uuid.uuid4())                                                                                                           
          trigger_timestamp = datetime.now(timezone.utc)                                                                                               
                                                                                                                                                       
          affected_symbols = event.affected_symbols or []                                                                                              
          logger.info(f"[{correlation_id}] Pathway 2: News event affecting {len(affected_symbols)} symbols")                                           
                                                                                                                                                       
          suggestions = []                                                                                                                             
                                                                                                                                                       
          for symbol in affected_symbols:                                                                                                              
              try:                                                                                                                                     
                  scanner_signal, ai_signal, ml_signal, latencies = await asyncio.wait_for(                                                            
                      self._gather_signals_pathway2(db, symbol, event),                                                                                
                      timeout=5.0                                                                                                                      
                  )                                                                                                                                    
                                                                                                                                                       
                  suggestion = await self._compute_consensus(                                                                                          
                      db=db,                                                                                                                           
                      correlation_id=f"{correlation_id}_{symbol}",                                                                                     
                      trigger_type="NEWS_EVENT",                                                                                                       
                      trigger_timestamp=trigger_timestamp,                                                                                             
                      scanner_signal=scanner_signal,                                                                                                   
                      ai_signal=ai_signal,                                                                                                             
                      ml_signal=ml_signal,                                                                                                             
                      latencies=latencies,                                                                                                             
                  )                                                                                                                                    
                                                                                                                                                       
                  if suggestion:                                                                                                                       
                      suggestions.append(suggestion)                                                                                                   
                                                                                                                                                       
              except Exception as e:                                                                                                                   
                  logger.error(f"[{correlation_id}] Error processing {symbol}: {e}")                                                                   
                  continue                                                                                                                             
                                                                                                                                                       
          return suggestions                                                                                                                           
                                                                                                                                                       
      async def _gather_signals_pathway1(                                                                                                              
          self,                                                                                                                                        
          db: AsyncSession,                                                                                                                            
          scan_result: ScanResult,                                                                                                                     
      ) -> tuple[dict, dict, dict, dict]:                                                                                                              
          """Gather AI and ML signals for scanner-triggered event."""                                                                                  
          start_time = datetime.now(timezone.utc)                                                                                                      
                                                                                                                                                       
          # Scanner signal already available                                                                                                           
          scanner_signal = {                                                                                                                           
              "direction": scan_result.signal,  # "buy" or "sell"                                                                                      
              "confidence": min(abs(scan_result.score) * 10, 100),  # Normalize to 0-100                                                               
              "signals": [s.model_dump() for s in scan_result.signals],                                                                                
              "price_change_pct": scan_result.price_change_pct,                                                                                        
              "volume_ratio": scan_result.volume_ratio,                                                                                                
              "instrument_key": scan_result.instrument_key,                                                                                            
              "trading_symbol": scan_result.trading_symbol,                                                                                            
          }                                                                                                                                            
          scanner_latency = 0  # Already computed                                                                                                      
                                                                                                                                                       
          # AI Intelligence: Search news                                                                                                               
          ai_start = datetime.now(timezone.utc)                                                                                                        
          ai_signal = await self.assembler.gather_event_signals(db, scan_result.instrument_key)                                                        
          ai_latency = (datetime.now(timezone.utc) - ai_start).total_seconds() * 1000                                                                  
                                                                                                                                                       
          # ML Predictor: Generate forecast                                                                                                            
          ml_start = datetime.now(timezone.utc)                                                                                                        
          ml_signal = await self.assembler.gather_ml_signals(db, scan_result.instrument_key)                                                           
          ml_latency = (datetime.now(timezone.utc) - ml_start).total_seconds() * 1000                                                                  
                                                                                                                                                       
          total_latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000                                                             
                                                                                                                                                       
          latencies = {                                                                                                                                
              "scanner_ms": scanner_latency,                                                                                                           
              "ai_ms": int(ai_latency),                                                                                                                
              "ml_ms": int(ml_latency),                                                                                                                
              "total_ms": int(total_latency),                                                                                                          
          }                                                                                                                                            
                                                                                                                                                       
          return scanner_signal, ai_signal, ml_signal, latencies                                                                                       
                                                                                                                                                       
      async def _gather_signals_pathway2(                                                                                                              
          self,                                                                                                                                        
          db: AsyncSession,                                                                                                                            
          symbol: str,                                                                                                                                 
          event: AIEventClassification,                                                                                                                
      ) -> tuple[dict, dict, dict, dict]:                                                                                                              
          """Gather scanner and ML signals for news-triggered event."""                                                                                
          start_time = datetime.now(timezone.utc)                                                                                                      
                                                                                                                                                       
          # AI signal from event                                                                                                                       
          ai_signal = {                                                                                                                                
              "score": float(event.impact_score),                                                                                                      
              "confidence": float(event.classification_confidence),                                                                                    
              "sentiment": "positive" if event.impact_score > 0 else "negative",                                                                       
              "event_type": event.event_type,                                                                                                          
              "event_count": 1,                                                                                                                        
              "events": [{"id": event.id, "type": event.event_type, "impact": float(event.impact_score)}],                                             
          }                                                                                                                                            
          ai_latency = 0  # Already available                                                                                                          
                                                                                                                                                       
          # Scanner: Check current technical state                                                                                                     
          scanner_start = datetime.now(timezone.utc)                                                                                                   
          scan_results = await self.scanner.scan_all(db, timeframe="1d", force_refresh=True)                                                           
          scanner_result = next((r for r in scan_results if r.instrument_key == symbol), None)                                                         
                                                                                                                                                       
          if not scanner_result:                                                                                                                       
              raise ValueError(f"Scanner result not found for {symbol}")                                                                               
                                                                                                                                                       
          scanner_signal = {                                                                                                                           
              "direction": scanner_result.signal,                                                                                                      
              "confidence": min(abs(scanner_result.score) * 10, 100),                                                                                  
              "signals": [s.model_dump() for s in scanner_result.signals],                                                                             
              "price_change_pct": scanner_result.price_change_pct,                                                                                     
              "volume_ratio": scanner_result.volume_ratio,                                                                                             
              "instrument_key": scanner_result.instrument_key,                                                                                         
              "trading_symbol": scanner_result.trading_symbol,                                                                                         
          }                                                                                                                                            
          scanner_latency = (datetime.now(timezone.utc) - scanner_start).total_seconds() * 1000                                                        
                                                                                                                                                       
          # ML Predictor                                                                                                                               
          ml_start = datetime.now(timezone.utc)                                                                                                        
          ml_signal = await self.assembler.gather_ml_signals(db, symbol)                                                                               
          ml_latency = (datetime.now(timezone.utc) - ml_start).total_seconds() * 1000                                                                  
                                                                                                                                                       
          total_latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000                                                             
                                                                                                                                                       
          latencies = {                                                                                                                                
              "scanner_ms": int(scanner_latency),                                                                                                      
              "ai_ms": ai_latency,                                                                                                                     
              "ml_ms": int(ml_latency),                                                                                                                
              "total_ms": int(total_latency),                                                                                                          
          }                                                                                                                                            
                                                                                                                                                       
          return scanner_signal, ai_signal, ml_signal, latencies                                                                                       
                                                                                                                                                       
      async def _compute_consensus(                                                                                                                    
          self,                                                                                                                                        
          db: AsyncSession,                                                                                                                            
          correlation_id: str,                                                                                                                         
          trigger_type: Literal["SCANNER_ANOMALY", "NEWS_EVENT"],                                                                                      
          trigger_timestamp: datetime,                                                                                                                 
          scanner_signal: dict,                                                                                                                        
          ai_signal: dict,                                                                                                                             
          ml_signal: dict,                                                                                                                             
          latencies: dict,                                                                                                                             
      ) -> TradeSuggestion | None:                                                                                                                     
          """                                                                                                                                          
          Compute weighted consensus with directional alignment check.                                                                                 
                                                                                                                                                       
          All three agents must agree on direction (BUY or SELL).                                                                                      
          Consensus score must exceed threshold.                                                                                                       
                                                                                                                                                       
          Args:                                                                                                                                        
              db: Database session                                                                                                                     
              correlation_id: Unique correlation identifier                                                                                            
              trigger_type: SCANNER_ANOMALY or NEWS_EVENT                                                                                              
              trigger_timestamp: When correlation started                                                                                              
              scanner_signal: Technical analysis signal                                                                                                
              ai_signal: News/sentiment signal                                                                                                         
              ml_signal: ML prediction signal                                                                                                          
              latencies: Response times per agent                                                                                                      
                                                                                                                                                       
          Returns:                                                                                                                                     
              TradeSuggestion if consensus reached, None otherwise                                                                                     
          """                                                                                                                                          
          # Map to unified direction                                                                                                                   
          scanner_dir = "BUY" if scanner_signal["direction"] in ["buy", "bullish"] else "SELL"                                                         
                                                                                                                                                       
          ai_score = ai_signal.get("score", 0.0)                                                                                                       
          ai_dir = "BUY" if ai_score > 0 else "SELL"                                                                                                   
                                                                                                                                                       
          ml_dir = ml_signal.get("prediction", {}).get("direction", "HOLD")                                                                            
          if ml_dir == "HOLD":                                                                                                                         
              # Neutral ML signal - reject                                                                                                             
              await self._record_correlation(                                                                                                          
                  db, correlation_id, trigger_timestamp, None, "ML_NEUTRAL", latencies                                                                 
              )                                                                                                                                        
              return None                                                                                                                              
                                                                                                                                                       
          # Check directional alignment                                                                                                                
          all_buy = (scanner_dir == "BUY" and ai_dir == "BUY" and ml_dir == "BUY")                                                                     
          all_sell = (scanner_dir == "SELL" and ai_dir == "SELL" and ml_dir == "SELL")                                                                 
                                                                                                                                                       
          if not (all_buy or all_sell):                                                                                                                
              await self._record_correlation(                                                                                                          
                  db, correlation_id, trigger_timestamp, None,                                                                                         
                  f"DIRECTION_MISMATCH: Scanner={scanner_dir}, AI={ai_dir}, ML={ml_dir}",                                                              
                  latencies                                                                                                                            
              )                                                                                                                                        
              return None                                                                                                                              
                                                                                                                                                       
          # Compute weighted consensus score                                                                                                           
          scanner_conf = scanner_signal.get("confidence", 0.0)                                                                                         
          ai_conf = abs(ai_signal.get("confidence", 0.0)) * 100  # Normalize to 0-100                                                                  
          ml_conf = ml_signal.get("confidence", 0.0) * 100  # Normalize to 0-100                                                                       
                                                                                                                                                       
          consensus_score = (                                                                                                                          
              SCANNER_WEIGHT * scanner_conf +                                                                                                          
              AI_WEIGHT * ai_conf +                                                                                                                    
              ML_WEIGHT * ml_conf                                                                                                                      
          )                                                                                                                                            
                                                                                                                                                       
          # Determine confidence level                                                                                                                 
          if consensus_score >= CONSENSUS_HIGH_THRESHOLD:                                                                                              
              confidence_level = "HIGH"                                                                                                                
          elif consensus_score >= CONSENSUS_MEDIUM_THRESHOLD:                                                                                          
              confidence_level = "MEDIUM"                                                                                                              
          else:                                                                                                                                        
              await self._record_correlation(                                                                                                          
                  db, correlation_id, trigger_timestamp, None,                                                                                         
                  f"LOW_CONFIDENCE: {consensus_score:.2f}",                                                                                            
                  latencies                                                                                                                            
              )                                                                                                                                        
              return None                                                                                                                              
                                                                                                                                                       
          # Extract trade parameters from ML signal                                                                                                    
          ml_prediction = ml_signal.get("prediction", {})                                                                                              
          entry_price = ml_prediction.get("entry_price")                                                                                               
          stop_loss = ml_prediction.get("stop_loss")                                                                                                   
          targets = ml_prediction.get("targets", [])                                                                                                   
                                                                                                                                                       
          # Calculate risk/reward ratio                                                                                                                
          risk_reward_ratio = None                                                                                                                     
          if entry_price and stop_loss and targets:                                                                                                    
              risk = abs(entry_price - stop_loss)                                                                                                      
              reward = abs(targets[0] - entry_price) if targets[0] else 0                                                                              
              risk_reward_ratio = reward / risk if risk > 0 else None                                                                                  
                                                                                                                                                       
          # Create trade suggestion                                                                                                                    
          suggestion = TradeSuggestion(                                                                                                                
              symbol=scanner_signal.get("instrument_key") or ml_signal.get("symbol"),                                                                  
              instrument_key=scanner_signal.get("instrument_key") or ml_signal.get("symbol"),                                                          
              trading_symbol=scanner_signal.get("trading_symbol"),                                                                                     
              consensus_score=Decimal(str(round(consensus_score, 2))),                                                                                 
              confidence_level=confidence_level,                                                                                                       
              signal_direction="BUY" if all_buy else "SELL",                                                                                           
              trigger_pathway="TECHNICAL_FIRST" if trigger_type == "SCANNER_ANOMALY" else "FUNDAMENTAL_FIRST",                                         
              scanner_signal=scanner_signal,                                                                                                           
              ai_signal=ai_signal,                                                                                                                     
              ml_signal=ml_signal,                                                                                                                     
              entry_price=Decimal(str(entry_price)) if entry_price else None,
              stop_loss=Decimal(str(stop_loss)) if stop_loss else None,
              risk_reward_ratio=Decimal(str(round(risk_reward_ratio, 2))) if risk_reward_ratio else None,
              take_profit_1=Decimal(str(targets[0])) if len(targets) > 0 and targets[0] else None,
              take_profit_2=Decimal(str(targets[1])) if len(targets) > 1 and targets[1] else None,
              take_profit_3=Decimal(str(targets[2])) if len(targets) > 2 and targets[2] else None,
              generated_at=trigger_timestamp,
              expires_at=trigger_timestamp + timedelta(hours=SUGGESTION_EXPIRY_HOURS),
              status="active",
          )

          db.add(suggestion)
          await db.commit()
          await db.refresh(suggestion)

          # Publish to Redis for real-time frontend updates
          await self.redis.publish(
              RedisChannels.SUGGESTIONS_NEW,
              json.dumps({
                  "type": "trade_suggestion",
                  "suggestion_id": str(suggestion.suggestion_id),
                  "symbol": suggestion.symbol,
                  "trading_symbol": suggestion.trading_symbol,
                  "direction": suggestion.signal_direction,
                  "confidence_level": suggestion.confidence_level,
                  "consensus_score": float(suggestion.consensus_score),
                  "generated_at": suggestion.generated_at.isoformat(),
              })
          )

          await self._record_correlation(
              db, correlation_id, trigger_timestamp, suggestion.suggestion_id,
              None, latencies,
              scanner_output=scanner_signal,
              ai_output=ai_signal,
              ml_output=ml_signal,
          )

          return suggestion

      async def _record_correlation(
          self,
          db: AsyncSession,
          correlation_id: str,
          trigger_timestamp: datetime,
          suggestion_id,
          rejection_reason: str | None,
          latencies: dict | None = None,
          scanner_output: dict | None = None,
          ai_output: dict | None = None,
          ml_output: dict | None = None,
      ) -> None:
          """Persist correlation event for audit trail and latency monitoring."""
          correlation = EventCorrelation(
              correlation_id=uuid.UUID(correlation_id) if "-" in correlation_id else uuid.uuid4(),
              suggestion_id=suggestion_id,
              trigger_type="SCANNER_ANOMALY" if rejection_reason != "NEWS_EVENT" else "NEWS_EVENT",
              trigger_timestamp=trigger_timestamp,
              scanner_response_ms=latencies.get("scanner_ms") if latencies else None,
              ai_response_ms=latencies.get("ai_ms") if latencies else None,
              ml_response_ms=latencies.get("ml_ms") if latencies else None,
              total_latency_ms=latencies.get("total_ms") if latencies else None,
              consensus_reached=suggestion_id is not None,
              rejection_reason=rejection_reason,
              scanner_output=scanner_output,
              ai_output=ai_output,
              ml_output=ml_output,
          )
          db.add(correlation)
          await db.commit()

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Task 2.2: Create __init__.py for correlation package**

  File: backend/app/ai/correlation/__init__.py

  from app.ai.correlation.engine import EventCorrelationEngine

  __all__ = ["EventCorrelationEngine"]

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Phase 3: Worker Integration**

  **Task 3.1: Add Correlation Loop to Worker**

  File: backend/app/worker.py (modify existing)
  Objective: Register the correlation engine as an 8th background loop alongside existing 7 loops.

  Changes required:

  # 1. Add import at top of worker.py
  from app.ai.correlation.engine import EventCorrelationEngine
  from app.ai.fusion.models import AIEventClassification
  from sqlalchemy import select

  # 2. Add correlation_loop() function inside worker_lifespan context
  async def correlation_loop(
      session_factory,
      redis_client,
      ml_components: dict,
  ) -> None:
      """
      8th worker loop: monitors scanner anomalies and news events,
      runs bidirectional correlation, persists trade suggestions.

      Cadence: every 30 seconds during market hours, 5 minutes off-hours.
      Circuit-breaker protected per agent.
      """
      logger.info("Starting correlation loop...")

      async with session_factory() as session:
          from app.services.market_scanner import MarketScannerService
          from app.ai.fusion.signal_assembler import SignalAssembler
          from app.ml.inference.feature_loader import FeatureLoader

          scanner_svc = MarketScannerService(cache=redis_client)
          assembler = SignalAssembler(
              ensemble_predictor=ml_components.get("ensemble_predictor"),
              feature_loader=ml_components.get("feature_loader"),
          )
          engine = EventCorrelationEngine(
              scanner_service=scanner_svc,
              signal_assembler=assembler,
              ensemble_predictor=ml_components.get("ensemble_predictor"),
              redis=redis_client._client,
          )

          while not shutdown_event.is_set():
              try:
                  # ── Pathway 1: Scanner anomalies ──────────────────────────
                  scan_results = await scanner_svc.scan_all(session, timeframe="1d", force_refresh=True)

                  # Filter for high-conviction anomalies only
                  anomalies = [
                      r for r in scan_results
                      if abs(r.score) >= 5 and (r.volume_ratio or 0) >= 2.0
                  ]

                  for result in anomalies:
                      await engine.on_scanner_anomaly(session, result)

                  # ── Pathway 2: High-impact news events ────────────────────
                  cutoff = datetime.utcnow() - timedelta(minutes=5)
                  stmt = (
                      select(AIEventClassification)
                      .where(
                          AIEventClassification.impact_score >= 80,
                          AIEventClassification.created_at >= cutoff,
                      )
                  )
                  events = (await session.execute(stmt)).scalars().all()

                  for event in events:
                      await engine.on_news_event(session, event)

                  # ── Expire stale suggestions ──────────────────────────────
                  from sqlalchemy import update
                  from app.models.trade_suggestions import TradeSuggestion
                  await session.execute(
                      update(TradeSuggestion)
                      .where(
                          TradeSuggestion.status == "active",
                          TradeSuggestion.expires_at <= datetime.now(timezone.utc),
                      )
                      .values(status="expired")
                  )
                  await session.commit()

                  await asyncio.sleep(30)

              except Exception as exc:
                  logger.error("Correlation loop error: %s", exc, exc_info=True)
                  await asyncio.sleep(60)

  # 3. Add to main() task list alongside existing loops
  tasks = [
      asyncio.create_task(rss_ingestion_loop(...)),
      asyncio.create_task(event_processing_loop(...)),
      asyncio.create_task(regime_detection_loop(...)),
      asyncio.create_task(drift_detection_loop(...)),
      asyncio.create_task(safety_monitoring_loop(...)),
      asyncio.create_task(data_ingestion_loop(...)),
      asyncio.create_task(heartbeat_loop(...)),
      asyncio.create_task(correlation_loop(session_factory, redis_client, ml_components)),  # NEW
  ]

  Test Cases:
  - ✅ Loop starts without error on worker boot
  - ✅ Graceful shutdown on SIGTERM (respects shutdown_event)
  - ✅ Anomaly threshold filters correctly (score >= 5, volume_ratio >= 2.0)
  - ✅ Expired suggestions are marked correctly
  - ✅ Loop recovers from individual agent failures without crashing

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Phase 4: Backend API, Schemas, Router Registration**

  **Task 4.1: Pydantic Schemas**

  File: backend/app/schemas/trade_suggestions.py

  from __future__ import annotations
  from datetime import datetime
  from decimal import Decimal
  from uuid import UUID
  from pydantic import BaseModel, Field


  class TradeSuggestionResponse(BaseModel):
      suggestion_id: UUID
      symbol: str
      instrument_key: str
      trading_symbol: str | None

      consensus_score: float
      confidence_level: str          # HIGH | MEDIUM | LOW
      signal_direction: str          # BUY | SELL
      trigger_pathway: str           # TECHNICAL_FIRST | FUNDAMENTAL_FIRST

      # Contributing signals (summary only for list view)
      scanner_signal: dict
      ai_signal: dict
      ml_signal: dict

      # Trade parameters
      entry_price: float | None
      stop_loss: float | None
      risk_reward_ratio: float | None
      take_profit_1: float | None
      take_profit_2: float | None
      take_profit_3: float | None

      generated_at: datetime
      expires_at: datetime
      status: str

      model_config = {"from_attributes": True}


  class TradeSuggestionsListResponse(BaseModel):
      suggestions: list[TradeSuggestionResponse]
      total: int
      limit: int
      offset: int


  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Task 4.2: API Router**

  File: backend/app/api/v1/trade_suggestions.py

  """
  Trade Suggestions API
  =====================
  Authenticated, rate-limited endpoints for multi-agent validated trade suggestions.
  Queries are optimized against the TimescaleDB hypertable landing-page composite index.
  """
  from fastapi import APIRouter, Depends, HTTPException, Query, Request
  from sqlalchemy import func, select, desc, and_, update
  from sqlalchemy.ext.asyncio import AsyncSession

  from app.core.database import get_db
  from app.core.limiter import limiter
  from app.core.security import get_current_user_id
  from app.models.trade_suggestions import TradeSuggestion
  from app.schemas.trade_suggestions import TradeSuggestionResponse, TradeSuggestionsListResponse

  router = APIRouter(dependencies=[Depends(get_current_user_id)])


  @router.get("/suggestions", response_model=TradeSuggestionsListResponse)
  @limiter.limit("30/minute")
  async def get_trade_suggestions(
      request: Request,
      user_id: str = Depends(get_current_user_id),
      min_confidence: int = Query(60, ge=0, le=100),
      signal_direction: str | None = Query(None, pattern="^(BUY|SELL)$"),
      confidence_level: str | None = Query(None, pattern="^(HIGH|MEDIUM|LOW)$"),
      limit: int = Query(50, ge=1, le=100),
      offset: int = Query(0, ge=0),
      db: AsyncSession = Depends(get_db),
  ) -> TradeSuggestionsListResponse:
      """
      Get active trade suggestions sorted by consensus score descending.
      Uses composite index idx_suggestions_landing_page for sub-10ms queries.
      """
      filters = [
          TradeSuggestion.status == "active",
          TradeSuggestion.consensus_score >= min_confidence,
      ]
      if signal_direction:
          filters.append(TradeSuggestion.signal_direction == signal_direction)
      if confidence_level:
          filters.append(TradeSuggestion.confidence_level == confidence_level)

      base = select(TradeSuggestion).where(and_(*filters))
      total = await db.scalar(select(func.count()).select_from(base.subquery())) or 0

      rows = (
          await db.execute(
              base.order_by(desc(TradeSuggestion.consensus_score), desc(TradeSuggestion.generated_at))
              .offset(offset)
              .limit(limit)
          )
      ).scalars().all()

      return TradeSuggestionsListResponse(
          suggestions=[TradeSuggestionResponse.model_validate(r) for r in rows],
          total=total,
          limit=limit,
          offset=offset,
      )


  @router.get("/suggestions/{suggestion_id}", response_model=TradeSuggestionResponse)
  @limiter.limit("60/minute")
  async def get_suggestion_detail(
      request: Request,
      suggestion_id: str,
      user_id: str = Depends(get_current_user_id),
      db: AsyncSession = Depends(get_db),
  ) -> TradeSuggestionResponse:
      """Get full detail for a single suggestion by UUID."""
      row = (
          await db.execute(
              select(TradeSuggestion).where(TradeSuggestion.suggestion_id == suggestion_id)
          )
      ).scalar_one_or_none()

      if not row:
          raise HTTPException(status_code=404, detail="Suggestion not found")

      return TradeSuggestionResponse.model_validate(row)


  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Task 4.3: Register Router in main.py**

  File: backend/app/main.py (modify existing)

  # Add import
  from app.api.v1 import trade_suggestions

  # Add router registration alongside hawk_eye router
  app.include_router(
      trade_suggestions.router,
      prefix=f"{settings.API_V1_PREFIX}/hawk-eye",
      tags=["Trade Suggestions"],
  )

  # Final hawk-eye route set:
  # GET /api/v1/hawk-eye/scan              → existing multi-timeframe scan
  # GET /api/v1/hawk-eye/analyze           → existing single instrument analysis
  # GET /api/v1/hawk-eye/fundamentals      → existing fundamentals
  # GET /api/v1/hawk-eye/suggestions       → NEW trade suggestions list
  # GET /api/v1/hawk-eye/suggestions/{id}  → NEW suggestion detail

  Test Cases:
  - ✅ GET /suggestions returns 200 with correct schema
  - ✅ Filtering by direction, confidence_level, min_confidence works
  - ✅ Pagination (limit/offset) works correctly
  - ✅ GET /suggestions/{id} returns 404 for unknown UUID
  - ✅ Rate limiting enforced (30/min for list, 60/min for detail)
  - ✅ Unauthenticated requests return 401

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Phase 5: Frontend Implementation**

  Route structure after this phase:
    /hawk-eye-radar                          → Landing page (Trade Suggestion cards)
    /hawk-eye-radar/details/[symbol]         → Details page (Chart + Analysis)

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Task 5.1: TypeScript Types**

  File: frontend/src/types/trade-suggestions.ts

  export type SignalDirection = "BUY" | "SELL";
  export type ConfidenceLevel = "HIGH" | "MEDIUM" | "LOW";
  export type TriggerPathway = "TECHNICAL_FIRST" | "FUNDAMENTAL_FIRST";
  export type SuggestionStatus = "active" | "expired" | "executed" | "invalidated";

  export interface ScannerSignalSummary {
    direction: string;
    confidence: number;
    price_change_pct: number;
    volume_ratio: number;
    instrument_key: string;
    trading_symbol: string | null;
    signals: Array<{ name: string; value: number; direction: string }>;
  }

  export interface AISignalSummary {
    score: number;
    confidence: number;
    sentiment: "positive" | "negative";
    event_count: number;
    events: Array<{ id: number; type: string; impact: number }>;
  }

  export interface MLSignalSummary {
    score: number;
    confidence: number;
    model: string | null;
    prediction: {
      direction: "BUY" | "SELL" | "HOLD";
      entry_price: number | null;
      stop_loss: number | null;
      targets: (number | null)[];
      probabilities: { up: number; down: number; hold: number } | null;
    };
  }

  export interface TradeSuggestion {
    suggestion_id: string;
    symbol: string;
    instrument_key: string;
    trading_symbol: string | null;
    consensus_score: number;
    confidence_level: ConfidenceLevel;
    signal_direction: SignalDirection;
    trigger_pathway: TriggerPathway;
    scanner_signal: ScannerSignalSummary;
    ai_signal: AISignalSummary;
    ml_signal: MLSignalSummary;
    entry_price: number | null;
    stop_loss: number | null;
    risk_reward_ratio: number | null;
    take_profit_1: number | null;
    take_profit_2: number | null;
    take_profit_3: number | null;
    generated_at: string;
    expires_at: string;
    status: SuggestionStatus;
  }

  export interface TradeSuggestionsListResponse {
    suggestions: TradeSuggestion[];
    total: number;
    limit: number;
    offset: number;
  }

  export interface SuggestionFilters {
    min_confidence: number;
    signal_direction?: "BUY" | "SELL";
    confidence_level?: ConfidenceLevel;
  }

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Task 5.2: Landing Page**

  File: frontend/src/app/hawk-eye-radar/page.tsx
  Action: REPLACE current content entirely.

  'use client';

  import { useState } from 'react';
  import { useQuery } from '@tanstack/react-query';
  import { Radar } from 'lucide-react';
  import { api } from '@/lib/api';
  import { TradeSuggestionCard } from './components/TradeSuggestionCard';
  import { SuggestionFilters } from './components/SuggestionFilters';
  import { EmptyState } from './components/EmptyState';
  import { SuggestionCardSkeleton } from './components/SuggestionCardSkeleton';
  import type { TradeSuggestion, TradeSuggestionsListResponse, SuggestionFilters as Filters } from '@/types/trade-suggestions';

  export default function HawkEyeRadarPage() {
    const [filters, setFilters] = useState<Filters>({ min_confidence: 60 });

    const { data, isLoading, isError, refetch } = useQuery<TradeSuggestionsListResponse>({
      queryKey: ['trade-suggestions', filters],
      queryFn: async () => {
        const params: Record<string, string | number> = {
          min_confidence: filters.min_confidence,
          limit: 50,
        };
        if (filters.signal_direction) params.signal_direction = filters.signal_direction;
        if (filters.confidence_level) params.confidence_level = filters.confidence_level;
        const res = await api.get('/hawk-eye/suggestions', { params });
        return res.data;
      },
      refetchInterval: 30_000,
      staleTime: 20_000,
    });

    const suggestions: TradeSuggestion[] = data?.suggestions ?? [];

    return (
      <div className="space-y-6">
        <div className="flex items-center gap-3">
          <Radar className="h-7 w-7 text-blue-600" />
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Hawk-Eye Radar</h1>
            <p className="text-sm text-muted-foreground">
              High-conviction trade suggestions validated across technical, news, and ML signals
            </p>
          </div>
        </div>

        <SuggestionFilters filters={filters} onChange={setFilters} total={data?.total ?? 0} />

        {isLoading && (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => <SuggestionCardSkeleton key={i} />)}
          </div>
        )}

        {isError && (
          <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            Failed to load suggestions.{' '}
            <button onClick={() => refetch()} className="underline">Retry</button>
          </div>
        )}

        {!isLoading && !isError && suggestions.length === 0 && (
          <EmptyState filters={filters} />
        )}

        {!isLoading && suggestions.length > 0 && (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {suggestions.map((s) => (
              <TradeSuggestionCard key={s.suggestion_id} suggestion={s} />
            ))}
          </div>
        )}
      </div>
    );
  }

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Task 5.3: Trade Suggestion Card Component**

  File: frontend/src/app/hawk-eye-radar/components/TradeSuggestionCard.tsx

  'use client';

  import { useRouter } from 'next/navigation';
  import { TrendingUp, TrendingDown, Target, Shield, Zap, Newspaper } from 'lucide-react';
  import { Card, CardContent, CardHeader } from '@/components/ui/card';
  import { Badge } from '@/components/ui/badge';
  import { Button } from '@/components/ui/button';
  import type { TradeSuggestion } from '@/types/trade-suggestions';

  const CONFIDENCE_COLORS: Record<string, string> = {
    HIGH: 'bg-emerald-500',
    MEDIUM: 'bg-amber-500',
    LOW: 'bg-slate-400',
  };

  const CONFIDENCE_BADGE: Record<string, string> = {
    HIGH: 'bg-emerald-100 text-emerald-800 border-emerald-200',
    MEDIUM: 'bg-amber-100 text-amber-800 border-amber-200',
    LOW: 'bg-slate-100 text-slate-700 border-slate-200',
  };

  export function TradeSuggestionCard({ suggestion }: { suggestion: TradeSuggestion }) {
    const router = useRouter();
    const isBuy = suggestion.signal_direction === 'BUY';
    const encodedKey = encodeURIComponent(suggestion.instrument_key);

    return (
      <Card className="group flex flex-col border-slate-200/80 bg-white/90 transition-shadow hover:shadow-lg">
        <CardHeader className="pb-3">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <span className="truncate text-lg font-bold text-slate-900">
                  {suggestion.trading_symbol ?? suggestion.symbol}
                </span>
                <Badge className={isBuy ? 'bg-emerald-600 text-white' : 'bg-red-600 text-white'}>
                  {suggestion.signal_direction}
                </Badge>
              </div>
              <p className="mt-0.5 truncate text-xs text-slate-400">{suggestion.instrument_key}</p>
            </div>
            {isBuy
              ? <TrendingUp className="h-5 w-5 shrink-0 text-emerald-600" />
              : <TrendingDown className="h-5 w-5 shrink-0 text-red-600" />
            }
          </div>
        </CardHeader>

        <CardContent className="flex flex-1 flex-col gap-4">
          {/* Consensus score bar */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between text-xs">
              <span className="font-medium text-slate-600">Consensus</span>
              <div className="flex items-center gap-1.5">
                <span className="font-bold text-slate-900">{suggestion.consensus_score}%</span>
                <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${CONFIDENCE_BADGE[suggestion.confidence_level]}`}>
                  {suggestion.confidence_level}
                </span>
              </div>
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
              <div
                className={`h-full rounded-full transition-all ${CONFIDENCE_COLORS[suggestion.confidence_level]}`}
                style={{ width: `${suggestion.consensus_score}%` }}
              />
            </div>
          </div>

          {/* Agent validation badges */}
          <div className="flex flex-wrap gap-1.5">
            <span className="flex items-center gap-1 rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-semibold text-blue-700">
              <Target className="h-3 w-3" /> Technical
            </span>
            <span className="flex items-center gap-1 rounded-full bg-purple-50 px-2 py-0.5 text-[10px] font-semibold text-purple-700">
              <Newspaper className="h-3 w-3" />
              {suggestion.ai_signal.event_count} News Event{suggestion.ai_signal.event_count !== 1 ? 's' : ''}
            </span>
            <span className="flex items-center gap-1 rounded-full bg-orange-50 px-2 py-0.5 text-[10px] font-semibold text-orange-700">
              <Zap className="h-3 w-3" /> ML {(suggestion.ml_signal.confidence * 100).toFixed(0)}%
            </span>
          </div>

          {/* Trade parameters */}
          {suggestion.entry_price && (
            <div className="grid grid-cols-2 gap-2 rounded-lg bg-slate-50 p-3 text-xs">
              <div>
                <p className="text-slate-500">Entry</p>
                <p className="font-semibold text-slate-900">₹{suggestion.entry_price.toFixed(2)}</p>
              </div>
              <div>
                <p className="text-slate-500">Stop Loss</p>
                <p className="font-semibold text-red-600">₹{suggestion.stop_loss?.toFixed(2) ?? '—'}</p>
              </div>
              {suggestion.take_profit_1 && (
                <div>
                  <p className="text-slate-500">Target 1</p>
                  <p className="font-semibold text-emerald-600">₹{suggestion.take_profit_1.toFixed(2)}</p>
                </div>
              )}
              {suggestion.risk_reward_ratio && (
                <div>
                  <p className="text-slate-500">R:R</p>
                  <p className="font-semibold text-slate-900">1:{suggestion.risk_reward_ratio.toFixed(1)}</p>
                </div>
              )}
            </div>
          )}

          {/* Trigger pathway */}
          <p className="text-[10px] text-slate-400">
            Triggered by: {suggestion.trigger_pathway === 'TECHNICAL_FIRST' ? 'Technical anomaly → News confirmed' : 'News event → Technical confirmed'}
          </p>

          {/* CTA */}
          <Button
            size="sm"
            variant="outline"
            className="mt-auto w-full"
            onClick={() => router.push(`/hawk-eye-radar/details/${encodedKey}`)}
          >
            View Details
          </Button>
        </CardContent>
      </Card>
    );
  }

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Task 5.4: Filters Component**

  File: frontend/src/app/hawk-eye-radar/components/SuggestionFilters.tsx

  'use client';

  import type { SuggestionFilters } from '@/types/trade-suggestions';

  interface Props {
    filters: SuggestionFilters;
    onChange: (f: SuggestionFilters) => void;
    total: number;
  }

  export function SuggestionFilters({ filters, onChange, total }: Props) {
    return (
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-xs font-semibold uppercase tracking-widest text-slate-400">Filter</span>

        {/* Direction */}
        {(['BUY', 'SELL', undefined] as const).map((dir) => (
          <button
            key={dir ?? 'ALL'}
            type="button"
            onClick={() => onChange({ ...filters, signal_direction: dir })}
            className={`rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-wide transition ${
              filters.signal_direction === dir
                ? dir === 'BUY' ? 'bg-emerald-600 text-white'
                  : dir === 'SELL' ? 'bg-red-600 text-white'
                  : 'bg-slate-900 text-white'
                : 'border border-slate-200 bg-white text-slate-600 hover:border-slate-300'
            }`}
          >
            {dir ?? 'All'}
          </button>
        ))}

        <div className="h-4 w-px bg-slate-200" />

        {/* Confidence level */}
        {(['HIGH', 'MEDIUM', undefined] as const).map((lvl) => (
          <button
            key={lvl ?? 'ALL_CONF'}
            type="button"
            onClick={() => onChange({ ...filters, confidence_level: lvl })}
            className={`rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-wide transition ${
              filters.confidence_level === lvl
                ? 'bg-slate-900 text-white'
                : 'border border-slate-200 bg-white text-slate-600 hover:border-slate-300'
            }`}
          >
            {lvl ?? 'Any Confidence'}
          </button>
        ))}

        <div className="h-4 w-px bg-slate-200" />

        {/* Min confidence slider */}
        <div className="flex items-center gap-2">
          <label className="text-xs text-slate-500">Min Score</label>
          <input
            type="range"
            min={0}
            max={100}
            step={5}
            value={filters.min_confidence}
            onChange={(e) => onChange({ ...filters, min_confidence: Number(e.target.value) })}
            className="w-24 accent-blue-600"
          />
          <span className="w-8 text-xs font-semibold text-slate-700">{filters.min_confidence}%</span>
        </div>

        <div className="ml-auto text-xs text-slate-400">{total} suggestion{total !== 1 ? 's' : ''}</div>
      </div>
    );
  }

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Task 5.5: Empty State Component**

  File: frontend/src/app/hawk-eye-radar/components/EmptyState.tsx

  import { Radar } from 'lucide-react';
  import type { SuggestionFilters } from '@/types/trade-suggestions';

  export function EmptyState({ filters }: { filters: SuggestionFilters }) {
    return (
      <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-slate-200 bg-slate-50/50 py-20 text-center">
        <Radar className="mb-4 h-10 w-10 text-slate-300" />
        <h3 className="text-base font-semibold text-slate-700">No high-conviction trades today</h3>
        <p className="mt-1 max-w-sm text-sm text-slate-400">
          The system requires all three agents (Technical, News, ML) to agree before surfacing a suggestion.
          {filters.min_confidence > 60 && ' Try lowering the minimum confidence score.'}
        </p>
        <div className="mt-6 grid grid-cols-3 gap-4 text-xs text-slate-500">
          <div className="rounded-lg border border-slate-200 bg-white p-3">
            <p className="font-semibold text-slate-700">Market Scanner</p>
            <p className="mt-1">Monitoring all NSE equities for price and volume anomalies</p>
          </div>
          <div className="rounded-lg border border-slate-200 bg-white p-3">
            <p className="font-semibold text-slate-700">AI Intelligence</p>
            <p className="mt-1">Scanning RSS feeds for high-impact company news</p>
          </div>
          <div className="rounded-lg border border-slate-200 bg-white p-3">
            <p className="font-semibold text-slate-700">ML Predictor</p>
            <p className="mt-1">Validating signals with ensemble model predictions</p>
          </div>
        </div>
      </div>
    );
  }

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Task 5.6: Skeleton Loader**

  File: frontend/src/app/hawk-eye-radar/components/SuggestionCardSkeleton.tsx

  export function SuggestionCardSkeleton() {
    return (
      <div className="animate-pulse rounded-xl border border-slate-200 bg-white p-5 space-y-4">
        <div className="flex justify-between">
          <div className="space-y-2">
            <div className="h-5 w-28 rounded bg-slate-200" />
            <div className="h-3 w-40 rounded bg-slate-100" />
          </div>
          <div className="h-5 w-5 rounded bg-slate-200" />
        </div>
        <div className="space-y-1.5">
          <div className="h-3 w-full rounded bg-slate-100" />
          <div className="h-1.5 w-full rounded-full bg-slate-100" />
        </div>
        <div className="flex gap-2">
          <div className="h-5 w-20 rounded-full bg-slate-100" />
          <div className="h-5 w-24 rounded-full bg-slate-100" />
          <div className="h-5 w-16 rounded-full bg-slate-100" />
        </div>
        <div className="grid grid-cols-2 gap-2 rounded-lg bg-slate-50 p-3">
          <div className="h-8 rounded bg-slate-100" />
          <div className="h-8 rounded bg-slate-100" />
        </div>
        <div className="h-8 w-full rounded-lg bg-slate-100" />
      </div>
    );
  }

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Task 5.7: Details Page (Move existing chart page)**

  File: frontend/src/app/hawk-eye-radar/details/[symbol]/page.tsx
  Action: CREATE new file. Move current page.tsx content here with one change:
          - Pre-select the instrument from the URL param on mount
          - Chart renders as a full-size floating pane (fixed overlay, not inline)

  Key changes from current page.tsx:
  1. Read `params.symbol` → decode → pre-populate `selected` instrument state
  2. Chart container: change from inline `h-[460px]` div to a full-screen fixed overlay
     triggered by a "Expand Chart" button, closeable with Escape or X button
  3. Add suggestion detail panel above the chart section showing the suggestion
     that was clicked (fetched via suggestion_id query param if present)
  4. Keep all existing candlestick, live tick, and analysis card functionality intact

  URL pattern: /hawk-eye-radar/details/NSE_EQ%7CINE002A01018
  Query param: ?suggestion_id=<uuid> (optional, for showing suggestion context)

  Floating chart pane implementation:
  - Triggered by "Expand Chart" button
  - Fixed overlay: fixed inset-0 z-50 bg-slate-950/95
  - Chart fills available space: h-[calc(100vh-80px)]
  - Close: Escape key or X button top-right
  - Smooth open/close transition with CSS opacity + scale

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Phase 6: Redis Channel Additions**

  **Task 6.1: Add new channels to RedisChannels**

  File: backend/app/core/redis.py (modify existing RedisChannels class)

  class RedisChannels:
      # ... existing channels unchanged ...

      # NEW: Trade suggestion channels
      SUGGESTIONS_NEW = "cai:suggestions:new"
      SUGGESTIONS_EXPIRED = "cai:suggestions:expired"

      # NEW: Correlation engine streams (Redis Streams, not pub/sub)
      STREAM_SCANNER_ANOMALIES = "cai:stream:scanner_anomalies"
      STREAM_NEWS_EVENTS = "cai:stream:news_events"

  Notes:
  - SUGGESTIONS_NEW: pub/sub channel, published when a new suggestion is persisted.
    Frontend can subscribe via WebSocket to receive real-time card additions.
  - SUGGESTIONS_EXPIRED: pub/sub channel, published when suggestions are expired.
    Frontend can subscribe to remove stale cards without polling.
  - STREAM_SCANNER_ANOMALIES: Redis Stream (xadd/xread), durable log of scanner
    anomalies for audit and replay. Max length: 10,000 entries (MAXLEN ~).
  - STREAM_NEWS_EVENTS: Redis Stream, durable log of high-impact news events
    that triggered Pathway 2 correlations.

  Stream configuration (set on first xadd):
    MAXLEN ~ 10000  (approximate trimming, O(1) performance)
    TTL: none (streams are trimmed by MAXLEN, not TTL)

  Cache keys added:
    cai:suggestions:active_count   → integer, updated on each suggestion write/expire
    cai:suggestions:last_updated   → ISO timestamp, used by frontend for stale detection

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Phase 7: Testing Strategy**

  **Task 7.1: Unit Tests**

  File: backend/tests/unit/test_correlation_engine.py

  Tests:
  - test_consensus_all_buy_high_confidence
      All three agents return BUY with high confidence → suggestion created, confidence_level=HIGH
  - test_consensus_all_sell_high_confidence
      All three agents return SELL with high confidence → suggestion created, confidence_level=HIGH
  - test_consensus_direction_mismatch_discarded
      Scanner=BUY, AI=SELL, ML=BUY → None returned, correlation recorded with DIRECTION_MISMATCH
  - test_consensus_ml_neutral_discarded
      ML returns HOLD → None returned, correlation recorded with ML_NEUTRAL
  - test_consensus_low_confidence_discarded
      All agents agree but weighted score < 60 → None returned, LOW_CONFIDENCE recorded
  - test_consensus_medium_confidence_threshold
      Weighted score between 60-79 → suggestion created with confidence_level=MEDIUM
  - test_risk_reward_calculation
      entry=100, stop_loss=95, target=115 → risk_reward_ratio=3.0
  - test_circuit_breaker_opens_after_threshold
      5 consecutive failures → circuit breaker state=open
  - test_circuit_breaker_half_open_after_timeout
      Circuit open, 60s elapsed → state transitions to half_open
  - test_suggestion_expiry_timestamp
      generated_at + 24h = expires_at

  File: backend/tests/unit/test_trade_suggestions_schema.py

  Tests:
  - test_response_serialization_from_orm
  - test_list_response_pagination_fields
  - test_filter_validation_invalid_direction (422 expected)

  **Task 7.2: Integration Tests**

  File: backend/tests/integration/test_correlation_engine_integration.py

  Tests:
  - test_pathway1_end_to_end
      Mock scanner result + mock AI events + mock ML prediction
      → suggestion persisted in DB with correct fields
  - test_pathway2_end_to_end
      Mock AIEventClassification + mock scanner + mock ML
      → suggestion persisted for each affected symbol
  - test_timeout_handled_gracefully
      AI agent sleeps 6s (> 5s timeout) → None returned, TIMEOUT recorded
  - test_duplicate_suggestion_not_created
      Same symbol triggered twice within 5 min → deduplication logic prevents duplicate

  File: backend/tests/integration/test_trade_suggestions_api.py

  Tests:
  - test_get_suggestions_empty (no active suggestions → empty list, 200)
  - test_get_suggestions_with_data (seeded suggestions → correct count and order)
  - test_filter_by_direction
  - test_filter_by_confidence_level
  - test_filter_by_min_confidence
  - test_get_suggestion_detail_found
  - test_get_suggestion_detail_not_found (404)
  - test_unauthenticated_returns_401
  - test_rate_limit_enforced (31 requests → 429)

  **Task 7.3: Frontend Tests**

  File: frontend/src/app/hawk-eye-radar/components/__tests__/TradeSuggestionCard.test.tsx

  Tests:
  - renders BUY card with correct colors and badges
  - renders SELL card with correct colors and badges
  - renders entry/stop/target prices when present
  - renders "—" when trade params absent
  - clicking "View Details" navigates to correct route
  - HIGH/MEDIUM confidence badge renders correct color class

  File: frontend/src/app/hawk-eye-radar/components/__tests__/SuggestionFilters.test.tsx

  Tests:
  - clicking BUY filter calls onChange with signal_direction=BUY
  - clicking All clears signal_direction filter
  - slider change updates min_confidence
  - total count displays correctly

  **Task 7.4: Performance Tests**

  File: backend/tests/performance/test_suggestions_latency.py

  Benchmarks:
  - Landing page query (50 suggestions, all filters): target <10ms p95
  - Suggestion detail query: target <5ms p95
  - Consensus computation (no DB): target <1ms
  - Full Pathway 1 end-to-end (with mocked agents): target <200ms p95

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Phase 8: Delivery Checklist & Complete File Map**

  **Complete File Map (all files touched or created)**

  NEW FILES:
    backend/migrations/0009_trade_suggestions.sql
    backend/app/models/trade_suggestions.py
    backend/app/ai/correlation/__init__.py
    backend/app/ai/correlation/engine.py
    backend/app/schemas/trade_suggestions.py
    backend/app/api/v1/trade_suggestions.py
    backend/tests/unit/test_correlation_engine.py
    backend/tests/unit/test_trade_suggestions_schema.py
    backend/tests/integration/test_correlation_engine_integration.py
    backend/tests/integration/test_trade_suggestions_api.py
    backend/tests/performance/test_suggestions_latency.py
    frontend/src/types/trade-suggestions.ts
    frontend/src/app/hawk-eye-radar/details/[symbol]/page.tsx
    frontend/src/app/hawk-eye-radar/components/TradeSuggestionCard.tsx
    frontend/src/app/hawk-eye-radar/components/SuggestionFilters.tsx
    frontend/src/app/hawk-eye-radar/components/EmptyState.tsx
    frontend/src/app/hawk-eye-radar/components/SuggestionCardSkeleton.tsx

  MODIFIED FILES:
    backend/app/core/redis.py              (add 4 new channel constants)
    backend/app/models/__init__.py         (register TradeSuggestion, EventCorrelation)
    backend/app/api/v1/__init__.py         (export trade_suggestions router)
    backend/app/main.py                    (register trade_suggestions router)
    backend/app/worker.py                  (add correlation_loop as 8th task)
    frontend/src/app/hawk-eye-radar/page.tsx  (replace with landing page)

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Pre-Implementation Checklist**

  Database:
  [ ] TimescaleDB extension enabled (SELECT extname FROM pg_extension WHERE extname='timescaledb')
  [ ] Migration 0009 applied and hypertables verified
  [ ] Indexes created and EXPLAIN ANALYZE confirms index usage
  [ ] Retention policy active (SELECT * FROM timescaledb_information.jobs)

  Backend:
  [ ] EventCorrelationEngine unit tests pass
  [ ] API endpoints return correct schemas
  [ ] Worker starts with 8 loops, no import errors
  [ ] Redis channels publish/subscribe verified
  [ ] Rate limiting enforced on all new endpoints
  [ ] All new endpoints require authentication

  Frontend:
  [ ] Landing page renders suggestion cards from API
  [ ] Filters update query params and refetch
  [ ] Empty state renders when no suggestions
  [ ] Skeleton renders during loading
  [ ] "View Details" navigates to correct route
  [ ] Details page pre-selects instrument from URL
  [ ] Floating chart pane opens/closes correctly
  [ ] Escape key closes floating chart pane
  [ ] 30s polling refetch works without flicker

  Performance:
  [ ] Landing page query <10ms (EXPLAIN ANALYZE)
  [ ] Consensus computation <1ms
  [ ] Full pathway end-to-end <200ms (mocked agents)
  [ ] Frontend LCP <1.5s on landing page

  Security:
  [ ] All new API endpoints require valid JWT
  [ ] suggestion_id is UUID (not sequential int) — prevents enumeration
  [ ] JSONB fields are read-only from API (no user input written to DB)
  [ ] Rate limits prevent abuse of suggestion generation

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  **Implementation Order (strict sequence)**

  1. Migration 0009 → run against DB
  2. ORM models (trade_suggestions.py)
  3. Update models/__init__.py
  4. Correlation engine (engine.py + __init__.py)
  5. Unit tests for engine → must pass before proceeding
  6. Schemas (trade_suggestions.py)
  7. API router (api/v1/trade_suggestions.py)
  8. Register router in main.py
  9. API integration tests → must pass
  10. Redis channel additions (redis.py)
  11. Worker integration (worker.py)
  12. Frontend types (trade-suggestions.ts)
  13. Frontend components (Card, Filters, EmptyState, Skeleton)
  14. Frontend landing page (page.tsx replacement)
  15. Frontend details page (details/[symbol]/page.tsx)
  16. Frontend component tests
  17. Performance benchmarks
  18. Full end-to-end smoke test

  ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  Status: PLAN COMPLETE — Ready for implementation
  Last updated: April 21, 2026
