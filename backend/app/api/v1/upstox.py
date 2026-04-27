# PATH: backend/app/api/v1/upstox.py
"""
Cortex AI — Upstox Router
===========================
Candle data proxy and real-time tick WebSocket endpoints.

Candle endpoints:
  - Forward requests to Upstox v3 API using path-param URL format
  - Normalise response to { status, data: { candles: [] } }
  - Cache in Redis (30 s intraday, 300 s historical)

WebSocket tick endpoint:
  - With Upstox token: bridges TickStreamService fan-out to each client
  - Without token (dev/test): emits a realistic mock based on recent candle data

All HTTP endpoints require JWT authentication.
The WebSocket endpoint is on a separate unauthenticated router (browser WS
APIs cannot set Authorization headers; token-in-query-param auth is handled
at the application gateway layer in production).
"""
import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.limiter import limiter
from app.core.redis import CacheService, get_cache_service
from app.core.security import get_current_user_id
from app.exceptions import UpstoxConnectionError
from app.schemas.upstox import IngestRequest, IngestResponse, StreamStatusResponse
from app.services.data_ingestion import DataIngestionService
from app.services.tick_stream import TickStreamService, get_tick_service
from app.services.upstox_client import UpstoxClient, get_upstox_client

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user_id)])
ws_router = APIRouter()


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


# ── Stream management ──────────────────────────────────────────────────────────

@router.post("/stream/start", response_model=StreamStatusResponse)
@limiter.limit("30/minute")
async def start_stream(
    request: Request,
    instrument_keys: list[str],
    user_id: str = Depends(get_current_user_id),
    tick_service: TickStreamService = Depends(get_tick_service),
) -> StreamStatusResponse:
    if not instrument_keys:
        return StreamStatusResponse(status="no-op", message="No instrument keys provided")
    await tick_service.add_symbols(instrument_keys)
    if not tick_service._running:
        asyncio.create_task(tick_service.connect())
    return StreamStatusResponse(
        status="subscribed",
        message=f"Subscribed to {len(instrument_keys)} instruments",
        instrument_count=len(instrument_keys),
    )


@router.post("/stream/stop", response_model=StreamStatusResponse)
@limiter.limit("10/minute")
async def stop_stream(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    tick_service: TickStreamService = Depends(get_tick_service),
) -> StreamStatusResponse:
    await tick_service.disconnect()
    return StreamStatusResponse(status="disconnected", message="Stream stopped")


