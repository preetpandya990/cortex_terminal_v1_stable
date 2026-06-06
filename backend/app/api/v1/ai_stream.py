"""
AI Analysis Stream — Server-Sent Events (SSE)
=============================================
Real-time SSE endpoint that streams prediction + pattern + sentiment +
LLM explanation updates to connected frontend clients.

Endpoint:
  GET /api/v1/ai/stream?instrument_key=...&token=<jwt>

Why query-param auth:
  Browser's EventSource API does not support custom request headers.
  The JWT is passed as a query parameter and validated server-side before
  any data is streamed. The Next.js proxy (frontend/src/app/api/v1/ai/stream)
  converts the Authorization header from the frontend into the query param.

Event types:
  - "analysis_update" : combined prediction + pattern + sentiment + explanation payload
  - "heartbeat"       : empty comment ping every 15s (keeps proxies alive)
  - "error"           : transient error info (client should keep connection)

analysis_update payload shape:
  {
    "prediction": {...} | null,
    "pattern":    {...} | null,
    "sentiment":  {...} | null,
    "explanation": {
      "available":        bool,
      "summary":          str | null,
      "full_explanation": str | null,
      "model":            str | null,
      "generated_at":     str | null,   # ISO-8601 UTC
      "sources": [                       # populated via push path only
        {"source_name": str, "as_of": str, "source_url": str}
      ]
    } | null,
    "instrument_key": str,
    "emitted_at":     str   # ISO-8601 UTC
  }

Explanation delivery — two complementary paths:

  Push path (real-time):
    The explanation_worker publishes to
    ``cortex:llm:explanation:ready:{suggestion_id}`` when generation completes.
    A background asyncio task per SSE connection subscribes to the
    ``cortex:llm:explanation:ready:*`` pattern via Redis PSUBSCRIBE.  On receipt,
    the main loop verifies the suggestion belongs to this instrument, then emits
    an analysis_update immediately — without waiting for the next polling cycle.
    Structured source refs (source_name, as_of, source_url) are included in this
    path, enabling the ExplanationPanel to render source attribution.

  Poll path (fallback):
    Every _EXPLANATION_REFRESH_SECS the stream re-queries the DB for the latest
    active suggestion's explanation fields.  This recovers state on reconnect or
    if a push notification is missed (Redis pub/sub is at-most-once).  Sources
    are not available on the poll path — the sources list will be empty, but
    inline citations in ``full_explanation`` still provide attribution.

Reconnect:
  SSE clients auto-reconnect on disconnect (browser default: 3s).
  The ``retry`` field in the stream-init event sets this to 5000ms.

Security:
  - JWT validated before first event is yielded
  - Token must be an access token (type="access")
  - Connection closed immediately on invalid/expired token
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from app.api.deps import get_db
from app.api.v1.ml_predictions import serialize_prediction_card
from app.core.metrics import llm_sse_explanation_pushes_total
from app.core.redis import get_redis
from app.core.security import CortexInvalidTokenError, decode_token
from app.ml.inference.feature_loader import FeatureLoader
from app.models.trade_suggestions import TradeSuggestion
from app.services.pattern_detection_service import PatternDetectionService
from app.services.sentiment_analysis_service import SentimentAnalysisService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["AI Analysis Stream"])

# Refresh intervals (seconds) for each analysis component
_PREDICTION_REFRESH_SECS  = 60    # 1 min — price-sensitive; Redis-cached so cheap
_PATTERN_REFRESH_SECS     = 300   # 5 min (matches Redis L2 TTL)
_SENTIMENT_REFRESH_SECS   = 120   # 2 min
_EXPLANATION_REFRESH_SECS = 30    # 30 s — fallback poll if push is missed; primary delivery is push
_HEARTBEAT_SECS           = 15    # Keep proxy / load-balancer connections alive

# Pattern for PSUBSCRIBE — matches all per-suggestion explanation-ready channels.
_EXPLANATION_READY_PATTERN = "cortex:llm:explanation:ready:*"


# ── Explanation helpers ────────────────────────────────────────────────────────

def _build_explanation_payload(
    suggestion: TradeSuggestion,
    sources: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Serialize a TradeSuggestion's explanation fields into the SSE payload shape.

    ``sources`` is populated only on the push path (the Redis ready notification
    carries the structured source list from the explanation worker).  On the poll
    path (periodic DB refresh) it will be an empty list — inline citations within
    ``full_explanation`` still provide source attribution.
    """
    return {
        "available":        suggestion.llm_summary is not None,
        "summary":          suggestion.llm_summary,
        "full_explanation": suggestion.llm_explanation,
        "model":            suggestion.explanation_model,
        "generated_at": (
            suggestion.explanation_generated_at.isoformat()
            if suggestion.explanation_generated_at else None
        ),
        "sources": sources if sources is not None else [],
    }


