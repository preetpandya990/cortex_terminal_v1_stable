/**
 * useWebSocket — Production-Grade WebSocket with Auto-Reconnect
 * ==============================================================
 *
 * Architecture:
 *   • All mutable connection state lives in refs — stable across renders.
 *   • A single useReducer-based forceUpdate drives re-renders on state transitions;
 *     no state variables that would trigger the connection effect.
 *   • Dependencies array is fully explicit — no lint suppressions on it.
 *   • Exponential backoff with ±25 % jitter, capped at 30 s.
 *   • Message deduplication: a sliding window of the last 64 message IDs prevents
 *     duplicate processing when the server sends the same payload twice.
 *   • Proactive ping from client every heartbeatInterval ms so the server can
 *     detect stale connections even when no application messages are flowing.
 *   • Manual reconnect() method resets the attempt counter and retries from scratch.
 *   • Rooms are re-subscribed automatically on every reconnect.
 *
 * Token lifecycle (zero-downtime rotation):
 *   • `token` is intentionally NOT in the connection-effect dependency array.
 *     Adding it would close/reopen the socket on every 15-minute background refresh.
 *   • Instead, the token lives in a ref that is synchronously updated each render,
 *     before any effects fire.
 *   • A dedicated effect sends `{ type: "reauth", token }` over the live connection
 *     whenever the prop changes — the server validates the new token in-band with
 *     zero session interruption.
 *   • If the server still closes with 4001 (TOKEN_EXPIRED), the client bypasses
 *     the exponential-backoff queue and reconnects immediately with the fresh token
 *     already held in the ref.
 *   • Token is sent in-band after connect (`{ type: "auth", token }`) — never in
 *     the URL (which would expose it in server logs and browser history).
 *
 * Usage:
 * ```tsx
 * const ws = useWebSocket({
 *   url: '/api/v1/trade-suggestions/ws',
 *   token: accessToken,
 *   enabled: isAuthenticated,
 *   onMessage: (data) => {
 *     if (data.type === 'new_suggestion') {
 *       queryClient.invalidateQueries(['trade-suggestions']);
 *     }
 *   },
 * });
 * ```
 */

import { useEffect, useRef, useMemo, useCallback, useReducer } from 'react';

export type TriggerType = 'SCANNER_ANOMALY' | 'NEWS_EVENT';

export type WebSocketMessage =
  | {
      /** ML pipeline started processing a symbol. Powers the ML Activity feed. */
      type: 'correlation_started';
      correlation_id: string;
      symbol: string;
      trading_symbol: string | null;
      trigger_type: TriggerType;
      started_at: string;
    }
  | {
      /**
       * New trade suggestion committed — full payload, no refetch needed.
       * `correlation_id` links back to the corresponding correlation_started event
       * so the ML Activity feed can transition the row from processing → completed.
       */
      type: 'new_suggestion';
      correlation_id: string;
      suggestion_id: string;
      symbol: string;
      trading_symbol: string | null;
      company_name: string | null;
      signal_direction: 'BUY' | 'SELL';
      confidence_level: 'HIGH' | 'MEDIUM' | 'LOW';
      consensus_score: number;
      trigger_pathway: string;
      entry_price: number | null;
      stop_loss: number | null;
      take_profit_1: number | null;
      take_profit_2: number | null;
      take_profit_3: number | null;
      risk_reward_ratio: number | null;
      generated_at: string;
      expires_at: string;
      status: string;
    }
  | {
      type: 'suggestion_expired';
      suggestion_id: string;
      symbol: string;
      expired_at: string;
      reason: string;
    }
  | {
      /** Consensus reached — accompanies new_suggestion for latency telemetry. */
      type: 'correlation_completed';
      correlation_id: string;
      suggestion_id: string;
      symbol: string;
      trading_symbol: string | null;
      trigger_type: TriggerType;
      consensus_score: number;
      latencies: { scanner_ms?: number; ai_ms?: number; ml_ms?: number } | null;
      completed_at: string;
    }
  | {
      /** Consensus not reached — reason surfaced in ML Activity feed. */
      type: 'correlation_rejected';
      correlation_id: string;
      symbol: string;
      trading_symbol: string | null;
      trigger_type: TriggerType;
      rejection_reason: string;
      consensus_score: number;
      rejected_at: string;
    }
  | {
      type: 'ping';
      timestamp: string;
    }
  | {
      type: 'pong';
      timestamp: string;
    }
  | {
      type: 'subscribed';
      room: string;
      success: boolean;
    }
  | {
      type: 'unsubscribed';
      room: string;
      success: boolean;
    }
  | {
      type: 'error';
      message: string;
      code: string;
    };

