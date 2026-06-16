"""
BacktestEngine — Historical strategy simulation in three replay modes.

Modes
-----
signal_replay  — Walk historical ai_trading_signals rows through the live 7-gate
                 pipeline.  SL/TP are sourced from the ML prediction payload
                 already stored in the signal row.  Bar-by-bar high/low
                 resolution is not available so exit triggers are approximated
                 against the *next* signal's price for the same symbol.

model_replay   — Re-run the EnsemblePredictor on raw OHLCV bars.  Provides
                 bar-level SL/TP resolution: within each simulated bar the
                 engine checks whether the bar's high/low crosses the open
                 position's SL or TP thresholds.  SL wins on same-bar conflicts.

hybrid         — Detects the boundary of available signal history (MIN signal
                 timestamp) and splices: model_replay for [start, boundary),
                 signal_replay for [boundary, end].

Architecture
------------
All three modes share the same position simulation kernel (_check_exits,
_compute_pnl) and metric aggregation (_aggregate_metrics).  Per-symbol
parallelism is achieved with asyncio.gather; each symbol is independent.

Output schema (result_metrics JSONB)
-------------------------------------
{
  "mode": "signal_replay",
  "start_dt": "...", "end_dt": "...",
  "symbols": [...],
  "metrics": {
    "total_trades": int,
    "win_rate_pct": float | null,
    "sharpe_ratio": float | null,
    "sortino_ratio": float | null,
    "calmar_ratio": float | null,
    "max_drawdown_pct": float | null,
    "avg_daily_return_pct": float | null,
    "total_net_pnl": float,
    "best_trade_pnl": float | null,
    "worst_trade_pnl": float | null,
  },
  "equity_curve": [{"date": "YYYY-MM-DD", "equity": float}, ...],
  "trades": [{...SimulatedTrade fields...}, ...],
  "per_symbol_stats": {symbol: {wins, losses, net_pnl}, ...},
  "per_regime_stats": {regime: {n, wins, losses, net_pnl, win_rate_pct}, ...},
}
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AITradingSignal
from app.ml.features.feature_pipeline import compute_features_for_symbol
from app.models.strategies import Strategy, StrategyBacktestRun
from app.services.regime_service import (
    REGIME_DETECTOR_VERSION,
    RegimeService as _RegimeService,
)
from app.services.strategy_engine.filter_pipeline import (
    StrategyFilterPipeline,
    strategy_filter_pipeline,
)
from app.services.strategy_engine.market_context import (
    MarketContext,
    resolve_market_context,
)

logger = logging.getLogger(__name__)

# NSE brokerage/tax cost approximation (round-trip %, both sides)
_ROUND_TRIP_COST_PCT: float = 0.05  # 5 bps — conservative flat estimate

# Minimum bars required before model_replay begins predicting
_MIN_LOOKBACK_BARS: int = 10


# ── Data containers ───────────────────────────────────────────────────────────

@dataclass
class SimulatedTrade:
    symbol: str
    direction: str              # "LONG" | "SHORT"
    entry_price: float
    exit_price: float
    quantity: float
    stop_loss: float | None
    take_profit_1: float | None
    exit_reason: str            # "TP1" | "SL" | "SIGNAL_EXPIRED" | "PERIOD_END"
    entry_at: datetime
    exit_at: datetime
    gross_pnl: float
    total_charges: float
    net_pnl: float
    pnl_pct: float
    hold_duration_seconds: float
    # ── Regime provenance (A1) ────────────────────────────────────────────────
    regime_label: str = "unknown"       # e.g. "bull_trending", "sideways_range"
    regime_detector_version: str | None = None  # "stored" | REGIME_DETECTOR_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_price": round(self.entry_price, 4),
            "exit_price": round(self.exit_price, 4),
            "quantity": round(self.quantity, 4),
            "stop_loss": round(self.stop_loss, 4) if self.stop_loss else None,
            "take_profit_1": round(self.take_profit_1, 4) if self.take_profit_1 else None,
            "exit_reason": self.exit_reason,
            "entry_at": self.entry_at.isoformat(),
            "exit_at": self.exit_at.isoformat(),
            "gross_pnl": round(self.gross_pnl, 4),
            "total_charges": round(self.total_charges, 6),
            "net_pnl": round(self.net_pnl, 4),
            "pnl_pct": round(self.pnl_pct, 4),
            "hold_duration_seconds": round(self.hold_duration_seconds, 1),
            "regime_label": self.regime_label,
            "regime_detector_version": self.regime_detector_version,
        }


@dataclass
class _OpenPosition:
    symbol: str
    direction: str
    entry_price: float
    quantity: float
    stop_loss: float | None
    take_profit_1: float | None
    entry_at: datetime
    regime_label: str = "unknown"
    regime_detector_version: str | None = None


@dataclass
class BacktestResult:
    run_id: UUID
    trades: list[SimulatedTrade] = field(default_factory=list)

    def build_result_metrics(
        self,
        mode: str,
        start_dt: datetime,
        end_dt: datetime,
        symbols: list[str],
    ) -> dict[str, Any]:
        return {
            "mode": mode,
            "start_dt": start_dt.isoformat(),
            "end_dt": end_dt.isoformat(),
            "symbols": symbols,
            "metrics": _aggregate_metrics(self.trades),
            "equity_curve": _build_equity_curve(self.trades),
            "trades": [t.to_dict() for t in self.trades],
            "per_symbol_stats": _per_symbol_stats(self.trades),
            "per_regime_stats": _per_regime_stats(self.trades),
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_pnl(
    direction: str,
    entry_price: float,
    exit_price: float,
    quantity: float,
) -> tuple[float, float, float]:
    """Return (gross_pnl, charges, net_pnl)."""
    if direction == "LONG":
        gross = (exit_price - entry_price) * quantity
    else:
        gross = (entry_price - exit_price) * quantity

    charges = (entry_price + exit_price) * quantity * (_ROUND_TRIP_COST_PCT / 100.0)
    net = gross - charges
    return gross, charges, net


def _check_exits(
    pos: _OpenPosition,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    bar_ts: datetime,
) -> tuple[float, str] | None:
    """
    Given a bar's OHLCV extremes, determine if SL or TP1 was hit.

    SL wins on same-bar conflicts (conservative assumption).
    Returns (exit_price, reason) or None if still open.
    """
    sl = pos.stop_loss
    tp = pos.take_profit_1

    if pos.direction == "LONG":
        sl_hit = sl is not None and bar_low <= sl
        tp_hit = tp is not None and bar_high >= tp
        if sl_hit and tp_hit:
            return sl, "SL"
        if sl_hit:
            return sl, "SL"
        if tp_hit:
            return tp, "TP1"
    else:  # SHORT
        sl_hit = sl is not None and bar_high >= sl
        tp_hit = tp is not None and bar_low <= tp
        if sl_hit and tp_hit:
            return sl, "SL"
        if sl_hit:
            return sl, "SL"
        if tp_hit:
            return tp, "TP1"
    return None


def _aggregate_metrics(trades: list[SimulatedTrade]) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {
            "total_trades": 0,
            "win_rate_pct": None,
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "calmar_ratio": None,
            "max_drawdown_pct": None,
            "avg_daily_return_pct": None,
            "total_net_pnl": 0.0,
            "best_trade_pnl": None,
            "worst_trade_pnl": None,
        }

    pnl_pcts = np.array([t.pnl_pct for t in trades], dtype=np.float64)
    net_pnls = np.array([t.net_pnl for t in trades], dtype=np.float64)

    wins = int((net_pnls > 0).sum())
    win_rate = (wins / n * 100.0) if n > 0 else None

    # Compounded equity curve for drawdown
    equity = np.cumprod(1.0 + pnl_pcts / 100.0)
    peak = np.maximum.accumulate(equity)
    peak_safe = np.where(peak > 0, peak, 1.0)
    dd = np.where(peak > 0, (peak - equity) / peak_safe * 100.0, 0.0)
    max_dd = float(dd.max()) if len(dd) > 0 else 0.0

    # Sharpe / Sortino
    if n >= 2:
        mean_r = float(np.mean(pnl_pcts))
        std_r = float(np.std(pnl_pcts, ddof=1))
        sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else None

        neg_sq = np.minimum(pnl_pcts, 0.0) ** 2
        downside_dev = float(math.sqrt(float(np.mean(neg_sq))))
        sortino = (mean_r / downside_dev * math.sqrt(252)) if downside_dev > 0 else None
    else:
        mean_r = float(np.mean(pnl_pcts)) if n else 0.0
        sharpe = sortino = None

    # Calmar — CAGR / max_drawdown
    calmar: float | None = None
    if max_dd > 0 and n >= 2:
        entry_dates = [t.entry_at for t in trades]
        exit_dates = [t.exit_at for t in trades]
        span_days = (max(exit_dates) - min(entry_dates)).total_seconds() / 86400.0
        if span_days > 1.0:
            years = span_days / 365.0
            final_equity = float(equity[-1])
            cagr = ((final_equity ** (1.0 / years)) - 1.0) * 100.0
            calmar = cagr / max_dd

    avg_daily_return = float(mean_r) if n >= 1 else None

    return {
        "total_trades": n,
        "win_rate_pct": round(win_rate, 2) if win_rate is not None else None,
        "sharpe_ratio": round(sharpe, 4) if sharpe is not None else None,
        "sortino_ratio": round(sortino, 4) if sortino is not None else None,
        "calmar_ratio": round(calmar, 4) if calmar is not None else None,
        "max_drawdown_pct": round(max_dd, 4),
        "avg_daily_return_pct": round(avg_daily_return, 4) if avg_daily_return is not None else None,
        "total_net_pnl": round(float(net_pnls.sum()), 4),
        "best_trade_pnl": round(float(net_pnls.max()), 4),
        "worst_trade_pnl": round(float(net_pnls.min()), 4),
    }


def _build_equity_curve(trades: list[SimulatedTrade]) -> list[dict[str, Any]]:
    if not trades:
        return []
    sorted_trades = sorted(trades, key=lambda t: t.exit_at)
    equity = 100.0
    curve: list[dict[str, Any]] = []
    for t in sorted_trades:
        equity *= 1.0 + t.pnl_pct / 100.0
        curve.append({
            "date": t.exit_at.strftime("%Y-%m-%d"),
            "equity": round(equity, 4),
            "pnl_pct": round(t.pnl_pct, 4),
        })
    return curve


def _per_symbol_stats(trades: list[SimulatedTrade]) -> dict[str, Any]:
    stats: dict[str, dict[str, Any]] = {}
    for t in trades:
        s = stats.setdefault(t.symbol, {"wins": 0, "losses": 0, "net_pnl": 0.0})
        if t.net_pnl > 0:
            s["wins"] += 1
        else:
            s["losses"] += 1
        s["net_pnl"] = round(s["net_pnl"] + t.net_pnl, 4)
    return stats


def _per_regime_stats(trades: list[SimulatedTrade]) -> dict[str, Any]:
    """
    Aggregate trade outcomes by regime label.

    Each bucket explicitly surfaces N so thin-regime slices cannot be
    over-interpreted by callers.  Acceptance criterion: every metric is
    accompanied by its regime-sample count (A1 parity requirement).
    """
    buckets: dict[str, dict[str, Any]] = {}
    for t in trades:
        b = buckets.setdefault(
            t.regime_label,
            {"n": 0, "wins": 0, "losses": 0, "net_pnl": 0.0},
        )
        b["n"] += 1
        if t.net_pnl > 0:
            b["wins"] += 1
        else:
            b["losses"] += 1
        b["net_pnl"] = round(b["net_pnl"] + t.net_pnl, 4)
    for b in buckets.values():
        n = b["n"]
        b["win_rate_pct"] = round(100.0 * b["wins"] / n, 2) if n > 0 else None
    return buckets


def _build_simulated_trade(
    pos: _OpenPosition,
    exit_price: float,
    exit_reason: str,
    exit_at: datetime,
) -> SimulatedTrade:
    gross, charges, net = _compute_pnl(
        pos.direction, pos.entry_price, exit_price, pos.quantity
    )
    entry_val = pos.entry_price * pos.quantity
    pnl_pct = (net / entry_val * 100.0) if entry_val > 0 else 0.0
    return SimulatedTrade(
        symbol=pos.symbol,
        direction=pos.direction,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        quantity=pos.quantity,
        stop_loss=pos.stop_loss,
        take_profit_1=pos.take_profit_1,
        exit_reason=exit_reason,
        entry_at=pos.entry_at,
        exit_at=exit_at,
        gross_pnl=gross,
        total_charges=charges,
        net_pnl=net,
        pnl_pct=pnl_pct,
        hold_duration_seconds=(exit_at - pos.entry_at).total_seconds(),
        regime_label=pos.regime_label,
        regime_detector_version=pos.regime_detector_version,
    )


# ── Mock strategy for pipeline compatibility ──────────────────────────────────

class _MockStrategy:
    """Duck-typed Strategy object for feeding the live 7-gate pipeline."""

    __slots__ = ("id", "name", "definition")

    def __init__(self, strategy_id: UUID, strategy_name: str, snapshot: dict[str, Any]) -> None:
        self.id = strategy_id
        self.name = strategy_name
        self.definition = snapshot


# ── BacktestEngine ────────────────────────────────────────────────────────────

class BacktestEngine:
    """
    Stateless backtest engine.  Instantiate once, call `run()` per job.

    The engine is intentionally synchronous in its simulation kernel and async
    only at the I/O boundary (DB queries).  This keeps the hot loop fast and
    avoids event-loop overhead for CPU-bound computations.
    """

    def __init__(
        self,
        ml_predictor: Any | None = None,  # EnsemblePredictor — required for model_replay
    ) -> None:
        self._ml_predictor = ml_predictor
        self._pipeline: StrategyFilterPipeline = strategy_filter_pipeline

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(
        self,
        run: StrategyBacktestRun,
        strategy: Strategy,
        db: AsyncSession,
        *,
        progress_callback: Any = None,  # Callable[[int], Awaitable[None]]
    ) -> BacktestResult:
        """
        Execute the full backtest and return aggregated results.

        Args:
            run:               The StrategyBacktestRun ORM row (read-only here).
            strategy:          The Strategy ORM row with `.definition`.
            db:                Async DB session (used for historical data queries).
            progress_callback: Optional async callable(pct: int) for progress
                               reporting.  Called after each symbol is processed.
        """
        mode = run.mode
        start_dt = run.start_dt.replace(tzinfo=timezone.utc) if run.start_dt.tzinfo is None else run.start_dt
        end_dt = run.end_dt.replace(tzinfo=timezone.utc) if run.end_dt.tzinfo is None else run.end_dt
        symbols = list(run.symbols)

        logger.info(
            "BacktestEngine.run: run_id=%s mode=%s symbols=%s [%s → %s]",
            run.id, mode, symbols, start_dt.date(), end_dt.date(),
        )

        mock = _MockStrategy(strategy.id, strategy.name, strategy.definition)
        result = BacktestResult(run_id=run.id)

        if mode == "signal_replay":
            await self._signal_replay(result, mock, symbols, start_dt, end_dt, db, progress_callback)
        elif mode == "model_replay":
            if self._ml_predictor is None:
                raise RuntimeError("ml_predictor is required for model_replay mode")
            await self._model_replay(result, mock, symbols, start_dt, end_dt, db, progress_callback)
        else:  # hybrid
            boundary = await self._detect_signal_boundary(symbols, start_dt, end_dt, db)
            if boundary is None or boundary <= start_dt:
                # No signal history — fall through to full model_replay
                if self._ml_predictor is None:
                    raise RuntimeError("ml_predictor is required for hybrid mode when no signal history exists")
                await self._model_replay(result, mock, symbols, start_dt, end_dt, db, progress_callback)
            elif boundary >= end_dt:
                # Full signal history available
                await self._signal_replay(result, mock, symbols, start_dt, end_dt, db, progress_callback)
            else:
                # Split: model_replay [start, boundary) + signal_replay [boundary, end]
                if self._ml_predictor is None:
                    raise RuntimeError("ml_predictor is required for hybrid mode")
                await self._model_replay(result, mock, symbols, start_dt, boundary, db, None)
                await self._signal_replay(result, mock, symbols, boundary, end_dt, db, progress_callback)

        return result

    # ── Signal replay ─────────────────────────────────────────────────────────

    async def _signal_replay(
        self,
        result: BacktestResult,
        mock: _MockStrategy,
        symbols: list[str],
        start_dt: datetime,
        end_dt: datetime,
        db: AsyncSession,
        progress_cb: Any,
    ) -> None:
        total = len(symbols)
        for idx, symbol in enumerate(symbols, 1):
            trades = await self._replay_signals_for_symbol(mock, symbol, start_dt, end_dt, db)
            result.trades.extend(trades)
            if progress_cb:
                await progress_cb(int(idx / total * 100))

    async def _replay_signals_for_symbol(
        self,
        mock: _MockStrategy,
        symbol: str,
        start_dt: datetime,
        end_dt: datetime,
        db: AsyncSession,
    ) -> list[SimulatedTrade]:
        stmt = (
            select(AITradingSignal)
            .where(
                AITradingSignal.symbol == symbol,
                AITradingSignal.signal_timestamp >= start_dt,
                AITradingSignal.signal_timestamp <= end_dt,
                AITradingSignal.action.in_(["BUY", "SELL"]),
            )
            .order_by(AITradingSignal.signal_timestamp)
        )
        rows = (await db.execute(stmt)).scalars().all()
        if not rows:
            return []

        trades: list[SimulatedTrade] = []
        open_pos: _OpenPosition | None = None

        for i, sig in enumerate(rows):
            # Build MarketContext from the historical signal row
            ctx = await resolve_market_context(
                symbol=sig.symbol,
                action=sig.action,
                confidence_score=float(sig.confidence_score),
                regime_type=sig.regime_type,
                time_horizon=sig.time_horizon or "intraday",
                technical_indicators=sig.technical_indicators or {},
                ml_predictions=sig.ml_predictions or {},
                instrument_key=None,  # no Redis in backtest
                redis=None,
            )

            # Run the 7-gate pipeline with historical timestamp for session gate
            pipeline_result = await self._pipeline.evaluate(
                mock,  # type: ignore[arg-type]
                ctx,
                db=None,
                user_id=0,
                evaluation_time=sig.signal_timestamp,
            )

            # If there's an open position, check for exit using current signal's price
            if open_pos is not None:
                ref_price = float(sig.ml_predictions.get("prediction", {}).get("entry_price") or 0) if sig.ml_predictions else 0
                if ref_price <= 0:
                    ref_price = float(sig.technical_indicators.get("indicators", {}).get("close") or 0) if sig.technical_indicators else 0

                if ref_price > 0:
                    exit_hit = _check_exits(
                        open_pos,
                        bar_high=ref_price * 1.005,  # conservative: 0.5% range assumption
                        bar_low=ref_price * 0.995,
                        bar_close=ref_price,
                        bar_ts=sig.signal_timestamp,
                    )
                    if exit_hit:
                        ex_price, reason = exit_hit
                        trades.append(_build_simulated_trade(open_pos, ex_price, reason, sig.signal_timestamp))
                        open_pos = None
                    elif sig.action != open_pos.direction.replace("LONG", "BUY").replace("SHORT", "SELL"):
                        # Opposite signal — close current position
                        trades.append(_build_simulated_trade(open_pos, ref_price, "SIGNAL_EXPIRED", sig.signal_timestamp))
                        open_pos = None

            if not pipeline_result.passed:
                continue

            # Open new position
            ml_pred = (sig.ml_predictions or {}).get("prediction") or {}
            entry_px = float(ml_pred.get("entry_price") or 0) or None
            if entry_px is None or entry_px <= 0:
                continue

            targets = ml_pred.get("targets") or []
            tp1 = float(targets[0]) if targets else None
            sl = float(sig.stop_loss) if sig.stop_loss else None

            direction = "LONG" if sig.action == "BUY" else "SHORT"
            open_pos = _OpenPosition(
                symbol=symbol,
                direction=direction,
                entry_price=entry_px,
                quantity=1.0,  # normalised — P&L expressed as per-unit
                stop_loss=sl,
                take_profit_1=tp1,
                entry_at=sig.signal_timestamp,
                # Regime was computed by the production detector at signal-generation
                # time and persisted in the DB; "stored" signals we have no live
                # version available here.
                regime_label=sig.regime_type or "unknown",
                regime_detector_version="stored",
            )

        # Force-close any position still open at period end
        if open_pos and rows:
            last = rows[-1]
            last_ml = (last.ml_predictions or {}).get("prediction") or {}
            close_px = float(last_ml.get("entry_price") or 0)
            if close_px > 0:
                trades.append(_build_simulated_trade(open_pos, close_px, "PERIOD_END", end_dt))

        return trades

    # ── Model replay ──────────────────────────────────────────────────────────

    async def _model_replay(
        self,
        result: BacktestResult,
        mock: _MockStrategy,
        symbols: list[str],
        start_dt: datetime,
        end_dt: datetime,
        db: AsyncSession,
        progress_cb: Any,
    ) -> None:
        total = len(symbols)
        for idx, symbol in enumerate(symbols, 1):
            try:
                trades = await self._replay_model_for_symbol(mock, symbol, start_dt, end_dt, db)
                result.trades.extend(trades)
            except Exception as exc:
                logger.warning("model_replay failed for symbol %s: %s", symbol, exc)
            if progress_cb:
                await progress_cb(int(idx / total * 100))

    async def _replay_model_for_symbol(
        self,
        mock: _MockStrategy,
        symbol: str,
        start_dt: datetime,
        end_dt: datetime,
        db: AsyncSession,
    ) -> list[SimulatedTrade]:
        # Extra lookback to ensure rolling features are warm
        lookback_start = start_dt - pd.Timedelta(days=self._ml_predictor.sequence_length * 2)

        features_df = await compute_features_for_symbol(
            symbol=symbol,
            start_date=lookback_start.replace(tzinfo=None),
            end_date=end_dt.replace(tzinfo=None),
            timeframe="1D",
            db=db,
            include_sentiment=False,
        )

        if features_df.empty or len(features_df) < _MIN_LOOKBACK_BARS:
            logger.warning("Insufficient OHLCV data for model_replay symbol=%s", symbol)
            return []

        # Align feature columns to the trained manifest for training-inference parity
        feature_manifest = list(self._ml_predictor.feature_names)
        non_feature_cols = {"symbol", "timestamp", "date", "open", "high", "low", "close", "volume"}
        if feature_manifest:
            available = set(features_df.columns)
            ordered_cols = [c for c in feature_manifest if c in available]
            missing = [c for c in feature_manifest if c not in available]
            if missing:
                for c in missing:
                    features_df[c] = 0.0
                ordered_cols = feature_manifest
            feature_matrix = features_df[ordered_cols].values.astype(np.float32)
        else:
            data_cols = [c for c in features_df.columns if c not in non_feature_cols]
            feature_matrix = features_df[data_cols].values.astype(np.float32)

        seq_len = self._ml_predictor.sequence_length
        n_features = feature_matrix.shape[1]

        # Determine which rows fall in the simulation window
        if "timestamp" in features_df.columns:
            ts_col = pd.to_datetime(features_df["timestamp"], utc=True)
        elif "date" in features_df.columns:
            ts_col = pd.to_datetime(features_df["date"], utc=True)
        else:
            ts_col = pd.Series(
                pd.date_range(start=lookback_start, periods=len(features_df), freq="D", tz="UTC")
            )

        # Price columns for SL/TP bar-level checking
        has_ohlcv = all(c in features_df.columns for c in ("open", "high", "low", "close"))

        trades: list[SimulatedTrade] = []
        open_pos: _OpenPosition | None = None

        for i in range(seq_len, len(feature_matrix)):
            bar_ts = ts_col.iloc[i]
            if bar_ts.tzinfo is None:
                bar_ts = bar_ts.tz_localize("UTC")

            # Only simulate bars within [start_dt, end_dt]
            if bar_ts < start_dt or bar_ts > end_dt:
                # Still need to check exits for open positions
                if open_pos and has_ohlcv:
                    bh = float(features_df["high"].iloc[i])
                    bl = float(features_df["low"].iloc[i])
                    bc = float(features_df["close"].iloc[i])
                    hit = _check_exits(open_pos, bh, bl, bc, bar_ts)
                    if hit:
                        ex_price, reason = hit
                        trades.append(_build_simulated_trade(open_pos, ex_price, reason, bar_ts))
                        open_pos = None
                continue

            # Check exit on current bar before opening a new position
            if open_pos and has_ohlcv:
                bh = float(features_df["high"].iloc[i])
                bl = float(features_df["low"].iloc[i])
                bc = float(features_df["close"].iloc[i])
                hit = _check_exits(open_pos, bh, bl, bc, bar_ts)
                if hit:
                    ex_price, reason = hit
                    trades.append(_build_simulated_trade(open_pos, ex_price, reason, bar_ts))
                    open_pos = None

            # Re-run model on this bar
            tab_feat = feature_matrix[i]
            seq_feat = feature_matrix[i - seq_len : i]
            current_price = float(features_df["close"].iloc[i]) if has_ohlcv else 1.0

            try:
                pred = await self._ml_predictor.predict(
                    features_tabular=tab_feat,
                    features_sequence=seq_feat,
                    symbol=symbol,
                    current_price=current_price,
                    use_cache=False,
                )
            except Exception as exc:
                logger.debug("Model inference failed bar %s symbol=%s: %s", bar_ts, symbol, exc)
                continue

            direction_label = pred.get("direction_label", "HOLD")
            if direction_label == "HOLD" or open_pos is not None:
                continue

            confidence = float(pred.get("confidence", 0.0))
            entry_px = float(pred.get("entry_price") or current_price)
            sl = pred.get("stop_loss")
            tp1 = pred.get("take_profit_1") or pred.get("take_profit_targets", [None])[0]

            # Causal regime detection: build a candle list from the OHLCV rows
            # available up to and including bar i (i.e., no future data).
            # We use up to 60 bars, matching the production detector's window.
            if has_ohlcv:
                candle_start = max(0, i - 59)
                ohlcv_slice = features_df.iloc[candle_start : i + 1]
                candles = [
                    {
                        "high": float(ohlcv_slice["high"].iloc[k]),
                        "low": float(ohlcv_slice["low"].iloc[k]),
                        "close": float(ohlcv_slice["close"].iloc[k]),
                    }
                    for k in range(len(ohlcv_slice))
                ]
                bar_regime_label = _RegimeService._compute_regime_from_candles(candles)["regime"]
            else:
                bar_regime_label = "insufficient_data"

            # Build MarketContext to run the full 7-gate pipeline
            ctx = MarketContext(
                symbol=symbol,
                action=direction_label,
                confidence_score=confidence,
                confidence_score_pct=confidence * 100,
                regime_type=bar_regime_label,
                time_horizon="intraday",
                rsi_14=None,
                ema_20=None,
                ema_50=None,
                ema_crossover=None,
                ml_direction=direction_label,
                ml_confidence=confidence,
                entry_price=entry_px,
                stop_loss=float(sl) if sl else None,
                take_profit_1=float(tp1) if tp1 else None,
                take_profit_2=None,
                take_profit_3=None,
                ltp=current_price,
                ltp_age_seconds=0.0,
                ltp_stale=False,
            )

            pipeline_result = await self._pipeline.evaluate(
                mock,  # type: ignore[arg-type]
                ctx,
                db=None,
                user_id=0,
                evaluation_time=bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts,
            )

            if not pipeline_result.passed:
                continue

            direction = "LONG" if direction_label == "BUY" else "SHORT"
            open_pos = _OpenPosition(
                symbol=symbol,
                direction=direction,
                entry_price=entry_px,
                quantity=1.0,
                stop_loss=float(sl) if sl else None,
                take_profit_1=float(tp1) if tp1 else None,
                entry_at=bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts,
                regime_label=bar_regime_label,
                regime_detector_version=REGIME_DETECTOR_VERSION,
            )

        # Force-close any open position at period end
        if open_pos and has_ohlcv and len(features_df) > 0:
            last_close = float(features_df["close"].iloc[-1])
            trades.append(_build_simulated_trade(open_pos, last_close, "PERIOD_END", end_dt))

        return trades

    # ── Hybrid boundary detection ─────────────────────────────────────────────

    async def _detect_signal_boundary(
        self,
        symbols: list[str],
        start_dt: datetime,
        end_dt: datetime,
        db: AsyncSession,
    ) -> datetime | None:
        """Return the earliest signal_timestamp across all symbols, or None."""
        stmt = select(func.min(AITradingSignal.signal_timestamp)).where(
            AITradingSignal.symbol.in_(symbols),
            AITradingSignal.signal_timestamp >= start_dt,
            AITradingSignal.signal_timestamp <= end_dt,
        )
        result = await db.execute(stmt)
        boundary = result.scalar_one_or_none()
        return boundary


# Module-level singleton — instantiated without ml_predictor; the runner
# injects it at job-dispatch time via BacktestEngine(ml_predictor=...).
# Applications that never use model_replay can keep the default None.
backtest_engine = BacktestEngine()
