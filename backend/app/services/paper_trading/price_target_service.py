"""
Paper Trading — Price Target Service
======================================
Computes stop-loss and take-profit levels for manual trades that have no linked
TradeSuggestion.

Primary path  — ML ensemble (XGBoost + GRU):
    Load 49-feature vectors via FeatureLoader → run EnsemblePredictor → extract
    the ML-calibrated annualised volatility → apply the vol-based formula below.

Fallback path — OHLCV historical volatility:
    Used when the ML predictor is unavailable or feature data is absent.
    Computes annualised vol from 63 daily log-returns.

Both paths share the same formula (from EnsemblePredictor._post_process):

    daily_vol = annualised_vol / √252
    sl_pct    = 1.5 × daily_vol

    BUY:  SL  = entry × (1 − sl_pct)
          TP1 = entry × (1 + 1.5 × sl_pct)
          TP2 = entry × (1 + 2.5 × sl_pct)
          TP3 = entry × (1 + 4.0 × sl_pct)

    SELL: SL  = entry × (1 + sl_pct)
          TP1 = entry × (1 − 1.5 × sl_pct)
          TP2 = entry × (1 − 2.5 × sl_pct)
          TP3 = entry × (1 − 4.0 × sl_pct)

Caching strategy:
    ML path:   EnsemblePredictor.predict(use_cache=True) maintains its own
               5-minute Redis cache at ml:ensemble:prediction:{key}:{tf}.
    Fallback:  The OHLCV intermediate result (annualised_vol, regime_type,
               candles_used) is cached per symbol with a 4-hour TTL at
               cai:pt:vol:{SYMBOL}.  Final price levels are cheap arithmetic
               on top of the cached context.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIRegimeDetection
from app.models.upstox_data import InstrumentMaster, UpstoxOHLCV

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

_CANDLES_LOOKBACK = 63   # ~3 months of NSE trading days — stable vol estimate
_MIN_CANDLES = 20        # minimum candles required to compute meaningful vol
_VOL_FLOOR = 0.05        # 5% annualised floor — prevents zero SL on illiquid ticks
_DP2 = Decimal("0.01")

# Cache TTL — 4 hours.  Volatility changes on a daily scale so this is safe
# during market hours without being stale.
VOL_CACHE_TTL = 14_400


# ── Data Transfer Object ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class PriceTargets:
    symbol: str
    direction: str            # "BUY" | "SELL"
    entry_price: Decimal
    stop_loss: Decimal
    take_profit_1: Decimal
    take_profit_2: Decimal
    take_profit_3: Decimal
    volatility_annualized: float
    regime_type: str | None
    candles_used: int
    source: str               # "ml_ensemble" | "historical_volatility"
    computed_at: datetime


# ── Intermediate cached result ─────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class _VolatilityContext:
    volatility: float
    regime_type: str | None
    candles_used: int


# ── Public API ─────────────────────────────────────────────────────────────────

async def compute_price_targets(
    session: AsyncSession,
    symbol: str,
    direction: str,
    entry_price: Decimal,
    cache_ctx: _VolatilityContext | None = None,
) -> PriceTargets | None:
    """
    Compute SL/TP levels for a manual trade.

    Parameters
    ----------
    session:     Active async DB session.
    symbol:      NSE trading symbol (e.g. "RELIANCE").
    direction:   "BUY" or "SELL".
    entry_price: User's intended entry price (Decimal for monetary precision).
    cache_ctx:   Pre-loaded volatility context (pass when already fetched to
                 skip the DB query — used by the endpoint's cache-hit path).

    Returns
    -------
    PriceTargets dataclass, or None when insufficient data exists.
    """
    ctx = cache_ctx or await _fetch_volatility_context(session, symbol.upper())
    if ctx is None:
        return None

    return _apply_formula(symbol.upper(), direction, entry_price, ctx, source="historical_volatility")


async def fetch_volatility_context(
    session: AsyncSession,
    symbol: str,
) -> _VolatilityContext | None:
    """
    Public wrapper around _fetch_volatility_context.
    Exposed so the API endpoint can cache the intermediate result independently.
    """
    return await _fetch_volatility_context(session, symbol.upper())


def volatility_context_to_dict(ctx: _VolatilityContext) -> dict[str, Any]:
    return {
        "volatility": ctx.volatility,
        "regime_type": ctx.regime_type,
        "candles_used": ctx.candles_used,
    }


def volatility_context_from_dict(d: dict[str, Any]) -> _VolatilityContext:
    return _VolatilityContext(
        volatility=float(d["volatility"]),
        regime_type=d.get("regime_type"),
        candles_used=int(d["candles_used"]),
    )


def apply_formula(
    symbol: str,
    direction: str,
    entry_price: Decimal,
    ctx: _VolatilityContext,
    source: str = "historical_volatility",
) -> PriceTargets:
    """Public shim for the endpoint's cache-hit path (no DB needed)."""
    return _apply_formula(symbol.upper(), direction, entry_price, ctx, source=source)


async def compute_price_targets_ml(
    session: AsyncSession,
    redis: "Redis",
    predictor: Any,          # EnsemblePredictor
    ensemble: Any,           # LoadedEnsemble
    symbol: str,
    direction: str,
    entry_price: Decimal,
) -> PriceTargets | None:
    """
    Compute SL/TP levels using the live XGBoost + GRU ensemble.

    Loads 49-feature vectors via FeatureLoader, runs the full ensemble to
    obtain a calibrated annualised volatility, and applies the standard
    vol-based formula.  Regime detection is fetched separately from the AI
    regime table for display context.

    Returns None (caller falls back to OHLCV path) when:
      - No NSE EQ instrument_key exists for the symbol
      - FeatureLoader cannot produce sufficient feature data
      - Predictor raises any exception (logged at WARNING level)
    """
    from app.ml.inference.feature_loader import FeatureLoader

    symbol_upper = symbol.upper()

    # 1. Resolve NSE EQ instrument_key — FeatureLoader requires it
    instr_stmt = (
        select(InstrumentMaster.instrument_key)
        .where(
            and_(
                InstrumentMaster.trading_symbol == symbol_upper,
                InstrumentMaster.exchange == "NSE",
                InstrumentMaster.instrument_type == "EQ",
            )
        )
        .limit(1)
    )
    instr_row = (await session.execute(instr_stmt)).first()
    if instr_row is None:
        logger.debug("ml_price_targets: no NSE EQ entry for symbol=%s", symbol_upper)
        return None
    instrument_key: str = instr_row[0]

    # 2. Load 49-feature vectors — uses feature store + on-demand OHLCV fallback
    feature_loader = FeatureLoader(
        db=session,
        redis=redis,
        sequence_length=ensemble.sequence_length,
        n_features=ensemble.n_features,
        feature_names=ensemble.feature_names,
    )
    tabular, sequence, current_price, ml_volatility = await feature_loader.load_features(
        symbol=instrument_key,
        timeframe="1d",
    )

    if current_price <= 0:
        logger.warning(
            "ml_price_targets: feature loader returned invalid price %.4f for %s",
            current_price, symbol_upper,
        )
        return None

    # 3. Run ensemble inference (EnsemblePredictor maintains its own 5-min cache)
    prediction = await predictor.predict(
        features_tabular=tabular,
        features_sequence=sequence,
        symbol=instrument_key,
        current_price=current_price,
        volatility=ml_volatility,
        timeframe="1d",
        use_cache=True,
    )

    # Use the volatility the model actually ran with — this is the ML-calibrated
    # estimate derived from 49 features, more comprehensive than raw log-returns.
    volatility = max(float(prediction.get("volatility") or ml_volatility), _VOL_FLOOR)

    # 4. Fetch latest regime detection (best-effort — enriches the response UI)
    regime_stmt = (
        select(AIRegimeDetection.regime_type)
        .where(AIRegimeDetection.symbol == symbol_upper)
        .order_by(desc(AIRegimeDetection.detection_timestamp))
        .limit(1)
    )
    regime_row = (await session.execute(regime_stmt)).first()
    regime_type = regime_row[0] if regime_row else None

    ctx = _VolatilityContext(
        volatility=volatility,
        regime_type=regime_type,
        # GRU used this many sequential candles — closest proxy to "candles used"
        candles_used=ensemble.sequence_length,
    )

    return _apply_formula(symbol_upper, direction, entry_price, ctx, source="ml_ensemble")


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _fetch_volatility_context(
    session: AsyncSession,
    symbol: str,
) -> _VolatilityContext | None:
    """
    Query DB for the last N daily candles, compute annualised vol, and fetch
    the latest regime detection.  Returns None when insufficient data exists.
    """
    # 1. Resolve NSE EQ instrument_key for this symbol
    instr_stmt = (
        select(InstrumentMaster.instrument_key)
        .where(
            and_(
                InstrumentMaster.trading_symbol == symbol,
                InstrumentMaster.exchange == "NSE",
                InstrumentMaster.instrument_type == "EQ",
            )
        )
        .limit(1)
    )
    instr_row = (await session.execute(instr_stmt)).first()
    if instr_row is None:
        logger.debug("price_targets: no NSE EQ entry for symbol=%s", symbol)
        return None
    instrument_key: str = instr_row[0]

    # 2. Fetch last (LOOKBACK + 1) daily closes — extra row needed to compute
    #    the first log return (requires two consecutive prices).
    candles_stmt = (
        select(UpstoxOHLCV.close)
        .where(
            and_(
                UpstoxOHLCV.instrument_key == instrument_key,
                UpstoxOHLCV.timeframe == "1D",
            )
        )
        .order_by(desc(UpstoxOHLCV.timestamp))
        .limit(_CANDLES_LOOKBACK + 1)
    )
    rows = (await session.execute(candles_stmt)).all()

    if len(rows) < _MIN_CANDLES + 1:
        logger.debug(
            "price_targets: insufficient candles for %s (got %d, need %d)",
            symbol, len(rows), _MIN_CANDLES + 1,
        )
        return None

    # Rows are DESC; reverse for chronological order before computing returns
    closes = [float(r[0]) for r in reversed(rows)]

    # Guard against zero/negative closes (data quality)
    if any(c <= 0 for c in closes):
        logger.warning("price_targets: non-positive close price for symbol=%s", symbol)
        return None

    # 3. Compute annualised historical volatility via log returns
    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, len(closes))
    ]
    n = len(log_returns)
    mean_r = sum(log_returns) / n
    variance = sum((r - mean_r) ** 2 for r in log_returns) / (n - 1)  # sample var
    daily_std = math.sqrt(variance)
    volatility = max(daily_std * math.sqrt(252), _VOL_FLOOR)

    # 4. Fetch latest regime detection for this symbol (best-effort)
    regime_stmt = (
        select(AIRegimeDetection.regime_type)
        .where(AIRegimeDetection.symbol == symbol)
        .order_by(desc(AIRegimeDetection.detection_timestamp))
        .limit(1)
    )
    regime_row = (await session.execute(regime_stmt)).first()
    regime_type = regime_row[0] if regime_row else None

    return _VolatilityContext(
        volatility=volatility,
        regime_type=regime_type,
        candles_used=n,
    )


