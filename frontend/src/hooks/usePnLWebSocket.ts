/**
 * usePnLWebSocket — Paper Trading Live P&L Stream
 * =================================================
 * Purpose-built WebSocket hook for the paper trading P&L channel.
 * Receives `LivePnLUpdate` frames from the backend's 500 ms recompute worker
 * and maintains a per-position live P&L map for zero-re-render row updates.
 *
 * Architecture:
 *  - Token passed as ?token= query param (backend WS endpoint requirement).
 *  - LivePnLUpdate frames merged into a ref-backed position map (no re-render per tick).
 *  - Portfolio aggregate stats exposed as stable state — one re-render per frame max.
 *  - Exponential backoff reconnect (1 s → 30 s cap, ±20 % jitter), 10 attempts.
 *  - Auth failures (4001 / 4004) abort reconnect immediately — stale token retries are futile.
 *  - Server heartbeat pings ({type:"ping"}) answered with {type:"pong"} in-band.
 *  - attempt counter reset on every effect re-run so URL/token changes start fresh.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { WS_BASE_URL } from '@/lib/api';
import type { LivePnLUpdate, LivePositionPnL } from '@/types/paper_trading';

// ── Close codes that must not trigger reconnect ───────────────────────────────
const FATAL_CLOSE_CODES = new Set([
  4001, // Invalid or expired token
  4004, // No active portfolio
]);

// ── Reconnect strategy ────────────────────────────────────────────────────────
const MAX_RECONNECT_ATTEMPTS = 10;
const BASE_RECONNECT_MS      = 1_000;
const MAX_RECONNECT_MS       = 30_000;

// ── Client heartbeat (independent of server heartbeat) ───────────────────────
const CLIENT_HEARTBEAT_MS = 25_000;

// ── Public types ──────────────────────────────────────────────────────────────

export type PnLConnectionState = 'connecting' | 'connected' | 'disconnected' | 'error';

export interface PnLPortfolioStats {
  total_unrealized_pnl: number;
  total_realized_pnl:   number;
  current_cash:         number;
  portfolio_value:      number;
  total_return_pct:     number;
  last_updated:         string | null;
}

export interface UsePnLWebSocketReturn {
  connectionState: PnLConnectionState;
  isConnected:     boolean;
  /** Live P&L keyed by position_id — read without triggering re-renders */
  positionPnLMap: React.MutableRefObject<Map<string, LivePositionPnL>>;
  /** Portfolio aggregate stats — one re-render per ~500 ms frame */
  portfolioStats: PnLPortfolioStats;
  /** Manually tear down the connection and suppress automatic reconnect */
  disconnect: () => void;
  /** Reset attempt counter and immediately reconnect (e.g. after token refresh) */
  reconnect: () => void;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const INITIAL_STATS: PnLPortfolioStats = {
  total_unrealized_pnl: 0,
  total_realized_pnl:   0,
  current_cash:         0,
  portfolio_value:      0,
  total_return_pct:     0,
  last_updated:         null,
};