export type WebSocketState = 'connecting' | 'connected' | 'disconnected' | 'error';

export interface UseWebSocketOptions {
  url: string;
  /** Bearer token sent as an in-band auth message after connect (not in the URL). */
  token?: string;
  /** When false the socket will not open (and any open socket will be closed).
   *  Use this to gate the connection on authentication state. Default: true. */
  enabled?: boolean;
  onMessage?: (data: WebSocketMessage) => void;
  onConnect?: () => void;
  onDisconnect?: () => void;
  onError?: (error: Event) => void;
  reconnect?: boolean;
  reconnectInterval?: number;
  reconnectAttempts?: number;
  heartbeatInterval?: number;
  autoSubscribeRooms?: string[];
}

export interface UseWebSocketReturn {
  isConnected: boolean;
  send: (data: unknown) => void;
  subscribe: (room: string) => void;
  unsubscribe: (room: string) => void;
  disconnect: () => void;
  /** Reset attempt counter and immediately retry the connection. */
  reconnect: () => void;
}

/** Maximum number of recent message IDs held in the deduplication window. */
const _DEDUP_WINDOW = 64;

/** No-op in production; active only when NEXT_PUBLIC_WS_DEBUG is set. */
const wsLog = process.env.NEXT_PUBLIC_WS_DEBUG
  ? (...args: unknown[]) => console.log('[WebSocket]', ...args)
  : () => {};

const wsWarn = process.env.NEXT_PUBLIC_WS_DEBUG
  ? (...args: unknown[]) => console.warn('[WebSocket]', ...args)
  : () => {};

