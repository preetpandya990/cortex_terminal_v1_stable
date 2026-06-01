"""
Pytest configuration and fixtures for AI service tests
"""
import os
import pytest
import asyncio
from pathlib import Path
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession

# Ensure .env is loaded before any app imports
from dotenv import load_dotenv
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# Override DATABASE_URL for tests (use actual database from .env)
# This ensures tests use the correct database even if env var is set
if "DATABASE_URL" in os.environ:
    # Load from .env file explicitly
    with open(env_path) as f:
        for line in f:
            if line.startswith("DATABASE_URL="):
                db_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ["DATABASE_URL"] = db_url
                break

# Database fixtures
@pytest.fixture
async def db_session() -> AsyncGenerator:
    """
    Provide async database session for tests with transaction rollback.
    
    Production-grade approach:
    - Uses actual PostgreSQL database (not SQLite)
    - Each test runs in a transaction that rolls back
    - Uses NullPool to avoid event loop conflicts
    - Ensures test isolation without affecting production data
    """
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.pool import NullPool
    from app.core.config import get_settings
    from app.core.database import Base
    
    settings = get_settings()
    
    # Create test engine with NullPool to avoid event loop issues
    test_engine = create_async_engine(
        str(settings.DATABASE_URL),
        poolclass=NullPool,  # Critical: prevents "different loop" errors
        echo=False,
    )
    
    # Create connection and transaction
    async with test_engine.connect() as conn:
        trans = await conn.begin()
        
        # Create session bound to this transaction
        session = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint")
        
        # Ensure tables exist (idempotent)
        await conn.run_sync(Base.metadata.create_all)
        
        yield session
        
        # Rollback transaction (cleanup)
        await session.close()
        await trans.rollback()
    
    # Dispose engine
    await test_engine.dispose()


@pytest.fixture
async def redis_client():
    """Provide Redis client for tests."""
    from app.core.redis import init_redis, close_redis
    await init_redis()
    yield
    await close_redis()


# Service fixtures
@pytest.fixture
def signal_assembler():
    """Provide SignalAssembler instance."""
    from app.ai.fusion.signal_assembler import SignalAssembler
    return SignalAssembler()


@pytest.fixture
def circuit_breaker():
    """Provide CircuitBreaker instance."""
    from app.ai.fusion.signal_pipeline import CircuitBreaker
    return CircuitBreaker()


@pytest.fixture
def signal_pipeline():
    """Provide SignalPipeline instance."""
    from app.ai.fusion.signal_pipeline import SignalPipeline
    return SignalPipeline()



# HTTP Client fixture for API tests
@pytest.fixture
async def async_client(db_session: AsyncSession) -> AsyncGenerator:
    """
    Provide HTTP test client with auth bypassed and test database.
    
    Production-grade approach:
    - Overrides get_db dependency to use test session
    - Overrides auth to bypass authentication
    - Avoids lifespan context to prevent event loop conflicts
    """
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.api.deps import get_current_user_id
    from app.core.database import get_db

    # Override auth to return a test user ID
    def override_auth():
        return "test-user-id"
    
    # Override database to use test session
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_current_user_id] = override_auth
    app.dependency_overrides[get_db] = override_get_db

    # Create client without lifespan to avoid event loop issues
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    # Clean up overrides
    app.dependency_overrides.clear()


# WebSocket Client fixture for WebSocket tests
@pytest.fixture
def websocket_client(db_session: AsyncSession):
    """
    Provide TestClient for WebSocket testing.
    
    Production-grade approach:
    - Uses Starlette TestClient which supports websocket_connect()
    - Overrides dependencies to use test database
    - Bypasses authentication for testing
    - Must be used as context manager: with client.websocket_connect(...)
    """
    from starlette.testclient import TestClient
    from app.main import app
    from app.api.deps import get_current_user_id
    from app.core.database import get_db

    # Override auth to return a test user ID
    def override_auth():
        return "test-user-id"
    
    # Override database to use test session
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_current_user_id] = override_auth
    app.dependency_overrides[get_db] = override_get_db

    # Create TestClient (supports WebSocket)
    with TestClient(app) as client:
        yield client

    # Clean up overrides
    app.dependency_overrides.clear()
