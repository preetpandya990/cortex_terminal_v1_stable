# PATH: backend/app/core/limiter.py
# ───────────────────────────
"""
Cortex AI — Rate Limiter Singleton
====================================
Extracted from main.py so routers can import it without a circular dependency.

Import pattern:
    from app.core.limiter import limiter   (in routers)
    from app.core.limiter import limiter   (in main, to attach to app.state)
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import get_settings

settings = get_settings()

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=str(settings.REDIS_URL),
    default_limits=["100000/hour"],  # Increased for load testing
)