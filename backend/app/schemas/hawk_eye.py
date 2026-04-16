# PATH: backend/app/schemas/hawk_eye.py
# ───────────────────────────────
"""Cortex AI — HawkEye Schemas"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HawkEyeSignal(BaseModel):
    name: str
    direction: Literal["buy", "sell", "neutral"]
    value: float
    timeframe: str


class HawkEyeResult(BaseModel):
    instrument_key: str
    trading_symbol: str
    exchange: str
    composite_signal: Literal["buy", "sell", "neutral"]
    composite_score: float
    timeframe_signals: list[HawkEyeSignal]
    last_price: float
    strength: Literal["strong", "moderate", "weak"]
    agreement_pct: float = Field(ge=0.0, le=100.0)
    scanned_at: str


class HawkEyeResponse(BaseModel):
    results: list[HawkEyeResult]
    total: int
    timeframes_scanned: list[str]
    scanned_at: str