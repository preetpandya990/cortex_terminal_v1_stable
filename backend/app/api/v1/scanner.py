# PATH: backend/app/api/v1/scanner.py
# ─────────────────────────────
"""
Cortex AI — Scanner Router
============================
Authenticated, rate-limited scanner endpoints.
Results are cached in Redis; failed instruments reported via warnings field.
"""
import logging

from fastapi import APIRouter, Depends, Query, Request
from time import perf_counter
import asyncio
from datetime import datetime, timezone

from app.core.config import get_settings
from app.core.database import get_db
from app.core.redis import CacheService, get_cache_service
from app.core.security import get_current_user_id
from app.core.limiter import limiter
from app.schemas.scanner import (
    ScanResponse,
    LatestScanResponse,
    RunScanResponse,
    ScannerContextResponse,
    ScanResultsData,
    StockAnalysis,
)
from app.schemas.scanner import ScanResult
from app.services.market_scanner import MarketScannerService
from app.services.market_calendar import nse_calendar
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(dependencies=[Depends(get_current_user_id)])


@router.get("/run", response_model=ScanResponse)
@limiter.limit(settings.RATE_LIMIT_SCANNER)
async def run_scanner(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    timeframe: str = Query(default="1d", pattern=r"^(1m|5m|15m|30m|1h|4h|1d|1w)$"),
    force_refresh: bool = Query(default=False),
    session: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
) -> ScanResponse:
    """
    Run the technical signal scanner across all instruments.
    Results are cached for 60 seconds by default.
    Use force_refresh=true to bypass cache (rate-limited to prevent abuse).
    """
    scanner = MarketScannerService(cache=cache)
    results = await scanner.scan_all(session, timeframe=timeframe, force_refresh=force_refresh)

    failed_count = sum(1 for r in results if r.warnings)
    return ScanResponse(
        results=results,
        total=len(results),
        timeframe=timeframe,
        failed_instruments=failed_count,
        cached=not force_refresh,
    )


def _to_stock_analysis(result: ScanResult) -> StockAnalysis:
    return StockAnalysis(
        symbol=result.instrument_key,
        current_price=result.last_price,
        previous_close=result.previous_close or result.last_price,
        price_change=result.price_change,
        price_change_pct=result.price_change_pct,
        volume=result.volume or 0.0,
        avg_volume=result.avg_volume or 0.0,
        volume_ratio=result.volume_ratio,
        rsi=result.rsi,
        timestamp=result.scanned_at,
    )


def _build_scan_results(results: list[ScanResult]) -> ScanResultsData:
    gainers = sorted(
        (r for r in results if r.price_change >= 0),
        key=lambda r: r.price_change_pct,
        reverse=True,
    )
    losers = sorted(
        (r for r in results if r.price_change < 0),
        key=lambda r: r.price_change_pct,
    )
    volume_spikes = [r for r in results if any(s.name == "VOLUME_SURGE" for s in r.signals)]
    breakouts = [
        r
        for r in results
        if any(s.name in {"MACD_CROSSOVER", "BB_BELOW_LOWER"} for s in r.signals)
    ]

    return ScanResultsData(
        top_gainers=[_to_stock_analysis(r) for r in gainers[:25]],
        top_losers=[_to_stock_analysis(r) for r in losers[:25]],
        volume_spikes=[_to_stock_analysis(r) for r in volume_spikes[:25]],
        breakouts=[_to_stock_analysis(r) for r in breakouts[:25]],
        total_scanned=len(results),
        errors=[],
        metadata={"quote_analyses": len(results)},
    )


@router.get("/latest", response_model=LatestScanResponse)
@limiter.limit(settings.RATE_LIMIT_SCANNER)
async def get_latest_scan(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    timeframe: str = Query(default="1d", pattern=r"^(1m|5m|15m|30m|1h|4h|1d|1w)$"),
    session: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
) -> LatestScanResponse:
    scanner = MarketScannerService(cache=cache)
    start = perf_counter()
    
    try:
        results = await scanner.scan_all(session, timeframe=timeframe, force_refresh=False)
    except Exception as e:
        logger.warning(f"Scanner failed, returning empty results: {e}")
        results = []
    
    elapsed_ms = int((perf_counter() - start) * 1000)

    payload = _build_scan_results(results)
    now = datetime.now(timezone.utc)
    return LatestScanResponse(
        scan_id=0,
        timestamp=now,
        scan_type="market_close",
        total_scanned=payload.total_scanned,
        processing_time_ms=elapsed_ms,
        results=payload,
    )


@router.post("/run", response_model=RunScanResponse)
@limiter.limit(settings.RATE_LIMIT_SCANNER)
async def run_scan(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    scan_type: str = Query(default="market_close", pattern=r"^(market_open|market_close|intraday)$"),
    trade_date: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
) -> RunScanResponse:
    timeframe = "1d"
    if scan_type == "intraday":
        timeframe = "1h"
    elif scan_type == "market_open":
        timeframe = "1d"

    scanner = MarketScannerService(cache=cache)
    start = perf_counter()
    results = await scanner.scan_all(session, timeframe=timeframe, force_refresh=True)
    elapsed_ms = int((perf_counter() - start) * 1000)
    payload = _build_scan_results(results)

    status = "success"
    message = "Scan completed"
    return RunScanResponse(
        status=status,
        message=message,
        scan_type=scan_type,
        total_scanned=payload.total_scanned,
        error_count=0,
        primary_error_code=None,
        results=payload,
    )


@router.get("/context", response_model=ScannerContextResponse)
@limiter.limit(settings.RATE_LIMIT_SCANNER)
async def get_scanner_context(
    request: Request,
    user_id: str = Depends(get_current_user_id),
) -> ScannerContextResponse:
    now_utc = datetime.now(timezone.utc)
    try:
        await asyncio.wait_for(nse_calendar.refresh_if_needed(), timeout=1.0)
    except Exception:
        pass
    session = nse_calendar.get_session(now_utc)
    recent = nse_calendar.get_recent_trading_days(5)
    close_dates = [d.isoformat() for d in recent]

    default_scan_type = "intraday" if session.is_open_now else "market_close"
    if session.is_trading_day and session.market_open_utc and now_utc < session.market_open_utc:
        default_scan_type = "market_open"

    return ScannerContextResponse(
        now_utc=now_utc,
        is_trading_day=session.is_trading_day,
        is_market_open=session.is_open_now,
        market_open_utc=session.market_open_utc,
        market_close_utc=session.market_close_utc,
        default_scan_type=default_scan_type,
        selectable_market_close_dates=close_dates,
    )
