# PATH: backend/app/api/v1/upstox.py
"""
Cortex AI — Upstox Router
===========================
Candle data proxy and unified real-time market-feed WebSocket endpoint.

Candle endpoints:
  - Forward requests to Upstox v3 API using path-param URL format
  - Normalise response to { status, data: { candles: [] } }
  - Cache in Redis (30 s intraday, 300 s historical)

Market-feed WebSocket (/upstox/market-feed/ws):
  - Authenticated via JWT query param (?token=<jwt>)
  - Client sends  {type:"sub",   instrument_keys:[...]} to subscribe
  - Client sends  {type:"unsub", instrument_keys:[...]} to unsubscribe
  - Server sends  {type:"ltpc",  instrument_key, ltp, cp, ts} on every tick
  - Server sends  {type:"ping",  ts} heartbeat every 30 s
  - Uses MarketFeedService (singleton) + Redis fan-out for horizontal scale

All HTTP endpoints require JWT authentication.
"""
import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from jose import JWTError, jwt
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.redis import CacheService, RedisChannels, get_cache_service, get_redis
from app.core.security import get_current_user_id
from app.schemas.market_feed import (
    ErrorEvent,
    HeartbeatEvent,
    LtpcMessage,
    SubCommand,
    UnsubCommand,
    SubscribedEvent,
    UnsubscribedEvent,
)
from app.services.data_ingestion import DataIngestionService
from app.services.market_feed import MarketFeedService, get_market_feed_service
from app.exceptions import UpstoxRateLimitError
from app.services.upstox_client import UpstoxClient, get_upstox_client
from app.schemas.upstox import IngestRequest, IngestResponse

logger = logging.getLogger(__name__)
settings = get_settings()

router    = APIRouter(dependencies=[Depends(get_current_user_id)])
ws_router = APIRouter()

_WS_HEARTBEAT_S   = 30.0
_WS_QUEUE_MAXSIZE = 256


# ── Helpers ────────────────────────────────────────────────────────────────────

def _candles_response(
    raw: dict[str, Any],
    instrument_key: str,
    unit: str,
    interval: int | str,
) -> dict[str, Any]:
    """Normalise any Upstox candle response to { status, data: { candles } }."""
    candles = raw.get("data", {})
    if isinstance(candles, dict):
        candles = candles.get("candles", [])
    if not isinstance(candles, list):
        candles = []
    return {
        "status": "success",
        "data": {"candles": candles},
        "instrument_key": instrument_key,
        "interval": interval,
        "unit": unit,
    }


def _empty_candles_response(
    instrument_key: str,
    unit: str,
    interval: int | str,
    message: str = "Unable to fetch candles from Upstox",
) -> dict[str, Any]:
    return {
        "status": "error",
        "data": {"candles": []},
        "instrument_key": instrument_key,
        "interval": interval,
        "unit": unit,
        "message": message,
    }


# ── Candle ingestion ───────────────────────────────────────────────────────────

@router.post("/ingest", response_model=IngestResponse)
@limiter.limit("5/minute")
async def ingest_candles(
    request: Request,
    body: IngestRequest,
    background_tasks: BackgroundTasks,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
) -> IngestResponse:
    ingestion_service: DataIngestionService = request.app.state.ingestion_service

    async def _run_ingest() -> None:
        for key in body.instrument_keys:
            try:
                count = await ingestion_service.fetch_and_store_candles(
                    session=session,
                    instrument_key=key,
                    timeframe=body.timeframe,
                    from_date=body.from_date,
                    to_date=body.to_date,
                )
                logger.info("Ingested %d candles for %s", count, key)
            except Exception:
                logger.exception("Ingestion failed for %s", key)

    background_tasks.add_task(_run_ingest)
    return IngestResponse(
        status="accepted",
        message=f"Ingestion queued for {len(body.instrument_keys)} instruments",
        instrument_count=len(body.instrument_keys),
    )


# ── Candle endpoints ───────────────────────────────────────────────────────────

