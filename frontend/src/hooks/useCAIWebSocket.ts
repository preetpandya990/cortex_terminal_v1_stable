/**
 * CORTEX AI Microservice - WebSocket Hook
 *
 * Provides real-time updates from CAI via WebSocket connection to Redis pub/sub.
 * Implements automatic reconnection with exponential backoff.
 *
 * Auth contract:
 * - Auto-connect is gated: the socket only opens once isAuthReady && isAuthenticated.
 * - An in-band auth handshake ({ type: 'auth', token }) is sent immediately on open
 *   so the backend can validate the session without embedding the token in the URL
 *   (URL tokens are visible in server logs and browser history).
 * - On logout the socket is intentionally closed and reconnect is suppressed.
 *
 * Requirements: 21.1, 21.2, 21.3, 21.4
 */

import { useEffect, useRef, useState, useCallback } from 'react';
import { useAuth } from '@/contexts/AuthContext';

export type ConnectionStatus = 'connected' | 'connecting' | 'disconnected' | 'reconnecting';

export interface CAIMessage {
  channel: string;
  data: any;
}

export interface UseCAIWebSocketOptions {
  onMessage?: (message: CAIMessage) => void;
  onSignal?: (signal: any) => void;
  onRegime?: (regime: any) => void;
  onEvent?: (event: any) => void;
  onModelAlert?: (alert: any) => void;
  autoConnect?: boolean;
  maxReconnectAttempts?: number;
  baseReconnectDelay?: number;
}

export interface UseCAIWebSocketReturn {
  status: ConnectionStatus;
  connect: () => void;
  disconnect: () => void;
  sendPing: () => void;
}

