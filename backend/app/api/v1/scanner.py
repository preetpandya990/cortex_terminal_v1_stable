# PATH: backend/app/api/v1/scanner.py
# ─────────────────────────────────────
"""
Cortex AI — Scanner Router
============================
Authenticated, rate-limited scanner endpoints.

Rate limits (per real client IP via X-Forwarded-For):
  GET /latest, GET /context  →  30 / minute  (polling)
  GET /run,    POST /run     →   5 / minute  (trigger)

Scan flow (delegated to MarketScannerService):
  1. Quorum-based reference session from DB  (last complete trading day)
  2. DB baselines  →  prev_close, 20-day avg volume, RSI candles
  3. Upstox live   →  LTP + today's volume  (market-open hours only)
  4. Rank: gainers/losers by % change, volume spikes by volume_ratio
"""
import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from time import perf_counter
from typing import Literal

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.redis import CacheService, get_cache_service
from app.core.security import get_current_user_id
from app.schemas.scanner import (
    LatestScanResponse,
    RunScanRequest,
    RunScanResponse,
    ScanProgressEvent,
    ScanResult,
    ScanResultsData,
    ScannerContextResponse,
    StockAnalysis,
)
from app.services.market_calendar import nse_calendar
from app.services.market_scanner import MarketScannerService, _VOLUME_SPIKE_THRESHOLD
from app.services.sector_map import get_sector
from app.services.upstox_client import UpstoxClient, get_upstox_client

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(dependencies=[Depends(get_current_user_id)])

_INTRADAY_TIMEFRAMES = frozenset({"1h", "4h", "1m", "5m", "15m", "30m"})


def _infer_scan_type(timeframe: str, is_market_open: bool = False) -> Literal["market_open", "market_close", "intraday"]:
    if timeframe in _INTRADAY_TIMEFRAMES:
        return "intraday"
    return "market_open" if is_market_open else "market_close"


def _to_stock_analysis(result: ScanResult) -> StockAnalysis:
    return StockAnalysis(
        symbol=result.instrument_key,
        trading_symbol=result.trading_symbol,
        name=result.name,
        sector=get_sector(result.trading_symbol, result.name),
        current_price=result.last_price,
        previous_close=result.previous_close or result.last_price,
        price_change=result.price_change,
        price_change_pct=result.price_change_pct,
        volume=result.volume or 0.0,
        avg_volume=result.avg_volume or 0.0,
        volume_ratio=result.volume_ratio,
        rsi=result.rsi,
        timestamp=result.scanned_at,
        warnings=result.warnings,
    )


def _build_scan_results(results: list[ScanResult]) -> ScanResultsData:
    gainers = sorted(
        (r for r in results if r.price_change_pct > 0),
        key=lambda r: r.price_change_pct,
        reverse=True,
    )
    losers = sorted(
        (r for r in results if r.price_change_pct < 0),
        key=lambda r: r.price_change_pct,  # most negative first
    )
    volume_spikes = sorted(
        (r for r in results if r.volume_ratio >= _VOLUME_SPIKE_THRESHOLD),
        key=lambda r: r.volume_ratio,
        reverse=True,
    )

    return ScanResultsData(
        top_gainers=[_to_stock_analysis(r) for r in gainers],
        top_losers=[_to_stock_analysis(r) for r in losers],
        volume_spikes=[_to_stock_analysis(r) for r in volume_spikes],
        total_scanned=len(results),
        errors=[],
        metadata={"quote_analyses": len(results)},
    )


# ── Endpoints ──────────────────────────────────────────────────────────────────


@router.get("/latest", response_model=LatestScanResponse)
@limiter.limit("30/minute")
async def get_latest_scan(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    timeframe: str = Query(default="1d", pattern=r"^(1m|5m|15m|30m|1h|4h|1d|1w)$"),
    session: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
    upstox_client: UpstoxClient = Depends(get_upstox_client),
) -> LatestScanResponse:
    scanner = MarketScannerService(cache=cache)
    start = perf_counter()

    try:
        results, live_prices_available = await scanner.scan_all(
            session, upstox_client, timeframe=timeframe, force_refresh=False
        )
    except Exception as exc:
        logger.warning("GET /scanner/latest failed, returning empty: %s", exc)
        results, live_prices_available = [], False

    elapsed_ms = int((perf_counter() - start) * 1000)
    stale_count = sum(1 for r in results if r.warnings)
    payload = _build_scan_results(results)

    now_utc = datetime.now(timezone.utc)
    try:
        await asyncio.wait_for(nse_calendar.refresh_if_needed(), timeout=1.0)
    except Exception:
        pass
    is_market_open = nse_calendar.get_session(now_utc).is_open_now

    return LatestScanResponse(
        scan_id=int(now_utc.timestamp()),
        timestamp=now_utc,
        scan_type=_infer_scan_type(timeframe, is_market_open),
        timeframe=timeframe,
        total_scanned=payload.total_scanned,
        processing_time_ms=elapsed_ms,
        stale_instrument_count=stale_count,
        live_prices_available=live_prices_available,
        results=payload,
    )


