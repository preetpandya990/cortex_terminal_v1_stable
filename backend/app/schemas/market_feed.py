"""
Cortex AI — Market Feed Schemas
================================
Pydantic contracts for the unified real-time market-data WebSocket endpoint.

Client → Server (commands):
  SubCommand    — subscribe instrument keys to the ltpc feed
  UnsubCommand  — unsubscribe instrument keys from the ltpc feed

Server → Client (events):
  LtpcMessage       — live price update (ltp + previous close)
  SubscribedEvent   — acknowledgement after successful subscription
  UnsubscribedEvent — acknowledgement after successful unsubscription
  HeartbeatEvent    — server-initiated keepalive
  ErrorEvent        — validation or auth failure (connection stays open)
"""
from __future__ import annotations

import re
import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Instrument key format: EXCHANGE_TYPE|SYMBOL  e.g. NSE_EQ|INE002A01018
_INSTRUMENT_KEY_RE = re.compile(r"^[A-Z_]+\|[A-Z0-9_|]+$")

_MAX_KEYS_PER_MESSAGE = 100


def _validate_instrument_keys(keys: list[str]) -> list[str]:
    if not keys:
        raise ValueError("instrument_keys must not be empty")
    if len(keys) > _MAX_KEYS_PER_MESSAGE:
        raise ValueError(
            f"instrument_keys exceeds max of {_MAX_KEYS_PER_MESSAGE} per message"
        )
    invalid = [k for k in keys if not _INSTRUMENT_KEY_RE.match(k)]
    if invalid:
        raise ValueError(f"Invalid instrument key format: {invalid[:3]}")
    return keys


# ── Client → Server ────────────────────────────────────────────────────────────

class SubCommand(BaseModel):
    type: Literal["sub"]
    instrument_keys: list[str] = Field(..., min_length=1)

    @field_validator("instrument_keys")
    @classmethod
    def validate_keys(cls, v: list[str]) -> list[str]:
        return _validate_instrument_keys(v)


class UnsubCommand(BaseModel):
    type: Literal["unsub"]
    instrument_keys: list[str] = Field(..., min_length=1)

    @field_validator("instrument_keys")
    @classmethod
    def validate_keys(cls, v: list[str]) -> list[str]:
        return _validate_instrument_keys(v)


# ── Server → Client ────────────────────────────────────────────────────────────

class LtpcMessage(BaseModel):
    type: Literal["ltpc"] = "ltpc"
    instrument_key: str
    ltp: float
    cp: float   # previous session close price
    ts: int     # server timestamp — milliseconds since epoch


class SubscribedEvent(BaseModel):
    type: Literal["subscribed"] = "subscribed"
    instrument_keys: list[str]
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))


class UnsubscribedEvent(BaseModel):
    type: Literal["unsubscribed"] = "unsubscribed"
    instrument_keys: list[str]
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))


class HeartbeatEvent(BaseModel):
    type: Literal["ping"] = "ping"
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))


class ErrorEvent(BaseModel):
    type: Literal["error"] = "error"
    code: Literal["INVALID_KEY", "AUTH_REQUIRED", "INVALID_MESSAGE", "INTERNAL"]
    message: str