export function useCAIWebSocket(options: UseCAIWebSocketOptions = {}): UseCAIWebSocketReturn {
  const {
    onMessage,
    onSignal,
    onRegime,
    onEvent,
    onModelAlert,
    autoConnect = true,
    maxReconnectAttempts = 10,
    baseReconnectDelay = 1000,
  } = options;

  const { isAuthenticated, isAuthReady, accessToken } = useAuth();

  const [status, setStatus] = useState<ConnectionStatus>('disconnected');
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const intentionalDisconnectRef = useRef(false);

  // Keep the latest token in a ref so the connect callback always sends the
  // current value without needing it as a dep (which would cause reconnects on refresh).
  const accessTokenRef = useRef(accessToken);
  useEffect(() => {
    accessTokenRef.current = accessToken;
  }, [accessToken]);

  // Keep callbacks in a ref so they can be updated without recreating connect/disconnect.
  const callbacksRef = useRef({ onMessage, onSignal, onRegime, onEvent, onModelAlert });
  useEffect(() => {
    callbacksRef.current = { onMessage, onSignal, onRegime, onEvent, onModelAlert };
  }, [onMessage, onSignal, onRegime, onEvent, onModelAlert]);

  const getReconnectDelay = useCallback(
    (attempt: number): number => Math.min(baseReconnectDelay * Math.pow(2, attempt), 30_000),
    [baseReconnectDelay]
  );

  const handleMessage = useCallback((event: MessageEvent) => {
    try {
      const message = JSON.parse(event.data);

      if (message.type === 'connected') {
        console.log('[CAI WebSocket] Connected to channels:', message.channels);
        setStatus('connected');
        reconnectAttemptsRef.current = 0;
        return;
      }

      if (message.type === 'heartbeat') return;

      if (message.channel && message.data) {
        const caiMessage: CAIMessage = message;
        callbacksRef.current.onMessage?.(caiMessage);

        if (message.channel === 'cai:signals:all') {
          callbacksRef.current.onSignal?.(message.data);
        } else if (message.channel === 'cai:regime:all') {
          callbacksRef.current.onRegime?.(message.data);
        } else if (message.channel === 'cai:events:high_impact') {
          callbacksRef.current.onEvent?.(message.data);
        } else if (message.channel === 'cai:models:drift_alerts') {
          callbacksRef.current.onModelAlert?.(message.data);
        }
      }
    } catch (error) {
      console.error('[CAI WebSocket] Failed to parse message:', error);
    }
  }, []);

  // Forward-declared via ref so scheduleReconnect can call connect before it is defined.
  const connectRef = useRef<() => void>(() => {});

  const scheduleReconnect = useCallback(() => {
    if (intentionalDisconnectRef.current) return;

    if (reconnectAttemptsRef.current >= maxReconnectAttempts) {
      console.error('[CAI WebSocket] Max reconnection attempts reached');
      setStatus('disconnected');
      return;
    }

    const delay = getReconnectDelay(reconnectAttemptsRef.current);
    console.log(
      `[CAI WebSocket] Reconnecting in ${delay}ms (attempt ${reconnectAttemptsRef.current + 1}/${maxReconnectAttempts})`
    );

    setStatus('reconnecting');
    reconnectAttemptsRef.current += 1;
    reconnectTimeoutRef.current = setTimeout(() => connectRef.current(), delay);
  }, [maxReconnectAttempts, getReconnectDelay]);

  const connect = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    intentionalDisconnectRef.current = false;
    setStatus('connecting');

    try {
      // Build the WebSocket URL from the dedicated CAI env var, or fall back to
      // deriving it from NEXT_PUBLIC_API_URL.  Never fall back to window.location.host
      // — the Next.js dev server cannot handle WebSocket upgrades and the connection
      // would fail silently, cycling through exponential backoff forever.
      const wsUrl = (() => {
        if (process.env.NEXT_PUBLIC_CAI_WS_URL) {
          return process.env.NEXT_PUBLIC_CAI_WS_URL;
        }
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const apiHost =
          process.env.NEXT_PUBLIC_API_URL?.replace(/^https?:\/\//, '') ?? 'localhost:8000';
        return `${protocol}//${apiHost}/api/v1/cai/stream`;
      })();

      console.log('[CAI WebSocket] Connecting to:', wsUrl);
      const ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        console.log('[CAI WebSocket] Connection opened');
        // In-band auth handshake — credentials never appear in URLs or server logs.
        const token = accessTokenRef.current;
        if (token) {
          ws.send(JSON.stringify({ type: 'auth', token }));
        }
        setStatus('connected');
        reconnectAttemptsRef.current = 0;
      };

      ws.onmessage = handleMessage;

      ws.onerror = () => {
        // WebSocket ErrorEvent objects are intentionally opaque by the browser spec —
        // no message or reason is exposed.  This fires for every connection attempt
        // while the backend is starting up, so log at warn to avoid flooding the
        // Next.js error overlay during normal reconnect cycles.
        // The definitive failure is logged by scheduleReconnect() once max attempts
        // are exhausted.
        console.warn(
          `[CAI WebSocket] Connection failed — url: ${wsUrl} | readyState: ${ws.readyState}`,
        );
      };

      ws.onclose = (event) => {
        console.log('[CAI WebSocket] Connection closed:', event.code, event.reason);
        wsRef.current = null;

        if (!intentionalDisconnectRef.current) {
          scheduleReconnect();
        } else {
          setStatus('disconnected');
        }
      };

      wsRef.current = ws;
    } catch (error) {
      console.error('[CAI WebSocket] Failed to create connection:', error);
      setStatus('disconnected');
      scheduleReconnect();
    }
  }, [handleMessage, scheduleReconnect]);

  // Keep the ref current so scheduleReconnect always calls the latest connect.
  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  const disconnect = useCallback(() => {
    intentionalDisconnectRef.current = true;

    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    setStatus('disconnected');
    reconnectAttemptsRef.current = 0;
  }, []);

  const sendPing = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send('ping');
    }
  }, []);

  // Auth-gated auto-connect.
  // Conditions: hook must be configured for autoConnect, auth init must have
  // completed (isAuthReady), and the user must be authenticated.
  // On logout (isAuthenticated flips to false) the socket is closed immediately
  // to avoid stale sessions receiving live trading data.
  const canConnect = isAuthReady && isAuthenticated;

  useEffect(() => {
    if (autoConnect && canConnect) {
      connect();
    } else if (!canConnect) {
      disconnect();
    }

    return () => {
      disconnect();
    };
    // connect/disconnect are stable useCallbacks; auth state drives re-evaluation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoConnect, canConnect]);

  return { status, connect, disconnect, sendPing };
}