@router.get("/candles/intraday")
@limiter.limit("200/minute")
async def get_intraday_candles(
    request: Request,
    instrument_key: str = Query(..., description="Upstox instrument key, e.g. NSE_EQ|INE002A01018"),
    interval: int = Query(1, ge=1, le=60, description="Candle interval value"),
    unit: str = Query("minutes", pattern="^(minutes|hours)$", description="Candle unit"),
    user_id: str = Depends(get_current_user_id),
    upstox: UpstoxClient = Depends(get_upstox_client),
    cache: CacheService = Depends(get_cache_service),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Intraday candles for the current trading session with DB-first strategy.

    Data Flow:
      1. Check Redis cache (30s TTL)
      2. Check database for recent intraday data
      3. If DB data is fresh (< 5 min old) → return from DB
      4. Otherwise → fetch from Upstox API
      5. Store fetched data in DB for future requests
      6. Cache result in Redis
    """
    from app.services.candle_service import candle_service

    cache_key = f"candles:intraday:{instrument_key}:{unit}:{interval}"
    cached = await cache.get(cache_key)
    if cached:
        return cached

    db_candles, is_from_db = await candle_service.get_intraday_candles(
        db, instrument_key, unit, interval
    )
    if is_from_db and db_candles:
        result = {
            "status": "success",
            "data": {"candles": db_candles},
            "instrument_key": instrument_key,
            "interval": interval,
            "unit": unit,
        }
        await cache.set(cache_key, result, ttl=30)
        return result

    try:
        path = f"historical-candle/intraday/{instrument_key}/{unit}/{interval}"
        raw = await upstox.get(path)
        result = _candles_response(raw, instrument_key, unit, interval)
        if result.get("status") == "success" and result.get("data", {}).get("candles"):
            await candle_service.store_candles(db, instrument_key, unit, interval, result["data"]["candles"])
        await cache.set(cache_key, result, ttl=30)
        return result
    except UpstoxRateLimitError:
        logger.warning("Upstox rate limit exhausted for intraday %s %s/%s", instrument_key, unit, interval)
        return JSONResponse(
            status_code=429,
            content=_empty_candles_response(instrument_key, unit, interval, "Rate limit exceeded — please retry"),
            headers={"Retry-After": "2"},
        )
    except Exception:
        logger.exception("Intraday candles error for %s", instrument_key)
        return _empty_candles_response(instrument_key, unit, interval)


@router.get("/candles/historical")
@limiter.limit("200/minute")
async def get_historical_candles(
    request: Request,
    instrument_key: str = Query(..., description="Upstox instrument key, e.g. NSE_EQ|INE002A01018"),
    interval: int = Query(1, ge=1, description="Candle interval value"),
    unit: str = Query(
        "days",
        pattern="^(minutes|hours|days|weeks|months)$",
        description="Candle unit — maps directly to Upstox v3 interval_unit path param",
    ),
    from_date: str = Query(..., description="YYYY-MM-DD (inclusive)"),
    to_date: str = Query(..., description="YYYY-MM-DD (inclusive)"),
    user_id: str = Depends(get_current_user_id),
    upstox: UpstoxClient = Depends(get_upstox_client),
    cache: CacheService = Depends(get_cache_service),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Historical OHLCV candles for a date range with intelligent gap-filling.

    Data Flow:
      1. Check Redis cache (5 min TTL)
      2. Check database for historical data
      3. If DB has gaps → fetch missing dates from Upstox API and merge
      4. If no DB data → fetch complete range from Upstox API
      5. Store fetched data in DB for future requests
      6. Cache complete result in Redis
    """
    from app.services.candle_service import candle_service
    from collections import Counter as PyCounter

    cache_key = f"candles:historical:{instrument_key}:{unit}:{interval}:{from_date}:{to_date}"
    cached = await cache.get(cache_key)
    if cached:
        return cached

    db_candles, is_from_db, missing_ranges = await candle_service.get_historical_candles(
        db, instrument_key, unit, interval, from_date, to_date
    )

    if is_from_db and db_candles:
        if missing_ranges:
            all_candles = db_candles
            for gap in missing_ranges:
                try:
                    gap_from = gap["from_date"]
                    gap_to   = gap["to_date"]
                    path = f"historical-candle/{instrument_key}/{unit}/{interval}/{gap_to}/{gap_from}"
                    raw = await upstox.get(path)
                    gap_candles = raw.get("data", {}).get("candles", [])
                    if gap_candles:
                        all_candles = candle_service.merge_candles(all_candles, gap_candles)
                        await candle_service.store_candles(db, instrument_key, unit, interval, gap_candles)
                except UpstoxRateLimitError:
                    logger.warning("Rate limit hit during gap-fill for %s — returning partial DB data", instrument_key)
                    break  # stop gap-filling; return whatever DB data we have
                except Exception as gap_error:
                    logger.warning("Failed to fetch gap %s: %s", gap, gap_error)

            timestamps = [c[0] for c in all_candles]
            if len(timestamps) != len(set(timestamps)):
                logger.warning("Duplicate timestamps detected after merge for %s", instrument_key)

            result = {
                "status": "success",
                "data": {"candles": all_candles},
                "instrument_key": instrument_key,
                "interval": interval,
                "unit": unit,
            }
            await cache.set(cache_key, result, ttl=300)
            return result

        result = {
            "status": "success",
            "data": {"candles": db_candles},
            "instrument_key": instrument_key,
            "interval": interval,
            "unit": unit,
        }
        await cache.set(cache_key, result, ttl=300)
        return result

    try:
        path = f"historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
        raw = await upstox.get(path)
        result = _candles_response(raw, instrument_key, unit, interval)
        if result.get("status") == "success" and result.get("data", {}).get("candles"):
            await candle_service.store_candles(db, instrument_key, unit, interval, result["data"]["candles"])
        await cache.set(cache_key, result, ttl=300)
        return result
    except UpstoxRateLimitError:
        logger.warning("Upstox rate limit exhausted for historical %s %s/%s", instrument_key, unit, interval)
        return JSONResponse(
            status_code=429,
            content=_empty_candles_response(instrument_key, unit, interval, "Rate limit exceeded — please retry"),
            headers={"Retry-After": "2"},
        )
    except Exception:
        logger.exception("Historical candles error for %s", instrument_key)
        return _empty_candles_response(instrument_key, unit, interval)


# ── LTP quote ──────────────────────────────────────────────────────────────────

@router.get("/ltp")
@limiter.limit("120/minute")
async def get_ltp_quote(
    request: Request,
    instrument_key: str = Query(..., description="Upstox instrument key"),
    user_id: str = Depends(get_current_user_id),
    upstox: UpstoxClient = Depends(get_upstox_client),
    cache: CacheService = Depends(get_cache_service),
) -> dict[str, Any]:
    """Single-instrument LTP quote. Use market-feed WebSocket for continuous prices."""
    cache_key = f"ltp:{instrument_key}"
    cached = await cache.get(cache_key)
    if cached:
        return cached
    try:
        raw = await upstox.get(f"market-quote/ltp?instrument_key={instrument_key}")
        data = raw.get("data", {}).get(instrument_key, {})
        result = {
            "last_price": data.get("last_price"),
            "prev_close_price": data.get("cp"),
            "instrument_key": instrument_key,
        }
        await cache.set(cache_key, result, ttl=settings.CACHE_TTL_LIVE_QUOTE)
        return result
    except Exception:
        logger.exception("LTP quote error for %s", instrument_key)
        return {"last_price": None, "prev_close_price": None, "instrument_key": instrument_key}


# ── Market-feed WebSocket ──────────────────────────────────────────────────────

def _verify_ws_token(token: str) -> str | None:
    """Decode JWT and return user_id, or None on failure."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload.get("sub")
    except JWTError:
        return None


async def _redis_listener(
    pubsub: Any,
    queue: asyncio.Queue[dict[str, Any]],
    subscribed_keys: set[str],
) -> None:
    """Subscribe to Redis market-feed channel, forward matching ticks to queue."""
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                data = json.loads(message["data"])
            except (json.JSONDecodeError, TypeError):
                continue
            if data.get("instrument_key") not in subscribed_keys:
                continue
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # Drop oldest tick to make room for the fresh one
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                queue.put_nowait(data)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("Market-feed Redis listener error")


async def _sender(
    websocket: WebSocket,
    queue: asyncio.Queue[dict[str, Any]],
) -> None:
    """Drain the outbound queue and write frames to the WebSocket client."""
    try:
        while True:
            message = await queue.get()
            await websocket.send_json(message)
    except asyncio.CancelledError:
        pass
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Market-feed sender error")


async def _heartbeat(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Enqueue a server ping every 30 s."""
    try:
        while True:
            await asyncio.sleep(_WS_HEARTBEAT_S)
            try:
                queue.put_nowait(HeartbeatEvent().model_dump())
            except asyncio.QueueFull:
                pass
    except asyncio.CancelledError:
        pass


@ws_router.websocket("/market-feed/ws")
async def websocket_market_feed(
    websocket: WebSocket,
    token: str | None = Query(None, description="JWT access token"),
) -> None:
    """
    Unified real-time ltpc market-feed WebSocket.

    Authentication:
      Pass JWT as ?token=<jwt> query param.
      Unauthenticated connections are rejected immediately.

    Client → Server:
      {type: "sub",   instrument_keys: [...]}  — start receiving ltpc ticks
      {type: "unsub", instrument_keys: [...]}  — stop receiving ltpc ticks
      {type: "pong"}                           — heartbeat reply (optional)

    Server → Client:
      {type: "ltpc",         instrument_key, ltp, cp, ts}
      {type: "subscribed",   instrument_keys, ts}
      {type: "unsubscribed", instrument_keys, ts}
      {type: "ping",         ts}   — server heartbeat every 30 s
      {type: "error",        code, message}   — validation error (connection stays open)
    """
    await websocket.accept()

    # ── Auth ───────────────────────────────────────────────────────────────────
    if not token:
        await websocket.send_json(ErrorEvent(code="AUTH_REQUIRED", message="Missing token").model_dump())
        await websocket.close(code=4001)
        return

    user_id = _verify_ws_token(token)
    if not user_id:
        await websocket.send_json(ErrorEvent(code="AUTH_REQUIRED", message="Invalid or expired token").model_dump())
        await websocket.close(code=4001)
        return

    market_feed: MarketFeedService = websocket.app.state.market_feed_service
    redis: Redis = get_redis()

    conn_id = uuid.uuid4().hex
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_WS_QUEUE_MAXSIZE)
    subscribed_keys: set[str] = set()

    # Dedicated pub/sub connection for this client (redis-py requires a fresh pubsub per coroutine)
    pubsub = redis.pubsub()
    await pubsub.subscribe(RedisChannels.MARKET_FEED_LTPC)

    listener_task  = asyncio.create_task(_redis_listener(pubsub, queue, subscribed_keys), name=f"mf_listener_{conn_id}")
    sender_task    = asyncio.create_task(_sender(websocket, queue), name=f"mf_sender_{conn_id}")
    heartbeat_task = asyncio.create_task(_heartbeat(queue), name=f"mf_heartbeat_{conn_id}")

    logger.info("Market-feed WS connected: user=%s conn=%s", user_id, conn_id)

    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
            except asyncio.TimeoutError:
                # Client hasn't sent anything in 60 s — connection is healthy (sender handles heartbeats)
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    ErrorEvent(code="INVALID_MESSAGE", message="Message must be valid JSON").model_dump()
                )
                continue

            msg_type = data.get("type")

            if msg_type == "sub":
                try:
                    cmd = SubCommand(**data)
                except Exception as exc:
                    await websocket.send_json(
                        ErrorEvent(code="INVALID_KEY", message=str(exc)).model_dump()
                    )
                    continue

                new_keys = [k for k in cmd.instrument_keys if k not in subscribed_keys]
                if new_keys:
                    subscribed_keys.update(new_keys)
                    await market_feed.subscribe_many(new_keys)

                await websocket.send_json(
                    SubscribedEvent(instrument_keys=cmd.instrument_keys).model_dump()
                )

            elif msg_type == "unsub":
                try:
                    cmd = UnsubCommand(**data)
                except Exception as exc:
                    await websocket.send_json(
                        ErrorEvent(code="INVALID_KEY", message=str(exc)).model_dump()
                    )
                    continue

                removed = [k for k in cmd.instrument_keys if k in subscribed_keys]
                if removed:
                    subscribed_keys.difference_update(removed)
                    await market_feed.unsubscribe_many(removed)

                await websocket.send_json(
                    UnsubscribedEvent(instrument_keys=cmd.instrument_keys).model_dump()
                )

            elif msg_type == "pong":
                pass  # Heartbeat reply — no action needed

            else:
                await websocket.send_json(
                    ErrorEvent(
                        code="INVALID_MESSAGE",
                        message=f"Unknown message type: {msg_type!r}",
                    ).model_dump()
                )

    except WebSocketDisconnect:
        logger.info("Market-feed WS disconnected: user=%s conn=%s", user_id, conn_id)
    except Exception:
        logger.exception("Market-feed WS error: conn=%s", conn_id)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        listener_task.cancel()
        sender_task.cancel()
        heartbeat_task.cancel()
        await asyncio.gather(listener_task, sender_task, heartbeat_task, return_exceptions=True)

        # Drain the queue to free memory
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Unsubscribe instruments from the feed (decrements ref counts)
        if subscribed_keys:
            await market_feed.unsubscribe_many(list(subscribed_keys))

        try:
            await pubsub.unsubscribe(RedisChannels.MARKET_FEED_LTPC)
            await pubsub.aclose()
        except Exception:
            pass

        logger.info("Market-feed WS cleanup complete: conn=%s", conn_id)
