"""
CAI API — Cortex AI Dashboard and Real-Time Stream

HTTP endpoints:
  GET /dashboard/stats           — aggregate counts and kill-switch state
  GET /dashboard/recent-activity — latest signal activity

WebSocket endpoint (ws_router):
  WS /stream                     — real-time fan-out of all 4 CAI Redis channels

WebSocket protocol (client → server):
  {"type": "auth", "token": "<jwt>"}        — MUST be first frame (10s timeout)
  {"type": "ping"}                           — client-initiated keep-alive

WebSocket protocol (server → client):
  {"type": "connected", "channels": [...]}  — auth success
  {"type": "heartbeat"}                     — every 30 seconds
  {"type": "error", "code": "AUTH_FAILED"}  — bad/missing JWT → close 4001
  {"type": "error", "code": "TOKEN_EXPIRED"}— exp passed → close 4001
  {"channel": "cai:signals:all", "data": {...}}  — live signal payload
  {"channel": "cai:regime:all", "data": {...}}   — regime update
  {"channel": "cai:events:high_impact", "data":{...}} — high-impact event
  {"channel": "cai:models:drift_alerts", "data":{...}} — model drift alert
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIEventClassification, AIKillSwitch, AITradingSignal
from app.api.deps import get_db
from app.core.auth import get_current_user
from app.core.security import TokenPayload, decode_token, CortexInvalidTokenError

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 30          # seconds between heartbeat frames
_AUTH_TIMEOUT = 10                # seconds to wait for the auth frame
_CAI_CHANNELS = [
    "cai:signals:all",
    "cai:regime:all",
    "cai:events:high_impact",
    "cai:models:drift_alerts",
]


# ── Connection Manager ─────────────────────────────────────────────────────────

class CAIConnectionManager:
    """
    Lightweight in-process WebSocket registry for the CAI stream.

    Maintains a dict of client_id → (WebSocket, TokenPayload).
    Token payloads are stored so the heartbeat loop can re-validate `exp`
    without an additional DB or Redis round-trip.

    Thread-safety: single-threaded asyncio event loop — no explicit locking needed.
    """

    def __init__(self) -> None:
        self._clients: dict[str, tuple[WebSocket, TokenPayload]] = {}

    def add(self, client_id: str, ws: WebSocket, token: TokenPayload) -> None:
        self._clients[client_id] = (ws, token)
        logger.info("CAI stream client connected: %s (total=%d)", client_id, len(self._clients))

    def remove(self, client_id: str) -> None:
        self._clients.pop(client_id, None)
        logger.info("CAI stream client disconnected: %s (total=%d)", client_id, len(self._clients))

    @property
    def count(self) -> int:
        return len(self._clients)

    async def broadcast(self, message: str) -> None:
        """Send a raw JSON string to all connected clients."""
        dead: list[str] = []
        for client_id, (ws, _) in list(self._clients.items()):
            try:
                await ws.send_text(message)
            except (WebSocketDisconnect, RuntimeError):
                dead.append(client_id)
        for cid in dead:
            self.remove(cid)

    async def close_expired(self) -> None:
        """
        Iterate all clients and close any whose JWT access token has expired.
        Called on every heartbeat tick — pure datetime comparison, no I/O.
        """
        now = datetime.now(timezone.utc)
        expired: list[tuple[str, WebSocket]] = []
        for client_id, (ws, token) in list(self._clients.items()):
            exp = getattr(token, "exp", None)
            if exp and datetime.fromtimestamp(exp, tz=timezone.utc) <= now:
                expired.append((client_id, ws))

        for client_id, ws in expired:
            logger.info("CAI stream token expired for client %s — closing 4001", client_id)
            self.remove(client_id)
            try:
                await ws.send_text(json.dumps({"type": "error", "code": "TOKEN_EXPIRED"}))
                await ws.close(code=4001)
            except Exception:
                pass


# Module-level singleton — shared by the WS endpoint and the background listener.
cai_manager = CAIConnectionManager()


# ── Background Redis listener (started in lifespan) ───────────────────────────

async def cai_redis_listener() -> None:
    """
    Subscribe to all 4 CAI Redis pub/sub channels and fan out each message to
    every connected WebSocket client.  Also ticks the heartbeat and token
    expiry check every _HEARTBEAT_INTERVAL seconds.

    This coroutine is registered as an asyncio Task in main.py lifespan.
    """
    from app.core.redis import get_redis

    redis = get_redis()
    pubsub = redis.pubsub()
    await pubsub.subscribe(*_CAI_CHANNELS)
    logger.info("CAI Redis listener subscribed to: %s", _CAI_CHANNELS)

    last_heartbeat = asyncio.get_event_loop().time()

    try:
        while True:
            now = asyncio.get_event_loop().time()
            elapsed = now - last_heartbeat

            # Use a short get_message timeout so we never block for > 1s.
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=min(1.0, max(0.01, _HEARTBEAT_INTERVAL - elapsed)),
            )

            if message and message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                    payload = json.dumps({"channel": message["channel"], "data": data})
                    await cai_manager.broadcast(payload)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("CAI listener: malformed message on %s", message.get("channel"))

            if asyncio.get_event_loop().time() - last_heartbeat >= _HEARTBEAT_INTERVAL:
                if cai_manager.count > 0:
                    await cai_manager.broadcast(json.dumps({"type": "heartbeat"}))
                    await cai_manager.close_expired()
                last_heartbeat = asyncio.get_event_loop().time()

    except asyncio.CancelledError:
        logger.info("CAI Redis listener shutting down")
    finally:
        await pubsub.unsubscribe(*_CAI_CHANNELS)
        await pubsub.aclose()


# ── Routers ────────────────────────────────────────────────────────────────────

router = APIRouter()
ws_router = APIRouter()


# ── WebSocket stream ───────────────────────────────────────────────────────────

@ws_router.websocket("/stream")
async def cai_stream(websocket: WebSocket) -> None:
    """
    Real-time CAI event stream.

    Auth is in-band: the first frame must be {"type": "auth", "token": "<jwt>"}.
    The JWT is never embedded in the URL — it never appears in server logs or
    browser history.  Token expiry is re-validated on every heartbeat tick.
    """
    await websocket.accept()

    # ── Step 1: wait for in-band auth frame ───────────────────────────────────
    client_id = str(uuid4())[:8]
    token_payload: Optional[TokenPayload] = None

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=_AUTH_TIMEOUT)
        frame = json.loads(raw)
        if frame.get("type") != "auth" or not frame.get("token"):
            raise ValueError("expected {type: 'auth', token: '...'}")
        token_payload = decode_token(frame["token"], expected_type="access")
    except asyncio.TimeoutError:
        logger.warning("CAI stream auth timeout for client %s", client_id)
        await _close_with_error(websocket, "AUTH_FAILED")
        return
    except (CortexInvalidTokenError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("CAI stream auth failed for client %s: %s", client_id, exc)
        await _close_with_error(websocket, "AUTH_FAILED")
        return

    # ── Step 2: register and confirm ─────────────────────────────────────────
    cai_manager.add(client_id, websocket, token_payload)

    try:
        await websocket.send_text(
            json.dumps({"type": "connected", "channels": _CAI_CHANNELS})
        )
    except Exception:
        cai_manager.remove(client_id)
        return

    # ── Step 3: read loop (client pings / disconnect detection) ──────────────
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                if msg.get("type") == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except json.JSONDecodeError:
                pass  # non-JSON frames are silently ignored
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.debug("CAI stream client %s error: %s", client_id, exc)
    finally:
        cai_manager.remove(client_id)


async def _close_with_error(websocket: WebSocket, code: str) -> None:
    """Send an error frame then close with 4001."""
    try:
        await websocket.send_text(json.dumps({"type": "error", "code": code}))
        await websocket.close(code=4001)
    except Exception:
        pass


# ── HTTP endpoints ─────────────────────────────────────────────────────────────

@router.get("/dashboard/stats")
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Aggregate signal / event counts and kill-switch state."""
    try:
        signal_count = await db.scalar(select(func.count()).select_from(AITradingSignal))
        event_count = await db.scalar(select(func.count()).select_from(AIEventClassification))
        kill_switch = (
            await db.execute(
                select(AIKillSwitch)
                .where(AIKillSwitch.status == "active")
                .order_by(desc(AIKillSwitch.activated_at))
                .limit(1)
            )
        ).scalar_one_or_none()

        return {
            "total_signals": signal_count or 0,
            "total_events": event_count or 0,
            "kill_switch_active": kill_switch is not None,
            "active_ws_clients": cai_manager.count,
        }
    except Exception as exc:
        logger.error("Dashboard stats error: %s", exc)
        return {
            "total_signals": 0,
            "total_events": 0,
            "kill_switch_active": False,
            "active_ws_clients": 0,
        }


@router.get("/dashboard/recent-activity")
async def get_recent_activity(
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Latest signal activity for the dashboard feed."""
    try:
        signals = (
            await db.execute(
                select(AITradingSignal)
                .order_by(desc(AITradingSignal.signal_timestamp))
                .limit(limit)
            )
        ).scalars().all()

        return [
            {
                "type": "signal",
                "symbol": s.symbol,
                "action": s.action,
                "confidence": float(s.confidence_score),
                "timestamp": s.signal_timestamp.isoformat(),
            }
            for s in signals
        ]
    except Exception as exc:
        logger.error("Recent activity error: %s", exc)
        return []
