/**
 * usePnLWebSocket — Paper Trading Live P&L Stream
 * =================================================
 * Purpose-built WebSocket hook for the paper trading P&L channel.
 * Receives `LivePnLUpdate` frames from the backend's 500 ms recompute worker
 * and maintains a per-position live P&L map for zero-re-render row updates.
 *
 * Architecture:
 *  - In-band auth: `{ type: "auth", token }` sent as first frame after open.
 *    The token is NEVER placed in the URL (would expose it in server logs and
 *    browser history).
 *  - Token rotation (zero-downtime): when `token` prop changes, a dedicated
 *    effect sends `{ type: "reauth", token }` over the live connection.
 *    The `token` prop is intentionally NOT in the main connection-effect deps —
 *    adding it would tear down and rebuild the stream on every 15-min refresh.
 *  - `connected` state is set only after the server's `{ type: "connected" }`
 *    confirmation frame, not on raw `onopen` — guarantees the auth handshake
 *    completed before the UI treats the stream as live.
 *  - TOKEN_EXPIRED close: if the server sends `TOKEN_EXPIRED` before the 4001
 *    close, the client holds a fresh token in its ref and reconnects immediately
 *    (bypassing exponential backoff).
 *  - LivePnLUpdate frames merged into a ref-backed position map (no re-render per tick).
 *  - Portfolio aggregate stats exposed as stable state — one re-render per frame max.
 *  - Exponential backoff reconnect (1 s → 30 s cap, ±20 % jitter), 10 attempts.
 *  - Auth failures (4001 / 4004) abort reconnect — stale credentials cannot self-heal.
 *  - Server heartbeat pings answered with pongs in-band.
 *  - Attempt counter reset on every effect re-run so portfolioId changes start fresh.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { WS_BASE_URL } from '@/lib/api';
import type { LivePnLUpdate, LivePositionPnL, OrderFilledEvent, OrderExpiredEvent } from '@/types/paper_trading';
import { paperTradingKeys } from './usePaperTrading';

// ── Fatal close codes — do NOT retry ─────────────────────────────────────────
const FATAL_CLOSE_CODES = new Set([
  4001, // Auth failed (bad token, timeout, or wrong user)
  4004, // No active portfolio
]);

// ── Reconnect strategy ────────────────────────────────────────────────────────
// No hard attempt cap — retry indefinitely so a backend restart doesn't leave
// the chip stuck "Offline" forever. The interval ceiling (60 s) means at most
// one connection attempt per minute during a prolonged outage.
const BASE_RECONNECT_MS = 1_000;
const MAX_RECONNECT_MS  = 60_000;

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
  /**
   * True once the first `LivePnLUpdate` frame has been received on the current
   * portfolio connection.  Callers should gate `portfolioStats` display on this
   * flag — until it is true, `portfolioStats` holds the INITIAL_STATS zero
   * sentinel and must not be preferred over REST data.
   *
   * Resets to false whenever `portfolioId` changes (new connection to a
   * different portfolio channel).  Does NOT reset on simple reconnects within
   * the same portfolio so the last-known values stay visible during the brief
   * reconnect window.
   */
  hasReceivedData: boolean;
  /** Live P&L keyed by position_id — read without triggering re-renders */
  positionPnLMap: React.MutableRefObject<Map<string, LivePositionPnL>>;
  /** Portfolio aggregate stats — one re-render per ~500 ms frame */
  portfolioStats: PnLPortfolioStats;
  /** Manually tear down the connection and suppress automatic reconnect */
  disconnect: () => void;
  /** Reset attempt counter and immediately reconnect (e.g. after token refresh) */
  reconnect: () => void;
  /** Attach a callback invoked on every `order_filled` WS event (e.g. for toast notifications) */
  onOrderFilled: React.MutableRefObject<((event: OrderFilledEvent) => void) | null>;
  /** Attach a callback invoked on every `order_expired` WS event */
  onOrderExpired: React.MutableRefObject<((event: OrderExpiredEvent) => void) | null>;
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
  const queryClient = useQueryClient();
  const [connectionState,  setConnectionState]  = useState<PnLConnectionState>('disconnected');
  const [portfolioStats,   setPortfolioStats]   = useState<PnLPortfolioStats>(INITIAL_STATS);
  // Ref holds the live value (no stale-closure risk inside ws.onmessage).
  // State drives the re-render so consumers see the change.
  const hasReceivedDataRef = useRef(false);
  const [hasReceivedData,  setHasReceivedData]  = useState(false);

  const positionPnLMap = useRef<Map<string, LivePositionPnL>>(new Map());

  // Caller-supplied callbacks for order lifecycle events (stable refs, never in deps).
  const onOrderFilled  = useRef<((e: OrderFilledEvent)  => void) | null>(null);
  const onOrderExpired = useRef<((e: OrderExpiredEvent) => void) | null>(null);

  const wsRef             = useRef<WebSocket | null>(null);
  const heartbeatRef      = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectRef      = useRef<ReturnType<typeof setTimeout>  | null>(null);
  const attemptsRef       = useRef(0);
  const shouldReconnectRef   = useRef(true);
  const tokenExpiredRef      = useRef(false); // set by TOKEN_EXPIRED message
  const connectRef           = useRef<(() => void) | null>(null);

  // Token in a ref — synchronously updated each render (before effects fire) so
  // every connect() call uses the latest value.  NEVER added to connection-effect
  // deps — doing so would tear down the stream on every background JWT rotation.
  const tokenRef = useRef(token);
  tokenRef.current = token;

  // URL contains only the non-sensitive endpoint path.  It changes only when
  // portfolioId changes (different portfolio → different Redis channel → reconnect).
  const url = useMemo(() => {
    if (!portfolioId) return null;
    return `${WS_BASE_URL}/paper-trading/ws/pnl`;
  }, [portfolioId]);

  const clearTimers = useCallback(() => {
    if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
    if (reconnectRef.current)  { clearTimeout(reconnectRef.current);  reconnectRef.current  = null; }
  }, []);

  // ── Connection effect ───────────────────────────────────────────────────────
  // NOTE: `token` is intentionally absent from deps — see tokenRef pattern above.
  useEffect(() => {
    if (!enabled || !url) return;

    attemptsRef.current        = 0;
    shouldReconnectRef.current = true;
    // Reset the data-received gate whenever the portfolio connection changes
    // (different portfolioId → different Redis channel → treat as fresh start).
    hasReceivedDataRef.current = false;
    setHasReceivedData(false);

    const connect = () => {
      if (wsRef.current?.readyState === WebSocket.OPEN) return;

      tokenExpiredRef.current = false; // reset on every fresh attempt
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
        // Send auth frame immediately — token never goes in the URL.
        const currentToken = tokenRef.current;
        if (currentToken) {
          ws.send(JSON.stringify({ type: 'auth', token: currentToken }));
        } else {
          console.warn('[PnLWS] No token available for in-band auth');
        }
        // `connected` state and heartbeat are set after server confirms auth below.
      };

      ws.onmessage = (event: MessageEvent) => {
        let frame: Record<string, unknown>;
        try {
          frame = JSON.parse(event.data as string) as Record<string, unknown>;
        } catch {
          return;
        }

        // ── Auth confirmation ─────────────────────────────────────────────────
        if (frame.type === 'connected') {
          setConnectionState('connected');
          attemptsRef.current = 0;
          // Start heartbeat now that the session is authenticated and live.
          heartbeatRef.current = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'ping' }));
            }
          }, CLIENT_HEARTBEAT_MS);
          return;
        }

        // ── Error frames ──────────────────────────────────────────────────────
        if (frame.type === 'error') {
          const code = frame.code as string;
          if (code === 'TOKEN_EXPIRED') {
            // Server is about to close with 4001 — but we have a fresh token.
            // Arm the fast-reconnect flag; onclose will bypass backoff.
            tokenExpiredRef.current = true;
          }
          // AUTH_FAILED / NO_PORTFOLIO / REAUTH_FAILED are handled in onclose.
          return;
        }

        // ── Protocol frames ───────────────────────────────────────────────────
        if (frame.type === 'ping') {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'pong' }));
          }
          return;
        }

        if (frame.type === 'pong' || frame.type === 'reauthed') return;

        // ── Order lifecycle events ────────────────────────────────────────────
        // Fired by the matching engine when a LIMIT/SL/SL-M order is filled or
        // expires.  Invalidate relevant React Query caches so the UI refreshes
        // without requiring a manual page reload.
        if (frame.type === 'order_filled') {
          const filledEvent = frame as unknown as OrderFilledEvent;
          // Invalidate positions (a fill may open/close a position), orders
          // (OPEN → COMPLETE), and portfolio (reserved_cash released).
          queryClient.invalidateQueries({ queryKey: paperTradingKeys.positions() });
          queryClient.invalidateQueries({ queryKey: paperTradingKeys.orders() });
          queryClient.invalidateQueries({ queryKey: paperTradingKeys.portfolio() });
          onOrderFilled.current?.(filledEvent);
          return;
        }

        if (frame.type === 'order_expired') {
          const expiredEvent = frame as unknown as OrderExpiredEvent;
          // Invalidate orders (OPEN → CANCELLED) and portfolio (reserved_cash released).
          queryClient.invalidateQueries({ queryKey: paperTradingKeys.orders() });
          queryClient.invalidateQueries({ queryKey: paperTradingKeys.portfolio() });
          onOrderExpired.current?.(expiredEvent);
          return;
        }

        // ── P&L data frame ────────────────────────────────────────────────────
        if (!Array.isArray(frame.positions)) return;

        const update = frame as unknown as LivePnLUpdate;

        // Merge into ref-backed map — no re-render per individual position
        update.positions.forEach((pos) => {
          positionPnLMap.current.set(pos.position_id, pos);
        });

        // Portfolio aggregate — triggers one re-render per ~500 ms frame
        setPortfolioStats({
          total_unrealized_pnl: update.total_unrealized_pnl,
          total_realized_pnl:   update.total_realized_pnl,
          current_cash:         update.current_cash,
          portfolio_value:      update.portfolio_value,
          total_return_pct:     update.total_return_pct,
          last_updated:         update.ts,
        });

        // Gate: mark that real data has arrived.  The ref is read inside this
        // closure (no stale-capture risk); the state setter triggers the render.
        if (!hasReceivedDataRef.current) {
          hasReceivedDataRef.current = true;
          setHasReceivedData(true);
        }
      };

      ws.onerror = () => {
        clearTimers();
      };

      ws.onclose = (event) => {
        wsRef.current = null;
        clearTimers();

        // TOKEN_EXPIRED + we have a fresh token → immediate reconnect, no backoff.
        if (tokenExpiredRef.current && tokenRef.current && shouldReconnectRef.current) {
          tokenExpiredRef.current = false;
          attemptsRef.current = 0;
          setConnectionState('disconnected');
          reconnectRef.current = setTimeout(connect, 100);
          return;
        }

        // Fatal auth / config errors — retrying with the same credentials is futile.
        if (FATAL_CLOSE_CODES.has(event.code)) {
          console.warn(`[PnLWS] Fatal close ${event.code}: ${event.reason} — reconnect suppressed`);
          setConnectionState('error');
          return;
        }

        // Clean disconnect or explicit unmount.
        if (event.code === 1000 || !shouldReconnectRef.current) {
          setConnectionState('disconnected');
          return;
        }

        attemptsRef.current += 1;
        const delay = backoffDelay(attemptsRef.current);
        console.log(`[PnLWS] Reconnecting in ${delay} ms (attempt ${attemptsRef.current})`);
        setConnectionState('disconnected');
        reconnectRef.current = setTimeout(connect, delay);
      };
    };

    connectRef.current = connect;
    connect();

    // When the user returns to the tab, reset the backoff counter and reconnect
    // immediately if the connection is not already open. This ensures a backend
    // restart that happened while the tab was backgrounded is recovered the
    // instant the user looks at the screen — without waiting for the next
    // scheduled retry (which could be up to 60 s away).
    const handleVisibilityChange = () => {
      if (document.visibilityState !== 'visible') return;
      if (wsRef.current?.readyState === WebSocket.OPEN) return;
      attemptsRef.current = 0;
      clearTimers();
      wsRef.current?.close();
      wsRef.current = null;
      connect();
    };
    document.addEventListener('visibilitychange', handleVisibilityChange);

    return () => {
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      shouldReconnectRef.current = false;
      clearTimers();
      wsRef.current?.close(1000, 'Component unmount');
      wsRef.current = null;
      setConnectionState('disconnected');
    };
  // `token` intentionally absent — handled by the reauth effect below.
  }, [url, enabled, clearTimers]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Token rotation effect ───────────────────────────────────────────────────
  // When the JWT rotates (background refresh), send a reauth frame in-band so
  // the stream continues uninterrupted.  If the connection is not open at that
  // moment, tokenRef is already updated and the next connect() uses the fresh token.
  useEffect(() => {
    if (!token) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'reauth', token }));
    }
  }, [token]);

  // ── Public API ──────────────────────────────────────────────────────────────

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
    attemptsRef.current        = 0;
    shouldReconnectRef.current = true;
    setConnectionState('disconnected');
    // Trigger connect on next tick so state flushes first.
    setTimeout(() => connectRef.current?.(), 0);
  }, [clearTimers]);

  return {
    connectionState,
    isConnected:     connectionState === 'connected',
    hasReceivedData,
    positionPnLMap,
    portfolioStats,
    disconnect,
    reconnect,
    onOrderFilled,
    onOrderExpired,
  };
}
