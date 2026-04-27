/**
 * useWebSocket Hook - Production-Grade WebSocket with Auto-Reconnect
 * ===================================================================
 * 
 * Architecture (2026 Best Practices - Zero Infinite Loops):
 * ----------------------------------------------------------
 * 1. Connection state stored in ref (stable reference)
 * 2. State updates via useState with functional updates (no dependencies)
 * 3. Dependencies stabilized with useMemo
 * 4. Single effect for connection lifecycle
 * 5. Cleanup guaranteed on unmount
 * 6. Force update mechanism for connection state changes
 * 
 * Key Principle: Minimize re-renders while keeping UI reactive.
 * 
 * Usage:
 * ```tsx
 * const ws = useWebSocket({
 *   url: 'ws://localhost:8000/api/v1/trade-suggestions/ws',
 *   onMessage: (data) => {
 *     if (data.type === 'new_suggestion') {
 *       queryClient.invalidateQueries(['trade-suggestions']);
 *     }
 *   },
 * });
 * 
 * // Access connection state
 * console.log(ws.isConnected);
 * ```
 */

import { useEffect, useRef, useMemo, useCallback, useReducer } from 'react';

export type WebSocketMessage = 
  | {
      type: 'new_suggestion';
      suggestion_id: string;
      symbol: string;
      signal_direction: 'BUY' | 'SELL';
      consensus_score: number;
      confidence_level: 'HIGH' | 'MEDIUM' | 'LOW';
      timestamp: string;
    }
  | {
      type: 'suggestion_expired';
      suggestion_id: string;
      symbol: string;
      expired_at: string;
      reason: string;
    }
  | {
      type: 'correlation_completed';
      correlation_id: string;
      suggestion_id: string;
      consensus_score: number;
      completed_at: string;
    }
  | {
      type: 'correlation_rejected';
      correlation_id: string;
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
  send: (data: any) => void;
  subscribe: (room: string) => void;
  unsubscribe: (room: string) => void;
  disconnect: () => void;
}

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

  // Stabilize autoSubscribeRooms to prevent effect re-runs
  const stableAutoSubscribeRooms = useMemo(
    () => autoSubscribeRooms,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [JSON.stringify(autoSubscribeRooms)]
  );

  // Force update mechanism (doesn't cause infinite loops)
  const [, forceUpdate] = useReducer((x: number) => x + 1, 0);

  // ALL state stored in refs
  const wsRef = useRef<WebSocket | null>(null);
  const isConnectedRef = useRef(false);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const heartbeatIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const shouldReconnectRef = useRef(true);
  const reconnectAttemptsCountRef = useRef(0);
  const subscribedRoomsRef = useRef<Set<string>>(new Set());
  
  // Store callbacks in ref to avoid dependency changes
  const callbacksRef = useRef({ onMessage, onConnect, onDisconnect, onError });
  
  // Update callbacks without triggering effects
  useEffect(() => {
    callbacksRef.current = { onMessage, onConnect, onDisconnect, onError };
  }, [onMessage, onConnect, onDisconnect, onError]);

  // Single effect for connection lifecycle
  useEffect(() => {
    // Respect the enabled gate — don't open a socket until the caller says it's safe
    // (typically: isAuthenticated && isAuthReady).
    if (!enabled) return;

    shouldReconnectRef.current = true;
    let ws: WebSocket | null = null;
    let reconnectTimeout: NodeJS.Timeout | null = null;
    let heartbeatIntervalId: NodeJS.Timeout | null = null;

    const connect = () => {
      // Don't connect if already connected
      if (ws?.readyState === WebSocket.OPEN) {
        return;
      }

      console.log('[WebSocket] Connecting to', url);

      try {
        ws = new WebSocket(url);
        wsRef.current = ws;

        ws.onopen = () => {
          console.log('[WebSocket] Connected');

          // In-band auth handshake — sending the token as a message keeps credentials
          // out of URLs, server logs, and browser history.
          if (token) {
            ws?.send(JSON.stringify({ type: 'auth', token }));
          }

          isConnectedRef.current = true;
          reconnectAttemptsCountRef.current = 0;
          forceUpdate(); // Trigger re-render to update UI

          // Start heartbeat
          heartbeatIntervalId = setInterval(() => {
            if (ws?.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'pong' }));
            }
          }, heartbeatInterval);
          heartbeatIntervalRef.current = heartbeatIntervalId;

          // Re-subscribe to rooms after reconnection
          subscribedRoomsRef.current.forEach((room) => {
            if (ws?.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'subscribe', room }));
            }
          });

          // Auto-subscribe to rooms
          stableAutoSubscribeRooms.forEach((room) => {
            if (ws?.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'subscribe', room }));
              subscribedRoomsRef.current.add(room);
            }
          });

          // Call user callback
          callbacksRef.current.onConnect?.();
        };

        ws.onmessage = (event) => {
          try {
            const data: WebSocketMessage = JSON.parse(event.data);

            // Handle ping (server heartbeat)
            if (data.type === 'ping') {
              if (ws?.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'pong' }));
              }
            }

            // Handle subscription confirmations
            if (data.type === 'subscribed') {
              console.log(`[WebSocket] Subscribed to room: ${data.room}`);
            } else if (data.type === 'unsubscribed') {
              console.log(`[WebSocket] Unsubscribed from room: ${data.room}`);
            } else if (data.type === 'error') {
              console.error(`[WebSocket] Server error: ${data.message} (${data.code})`);
            }

            // Call user callback
            callbacksRef.current.onMessage?.(data);
          } catch (err) {
            console.error('[WebSocket] Failed to parse message:', err);
          }
        };

        ws.onerror = (event) => {
          // Suppress logging for transient connection errors (will auto-reconnect)
          // Only log if there's meaningful error info
          if (event && (event as any).message) {
            console.error('[WebSocket] Error:', event);
          }
          isConnectedRef.current = false;
          forceUpdate(); // Trigger re-render to update UI
          callbacksRef.current.onError?.(event);
        };

        ws.onclose = (event) => {
          console.log('[WebSocket] Disconnected:', event.code, event.reason);
          isConnectedRef.current = false;
          forceUpdate(); // Trigger re-render to update UI
          
          // Clear heartbeat
          if (heartbeatIntervalId) {
            clearInterval(heartbeatIntervalId);
            heartbeatIntervalId = null;
          }
          
          ws = null;
          wsRef.current = null;
          callbacksRef.current.onDisconnect?.();

          // Attempt reconnection
          if (shouldReconnectRef.current && reconnect && reconnectAttemptsCountRef.current < reconnectAttempts) {
            reconnectAttemptsCountRef.current += 1;

            // Exponential backoff with jitter
            const baseDelay = reconnectInterval;
            const maxDelay = 30000; // Max 30 seconds
            const delay = Math.min(baseDelay * Math.pow(2, reconnectAttemptsCountRef.current), maxDelay);
            const jitter = delay * 0.25 * (Math.random() * 2 - 1);
            const finalDelay = delay + jitter;

            console.log(`[WebSocket] Reconnecting in ${Math.round(finalDelay)}ms (attempt ${reconnectAttemptsCountRef.current}/${reconnectAttempts})`);

            reconnectTimeout = setTimeout(() => {
              connect();
            }, finalDelay);
            reconnectTimeoutRef.current = reconnectTimeout;
          } else if (reconnectAttemptsCountRef.current >= reconnectAttempts) {
            console.error('[WebSocket] Max reconnection attempts reached');
          }
        };
      } catch (err) {
        console.error('[WebSocket] Connection failed:', err);
        isConnectedRef.current = false;
      }
    };

    // Start connection
    connect();

    // Cleanup on unmount
    return () => {
      shouldReconnectRef.current = false;

      if (reconnectTimeout) {
        clearTimeout(reconnectTimeout);
      }

      if (heartbeatIntervalId) {
        clearInterval(heartbeatIntervalId);
      }

      if (ws) {
        ws.close(1000, 'Client disconnect');
      }

      isConnectedRef.current = false;
    };
  }, [url, token, enabled, reconnect, reconnectAttempts, reconnectInterval, heartbeatInterval, stableAutoSubscribeRooms]);

  // Expose stable API using useCallback
  const send = useCallback((data: any) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    } else {
      console.warn('[WebSocket] Cannot send message: connection not open');
    }
  }, []);

  const subscribe = useCallback((room: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'subscribe', room }));
      subscribedRoomsRef.current.add(room);
      console.log(`[WebSocket] Subscribed to room: ${room}`);
    } else {
      console.warn('[WebSocket] Cannot subscribe: connection not open');
    }
  }, []);

  const unsubscribe = useCallback((room: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'unsubscribe', room }));
      subscribedRoomsRef.current.delete(room);
      console.log(`[WebSocket] Unsubscribed from room: ${room}`);
    } else {
      console.warn('[WebSocket] Cannot unsubscribe: connection not open');
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

    if (wsRef.current) {
      wsRef.current.close(1000, 'Client disconnect');
      wsRef.current = null;
    }

    isConnectedRef.current = false;
  }, []);

  return {
    isConnected: isConnectedRef.current,
    send,
    subscribe,
    unsubscribe,
    disconnect,
  };
}
