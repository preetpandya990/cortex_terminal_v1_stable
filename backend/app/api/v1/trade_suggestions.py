"""
Cortex AI — Trade Suggestions Router
======================================
Multi-agent validated trade suggestions API.
Authenticated, rate-limited, with filtering, pagination, and statistics.
Real-time WebSocket updates via Redis pub/sub.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from uuid import UUID as _UUID

from fastapi import APIRouter, Depends, Query, Request, Response, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.limiter import limiter
from app.core.request_context import get_request_id
from app.core.security import get_current_user_id
from app.exceptions import DataNotFoundError
from app.models.strategies import UserPreferences, UserStrategySubscription
from app.models.trade_suggestions import EventCorrelation, TradeSuggestion, UserSuggestionCompliance
from app.models.upstox_data import InstrumentMaster
from app.schemas.trade_suggestions import (
    ConfidenceLevel,
    CorrelationActivityItem,
    CorrelationActivityResponse,
    CorrelationActivityStatus,
    EventCorrelationResponse,
    SignalDirection,
    StrategyComplianceInfo,
    SuggestionDetailResponse,
    SuggestionStatus,
    SuggestionStatsResponse,
    TradeSuggestionResponse,
    TradeSuggestionsListResponse,
    TriggerType,
)

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(dependencies=[Depends(get_current_user_id)])


async def _load_user_enforcement(
    session: AsyncSession, user_id: int
) -> tuple[str, bool]:
    """
    Return (enforcement_mode, has_active_subscriptions) for the user.

    enforcement_mode defaults to 'soft_filter' when no preferences row exists.
    has_active_subscriptions is True when the user has at least one active
    strategy subscription — this gate prevents hard_gate from suppressing all
    suggestions when the user hasn't set up any strategies yet.
    """
    prefs_stmt = select(UserPreferences.enforcement_mode).where(
        UserPreferences.user_id == user_id
    )
    prefs_result = await session.execute(prefs_stmt)
    enforcement_mode: str = prefs_result.scalar_one_or_none() or "soft_filter"

    subs_stmt = select(func.count()).select_from(UserStrategySubscription).where(
        UserStrategySubscription.user_id == user_id,
        UserStrategySubscription.is_active.is_(True),
    )
    subs_result = await session.execute(subs_stmt)
    has_subscriptions: bool = (subs_result.scalar_one() or 0) > 0

    return enforcement_mode, has_subscriptions


async def _load_compliance_map(
    session: AsyncSession,
    user_id: int,
    suggestion_ids: list,
) -> dict[str, "UserSuggestionCompliance"]:
    """
    Batch-load compliance rows for the given suggestion IDs and user.

    Returns a dict keyed by suggestion_id (str).  For each suggestion there
    may be multiple compliance rows (one per subscribed strategy); we surface
    the highest-priority one by taking the row with the lowest evaluated_at
    (earliest evaluation, correlating to highest priority).

    Optimised: single query, no per-suggestion round-trips.
    """
    if not suggestion_ids:
        return {}

    stmt = (
        select(UserSuggestionCompliance)
        .where(
            UserSuggestionCompliance.user_id == user_id,
            UserSuggestionCompliance.suggestion_id.in_(suggestion_ids),
        )
        .order_by(
            UserSuggestionCompliance.suggestion_id,
            UserSuggestionCompliance.evaluated_at,
        )
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    # Keep only the first row per suggestion (highest-priority strategy evaluated)
    compliance_map: dict[str, UserSuggestionCompliance] = {}
    for row in rows:
        key = str(row.suggestion_id)
        if key not in compliance_map:
            compliance_map[key] = row
    return compliance_map


async def _load_instrument_meta_map(
    session: AsyncSession,
    instrument_keys: list[str],
) -> dict[str, tuple[str | None, str | None]]:
    """
    Batch-fetch (name, instrument_type) from instrument_master for the given keys.

    Intentionally NOT filtered on is_active — a suggestion referencing a
    since-delisted instrument must still render its company name and series code.

    Returns: dict mapping instrument_key → (name, instrument_type).
    Both values are None when absent from the source row.
    """
    if not instrument_keys:
        return {}
    stmt = select(
        InstrumentMaster.instrument_key,
        InstrumentMaster.name,
        InstrumentMaster.instrument_type,
    ).where(InstrumentMaster.instrument_key.in_(instrument_keys))
    result = await session.execute(stmt)
    return {
        row.instrument_key: (row.name or None, row.instrument_type)
        for row in result
    }


def _build_suggestion_response(
    suggestion: TradeSuggestion,
    compliance: "UserSuggestionCompliance | None",
    *,
    company_name: str | None = None,
    instrument_type: str | None = None,
) -> TradeSuggestionResponse:
    """
    Build a TradeSuggestionResponse with optional compliance and instrument metadata.

    Company name resolution priority:
      1. ``suggestion.company_name`` — populated at creation time by the
         correlation engine via SymbolValidatorService (fast, always correct).
      2. ``company_name`` kwarg — live lookup fallback for historical rows that
         predate the denormalized column (migration 0025 backfills most of them).

    ``instrument_type`` is fetched live from instrument_master (reflects current
    NSE series, e.g. BZ after a regulatory action) and drives ``requires_risk_disclaimer``.
    """
    resp = TradeSuggestionResponse.model_validate(suggestion)

    # Use the denormalized column; fall back to the live-lookup arg for rows
    # that still have a NULL company_name after the backfill migration.
    if resp.company_name is None and company_name is not None:
        resp.company_name = company_name

    # Instrument series — reflects the *current* NSE classification of this
    # instrument (not what it was at signal generation time), because the
    # risk disclaimer should track the instrument's live regulatory status.
    resp.instrument_series = instrument_type

    if compliance is not None:
        pr = compliance.pipeline_result or {}
        resp.strategy_compliance = StrategyComplianceInfo(
            strategy_id=compliance.strategy_id,
            strategy_name=pr.get("strategy_name", "Unknown Strategy"),
            passed=compliance.passed,
            blocked_at=pr.get("blocked_at"),
            pipeline_result=pr,
        )

    return resp


@router.get("", response_model=TradeSuggestionsListResponse)
@limiter.limit("30/minute")
async def list_suggestions(
    request: Request,
    response: Response,
    user_id: str = Depends(get_current_user_id),
    status: SuggestionStatus | None = Query(None, description="Filter by status"),
    direction: SignalDirection | None = Query(None, description="Filter by signal direction"),
    confidence_level: ConfidenceLevel | None = Query(None, description="Filter by confidence level"),
    min_confidence: float | None = Query(None, ge=0.0, le=100.0, description="Minimum consensus score"),
    symbol: str | None = Query(None, max_length=50, description="Filter by symbol"),
    # Cursor pagination (preferred)
    cursor: str | None = Query(None, description="Cursor for pagination (opaque token)"),
    limit: int = Query(50, ge=1, le=100, description="Results per page (cursor mode)"),
    # Offset pagination (backward compatibility)
    page: int | None = Query(None, ge=1, description="Page number (offset mode, deprecated)"),
    page_size: int | None = Query(None, ge=1, le=100, description="Results per page (offset mode, deprecated)"),
    session: AsyncSession = Depends(get_db),
) -> TradeSuggestionsListResponse:
    """
    List trade suggestions with filtering, pagination, and per-user strategy compliance.

    Returns active suggestions by default, ordered by consensus score (descending).

    **Strategy compliance:**
    - `hard_gate` users with active subscriptions only see suggestions that
      passed their strategy pipeline (`strategy_compliance.passed = True`).
    - `soft_filter` users (default) see all suggestions; each suggestion card
      carries a `strategy_compliance` badge indicating whether their strategy
      matched, without suppressing non-matching suggestions.
    - Users with no active subscriptions see all suggestions with no badge.

    **Pagination Modes:**

    1. **Cursor-based (Recommended)**: Use `cursor` and `limit` parameters
       - Consistent performance regardless of page depth (O(1))
       - Stable pagination under concurrent writes
       - Example: `?limit=50&cursor=abc123`

    2. **Offset-based (Deprecated)**: Use `page` and `page_size` parameters
       - Maintained for backward compatibility
       - Example: `?page=2&page_size=50`

    **Response Headers:**
    - `Link`: RFC 5988 pagination links (cursor mode only)
    """
    from app.core.pagination import (
        apply_cursor_pagination,
        generate_link_header,
        get_estimated_count,
    )

    uid = int(user_id)

    # ── Load user's enforcement mode and subscription state ───────────────────
    enforcement_mode, has_subscriptions = await _load_user_enforcement(session, uid)
    is_hard_gate = enforcement_mode == "hard_gate" and has_subscriptions

    # ── Build base query with user-supplied filters ───────────────────────────
    stmt = select(TradeSuggestion)

    if status:
        stmt = stmt.where(TradeSuggestion.status == status.value)
    else:
        stmt = stmt.where(TradeSuggestion.status == "active")

    if direction:
        stmt = stmt.where(TradeSuggestion.signal_direction == direction.value)
    if confidence_level:
        stmt = stmt.where(TradeSuggestion.confidence_level == confidence_level.value)
    if min_confidence is not None:
        stmt = stmt.where(TradeSuggestion.consensus_score >= min_confidence)
    if symbol:
        stmt = stmt.where(TradeSuggestion.symbol == symbol.upper())

    # ── hard_gate: restrict to strategy-approved suggestions via subquery ─────
    # Uses idx_usc_user_passed partial index — only pages over passing rows.
    if is_hard_gate:
        passed_ids_sq = (
            select(UserSuggestionCompliance.suggestion_id)
            .where(
                UserSuggestionCompliance.user_id == uid,
                UserSuggestionCompliance.passed.is_(True),
            )
            .scalar_subquery()
        )
        stmt = stmt.where(TradeSuggestion.suggestion_id.in_(passed_ids_sq))

    # ── Determine pagination mode ─────────────────────────────────────────────
    is_cursor_mode = cursor is not None or (page is None and page_size is None)

    if is_cursor_mode:
        # ── Cursor-based pagination (O(1) performance) ────────────────────────
        suggestions, next_cursor, has_more = await apply_cursor_pagination(
            session=session,
            base_query=stmt,
            score_column=TradeSuggestion.consensus_score,
            timestamp_column=TradeSuggestion.created_at,
            id_column=TradeSuggestion.id,
            cursor=cursor,
            limit=limit,
            direction="desc",
        )

        estimated_total = await get_estimated_count(
            session=session,
            table_name="trade_suggestions",
            where_clause=f"status = '{status.value if status else 'active'}'",
            threshold=10000,
        )

        link_header = generate_link_header(
            request=request,
            next_cursor=next_cursor,
            prev_cursor=None,
        )
        if link_header:
            response.headers["Link"] = link_header

        # ── Compliance annotations + instrument metadata ──────────────────────
        compliance_map = await _load_compliance_map(
            session, uid, [s.suggestion_id for s in suggestions]
        )
        instrument_meta_map = await _load_instrument_meta_map(
            session, [s.instrument_key for s in suggestions]
        )

        logger.info(
            "Listed trade suggestions (cursor mode)",
            extra={
                "request_id": get_request_id(),
                "user_id": user_id,
                "count": len(suggestions),
                "estimated_total": estimated_total,
                "limit": limit,
                "has_more": has_more,
                "enforcement_mode": enforcement_mode,
                "has_subscriptions": has_subscriptions,
                "cursor": cursor[:20] + "..." if cursor and len(cursor) > 20 else cursor,
                "status": status.value if status else None,
                "direction": direction.value if direction else None,
                "confidence_level": confidence_level.value if confidence_level else None,
                "min_confidence": min_confidence,
                "symbol": symbol,
            },
        )

        return TradeSuggestionsListResponse(
            suggestions=[
                _build_suggestion_response(
                    s,
                    compliance_map.get(str(s.suggestion_id)),
                    company_name=instrument_meta_map.get(s.instrument_key, (None, None))[0],
                    instrument_type=instrument_meta_map.get(s.instrument_key, (None, None))[1],
                )
                for s in suggestions
            ],
            next_cursor=next_cursor,
            has_more=has_more,
            limit=limit,
            estimated_total=estimated_total,
            total=None,
            page=None,
            page_size=None,
        )

    else:
        # ── Offset-based pagination (backward compatibility) ──────────────────
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total_result = await session.execute(count_stmt)
        total = total_result.scalar_one()

        effective_page = page or 1
        effective_page_size = page_size or 50
        offset = (effective_page - 1) * effective_page_size

        stmt = (
            stmt
            .order_by(
                TradeSuggestion.consensus_score.desc(),
                TradeSuggestion.created_at.desc(),
            )
            .offset(offset)
            .limit(effective_page_size)
        )

        result = await session.execute(stmt)
        suggestions = result.scalars().all()

        # ── Compliance annotations + instrument metadata ──────────────────────
        compliance_map = await _load_compliance_map(
            session, uid, [s.suggestion_id for s in suggestions]
        )
        instrument_meta_map = await _load_instrument_meta_map(
            session, [s.instrument_key for s in suggestions]
        )

        logger.info(
            "Listed trade suggestions (offset mode - deprecated)",
            extra={
                "request_id": get_request_id(),
                "user_id": user_id,
                "count": len(suggestions),
                "total": total,
                "page": effective_page,
                "page_size": effective_page_size,
                "enforcement_mode": enforcement_mode,
                "has_subscriptions": has_subscriptions,
                "status": status.value if status else None,
                "direction": direction.value if direction else None,
                "confidence_level": confidence_level.value if confidence_level else None,
                "min_confidence": min_confidence,
                "symbol": symbol,
            },
        )

        return TradeSuggestionsListResponse(
            suggestions=[
                _build_suggestion_response(
                    s,
                    compliance_map.get(str(s.suggestion_id)),
                    company_name=instrument_meta_map.get(s.instrument_key, (None, None))[0],
                    instrument_type=instrument_meta_map.get(s.instrument_key, (None, None))[1],
                )
                for s in suggestions
            ],
            total=total,
            page=effective_page,
            page_size=effective_page_size,
            has_more=(offset + len(suggestions)) < total,
            next_cursor=None,
            limit=None,
            estimated_total=None,
        )


async def _fetch_inflight_correlations(redis_client) -> list[CorrelationActivityItem]:
    """
    Read in-flight correlation checkpoints from Redis.

    The engine writes a sorted-set entry + per-key JSON blob at correlation start
    and removes both on resolution (or they expire after 60s).  This gives the
    seed endpoint a live view of what the pipeline is currently processing.
    Returns [] on any Redis error so callers degrade gracefully.
    """
    try:
        ids = await redis_client.zrangebyscore("cortex:correlations:inflight", "-inf", "+inf")
        if not ids:
            return []
        keys = [f"cortex:correlation:inflight:{cid}" for cid in ids]
        payloads = await redis_client.mget(*keys)
        result: list[CorrelationActivityItem] = []
        for raw in payloads:
            if raw is None:
                continue
            d = json.loads(raw)
            try:
                trigger_type = TriggerType(d["trigger_type"])
            except (ValueError, KeyError):
                logger.debug(
                    "trade_suggestions: unknown trigger_type %r in inflight correlation — skipping",
                    d.get("trigger_type"),
                )
                continue
            result.append(CorrelationActivityItem(
                correlation_id = d["correlation_id"],
                symbol         = d["symbol"],
                trading_symbol = d.get("trading_symbol"),
                trigger_type   = trigger_type,
                status         = CorrelationActivityStatus.PROCESSING,
                started_at     = datetime.fromisoformat(d["started_at"]),
                resolved_at    = None,
            ))
        return result
    except Exception as exc:
        logger.debug("Non-fatal: failed to fetch inflight correlations: %s", exc)
        return []


@router.get("/correlations/recent", response_model=CorrelationActivityResponse)
@limiter.limit("60/minute")
async def get_recent_correlation_activity(
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user_id: str = Depends(get_current_user_id),
) -> CorrelationActivityResponse:
    """
    Return recent correlation attempts for the ML Activity feed.

    Merges three sources:
      1. Completed correlations from DB (enriched with suggestion data)
      2. Rejected correlations from DB (symbol from scanner_output)
      3. In-flight correlations from Redis (processing, not yet in DB)
    DB rows win over Redis for the same correlation_id (de-dup by seen set).
    """
    from sqlalchemy.orm import selectinload
    from app.core.redis import get_redis

    # ── Two targeted DB queries ───────────────────────────────────────────────

    completed_stmt = (
        select(EventCorrelation)
        .options(selectinload(EventCorrelation.suggestion))
        .where(EventCorrelation.consensus_reached.is_(True))
        .order_by(EventCorrelation.trigger_timestamp.desc())
        .limit(limit)
    )

    rejected_stmt = (
        select(EventCorrelation)
        .options(selectinload(EventCorrelation.suggestion))
        .where(
            EventCorrelation.consensus_reached.is_(False),
            EventCorrelation.scanner_output.isnot(None),
        )
        .order_by(EventCorrelation.trigger_timestamp.desc())
        .limit(limit)
    )

    # asyncpg does not support concurrent operations on a single connection,
    # so the two DB queries must run sequentially.  The Redis fetch is
    # independent and overlaps with the second DB query.
    completed_rows = await db.execute(completed_stmt)
    rejected_rows, inflight_items = await asyncio.gather(
        db.execute(rejected_stmt),
        _fetch_inflight_correlations(get_redis()),
    )
    correlations = [
        *completed_rows.scalars().all(),
        *rejected_rows.scalars().all(),
    ]

    def _build_item(corr: EventCorrelation) -> CorrelationActivityItem | None:
        suggestion  = getattr(corr, "suggestion", None)
        scanner_out = corr.scanner_output or {}

        if suggestion is not None:
            symbol      = suggestion.symbol
            trading_sym = suggestion.trading_symbol or suggestion.symbol
            try:
                direction = SignalDirection(suggestion.signal_direction)
            except ValueError:
                direction = None
            score = float(suggestion.consensus_score)
            try:
                confidence = ConfidenceLevel(suggestion.confidence_level)
            except ValueError:
                confidence = None
        else:
            symbol = (
                scanner_out.get("instrument_key")
                or scanner_out.get("trading_symbol")
            )
            if not symbol:
                return None
            trading_sym = scanner_out.get("trading_symbol") or symbol
            direction   = None
            score       = None
            confidence  = None

        try:
            trigger_type = TriggerType(corr.trigger_type)
        except ValueError:
            logger.warning(
                "trade_suggestions: unknown trigger_type %r on correlation %s — skipping item",
                corr.trigger_type, corr.correlation_id,
            )
            return None

        raw_reason = corr.rejection_reason
        rejection_reason: str | None = None
        if raw_reason:
            rejection_reason = raw_reason.split(":")[0].split(" ")[0].strip()

        return CorrelationActivityItem(
            correlation_id   = corr.correlation_id,
            symbol           = symbol,
            trading_symbol   = trading_sym,
            trigger_type     = trigger_type,
            status           = (
                CorrelationActivityStatus.COMPLETED
                if corr.consensus_reached
                else CorrelationActivityStatus.REJECTED
            ),
            started_at       = corr.trigger_timestamp,
            resolved_at      = corr.created_at,
            direction        = direction,
            consensus_score  = score,
            confidence_level = confidence,
            rejection_reason = rejection_reason,
        )

    # DB rows first so the seen-set de-dup prefers resolved state over in-flight.
    seen: set[str] = set()
    items: list[CorrelationActivityItem] = []
    for corr in sorted(correlations, key=lambda c: c.trigger_timestamp, reverse=True):
        cid = str(corr.correlation_id)
        if cid in seen:
            continue
        seen.add(cid)
        item = _build_item(corr)
        if item is not None:
            items.append(item)

    # Append in-flight items — skip any already resolved (seen by DB queries above).
    for inflight in inflight_items:
        cid = str(inflight.correlation_id)
        if cid not in seen:
            seen.add(cid)
            items.append(inflight)

    # Sort combined list newest-first and enforce limit.
    items.sort(key=lambda i: i.started_at, reverse=True)
    items = items[:limit]

    return CorrelationActivityResponse(items=items, total=len(items))


@router.get("/{suggestion_id}", response_model=SuggestionDetailResponse)
@limiter.limit("60/minute")
async def get_suggestion(
    request: Request,
    response: Response,
    suggestion_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
) -> SuggestionDetailResponse:
    """
    Get detailed trade suggestion with correlation history.
    
    Returns the suggestion along with all related event correlations
    for audit trail and debugging.
    """
    # Fetch suggestion
    stmt = select(TradeSuggestion).where(TradeSuggestion.suggestion_id == suggestion_id)
    result = await session.execute(stmt)
    suggestion = result.scalar_one_or_none()
    
    if not suggestion:
        logger.warning(
            "Suggestion not found",
            extra={
                "request_id": get_request_id(),
                "user_id": user_id,
                "suggestion_id": str(suggestion_id),
                "status": "not_found",
            }
        )
        raise DataNotFoundError(f"Trade suggestion {suggestion_id} not found")
    
    # Fetch correlations
    corr_stmt = (
        select(EventCorrelation)
        .where(EventCorrelation.suggestion_id == suggestion_id)
        .order_by(EventCorrelation.created_at.desc())
    )
    corr_result = await session.execute(corr_stmt)
    correlations = corr_result.scalars().all()
    
    logger.info(
        "Retrieved trade suggestion",
        extra={
            "request_id": get_request_id(),
            "user_id": user_id,
            "suggestion_id": str(suggestion_id),
            "symbol": suggestion.symbol,
            "direction": suggestion.signal_direction,
            "consensus_score": float(suggestion.consensus_score),
            "correlation_count": len(correlations),
            "status": "success",
        }
    )
    
    instrument_meta_map = await _load_instrument_meta_map(session, [suggestion.instrument_key])
    name, itype = instrument_meta_map.get(suggestion.instrument_key, (None, None))
    suggestion_resp = _build_suggestion_response(
        suggestion,
        compliance=None,
        company_name=name,
        instrument_type=itype,
    )
    return SuggestionDetailResponse(
        suggestion=suggestion_resp,
        correlations=[EventCorrelationResponse.model_validate(c) for c in correlations],
        correlation_count=len(correlations),
    )


@router.post("/{suggestion_id}/dismiss", response_model=TradeSuggestionResponse)
@limiter.limit("60/minute")
async def dismiss_suggestion(
    request: Request,
    suggestion_id: UUID,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
) -> TradeSuggestionResponse:
    """
    Dismiss a trade suggestion.
    
    Updates the suggestion status to 'invalidated' and sets updated_at timestamp.
    Idempotent - dismissing an already dismissed suggestion returns success.
    """
    # Update suggestion status
    now = datetime.now(timezone.utc)
    stmt = (
        update(TradeSuggestion)
        .where(TradeSuggestion.suggestion_id == suggestion_id)
        .values(status="invalidated", updated_at=now)
        .returning(TradeSuggestion)
    )
    
    result = await session.execute(stmt)
    suggestion = result.scalar_one_or_none()
    
    if not suggestion:
        logger.warning(
            "Cannot dismiss - suggestion not found",
            extra={
                "request_id": get_request_id(),
                "user_id": user_id,
                "suggestion_id": str(suggestion_id),
                "status": "not_found",
            }
        )
        raise DataNotFoundError(f"Trade suggestion {suggestion_id} not found")
    
    await session.commit()
    
    logger.info(
        "Dismissed trade suggestion",
        extra={
            "request_id": get_request_id(),
            "user_id": user_id,
            "suggestion_id": str(suggestion_id),
            "symbol": suggestion.symbol,
            "direction": suggestion.signal_direction,
            "consensus_score": float(suggestion.consensus_score),
            "action": "dismiss",
            "status": "success",
        }
    )
    
    # The dismiss response is a tombstone acknowledged by the client only to
    # confirm the status transition.  The frontend discards it immediately after
    # triggering a cache invalidation, so instrument_series/requires_risk_disclaimer
    # being null/False here is intentional — no instrument_master lookup needed.
    return TradeSuggestionResponse.model_validate(suggestion)


@router.get("/stats/summary", response_model=SuggestionStatsResponse)
@limiter.limit("30/minute")
async def get_stats(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
) -> SuggestionStatsResponse:
    """
    Get aggregate statistics for trade suggestions.
    
    Returns counts, quality metrics, direction distribution, and performance metrics.
    Useful for monitoring dashboards and system health checks.
    """
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Active suggestions count
    active_stmt = select(func.count()).where(TradeSuggestion.status == "active")
    active_result = await session.execute(active_stmt)
    total_active = active_result.scalar_one()
    
    # Today's suggestions count
    today_stmt = select(func.count()).where(TradeSuggestion.created_at >= today_start)
    today_result = await session.execute(today_stmt)
    total_today = today_result.scalar_one()
    
    # Average consensus score (active only)
    avg_score_stmt = select(func.avg(TradeSuggestion.consensus_score)).where(
        TradeSuggestion.status == "active"
    )
    avg_score_result = await session.execute(avg_score_stmt)
    avg_consensus_score = float(avg_score_result.scalar_one() or 0.0)
    
    # High confidence count
    high_conf_stmt = select(func.count()).where(
        TradeSuggestion.status == "active",
        TradeSuggestion.confidence_level == "HIGH",
    )
    high_conf_result = await session.execute(high_conf_stmt)
    high_confidence_count = high_conf_result.scalar_one()
    
    # Direction distribution (active only)
    buy_stmt = select(func.count()).where(
        TradeSuggestion.status == "active",
        TradeSuggestion.signal_direction == "BUY",
    )
    buy_result = await session.execute(buy_stmt)
    buy_count = buy_result.scalar_one()
    
    sell_stmt = select(func.count()).where(
        TradeSuggestion.status == "active",
        TradeSuggestion.signal_direction == "SELL",
    )
    sell_result = await session.execute(sell_stmt)
    sell_count = sell_result.scalar_one()
    
    # Performance metrics from correlations
    avg_latency_stmt = select(func.avg(EventCorrelation.total_latency_ms)).where(
        EventCorrelation.created_at >= today_start
    )
    avg_latency_result = await session.execute(avg_latency_stmt)
    avg_latency_ms = float(avg_latency_result.scalar_one() or 0.0)
    
    # Consensus rate (today)
    total_corr_stmt = select(func.count()).where(EventCorrelation.created_at >= today_start)
    total_corr_result = await session.execute(total_corr_stmt)
    total_correlations = total_corr_result.scalar_one()
    
    success_corr_stmt = select(func.count()).where(
        EventCorrelation.created_at >= today_start,
        EventCorrelation.consensus_reached == True,
    )
    success_corr_result = await session.execute(success_corr_stmt)
    successful_correlations = success_corr_result.scalar_one()
    
    consensus_rate = (
        successful_correlations / total_correlations if total_correlations > 0 else 0.0
    )
    
    logger.info(
        "Stats: active=%d, today=%d, avg_score=%.1f, high_conf=%d, buy=%d, sell=%d, latency=%.1fms, consensus_rate=%.2f",
        total_active,
        total_today,
        avg_consensus_score,
        high_confidence_count,
        buy_count,
        sell_count,
        avg_latency_ms,
        consensus_rate,
    )
    
    return SuggestionStatsResponse(
        total_active=total_active,
        total_today=total_today,
        avg_consensus_score=avg_consensus_score,
        high_confidence_count=high_confidence_count,
        buy_count=buy_count,
        sell_count=sell_count,
        avg_latency_ms=avg_latency_ms,
        consensus_rate=consensus_rate,
    )



# ============================================================================
# WEBSOCKET REAL-TIME UPDATES
# ============================================================================

from app.core.websocket_manager import get_websocket_manager
from app.core.redis import RedisChannels

# Channels this listener subscribes to
_SUGGESTION_CHANNELS = (
    RedisChannels.CORRELATIONS_STARTED,
    RedisChannels.SUGGESTIONS_NEW,
    RedisChannels.SUGGESTIONS_EXPIRED,
    RedisChannels.CORRELATIONS_COMPLETED,
    RedisChannels.CORRELATIONS_REJECTED,
)


async def suggestions_redis_listener() -> None:
    """
    Persistent application-level task that bridges Redis pub/sub to the
    WebSocket manager.  Registered in main.py lifespan — one instance for
    the lifetime of the process, not per-connection.

    Channels → WebSocket rooms mapping:
      cai:suggestions:new        → "suggestions" + "symbol:{symbol}"
      cai:suggestions:expired    → "suggestions" + "symbol:{symbol}"
      cai:correlations:completed → "suggestions"
      cai:correlations:rejected  → "suggestions"

    Message format published by the engine is a JSON-serialized payload
    (not a bare UUID string) so the frontend receives a zero-latency full
    suggestion object for new_suggestion events.
    """
    from app.core.redis import get_redis as _get_redis

    manager = get_websocket_manager()

    while True:
        redis = _get_redis()
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(*_SUGGESTION_CHANNELS)
            logger.info(
                "Suggestions Redis listener subscribed to %d channels",
                len(_SUGGESTION_CHANNELS),
            )

            while True:
                # Non-blocking poll with short timeout so CancelledError propagates
                # promptly and we never hold the event loop for > 100 ms.
                raw = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.1
                )
                if raw is None:
                    await asyncio.sleep(0)  # yield to event loop
                    continue

                channel: str = raw.get("channel", "")
                try:
                    data: dict = json.loads(raw["data"])
                except (json.JSONDecodeError, TypeError, KeyError):
                    logger.warning(
                        "Suggestions listener: malformed payload on channel %s", channel
                    )
                    continue

                suggestion_id = data.get("suggestion_id")
                symbol = data.get("symbol")

                if channel == RedisChannels.CORRELATIONS_STARTED:
                    ws_message = {
                        "type":           "correlation_started",
                        "correlation_id": data.get("correlation_id"),
                        "symbol":         data.get("symbol"),
                        "trading_symbol": data.get("trading_symbol"),
                        "trigger_type":   data.get("trigger_type"),
                        "started_at":     data.get("started_at"),
                    }
                elif channel == RedisChannels.SUGGESTIONS_NEW:
                    ws_message = {
                        "type": "new_suggestion",
                        # Full suggestion payload — no round-trip fetch needed.
                        # correlation_id is included so the ML Activity feed can
                        # resolve the in-flight processing row to completed state.
                        **data,
                    }
                elif channel == RedisChannels.SUGGESTIONS_EXPIRED:
                    ws_message = {
                        "type":          "suggestion_expired",
                        "suggestion_id": suggestion_id,
                        "symbol":        symbol,
                        "expired_at":    data.get("expired_at"),
                        "reason":        data.get("reason"),
                    }
                elif channel == RedisChannels.CORRELATIONS_COMPLETED:
                    ws_message = {
                        "type":            "correlation_completed",
                        "correlation_id":  data.get("correlation_id"),
                        "suggestion_id":   suggestion_id,
                        "symbol":          data.get("symbol"),
                        "trading_symbol":  data.get("trading_symbol"),
                        "trigger_type":    data.get("trigger_type"),
                        "consensus_score": data.get("consensus_score"),
                        "latencies":       data.get("latencies"),
                        "completed_at":    data.get("completed_at"),
                    }
                elif channel == RedisChannels.CORRELATIONS_REJECTED:
                    ws_message = {
                        "type":             "correlation_rejected",
                        "correlation_id":   data.get("correlation_id"),
                        "symbol":           data.get("symbol"),
                        "trading_symbol":   data.get("trading_symbol"),
                        "trigger_type":     data.get("trigger_type"),
                        "rejection_reason": data.get("rejection_reason"),
                        "consensus_score":  data.get("consensus_score"),
                        "rejected_at":      data.get("rejected_at"),
                    }
                else:
                    continue

                t_broadcast_start = datetime.now(timezone.utc)
                await manager.broadcast_to_room("suggestions", ws_message)
                if symbol:
                    await manager.broadcast_to_room(f"symbol:{symbol}", ws_message)
                broadcast_ms = int(
                    (datetime.now(timezone.utc) - t_broadcast_start).total_seconds() * 1000
                )

                if channel == RedisChannels.SUGGESTIONS_NEW:
                    # Compute end-to-end latency from suggestion generation timestamp
                    generated_at_str = data.get("generated_at")
                    e2e_ms: int | None = None
                    if generated_at_str:
                        try:
                            from datetime import datetime as _dt
                            generated_at = _dt.fromisoformat(generated_at_str)
                            if generated_at.tzinfo is None:
                                generated_at = generated_at.replace(tzinfo=timezone.utc)
                            e2e_ms = int(
                                (datetime.now(timezone.utc) - generated_at).total_seconds() * 1000
                            )
                        except Exception:
                            pass

                    logger.info(
                        "Pipeline stage=ws_broadcast suggestion_id=%s symbol=%s "
                        "broadcast_ms=%d e2e_ms=%s",
                        suggestion_id, symbol, broadcast_ms, e2e_ms,
                        extra={
                            "pipeline_stage": "ws_broadcast",
                            "suggestion_id": suggestion_id,
                            "symbol": symbol,
                            "broadcast_ms": broadcast_ms,
                            "e2e_ms": e2e_ms,
                        },
                    )
                else:
                    logger.debug(
                        "Suggestions broadcast: type=%s suggestion=%s symbol=%s broadcast_ms=%d",
                        ws_message["type"], suggestion_id, symbol, broadcast_ms,
                    )

        except asyncio.CancelledError:
            logger.info("Suggestions Redis listener cancelled — shutting down")
            raise
        except Exception as exc:
            logger.error(
                "Suggestions Redis listener error: %s — reconnecting in 5s", exc,
                exc_info=True,
            )
            await asyncio.sleep(5)
        finally:
            try:
                await pubsub.unsubscribe(*_SUGGESTION_CHANNELS)
                await pubsub.aclose()
            except Exception:
                pass


# Separate router for WebSocket (no auth dependency)
ws_router = APIRouter()


@ws_router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(None, description="Optional JWT token for authentication"),
) -> None:
    """
    WebSocket endpoint for real-time trade suggestion updates.
    
    **Protocol:**
    
    Server → Client Messages:
    - `{ type: "new_suggestion", suggestion_id: "...", symbol: "...", ... }`
    - `{ type: "suggestion_expired", suggestion_id: "...", symbol: "...", ... }`
    - `{ type: "correlation_completed", correlation_id: "...", ... }`
    - `{ type: "correlation_rejected", correlation_id: "...", ... }`
    - `{ type: "ping", timestamp: "..." }` (heartbeat every 30s)
    - `{ type: "subscribed", room: "...", success: true }`
    - `{ type: "unsubscribed", room: "...", success: true }`
    - `{ type: "error", message: "...", code: "..." }`
    
    Client → Server Messages:
    - `{ type: "pong" }` (heartbeat response, optional)
    - `{ type: "subscribe", room: "symbol:AAPL" }` (subscribe to symbol updates)
    - `{ type: "unsubscribe", room: "symbol:AAPL" }` (unsubscribe from symbol)
    - `{ type: "ping" }` (client-initiated ping)
    
    **Rooms:**
    - `global` - All connected clients (auto-subscribed)
    - `suggestions` - All suggestion updates (auto-subscribed)
    - `symbol:{symbol}` - Per-symbol updates (subscribe via message)
    - `user:{user_id}` - Per-user updates (auto-subscribed if authenticated)
    
    **Authentication:**
    - Optional JWT token via query parameter: `?token=<jwt>`
    - If provided, validates token and subscribes to user-specific room
    - If not provided, connects as anonymous (limited to global/suggestions rooms)
    
    **Connection:**
    - Auto-reconnect on client side with exponential backoff
    - Heartbeat every 30 seconds (server → client)
    - Idle timeout: 5 minutes (configurable)
    - Max queue size: 100 messages per client (backpressure handling)
    
    **Usage Example:**
    ```javascript
    const ws = new WebSocket('ws://localhost:8000/api/v1/trade-suggestions/ws?token=<jwt>');
    
    ws.onopen = () => {
        // Subscribe to AAPL updates
        ws.send(JSON.stringify({ type: 'subscribe', room: 'symbol:AAPL' }));
    };
    
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        
        if (data.type === 'new_suggestion') {
            // Invalidate React Query cache
            queryClient.invalidateQueries(['trade-suggestions']);
        } else if (data.type === 'ping') {
            // Respond to heartbeat (optional)
            ws.send(JSON.stringify({ type: 'pong' }));
        }
    };
    ```
    
    **Performance:**
    - Broadcast latency: <10ms (P95)
    - Message throughput: 1000+ messages/sec per connection
    - Memory per connection: ~100KB (with full queue)
    - CPU overhead: <1% per 100 connections
    """
    manager = get_websocket_manager()

    # Validate token if provided (token is only present when passed as URL query param;
    # the frontend sends it in-band after connect, so this branch is currently latent)
    user_id = None
    if token:
        try:
            from app.core.security import decode_token
            payload = decode_token(token)
            user_id = payload.sub
            logger.info(f"WebSocket authenticated as user {user_id}")
        except Exception as e:
            logger.warning(f"WebSocket authentication failed: {e}")
            # Accept first so the close frame is valid per RFC 6455
            await websocket.accept()
            await websocket.close(code=1008, reason="Invalid token")
            return

    # Connect client (calls websocket.accept() internally)
    client_id = await manager.connect(websocket, user_id=user_id)

    # Auto-subscribe to suggestions room
    await manager.subscribe_to_room(client_id, "suggestions")

    try:
        # Keep connection alive and handle client messages
        while True:
            data = await websocket.receive_text()
            
            try:
                message = json.loads(data)
                message_type = message.get("type")
                
                # In-band auth — client sends this immediately after onopen.
                # Sets up the per-user room so the client receives user-scoped events.
                if message_type == "auth":
                    token_str = message.get("token", "")
                    try:
                        from app.core.security import decode_token
                        payload = decode_token(token_str)
                        authed_user_id = str(payload.sub)
                        user_room = f"user:{authed_user_id}"
                        await manager.subscribe_to_room(client_id, user_room)
                        conn = manager.connections.get(client_id)
                        if conn:
                            conn.user_id = authed_user_id
                        logger.info("Trade WS client %s authenticated as user %s", client_id, authed_user_id)
                    except Exception as exc:
                        logger.warning("Trade WS in-band auth failed for client %s: %s", client_id, exc)

                # Reauth — client is rotating its JWT; validate and acknowledge.
                # No state change needed here: user rooms are already set up.
                elif message_type == "reauth":
                    token_str = message.get("token", "")
                    try:
                        from app.core.security import decode_token
                        decode_token(token_str)  # validate only
                        logger.debug("Trade WS client %s reauthed", client_id)
                    except Exception as exc:
                        logger.warning("Trade WS reauth failed for client %s: %s", client_id, exc)

                # Handle pong responses
                elif message_type == "pong":
                    # Silently acknowledge pong (no logging to reduce noise)
                    pass

                # Handle ping requests
                elif message_type == "ping":
                    await manager.send_to_client(client_id, {
                        "type": "pong",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                
                # Handle room subscriptions
                elif message_type == "subscribe":
                    room = message.get("room")
                    if room and room.startswith("symbol:"):
                        success = await manager.subscribe_to_room(client_id, room)
                        await manager.send_to_client(client_id, {
                            "type": "subscribed",
                            "room": room,
                            "success": success,
                        })
                    else:
                        await manager.send_to_client(client_id, {
                            "type": "error",
                            "message": "Invalid room name",
                            "code": "INVALID_ROOM",
                        })
                
                # Handle room unsubscriptions
                elif message_type == "unsubscribe":
                    room = message.get("room")
                    if room and room.startswith("symbol:"):
                        success = await manager.unsubscribe_from_room(client_id, room)
                        await manager.send_to_client(client_id, {
                            "type": "unsubscribed",
                            "room": room,
                            "success": success,
                        })
                    else:
                        await manager.send_to_client(client_id, {
                            "type": "error",
                            "message": "Invalid room name",
                            "code": "INVALID_ROOM",
                        })
                
                else:
                    logger.warning(f"Unknown message type from client {client_id}: {message_type}")
            
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON from client {client_id}")
                await manager.send_to_client(client_id, {
                    "type": "error",
                    "message": "Invalid JSON",
                    "code": "INVALID_JSON",
                })
    
    except WebSocketDisconnect:
        await manager.disconnect(client_id, reason="normal")
        logger.info("Client %s disconnected normally", client_id)
    except Exception as e:
        await manager.disconnect(client_id, reason="error")
        logger.error("WebSocket error for client %s: %s", client_id, e, exc_info=True)
