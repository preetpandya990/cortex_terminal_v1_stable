# PATH: backend/app/schemas/upstox.py
# ─────────────────────────────
"""Cortex AI — Upstox Schemas"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    instrument_keys: list[str] = Field(..., min_length=1, max_length=100)
    timeframe: str = Field(default="1d", pattern=r"^(1m|5m|15m|30m|1h|4h|1d|1w)$")
    from_date: str = Field(..., description="YYYY-MM-DD")
    to_date: str = Field(..., description="YYYY-MM-DD")


class IngestResponse(BaseModel):
    status: str
    message: str
    instrument_count: int


class StreamStatusResponse(BaseModel):
    status: str
    message: str | None = None
    instrument_count: int | None = None