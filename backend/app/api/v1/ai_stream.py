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
  - "error"           : transient error info (client should keep connection)
  - heartbeat         : ":ping" comment emitted by sse-starlette every _HEARTBEAT_SECS

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

Architecture — decoupled producers, single consumer
---------------------------------------------------
The connection coroutine (``event_generator``) is a *pure consumer*: it drains
an in-memory ``asyncio.Queue`` and yields whatever ServerSentEvents arrive.  All
data production runs in independent background tasks, one per concern:

  - prediction / pattern / sentiment refreshers  (interval-gated)
  - explanation poll refresher                   (3-stage lookup fallback)
  - explanation watcher                          (Redis PSUBSCRIBE, real-time)

Each producer owns a short-lived ``AsyncSession`` scoped to a single operation
(``async with AsyncSessionLocal()``) and releases the pooled connection the
instant the query completes — it is never held across the producer's sleep, an
LLM call, or the stream's lifetime.  Consequences:

  - No connection-pool leak.  The endpoint takes **no** ``Depends(get_db)``; a
    request-scoped session held for the life of a (minutes-to-hours) SSE stream
    would pin one pooled connection per open view as "idle in transaction" and
    exhaust the pool — the root cause of multi-minute stalls.  Per-operation
    sessions remove this by construction.
  - No component can stall another.  A slow sentiment LLM call delays only the
    sentiment card; prediction, explanation and heartbeats keep flowing.
  - Each refresh is bounded by ``_OPERATION_TIMEOUT_SECS`` purely as a resource
    guarantee — on timeout the ``async with`` unwinds and returns the connection
    to the pool, then the producer waits its full interval before retrying (no
    hot-retry).  DB statements are independently capped by the connection-level
    ``statement_timeout`` (30 s).

Explanation delivery — two complementary paths
----------------------------------------------
  Push path (real-time):
    The explanation_worker publishes to ``cortex:llm:explanation:ready:{id}``
    (or ``cortex:llm:context:ready:{id}``) when generation completes.  The
    watcher task PSUBSCRIBEs the patterns, verifies the message belongs to this
    instrument, updates shared state and enqueues an analysis_update at once.

  Poll path (fallback):
    Every _EXPLANATION_REFRESH_SECS the explanation refresher re-runs the
    3-stage lookup.  This recovers state on reconnect or if a push is missed
    (Redis pub/sub is at-most-once).  A poll never downgrades an available or
    streaming explanation back to a skeleton.

Security:
  - JWT validated before the EventSourceResponse is constructed (HTTP 401 on failure)
  - Token must be an access token (type="access")
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable
from uuid import UUID

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from app.ai.fusion.models import AIInstrumentContext
from app.api.v1.ml_predictions import serialize_prediction_card
from app.core.database import AsyncSessionLocal
from app.core.metrics import llm_sse_explanation_pushes_total
from app.core.redis import RedisChannels, RedisStreams, get_redis
from app.core.security import CortexInvalidTokenError, decode_token
from app.ml.inference.feature_loader import FeatureLoader
from app.models.trade_suggestions import TradeSuggestion
from app.services.pattern_detection_service import PatternDetectionService
from app.services.sentiment_analysis_service import SentimentAnalysisService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["AI Analysis Stream"])

# Refresh intervals (seconds) for each analysis component
_PREDICTION_REFRESH_SECS  = 60    # 1 min  — price-sensitive; Redis-cached so cheap
_PATTERN_REFRESH_SECS     = 300   # 5 min  — matches Redis L2 TTL
_SENTIMENT_REFRESH_SECS   = 300   # 5 min  — news sentiment evolves slowly; L2 TTL is 900 s so
                                  #           the refresher hits cache ~3× before a real re-query
_EXPLANATION_REFRESH_SECS = 30    # 30 s   — fallback poll; primary delivery is push via pub/sub
_HEARTBEAT_SECS           = 15    # sse-starlette ping interval — keeps proxies / LBs alive

# Hard ceiling on any single refresh operation.  This is a resource guarantee,
# not a loop-unblocking band-aid: producers are independent, so a slow component
# never blocks the stream.  The bound exists so a hung downstream call (e.g. a
# stalled sentiment LLM request) cannot hold a pooled connection indefinitely —
# on timeout the per-operation session context unwinds and returns it.
_OPERATION_TIMEOUT_SECS = 25

# Outbound event queue bound.  When a slow client cannot keep up, the oldest
# pending event is evicted before enqueuing a new one (analysis_update is a full
# snapshot, so dropping a superseded one is lossless).
_QUEUE_MAXSIZE = 64

# How long the consumer blocks on the queue before re-checking client disconnect.
_DISCONNECT_POLL_SECS = 1.0

