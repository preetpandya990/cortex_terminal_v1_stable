"""
Database error classification for the ML feature pipeline.

Distinguishes *transient connection faults* (the network dropped, the server
closed the socket, the pool handed out a dead connection) from genuine
application errors.  Transient faults are recoverable by retrying the failed
unit of work on a **fresh session** — SQLAlchemy has no mid-transaction
reconnect, so the caller must abandon the poisoned session and retry the
operation itself (see the SQLAlchemy "dealing with disconnects" FAQ).

Lives in its own module because both ``feature_pipeline`` (batch-level retry)
and ``sentiment_features`` (re-raise so the batch retry can act) need it, and
``feature_pipeline`` already imports ``sentiment_features``.

Author: Cortex AI Team
Date: 2026-07-16
"""

import logging

from asyncpg.exceptions import ConnectionDoesNotExistError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def is_transient_connection_error(exc: BaseException) -> bool:
    """
    Return True when ``exc`` indicates a dead/lost database connection that a
    retry on a fresh session can recover from.

    Recognised (at any depth of the ``orig``/``__cause__``/``__context__``
    chain, since SQLAlchemy wraps driver exceptions):
      - asyncpg ``ConnectionDoesNotExistError`` — the server-side connection
        vanished mid-query (the exact failure from the 2026-07-15 forensics)
      - SQLAlchemy ``OperationalError`` / ``InterfaceError`` — the standard
        disconnect wrappers
      - any ``DBAPIError`` with ``connection_invalidated=True`` — SQLAlchemy's
        own verdict that the connection is gone
    """
    seen: set = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionDoesNotExistError):
            return True
        if isinstance(current, (OperationalError, InterfaceError)):
            return True
        if isinstance(current, DBAPIError) and current.connection_invalidated:
            return True
        # SQLAlchemy exposes the driver error as .orig; plain Python chains
        # surface it via __cause__ / __context__.
        current = (
            getattr(current, "orig", None)
            or current.__cause__
            or current.__context__
        )
    return False


async def safe_rollback(session: AsyncSession, context: str) -> None:
    """
    Roll back ``session``, never raising.

    Rollback itself can fail on a dead connection (the very situation that
    made the rollback necessary); that secondary failure must not mask the
    original error or abort the caller's per-symbol loop.
    """
    try:
        await session.rollback()
    except Exception as rb_exc:  # noqa: BLE001 — deliberately broad, see docstring
        logger.error("Rollback failed (%s): %s", context, rb_exc)


async def safe_close(session: AsyncSession, context: str) -> None:
    """Close ``session``, never raising (close can fail on a dead connection)."""
    try:
        await session.close()
    except Exception as close_exc:  # noqa: BLE001 — deliberately broad
        logger.warning("Session close failed (%s): %s", context, close_exc)
