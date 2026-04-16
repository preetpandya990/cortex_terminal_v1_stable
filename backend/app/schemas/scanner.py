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
from typing import Literal

from pydantic import BaseModel, Field


class ScanSignal(BaseModel):
    name: str
    value: float
    direction: Literal["buy", "sell", "neutral"]


class ScanResult(BaseModel):
    instrument_key: str
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


class StockAnalysis(BaseModel):
    symbol: str
    current_price: float
    previous_close: float
    price_change: float
    price_change_pct: float
    volume: float
    avg_volume: float
    volume_ratio: float
    above_ema200: bool | None = None
    rsi: float | None = None
    timestamp: datetime


class ScanResultsData(BaseModel):
    top_gainers: list[StockAnalysis]
    top_losers: list[StockAnalysis]
    volume_spikes: list[StockAnalysis]
    breakouts: list[StockAnalysis]
    total_scanned: int
    errors: list[dict]
    metadata: dict


class LatestScanResponse(BaseModel):
    scan_id: int
    timestamp: datetime
    scan_type: Literal["market_open", "market_close", "intraday"]
    total_scanned: int
    processing_time_ms: int
    results: ScanResultsData


class RunScanResponse(BaseModel):
    status: Literal["success", "partial", "failed"]
    message: str
    scan_type: Literal["market_open", "market_close", "intraday"]
    total_scanned: int
    error_count: int
    primary_error_code: str | None
    results: ScanResultsData