# PSUBSCRIBE patterns — the watcher subscribes to all concurrently.
_EXPLANATION_READY_PATTERN = "cortex:llm:explanation:ready:*"
_CONTEXT_READY_PATTERN     = "cortex:llm:context:ready:*"

# Suggestion lookback window for the "any-status recent signal" stage.
_SUGGESTION_LOOKBACK_DAYS = 7

# Redis distributed lock initial TTL for idempotent context-generation triggers.
# Shorter than the LLM call ceiling so the lock can expire if the worker dies
# mid-call (Gap 6).  The context_worker extends it via its lock heartbeat coroutine.
_CONTEXT_LOCK_INITIAL_TTL_SECS = 45

# On refresher timeout, sleep this short interval before retrying instead of the
# full refresh interval (Gap 9 — avoids a 55s dead zone after a 25s timeout).
_TIMEOUT_RECOVERY_SECS = 10

# Context job stream max length (approximate trim — amortised O(1)).
_STREAM_MAXLEN_CONTEXT = 1000


# ── Shared stream state ─────────────────────────────────────────────────────────

@dataclass
class _StreamState:
    """Latest payload for each analysis component, shared across producer tasks.

    Mutated only between awaits on the single event loop, so no lock is required.
    Each producer updates its own slice; ``_render`` composes the combined
    analysis_update snapshot from whatever is currently present.
    """
    prediction:  dict[str, Any] | None = None
    pattern:     dict[str, Any] | None = None
    sentiment:   dict[str, Any] | None = None
    explanation: dict[str, Any] | None = None

    def render(self, instrument_key: str) -> dict[str, Any]:
        return {
            "prediction":     self.prediction,
            "pattern":        self.pattern,
            "sentiment":      self.sentiment,
            "explanation":    self.explanation,
            "instrument_key": instrument_key,
            "emitted_at":     datetime.now(timezone.utc).isoformat(),
        }


def _should_apply_polled_explanation(
    current: dict[str, Any] | None,
    polled: dict[str, Any] | None,
) -> bool:
    """Decide whether a poll-path explanation should replace the current one.

    A periodic poll must never downgrade a richer real-time state.  Three cases
    where a poll is suppressed:
      1. Payload is identical — no-op.
      2. Poll is a pending skeleton (available=False, failed=False) and we already
         hold a delivered explanation (available=True) or streaming state.
      3. Poll is a pending skeleton and we already hold a confirmed failed state
         (failed=True) — the DLQ already decided; a skeleton would hide the error.
    """
    if current == polled:
        return False
    if polled is not None and not polled.get("available") and not polled.get("failed"):
        if current is not None and (
            current.get("available") or current.get("streaming") or current.get("failed")
        ):
            return False
    # A weak_signal poll must not overwrite a successfully delivered explanation
    # pushed via the bypass path (user clicked "Request AI Explanation" and the
    # worker completed before the next poll cycle).
    if polled is not None and polled.get("weak_signal") and current is not None:
        if current.get("available") or current.get("streaming"):
            return False
    return True


# ── Explanation payload helpers ─────────────────────────────────────────────────

