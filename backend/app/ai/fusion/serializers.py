"""
Signal Serialization — shared JSONB normalizers and TradingSignal payload builder.

Centralised here so that both the REST API layer (fusion.py) and the assembler
layer (signal_assembler.py) use the exact same serialization logic without either
module importing the other (which would create a circular dependency).

The three normalizers translate the compact internal JSONB shapes stored in
ai_trading_signals into the typed shapes the frontend TradingSignal interface
expects.  serialise_signal() produces the canonical full payload used by both
GET /fusion/signals and the Redis pub/sub publish in assemble_signal().
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.ai.fusion.models import AITradingSignal


# ── JSONB normalizers ──────────────────────────────────────────────────────────

def normalise_events(raw: Any) -> list[dict]:
    """
    Translate ai_trading_signals.contributing_events JSONB to ContributingEvent[].

    DB shape: [{id, type, impact (0–100 integer), source_url?, source_name?,
                article_title?, summary?}, ...]
    Frontend:  [{event_id, event_type, impact_score (0–1 float), summary,
                 source_url?, source_name?, article_title?}, ...]
    """
    if not raw or not isinstance(raw, list):
        return []
    result = []
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            continue

        source_url = e.get("source_url")
        # Allowlist http/https only — strips javascript:, data:, and other schemes
        # that could be weaponised as XSS vectors via target="_blank" links.
        if source_url and not (
            source_url.startswith("http://") or source_url.startswith("https://")
        ):
            source_url = None

        result.append({
            "event_id": str(e.get("id", i)),
            "event_type": e.get("type", "unknown"),
            # DB stores impact as 0–100; frontend expects 0.0–1.0
            "impact_score": round(float(e.get("impact", 0)) / 100, 4),
            "summary": e.get("summary") or e.get("type") or "—",
            "source_url": source_url or None,
            "source_name": e.get("source_name") or None,
            "article_title": e.get("article_title") or None,
        })
    return result


def normalise_ml_predictions(raw: Any) -> list[dict]:
    """
    Translate ai_trading_signals.ml_predictions JSONB to ContributingMLPrediction[].

    DB shape: {score, confidence, model, prediction: {...}}
    Frontend:  [{model_id, model_name, prediction, confidence}, ...]
    """
    if not raw or not isinstance(raw, dict):
        return []
    confidence = float(raw.get("confidence", 0))
    score = float(raw.get("score", 0))
    model = raw.get("model") or "ensemble"
    if confidence == 0.0 and score == 0.0:
        return []
    return [{
        "model_id": str(model),
        "model_name": str(model).capitalize(),
        "prediction": "bullish" if score > 0.0 else "bearish" if score < 0.0 else "neutral",
        "confidence": confidence,
    }]


def normalise_technical(raw: Any) -> list[dict]:
    """
    Translate ai_trading_signals.technical_indicators JSONB to ContributingTechnical[].

    DB shape (Phase 5): {indicators: {rsi_14, ema_20, ema_50, ema_crossover}}
    Frontend:            [{indicator, value, signal}, ...]

    ema_crossover is a directional label ("bullish"/"bearish"), not a numeric
    indicator value.  It is used as the signal for all numeric indicators and
    excluded from the row list itself — the frontend already knows the direction
    from the signal_type field.
    """
    if not raw or not isinstance(raw, dict):
        return []
    indicators = raw.get("indicators")
    if not indicators or not isinstance(indicators, dict):
        return []

    crossover = indicators.get("ema_crossover", "neutral")
    signal = crossover if crossover in ("bullish", "bearish") else "neutral"

    return [
        {
            "indicator": k,
            "value": float(v),
            "signal": signal,
        }
        for k, v in indicators.items()
        if isinstance(v, (int, float))  # skip string labels like ema_crossover
    ]


# ── Canonical signal payload ───────────────────────────────────────────────────

def serialise_signal(signal: AITradingSignal) -> dict[str, Any]:
    """
    Produce the canonical TradingSignal payload that satisfies the frontend
    TradingSignal TypeScript interface.

    Used in two places:
      1. GET /fusion/signals REST response (via fusion.py)
      2. Redis pub/sub publish after assemble_signal() (via signal_assembler.py)

    Both consumers must receive identical shapes so that useSignalsRealtime can
    inject WebSocket-delivered signals directly into the React Query cache without
    any client-side transformation.
    """
    return {
        "signal_id": str(signal.id),
        "symbol": signal.symbol,
        "signal_type": signal.action.lower(),
        "confidence": float(signal.confidence_score),
        "calibrated_confidence": float(signal.confidence_score),
        "time_horizon": signal.time_horizon or "swing",
        "reasoning": signal.reasoning or "",
        "contributing_factors": {
            "events": normalise_events(signal.contributing_events),
            "ml_predictions": normalise_ml_predictions(signal.ml_predictions),
            "technical": normalise_technical(signal.technical_indicators),
        },
        "regime_type": signal.regime_type,
        "generated_at": signal.signal_timestamp.isoformat(),
        "expires_at": (
            signal.expires_at.isoformat()
            if signal.expires_at
            else signal.signal_timestamp.isoformat()
        ),
        "target_price": float(signal.target_price) if signal.target_price else None,
        "stop_loss": float(signal.stop_loss) if signal.stop_loss else None,
    }
