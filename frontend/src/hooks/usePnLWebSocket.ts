/**
 * usePnLWebSocket — Paper Trading Live P&L Stream
 * =================================================
 * Purpose-built WebSocket hook for the paper trading P&L channel.
 * Receives `LivePnLUpdate` frames from the backend's 500 ms recompute worker
 * and maintains a per-position live P&L map for zero-re-render row updates.
 *
 * Architecture:
 *  - Token passed as ?token= query param (backend WS endpoint requirement).
 *  - LivePnLUpdate frames are merged into a ref-backed position map.
 *  - Portfolio aggregate stats (unrealized P&L, portfolio value, return %) are
 *    exposed as stable state — one re-render per 500 ms frame maximum.
 *  - Exponential backoff reconnect with 30 s cap.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { WS_BASE_URL } from '@/lib/api';
import type { LivePnLUpdate, LivePositionPnL } from '@/types/paper_trading';

export type PnLConnectionState = 'connecting' | 'connected' | 'disconnected' | 'error';

export interface PnLPortfolioStats {
  total_unrealized_pnl: number;
  total_realized_pnl: number;
  current_cash: number;
  portfolio_value: number;
  total_return_pct: number;
  last_updated: string | null;
}

export interface UsePnLWebSocketReturn {
  connectionState: PnLConnectionState;
  isConnected: boolean;
  /** Live P&L keyed by position_id — update without triggering parent re-renders */
  positionPnLMap: React.MutableRefObject<Map<string, LivePositionPnL>>;
  /** Portfolio-level aggregate stats — triggers re-render on each frame */
  portfolioStats: PnLPortfolioStats;
  disconnect: () => void;
}

const INITIAL_STATS: PnLPortfolioStats = {
  total_unrealized_pnl: 0,
  total_realized_pnl: 0,
  current_cash: 0,
  portfolio_value: 0,
  total_return_pct: 0,
  last_updated: null,
};

const MAX_RECONNECT_ATTEMPTS = 10;
const BASE_RECONNECT_MS = 1_000;
const MAX_RECONNECT_MS = 30_000;
const HEARTBEAT_MS = 30_000;

export function usePnLWebSocket(
  portfolioId: string | null | undefined,
  token: string | null | undefined,
  enabled = true
): UsePnLWebSocketReturn {
  const [, forceUpdate] = useReducer((x: number) => x + 1, 0);
  const [connectionState, setConnectionState] = useState<PnLConnectionState>('disconnected');
  const [portfolioStats, setPortfolioStats] = useState<PnLPortfolioStats>(INITIAL_STATS);

  // Ref-backed position map — updated every 500 ms frame without causing re-renders.
  // Parent rows read this directly via ref to paint their own P&L cells.
  const positionPnLMap = useRef<Map<string, LivePositionPnL>>(new Map());

  const wsRef = useRef<WebSocket | null>(null);
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptsRef = useRef(0);
  const shouldReconnectRef = useRef(true);

  const url = useMemo(() => {
    if (!portfolioId || !token) return null;
    return `${WS_BASE_URL}/paper-trading/ws/pnl?portfolio_id=${portfolioId}&token=${encodeURIComponent(token)}`;
  }, [portfolioId, token]);

  const clearTimers = useCallback(() => {
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
    if (reconnectRef.current) {
      clearTimeout(reconnectRef.current);
      reconnectRef.current = null;
    }
  }, []);

  const scheduleReconnect = useCallback((connectFn: () => void) => {
    if (!shouldReconnectRef.current || attemptsRef.current >= MAX_RECONNECT_ATTEMPTS) {
      if (attemptsRef.current >= MAX_RECONNECT_ATTEMPTS) {
        console.error('[PnLWS] Max reconnection attempts reached');
        setConnectionState('error');
      }
      return;
    }

    attemptsRef.current += 1;
    const delay = Math.min(
      BASE_RECONNECT_MS * Math.pow(2, attemptsRef.current),
      MAX_RECONNECT_MS
    );
    const jitter = delay * 0.2 * (Math.random() * 2 - 1);
    const finalDelay = Math.round(delay + jitter);

    console.log(`[PnLWS] Reconnecting in ${finalDelay}ms (attempt ${attemptsRef.current}/${MAX_RECONNECT_ATTEMPTS})`);
    reconnectRef.current = setTimeout(connectFn, finalDelay);
  }, []);

  useEffect(() => {
    if (!enabled || !url) return;

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

        heartbeatRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'ping' }));
          }
        }, HEARTBEAT_MS);
      };

      ws.onmessage = (event: MessageEvent) => {
        try {
          const frame: LivePnLUpdate = JSON.parse(event.data as string);

          // Update per-position map (no re-render)
          frame.positions.forEach((pos) => {
            positionPnLMap.current.set(pos.position_id, pos);
          });

          // Update portfolio aggregate stats (triggers one re-render per frame)
          setPortfolioStats({
            total_unrealized_pnl: frame.total_unrealized_pnl,
            total_realized_pnl: frame.total_realized_pnl,
            current_cash: frame.current_cash,
            portfolio_value: frame.portfolio_value,
            total_return_pct: frame.total_return_pct,
            last_updated: frame.ts,
          });
        } catch (err) {
          // Silently ignore parse errors — the backend may send health pings
        }
      };

      ws.onerror = () => {
        setConnectionState('error');
        clearTimers();
      };

      ws.onclose = (event) => {
        wsRef.current = null;
        clearTimers();

        if (event.code === 1000 || !shouldReconnectRef.current) {
          setConnectionState('disconnected');
          return;
        }

        setConnectionState('disconnected');
        scheduleReconnect(connect);
      };
    };

    connect();

    return () => {
      shouldReconnectRef.current = false;
      clearTimers();
      if (wsRef.current) {
        wsRef.current.close(1000, 'Component unmount');
        wsRef.current = null;
      }
      setConnectionState('disconnected');
    };
  }, [url, enabled, clearTimers, scheduleReconnect]);

  const disconnect = useCallback(() => {
    shouldReconnectRef.current = false;
    clearTimers();
    if (wsRef.current) {
      wsRef.current.close(1000, 'Client disconnect');
      wsRef.current = null;
    }
    setConnectionState('disconnected');
  }, [clearTimers]);

  return {
    connectionState,
    isConnected: connectionState === 'connected',
    positionPnLMap,
    portfolioStats,
    disconnect,
  };
}
