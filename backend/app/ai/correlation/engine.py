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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import CacheService

from app.ai.fusion.models import AIEventClassification
from app.ai.fusion.signal_assembler import SignalAssembler
from app.core.metrics import (
    suggestions_active,
    suggestions_generated_total,
    consensus_score_distribution,
    correlation_latency_seconds,
)
from app.core.redis import RedisChannels
from app.models.trade_suggestions import EventCorrelation, TradeSuggestion
from app.services.regime_service import RegimeService

logger = logging.getLogger(__name__)

# Consensus configuration
CONSENSUS_HIGH_THRESHOLD = 80.0
CONSENSUS_MEDIUM_THRESHOLD = 60.0
SUGGESTION_EXPIRY_HOURS = 24

# Agent weights (must sum to 1.0)
SCANNER_WEIGHT = 0.30
AI_WEIGHT = 0.40
ML_WEIGHT = 0.30

# AI forecast sources that carry a genuinely computed direction (F.A).
# Every other shape — fallback/batch_pending, circuit_breaker_open,
# stale_data, no_news — is treated as unavailable: its weight renormalizes
# to scanner/ML and it abstains from the direction vote.
LIVE_FORECAST_SOURCES = frozenset({"gemini_batch", "demand"})

# Regime-conditioned weight overrides (F.C), keyed by RegimeService label:
# in high-volatility regimes technical anomaly readings are noisier, so the
# scanner cedes weight to ML (whose features include volatility context).
# Static map by design — no learned weighting. Absent labels use defaults.
REGIME_WEIGHT_OVERRIDES: dict[str, tuple[float, float, float]] = {
    # (scanner, ai, ml)
    "high_volatility": (0.25, 0.30, 0.45),
}

# Deduplication thresholds — a new signal must exceed at least one of these
# to supersede an existing active suggestion for the same instrument+direction.
# Below all thresholds the new signal is suppressed (no new row created).
DRASTIC_SCORE_DELTA = 5.0        # consensus_score change ≥ 5 points
DRASTIC_PRICE_DELTA_PCT = 0.02   # entry_price change ≥ 2%


