# PATH: backend/app/core/limiter.py
# ───────────────────────────
"""
Cortex AI — Rate Limiter Singleton
====================================
Extracted from main.py so routers can import it without a circular dependency.

Key function resolves the real client IP via X-Forwarded-For / X-Real-IP so
that per-user limits are enforced correctly when all requests arrive through
the Next.js BFF proxy (which would otherwise make every user appear as 127.0.0.1).

Import pattern:
    from app.core.limiter import limiter
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from app.core.config import get_settings

settings = get_settings()


def _get_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip
    return get_remote_address(request)


limiter = Limiter(
    key_func=_get_client_ip,
    storage_uri=str(settings.REDIS_URL),
    default_limits=["100000/hour"],
)