async def _fetch_active_explanation(
    db: AsyncSession,
    instrument_key: str,
) -> dict[str, Any] | None:
    """
    Query the latest active suggestion for the given instrument and return its
    explanation payload.

    Returns None when no active suggestion exists (frontend renders nothing).
    Returns the payload dict (with ``available=False``) when a suggestion exists
    but explanation generation is still pending.
    """
    stmt = (
        select(TradeSuggestion)
        .where(
            TradeSuggestion.instrument_key == instrument_key,
            TradeSuggestion.status == "active",
        )
        .order_by(TradeSuggestion.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    suggestion = result.scalar_one_or_none()
    if suggestion is None:
        return None
    return _build_explanation_payload(suggestion)


# ── SSE endpoint ───────────────────────────────────────────────────────────────

@router.get(
    "/stream",
    summary="Server-Sent Events stream for AI Analysis Cards",
    description="""
    Establishes a persistent SSE connection and streams real-time analysis
    updates for the specified instrument.

    **Authentication**: Pass your JWT access token as the `token` query parameter.
    (The browser EventSource API does not support custom headers.)

    **Events**:
    - `analysis_update`: Full combined payload (prediction + pattern + sentiment + explanation)
    - `heartbeat`: Empty ping every 15 seconds
    - `error`: Non-fatal error notification

    **Reconnection**: Browser will auto-reconnect after 5 seconds on disconnect.
    """,
    responses={
        200: {"description": "SSE stream established", "content": {"text/event-stream": {}}},
        401: {"description": "Invalid or expired JWT token"},
    },
)
async def analysis_stream(
    request: Request,
    instrument_key: str = Query(
        ...,
        min_length=1,
        max_length=100,
        description="NSE instrument key",
    ),
    symbol: str | None = Query(
        None,
        description="NSE trading symbol for news filtering (e.g. RELIANCE)",
    ),
    token: str = Query(
        ...,
        description="JWT access token (passed as query param — EventSource limitation)",
    ),
    lookback_hours: int = Query(
        24,
        ge=1,
        le=168,
        description="Sentiment lookback window in hours",
    ),
    db: AsyncSession = Depends(get_db),
) -> EventSourceResponse:
    """Stream combined prediction + pattern + sentiment + explanation analysis via SSE."""

    # Validate JWT before opening stream — fail fast with HTTP 401.
    # EventSourceResponse cannot return a non-200 status once established, so
    # we must validate synchronously and return a plain JSONResponse on failure.
    try:
        payload = decode_token(token, expected_type="access")
        user_id: str = payload.sub
    except CortexInvalidTokenError as exc:
        logger.warning("SSE auth failed: instrument=%s error=%s", instrument_key, exc)
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_token", "message": "Invalid or expired access token"},
        )

    redis = get_redis()

    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        logger.info(
            "SSE stream opened: user=%s instrument=%s", user_id, instrument_key
        )

        # ── Per-component refresh state ────────────────────────────────────
        last_prediction_refresh:  float = 0.0
        last_pattern_refresh:     float = 0.0
        last_sentiment_refresh:   float = 0.0
        last_explanation_refresh: float = 0.0

        prediction_data:  dict[str, Any] | None = None
        pattern_data:     dict[str, Any] | None = None
        sentiment_data:   dict[str, Any] | None = None
        explanation_data: dict[str, Any] | None = None

        heartbeat_counter: int = 0

        # ── Explanation push path ──────────────────────────────────────────
        # A background task subscribes to the PSUBSCRIBE pattern and forwards
        # all explanation-ready messages into this queue.  The main loop drains
        # the queue, verifies each message belongs to the current instrument,
        # and emits immediately when a match is found.
        #
        # Queue is bounded (maxsize=8).  When full, the oldest entry is evicted
        # before each new enqueue to prevent unbounded memory growth in a
        # slow-consumer scenario.  Explanation pushes are idempotent — emitting
        # the same explanation twice is harmless.
        _explanation_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=8)

        async def _watch_explanations() -> None:
            """
            Background task: PSUBSCRIBE to all explanation-ready channels and
            forward matching messages to ``_explanation_queue`` for the main loop.

            Lifetime: tied to this SSE connection.  Started before the main loop
            and cancelled (with await) in the generator's finally block.
            """
            pubsub = redis.pubsub()
            try:
                await pubsub.psubscribe(_EXPLANATION_READY_PATTERN)
                logger.debug(
                    "SSE explanation watcher started: user=%s instrument=%s",
                    user_id, instrument_key,
                )
                while True:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=0.5
                    )
                    if msg is not None and msg.get("type") == "pmessage":
                        try:
                            data = json.loads(msg["data"])
                            # Evict oldest entry if the queue is full
                            if _explanation_queue.full():
                                with contextlib.suppress(asyncio.QueueEmpty):
                                    _explanation_queue.get_nowait()
                            _explanation_queue.put_nowait(data)
                        except (json.JSONDecodeError, TypeError):
                            logger.debug(
                                "SSE explanation watcher: unparseable message data: %.80s",
                                msg.get("data", ""),
                            )
                    await asyncio.sleep(0)  # yield to event loop between polls

            except asyncio.CancelledError:
                pass
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.punsubscribe(_EXPLANATION_READY_PATTERN)
                    await pubsub.aclose()
                logger.debug(
                    "SSE explanation watcher stopped: user=%s instrument=%s",
                    user_id, instrument_key,
                )

        watcher_task = asyncio.create_task(_watch_explanations())

        try:
            # Tell the client to retry after 5 seconds on disconnect
            yield ServerSentEvent(comment="stream-init", retry=5000)

            while True:
                # ── Disconnect check ───────────────────────────────────────
                if await request.is_disconnected():
                    logger.info(
                        "SSE stream closed: user=%s instrument=%s",
                        user_id, instrument_key,
                    )
                    break

                now = asyncio.get_event_loop().time()
                refresh_needed = False

                # ── Ensemble prediction refresh ────────────────────────────
                if (now - last_prediction_refresh) >= _PREDICTION_REFRESH_SECS:
                    try:
                        predictor = getattr(request.app.state, "ml_predictor", None)
                        if predictor is not None:
                            feat_loader = FeatureLoader(
                                db=db,
                                redis=redis,
                                sequence_length=predictor.sequence_length,
                                n_features=predictor.n_features,
                                feature_names=predictor.feature_names,
                            )
                            try:
                                tabular, sequence, current_price, vol = (
                                    await feat_loader.load_features(
                                        symbol=instrument_key,
                                        timeframe="1d",
                                    )
                                )
                                raw_pred = await predictor.predict(
                                    features_tabular=tabular,
                                    features_sequence=sequence,
                                    symbol=instrument_key,
                                    current_price=current_price,
                                    volatility=vol,
                                    timeframe="1d",
                                    use_cache=True,
                                )
                                prediction_data = serialize_prediction_card(
                                    raw_pred, timeframe="1d"
                                )
                            except ValueError:
                                prediction_data = {
                                    "available": False,
                                    "unavailable_reason": "insufficient_data",
                                }
                        else:
                            prediction_data = {
                                "available": False,
                                "unavailable_reason": "no_model",
                            }
                        last_prediction_refresh = now
                        refresh_needed = True
                    except Exception as exc:
                        logger.warning(
                            "SSE prediction refresh failed: instrument=%s error=%s",
                            instrument_key, exc,
                        )
                        yield ServerSentEvent(
                            data=json.dumps({
                                "type":      "error",
                                "component": "prediction",
                                "message":   "Ensemble prediction temporarily unavailable",
                            }),
                            event="error",
                        )

                # ── Pattern refresh ────────────────────────────────────────
                if (now - last_pattern_refresh) >= _PATTERN_REFRESH_SECS:
                    try:
                        pattern_service = PatternDetectionService(db=db, redis=redis)
                        result = await pattern_service.detect_strongest_signal(
                            instrument_key=instrument_key,
                        )
                        pattern_data = result
                        last_pattern_refresh = now
                        refresh_needed = True
                    except Exception as exc:
                        logger.warning(
                            "SSE pattern refresh failed: instrument=%s error=%s",
                            instrument_key, exc,
                        )
                        yield ServerSentEvent(
                            data=json.dumps({
                                "type":      "error",
                                "component": "pattern",
                                "message":   "Pattern detection temporarily unavailable",
                            }),
                            event="error",
                        )

                # ── Sentiment refresh ──────────────────────────────────────
                if (now - last_sentiment_refresh) >= _SENTIMENT_REFRESH_SECS:
                    try:
                        sentiment_service = SentimentAnalysisService(db=db, redis=redis)
                        sentiment_result = await sentiment_service.analyze(
                            instrument_key=instrument_key,
                            symbol=symbol,
                            lookback_hours=lookback_hours,
                        )
                        sentiment_data = sentiment_result.model_dump()
                        last_sentiment_refresh = now
                        refresh_needed = True
                    except Exception as exc:
                        logger.warning(
                            "SSE sentiment refresh failed: instrument=%s error=%s",
                            instrument_key, exc,
                        )
                        yield ServerSentEvent(
                            data=json.dumps({
                                "type":      "error",
                                "component": "sentiment",
                                "message":   "Sentiment analysis temporarily unavailable",
                            }),
                            event="error",
                        )

                # ── Explanation: push path ─────────────────────────────────
                # Drain the watcher queue.  For each notification, verify the
                # suggestion belongs to this instrument before emitting.
                # Emit immediately on match — do not wait for the next cycle.
                while not _explanation_queue.empty():
                    try:
                        push = _explanation_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break

                    push_suggestion_id: str | None = push.get("suggestion_id")
                    if not push_suggestion_id:
                        continue

                    try:
                        sid = UUID(push_suggestion_id)
                    except (ValueError, AttributeError):
                        logger.debug(
                            "SSE explanation push: malformed suggestion_id=%s",
                            push_suggestion_id,
                        )
                        continue

                    try:
                        stmt = select(TradeSuggestion).where(
                            TradeSuggestion.suggestion_id == sid,
                            TradeSuggestion.instrument_key == instrument_key,
                        )
                        row = await db.execute(stmt)
                        matched = row.scalar_one_or_none()
                    except Exception as exc:
                        logger.warning(
                            "SSE explanation push: DB lookup failed "
                            "instrument=%s sid=%s error=%s",
                            instrument_key, push_suggestion_id, exc,
                        )
                        continue

                    if matched is None:
                        # Notification is for a different instrument — silently discard.
                        continue

                    sources: list[dict] = push.get("sources", [])
                    explanation_data = _build_explanation_payload(matched, sources)
                    llm_sse_explanation_pushes_total.labels(path="push").inc()

                    logger.info(
                        "SSE explanation push: emitting immediately "
                        "instrument=%s suggestion=%s model=%s",
                        instrument_key,
                        push_suggestion_id,
                        push.get("model", "unknown"),
                    )

                    push_now = asyncio.get_event_loop().time()
                    yield ServerSentEvent(
                        data=json.dumps(
                            {
                                "prediction":     prediction_data,
                                "pattern":        pattern_data,
                                "sentiment":      sentiment_data,
                                "explanation":    explanation_data,
                                "instrument_key": instrument_key,
                                "emitted_at":     datetime.now(timezone.utc).isoformat(),
                            },
                            default=str,
                        ),
                        event="analysis_update",
                        id=str(int(push_now * 1000)),
                    )

                # ── Explanation: poll path (fallback) ──────────────────────
                # Re-fetches explanation on each periodic tick.  Recovers state
                # on reconnect or if a Redis push notification was missed.
                if (now - last_explanation_refresh) >= _EXPLANATION_REFRESH_SECS:
                    try:
                        fetched = await _fetch_active_explanation(db, instrument_key)
                        if fetched != explanation_data:
                            explanation_data = fetched
                            if fetched is not None:
                                llm_sse_explanation_pushes_total.labels(path="poll").inc()
                        last_explanation_refresh = now
                        refresh_needed = True
                    except Exception as exc:
                        logger.warning(
                            "SSE explanation refresh failed: instrument=%s error=%s",
                            instrument_key, exc,
                        )

                # ── Emit combined update if anything changed ───────────────
                if refresh_needed and (
                    prediction_data is not None
                    or pattern_data is not None
                    or sentiment_data is not None
                    or explanation_data is not None
                ):
                    yield ServerSentEvent(
                        data=json.dumps(
                            {
                                "prediction":     prediction_data,
                                "pattern":        pattern_data,
                                "sentiment":      sentiment_data,
                                "explanation":    explanation_data,
                                "instrument_key": instrument_key,
                                "emitted_at":     datetime.now(timezone.utc).isoformat(),
                            },
                            default=str,
                        ),
                        event="analysis_update",
                        id=str(int(now)),
                    )

                # ── Heartbeat ping (keeps proxies and load balancers alive) ──
                heartbeat_counter += 1
                if heartbeat_counter % int(_HEARTBEAT_SECS) == 0:
                    yield ServerSentEvent(comment="heartbeat")

                await asyncio.sleep(1)

        finally:
            # Cancel the watcher and wait for it to release the pubsub connection
            # cleanly before the generator exits.
            watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher_task
            logger.info(
                "SSE stream teardown complete: user=%s instrument=%s",
                user_id, instrument_key,
            )

    return EventSourceResponse(event_generator())