class EventCorrelationEngine:
    """
    Orchestrates bidirectional signal validation between agents.
    
    Implements weighted consensus with directional alignment checks.
    All three agents (Scanner, AI, ML) must agree on direction.

    Features:
    - Sub-100ms consensus latency target
    - Comprehensive audit trail via EventCorrelation records
    - Redis pub/sub for real-time frontend updates

    Fault tolerance for the one external dependency (Gemini news forecast)
    lives in SignalAssembler's circuit breaker; scanner and ML signal loads
    are local DB reads with their own error handling.
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

        # Ceiling for the consensus signal-gather (ML → news forecast).
        from app.core.config import get_settings
        self._gather_timeout = get_settings().CONSENSUS_GATHER_TIMEOUT

    async def _publish_correlation_event(
        self,
        channel: str,
        payload: dict[str, Any],
    ) -> None:
        """
        Non-fatal Redis publish wrapper for correlation lifecycle events.

        Telemetry must never interrupt the signal pipeline — any publish failure
        is logged at DEBUG and swallowed so that the caller can continue normally.
        """
        try:
            await self.redis.publish(channel, json.dumps(payload, default=str))
        except Exception as exc:
            logger.debug(
                "Non-fatal: failed to publish to %s: %s", channel, exc
            )

    async def _mark_inflight(
        self,
        correlation_id: UUID,
        symbol: str,
        trading_symbol: str | None,
        trigger_type: str,
        started_at: datetime,
    ) -> None:
        """Checkpoint an in-flight correlation to Redis so the seed endpoint can surface it."""
        try:
            started_at_ms = int(started_at.timestamp() * 1000)
            payload = json.dumps({
                "correlation_id": str(correlation_id),
                "symbol":         symbol,
                "trading_symbol": trading_symbol,
                "trigger_type":   trigger_type,
                "started_at":     started_at.isoformat(),
                "started_at_ms":  started_at_ms,
            }, default=str)
            key = f"cortex:correlation:inflight:{correlation_id}"
            pipe = self.redis.pipeline()
            pipe.setex(key, 60, payload)
            pipe.zadd("cortex:correlations:inflight", {str(correlation_id): started_at_ms})
            pipe.expire("cortex:correlations:inflight", 3600)
            await pipe.execute()
        except Exception as exc:
            logger.debug("Non-fatal: failed to mark inflight %s: %s", correlation_id, exc)

    async def _clear_inflight(self, correlation_id: UUID) -> None:
        """Remove a correlation from the in-flight Redis checkpoint on resolution."""
        try:
            pipe = self.redis.pipeline()
            pipe.delete(f"cortex:correlation:inflight:{correlation_id}")
            pipe.zrem("cortex:correlations:inflight", str(correlation_id))
            await pipe.execute()
        except Exception as exc:
            logger.debug("Non-fatal: failed to clear inflight %s: %s", correlation_id, exc)

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

        await self._mark_inflight(
            correlation_id, symbol,
            scanner_signal.get("trading_symbol"),
            "SCANNER_ANOMALY", trigger_timestamp,
        )
        try:
            await self._publish_correlation_event(
                RedisChannels.CORRELATIONS_STARTED,
                {
                    "correlation_id": str(correlation_id),
                    "symbol":         symbol,
                    "trading_symbol": scanner_signal.get("trading_symbol"),
                    "trigger_type":   "SCANNER_ANOMALY",
                    "started_at":     trigger_timestamp.isoformat(),
                },
            )

            # Gather signals with timeout
            t_gather_start = datetime.now(timezone.utc)
            try:
                ai_signal, ml_signal, latencies = await asyncio.wait_for(
                    self._gather_signals_pathway1(db, scanner_signal),
                    timeout=self._gather_timeout,
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
                await self._publish_correlation_event(
                    RedisChannels.CORRELATIONS_REJECTED,
                    {
                        "correlation_id":   str(correlation_id),
                        "symbol":           symbol,
                        "trading_symbol":   scanner_signal.get("trading_symbol"),
                        "trigger_type":     "SCANNER_ANOMALY",
                        "rejection_reason": "TIMEOUT",
                        "consensus_score":  0.0,
                        "rejected_at":      datetime.now(timezone.utc).isoformat(),
                    },
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
                await self._publish_correlation_event(
                    RedisChannels.CORRELATIONS_REJECTED,
                    {
                        "correlation_id":   str(correlation_id),
                        "symbol":           symbol,
                        "trading_symbol":   scanner_signal.get("trading_symbol"),
                        "trigger_type":     "SCANNER_ANOMALY",
                        "rejection_reason": "PROCESSING_ERROR",
                        "consensus_score":  0.0,
                        "rejected_at":      datetime.now(timezone.utc).isoformat(),
                    },
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
        finally:
            await self._clear_inflight(correlation_id)

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
            per_symbol_correlation_id = uuid4()
            per_symbol_started_at = datetime.now(timezone.utc)

            await self._mark_inflight(
                per_symbol_correlation_id, symbol, symbol,
                "NEWS_EVENT", per_symbol_started_at,
            )
            try:
                await self._publish_correlation_event(
                    RedisChannels.CORRELATIONS_STARTED,
                    {
                        "correlation_id": str(per_symbol_correlation_id),
                        "symbol":         symbol,
                        "trading_symbol": symbol,
                        "trigger_type":   "NEWS_EVENT",
                        "started_at":     per_symbol_started_at.isoformat(),
                    },
                )

                scanner_signal, ml_signal, latencies = await asyncio.wait_for(
                    self._gather_signals_pathway2(db, symbol, event),
                    timeout=self._gather_timeout,
                )

                # AI/news slot: Gemini news forecast over the ML's indicators +
                # recent news (incl. this trigger event); deterministic event-score
                # fallback on any failure.
                ai_signal = await self.assembler.gather_news_forecast(
                    db, symbol, ml_signal=ml_signal,
                )

                suggestion = await self._compute_consensus(
                    db=db,
                    correlation_id=per_symbol_correlation_id,
                    trigger_type="NEWS_EVENT",
                    trigger_timestamp=per_symbol_started_at,
                    scanner_signal=scanner_signal,
                    ai_signal=ai_signal,
                    ml_signal=ml_signal,
                    latencies=latencies,
                )

                if suggestion:
                    suggestions.append(suggestion)

            except Exception as e:
                logger.error(
                    "[%s] Error processing symbol %s: %s",
                    correlation_id, symbol, e,
                )
                await self._publish_correlation_event(
                    RedisChannels.CORRELATIONS_REJECTED,
                    {
                        "correlation_id":   str(per_symbol_correlation_id),
                        "symbol":           symbol,
                        "trading_symbol":   symbol,
                        "trigger_type":     "NEWS_EVENT",
                        "rejection_reason": "PROCESSING_ERROR",
                        "consensus_score":  0.0,
                        "rejected_at":      datetime.now(timezone.utc).isoformat(),
                    },
                )
            finally:
                await self._clear_inflight(per_symbol_correlation_id)

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

        # ML first (its indicators feed the news forecaster), then the AI/news
        # forecast.  Sequential by necessity: both share one DB session, and the
        # forecaster reasons over the ML's exact indicator snapshot.
        ml_start = datetime.now(timezone.utc)
        ml_signal = await self.assembler.gather_ml_signals(db, symbol)
        ml_latency = (datetime.now(timezone.utc) - ml_start).total_seconds() * 1000

        ai_start = datetime.now(timezone.utc)
        ai_signal = await self.assembler.gather_news_forecast(
            db, symbol, ml_signal=ml_signal,
        )
        ai_latency = (datetime.now(timezone.utc) - ai_start).total_seconds() * 1000

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

        Returns the real scanner dict when found; otherwise falls back to a
        synthetic signal derived from the event's PERSISTED directional
        sentiment (available=False flagged) so the consensus engine can still
        evaluate Pathway 2 with a weak scanner agent.

        Direction comes from ``event.sentiment`` (bullish/bearish, written by
        the classifier since migration 0052) — NEVER from ``impact_score``,
        which is an unsigned severity magnitude: the old ``impact > 0``
        derivation returned "buy" for virtually every event, including
        severely bearish ones (fraud probe, impact 85 → BUY-leaning signal).
        Neutral or NULL sentiment yields no synthetic direction; consensus
        then rejects via NEUTRAL_SIGNAL instead of manufacturing agreement.
        """
        # Synthetic fallback used when scanner cache is absent or symbol not found.
        # Confidence is intentionally capped at 50 so a synthetic-only Pathway 2
        # signal can only reach consensus if the ML score is very high.
        sentiment = (event.sentiment or "").lower()
        if sentiment == "bullish":
            fallback_direction = "buy"
        elif sentiment == "bearish":
            fallback_direction = "sell"
        else:
            fallback_direction = "neutral"  # no directional basis — never fabricate one
        fallback: dict[str, Any] = {
            "direction":       fallback_direction,
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
        # Derive symbol identifiers early — needed for rejection event payloads
        # before the full instrument resolution block runs later in the method.
        _act_symbol: str = (
            scanner_signal.get("instrument_key")
            or scanner_signal.get("trading_symbol")
            or ml_signal.get("symbol", "UNKNOWN")
        )
        _act_trading_sym: str | None = scanner_signal.get("trading_symbol") or _act_symbol

        # Map to unified direction.
        # Scanner output uses "signal" key ("buy"/"sell"); "direction" is the
        # legacy / Pathway-2-synthetic key.  Accept both.
        # Neutral/hold/unknown signals are hard-rejected — mapping them to SELL
        # would introduce a systematic directional bias (all non-BUY → SELL).
        _scanner_signal_raw = (
            scanner_signal.get("signal") or scanner_signal.get("direction") or ""
        ).lower()
        if _scanner_signal_raw in ("buy", "bullish"):
            scanner_dir = "BUY"
        elif _scanner_signal_raw in ("sell", "bearish"):
            scanner_dir = "SELL"
        else:
            await self._record_correlation(
                db, correlation_id, trigger_type, trigger_timestamp,
                None,
                f"NEUTRAL_SIGNAL: scanner={_scanner_signal_raw!r}",
                latencies,
                scanner_output={"instrument_key": _act_symbol, "trading_symbol": _act_trading_sym},
            )
            await self._publish_correlation_event(
                RedisChannels.CORRELATIONS_REJECTED,
                {
                    "correlation_id":   str(correlation_id),
                    "symbol":           _act_symbol,
                    "trading_symbol":   _act_trading_sym,
                    "trigger_type":     trigger_type,
                    "rejection_reason": "NEUTRAL_SIGNAL",
                    "consensus_score":  0.0,
                    "rejected_at":      datetime.now(timezone.utc).isoformat(),
                },
            )
            return None

        # Derive scanner confidence from the anomaly score (0–20 nominal range).
        # score=5 → 25%, score=10 → 50%, score=20 → 100%; capped at 100.
        # Falls back to the explicit "confidence" key when present (Pathway 2 synthetic).
        _raw_score = abs(scanner_signal.get("score", 0.0))
        scanner_conf = scanner_signal.get("confidence") or min(_raw_score / 20.0 * 100, 100.0)

        # AI directional vote (F.A/F.B): only a genuinely computed forecast —
        # live source AND an explicit BUY/SELL direction — votes in the
        # unanimity check. Pending/fallback/neutral(HOLD) AI ABSTAINS. The old
        # code force-aligned a zero score to the scanner direction, which
        # manufactured agreement a real forecast (landing seconds later) could
        # contradict — the scoring-side twin of the batch_pending race.
        ai_direction_raw = str(ai_signal.get("direction") or "").upper()
        ai_available = (
            ai_signal.get("forecast_source") in LIVE_FORECAST_SOURCES
            and ai_direction_raw in ("BUY", "SELL")
        )
        ai_dir = ai_direction_raw if ai_available else "ABSTAIN"

        ml_prediction = ml_signal.get("prediction", {})
        ml_dir = ml_prediction.get("direction", "HOLD")

        # Reject ML HOLD signals
        if ml_dir == "HOLD":
            await self._record_correlation(
                db, correlation_id, trigger_type, trigger_timestamp,
                None, "ML_NEUTRAL", latencies,
                scanner_output={"instrument_key": _act_symbol, "trading_symbol": _act_trading_sym},
            )
            await self._publish_correlation_event(
                RedisChannels.CORRELATIONS_REJECTED,
                {
                    "correlation_id":   str(correlation_id),
                    "symbol":           _act_symbol,
                    "trading_symbol":   _act_trading_sym,
                    "trigger_type":     trigger_type,
                    "rejection_reason": "ML_NEUTRAL",
                    "consensus_score":  0.0,
                    "rejected_at":      datetime.now(timezone.utc).isoformat(),
                },
            )
            return None

        # Directional unanimity over the VOTING components only (F.B): scanner
        # and ML always vote; AI votes only when available. An abstaining AI
        # neither agrees nor vetoes — this is what keeps the no-news
        # scanner+ML pathway alive after removing the force-align above.
        votes = [scanner_dir, ml_dir] + ([ai_dir] if ai_available else [])
        all_buy = all(v == "BUY" for v in votes)
        all_sell = all(v == "SELL" for v in votes)

        if not (all_buy or all_sell):
            await self._record_correlation(
                db, correlation_id, trigger_type, trigger_timestamp,
                None,
                f"DIRECTION_MISMATCH: Scanner={scanner_dir}, AI={ai_dir}, ML={ml_dir}",
                latencies,
                scanner_output={"instrument_key": _act_symbol, "trading_symbol": _act_trading_sym},
            )
            await self._publish_correlation_event(
                RedisChannels.CORRELATIONS_REJECTED,
                {
                    "correlation_id":   str(correlation_id),
                    "symbol":           _act_symbol,
                    "trading_symbol":   _act_trading_sym,
                    "trigger_type":     trigger_type,
                    "rejection_reason": "DIRECTION_MISMATCH",
                    "consensus_score":  0.0,
                    "rejected_at":      datetime.now(timezone.utc).isoformat(),
                },
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

        # ── Regime-conditioned weighting (F.C) ────────────────────────────────
        # Real regime classification (RegimeService over 60 daily candles)
        # replaces the old post-hoc placeholder label and conditions the agent
        # weights. Fetched only after the unanimity gate — candidates rejected
        # on direction never pay the OHLCV read. Any failure degrades to
        # default weights + "unknown"; regime must never block consensus.
        _regime_instrument = scanner_signal.get("instrument_key") or ml_signal.get("symbol", "")
        regime_type = "unknown"
        try:
            regime_info = await RegimeService().get_instrument_regime(
                db,
                instrument_key=_regime_instrument,
                # trading_symbol/company_name are echo-only in the regime
                # response; the computation needs only the instrument_key.
                trading_symbol=_act_trading_sym or _regime_instrument,
                company_name=_act_trading_sym or _regime_instrument,
            )
            regime_type = regime_info.get("regime", "unknown")
        except Exception as exc:
            logger.warning(
                "Regime lookup failed for %s — using default weights: %s",
                _regime_instrument, exc,
            )

        base_scanner, base_ai, base_ml = REGIME_WEIGHT_OVERRIDES.get(
            regime_type, (SCANNER_WEIGHT, AI_WEIGHT, ML_WEIGHT)
        )
        if regime_type in REGIME_WEIGHT_OVERRIDES:
            logger.info(
                "Regime weight override active for %s: regime=%s weights=(%.2f/%.2f/%.2f)",
                _regime_instrument, regime_type, base_scanner, base_ai, base_ml,
            )

        # Dynamic weight redistribution (F.A): when the AI forecast is not a
        # genuinely computed one (see ai_available above), its weight is dead
        # weight that silently zero-drags the score — redistribute it
        # proportionally to scanner and ML from the regime-conditioned base,
        # mirroring SignalAssembler.fuse_signals() renormalization.
        if ai_available:
            w_scanner, w_ai, w_ml = base_scanner, base_ai, base_ml
        else:
            total_remaining = base_scanner + base_ml
            w_scanner = base_scanner / total_remaining
            w_ai      = 0.0
            w_ml      = base_ml / total_remaining

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
                None, f"LOW_CONFIDENCE: {consensus_score:.2f}", latencies,
                scanner_output={"instrument_key": _act_symbol, "trading_symbol": _act_trading_sym},
            )
            await self._publish_correlation_event(
                RedisChannels.CORRELATIONS_REJECTED,
                {
                    "correlation_id":   str(correlation_id),
                    "symbol":           _act_symbol,
                    "trading_symbol":   _act_trading_sym,
                    "trigger_type":     trigger_type,
                    "rejection_reason": "LOW_CONFIDENCE",
                    "consensus_score":  round(consensus_score, 2),
                    "rejected_at":      datetime.now(timezone.utc).isoformat(),
                },
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

        # regime_type was computed above by RegimeService (F.C) — the old
        # direction-derived placeholder ("bull_trending" if all_buy) is gone.

        # Derive time_horizon from trigger pathway:
        # Scanner anomalies are intraday momentum events; news events typically
        # resolve on a longer swing/multi-day horizon.
        time_horizon = (
            "intraday" if trigger_type == "SCANNER_ANOMALY" else "swing"
        )

        # ── Instrument identification ─────────────────────────────────────────
        symbol = scanner_signal.get("instrument_key") or ml_signal.get("symbol", "UNKNOWN")
        # trading_symbol is the human-readable NSE ticker (e.g. "RELIANCE").
        # In Pathway 2 both fields equal the ticker; in Pathway 1 the scanner
        # provides the real trading_symbol separately from the instrument_key.
        trading_sym: str | None = scanner_signal.get("trading_symbol")
        direction = "BUY" if all_buy else "SELL"

        # ── Deduplication guard ───────────────────────────────────────────────
        # Before writing anything, check whether an active suggestion already
        # exists for this (instrument_key, direction) pair.  The partial unique
        # index idx_trade_suggestions_active_unique enforces this constraint at
        # the DB level; this application-level check is the first line of defence
        # and avoids unnecessary INSERT attempts.
        #
        # SELECT FOR UPDATE serializes concurrent workers: only one process at a
        # time can check-and-modify the active suggestion for a given symbol.
        existing_stmt = (
            select(TradeSuggestion)
            .where(
                TradeSuggestion.instrument_key == symbol,
                TradeSuggestion.signal_direction == direction,
                TradeSuggestion.status == "active",
            )
            .with_for_update(skip_locked=False)
        )
        existing_result = await db.execute(existing_stmt)
        existing = existing_result.scalar_one_or_none()

        if existing is not None:
            score_delta = abs(consensus_score - float(existing.consensus_score))
            price_delta_pct = (
                abs(float(entry_price) / float(existing.entry_price) - 1.0)
                if entry_price and existing.entry_price
                else 0.0
            )
            confidence_changed = confidence_level != existing.confidence_level

            is_drastic = (
                score_delta >= DRASTIC_SCORE_DELTA
                or price_delta_pct >= DRASTIC_PRICE_DELTA_PCT
                or confidence_changed
            )

            if not is_drastic:
                # Signal is not materially different from the existing one.
                # Record the suppression for audit/metrics and return without
                # creating a new row — the existing suggestion stays active.
                await self._record_correlation(
                    db, correlation_id, trigger_type, trigger_timestamp,
                    None,
                    (
                        f"DUPLICATE_SUPPRESSED: existing={existing.suggestion_id} "
                        f"score_delta={score_delta:.2f} "
                        f"price_delta_pct={price_delta_pct:.3f}"
                    ),
                    latencies,
                    scanner_output={"instrument_key": _act_symbol, "trading_symbol": _act_trading_sym},
                )
                logger.debug(
                    "Pipeline stage=duplicate_suppressed correlation_id=%s "
                    "symbol=%s direction=%s existing_id=%s score_delta=%.2f",
                    correlation_id, symbol, direction,
                    existing.suggestion_id, score_delta,
                )
                await self._publish_correlation_event(
                    RedisChannels.CORRELATIONS_REJECTED,
                    {
                        "correlation_id":   str(correlation_id),
                        "symbol":           _act_symbol,
                        "trading_symbol":   _act_trading_sym,
                        "trigger_type":     trigger_type,
                        "rejection_reason": "DUPLICATE_SUPPRESSED",
                        "consensus_score":  round(consensus_score, 2),
                        "rejected_at":      datetime.now(timezone.utc).isoformat(),
                    },
                )
                return None

            # New signal is materially different — supersede the existing suggestion.
            # Publish a superseded event before the commit so the frontend can
            # remove the old card before the new one arrives.
            superseded_id = existing.suggestion_id
            existing.status = "superseded"
            existing.updated_at = trigger_timestamp
            logger.info(
                "Pipeline stage=superseding correlation_id=%s "
                "superseded_id=%s symbol=%s direction=%s "
                "score_delta=%.2f price_delta_pct=%.2f%% confidence_changed=%s",
                correlation_id, superseded_id, symbol, direction,
                score_delta, price_delta_pct * 100, confidence_changed,
                extra={
                    "pipeline_stage": "superseding",
                    "correlation_id": str(correlation_id),
                    "superseded_id": str(superseded_id),
                },
            )
            # Notify WebSocket clients that the old suggestion is gone.
            # Published after the DB commit below; captured here for the
            # publish block at the end of this method.
            _superseded_payload: dict | None = {
                "suggestion_id": str(superseded_id),
                "symbol": symbol,
                "expired_at": trigger_timestamp.isoformat(),
                "reason": "SUPERSEDED",
            }
        else:
            _superseded_payload = None

        # ── Resolve company name ──────────────────────────────────────────────
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
            symbol=trading_sym or symbol,
            instrument_key=symbol,
            trading_symbol=trading_sym,
            company_name=company_name,
            consensus_score=Decimal(str(round(consensus_score, 2))),
            confidence_level=confidence_level,
            signal_direction=direction,
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
        suggestions_active.labels(
            direction=suggestion.signal_direction,
            confidence_level=confidence_level,
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

        # Trigger async LLM explanation generation (LEGACY auto-publish path).
        # Skipped entirely under EXPLANATION_ON_DEMAND: explanations then
        # generate at first view (ai_stream → explanation_service publishes a
        # demand job), so demand — not consensus strength — is the gate.
        # Flipping the flag back to False restores this path (rollback lever).
        # In legacy mode the consensus gate applies: only enqueue when the
        # signal is strong enough to warrant a Gemini call; weak signals
        # surface a placeholder with a user-driven refresh button (bypass
        # endpoint).  Publish failure is non-fatal: it must never block or
        # roll back the committed suggestion.
        try:
            from app.core.config import get_settings as _get_settings
            from app.core.kafka import KafkaTopics, publish
            _settings = _get_settings()
            _threshold = _settings.EXPLANATION_CONSENSUS_THRESHOLD
            if _settings.EXPLANATION_ON_DEMAND:
                logger.debug(
                    "explanation auto-publish skipped (on-demand mode): suggestion=%s",
                    suggestion.suggestion_id,
                )
            elif float(suggestion.consensus_score) >= _threshold:
                await publish(
                    KafkaTopics.EXPLANATION_JOBS,
                    {
                        "suggestion_id":  str(suggestion.suggestion_id),
                        "id":             str(suggestion.id),
                        "instrument_key": suggestion.instrument_key,
                    },
                    key=str(suggestion.suggestion_id),
                )
                logger.debug(
                    "explanation job enqueued: suggestion=%s consensus_score=%.1f",
                    suggestion.suggestion_id, float(suggestion.consensus_score),
                )
            else:
                logger.info(
                    "explanation job skipped (weak signal): suggestion=%s "
                    "consensus_score=%.1f < threshold=%.1f",
                    suggestion.suggestion_id,
                    float(suggestion.consensus_score),
                    _threshold,
                )
        except Exception as exc:
            logger.warning(
                "Failed to enqueue explanation job for suggestion %s (non-fatal): %s",
                suggestion.suggestion_id, exc,
            )

        # Publish full suggestion payload so WebSocket clients can render
        # immediately without a round-trip REST fetch.
        try:
            t_publish_start = datetime.now(timezone.utc)
            payload = json.dumps({
                "correlation_id":    str(correlation_id),
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

        # Publish correlation completion — carries per-agent latencies for the
        # ML Activity feed. Sent after SUGGESTIONS_NEW: the frontend row is
        # already "completed" via new_suggestion, this just attaches timing.
        await self._publish_correlation_event(
            RedisChannels.CORRELATIONS_COMPLETED,
            {
                "correlation_id":  str(correlation_id),
                "suggestion_id":   str(suggestion.suggestion_id),
                "symbol":          suggestion.symbol,
                "trading_symbol":  suggestion.trading_symbol,
                "trigger_type":    trigger_type,
                "consensus_score": float(suggestion.consensus_score),
                "latencies":       latencies or {},
                "completed_at":    datetime.now(timezone.utc).isoformat(),
            },
        )

        # Notify WebSocket clients that the superseded suggestion is gone.
        # Published after the new suggestion so the frontend replaces rather
        # than just removes the card.
        if _superseded_payload is not None:
            try:
                await self.redis.publish(
                    RedisChannels.SUGGESTIONS_EXPIRED,
                    json.dumps(_superseded_payload),
                )
            except Exception as e:
                logger.error(
                    "Failed to publish superseded event for %s to Redis: %s",
                    _superseded_payload.get("suggestion_id"), e,
                )

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
