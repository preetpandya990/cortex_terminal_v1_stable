"""
Fusion API — Trading Signal Generation and Retrieval

Endpoints:
  POST /signals/generate/{symbol}  — on-demand signal generation
  GET  /signals                    — paginated signal list with full filtering
  GET  /signals/by-id/{id}/audit   — per-signal audit trail
  GET  /signals/{symbol}           — recent signals for a symbol (legacy)
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import and_, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.serializers import serialise_signal
from app.ai.fusion.signal_assembler import SignalAssembler
from app.api.deps import get_db
from app.core.auth import get_current_user, require_role
from app.core.limiter import limiter
from app.core.redis import get_pubsub_client, PubSubClient
from app.services.symbol_validator import symbol_validator

router = APIRouter()


async def _enrich_instrument_keys(signals_data: list[dict], db: AsyncSession) -> None:
    """
    Backfill instrument_key for NSE-eligible signals that were assembled before
    the extra_data denormalization was introduced.  One batch query against
    instrument_master; mutates signals_data in-place.
    """
    from app.models.upstox_data import InstrumentMaster

    symbols_needing_key = {
        sig["symbol"]
        for sig in signals_data
        if sig.get("is_nse_eligible") and not sig.get("instrument_key")
    }
    if not symbols_needing_key:
        return

    rows = (
        await db.execute(
            select(
                InstrumentMaster.trading_symbol,
                InstrumentMaster.instrument_key,
            ).where(
                InstrumentMaster.trading_symbol.in_(symbols_needing_key),
                InstrumentMaster.exchange == "NSE",
                InstrumentMaster.instrument_type == "EQ",
                # Signal-generation path: resolve keys for live instruments only.
                InstrumentMaster.is_active.is_(True),
            )
        )
    ).all()

    key_map = {row.trading_symbol: row.instrument_key for row in rows}

    for sig in signals_data:
        if sig.get("is_nse_eligible") and not sig.get("instrument_key"):
            sig["instrument_key"] = key_map.get(sig["symbol"])


async def _enrich_event_titles(signals_data: list[dict], db: AsyncSession) -> None:
    """
    Backfill article_title and summary for contributing events where the stored
    JSONB has article_title=null (all signals assembled before the rss_fetcher fix).

    Strategy: collect every AIEventClassification.id that is missing a title,
    do one batch JOIN query (classifications → nlp → processed → raw), then
    extract the headline from the first line of raw_content (RSS fetcher always
    stores content as "{title}\n\n{description}").  Mutates signals_data in-place.
    """
    from app.ai.fusion.models import (
        AIEventClassification,
        AINLPResult,
        AIProcessedEvent,
        AIRawEvent,
    )

    # Map classification_id → list of event dicts that need enrichment
    needs: dict[int, list[dict]] = {}
    for sig in signals_data:
        for event in sig.get("contributing_factors", {}).get("events", []):
            if not event.get("article_title"):
                try:
                    cls_id = int(event["event_id"])
                    needs.setdefault(cls_id, []).append(event)
                except (ValueError, KeyError):
                    pass

    if not needs:
        return

    stmt = (
        select(
            AIEventClassification.id.label("cls_id"),
            AIRawEvent.raw_content,
            func.jsonb_extract_path_text(
                AIRawEvent.extra_data, "title"
            ).label("jsonb_title"),
        )
        .outerjoin(AINLPResult, AINLPResult.id == AIEventClassification.nlp_result_id)
        .outerjoin(AIProcessedEvent, AIProcessedEvent.id == AINLPResult.processed_event_id)
        .outerjoin(AIRawEvent, AIRawEvent.id == AIProcessedEvent.raw_event_id)
        .where(AIEventClassification.id.in_(list(needs.keys())))
    )
    rows = (await db.execute(stmt)).all()

    for row in rows:
        raw = row.raw_content or ""
        parts = raw.split("\n\n", 1)
        title = row.jsonb_title or (parts[0].strip() if parts else None)
        summary = parts[1].strip()[:300] if len(parts) > 1 else None

        for event_dict in needs.get(row.cls_id, []):
            if title:
                event_dict["article_title"] = title
            if summary and not event_dict.get("summary"):
                event_dict["summary"] = summary


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/signals/generate/{symbol}")
@limiter.limit("60/minute")
async def generate_signal(
    request: Request,
    symbol: str,
    db: AsyncSession = Depends(get_db),
    pubsub: PubSubClient = Depends(get_pubsub_client),
    current_user=Depends(require_role("trader")),
):
    """On-demand signal generation for a symbol."""
    from app.core.redis import get_redis
    from app.ml.inference.feature_loader import FeatureLoader

    ml_predictor = getattr(request.app.state, "ml_predictor", None)

    feature_loader = (
        FeatureLoader(
            db=db,
            redis=get_redis(),
            sequence_length=ml_predictor.sequence_length,
            n_features=ml_predictor.n_features,
            feature_names=ml_predictor.feature_names,
            feature_version=getattr(ml_predictor, "feature_version", "1.0.0"),
        )
        if ml_predictor
        else None
    )

    assembler = SignalAssembler(
        ensemble_predictor=ml_predictor,
        feature_loader=feature_loader,
    )

    _sym = symbol.upper()

    # Explicit user-facing requests are hard-rejected for ineligible symbols.
    # The internal pipeline (event processor, scheduler) calls assemble_signal()
    # directly and stores Non-NSE signals with is_nse_eligible=False instead.
    if not await symbol_validator.validate_symbol(_sym, db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "symbol_not_eligible",
                "symbol": _sym,
                "message": (
                    f"Symbol '{_sym}' is not eligible: not found in instrument_master "
                    "as an NSE EQ equity. Only NSE-listed equities can be requested on-demand."
                ),
            },
        )

    try:
        signal = await assembler.assemble_signal(db, pubsub, _sym)
        return serialise_signal(signal)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Signal generation failed: {exc}",
        ) from exc


@router.get("/signals")
async def get_all_signals(
    page: int = 1,
    limit: int = 20,
    symbol: Optional[str] = None,
    signal_type: Optional[str] = None,
    time_horizon: Optional[str] = None,
    min_confidence: Optional[float] = None,
    eligible: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Paginated signal list with filtering.

    Default window: active signals + signals that expired within the last 24 hours.
    This matches professional terminal behaviour — same-day context is always visible.

    Filters:
      symbol         — exact NSE trading symbol (case-sensitive)
      signal_type    — buy / sell / hold
      time_horizon   — intraday / swing / positional
      min_confidence — 0.0–1.0 inclusive threshold on calibrated_confidence
      eligible       — true = NSE-listed equities only; false = Non-NSE informational
                       signals; omit = return both (admin / bulk views)
    """
    from app.ai.fusion.models import AITradingSignal

    predicates = [
        or_(
            AITradingSignal.expires_at.is_(None),
            AITradingSignal.expires_at > text("NOW() - INTERVAL '24 hours'"),
        )
    ]

    if symbol:
        predicates.append(AITradingSignal.symbol == symbol)
    if signal_type:
        predicates.append(AITradingSignal.action == signal_type.upper())
    if time_horizon:
        predicates.append(AITradingSignal.time_horizon == time_horizon)
    if min_confidence is not None:
        predicates.append(AITradingSignal.confidence_score >= min_confidence)
    if eligible is not None:
        predicates.append(AITradingSignal.is_nse_eligible == eligible)

    where_clause = and_(*predicates)

    total = await db.scalar(
        select(func.count()).select_from(AITradingSignal).where(where_clause)
    ) or 0

    offset = (page - 1) * limit
    rows = (
        await db.execute(
            select(AITradingSignal)
            .where(where_clause)
            .order_by(desc(AITradingSignal.signal_timestamp))
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()

    signals_data = [serialise_signal(s) for s in rows]
    await _enrich_instrument_keys(signals_data, db)
    await _enrich_event_titles(signals_data, db)
    return {
        "signals": signals_data,
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/signals/by-id/{signal_id}/audit")
async def get_signal_audit(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return the audit trail entry for a specific signal ID."""
    from app.ai.fusion.models import AITradingSignal

    try:
        signal_id_int = int(signal_id)
    except (ValueError, TypeError):
        return {"audit_log": [], "total": 0}

    signal = (
        await db.execute(
            select(AITradingSignal).where(AITradingSignal.id == signal_id_int)
        )
    ).scalar_one_or_none()

    if not signal:
        return {"audit_log": [], "total": 0}

    return {
        "audit_log": [
            {
                "audit_id": signal.id,
                "signal_id": str(signal.id),
                "symbol": signal.symbol,
                "signal_type": signal.action.lower(),
                "confidence": float(signal.confidence_score),
                "generated_at": signal.signal_timestamp.isoformat(),
                "outcome": None,
            }
        ],
        "total": 1,
    }


@router.get("/signals/{symbol}")
async def get_signals(
    symbol: str,
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Recent signals for a symbol (lightweight — no pagination or filters)."""
    from app.ai.fusion.models import AITradingSignal

    rows = (
        await db.execute(
            select(AITradingSignal)
            .where(AITradingSignal.symbol == symbol)
            .order_by(desc(AITradingSignal.signal_timestamp))
            .limit(limit)
        )
    ).scalars().all()

    signals_data = [serialise_signal(s) for s in rows]
    await _enrich_instrument_keys(signals_data, db)
    await _enrich_event_titles(signals_data, db)
    return signals_data
