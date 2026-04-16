# PATH: backend/app/schemas/market_data.py
# ──────────────────────────────────
"""Cortex AI — Market Data Schemas"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class LivePriceResponse(BaseModel):
    instrument_key: str
    last_price: float
    timestamp: str | None = None


class InstrumentSearchResult(BaseModel):
    instrument_key: str
    trading_symbol: str
    name: str
    exchange: str
    instrument_type: str


class InstrumentSearchResponse(BaseModel):
    query: str
    count: int
    results: list[InstrumentSearchResult]


class OHLCVResponse(BaseModel):
    instrument_key: str
    timeframe: str
    candles: list[list[float | int | str]]
    source: Literal["db"]
