# PATH: backend/app/schemas/scanner.py
# ──────────────────────────────
"""
Cortex AI — Scanner Schemas
=============================
Typed Pydantic v2 models for scanner request/response shapes.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ScanSignal(BaseModel):
    name: str
    value: float
    direction: Literal["buy", "sell", "neutral"]


class ScanResult(BaseModel):
    instrument_key: str
    trading_symbol: str | None = None
    name: str | None = None
    signal: Literal["buy", "sell", "neutral"]
    score: float
    signals: list[ScanSignal] = Field(default_factory=list)
    last_price: float
    previous_close: float | None = None
    price_change: float = 0.0
    price_change_pct: float = 0.0
    volume: float | None = None
    avg_volume: float | None = None
    volume_ratio: float = 0.0
    rsi: float | None = None
    scanned_at: datetime
    warnings: list[str] = Field(default_factory=list)


class ScanResponse(BaseModel):
    results: list[ScanResult]
    total: int
    timeframe: str
    failed_instruments: int = 0
    cached: bool = False


class ScannerContextResponse(BaseModel):
    now_utc: datetime
    is_trading_day: bool
    is_market_open: bool
    market_open_utc: datetime | None
    market_close_utc: datetime | None
    default_scan_type: Literal["market_open", "market_close", "intraday"]
    selectable_market_close_dates: list[str]
    closed_reason: str | None = None


class StockAnalysis(BaseModel):
    symbol: str
    trading_symbol: str | None = None
    name: str | None = None
    sector: str | None = None
    current_price: float
    previous_close: float
    price_change: float
    price_change_pct: float
    volume: float
    avg_volume: float
    volume_ratio: float
    rsi: float | None = None
    timestamp: datetime
    warnings: list[str] = Field(default_factory=list)


class ScanResultsData(BaseModel):
    top_gainers: list[StockAnalysis]
    top_losers: list[StockAnalysis]
    volume_spikes: list[StockAnalysis]
    total_scanned: int
    errors: list[dict]
    metadata: dict


class RunScanRequest(BaseModel):
    scan_type: Literal["market_open", "market_close", "intraday"] = "market_close"


class LatestScanResponse(BaseModel):
    scan_id: int
    timestamp: datetime
    scan_type: Literal["market_open", "market_close", "intraday"]
    timeframe: str
    total_scanned: int
    processing_time_ms: int
    stale_instrument_count: int = 0
    live_prices_available: bool = True
    results: ScanResultsData


class RunScanResponse(BaseModel):
    status: Literal["success", "partial", "failed"]
    message: str
    scan_type: Literal["market_open", "market_close", "intraday"]
    total_scanned: int
    error_count: int
    primary_error_code: str | None
    stale_instrument_count: int = 0
    live_prices_available: bool = True
    results: ScanResultsData


class ScanProgressEvent(BaseModel):
    """
    Yielded by MarketScannerService.scan_all_stream() at each pipeline stage.
    The terminal event has results populated; all prior events are progress-only.
    """
    pct: int = Field(ge=0, le=100)
    stage: str
    message: str
    results: list[ScanResult] | None = None
    live_prices_available: bool = False