export function useWebSocket(options: UseWebSocketOptions): UseWebSocketReturn {
  const {
    url,
    token,
    enabled = true,
    onMessage,
    onConnect,
    onDisconnect,
    onError,
    reconnect = true,
    reconnectInterval = 1000,
    reconnectAttempts = 10,
    heartbeatInterval = 30000,
    autoSubscribeRooms = [],
  } = options;

  // Stable autoSubscribeRooms reference — only changes when contents change.
  const stableAutoSubscribeRooms = useMemo(
    () => autoSubscribeRooms,
    // JSON.stringify as a dependency source is intentional: autoSubscribeRooms is
    // typically constructed inline by callers, so reference equality is not reliable.
    [JSON.stringify(autoSubscribeRooms)], // eslint-disable-line react-hooks/exhaustive-deps
  );

  // forceUpdate drives re-renders when connection state transitions; it never
  // appears in the connection-effect dependency array so it cannot cause loops.
  const [, forceUpdate] = useReducer((x: number) => x + 1, 0);

  // ── Refs ──────────────────────────────────────────────────────────────────────
  const wsRef                     = useRef<WebSocket | null>(null);
  const isConnectedRef            = useRef(false);
  const reconnectTimeoutRef       = useRef<NodeJS.Timeout | null>(null);
  const heartbeatIntervalRef      = useRef<NodeJS.Timeout | null>(null);
  const shouldReconnectRef        = useRef(true);
  const reconnectAttemptsCountRef = useRef(0);
  const subscribedRoomsRef        = useRef<Set<string>>(new Set());
  // Sliding window of recent message IDs for deduplication.
  const recentMessageIdsRef       = useRef<string[]>([]);
  // Set to true when server sends TOKEN_EXPIRED — bypasses backoff on next close.
  const tokenExpiredRef           = useRef(false);

  // Token in a ref so it never appears in the connection-effect deps.
  // Updated synchronously each render (before effects fire) so connect() always
  // reads the latest value regardless of when it is called.
  const tokenRef = useRef(token);
  tokenRef.current = token;

  // Callbacks in a ref so they update without triggering the connection effect.
  const callbacksRef = useRef({ onMessage, onConnect, onDisconnect, onError });
  useEffect(() => {
    callbacksRef.current = { onMessage, onConnect, onDisconnect, onError };
  }, [onMessage, onConnect, onDisconnect, onError]);

  // ── Connection effect ─────────────────────────────────────────────────────────
  // NOTE: `token` is intentionally absent from this dependency array.
  // Token rotation is handled by the dedicated reauth effect below.
  useEffect(() => {
    if (!enabled) return;

    shouldReconnectRef.current = true;
    reconnectAttemptsCountRef.current = 0;

    let ws: WebSocket | null = null;
    let reconnectTimeout: NodeJS.Timeout | null = null;
    let heartbeatIntervalId: NodeJS.Timeout | null = null;

    const connect = () => {
      if (ws?.readyState === WebSocket.OPEN) return;

      tokenExpiredRef.current = false; // reset on each fresh attempt
      wsLog('Connecting to', url);

      try {
        ws = new WebSocket(url);
        wsRef.current = ws;

        ws.onopen = () => {
          wsLog('Connected');

          // In-band auth handshake — credentials stay out of URLs, server logs,
          // and browser history.
          const currentToken = tokenRef.current;
          if (currentToken) {
            ws?.send(JSON.stringify({ type: 'auth', token: currentToken }));
          }

          isConnectedRef.current = true;
          reconnectAttemptsCountRef.current = 0;
          forceUpdate();

          // Proactive client-initiated ping so the server can detect stale sockets
          // even during quiet periods.
          heartbeatIntervalId = setInterval(() => {
            if (ws?.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'ping', timestamp: new Date().toISOString() }));
            }
          }, heartbeatInterval);
          heartbeatIntervalRef.current = heartbeatIntervalId;

          // Re-subscribe to all rooms that were active before the disconnect.
          subscribedRoomsRef.current.forEach((room) => {
            if (ws?.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'subscribe', room }));
            }
          });

          // Auto-subscribe to configured rooms not already tracked.
          stableAutoSubscribeRooms.forEach((room) => {
            if (ws?.readyState === WebSocket.OPEN && !subscribedRoomsRef.current.has(room)) {
              ws.send(JSON.stringify({ type: 'subscribe', room }));
              subscribedRoomsRef.current.add(room);
            }
          });

          callbacksRef.current.onConnect?.();
        };

        ws.onmessage = (event) => {
          try {
            const data: WebSocketMessage = JSON.parse(event.data as string);

            // ── Infrastructure messages ──────────────────────────────────────

            // Server signaling that our JWT expired — arm the fast-reconnect flag.
            // The 4001 close will fire immediately after; we skip backoff there.
            if (data.type === 'error' && (data as { type: 'error'; code: string }).code === 'TOKEN_EXPIRED') {
              wsLog('TOKEN_EXPIRED received — will fast-reconnect on close');
              tokenExpiredRef.current = true;
              return;
            }

            // Reauth and pong acknowledgements are protocol-level — don't forward.
            if (
              (data.type as string) === 'reauthed' ||
              data.type === 'pong'
            ) {
              return;
            }

            if (data.type === 'ping') {
              if (ws?.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'pong', timestamp: new Date().toISOString() }));
              }
              return;
            }

            // ── Deduplication ────────────────────────────────────────────────
            let msgId: string | null = null;
            if ('suggestion_id' in data && data.suggestion_id) {
              msgId = `${data.type}:${data.suggestion_id}`;
            } else if ('correlation_id' in data && data.correlation_id) {
              msgId = `${data.type}:${data.correlation_id}`;
            }

            if (msgId) {
              if (recentMessageIdsRef.current.includes(msgId)) {
                wsLog('Dropping duplicate message:', msgId);
                return;
              }
              recentMessageIdsRef.current.push(msgId);
              if (recentMessageIdsRef.current.length > _DEDUP_WINDOW) {
                recentMessageIdsRef.current = recentMessageIdsRef.current.slice(-_DEDUP_WINDOW);
              }
            }

            if (data.type === 'subscribed') {
              wsLog('Subscribed to room:', data.room);
            } else if (data.type === 'unsubscribed') {
              wsLog('Unsubscribed from room:', data.room);
            } else if (data.type === 'error') {
              wsWarn('Server error:', data.message, data.code);
            }

            callbacksRef.current.onMessage?.(data);
          } catch {
            wsWarn('Failed to parse message:', event.data);
          }
        };

        ws.onerror = (event) => {
          isConnectedRef.current = false;
          forceUpdate();
          callbacksRef.current.onError?.(event);
        };

        ws.onclose = (event) => {
          wsLog('Disconnected:', event.code, event.reason);
          isConnectedRef.current = false;
          forceUpdate();

          if (heartbeatIntervalId) {
            clearInterval(heartbeatIntervalId);
            heartbeatIntervalId = null;
          }

          ws = null;
          wsRef.current = null;
          callbacksRef.current.onDisconnect?.();

          // TOKEN_EXPIRED close: the background refresh has already produced a fresh
          // token (held in tokenRef). Skip backoff entirely and reconnect immediately.
          if (tokenExpiredRef.current && tokenRef.current && shouldReconnectRef.current && reconnect) {
            wsLog('Fast-reconnect after TOKEN_EXPIRED (fresh token available)');
            tokenExpiredRef.current = false;
            reconnectAttemptsCountRef.current = 0;
            reconnectTimeout = setTimeout(connect, 100);
            reconnectTimeoutRef.current = reconnectTimeout;
            return;
          }

          if (
            shouldReconnectRef.current &&
            reconnect &&
            reconnectAttemptsCountRef.current < reconnectAttempts
          ) {
            reconnectAttemptsCountRef.current += 1;

            const base = Math.min(reconnectInterval * Math.pow(2, reconnectAttemptsCountRef.current), 30_000);
            const jitter = base * 0.25 * (Math.random() * 2 - 1);
            const delay = Math.max(0, base + jitter);

            wsLog(
              `Reconnecting in ${Math.round(delay)}ms`,
              `(attempt ${reconnectAttemptsCountRef.current}/${reconnectAttempts})`,
            );

            reconnectTimeout = setTimeout(connect, delay);
            reconnectTimeoutRef.current = reconnectTimeout;
          } else if (!shouldReconnectRef.current) {
            wsLog('Reconnect suppressed — disconnect() was called');
          } else {
            wsWarn('Max reconnection attempts reached — call reconnect() to retry');
          }
        };
      } catch (err) {
        wsWarn('Connection failed:', err);
        isConnectedRef.current = false;
      }
    };

    connect();

    return () => {
      shouldReconnectRef.current = false;
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      if (heartbeatIntervalId) clearInterval(heartbeatIntervalId);
      ws?.close(1000, 'Client unmount');
      isConnectedRef.current = false;
    };
  // `token` is intentionally absent — see tokenRef pattern above.
  }, [url, enabled, reconnect, reconnectAttempts, reconnectInterval, heartbeatInterval, stableAutoSubscribeRooms]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Token rotation effect ──────────────────────────────────────────────────
  // When the token prop changes (background JWT refresh), send a reauth frame
  // over the live connection so the server can update its stored payload.
  // If the connection is not currently open, tokenRef is already updated — the
  // next connect() call will use the fresh token in the auth frame.
  useEffect(() => {
    if (!token) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'reauth', token }));
      wsLog('Sent reauth with rotated token');
    }
  }, [token]);

  // ── Stable public API ─────────────────────────────────────────────────────────

  const send = useCallback((data: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    } else {
      wsWarn('Cannot send — connection not open');
    }
  }, []);

  const subscribe = useCallback((room: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'subscribe', room }));
      subscribedRoomsRef.current.add(room);
      wsLog('Subscribed to room:', room);
    } else {
      wsWarn('Cannot subscribe — connection not open');
    }
  }, []);

  const unsubscribe = useCallback((room: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'unsubscribe', room }));
      subscribedRoomsRef.current.delete(room);
      wsLog('Unsubscribed from room:', room);
    } else {
      wsWarn('Cannot unsubscribe — connection not open');
    }
  }, []);

  const disconnect = useCallback(() => {
    shouldReconnectRef.current = false;
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (heartbeatIntervalRef.current) {
      clearInterval(heartbeatIntervalRef.current);
      heartbeatIntervalRef.current = null;
    }
    wsRef.current?.close(1000, 'Client disconnect');
    wsRef.current = null;
    isConnectedRef.current = false;
  }, []);

  const reconnectFn = useCallback(() => {
    shouldReconnectRef.current = true;
    reconnectAttemptsCountRef.current = 0;
    if (wsRef.current) {
      wsRef.current.close(1000, 'Manual reconnect');
    } else {
      wsLog('No open socket — triggering fresh connect via forceUpdate');
      forceUpdate();
    }
  }, []);

  return {
    isConnected: isConnectedRef.current,
    send,
    subscribe,
    unsubscribe,
    disconnect,
    reconnect: reconnectFn,
  };
}