function backoffDelay(attempt: number): number {
  const base   = Math.min(BASE_RECONNECT_MS * 2 ** attempt, MAX_RECONNECT_MS);
  const jitter = base * 0.2 * (Math.random() * 2 - 1);
  return Math.round(base + jitter);
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function usePnLWebSocket(
  portfolioId: string | null | undefined,
  token:       string | null | undefined,
  enabled = true,
): UsePnLWebSocketReturn {
  const [connectionState, setConnectionState] = useState<PnLConnectionState>('disconnected');
  const [portfolioStats,  setPortfolioStats]  = useState<PnLPortfolioStats>(INITIAL_STATS);

  const positionPnLMap = useRef<Map<string, LivePositionPnL>>(new Map());

  const wsRef          = useRef<WebSocket | null>(null);
  const heartbeatRef   = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectRef   = useRef<ReturnType<typeof setTimeout>  | null>(null);
  const attemptsRef    = useRef(0);
  const shouldReconnectRef = useRef(true);
  // Stable ref so connect() inside useEffect always sees fresh values
  const connectRef     = useRef<(() => void) | null>(null);

  const url = useMemo(() => {
    if (!portfolioId || !token) return null;
    return `${WS_BASE_URL}/paper-trading/ws/pnl?portfolio_id=${portfolioId}&token=${encodeURIComponent(token)}`;
  }, [portfolioId, token]);

  const clearTimers = useCallback(() => {
    if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
    if (reconnectRef.current)  { clearTimeout(reconnectRef.current);  reconnectRef.current  = null; }
  }, []);

  useEffect(() => {
    if (!enabled || !url) return;

    // Reset state for this URL / token combination
    attemptsRef.current       = 0;
    shouldReconnectRef.current = true;

    const connect = () => {
      if (wsRef.current?.readyState === WebSocket.OPEN) return;

      setConnectionState('connecting');
      let ws: WebSocket;

      try {
        ws = new WebSocket(url);
      } catch (err) {
        console.error('[PnLWS] Failed to create WebSocket:', err);
        setConnectionState('error');
        return;
      }

      wsRef.current = ws;

      ws.onopen = () => {
        setConnectionState('connected');
        attemptsRef.current = 0;

        // Periodic client→server ping so the backend knows the client is alive
        heartbeatRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping' }));
          }
        }, CLIENT_HEARTBEAT_MS);
      };

      ws.onmessage = (event: MessageEvent) => {
        let frame: Record<string, unknown>;
        try {
          frame = JSON.parse(event.data as string) as Record<string, unknown>;
        } catch {
          return;
        }

        // Server heartbeat — respond with pong, nothing else to do
        if (frame.type === 'ping') {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'pong' }));
          }
          return;
        }

        // Ignore our own pong echoes or unknown frame types
        if (frame.type === 'pong' || !Array.isArray(frame.positions)) return;

        const update = frame as unknown as LivePnLUpdate;

        // Update per-position map — no re-render
        update.positions.forEach((pos) => {
          positionPnLMap.current.set(pos.position_id, pos);
        });

        // Portfolio aggregate — one re-render per frame
        setPortfolioStats({
          total_unrealized_pnl: update.total_unrealized_pnl,
          total_realized_pnl:   update.total_realized_pnl,
          current_cash:         update.current_cash,
          portfolio_value:      update.portfolio_value,
          total_return_pct:     update.total_return_pct,
          last_updated:         update.ts,
        });
      };

      ws.onerror = () => {
        // onclose fires right after; state set there
        clearTimers();
      };

      ws.onclose = (event) => {
        wsRef.current = null;
        clearTimers();

        // Auth / config errors — retrying with the same token is pointless
        if (FATAL_CLOSE_CODES.has(event.code)) {
          console.warn(`[PnLWS] Fatal close ${event.code}: ${event.reason} — reconnect suppressed`);
          setConnectionState('error');
          return;
        }

        // Clean disconnect or unmount
        if (event.code === 1000 || !shouldReconnectRef.current) {
          setConnectionState('disconnected');
          return;
        }

        if (attemptsRef.current >= MAX_RECONNECT_ATTEMPTS) {
          console.error('[PnLWS] Max reconnection attempts reached');
          setConnectionState('error');
          return;
        }

        attemptsRef.current += 1;
        const delay = backoffDelay(attemptsRef.current);
        console.log(`[PnLWS] Reconnecting in ${delay} ms (attempt ${attemptsRef.current}/${MAX_RECONNECT_ATTEMPTS})`);
        setConnectionState('disconnected');
        reconnectRef.current = setTimeout(connect, delay);
      };
    };

    connectRef.current = connect;
    connect();

    return () => {
      shouldReconnectRef.current = false;
      clearTimers();
      wsRef.current?.close(1000, 'Component unmount');
      wsRef.current = null;
      setConnectionState('disconnected');
    };
  }, [url, enabled, clearTimers]);

  const disconnect = useCallback(() => {
    shouldReconnectRef.current = false;
    clearTimers();
    wsRef.current?.close(1000, 'Client disconnect');
    wsRef.current = null;
    setConnectionState('disconnected');
  }, [clearTimers]);

  const reconnect = useCallback(() => {
    clearTimers();
    wsRef.current?.close(1000, 'Manual reconnect');
    wsRef.current = null;
    attemptsRef.current       = 0;
    shouldReconnectRef.current = true;
    setConnectionState('disconnected');
    // Trigger connect on next tick so state flushes first
    setTimeout(() => connectRef.current?.(), 0);
  }, [clearTimers]);

  return {
    connectionState,
    isConnected: connectionState === 'connected',
    positionPnLMap,
    portfolioStats,
    disconnect,
    reconnect,
  };
}