def _build_explanation_payload(
    suggestion: TradeSuggestion,
    sources: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Serialize a TradeSuggestion's explanation fields into the SSE payload shape.

    ``sources`` is populated only on the push path.  On the poll path it will be
    an empty list — inline citations within ``full_explanation`` still provide
    source attribution.

    ``context_type = 'suggestion_explanation'`` lets the frontend render the
    staleness banner ("Based on BUY signal · 6h ago") when the suggestion is
    not currently active.
    """
    return {
        "available":          suggestion.llm_summary is not None,
        "summary":            suggestion.llm_summary,
        "full_explanation":   suggestion.llm_explanation,
        "model":              suggestion.explanation_model,
        "generated_at": (
            suggestion.explanation_generated_at.isoformat()
            if suggestion.explanation_generated_at else None
        ),
        "sources":            sources if sources is not None else [],
        # Context metadata — used by the frontend staleness banner
        "context_type":       "suggestion_explanation",
        "signal_direction":   suggestion.signal_direction,
        "signal_generated_at": (
            suggestion.created_at.isoformat()
            if suggestion.created_at else None
        ),
    }


def _build_context_payload(
    context: AIInstrumentContext,
    sources: list[dict] | None = None,
) -> dict[str, Any]:
    """
    Serialize an AIInstrumentContext row into the SSE payload shape.

    Used when no recent suggestion exists but a cached instrument-level context
    is available.  ``context_type = 'instrument_context'`` tells the frontend
    to render market context copy rather than signal-specific explanation copy.
    """
    return {
        "available":          context.context_summary is not None,
        "summary":            context.context_summary,
        "full_explanation":   context.context_full,
        "model":              context.model_used,
        "generated_at": (
            context.generated_at.isoformat()
            if context.generated_at else None
        ),
        "sources":            sources if sources is not None else [],
        "context_type":       "instrument_context",
        "signal_direction":   None,
        "signal_generated_at": None,
    }


def _build_weak_signal_payload(suggestion: TradeSuggestion) -> dict[str, Any]:
    """Payload emitted when a suggestion's consensus_score is below
    EXPLANATION_CONSENSUS_THRESHOLD.

    ``weak_signal=True`` is the frontend discriminator that renders the
    "confidence below threshold" card with a user-driven refresh button.
    ``suggestion_id`` enables the bypass endpoint call from the panel.
    Mutually exclusive with ``available=True`` and ``failed=True``.
    """
    return {
        "available":           False,
        "failed":              False,
        "weak_signal":         True,
        "suggestion_id":       str(suggestion.suggestion_id),
        "consensus_score":     float(suggestion.consensus_score),
        "summary":             None,
        "full_explanation":    None,
        "model":               None,
        "generated_at":        None,
        "sources":             [],
        "context_type":        "suggestion_explanation",
        "signal_direction":    suggestion.signal_direction,
        "signal_generated_at": (
            suggestion.created_at.isoformat() if suggestion.created_at else None
        ),
    }


async def _fetch_explanation_for_instrument(
    db: AsyncSession,
    instrument_key: str,
    symbol: str | None,
    redis: Any,
    prediction_snapshot: dict | None = None,
) -> dict[str, Any]:
    """
    3-stage lookup that ensures every instrument — including Watchlist items with
    no active signal — always receives a non-null explanation payload.

    Stage 1 — Any-status suggestion within the last 7 days WITH an explanation
        Covers the common case: a signal was generated recently but has since
        expired or been superseded.  The explanation is still informative; the
        frontend renders a staleness label ("Based on BUY signal · 6h ago").
        Active suggestions with no explanation also block here — the worker
        will generate it.  Non-active suggestions with no explanation (were
        superseded before the worker ran) are skipped so Stage 2/3 can serve
        fresh context instead of an eternal skeleton.

    Stage 2 — Non-expired AIInstrumentContext row
        A market-context summary generated for this instrument previously.
        Served from DB cache as long as it has not passed its 2-hour TTL.

    Stage 3 — No valid data → trigger background generation
        Acquires a distributed lock (SET NX EX 120) to prevent duplicate
        requests from concurrent SSE connections on the same instrument, then
        publishes LLM_CONTEXT_PENDING.  Returns ``{available: false}`` so the
        frontend shows the "Generating…" skeleton.

    This function never raises — on errors it falls through to the next stage.
    """
    # ── Stage 1: any-status suggestion within the lookback window ─────────────
    # Only blocks the fall-through when there is something to actually show:
    #   a) suggestion has a generated explanation (llm_summary is not None), OR
    #   b) suggestion is still active — the explanation worker WILL generate it.
    #
    # Non-active suggestions with llm_summary = NULL were superseded before the
    # worker ran and will never be explained.  Fall through to Stage 2/3 so the
    # user gets a fresh market-context summary instead of an eternal skeleton.
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=_SUGGESTION_LOOKBACK_DAYS)
        stmt = (
            select(TradeSuggestion)
            .where(
                TradeSuggestion.instrument_key == instrument_key,
                TradeSuggestion.created_at >= cutoff,
            )
            .order_by(TradeSuggestion.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        suggestion = result.scalar_one_or_none()
        if suggestion is not None:
            has_explanation = suggestion.llm_summary is not None
            is_active       = suggestion.status == "active"
            if has_explanation or is_active:
                # Weak-signal gate: suggestion is active but the correlation engine
                # intentionally skipped enqueueing an explanation job because
                # consensus_score < EXPLANATION_CONSENSUS_THRESHOLD.  The worker
                # will never generate one — return the weak-signal placeholder so
                # the panel shows the "Request AI Explanation" button instead of
                # an eternal generating skeleton.
                if is_active and not has_explanation:
                    from app.core.config import get_settings as _cfg
                    if float(suggestion.consensus_score) < _cfg().EXPLANATION_CONSENSUS_THRESHOLD:
                        return _build_weak_signal_payload(suggestion)

                # Prefer the SSE event store payload: it includes RAG source citations
                # that are not stored in the trade_suggestions table (Gap 5 fix).
                if has_explanation:
                    try:
                        ess_key = RedisStreams.sse_explanation_key(
                            str(suggestion.suggestion_id)
                        )
                        entries = await redis.xrevrange(ess_key, max="+", min="-", count=1)
                        if entries:
                            _, fields = entries[0]
                            return json.loads(fields["data"])
                    except Exception:
                        pass  # fall through to DB payload
                return _build_explanation_payload(suggestion)
            # Non-active + no explanation → fall through to Stage 2/3
            logger.debug(
                "SSE stage-1 skip: instrument=%s suggestion=%s status=%s "
                "has_explanation=False — falling through to context generation",
                instrument_key, suggestion.suggestion_id, suggestion.status,
            )
    except Exception as exc:
        logger.warning(
            "SSE explanation lookup stage-1 failed: instrument=%s error=%s",
            instrument_key, exc,
        )

    # ── Stage 2: non-expired instrument context ────────────────────────────────
    try:
        ctx_stmt = (
            select(AIInstrumentContext)
            .where(
                AIInstrumentContext.instrument_key == instrument_key,
                AIInstrumentContext.expires_at > datetime.now(timezone.utc),
            )
        )
        ctx_result = await db.execute(ctx_stmt)
        cached_ctx = ctx_result.scalar_one_or_none()
        if cached_ctx is not None:
            # Prefer the SSE context event store payload: richer (includes sources)
            try:
                ctx_ess_key = RedisStreams.sse_context_key(instrument_key)
                ctx_entries = await redis.xrevrange(ctx_ess_key, max="+", min="-", count=1)
                if ctx_entries:
                    _, ctx_fields = ctx_entries[0]
                    return json.loads(ctx_fields["data"])
            except Exception:
                pass  # fall through to DB payload
            return _build_context_payload(cached_ctx)
    except Exception as exc:
        logger.warning(
            "SSE explanation lookup stage-2 failed: instrument=%s error=%s",
            instrument_key, exc,
        )

    # ── Stage 3: trigger background context generation ────────────────────────
    # Uses a Redis Stream (XADD) instead of fire-and-forget PUBLISH so the job
    # survives a worker restart (Gap 1 + Gap 8 partial fix for context path).
    # The lock token is stored so the context_worker's heartbeat can do an atomic
    # ownership-check before extending the TTL (Gap 6 fix).
    try:
        lock_key   = f"cortex:instrument_context:generating:{instrument_key}"
        lock_token = secrets.token_hex(16)
        acquired   = await redis.set(
            lock_key, lock_token, nx=True, ex=_CONTEXT_LOCK_INITIAL_TTL_SECS
        )
        if acquired:
            await redis.xadd(
                RedisStreams.CONTEXT_JOBS,
                {
                    "instrument_key":  instrument_key,
                    "symbol":          symbol or "",
                    "prediction_data": (
                        json.dumps(prediction_snapshot, default=str)
                        if prediction_snapshot else ""
                    ),
                    "lock_key":   lock_key,
                    "lock_token": lock_token,
                },
                maxlen=_STREAM_MAXLEN_CONTEXT,
                approximate=True,
            )
            logger.info(
                "SSE triggered instrument context generation: instrument=%s",
                instrument_key,
            )
    except Exception as exc:
        logger.warning(
            "SSE explanation lookup stage-3 failed: instrument=%s error=%s",
            instrument_key, exc,
        )

    # Return the pending-state payload so the frontend renders the skeleton.
    return {
        "available":          False,
        "failed":             False,
        "summary":            None,
        "full_explanation":   None,
        "model":              None,
        "generated_at":       None,
        "sources":            [],
        "context_type":       "instrument_context",
        "signal_direction":   None,
        "signal_generated_at": None,
    }


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
    - `error`: Non-fatal error notification
    - heartbeat: `:ping` comment every 15 seconds

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
) -> EventSourceResponse:
    """Stream combined prediction + pattern + sentiment + explanation analysis via SSE.

    Note: this endpoint deliberately takes **no** ``Depends(get_db)`` session.
    A request-scoped session would be held for the entire (long-lived) stream and
    pin a pooled connection until disconnect.  Each producer task opens its own
    short-lived session per operation instead — see the module docstring.
    """

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

        state = _StreamState()
        events: asyncio.Queue[ServerSentEvent] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)

        # ── Outbound helpers ───────────────────────────────────────────────
        def _enqueue(event: ServerSentEvent) -> None:
            """Non-blocking enqueue; evict the oldest event if the client is slow."""
            if events.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    events.get_nowait()
            events.put_nowait(event)

        def _emit_update() -> None:
            """Enqueue a combined analysis_update from the current shared state."""
            _enqueue(ServerSentEvent(
                data=json.dumps(state.render(instrument_key), default=str),
                event="analysis_update",
                id=str(int(time.time() * 1000)),
            ))

        def _emit_error(component: str, message: str) -> None:
            _enqueue(ServerSentEvent(
                data=json.dumps({"type": "error", "component": component, "message": message}),
                event="error",
            ))

        # ── Per-component refreshers ───────────────────────────────────────
        async def _refresh_prediction() -> None:
            predictor = getattr(request.app.state, "ml_predictor", None)
            if predictor is None:
                state.prediction = {"available": False, "unavailable_reason": "no_model"}
                _emit_update()
                return
            try:
                # Hold the session only for the feature load; release it before
                # the (CPU/GPU-bound) inference call.
                async with AsyncSessionLocal() as db:
                    feat_loader = FeatureLoader(
                        db=db,
                        redis=redis,
                        sequence_length=predictor.sequence_length,
                        n_features=predictor.n_features,
                        feature_names=predictor.feature_names,
                    )
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
                state.prediction = serialize_prediction_card(raw_pred, timeframe="1d")
            except ValueError:
                state.prediction = {"available": False, "unavailable_reason": "insufficient_data"}
            _emit_update()

        async def _refresh_pattern() -> None:
            async with AsyncSessionLocal() as db:
                pattern_service = PatternDetectionService(db=db, redis=redis)
                state.pattern = await pattern_service.detect_strongest_signal(
                    instrument_key=instrument_key,
                )
            _emit_update()

        async def _refresh_sentiment() -> None:
            async with AsyncSessionLocal() as db:
                sentiment_service = SentimentAnalysisService(db=db, redis=redis)
                result = await sentiment_service.analyze(
                    instrument_key=instrument_key,
                    symbol=symbol,
                    lookback_hours=lookback_hours,
                )
            state.sentiment = result.model_dump()
            _emit_update()

        async def _refresh_explanation() -> None:
            async with AsyncSessionLocal() as db:
                fetched = await _fetch_explanation_for_instrument(
                    db,
                    instrument_key,
                    symbol,
                    redis,
                    prediction_snapshot=state.prediction,
                )
            if _should_apply_polled_explanation(state.explanation, fetched):
                state.explanation = fetched
                llm_sse_explanation_pushes_total.labels(path="poll").inc()
                _emit_update()

        async def _refresher(
            name: str,
            interval: float,
            do_refresh: Callable[[], Awaitable[None]],
            *,
            error_component: str | None = None,
        ) -> None:
            """Run ``do_refresh`` immediately, then every ``interval`` seconds.

            Each invocation is bounded by ``_OPERATION_TIMEOUT_SECS`` so a hung
            downstream call can never hold a pooled connection past that ceiling.
            On timeout the producer sleeps only ``_TIMEOUT_RECOVERY_SECS`` before
            retrying — not the full interval, which would leave the stream dark for
            up to 55 s (Gap 9 fix).  On other errors the full interval applies.
            """
            while True:
                timed_out = False
                try:
                    await asyncio.wait_for(do_refresh(), timeout=_OPERATION_TIMEOUT_SECS)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    timed_out = True
                    logger.warning(
                        "SSE %s refresh timed out (instrument=%s) — connection "
                        "released, retrying after %ds",
                        name, instrument_key, _TIMEOUT_RECOVERY_SECS,
                    )
                    if error_component:
                        _emit_error(
                            error_component, f"{name.capitalize()} temporarily unavailable"
                        )
                except Exception as exc:
                    logger.warning(
                        "SSE %s refresh failed: instrument=%s error=%s",
                        name, instrument_key, exc,
                    )
                    if error_component:
                        _emit_error(
                            error_component, f"{name.capitalize()} temporarily unavailable"
                        )
                await asyncio.sleep(_TIMEOUT_RECOVERY_SECS if timed_out else interval)

        # ── Explanation push/stream watcher ────────────────────────────────
        async def _handle_push(channel: str, push: dict[str, Any]) -> None:
            """Route a single PSUBSCRIBE routing signal into shared state + an emit.

            The routing signal carries only {suggestion_id, instrument_key} (and
            optionally failed=True).  Full payload — including RAG source citations
            — lives in the per-suggestion SSE event store, keyed by suggestion_id.
            This eliminates the DB round-trip per push (Gap 10) and the lost-sources
            defect (Gap 5).
            """
            # ── Suggestion explanation ready ───────────────────────────────
            if channel.startswith("cortex:llm:explanation:ready:"):
                # Pre-filter without DB query — routing signal carries instrument_key
                if push.get("instrument_key") != instrument_key:
                    return

                suggestion_id = push.get("suggestion_id", "")
                if not suggestion_id:
                    return

                # Handle DLQ failed state — explanation_worker gave up after MAX_ATTEMPTS
                if push.get("failed"):
                    state.explanation = {
                        "available":           False,
                        "failed":              True,
                        "summary":             None,
                        "full_explanation":    None,
                        "model":               None,
                        "generated_at":        None,
                        "sources":             [],
                        "context_type":        "suggestion_explanation",
                        "signal_direction":    None,
                        "signal_generated_at": None,
                    }
                    llm_sse_explanation_pushes_total.labels(path="push_failed").inc()
                    logger.info(
                        "SSE push: explanation failed state instrument=%s suggestion=%s",
                        instrument_key, suggestion_id,
                    )
                    _emit_update()
                    return

                # Read full payload from the per-suggestion SSE event store
                event_store_key = RedisStreams.sse_explanation_key(suggestion_id)
                try:
                    entries = await redis.xrevrange(
                        event_store_key, max="+", min="-", count=1
                    )
                except Exception as exc:
                    logger.warning(
                        "SSE push: event store read failed instrument=%s suggestion=%s error=%s",
                        instrument_key, suggestion_id, exc,
                    )
                    return

                if not entries:
                    logger.warning(
                        "SSE push: event store empty for suggestion %s — routing signal "
                        "arrived before worker wrote the payload; skipping push",
                        suggestion_id,
                    )
                    return

                try:
                    _, fields = entries[0]
                    payload = json.loads(fields["data"])
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    logger.warning(
                        "SSE push: malformed event store entry for suggestion %s: %s",
                        suggestion_id, exc,
                    )
                    return

                state.explanation = payload
                llm_sse_explanation_pushes_total.labels(path="push").inc()
                logger.info(
                    "SSE push: suggestion explanation ready instrument=%s suggestion=%s",
                    instrument_key, suggestion_id,
                )
                _emit_update()

            # ── Instrument market context ready ────────────────────────────
            elif channel.startswith("cortex:llm:context:ready:"):
                if push.get("instrument_key") != instrument_key:
                    return

                event_store_key = RedisStreams.sse_context_key(instrument_key)
                try:
                    entries = await redis.xrevrange(
                        event_store_key, max="+", min="-", count=1
                    )
                except Exception as exc:
                    logger.warning(
                        "SSE push: context event store read failed instrument=%s error=%s",
                        instrument_key, exc,
                    )
                    return

                if not entries:
                    logger.warning(
                        "SSE push: context event store empty for %s — skipping push",
                        instrument_key,
                    )
                    return

                try:
                    _, fields = entries[0]
                    payload = json.loads(fields["data"])
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    logger.warning(
                        "SSE push: malformed context event store entry for %s: %s",
                        instrument_key, exc,
                    )
                    return

                state.explanation = payload
                llm_sse_explanation_pushes_total.labels(path="push_context").inc()
                logger.info(
                    "SSE push: instrument context ready instrument=%s",
                    instrument_key,
                )
                _emit_update()

        async def _watch_explanations(pubsub: Any) -> None:
            """Route explanation/context ready routing signals into shared state.

            Accepts a *pre-subscribed* pubsub object — the caller called
            ``psubscribe`` before spawning this task, ensuring no ready signal
            can be published in the gap between task creation and subscription
            (Gap 2 fix).

            Uses ``pubsub.listen()`` async generator (Gap 11 fix) instead of
            the tight-poll ``get_message(timeout=0.5)`` loop that spun the event
            loop at 500ms intervals and added unnecessary CPU load.
            """
            logger.debug(
                "SSE explanation watcher started: user=%s instrument=%s",
                user_id, instrument_key,
            )
            try:
                async for msg in pubsub.listen():
                    if msg is None or msg.get("type") != "pmessage":
                        continue
                    try:
                        data = json.loads(msg["data"])
                    except (json.JSONDecodeError, TypeError):
                        logger.debug(
                            "SSE explanation watcher: unparseable message data: %.80s",
                            msg.get("data", ""),
                        )
                        continue
                    await _handle_push(msg.get("channel", ""), data)
            except asyncio.CancelledError:
                raise
            finally:
                with contextlib.suppress(Exception):
                    await pubsub.punsubscribe(
                        _EXPLANATION_READY_PATTERN,
                        _CONTEXT_READY_PATTERN,
                    )
                    await pubsub.aclose()
                logger.debug(
                    "SSE explanation watcher stopped: user=%s instrument=%s",
                    user_id, instrument_key,
                )

        # ── Subscribe BEFORE spawning producers (Gap 2 fix) ───────────────
        # psubscribe must complete before any producer task is created so that
        # a fast explanation worker cannot fire LLM_EXPLANATION_READY between
        # task creation and the coroutine's first await inside the old version.
        pubsub = redis.pubsub()
        await pubsub.psubscribe(
            _EXPLANATION_READY_PATTERN,
            _CONTEXT_READY_PATTERN,
        )
        logger.debug(
            "SSE psubscribe complete: user=%s instrument=%s", user_id, instrument_key
        )

        # ── Spawn producers ────────────────────────────────────────────────
        producers = [
            asyncio.create_task(_watch_explanations(pubsub)),
            asyncio.create_task(_refresher(
                "prediction", _PREDICTION_REFRESH_SECS, _refresh_prediction,
                error_component="prediction",
            )),
            asyncio.create_task(_refresher(
                "pattern", _PATTERN_REFRESH_SECS, _refresh_pattern,
                error_component="pattern",
            )),
            asyncio.create_task(_refresher(
                "sentiment", _SENTIMENT_REFRESH_SECS, _refresh_sentiment,
                error_component="sentiment",
            )),
            asyncio.create_task(_refresher(
                "explanation", _EXPLANATION_REFRESH_SECS, _refresh_explanation,
            )),
        ]

        # ── Consume + yield ────────────────────────────────────────────────
        try:
            # Tell the client to retry after 5 seconds on disconnect.
            yield ServerSentEvent(comment="stream-init", retry=5000)

            while True:
                if await request.is_disconnected():
                    logger.info(
                        "SSE stream closed: user=%s instrument=%s",
                        user_id, instrument_key,
                    )
                    break
                # Block on the queue but wake periodically to re-check disconnect.
                try:
                    event = await asyncio.wait_for(
                        events.get(), timeout=_DISCONNECT_POLL_SECS
                    )
                except asyncio.TimeoutError:
                    continue
                yield event
        finally:
            for task in producers:
                task.cancel()
            await asyncio.gather(*producers, return_exceptions=True)
            logger.info(
                "SSE stream teardown complete: user=%s instrument=%s",
                user_id, instrument_key,
            )

    return EventSourceResponse(event_generator(), ping=_HEARTBEAT_SECS)


# ── REST explanation fallback ──────────────────────────────────────────────────

@router.get(
    "/explanation",
    summary="Current AI explanation for an instrument (REST fallback)",
    description=(
        "Stateless REST alternative to the SSE stream's explanation payload. "
        "Runs the same 3-stage lookup used by the SSE explanation refresher: "
        "(1) recent suggestion with explanation, (2) cached instrument context, "
        "(3) trigger background generation and return a pending skeleton. "
        "Always returns HTTP 200 — `available: false` means 'generating, poll again'. "
        "Accepts auth via Bearer header (standard REST) or `token` query param "
        "(parity with the SSE endpoint for clients that cannot set headers)."
    ),
    responses={
        200: {"description": "Explanation payload — check `available` field for readiness"},
        401: {"description": "Invalid or expired JWT token"},
        408: {"description": "Lookup timed out — retry shortly"},
    },
)
async def get_explanation(
    request: Request,
    instrument_key: str = Query(
        ...,
        min_length=1,
        max_length=100,
        description="NSE instrument key, e.g. NSE_EQ|INE296A01032",
    ),
    symbol: str | None = Query(
        None,
        description="NSE trading symbol for news context (e.g. BAJFINANCE)",
    ),
    token: str | None = Query(
        None,
        description="JWT access token (only required when Authorization header is absent)",
    ),
) -> JSONResponse:
    """Return the current explanation payload for an instrument via a single REST call.

    This endpoint is the reliability backbone for the AI Explanation Panel: it
    activates when the SSE proxy is unavailable or degraded, and on component
    remount (React Query cache survives unmount, so the explanation is instantly
    available on modal reopen without a network request).

    Auth: identical to the SSE stream — accepts the JWT either as a Bearer token
    in the Authorization header (standard REST) or as a `token` query param
    (EventSource parity, for clients that cannot set headers).

    The 3-stage lookup is identical to the SSE explanation refresher and will
    trigger background context generation (Stage 3) when no cached data exists,
    so opening the Detail Pane always initiates generation regardless of whether
    the SSE stream is healthy.
    """
    # ── Auth: accept Bearer header or query-param token (EventSource parity) ──
    raw_token: str | None = token
    if not raw_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            raw_token = auth_header[7:]
    if not raw_token:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "missing_token", "message": "Authentication required"},
        )
    try:
        decode_token(raw_token, expected_type="access")
    except CortexInvalidTokenError as exc:
        logger.warning(
            "REST explanation auth failed: instrument=%s error=%s", instrument_key, exc
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_token", "message": str(exc)},
        )

    # ── 3-stage explanation lookup ─────────────────────────────────────────────
    _pending_skeleton: dict[str, Any] = {
        "available":           False,
        "failed":              False,
        "summary":             None,
        "full_explanation":    None,
        "model":               None,
        "generated_at":        None,
        "sources":             [],
        "context_type":        "instrument_context",
        "signal_direction":    None,
        "signal_generated_at": None,
    }
    try:
        async with AsyncSessionLocal() as db:
            redis = get_redis()
            payload = await asyncio.wait_for(
                _fetch_explanation_for_instrument(
                    db=db,
                    instrument_key=instrument_key,
                    symbol=symbol,
                    redis=redis,
                ),
                timeout=_OPERATION_TIMEOUT_SECS,
            )
        return JSONResponse(content=payload)
    except asyncio.TimeoutError:
        logger.warning(
            "REST explanation lookup timed out: instrument=%s", instrument_key
        )
        return JSONResponse(
            status_code=status.HTTP_408_REQUEST_TIMEOUT,
            content=_pending_skeleton,
        )
    except Exception as exc:
        logger.error(
            "REST explanation lookup failed: instrument=%s error=%s",
            instrument_key, exc, exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "internal_error", "message": "An unexpected error occurred"},
        )


# ── On-demand explanation bypass ───────────────────────────────────────────────

# Per-suggestion debounce prevents quota abuse from rapid re-clicks.
# 60 s matches the explanation worker's typical generation latency ceiling.
_BYPASS_DEBOUNCE_TTL_SECS = 60
_BYPASS_DEBOUNCE_KEY = "cortex:explanation:bypass:{suggestion_id}"


@router.post(
    "/explanation/{suggestion_id}/request",
    summary="Request on-demand LLM explanation for a weak-signal suggestion",
    status_code=202,
    responses={
        202: {"description": "Explanation job enqueued; result delivered via SSE push"},
        400: {"description": "Malformed suggestion_id"},
        401: {"description": "Invalid or expired JWT token"},
        404: {"description": "Suggestion not found or not active"},
        409: {"description": "Explanation already exists for this suggestion"},
        429: {"description": "Rate-limited — wait 60 s before retrying"},
    },
)
async def request_explanation(
    suggestion_id: str,
    token: str = Query(..., description="JWT access token"),
) -> JSONResponse:
    """Enqueue an on-demand LLM explanation job for a suggestion gated by the
    consensus threshold.

    The correlation engine skips enqueueing explanation jobs when
    ``consensus_score < EXPLANATION_CONSENSUS_THRESHOLD``.  This endpoint
    lets the user override that gate for a specific suggestion.  The result
    is delivered asynchronously via the SSE push path
    (``cortex:llm:explanation:ready:{suggestion_id}``).

    A 60-second per-suggestion debounce key is held in Redis for the duration
    of the job so rapid re-clicks don't flood the explanation worker.  The key
    is deleted immediately on any error path so a transient failure doesn't
    silently block retries.

    Auth: JWT access token passed as a query parameter (same as the SSE stream
    endpoint — EventSource cannot send headers).
    """
    try:
        decode_token(token, expected_type="access")
    except CortexInvalidTokenError as exc:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"error": "invalid_token", "message": str(exc)},
        )

    try:
        _uuid = UUID(suggestion_id)
    except (ValueError, AttributeError):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": "invalid_suggestion_id"},
        )

    redis = get_redis()
    debounce_key = _BYPASS_DEBOUNCE_KEY.format(suggestion_id=suggestion_id)

    acquired = await redis.set(debounce_key, "1", nx=True, ex=_BYPASS_DEBOUNCE_TTL_SECS)
    if not acquired:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error":   "rate_limited",
                "message": f"An explanation request for this suggestion is already in "
                           f"progress. Wait {_BYPASS_DEBOUNCE_TTL_SECS}s before retrying.",
            },
        )

    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(TradeSuggestion).where(
                    TradeSuggestion.suggestion_id == _uuid
                )
            )
            suggestion = result.scalar_one_or_none()

        if suggestion is None:
            await redis.delete(debounce_key)
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "suggestion_not_found"},
            )

        if suggestion.status != "active":
            await redis.delete(debounce_key)
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"error": "suggestion_not_active", "status": suggestion.status},
            )

        if suggestion.llm_summary is not None:
            await redis.delete(debounce_key)
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"error": "explanation_already_exists"},
            )

    except Exception as exc:
        await redis.delete(debounce_key)
        logger.error(
            "on-demand bypass: DB check failed suggestion=%s: %s",
            suggestion_id, exc,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "internal_error"},
        )

    try:
        await redis.xadd(
            RedisStreams.EXPLANATION_JOBS,
            {
                "suggestion_id":  suggestion_id,
                "id":             str(suggestion.id),
                "instrument_key": suggestion.instrument_key,
            },
            maxlen=5000,
            approximate=True,
        )
        logger.info(
            "on-demand bypass enqueued: suggestion=%s instrument=%s",
            suggestion_id, suggestion.instrument_key,
        )
    except Exception as exc:
        await redis.delete(debounce_key)
        logger.error(
            "on-demand bypass: XADD failed suggestion=%s: %s",
            suggestion_id, exc,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "queue_error"},
        )

    return JSONResponse(
        status_code=202,
        content={
            "queued":        True,
            "suggestion_id": suggestion_id,
            "message":       "Explanation queued. Watch the SSE stream for delivery.",
        },
    )
