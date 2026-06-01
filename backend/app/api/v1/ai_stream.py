"""
AI Analysis Stream — Server-Sent Events (SSE)
=============================================
Real-time SSE endpoint that streams pattern detection + sentiment analysis
updates to connected frontend clients.

Endpoint:
  GET /api/v1/ai/stream?instrument_key=...&token=<jwt>

Why query-param auth:
  Browser's EventSource API does not support custom request headers.
  The JWT is passed as a query parameter and validated server-side before
  any data is streamed. The Next.js proxy (frontend/src/app/api/v1/ai/stream)
  converts the Authorization header from the frontend into the query param.

Event types:
  - "analysis_update" : combined pattern + sentiment payload
  - "heartbeat"       : empty comment ping every 15s (keeps proxies alive)
  - "error"           : transient error info (client should keep connection)

Reconnect:
  SSE clients auto-reconnect on disconnect (browser default: 3s).
  The `retry` field in initial event sets this to 5000ms.

Security:
  - JWT validated before first event is yielded
  - Token must be an access token (type="access")
  - Connection closed immediately on invalid/expired token
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from app.api.deps import get_db
from app.api.v1.ml_predictions import serialize_prediction_card
from app.core.redis import get_redis
from app.core.security import CortexInvalidTokenError, decode_token
from app.ml.inference.feature_loader import FeatureLoader
from app.services.pattern_detection_service import PatternDetectionService
from app.services.sentiment_analysis_service import SentimentAnalysisService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["AI Analysis Stream"])

# How often the server re-fetches and emits updated analysis (seconds)
_PREDICTION_REFRESH_SECS = 60  # 1 minute — price-level sensitive; Redis-cached so cheap
_PATTERN_REFRESH_SECS = 300    # 5 minutes (matches Redis L2 TTL)
_SENTIMENT_REFRESH_SECS = 120  # 2 minutes
_HEARTBEAT_SECS = 15           # Keeps proxy/load-balancer connections alive


@router.get(
    "/stream",
    summary="Server-Sent Events stream for AI Analysis Cards",
    description="""
    Establishes a persistent SSE connection and streams real-time analysis
    updates for the specified instrument.

    **Authentication**: Pass your JWT access token as the `token` query parameter.
    (The browser EventSource API does not support custom headers.)

    **Events**:
    - `analysis_update`: Full combined payload (pattern + sentiment)
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
    """Stream combined pattern + sentiment analysis updates via SSE."""

    # Validate JWT before opening stream — fail fast with HTTP 401
    try:
        payload = decode_token(token, expected_type="access")
        user_id: str = payload.sub
    except CortexInvalidTokenError as exc:
        logger.warning("SSE auth failed: instrument=%s error=%s", instrument_key, exc)
        # EventSourceResponse can't return 401 once established; return JSON error directly
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_token", "message": "Invalid or expired access token"},
        )

    redis = get_redis()

    async def event_generator() -> AsyncGenerator[ServerSentEvent, None]:
        logger.info(
            "SSE stream opened: user=%s instrument=%s", user_id, instrument_key
        )

        # Track when we last refreshed each data source
        last_prediction_refresh = 0.0
        last_pattern_refresh = 0.0
        last_sentiment_refresh = 0.0
        prediction_data: dict[str, Any] | None = None
        pattern_data: dict[str, Any] | None = None
        sentiment_data: dict[str, Any] | None = None
        heartbeat_counter = 0

        # Tell the client to retry after 5 seconds on disconnect
        yield ServerSentEvent(
            comment="stream-init",
            retry=5000,
        )

        while True:
            # ── Disconnect check ───────────────────────────────────────────────
            if await request.is_disconnected():
                logger.info(
                    "SSE stream closed: user=%s instrument=%s", user_id, instrument_key
                )
                break

            now = asyncio.get_event_loop().time()
            refresh_needed = False

            # ── Ensemble prediction refresh ────────────────────────────────────
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
                            tabular, sequence, current_price, vol = await feat_loader.load_features(
                                symbol=instrument_key,
                                timeframe="1d",
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
                            prediction_data = serialize_prediction_card(raw_pred, timeframe="1d")
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
                            "type": "error",
                            "component": "prediction",
                            "message": "Ensemble prediction temporarily unavailable",
                        }),
                        event="error",
                    )

            # ── Pattern refresh ────────────────────────────────────────────────
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
                            "type": "error",
                            "component": "pattern",
                            "message": "Pattern detection temporarily unavailable",
                        }),
                        event="error",
                    )

            # ── Sentiment refresh ──────────────────────────────────────────────
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
                            "type": "error",
                            "component": "sentiment",
                            "message": "Sentiment analysis temporarily unavailable",
                        }),
                        event="error",
                    )

            # ── Emit combined update if anything changed ───────────────────────
            if refresh_needed and (prediction_data is not None or pattern_data is not None or sentiment_data is not None):
                payload_dict = {
                    "prediction": prediction_data,
                    "pattern":    pattern_data,
                    "sentiment":  sentiment_data,
                    "instrument_key": instrument_key,
                    "emitted_at": datetime.now(timezone.utc).isoformat(),
                }
                yield ServerSentEvent(
                    data=json.dumps(payload_dict, default=str),
                    event="analysis_update",
                    id=str(int(now)),
                )

            # ── Heartbeat ping (keeps proxies and load balancers alive) ────────
            heartbeat_counter += 1
            if heartbeat_counter % int(_HEARTBEAT_SECS) == 0:
                yield ServerSentEvent(comment="heartbeat")

            await asyncio.sleep(1)

    return EventSourceResponse(event_generator())