@router.post("/run", response_model=RunScanResponse)
@limiter.limit("5/minute")
async def run_scan(
    request: Request,
    body: RunScanRequest = Body(default_factory=RunScanRequest),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
    upstox_client: UpstoxClient = Depends(get_upstox_client),
) -> RunScanResponse:
    timeframe = "1h" if body.scan_type == "intraday" else "1d"

    scanner = MarketScannerService(cache=cache)
    try:
        results, live_prices_available = await scanner.scan_all(
            session, upstox_client, timeframe=timeframe, force_refresh=True
        )
        status: Literal["success", "partial", "failed"] = "success"
        message = f"Scan completed — {len(results)} instruments evaluated"
        error_count = 0
        primary_error_code = None
    except Exception as exc:
        logger.warning("POST /scanner/run failed: %s", exc)
        results, live_prices_available = [], False
        status = "failed"
        message = str(exc)
        error_count = 1
        primary_error_code = type(exc).__name__

    stale_count = sum(1 for r in results if r.warnings)
    payload = _build_scan_results(results)

    return RunScanResponse(
        status=status,
        message=message,
        scan_type=body.scan_type,
        total_scanned=payload.total_scanned,
        error_count=error_count,
        primary_error_code=primary_error_code,
        stale_instrument_count=stale_count,
        live_prices_available=live_prices_available,
        results=payload,
    )


@router.post("/run/stream")
@limiter.limit("5/minute")
async def run_scan_stream(
    request: Request,
    body: RunScanRequest = Body(default_factory=RunScanRequest),
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
    upstox_client: UpstoxClient = Depends(get_upstox_client),
) -> StreamingResponse:
    """
    POST /scanner/run/stream — Server-Sent Events scan endpoint.

    Streams ScanProgressEvent updates as text/event-stream.  The terminal
    'complete' event carries the full RunScanResponse payload so the client
    has results without a follow-up GET /latest round-trip.

    SSE event types:
      progress  →  { pct, stage, message }
      complete  →  { pct, stage, message, response: RunScanResponse }
      error     →  { message, stage }

    Rate limit: 5/minute — same budget as POST /run.
    """
    timeframe = "1h" if body.scan_type == "intraday" else "1d"
    cache_key = f"scanner:results:v2:{timeframe}"

    async def _sse_generator() -> AsyncGenerator[str, None]:
        scanner = MarketScannerService(cache=cache)
        try:
            async for event in scanner.scan_all_stream(session, upstox_client, timeframe):
                if await request.is_disconnected():
                    logger.debug("SSE scanner: client disconnected mid-stream")
                    return

                if event.results is not None:
                    # Terminal event — build full response, update cache, send
                    stale_count = sum(1 for r in event.results if r.warnings)
                    payload = _build_scan_results(event.results)

                    _ttl = (
                        settings.CACHE_TTL_SCANNER_OPEN
                        if nse_calendar.get_session(datetime.now(timezone.utc)).is_open_now
                        else settings.CACHE_TTL_SCANNER_CLOSED
                    )
                    await cache.set(
                        cache_key,
                        {
                            "results": [r.model_dump(mode="json") for r in event.results],
                            "live_prices_available": event.live_prices_available,
                        },
                        ttl=_ttl,
                    )

                    run_response = RunScanResponse(
                        status="success",
                        message=event.message,
                        scan_type=body.scan_type,
                        total_scanned=payload.total_scanned,
                        error_count=0,
                        primary_error_code=None,
                        stale_instrument_count=stale_count,
                        live_prices_available=event.live_prices_available,
                        results=payload,
                    )
                    data = json.dumps({
                        "pct": 100,
                        "stage": "complete",
                        "message": event.message,
                        "response": run_response.model_dump(mode="json"),
                    }, default=str)
                    yield f"event: complete\ndata: {data}\n\n"

                elif event.stage == "error":
                    data = json.dumps({"message": event.message, "stage": event.stage})
                    yield f"event: error\ndata: {data}\n\n"

                else:
                    data = json.dumps({
                        "pct": event.pct,
                        "stage": event.stage,
                        "message": event.message,
                    })
                    yield f"event: progress\ndata: {data}\n\n"

        except Exception as exc:
            logger.warning("SSE scanner stream failed: %s", exc)
            data = json.dumps({"message": str(exc), "stage": "error", "code": type(exc).__name__})
            yield f"event: error\ndata: {data}\n\n"

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/context", response_model=ScannerContextResponse)
@limiter.limit("30/minute")
async def get_scanner_context(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> ScannerContextResponse:
    now_utc = datetime.now(timezone.utc)
    try:
        await asyncio.wait_for(nse_calendar.refresh_if_needed(), timeout=1.0)
    except Exception:
        pass

    market = nse_calendar.get_session(now_utc)
    recent = nse_calendar.get_recent_trading_days(5)
    close_dates = [d.isoformat() for d in recent]

    default_scan_type: Literal["market_open", "market_close", "intraday"] = "market_close"
    if market.is_open_now:
        default_scan_type = "intraday"
    elif market.is_trading_day and market.market_open_utc and now_utc < market.market_open_utc:
        default_scan_type = "market_open"

    return ScannerContextResponse(
        now_utc=now_utc,
        is_trading_day=market.is_trading_day,
        is_market_open=market.is_open_now,
        market_open_utc=market.market_open_utc,
        market_close_utc=market.market_close_utc,
        default_scan_type=default_scan_type,
        selectable_market_close_dates=close_dates,
        closed_reason=market.closed_reason,
    )
