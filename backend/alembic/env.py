# PATH: backend/alembic/env.py
# ──────────────────────
"""
Cortex AI — Alembic Migrations
================================
Configured for async SQLAlchemy engine (asyncpg driver).
Run: alembic revision --autogenerate -m "description"
     alembic upgrade head

WHY sys.path IS MODIFIED HERE
──────────────────────────────
Alembic executes env.py as a standalone script — it is NOT imported as part
of the normal FastAPI application. This means the `backend/` directory is not
on sys.path, so `from app.core.config import ...` raises ModuleNotFoundError.

We resolve this by inserting the `backend/` directory (the folder that
*contains* the `app/` package) onto sys.path at the very start of the file,
before any app imports. pathlib.Path(__file__) gives us the location of this
env.py file:

  backend/alembic/env.py
         ↑
  __file__.parent       = backend/alembic/
  __file__.parent.parent = backend/          ← this is what we add to sys.path

This is the standard pattern for Alembic with a src-layout project.
"""
from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

# ── Add backend/ to sys.path so `app` is importable ───────────────────────────
# Must happen before any `from app.*` imports below.
sys.path.insert(0, str(Path(__file__).parent.parent))

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.database import Base

# Import all models so Alembic autogenerate can detect table changes.
# Add a new import here whenever you create a new models/*.py file.
import app.models.upstox_data        # noqa: F401
import app.models.user                # noqa: F401
import app.models.trade_suggestions   # noqa: F401
import app.models.watchlist           # noqa: F401
import app.models.paper_trading       # noqa: F401
import app.models.strategies          # noqa: F401
import app.models.ml_feedback         # noqa: F401

config = context.config
settings = get_settings()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL without a live DB connection (for review/CI diff)."""
    context.configure(
        url=str(settings.DATABASE_URL),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Configure context with TimescaleDB index exclusion."""
    
    def include_object(object, name, type_, reflected, compare_to):
        """Exclude TimescaleDB auto-generated indexes from autogenerate."""
        if type_ == "index" and name and "_hyper_" in name:
            return False
        return True
    
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()



async def run_migrations_online() -> None:
    """Run migrations against a live DB using the async engine."""
    engine = create_async_engine(str(settings.DATABASE_URL))
    async with engine.begin() as conn:
        await conn.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())