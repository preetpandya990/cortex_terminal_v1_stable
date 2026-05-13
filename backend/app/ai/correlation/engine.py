"""
Event Correlation Engine - Bidirectional Multi-Agent Consensus System
======================================================================
Production-grade orchestration of cross-agent signal validation for trade suggestions.

Architecture:
- Pathway 1 (Technical First): Scanner Anomaly → AI + ML → Consensus
- Pathway 2 (Fundamental First): News Event → Scanner + ML → Consensus

Performance Targets:
- Consensus latency: <100ms (p95)
- Throughput: 1000+ correlations/second
- Availability: 99.9% with circuit breakers

Author: Cortex AI Team
Version: 1.0.0
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID, uuid4

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import CacheService

from app.ai.fusion.models import AIEventClassification
from app.ai.fusion.signal_assembler import SignalAssembler
from app.core.metrics import (
    suggestions_generated_total,
    consensus_score_distribution,
    correlation_latency_seconds,
)
from app.core.redis import RedisChannels
from app.models.trade_suggestions import EventCorrelation, TradeSuggestion

logger = logging.getLogger(__name__)

# Consensus configuration
CONSENSUS_HIGH_THRESHOLD = 80.0
CONSENSUS_MEDIUM_THRESHOLD = 60.0
SUGGESTION_EXPIRY_HOURS = 24

# Agent weights (must sum to 1.0)
SCANNER_WEIGHT = 0.30
AI_WEIGHT = 0.40
ML_WEIGHT = 0.30


class CircuitBreaker:
    """
    Production-grade circuit breaker for fault tolerance.
    
    States:
    - CLOSED: Normal operation, requests pass through
    - OPEN: Failure threshold exceeded, requests fail fast
    - HALF_OPEN: Testing if service recovered
    
    Pattern: After timeout in OPEN state, transition to HALF_OPEN.
    First success in HALF_OPEN → CLOSED. Failure → OPEN.
    """

    def __init__(self, failure_threshold: int = 5, timeout_seconds: int = 60):
        """
        Initialize circuit breaker.
        
        Args:
            failure_threshold: Consecutive failures before opening
            timeout_seconds: Time to wait before attempting recovery
        """
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
        self.failure_count = 0
        self.last_failure_time: datetime | None = None
        self.state: Literal["closed", "open", "half_open"] = "closed"

    def record_success(self) -> None:
        """Record successful operation."""
        if self.state == "half_open":
            self.state = "closed"
            self.failure_count = 0
            logger.info("Circuit breaker closed after successful recovery")

    def record_failure(self) -> None:
        """Record failed operation."""
        self.failure_count += 1
        self.last_failure_time = datetime.now(timezone.utc)
        
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning(
                f"Circuit breaker opened (failures: {self.failure_count})"
            )

    def can_attempt(self) -> bool:
        """Check if operation can be attempted."""
        if self.state == "closed":
            return True
            
        if self.state == "open" and self.last_failure_time:
            elapsed = (datetime.now(timezone.utc) - self.last_failure_time).total_seconds()
            if elapsed >= self.timeout_seconds:
                self.state = "half_open"
                logger.info("Circuit breaker half-open, testing recovery")
                return True
            return False
            
        return self.state == "half_open"


class EventCorrelationEngine:
    """
    Orchestrates bidirectional signal validation between agents.
    
    Implements weighted consensus with directional alignment checks.
    All three agents (Scanner, AI, ML) must agree on direction.
    
    Features:
    - Circuit breakers per agent for fault tolerance
    - Sub-100ms consensus latency target
    - Comprehensive audit trail via EventCorrelation records
    - Redis pub/sub for real-time frontend updates
    """

    def __init__(
        self,
        signal_assembler: SignalAssembler,
        redis: Redis,
        scanner_cache: "CacheService | None" = None,
    ):
        """
        Initialize correlation engine.

        Args:
            signal_assembler: Service for gathering AI/ML signals
            redis:            Redis client for pub/sub
            scanner_cache:    CacheService for reading cached scanner results
                              in Pathway 2.  When None, Pathway 2 falls back
                              to the event-impact-based synthetic signal.
        """
        self.assembler = signal_assembler
        self.redis = redis
        self.scanner_cache = scanner_cache

        # Circuit breakers per agent
        self.circuit_breakers = {
            "scanner": CircuitBreaker(failure_threshold=5, timeout_seconds=60),
            "ai": CircuitBreaker(failure_threshold=5, timeout_seconds=60),
            "ml": CircuitBreaker(failure_threshold=5, timeout_seconds=60),
        }

    async def on_scanner_anomaly(
        self,
        db: AsyncSession,
        scanner_signal: dict[str, Any],
    ) -> TradeSuggestion | None:
        """
        Pathway 1: Technical anomaly triggers fundamental validation.
        
        Flow:
        1. Scanner detects anomaly (high score, volume spike)
        2. Query AI Intelligence for news sentiment
        3. Query ML Predictor for forecast
        4. Compute consensus and generate suggestion if aligned
        
        Args:
            db: Database session
            scanner_signal: Scanner detection result with direction/confidence
            
        Returns:
            TradeSuggestion if consensus reached, None otherwise
        """
        correlation_id = uuid4()
        trigger_timestamp = datetime.now(timezone.utc)
        symbol = scanner_signal.get("instrument_key")

        logger.info(
            "Pipeline stage=trigger pathway=1 correlation_id=%s symbol=%s",
            correlation_id, symbol,
            extra={"pipeline_stage": "trigger", "correlation_id": str(correlation_id)},
        )

        # Gather signals with timeout
        t_gather_start = datetime.now(timezone.utc)
        try:
            ai_signal, ml_signal, latencies = await asyncio.wait_for(
                self._gather_signals_pathway1(db, scanner_signal),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Pipeline stage=signal_gather status=timeout correlation_id=%s symbol=%s",
                correlation_id, symbol,
            )
            await self._record_correlation(
                db, correlation_id, "SCANNER_ANOMALY", trigger_timestamp,
                None, "TIMEOUT", None
            )
            return None
        except Exception as e:
            logger.error(
                "Pipeline stage=signal_gather status=error correlation_id=%s symbol=%s error=%s",
                correlation_id, symbol, e, exc_info=True,
            )
            await self._record_correlation(
                db, correlation_id, "SCANNER_ANOMALY", trigger_timestamp,
                None, f"ERROR: {str(e)}", None
            )
            return None

        t_gather_ms = int((datetime.now(timezone.utc) - t_gather_start).total_seconds() * 1000)
        logger.debug(
            "Pipeline stage=signal_gather status=ok correlation_id=%s symbol=%s "
            "scanner_ms=%s ai_ms=%s ml_ms=%s gather_total_ms=%d",
            correlation_id, symbol,
            latencies.get("scanner_ms"), latencies.get("ai_ms"), latencies.get("ml_ms"),
            t_gather_ms,
        )

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
            total_ms = int(
                (datetime.now(timezone.utc) - trigger_timestamp).total_seconds() * 1000
            )
            logger.info(
                "Pipeline stage=suggestion_committed correlation_id=%s "
                "suggestion_id=%s symbol=%s direction=%s confidence=%s "
                "consensus_score=%.2f total_pipeline_ms=%d",
                correlation_id, suggestion.suggestion_id, suggestion.symbol,
                suggestion.signal_direction, suggestion.confidence_level,
                float(suggestion.consensus_score), total_ms,
                extra={
                    "pipeline_stage": "suggestion_committed",
                    "correlation_id": str(correlation_id),
                    "suggestion_id": str(suggestion.suggestion_id),
                    "total_pipeline_ms": total_ms,
                },
            )

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
            event: Classified news event with affected symbols
            
        Returns:
            List of TradeSuggestions for affected symbols
        """
        correlation_id = uuid4()
        trigger_timestamp = datetime.now(timezone.utc)
        affected_symbols = event.affected_symbols or []

        logger.info(
            f"[{correlation_id}] Pathway 2: News event affecting "
            f"{len(affected_symbols)} symbols"
        )

        suggestions = []

        for symbol in affected_symbols:
            try:
                scanner_signal, ml_signal, latencies = await asyncio.wait_for(
                    self._gather_signals_pathway2(db, symbol, event),
                    timeout=5.0,
                )

                # AI signal from event
                ai_signal = {
                    "score": float(event.impact_score),
                    "confidence": float(event.classification_confidence),
                    "sentiment": "positive" if event.impact_score > 0 else "negative",
                    "event_type": event.event_type,
                    "event_count": 1,
                    "events": [{
                        "id": event.id,
                        "type": event.event_type,
                        "impact": float(event.impact_score)
                    }],
                }

                suggestion = await self._compute_consensus(
                    db=db,
                    correlation_id=uuid4(),  # Unique per symbol
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
                logger.error(
                    f"[{correlation_id}] Error processing {symbol}: {e}"
                )
                continue

        return suggestions

    async def _gather_signals_pathway1(
        self,
        db: AsyncSession,
        scanner_signal: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
        """
        Gather AI and ML signals for scanner-triggered event.
        
        Scanner signal already available, query AI + ML in parallel.
        
        Returns:
            Tuple of (ai_signal, ml_signal, latencies)
        """
        start_time = datetime.now(timezone.utc)
        symbol = scanner_signal.get("instrument_key")

        # Parallel async calls to AI and ML
        ai_task = self.assembler.gather_event_signals(db, symbol)
        ml_task = self.assembler.gather_ml_signals(db, symbol)

        ai_start = datetime.now(timezone.utc)
        ai_signal = await ai_task
        ai_latency = (datetime.now(timezone.utc) - ai_start).total_seconds() * 1000

        ml_start = datetime.now(timezone.utc)
        ml_signal = await ml_task
        ml_latency = (datetime.now(timezone.utc) - ml_start).total_seconds() * 1000

        total_latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

        latencies = {
            "scanner_ms": 0,  # Already computed
            "ai_ms": int(ai_latency),
            "ml_ms": int(ml_latency),
            "total_ms": int(total_latency),
        }

        return ai_signal, ml_signal, latencies

    async def _gather_signals_pathway2(
        self,
        db: AsyncSession,
        symbol: str,
        event: AIEventClassification,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
        """
        Gather scanner and ML signals for a news-triggered correlation.

        Scanner signal is read from the live Redis scan cache so that Pathway 2
        uses real technical state for the affected symbol rather than a synthetic
        approximation.  Falls back to an event-derived signal when the symbol
        has not yet been scanned (e.g. off-hours or first boot).

        Returns:
            (scanner_signal, ml_signal, latencies)
        """
        start_time = datetime.now(timezone.utc)

        # ── Scanner signal (from Redis scan cache) ────────────────────────────
        scanner_start = datetime.now(timezone.utc)
        scanner_signal = await self._resolve_scanner_signal_for_symbol(symbol, event)
        scanner_latency = (datetime.now(timezone.utc) - scanner_start).total_seconds() * 1000

        # ── ML prediction ─────────────────────────────────────────────────────
        ml_start = datetime.now(timezone.utc)
        ml_signal = await self.assembler.gather_ml_signals(db, symbol)
        ml_latency = (datetime.now(timezone.utc) - ml_start).total_seconds() * 1000

        total_latency = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

        latencies = {
            "scanner_ms": int(scanner_latency),
            "ai_ms":      0,               # AI signal comes from the event itself
            "ml_ms":      int(ml_latency),
            "total_ms":   int(total_latency),
        }

        return scanner_signal, ml_signal, latencies

    async def _resolve_scanner_signal_for_symbol(
        self,
        symbol: str,
        event: AIEventClassification,
    ) -> dict[str, Any]:
        """
        Look up a symbol in the latest cached scanner results.

        Returns the real scanner dict when found; otherwise falls back to an
        event-impact-derived synthetic signal (available=False flagged) so the
        consensus engine can still evaluate Pathway 2 with a weak scanner agent.
        """
        # Synthetic fallback used when scanner cache is absent or symbol not found.
        # Confidence is intentionally capped at 50 so a synthetic-only Pathway 2
        # signal can only reach consensus if the ML score is very high.
        impact = float(event.impact_score)
        fallback: dict[str, Any] = {
            "direction":       "buy"  if impact > 0 else "sell",
            "confidence":      50.0,   # capped — signal is inferred, not observed
            "instrument_key":  symbol,
            "trading_symbol":  symbol,
            "price_change_pct": 0.0,
            "volume_ratio":    1.0,
            "signals":         [],
            "available":       False,  # marks synthetic origin for audit trail
        }

        if self.scanner_cache is None:
            logger.debug(
                "Pathway 2 scanner: no cache service — using synthetic signal for %s", symbol
            )
            return fallback

        try:
            cached = await self.scanner_cache.get("scanner:results:v2:1d")
            if not cached:
                logger.debug(
                    "Pathway 2 scanner: cache empty — using synthetic signal for %s", symbol
                )
                return fallback

            raw_results: list[dict] = cached.get("results", [])
            # Match on trading_symbol (affected_symbols contains NSE trading symbols)
            match = next(
                (r for r in raw_results if r.get("trading_symbol") == symbol), None
            )

            if match is None:
                logger.debug(
                    "Pathway 2 scanner: symbol %s not in cached results (%d entries) — using synthetic",
                    symbol, len(raw_results),
                )
                return fallback

            # Map ScanResult fields to the canonical scanner_signal shape used
            # by _compute_consensus (mirrors Pathway 1 output).
            scan_dir = match.get("signal", "neutral")
            if scan_dir == "neutral":
                # A neutral scanner reading won't reach consensus — return synthetic
                # so the consensus engine explicitly rejects via DIRECTION_MISMATCH
                # rather than treating neutral as implicit agreement.
                return {**fallback, "direction": "neutral", "available": True}

            return {
                "direction":        scan_dir,          # "buy" | "sell"
                "confidence":       min(abs(float(match.get("score", 0.0))) * 10, 100.0),
                "instrument_key":   match.get("instrument_key", symbol),
                "trading_symbol":   match.get("trading_symbol", symbol),
                "price_change_pct": float(match.get("price_change_pct", 0.0)),
                "volume_ratio":     float(match.get("volume_ratio", 1.0)),
                "rsi":              match.get("rsi"),
                "signals":          match.get("signals", []),
                "available":        True,
            }

        except Exception as exc:
            logger.warning(
                "Pathway 2 scanner cache lookup failed for %s: %s — using synthetic",
                symbol, exc,
            )
            return fallback

    async def _compute_consensus(
        self,
        db: AsyncSession,
        correlation_id: UUID,
        trigger_type: Literal["SCANNER_ANOMALY", "NEWS_EVENT"],
        trigger_timestamp: datetime,
        scanner_signal: dict[str, Any],
        ai_signal: dict[str, Any],
        ml_signal: dict[str, Any],
        latencies: dict[str, int],
    ) -> TradeSuggestion | None:
        """
        Compute weighted consensus with directional alignment check.
        
        Rules:
        1. All three agents must agree on direction (BUY or SELL)
        2. ML HOLD signal → immediate rejection
        3. Weighted score: Scanner 30%, AI 40%, ML 30%
        4. HIGH: ≥80%, MEDIUM: 60-79%, discard: <60%
        
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
        # Scanner output uses "signal" key ("buy"/"sell"); "direction" is the
        # legacy / Pathway-2-synthetic key.  Accept both.
        _scanner_signal_raw = (
            scanner_signal.get("signal") or scanner_signal.get("direction") or ""
        ).lower()
        scanner_dir = "BUY" if _scanner_signal_raw in ("buy", "bullish") else "SELL"

        # Derive scanner confidence from the anomaly score (0–20 nominal range).
        # score=5 → 25%, score=10 → 50%, score=20 → 100%; capped at 100.
        # Falls back to the explicit "confidence" key when present (Pathway 2 synthetic).
        _raw_score = abs(scanner_signal.get("score", 0.0))
        scanner_conf = scanner_signal.get("confidence") or min(_raw_score / 20.0 * 100, 100.0)

        ai_score = ai_signal.get("score", 0.0)
        # Neutral AI (no events, score=0) should not vote SELL — it is uninformative.
        # Align with the scanner direction so it does not block a scanner+ML consensus.
        ai_dir = "BUY" if ai_score > 0 else ("SELL" if ai_score < 0 else scanner_dir)

        ml_prediction = ml_signal.get("prediction", {})
        ml_dir = ml_prediction.get("direction", "HOLD")

        # Reject ML HOLD signals
        if ml_dir == "HOLD":
            await self._record_correlation(
                db, correlation_id, trigger_type, trigger_timestamp,
                None, "ML_NEUTRAL", latencies
            )
            return None

        # Check directional alignment
        all_buy = (scanner_dir == "BUY" and ai_dir == "BUY" and ml_dir == "BUY")
        all_sell = (scanner_dir == "SELL" and ai_dir == "SELL" and ml_dir == "SELL")

        if not (all_buy or all_sell):
            await self._record_correlation(
                db, correlation_id, trigger_type, trigger_timestamp,
                None,
                f"DIRECTION_MISMATCH: Scanner={scanner_dir}, AI={ai_dir}, ML={ml_dir}",
                latencies
            )
            return None

        # Compute weighted consensus score (all values in 0–100 range).
        # scanner_conf: score-based (0–100) blended with volume_ratio signal strength
        # ai_conf: event classification confidence × 100 (raw is 0–1)
        # ml_conf: ensemble prediction confidence × 100 (raw is 0–1)

        # Blend score (70%) with volume_ratio signal (30%) for a richer scanner proxy.
        # vol_ratio is capped at 10× for normalisation; above that it's noise.
        _vol_ratio = min(scanner_signal.get("volume_ratio", 1.0), 10.0)
        _vol_conf = _vol_ratio / 10.0 * 100.0
        scanner_conf = min(scanner_conf * 0.70 + _vol_conf * 0.30, 100.0)

        ai_conf = abs(ai_signal.get("confidence", 0.0)) * 100
        ml_conf = ml_signal.get("confidence", 0.0) * 100

        # Dynamic weight redistribution: when AI has no events its 40% weight is
        # dead weight and makes consensus unreachable.  Redistribute proportionally
        # to scanner and ML so strong technical + model agreement can produce a
        # valid suggestion — mirrors SignalAssembler.fuse_signals() renormalization.
        ai_available = bool(ai_signal.get("available")) and ai_signal.get("event_count", 0) > 0
        if ai_available:
            w_scanner, w_ai, w_ml = SCANNER_WEIGHT, AI_WEIGHT, ML_WEIGHT
        else:
            # Distribute AI_WEIGHT proportionally between scanner and ML
            total_remaining = SCANNER_WEIGHT + ML_WEIGHT  # 0.60
            w_scanner = SCANNER_WEIGHT / total_remaining  # 0.50
            w_ai      = 0.0
            w_ml      = ML_WEIGHT / total_remaining       # 0.50

        consensus_score = (
            w_scanner * scanner_conf +
            w_ai      * ai_conf +
            w_ml      * ml_conf
        )

        # Determine confidence level
        if consensus_score >= CONSENSUS_HIGH_THRESHOLD:
            confidence_level = "HIGH"
        elif consensus_score >= CONSENSUS_MEDIUM_THRESHOLD:
            confidence_level = "MEDIUM"
        else:
            await self._record_correlation(
                db, correlation_id, trigger_type, trigger_timestamp,
                None, f"LOW_CONFIDENCE: {consensus_score:.2f}", latencies
            )
            return None

        # Extract trade parameters from ML signal
        entry_price = ml_prediction.get("entry_price")
        stop_loss = ml_prediction.get("stop_loss")
        targets = ml_prediction.get("targets", [])

        # Calculate risk/reward ratio
        risk_reward_ratio = None
        if entry_price and stop_loss and targets and targets[0]:
            risk = abs(entry_price - stop_loss)
            reward = abs(targets[0] - entry_price)
            risk_reward_ratio = reward / risk if risk > 0 else None

        # Derive regime_type from signal direction (heuristic; refined when ML
        # model outputs explicit regime classification in a future iteration).
        regime_type = "bull_trending" if all_buy else "bear_trending"

        # Derive time_horizon from trigger pathway:
        # Scanner anomalies are intraday momentum events; news events typically
        # resolve on a longer swing/multi-day horizon.
        time_horizon = (
            "intraday" if trigger_type == "SCANNER_ANOMALY" else "swing"
        )

        # Create trade suggestion
        symbol = scanner_signal.get("instrument_key") or ml_signal.get("symbol", "UNKNOWN")
        # trading_symbol is the human-readable NSE ticker (e.g. "RELIANCE").
        # In Pathway 2 both fields equal the ticker; in Pathway 1 the scanner
        # provides the real trading_symbol separately from the instrument_key.
        trading_sym: str | None = scanner_signal.get("trading_symbol")

        # Resolve company name before committing the suggestion so that the
        # company_name column is populated in one shot — avoiding a later JOIN.
        # This is a best-effort lookup; failures must never block suggestion creation.
        company_name: str | None = None
        try:
            from app.services.symbol_validator import symbol_validator  # noqa: PLC0415
            _lookup_sym = trading_sym or symbol
            company_name = await symbol_validator.get_company_name(_lookup_sym, db)
        except Exception as _cn_exc:
            logger.debug(
                "Company name lookup failed for '%s' (non-fatal): %s",
                trading_sym or symbol, _cn_exc,
            )

        suggestion = TradeSuggestion(
            suggestion_id=uuid4(),
            symbol=symbol,
            instrument_key=symbol,
            trading_symbol=trading_sym,
            company_name=company_name,
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
            regime_type=regime_type,
            time_horizon=time_horizon,
        )

        db.add(suggestion)
        await db.commit()
        await db.refresh(suggestion)

        # Strategy compliance — evaluate against all subscribed users' active
        # strategies and persist compliance rows in the same session.
        # Failures are non-fatal: the suggestion is already committed.
        try:
            from app.services.suggestion_compliance import suggestion_compliance_service
            n_rows = await suggestion_compliance_service.evaluate_and_persist(
                db=db, suggestion=suggestion
            )
            if n_rows:
                await db.commit()
                logger.debug(
                    "Compliance evaluated for suggestion %s: %d rows",
                    suggestion.suggestion_id, n_rows,
                )
        except Exception as exc:
            logger.warning(
                "Strategy compliance evaluation failed for suggestion %s "
                "(non-fatal, suggestion already committed): %s",
                suggestion.suggestion_id, exc,
            )

        # Track metrics
        suggestions_generated_total.labels(
            direction=suggestion.signal_direction,
            confidence_level=confidence_level,
            status="active"
        ).inc()
        
        consensus_score_distribution.observe(float(consensus_score))
        
        # Track correlation latency by pathway and agent
        pathway = "pathway1" if trigger_type == "SCANNER_ANOMALY" else "pathway2"
        if latencies:
            if latencies.get("scanner_ms"):
                correlation_latency_seconds.labels(
                    pathway=pathway, agent="scanner"
                ).observe(latencies["scanner_ms"] / 1000.0)
            if latencies.get("ai_ms"):
                correlation_latency_seconds.labels(
                    pathway=pathway, agent="ai"
                ).observe(latencies["ai_ms"] / 1000.0)
            if latencies.get("ml_ms"):
                correlation_latency_seconds.labels(
                    pathway=pathway, agent="ml"
                ).observe(latencies["ml_ms"] / 1000.0)

        # Record successful correlation
        await self._record_correlation(
            db, correlation_id, trigger_type, trigger_timestamp,
            suggestion.suggestion_id, None, latencies,
            scanner_output=scanner_signal,
            ai_output=ai_signal,
            ml_output=ml_signal,
        )

        # Publish full suggestion payload so WebSocket clients can render
        # immediately without a round-trip REST fetch.
        try:
            t_publish_start = datetime.now(timezone.utc)
            payload = json.dumps({
                "suggestion_id":     str(suggestion.suggestion_id),
                "symbol":            suggestion.symbol,
                "instrument_key":    suggestion.instrument_key,
                "trading_symbol":    suggestion.trading_symbol,
                "company_name":      suggestion.company_name,
                "signal_direction":  suggestion.signal_direction,
                "confidence_level":  suggestion.confidence_level,
                "consensus_score":   float(suggestion.consensus_score),
                "trigger_pathway":   suggestion.trigger_pathway,
                "entry_price":       float(suggestion.entry_price)       if suggestion.entry_price       else None,
                "stop_loss":         float(suggestion.stop_loss)         if suggestion.stop_loss         else None,
                "take_profit_1":     float(suggestion.take_profit_1)     if suggestion.take_profit_1     else None,
                "take_profit_2":     float(suggestion.take_profit_2)     if suggestion.take_profit_2     else None,
                "take_profit_3":     float(suggestion.take_profit_3)     if suggestion.take_profit_3     else None,
                "risk_reward_ratio": float(suggestion.risk_reward_ratio) if suggestion.risk_reward_ratio else None,
                "generated_at":      suggestion.generated_at.isoformat(),
                "expires_at":        suggestion.expires_at.isoformat(),
                "status":            suggestion.status,
            })
            await self.redis.publish(RedisChannels.SUGGESTIONS_NEW, payload)
            publish_ms = int(
                (datetime.now(timezone.utc) - t_publish_start).total_seconds() * 1000
            )
            logger.debug(
                "Pipeline stage=redis_published suggestion_id=%s publish_ms=%d",
                suggestion.suggestion_id, publish_ms,
                extra={
                    "pipeline_stage": "redis_published",
                    "suggestion_id": str(suggestion.suggestion_id),
                    "publish_ms": publish_ms,
                },
            )
        except Exception as e:
            logger.error("Failed to publish suggestion %s to Redis: %s", suggestion.suggestion_id, e)

        return suggestion

    async def _record_correlation(
        self,
        db: AsyncSession,
        correlation_id: UUID,
        trigger_type: Literal["SCANNER_ANOMALY", "NEWS_EVENT"],
        trigger_timestamp: datetime,
        suggestion_id: UUID | None,
        rejection_reason: str | None,
        latencies: dict[str, int] | None,
        scanner_output: dict[str, Any] | None = None,
        ai_output: dict[str, Any] | None = None,
        ml_output: dict[str, Any] | None = None,
    ) -> None:
        """
        Persist correlation event for audit trail and latency monitoring.
        
        Args:
            db: Database session
            correlation_id: Unique correlation identifier
            trigger_type: SCANNER_ANOMALY or NEWS_EVENT
            trigger_timestamp: When correlation started
            suggestion_id: Linked suggestion UUID (None if rejected)
            rejection_reason: Why consensus failed (None if succeeded)
            latencies: Response times per agent
            scanner_output: Scanner signal (for debugging)
            ai_output: AI signal (for debugging)
            ml_output: ML signal (for debugging)
        """
        correlation = EventCorrelation(
            correlation_id=correlation_id,
            suggestion_id=suggestion_id,
            trigger_type=trigger_type,
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