def _apply_formula(
    symbol: str,
    direction: str,
    entry_price: Decimal,
    ctx: _VolatilityContext,
    source: str = "historical_volatility",
) -> PriceTargets:
    """Apply the EnsemblePredictor formula to produce price levels."""
    ep = float(entry_price)
    daily_vol = ctx.volatility / math.sqrt(252)
    sl_pct = 1.5 * daily_vol

    if direction == "BUY":
        sl  = ep * (1.0 - sl_pct)
        tp1 = ep * (1.0 + 1.5 * sl_pct)
        tp2 = ep * (1.0 + 2.5 * sl_pct)
        tp3 = ep * (1.0 + 4.0 * sl_pct)
    else:  # SELL
        sl  = ep * (1.0 + sl_pct)
        tp1 = ep * (1.0 - 1.5 * sl_pct)
        tp2 = ep * (1.0 - 2.5 * sl_pct)
        tp3 = ep * (1.0 - 4.0 * sl_pct)

    def _dec(v: float) -> Decimal:
        return Decimal(str(v)).quantize(_DP2, rounding=ROUND_HALF_UP)

    return PriceTargets(
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=_dec(sl),
        take_profit_1=_dec(tp1),
        take_profit_2=_dec(tp2),
        take_profit_3=_dec(tp3),
        volatility_annualized=round(ctx.volatility, 6),
        regime_type=ctx.regime_type,
        candles_used=ctx.candles_used,
        source=source,
        computed_at=datetime.now(timezone.utc),
    )
