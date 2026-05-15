"""
Symbol Validator Service
========================
Validates NSE trading symbols against the instrument_master table.

A symbol is eligible if it exists in instrument_master as:
  - exchange = 'NSE'
  - instrument_type = 'EQ'

This is the canonical eligibility check used by all signal generation
paths — event classification, on-demand assembly, and the correlation engine.
The source of truth is the Upstox instrument sync, which is tied to real ISIN
keys for every listed NSE equity.

Caching strategy:
  Positive (eligible)   → Redis TTL 3 600 s  (1 h)
  Negative (ineligible) → Redis TTL   300 s  (5 min — allows rapid recovery)
  Redis failure         → Falls through to DB; validation never hard-fails
                          due to cache unavailability.

Batch operations use Redis MGET + a single SQL ANY($1) query to minimise
both network round-trips and DB load.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.upstox_data import InstrumentMaster

logger = logging.getLogger(__name__)

_ELIGIBLE_TTL    = 3_600   # 1 hour
_INELIGIBLE_TTL  =   300   # 5 minutes — allows quick recovery on new instruments
_ELIG_KEY_PFX    = "cortex:sym:elig:"   # cortex:sym:elig:{SYMBOL}
_NAME_KEY_PFX    = "cortex:sym:name:"   # cortex:sym:name:{SYMBOL}
_IKEY_KEY_PFX    = "cortex:sym:ikey:"   # cortex:sym:ikey:{SYMBOL}
_EXCHANGE        = "NSE"
_INSTRUMENT_TYPE = "EQ"


# ── Exception ──────────────────────────────────────────────────────────────────

class SymbolNotEligibleError(ValueError):
    """
    Raised when a symbol cannot be found in instrument_master as an NSE EQ equity.

    API handlers catch this and return HTTP 400 with a structured error body.
    The signal pipeline raises this to abort processing before any expensive
    ML inference or DB writes are attempted.
    """

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        super().__init__(
            f"Symbol '{symbol}' is not eligible: not found in instrument_master "
            f"as an NSE EQ equity. Only NSE equity instruments covered by the "
            f"Upstox data feed are supported. Verify the symbol is listed on NSE "
            f"and has been synced to the platform."
        )


# ── Service ────────────────────────────────────────────────────────────────────

class SymbolValidatorService:
    """
    Validates NSE trading symbols and resolves company names from instrument_master.

    Instantiate with an optional Redis client for caching; pass ``redis=None``
    to run in DB-only mode (useful in tests or low-memory environments).

    The module-level ``symbol_validator`` singleton uses lazy Redis acquisition
    via ``get_redis()`` so the service is safe to import at module load time
    before the Redis pool is initialised.
    """

    __slots__ = ("_redis",)

    def __init__(self, redis: Any = None) -> None:
        self._redis = redis

    # ── Public API ─────────────────────────────────────────────────────────────

    async def validate_symbol(self, symbol: str, db: AsyncSession) -> bool:
        """
        Return ``True`` if *symbol* exists in instrument_master as NSE EQ.

        Flow: Redis cache → DB → write-back to cache.
        Redis failures are silently swallowed; the DB is always the fallback.
        """
        symbol = symbol.strip().upper()
        if not symbol:
            return False

        cache_key = f"{_ELIG_KEY_PFX}{symbol}"

        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached == "1"

        eligible = await self._db_check_single(symbol, db)
        ttl = _ELIGIBLE_TTL if eligible else _INELIGIBLE_TTL
        await self._cache_set(cache_key, "1" if eligible else "0", ttl)

        if not eligible:
            logger.debug(
                "Symbol not eligible (not in instrument_master NSE EQ): %s", symbol
            )
        return eligible

    async def validate_symbols(
        self, symbols: list[str], db: AsyncSession
    ) -> list[str]:
        """
        Batch-validate *symbols*. Returns the eligible subset, preserving input order.

        Uses Redis MGET for cache reads and a single ``IN`` query for DB misses —
        O(1) Redis round-trips regardless of list length.
        """
        if not symbols:
            return []

        normalised = [s.strip().upper() for s in symbols if s and s.strip()]
        if not normalised:
            return []

        # Deduplicate while preserving order.
        ordered_unique: list[str] = list(dict.fromkeys(normalised))

        # ── 1. Batch cache read ────────────────────────────────────────────────
        keys = [f"{_ELIG_KEY_PFX}{s}" for s in ordered_unique]
        cached_values = await self._cache_mget(keys)

        eligible_set: set[str] = set()
        cache_misses: list[str] = []

        for sym, val in zip(ordered_unique, cached_values):
            if val is None:
                cache_misses.append(sym)
            elif val == "1":
                eligible_set.add(sym)
            # val == "0" → explicitly cached as ineligible; skip

        # ── 2. DB query for cache misses ───────────────────────────────────────
        if cache_misses:
            db_eligible = await self._db_check_batch(cache_misses, db)

            pipe_entries: list[tuple[str, str, int]] = []
            for sym in cache_misses:
                is_elig = sym in db_eligible
                if is_elig:
                    eligible_set.add(sym)
                ttl = _ELIGIBLE_TTL if is_elig else _INELIGIBLE_TTL
                pipe_entries.append((f"{_ELIG_KEY_PFX}{sym}", "1" if is_elig else "0", ttl))

            await self._cache_pipeline_set(pipe_entries)

        dropped = [s for s in ordered_unique if s not in eligible_set]
        if dropped:
            logger.debug(
                "Symbol validation: %d/%d eligible; dropped: %s",
                len(eligible_set), len(ordered_unique), dropped,
            )

        # Return in original input order, deduplicated.
        seen: set[str] = set()
        result: list[str] = []
        for s in normalised:
            if s in eligible_set and s not in seen:
                result.append(s)
                seen.add(s)
        return result

    async def get_company_name(
        self, symbol: str, db: AsyncSession
    ) -> str | None:
        """
        Return the official company name from ``instrument_master.name``.

        Cached in Redis for ``_ELIGIBLE_TTL`` seconds.  Returns ``None`` when
        the symbol is not found or its name field is null.
        """
        symbol = symbol.strip().upper()
        if not symbol:
            return None

        cache_key = f"{_NAME_KEY_PFX}{symbol}"

        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached or None  # empty string sentinel → None

        stmt = (
            select(InstrumentMaster.name)
            .where(
                InstrumentMaster.trading_symbol == symbol,
                InstrumentMaster.exchange == _EXCHANGE,
                InstrumentMaster.instrument_type == _INSTRUMENT_TYPE,
            )
            .limit(1)
        )
        result = await db.execute(stmt)
        name: str | None = result.scalar_one_or_none()

        # Cache result; empty string represents "no name" to avoid repeated DB hits.
        await self._cache_set(cache_key, name or "", _ELIGIBLE_TTL)
        return name

    async def get_instrument_key(
        self, symbol: str, db: AsyncSession
    ) -> str | None:
        """
        Return the Upstox instrument_key for *symbol* (e.g. ``NSE_EQ|INE001A01036``).

        Cached in Redis for ``_ELIGIBLE_TTL`` seconds.  Returns ``None`` when
        the symbol is not found in instrument_master.
        """
        symbol = symbol.strip().upper()
        if not symbol:
            return None

        cache_key = f"{_IKEY_KEY_PFX}{symbol}"

        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached or None  # empty string sentinel → None

        stmt = (
            select(InstrumentMaster.instrument_key)
            .where(
                InstrumentMaster.trading_symbol == symbol,
                InstrumentMaster.exchange == _EXCHANGE,
                InstrumentMaster.instrument_type == _INSTRUMENT_TYPE,
            )
            .limit(1)
        )
        result = await db.execute(stmt)
        key: str | None = result.scalar_one_or_none()

        await self._cache_set(cache_key, key or "", _ELIGIBLE_TTL)
        return key

    async def get_company_names(
        self, symbols: list[str], db: AsyncSession
    ) -> dict[str, str]:
        """
        Batch-fetch company names for *symbols*.

        Returns ``{trading_symbol: company_name}`` for symbols that have names.
        Symbols with a null name in instrument_master are omitted from the result.
        """
        if not symbols:
            return {}

        unique = list({s.strip().upper() for s in symbols if s and s.strip()})
        if not unique:
            return {}

        stmt = (
            select(InstrumentMaster.trading_symbol, InstrumentMaster.name)
            .where(
                InstrumentMaster.trading_symbol.in_(unique),
                InstrumentMaster.exchange == _EXCHANGE,
                InstrumentMaster.instrument_type == _INSTRUMENT_TYPE,
                InstrumentMaster.name.isnot(None),
            )
        )
        result = await db.execute(stmt)
        return {row.trading_symbol: row.name for row in result.all()}

    # ── Private — DB helpers ───────────────────────────────────────────────────

    async def _db_check_single(self, symbol: str, db: AsyncSession) -> bool:
        try:
            stmt = (
                select(InstrumentMaster.id)
                .where(
                    InstrumentMaster.trading_symbol == symbol,
                    InstrumentMaster.exchange == _EXCHANGE,
                    InstrumentMaster.instrument_type == _INSTRUMENT_TYPE,
                )
                .limit(1)
            )
            result = await db.execute(stmt)
            return result.scalar_one_or_none() is not None
        except Exception as exc:
            logger.warning("DB eligibility check failed for '%s': %s", symbol, exc)
            return False

    async def _db_check_batch(
        self, symbols: list[str], db: AsyncSession
    ) -> set[str]:
        """Single IN query for a list of symbols. Returns the eligible subset."""
        if not symbols:
            return set()
        try:
            stmt = select(InstrumentMaster.trading_symbol).where(
                InstrumentMaster.trading_symbol.in_(symbols),
                InstrumentMaster.exchange == _EXCHANGE,
                InstrumentMaster.instrument_type == _INSTRUMENT_TYPE,
            )
            result = await db.execute(stmt)
            return {row.trading_symbol for row in result.all()}
        except Exception as exc:
            logger.warning(
                "Batch DB eligibility check failed (returning empty set): %s", exc
            )
            return set()

    # ── Private — Redis helpers ────────────────────────────────────────────────

    def _get_redis(self) -> Any:
        """Return the configured Redis client, or lazily acquire the global one."""
        if self._redis is not None:
            return self._redis
        try:
            from app.core.redis import get_redis  # noqa: PLC0415
            return get_redis()
        except Exception:
            return None

    async def _cache_get(self, key: str) -> str | None:
        redis = self._get_redis()
        if redis is None:
            return None
        try:
            val = await redis.get(key)
            if val is None:
                return None
            return val.decode() if isinstance(val, bytes) else str(val)
        except Exception:
            return None

    async def _cache_set(self, key: str, value: str, ttl: int) -> None:
        redis = self._get_redis()
        if redis is None:
            return
        try:
            await redis.setex(key, ttl, value)
        except Exception:
            pass

    async def _cache_mget(self, keys: list[str]) -> list[str | None]:
        redis = self._get_redis()
        if redis is None or not keys:
            return [None] * len(keys)
        try:
            values = await redis.mget(*keys)
            return [
                (v.decode() if isinstance(v, bytes) else str(v)) if v is not None else None
                for v in values
            ]
        except Exception:
            return [None] * len(keys)

    async def _cache_pipeline_set(
        self, entries: list[tuple[str, str, int]]
    ) -> None:
        """Write multiple key/value/ttl entries atomically via a Redis pipeline."""
        redis = self._get_redis()
        if redis is None or not entries:
            return
        try:
            pipe = redis.pipeline()
            for key, value, ttl in entries:
                pipe.setex(key, ttl, value)
            await pipe.execute()
        except Exception:
            pass


# ── Module-level singleton ─────────────────────────────────────────────────────
# Redis is acquired lazily on first use — safe to import at module load time
# before the Redis connection pool is initialised by the app lifespan.
symbol_validator = SymbolValidatorService()