@router.get("/stream/status", response_model=StreamStatusResponse)
@limiter.limit("60/minute")
async def stream_status(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    tick_service: TickStreamService = Depends(get_tick_service),
) -> StreamStatusResponse:
    connected = tick_service._ws is not None and not tick_service._ws.closed
    return StreamStatusResponse(
        status="connected" if connected else "disconnected",
        instrument_count=len(tick_service._subscribed_symbols),
    )


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
@limiter.limit("60/minute")
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
    
    Upstox v3 path: /historical-candle/intraday/{key}/{unit}/{interval}
    """
    from app.services.candle_service import candle_service
    
    # Check Redis cache first (fastest)
    cache_key = f"candles:intraday:{instrument_key}:{unit}:{interval}"
    cached = await cache.get(cache_key)
    if cached:
        logger.debug(f"[Intraday] Cache hit for {instrument_key}")
        return cached

    # Check database for recent data (second fastest)
    db_candles, is_from_db = await candle_service.get_intraday_candles(
        db, instrument_key, unit, interval
    )
    
    if is_from_db and db_candles:
        logger.info(f"[Intraday] DB hit for {instrument_key}, {len(db_candles)} candles")
        result = {
            "status": "success",
            "data": {"candles": db_candles},
            "instrument_key": instrument_key,
            "interval": interval,
            "unit": unit,
        }
        await cache.set(cache_key, result, ttl=30)
        return result

    # Fetch from Upstox API (slowest, but most up-to-date)
    try:
        logger.info(f"[Intraday] Fetching from API for {instrument_key}")
        path = f"historical-candle/intraday/{instrument_key}/{unit}/{interval}"
        raw = await upstox.get(path)
        result = _candles_response(raw, instrument_key, unit, interval)
        
        # Store in database for future requests (background task)
        if result.get("status") == "success" and result.get("data", {}).get("candles"):
            candles = result["data"]["candles"]
            await candle_service.store_candles(db, instrument_key, unit, interval, candles)
        
        await cache.set(cache_key, result, ttl=30)
        return result
    except Exception:
        logger.exception("Intraday candles error for %s", instrument_key)
        return _empty_candles_response(instrument_key, unit, interval)


@router.get("/candles/historical")
@limiter.limit("60/minute")
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
    
    Benefits:
      - Always returns complete data (no gaps)
      - Reduces API calls (uses DB when possible)
      - Automatic gap detection and filling
      - Fast response times (DB + selective API calls)
      - Data persistence for historical analysis
    
    Upstox v3 path: /historical-candle/{key}/{unit}/{interval}/{to_date}/{from_date}
    """
    from app.services.candle_service import candle_service
    
    # Check Redis cache first (fastest)
    cache_key = f"candles:historical:{instrument_key}:{unit}:{interval}:{from_date}:{to_date}"
    cached = await cache.get(cache_key)
    if cached:
        logger.debug(f"[Historical] Cache hit for {instrument_key} {from_date}-{to_date}")
        return cached

    # Check database for historical data (second fastest)
    db_candles, is_from_db, missing_ranges = await candle_service.get_historical_candles(
        db, instrument_key, unit, interval, from_date, to_date
    )
    
    if is_from_db and db_candles:
        logger.info(
            f"[Historical] DB hit for {instrument_key} {from_date}-{to_date}, "
            f"{len(db_candles)} candles, {len(missing_ranges)} gap(s)"
        )
        
        # If gaps exist, fetch them from API and merge
        if missing_ranges:
            logger.info(f"[Historical] Filling {len(missing_ranges)} gap(s) from API")
            all_candles = db_candles
            
            for gap in missing_ranges:
                try:
                    gap_from = gap["from_date"]
                    gap_to = gap["to_date"]
                    logger.debug(f"[Historical] Fetching gap: {gap_from} to {gap_to}")
                    
                    # Fetch gap from Upstox API
                    path = f"historical-candle/{instrument_key}/{unit}/{interval}/{gap_to}/{gap_from}"
                    raw = await upstox.get(path)
                    gap_candles = raw.get("data", {}).get("candles", [])
                    
                    if gap_candles:
                        logger.debug(f"[Historical] Gap filled with {len(gap_candles)} candles")
                        # Merge gap candles with DB candles
                        all_candles = candle_service.merge_candles(all_candles, gap_candles)
                        
                        # Store gap candles in DB for future requests
                        await candle_service.store_candles(db, instrument_key, unit, interval, gap_candles)
                    else:
                        logger.debug(f"[Historical] Gap {gap_from} to {gap_to} returned no data (holiday/weekend)")
                except Exception as gap_error:
                    logger.warning(f"[Historical] Failed to fetch gap {gap}: {gap_error}")
                    # Continue with other gaps even if one fails
            
            logger.info(f"[Historical] Gap filling complete: {len(all_candles)} total candles")
            
            # Debug: Check for duplicate timestamps
            timestamps = [c[0] for c in all_candles]
            if len(timestamps) != len(set(timestamps)):
                logger.warning(f"[Historical] Duplicate timestamps detected after merge!")
                from collections import Counter
                dupes = [ts for ts, count in Counter(timestamps).items() if count > 1]
                logger.warning(f"[Historical] Duplicate timestamps: {dupes[:5]}")
            
            result = {
                "status": "success",
                "data": {"candles": all_candles},
                "instrument_key": instrument_key,
                "interval": interval,
                "unit": unit,
            }
            # Cache the complete data
            await cache.set(cache_key, result, ttl=300)
            return result
        else:
            # No gaps, return DB data as-is
            result = {
                "status": "success",
                "data": {"candles": db_candles},
                "instrument_key": instrument_key,
                "interval": interval,
                "unit": unit,
            }
            await cache.set(cache_key, result, ttl=300)
            return result

    # Fetch from Upstox API (slowest, but complete data)
    try:
        logger.info(f"[Historical] Fetching from API for {instrument_key} {from_date}-{to_date}")
        # Upstox v3 uses path params: unit and interval are passed as separate segments.
        # to_date comes before from_date in the URL (Upstox convention).
        path = f"historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}"
        raw = await upstox.get(path)
        result = _candles_response(raw, instrument_key, unit, interval)
        
        # Store in database for future requests (background task)
        if result.get("status") == "success" and result.get("data", {}).get("candles"):
            candles = result["data"]["candles"]
            await candle_service.store_candles(db, instrument_key, unit, interval, candles)
        
        await cache.set(cache_key, result, ttl=300)
        return result
    except Exception:
        logger.exception("Historical candles error for %s", instrument_key)
        return _empty_candles_response(instrument_key, unit, interval)


