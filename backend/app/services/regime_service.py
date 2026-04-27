"""
Cortex AI — Market Regime Service
===================================
Computes market regime for Nifty indices and their constituent stocks.

Data source: upstox_ohlcv table (timeframe='1D', ~60 daily candles per instrument).
All technical indicators are computed server-side from DB data — no external API calls.

Regime pipeline per instrument:
  1. Load last 60 daily OHLCV candles from DB
  2. Compute ADX(14), RSI(14), ATR(14)%, Bollinger Width(20)
  3. Classify regime + confidence via rule engine
  4. Aggregate constituent regimes → index regime

Caching: Redis with 15-min TTL for indices, 5-min for instruments.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ── Nifty index definitions ─────────────────────────────────────────────────────

NIFTY_INDICES: dict[str, dict[str, Any]] = {
    "nifty50": {
        "name": "Nifty 50",
        "short": "NIFTY50",
        "constituents": [
            "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
            "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BHARTIARTL", "BPCL",
            "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
            "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
            "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
            "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
            "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
            "POWERGRID", "RELIANCE", "SBIN", "SBILIFE", "SHRIRAMFIN",
            "SUNPHARMA", "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS",
            "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO",
        ],
    },
    "niftybank": {
        "name": "Nifty Bank",
        "short": "BANKNIFTY",
        "constituents": [
            "AUBANK", "AXISBANK", "BANDHANBNK", "FEDERALBNK", "HDFCBANK",
            "ICICIBANK", "IDFCFIRSTB", "INDUSINDBK", "KOTAKBANK", "PNB",
            "SBIN", "BANKBARODA",
        ],
    },
    "niftyit": {
        "name": "Nifty IT",
        "short": "NIFTYIT",
        "constituents": [
            "COFORGE", "HCLTECH", "INFY", "LTIM", "MPHASIS",
            "OFSS", "PERSISTENT", "TCS", "TECHM", "WIPRO",
        ],
    },
    "niftyauto": {
        "name": "Nifty Auto",
        "short": "NIFTYAUTO",
        "constituents": [
            "APOLLOTYRE", "ASHOKLEY", "BAJAJ-AUTO", "BALKRISIND", "BHARATFORG",
            "BOSCHLTD", "EICHERMOT", "EXIDEIND", "HEROMOTOCO", "M&M",
            "MARUTI", "MOTHERSON", "MRF", "TATAMOTORS", "TVSMOTOR",
        ],
    },
    "niftyfmcg": {
        "name": "Nifty FMCG",
        "short": "NIFTYFMCG",
        "constituents": [
            "BRITANNIA", "COLPAL", "DABUR", "EMAMILTD", "GODREJCP",
            "HINDUNILVR", "ITC", "JYOTHYLAB", "MARICO", "MCDOWELL-N",
            "NESTLEIND", "PGHH", "RADICO", "TATACONSUM", "UBL",
        ],
    },
    "niftypharma": {
        "name": "Nifty Pharma",
        "short": "NIFTYPHARMA",
        "constituents": [
            "APOLLOHOSP", "AUROPHARMA", "BIOCON", "CIPLA", "DIVISLAB",
            "DRREDDY", "IPCA", "LUPIN", "SUNPHARMA", "TORNTPHARM",
        ],
    },
    "niftymetal": {
        "name": "Nifty Metal",
        "short": "NIFTYMETAL",
        "constituents": [
            "APLAPOLLO", "COALINDIA", "HINDALCO", "HINDCOPPER", "JSWSTEEL",
            "MOIL", "NATIONALUM", "NMDC", "RATNAMANI", "SAIL",
            "TATASTEEL", "VEDL", "WELCORP", "JINDALSAW", "APL",
        ],
    },
    "niftyrealty": {
        "name": "Nifty Realty",
        "short": "NIFTYREALTY",
        "constituents": [
            "BRIGADE", "DLF", "GODREJPROP", "IBREALEST", "MAHLIFE",
            "OBEROIRLTY", "PHOENIXLTD", "PRESTIGE", "SOBHA", "SUNTECK",
        ],
    },
    "niftymidcap50": {
        "name": "Nifty Midcap 50",
        "short": "MIDCAP50",
        "constituents": [
            "ABCAPITAL", "ALKEM", "ASHOKLEY", "ATUL", "AUBANK",
            "BALKRISIND", "BANDHANBNK", "BHARATFORG", "CANBK", "CHOLAFIN",
            "COLPAL", "CONCOR", "CROMPTON", "CUMMINSIND", "FEDERALBNK",
            "GLENMARK", "IDFCFIRSTB", "INDHOTEL", "IRCTC", "JUBLFOOD",
            "KANSAINER", "LICHSGFIN", "LUPIN", "MANAPPURAM", "MFSL",
            "MOTHERSON", "MPHASIS", "MRF", "NMDC", "OFSS",
            "PAGEIND", "PERSISTENT", "PIIND", "SAIL", "SUNDARMFIN",
            "SUNTV", "TATACOMM", "TORNTPHARM", "VOLTAS", "ZYDUSLIFE",
        ],
    },
    "niftysmallcap50": {
        "name": "Nifty Smallcap 50",
        "short": "SMALLCAP50",
        "constituents": [
            "AARTIIND", "AEGISCHEM", "AFFLE", "ASTRAL", "BSOFT",
            "CAMPUS", "CHALET", "CRAFTSMAN", "DEEPAKNTR", "EDELWEISS",
            "ELECON", "ENDURANCE", "EPIGRAL", "FINEORG", "GPIL",
            "GRSE", "HBLPOWER", "IDEAFORGE", "JKCEMENT", "JKPAPER",
            "KARURVYSYA", "KPRMILL", "KRBL", "LATENTVIEW", "MAZDOCK",
            "MEDANTA", "OLECTRA", "PNCINFRA", "RAYMOND", "SAFARI",
            "SBFC", "SBICARDS", "SUDARSCHEM", "TANLA", "THYROCARE",
        ],
    },
}

# ── Pure-Python technical indicators ───────────────────────────────────────────

def _rsi(closes: list[float], period: int = 14) -> float:
    """Wilder's RSI."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas[-period:]]
    losses = [max(-d, 0.0) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """Average True Range (absolute value)."""
    if len(closes) < 2:
        return 0.0
    tr_vals: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_vals.append(tr)
    window = tr_vals[-period:]
    return round(sum(window) / len(window), 4) if window else 0.0


def _adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    """
    Average Directional Index (Wilder's smoothing).

    TR / +DM / -DM use sum-based Wilder initialisation so that DI+ / DI-
    remain in [0, 100] (division by the same-scale smoothed TR cancels the
    factor).  DX is already in [0, 100], so ADX uses an average-based
    initialisation to keep it in the same range.
    """
    n = len(closes)
    if n < period * 2 + 1:
        return 0.0

    tr_list: list[float] = []
    pdm_list: list[float] = []
    mdm_list: list[float] = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm_list.append(up if up > dn and up > 0 else 0.0)
        mdm_list.append(dn if dn > up and dn > 0 else 0.0)

    def _wilder_sum(vals: list[float]) -> list[float]:
        """Sum-based Wilder smoothing (correct for TR / DM series)."""
        if len(vals) < period:
            return []
        result = [sum(vals[:period])]
        for v in vals[period:]:
            result.append(result[-1] - result[-1] / period + v)
        return result

    sm_tr = _wilder_sum(tr_list)
    sm_pdm = _wilder_sum(pdm_list)
    sm_mdm = _wilder_sum(mdm_list)

    dx_list: list[float] = []
    for st, sp, sm in zip(sm_tr, sm_pdm, sm_mdm):
        if st == 0.0:
            continue
        di_plus = 100.0 * sp / st
        di_minus = 100.0 * sm / st
        di_sum = di_plus + di_minus
        if di_sum == 0.0:
            continue
        dx_list.append(100.0 * abs(di_plus - di_minus) / di_sum)

    if len(dx_list) < period:
        return 0.0

    # ADX: average-based Wilder initialisation — DX is already 0-100
    adx = sum(dx_list[:period]) / period
    for dx in dx_list[period:]:
        adx = (adx * (period - 1) + dx) / period

    return round(adx, 2)


def _bollinger_width(closes: list[float], period: int = 20) -> float:
    """Bollinger Band width normalised by midpoint."""
    if len(closes) < period:
        return 0.0
    window = closes[-period:]
    mean = sum(window) / period
    if mean == 0.0:
        return 0.0
    variance = sum((x - mean) ** 2 for x in window) / period
    std = variance ** 0.5
    return round(4.0 * std / mean, 4)  # width = 4σ / mean (upper - lower relative to mid)


def _classify_regime(
    adx: float,
    rsi: float,
    atr_pct: float,
    bb_width: float,
    change_20d_pct: float,
) -> tuple[str, float]:
    """
    Map indicators to a regime label and confidence score [0, 1].

    Priority order:
      1. BULL_TRENDING  — directional momentum (ADX + RSI + 20d drift) confirms uptrend
      2. BEAR_TRENDING  — same logic, downside
      3. HIGH_VOLATILITY — only when truly extreme AND no clear trend direction
      4. LOW_LIQUIDITY  — very compressed, near-zero movement
      5. SIDEWAYS_RANGE — default / mixed / transitional signals
    """
    # 1 & 2: Trend signals take priority over volatility labels.
    #        A trending-but-volatile stock is still trending.
    if adx > 22:
        if rsi > 56 and change_20d_pct > 1.5:
            raw = min(0.95, 0.62 + (adx - 22) * 0.008 + (rsi - 56) * 0.005
                      + min(change_20d_pct, 15) * 0.003)
            return "bull_trending", round(raw, 2)
        if rsi < 44 and change_20d_pct < -1.5:
            raw = min(0.95, 0.62 + (adx - 22) * 0.008 + (44 - rsi) * 0.005
                      + min(-change_20d_pct, 15) * 0.003)
            return "bear_trending", round(raw, 2)

    # 3. High volatility — only for truly extreme volatility with no trend conviction
    if bb_width > 0.065 or atr_pct > 3.5:
        raw = min(0.95, 0.62 + max(bb_width - 0.065, 0) * 4.0 + max(atr_pct - 3.5, 0) * 0.06)
        return "high_volatility", round(raw, 2)

    # Weaker trend signals (ADX 16-22 or RSI not strongly directional)
    if change_20d_pct > 2.5 and rsi > 54:
        return "bull_trending", 0.62
    if change_20d_pct < -2.5 and rsi < 46:
        return "bear_trending", 0.62

    # 4. Low liquidity / compression
    if adx < 12 and bb_width < 0.015 and atr_pct < 0.6:
        return "low_liquidity", 0.65

    # 5. Sideways / transitional
    raw = min(0.82, 0.50 + max(20 - adx, 0) * 0.008)
    return "sideways_range", round(raw, 2)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _trend_direction(change_1d_pct: float) -> str:
    if change_1d_pct > 0.4:
        return "up"
    if change_1d_pct < -0.4:
        return "down"
    return "flat"


def _strength_from_confidence(confidence: float) -> str:
    if confidence >= 0.78:
        return "strong"
    if confidence >= 0.63:
        return "moderate"
    return "weak"


# ── Core service ───────────────────────────────────────────────────────────────

class RegimeService:
    """
    All public methods accept an AsyncSession and return plain dicts
    suitable for JSON serialisation.  Redis caching is handled by the
    API layer so that the service stays stateless and testable.
    """

    # ── OHLCV loading ──────────────────────────────────────────────────────────

    @staticmethod
    async def _load_ohlcv(db: AsyncSession, instrument_key: str, limit: int = 60) -> list[dict]:
        """Return up to `limit` daily candles, oldest-first, for a single instrument."""
        result = await db.execute(
            text("""
                SELECT timestamp, open::float, high::float,
                       low::float, close::float, volume
                FROM upstox_ohlcv
                WHERE instrument_key = :key AND timeframe = '1D'
                ORDER BY timestamp DESC
                LIMIT :lim
            """),
            {"key": instrument_key, "lim": limit},
        )
        rows = result.fetchall()
        return [
            {"ts": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}
            for r in reversed(rows)
        ]

    @staticmethod
    async def _load_ohlcv_batch(
        db: AsyncSession,
        instrument_keys: list[str],
        limit: int = 60,
    ) -> dict[str, list[dict]]:
        """
        Fetch up to `limit` daily candles for ALL instrument_keys in ONE query.
        Returns a dict keyed by instrument_key, candles in chronological order.
        Avoids N concurrent DB operations on a shared session.
        """
        if not instrument_keys:
            return {}

        result = await db.execute(
            text("""
                SELECT instrument_key,
                       timestamp, open::float, high::float,
                       low::float, close::float, volume
                FROM (
                    SELECT instrument_key, timestamp,
                           open, high, low, close, volume,
                           ROW_NUMBER() OVER (
                               PARTITION BY instrument_key
                               ORDER BY timestamp DESC
                           ) AS rn
                    FROM upstox_ohlcv
                    WHERE instrument_key = ANY(:keys) AND timeframe = '1D'
                ) ranked
                WHERE rn <= :lim
                ORDER BY instrument_key, timestamp ASC
            """),
            {"keys": instrument_keys, "lim": limit},
        )
        grouped: dict[str, list[dict]] = {}
        for row in result.fetchall():
            ikey = row[0]
            if ikey not in grouped:
                grouped[ikey] = []
            grouped[ikey].append(
                {"ts": row[1], "open": row[2], "high": row[3],
                 "low": row[4], "close": row[5], "volume": row[6]}
            )
        return grouped

    # ── Instrument resolution ─────────────────────────────────────────────────

    @staticmethod
    async def _resolve_instruments(
        db: AsyncSession, symbols: list[str]
    ) -> dict[str, dict]:
        """Map trading_symbol → {instrument_key, name} for available NSE EQ instruments."""
        result = await db.execute(
            text("""
                SELECT trading_symbol, instrument_key, name
                FROM instrument_master
                WHERE trading_symbol = ANY(:syms)
                  AND exchange = 'NSE'
                  AND (instrument_type = 'EQ' OR instrument_type IS NULL)
            """),
            {"syms": symbols},
        )
        return {
            row[0]: {"instrument_key": row[1], "name": row[2] or row[0]}
            for row in result.fetchall()
        }

    # ── Regime computation for a single instrument ────────────────────────────

    @staticmethod
    def _compute_regime_from_candles(candles: list[dict]) -> dict:
        """Compute indicators + classify regime from a list of daily candles."""
        if len(candles) < 20:
            return {
                "regime": "insufficient_data",
                "confidence": 0.0,
                "strength": "weak",
                "trend": "flat",
                "indicators": {"adx": 0.0, "rsi": 50.0, "atr": 0.0, "atr_pct": 0.0, "bollinger_width": 0.0},
                "current_price": candles[-1]["close"] if candles else 0.0,
                "change_pct": 0.0,
                "change_pct_20d": 0.0,
            }

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]

        current_price = closes[-1]
        change_1d = ((closes[-1] - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 else 0.0
        change_20d = ((closes[-1] - closes[-21]) / closes[-21] * 100) if len(closes) >= 21 else change_1d

        adx = _adx(highs, lows, closes)
        rsi = _rsi(closes)
        atr = _atr(highs, lows, closes)
        atr_pct = (atr / current_price * 100) if current_price > 0 else 0.0
        bb_width = _bollinger_width(closes)

        regime, confidence = _classify_regime(adx, rsi, atr_pct, bb_width, change_20d)

        return {
            "regime": regime,
            "confidence": confidence,
            "strength": _strength_from_confidence(confidence),
            "trend": _trend_direction(change_1d),
            "indicators": {
                "adx": adx,
                "rsi": rsi,
                "atr": round(atr, 4),
                "atr_pct": round(atr_pct, 2),
                "bollinger_width": bb_width,
            },
            "current_price": round(current_price, 2),
            "change_pct": round(change_1d, 2),
            "change_pct_20d": round(change_20d, 2),
        }

    # ── Single instrument regime ───────────────────────────────────────────────

    async def get_instrument_regime(
        self,
        db: AsyncSession,
        instrument_key: str,
        trading_symbol: str,
        company_name: str,
    ) -> dict:
        candles = await self._load_ohlcv(db, instrument_key)
        result = self._compute_regime_from_candles(candles)
        return {
            "instrument_key": instrument_key,
            "trading_symbol": trading_symbol,
            "company_name": company_name,
            "data_points": len(candles),
            **result,
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Index constituents ─────────────────────────────────────────────────────

    async def get_index_constituents(
        self,
        db: AsyncSession,
        index_key: str,
    ) -> dict:
        index_def = NIFTY_INDICES.get(index_key)
        if not index_def:
            return {"error": f"Unknown index: {index_key}", "constituents": [], "total": 0}

        symbols = index_def["constituents"]
        instrument_map = await self._resolve_instruments(db, symbols)

        async def _fetch(sym: str) -> dict | None:
            info = instrument_map.get(sym)
            if not info:
                return None
            try:
                return await self.get_instrument_regime(
                    db, info["instrument_key"], sym, info["name"]
                )
            except Exception:
                logger.debug("Regime computation failed for %s", sym)
                return None

        results = await asyncio.gather(*(_fetch(s) for s in symbols))
        constituents = [r for r in results if r and r.get("regime") != "insufficient_data"]
        constituents.sort(key=lambda x: x.get("change_pct", 0), reverse=True)

        return {
            "index_key": index_key,
            "name": index_def["name"],
            "constituents": constituents,
            "total": len(constituents),
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Index-level aggregated regime ─────────────────────────────────────────

    async def get_index_regime(self, db: AsyncSession, index_key: str) -> dict:
        index_def = NIFTY_INDICES.get(index_key)
        if not index_def:
            return {"index_key": index_key, "error": "Unknown index"}

        symbols = index_def["constituents"]
        instrument_map = await self._resolve_instruments(db, symbols)

        async def _fetch(sym: str) -> dict | None:
            info = instrument_map.get(sym)
            if not info:
                return None
            candles = await self._load_ohlcv(db, info["instrument_key"])
            r = self._compute_regime_from_candles(candles)
            return r if r["regime"] != "insufficient_data" else None

        results = await asyncio.gather(*(_fetch(s) for s in symbols))
        valid = [r for r in results if r]

        if not valid:
            return {
                "index_key": index_key,
                "name": index_def["name"],
                "short": index_def["short"],
                "regime": "insufficient_data",
                "confidence": 0.0,
                "strength": "weak",
                "change_pct": 0.0,
                "bullish_count": 0,
                "bearish_count": 0,
                "sideways_count": 0,
                "volatile_count": 0,
                "total_constituents": 0,
                "computed_at": datetime.now(timezone.utc).isoformat(),
            }

        regime_counts: dict[str, int] = {}
        total_conf = 0.0
        total_change = 0.0
        for r in valid:
            regime_counts[r["regime"]] = regime_counts.get(r["regime"], 0) + 1
            total_conf += r["confidence"]
            total_change += r["change_pct"]

        # Majority regime by count
        dominant = max(regime_counts, key=lambda k: regime_counts[k])
        avg_confidence = total_conf / len(valid)
        avg_change = total_change / len(valid)

        return {
            "index_key": index_key,
            "name": index_def["name"],
            "short": index_def["short"],
            "regime": dominant,
            "confidence": round(avg_confidence, 2),
            "strength": _strength_from_confidence(avg_confidence),
            "change_pct": round(avg_change, 2),
            "trend": _trend_direction(avg_change),
            "bullish_count": regime_counts.get("bull_trending", 0),
            "bearish_count": regime_counts.get("bear_trending", 0),
            "sideways_count": regime_counts.get("sideways_range", 0),
            "volatile_count": regime_counts.get("high_volatility", 0),
            "total_constituents": len(valid),
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Market overview ────────────────────────────────────────────────────────

    async def get_market_overview(self, db: AsyncSession) -> dict:
        """
        Compute regime for all 10 Nifty indices concurrently and
        return the market overview anchored on Nifty 50.
        """
        index_keys = list(NIFTY_INDICES.keys())
        all_regimes = await asyncio.gather(
            *(self.get_index_regime(db, k) for k in index_keys)
        )

        index_map = {r["index_key"]: r for r in all_regimes}
        nifty50 = index_map.get("nifty50", {})

        # Market breadth across all constituents of Nifty 50
        total = nifty50.get("total_constituents", 1) or 1
        breadth = {
            "bullish_pct": round(nifty50.get("bullish_count", 0) / total * 100, 1),
            "bearish_pct": round(nifty50.get("bearish_count", 0) / total * 100, 1),
            "sideways_pct": round(nifty50.get("sideways_count", 0) / total * 100, 1),
            "volatile_pct": round(nifty50.get("volatile_count", 0) / total * 100, 1),
        }

        return {
            "nifty50": nifty50,
            "breadth": breadth,
            "all_indices": list(all_regimes),
            "computed_at": datetime.now(timezone.utc).isoformat(),
        }


# Singleton — share one instance across requests (all state lives in DB/Redis)
regime_service = RegimeService()
