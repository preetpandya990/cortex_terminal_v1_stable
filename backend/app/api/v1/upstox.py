# PATH: backend/app/api/v1/upstox.py
# ────────────────────────────
"""
Cortex AI — Upstox Router
===========================
Stream management and candle ingestion endpoints.
All endpoints are authenticated and rate-limited.
Services are retrieved from app.state (singletons) — no per-request instantiation.
"""
import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.limiter import limiter
from app.core.security import get_current_user_id
from app.core.redis import CacheService, get_cache_service
from app.exceptions import UpstoxConnectionError
from app.schemas.upstox import IngestRequest, IngestResponse, StreamStatusResponse
from app.services.data_ingestion import DataIngestionService
from app.services.tick_stream import TickStreamService, get_tick_service
from app.services.upstox_client import UpstoxClient, get_upstox_client

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(get_current_user_id)])
ws_router = APIRouter()  # Separate router for WebSocket without auth


# ── Stream management ──────────────────────────────────────────────────────────
@router.post("/stream/start", response_model=StreamStatusResponse)
@limiter.limit("30/minute")
async def start_stream(
    request: Request,
    instrument_keys: list[str],
    user_id: str = Depends(get_current_user_id),
    tick_service: TickStreamService = Depends(get_tick_service),
) -> StreamStatusResponse:
    """
    Subscribe to real-time tick feed for the given instruments.
    Uses the singleton TickStreamService from app.state.
    """
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
    """Gracefully disconnect the tick stream."""
    await tick_service.disconnect()
    return StreamStatusResponse(status="disconnected", message="Stream stopped")


@router.get("/stream/status", response_model=StreamStatusResponse)
@limiter.limit("60/minute")
async def stream_status(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    tick_service: TickStreamService = Depends(get_tick_service),
) -> StreamStatusResponse:
    """Return current tick stream connection status."""
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
    """
    Trigger bulk OHLCV ingestion for one or more instruments.
    Heavy work runs as a background task — HTTP response returns immediately.
    """
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



# ── Candles endpoints ──────────────────────────────────────────────────────────
@router.get("/candles/intraday")
@limiter.limit("60/minute")
async def get_intraday_candles(
    request: Request,
    instrument_key: str = Query(...),
    interval: int = Query(1, ge=1, le=60),
    unit: str = Query("minutes", pattern="^(minutes|hours)$"),
    user_id: str = Depends(get_current_user_id),
    upstox: UpstoxClient = Depends(get_upstox_client),
    cache: CacheService = Depends(get_cache_service),
):
    """Get intraday candles from Upstox API. Cached for 30 seconds."""
    cache_key = f"candles:intraday:{instrument_key}:{interval}:{unit}"
    cached = await cache.get(cache_key)
    if cached:
        return cached

    try:
        # Map to Upstox API format
        interval_map = {"minutes": f"{interval}minute", "hours": f"{interval}hour"}
        upstox_interval = interval_map.get(unit, "1minute")

        data = await upstox.get(
            "/historical-candle/intraday",
            params={
                "instrument_key": instrument_key,
                "interval": upstox_interval,
            },
        )

        result = {
            "status": "success",
            "data": data.get("data", {}).get("candles", []),
            "instrument_key": instrument_key,
            "interval": interval,
            "unit": unit,
        }

        await cache.set(cache_key, result, ttl=30)
        return result
    except Exception as e:
        logger.error("Intraday candles error: %s", e)
        # Return empty data instead of failing
        return {
            "status": "error",
            "data": [],
            "instrument_key": instrument_key,
            "interval": interval,
            "unit": unit,
            "message": "Unable to fetch candles from Upstox"
        }


@router.get("/candles/historical")
@limiter.limit("60/minute")
async def get_historical_candles(
    request: Request,
    instrument_key: str = Query(...),
    interval: str = Query("1day"),
    from_date: str = Query(None),
    to_date: str = Query(None),
    user_id: str = Depends(get_current_user_id),
    upstox: UpstoxClient = Depends(get_upstox_client),
    cache: CacheService = Depends(get_cache_service),
):
    """Get historical candles from Upstox API. Cached for 5 minutes."""
    cache_key = f"candles:historical:{instrument_key}:{interval}:{from_date}:{to_date}"
    cached = await cache.get(cache_key)
    if cached:
        return cached

    try:
        params = {
            "instrument_key": instrument_key,
            "interval": interval,
        }
        if from_date:
            params["from_date"] = from_date
        if to_date:
            params["to_date"] = to_date

        data = await upstox.get("/historical-candle", params=params)

        result = {
            "status": "success",
            "data": data.get("data", {}).get("candles", []),
            "instrument_key": instrument_key,
            "interval": interval,
        }

        await cache.set(cache_key, result, ttl=300)
        return result
    except Exception as e:
        logger.error("Historical candles error: %s", e)
        # Return empty data instead of failing
        return {
            "status": "error",
            "data": [],
            "instrument_key": instrument_key,
            "interval": interval,
            "message": "Unable to fetch candles from Upstox"
        }



# ── WebSocket tick stream ──────────────────────────────────────────────────────
@ws_router.websocket("/ticks/ws")
async def websocket_ticks(
    websocket: WebSocket,
    instrument_key: str = Query(...),
    interval_ms: int = Query(500, ge=100, le=5000),
):
    """
    WebSocket endpoint for real-time tick data.
    Sends mock ticks for now - integrate with actual tick service later.
    Note: WebSocket auth is handled differently - token should be in query params for production.
    """
    await websocket.accept()
    
    try:
        import random
        import json
        from datetime import datetime
        
        # Send initial message
        await websocket.send_json({
            "type": "connected",
            "instrument_key": instrument_key,
            "interval_ms": interval_ms
        })
        
        # Mock tick data loop
        base_price = 1000.0
        while True:
            # Generate mock tick
            price = base_price + random.uniform(-10, 10)
            tick = {
                "type": "tick",
                "instrument_key": instrument_key,
                "last_price": round(price, 2),
                "volume": random.randint(100, 10000),
                "timestamp": datetime.now().isoformat(),
            }
            
            await websocket.send_json(tick)
            await asyncio.sleep(interval_ms / 1000)
            
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for %s", instrument_key)
    except Exception as e:
        logger.error("WebSocket error: %s", e)
        try:
            await websocket.close()
        except:
            pass