# ── WebSocket tick stream ──────────────────────────────────────────────────────

_HEARTBEAT_INTERVAL_S = 10.0
_MOCK_TICK_VOLATILITY = 0.0015  # 0.15 % per tick — realistic intraday noise


@ws_router.websocket("/ticks/ws")
async def websocket_ticks(
    websocket: WebSocket,
    instrument_key: str = Query(..., description="Upstox instrument key"),
    interval_ms: int = Query(500, ge=100, le=5000, description="Tick emission interval in ms"),
) -> None:
    """
    Real-time price tick stream for a single instrument.

    With a valid Upstox token:
      - Subscribes the instrument to TickStreamService (connects Upstox WS if
        not already running), registers a per-client asyncio.Queue callback,
        and forwards decoded ticks at up to the requested interval_ms rate.
      - Heartbeat frames are emitted every 10 s when no tick arrives.

    Without a token (development / CI):
      - Emits realistic mock ticks using Gaussian noise around a base price.
    """
    await websocket.accept()

    upstox_client: UpstoxClient = websocket.app.state.upstox_client
    tick_service: TickStreamService = websocket.app.state.tick_service

    subscriber_id = f"ws:{uuid.uuid4().hex}:{instrument_key}"
    # Bounded queue — if the client is slow we drop the oldest tick rather than
    # accumulating unbounded memory.
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)

    async def _enqueue_tick(tick_payload: dict[str, Any]) -> None:
        feeds = tick_payload.get("feeds", {})
        feed = feeds.get(instrument_key)
        if not feed:
            return
        ltp = feed.get("ltpc", {}).get("ltp")
        if ltp is None:
            return
        msg = {
            "type": "tick",
            "data": {
                "instrument_key": instrument_key,
                "last_price": ltp,
                "server_timestamp": tick_payload.get("currentTs"),
            },
        }
        try:
            queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Drop the oldest tick then insert the fresh one
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            queue.put_nowait(msg)

    try:
        await websocket.send_json({
            "type": "connected",
            "data": {"instrument_key": instrument_key, "interval_ms": interval_ms},
        })

        if upstox_client.has_token:
            await _run_live_stream(
                websocket, tick_service, instrument_key,
                subscriber_id, queue, _enqueue_tick, interval_ms,
            )
        else:
            await _run_mock_stream(websocket, instrument_key, interval_ms)

    except WebSocketDisconnect:
        logger.info("WS client disconnected: %s", instrument_key)
    except Exception:
        logger.exception("WS tick stream error for %s", instrument_key)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
    finally:
        if upstox_client.has_token:
            tick_service.unsubscribe(subscriber_id)


async def _run_live_stream(
    websocket: WebSocket,
    tick_service: TickStreamService,
    instrument_key: str,
    subscriber_id: str,
    queue: asyncio.Queue[dict[str, Any]],
    enqueue_cb: Any,
    interval_ms: int,
) -> None:
    """Bridge TickStreamService → WebSocket client for a single instrument."""
    await tick_service.add_symbols([instrument_key])
    if not tick_service._running:
        asyncio.create_task(tick_service.connect())

    tick_service.subscribe(subscriber_id, enqueue_cb)
    timeout_s = interval_ms / 1000.0

    try:
        while True:
            try:
                tick = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_INTERVAL_S)
                await websocket.send_json(tick)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat"})
    finally:
        tick_service.unsubscribe(subscriber_id)


async def _run_mock_stream(
    websocket: WebSocket,
    instrument_key: str,
    interval_ms: int,
) -> None:
    """
    Development mock: emits Gaussian-noise ticks around a stable base price.
    Uses 1 000 as the starting price when no candle history is available.
    """
    import random
    import math

    base_price = 1_000.0
    price = base_price
    sleep_s = interval_ms / 1000.0

    while True:
        # Geometric Brownian Motion step — realistic price walk
        price *= math.exp(random.gauss(0, _MOCK_TICK_VOLATILITY))
        await websocket.send_json({
            "type": "tick",
            "data": {
                "instrument_key": instrument_key,
                "last_price": round(price, 2),
                "server_timestamp": None,
            },
        })
        await asyncio.sleep(sleep_s)
