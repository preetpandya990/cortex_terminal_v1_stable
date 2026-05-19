"""
Paper Trading — Position Conversion Service
=============================================
Handles CNC↔MIS product-type conversions on open positions.

Phase 1 constraints:
  - Position must be OPEN.
  - to_product must differ from the current position.product_type.
  - Only CNC ↔ MIS is supported (NRML is out of scope).
  - Full-position conversion only — partial conversions are not yet supported.
  - Same trading day: position.opened_at (IST) must be today's IST date.

Side-effects of a successful conversion (atomic within the caller's session):
  1. position.product_type is updated to to_product.
  2. An immutable PaperPositionConversion audit row is written.

Charge implications:
  Future close operations on this position will use the NEW product_type
  (position.product_type is the authoritative source for close charges after the
  fix in position_service.close_position). Existing fills retain their original
  charge breakdown; only the exit fill will use the converted product_type.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    ConversionNotAllowedError,
    ForbiddenError,
    PositionNotFoundError,
)
from app.models.paper_trading import (
    PaperPosition,
    PaperPositionConversion,
    Portfolio,
)
from app.schemas.paper_trading import ConvertPositionRequest

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

async def convert_position(
    session: AsyncSession,
    position_id: UUID,
    user_id: int,
    payload: ConvertPositionRequest,
) -> PaperPosition:
    """
    Convert an open position's product type between CNC and MIS.

    Parameters
    ----------
    session     : Async SQLAlchemy session (caller commits).
    position_id : UUID of the target PaperPosition.
    user_id     : Authenticated user — used for ownership assertion.
    payload     : ConvertPositionRequest containing the target product type.

    Returns
    -------
    The updated PaperPosition (refreshed from DB).

    Raises
    ------
    PositionNotFoundError      If position does not exist.
    ForbiddenError             If position belongs to a different user.
    ConversionNotAllowedError  If any Phase 1 constraint is violated.
    """
    position, portfolio = await _load_and_authorize(session, position_id, user_id)

    _assert_open(position)
    _assert_same_day(position)
    _assert_different_product(position, payload.to_product.value)

    from_product = position.product_type
    to_product = payload.to_product.value

    # ── Mutate position ───────────────────────────────────────────────────────
    position.product_type = to_product
    position.updated_at = datetime.now(timezone.utc)

    # ── Write immutable audit row ─────────────────────────────────────────────
    audit = PaperPositionConversion(
        position_id=position.id,
        portfolio_id=portfolio.id,
        from_product=from_product,
        to_product=to_product,
        quantity=position.quantity,
    )
    session.add(audit)

    await session.flush()
    await session.refresh(position)

    logger.info(
        "Position converted: position=%s symbol=%s %s→%s qty=%d portfolio=%s user=%d",
        position.id, position.symbol,
        from_product, to_product,
        position.quantity, portfolio.id, user_id,
    )
    return position


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _load_and_authorize(
    session: AsyncSession,
    position_id: UUID,
    user_id: int,
) -> tuple[PaperPosition, Portfolio]:
    """Load position + portfolio and verify ownership in one round-trip."""
    stmt = (
        select(PaperPosition, Portfolio)
        .join(Portfolio, PaperPosition.portfolio_id == Portfolio.id)
        .where(PaperPosition.id == position_id)
    )
    row = (await session.execute(stmt)).one_or_none()

    if row is None:
        raise PositionNotFoundError(f"Position {position_id} not found.")

    position, portfolio = row
    if portfolio.user_id != user_id:
        raise ForbiddenError("You do not have access to this position.")

    return position, portfolio


def _assert_open(position: PaperPosition) -> None:
    if not position.is_open:
        raise ConversionNotAllowedError(
            f"Position {position.id} is already CLOSED. "
            "Only open positions can be converted."
        )


def _assert_same_day(position: PaperPosition) -> None:
    """
    Enforce same-trading-day constraint.

    NSE permits CNC↔MIS conversions only within the same trading session.
    The position must have been opened on today's IST date.
    """
    today_ist = datetime.now(_IST).date()

    opened_at_utc = position.opened_at
    if opened_at_utc.tzinfo is None:
        opened_at_utc = opened_at_utc.replace(tzinfo=timezone.utc)
    opened_ist_date = opened_at_utc.astimezone(_IST).date()

    if opened_ist_date != today_ist:
        raise ConversionNotAllowedError(
            "CNC↔MIS conversion is only permitted on the same trading day. "
            f"This position was opened on {opened_ist_date.isoformat()} (IST)."
        )


def _assert_different_product(position: PaperPosition, to_product: str) -> None:
    if position.product_type == to_product:
        raise ConversionNotAllowedError(
            f"Position is already '{to_product}'. No conversion needed."
        )
