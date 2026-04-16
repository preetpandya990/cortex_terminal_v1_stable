/**
 * CORTEX AI Microservice - WebSocket Hook
 * 
 * Provides real-time updates from CAI via WebSocket connection to Redis pub/sub.
 * Implements automatic reconnection with exponential backoff.
 * 
 * Requirements: 21.1, 21.2, 21.3, 21.4
 */

import { useEffect, useRef, useState, useCallback } from 'react';

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

/**
 * Custom hook for CAI WebSocket connection with automatic reconnection.
 * 
 * @param options Configuration options
 * @returns WebSocket connection state and controls
 */
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

  const [status, setStatus] = useState<ConnectionStatus>('disconnected');
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const intentionalDisconnectRef = useRef(false);

  /**
   * Calculate exponential backoff delay for reconnection.
   * Implements exponential backoff: delay = baseDelay * 2^attempt (capped at 30s)
   */
  const getReconnectDelay = useCallback((attempt: number): number => {
    const delay = baseReconnectDelay * Math.pow(2, attempt);
    return Math.min(delay, 30000); // Cap at 30 seconds
  }, [baseReconnectDelay]);

  /**
   * Handle incoming WebSocket messages and route to appropriate handlers.
   */
  const handleMessage = useCallback((event: MessageEvent) => {
    try {
      const message = JSON.parse(event.data);

      // Handle connection confirmation
      if (message.type === 'connected') {
        console.log('[CAI WebSocket] Connected to channels:', message.channels);
        setStatus('connected');
        reconnectAttemptsRef.current = 0;
        return;
      }

      // Handle heartbeat
      if (message.type === 'heartbeat') {
        return;
      }

      // Handle channel messages
      if (message.channel && message.data) {
        const caiMessage: CAIMessage = message;

        // Call generic message handler
        onMessage?.(caiMessage);

        // Route to specific handlers based on channel
        if (message.channel === 'cai:signals:all') {
          onSignal?.(message.data);
        } else if (message.channel === 'cai:regime:all') {
          onRegime?.(message.data);
        } else if (message.channel === 'cai:events:high_impact') {
          onEvent?.(message.data);
        } else if (message.channel === 'cai:models:drift_alerts') {
          onModelAlert?.(message.data);
        }
      }
    } catch (error) {
      console.error('[CAI WebSocket] Failed to parse message:', error);
    }
  }, [onMessage, onSignal, onRegime, onEvent, onModelAlert]);

  /**
   * Attempt to reconnect with exponential backoff.
   */
  const scheduleReconnect = useCallback(() => {
    if (intentionalDisconnectRef.current) {
      return;
    }

    if (reconnectAttemptsRef.current >= maxReconnectAttempts) {
      console.error('[CAI WebSocket] Max reconnection attempts reached');
      setStatus('disconnected');
      return;
    }

    const delay = getReconnectDelay(reconnectAttemptsRef.current);
    console.log(`[CAI WebSocket] Reconnecting in ${delay}ms (attempt ${reconnectAttemptsRef.current + 1}/${maxReconnectAttempts})`);
    
    setStatus('reconnecting');
    reconnectAttemptsRef.current += 1;

    reconnectTimeoutRef.current = setTimeout(() => {
      connect();
    }, delay);
  }, [maxReconnectAttempts, getReconnectDelay]);

  /**
   * Connect to CAI WebSocket endpoint.
   */
  const connect = useCallback(() => {
    // Clean up existing connection
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    // Clear any pending reconnection
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }

    intentionalDisconnectRef.current = false;
    setStatus('connecting');

    try {
      // Determine WebSocket URL based on current location
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const host = process.env.NEXT_PUBLIC_API_URL?.replace(/^https?:\/\//, '') || window.location.host;
      const wsUrl = `${protocol}//${host}/api/v1/cai/stream`;

      console.log('[CAI WebSocket] Connecting to:', wsUrl);
      const ws = new WebSocket(wsUrl);

      ws.onopen = () => {
        console.log('[CAI WebSocket] Connection opened');
        setStatus('connected');
        reconnectAttemptsRef.current = 0;
      };

      ws.onmessage = handleMessage;

      ws.onerror = (error) => {
        console.error('[CAI WebSocket] Error:', error);
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

  /**
   * Disconnect from WebSocket.
   */
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

  /**
   * Send ping to keep connection alive.
   */
  const sendPing = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send('ping');
    }
  }, []);

  // Auto-connect on mount if enabled
  useEffect(() => {
    if (autoConnect) {
      connect();
    }

    return () => {
      disconnect();
    };
  }, [autoConnect]); // Only run on mount/unmount

  return {
    status,
    connect,
    disconnect,
    sendPing,
  };
}
