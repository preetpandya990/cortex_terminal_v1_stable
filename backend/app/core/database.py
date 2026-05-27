"""
Cortex AI — Database Layer
===========================
Async SQLAlchemy with TimescaleDB support. Separate engines for API and worker processes.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncGenerator

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── SQL operation classifier ───────────────────────────────────────────────────
_SQL_OP_RE = re.compile(r"^\s*(\w+)", re.IGNORECASE)
_KNOWN_OPS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "CALL", "BEGIN", "COMMIT", "ROLLBACK"})


def _sql_operation(statement: str) -> str:
    m = _SQL_OP_RE.match(statement)
    op = m.group(1).upper() if m else "OTHER"
    return op if op in _KNOWN_OPS else "OTHER"


# ── SQLAlchemy query timing hooks ──────────────────────────────────────────────
def _attach_query_metrics(async_engine: AsyncEngine) -> None:
    """Attach before/after_cursor_execute listeners to the underlying sync engine.

    SQLAlchemy async engines wrap a synchronous engine internally; the cursor
    events fire on the sync layer, which is the canonical instrumentation point
    for SQLAlchemy 2.x async engines.
    """
    from app.core.metrics import db_query_duration_seconds

    @event.listens_for(async_engine.sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        conn.info["_query_start"] = time.perf_counter()

    @event.listens_for(async_engine.sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        start = conn.info.pop("_query_start", None)
        if start is not None:
            db_query_duration_seconds.labels(
                operation=_sql_operation(statement)
            ).observe(time.perf_counter() - start)


# ── API Engine (main process) ──────────────────────────────────────────────────
engine = create_async_engine(
    str(settings.DATABASE_URL),
    pool_size=20,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
    echo=settings.DEBUG,
    connect_args={
        "server_settings": {
            "timezone": "UTC",
            "statement_timeout": "30000",  # ms
        }
    },
)

# ── Worker Engine (background process) ─────────────────────────────────────────
worker_engine = create_async_engine(
    str(settings.DATABASE_URL),
    pool_size=10,
    max_overflow=5,
    pool_timeout=30,
    pool_recycle=1800,
    pool_pre_ping=True,
    echo=settings.DEBUG,
    connect_args={
        "server_settings": {
            "timezone": "UTC",
            "statement_timeout": "30000",  # ms
        }
    },
)

# Attach Prometheus query-timing hooks to both engines at module load time.
_attach_query_metrics(engine)
_attach_query_metrics(worker_engine)

# ── Session factories ──────────────────────────────────────────────────────────
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

AsyncSessionFactory = AsyncSessionLocal  # backwards-compat alias

WorkerSessionLocal = async_sessionmaker(
    worker_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# ── Declarative base ───────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    """Common declarative base for all ORM models."""


# ── FastAPI dependency ─────────────────────────────────────────────────────────
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield AsyncSession scoped to a single request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ── Database health check ──────────────────────────────────────────────────────
async def check_db_connection() -> bool:
    """Verify database connectivity."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database connection check failed: {e}")
        return False
