/**
 * useTrainingRunStream — ML Training Operator Console Live Stream
 * ================================================================
 * WebSocket hook that streams structured events from run_log.ndjson for an
 * active training run.
 *
 * Architecture mirrors usePnLWebSocket:
 *  - In-band auth: {"type":"auth","token":"<admin-jwt>"} as first frame (never in URL)
 *  - Token rotation: {"type":"reauth","token":"..."} in a dedicated effect
 *  - `connected` set only after server's {"type":"connected"} confirmation frame
 *  - Exponential backoff reconnect, capped at 30 s, ±20% jitter
 *  - Fatal close codes (4001, 4004) suppress reconnect
 *  - Heartbeat pings answered with pongs
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { WS_BASE_URL } from '@/lib/api';
import type { RunLogEntry, TrainingWsFrame } from '@/types/admin_training';
import { PIPELINE_STEPS } from '@/types/admin_training';

// ── Fatal close codes ─────────────────────────────────────────────────────────
const FATAL_CLOSE_CODES = new Set([
  4001, // Auth failed / not admin
  4004, // Run not found
]);

// ── Reconnect strategy ────────────────────────────────────────────────────────
const BASE_RECONNECT_MS = 1_000;
const MAX_RECONNECT_MS  = 30_000;
const CLIENT_HEARTBEAT_MS = 25_000;

// ── Types ─────────────────────────────────────────────────────────────────────

export type StreamConnectionState = 'connecting' | 'connected' | 'disconnected' | 'error' | 'complete';

export interface UseTrainingRunStreamReturn {
  connectionState: StreamConnectionState;
  isConnected: boolean;
  events: RunLogEntry[];
  completedSteps: Set<string>;
  currentStep: string | null;
  /** Actual elapsed seconds for each completed step (step_key → duration_s). */
  stepDurations: Map<string, number>;
  /** Server-side start timestamp (ms) for each step that has begun (step_key → Date.parse(ts)). */
  stepStartTimes: Map<string, number>;
  exitCode: number | null;
  disconnect: () => void;
  reconnect: () => void;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function backoffDelay(attempt: number): number {
  const base   = Math.min(BASE_RECONNECT_MS * 2 ** attempt, MAX_RECONNECT_MS);
  const jitter = base * 0.2 * (Math.random() * 2 - 1);
  return Math.round(base + jitter);
}

// ── Hook ──────────────────────────────────────────────────────────────────────

export function useTrainingRunStream(
  runId: string | null | undefined,
  token: string | null | undefined,
  enabled = true,
): UseTrainingRunStreamReturn {
  const [connectionState, setConnectionState] = useState<StreamConnectionState>('disconnected');
  const [events,          setEvents]          = useState<RunLogEntry[]>([]);
  const [completedSteps,  setCompletedSteps]  = useState<Set<string>>(new Set());
  const [currentStep,     setCurrentStep]     = useState<string | null>(null);
  const [stepDurations,   setStepDurations]   = useState<Map<string, number>>(new Map());
  const [stepStartTimes,  setStepStartTimes]  = useState<Map<string, number>>(new Map());
  const [exitCode,        setExitCode]        = useState<number | null>(null);

  const wsRef             = useRef<WebSocket | null>(null);
  const heartbeatRef      = useRef<ReturnType<typeof setInterval>  | null>(null);
  const reconnectRef      = useRef<ReturnType<typeof setTimeout>   | null>(null);
  const attemptsRef       = useRef(0);
  const shouldReconnectRef   = useRef(true);
  const tokenExpiredRef      = useRef(false);
  const connectRef           = useRef<(() => void) | null>(null);

  const tokenRef = useRef(token);
  tokenRef.current = token;

  const url = useMemo(() => {
    if (!runId) return null;
    return `${WS_BASE_URL}/admin/training/runs/${runId}/stream`;
  }, [runId]);

  const clearTimers = useCallback(() => {
    if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
    if (reconnectRef.current)  { clearTimeout(reconnectRef.current);  reconnectRef.current  = null; }
  }, []);

  // ── Connection effect ───────────────────────────────────────────────────────
  useEffect(() => {
    if (!enabled || !url) return;

    attemptsRef.current        = 0;
    shouldReconnectRef.current = true;

    const connect = () => {
      if (wsRef.current?.readyState === WebSocket.OPEN) return;

      tokenExpiredRef.current = false;
      setConnectionState('connecting');

      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch {
        setConnectionState('error');
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        const currentToken = tokenRef.current;
        if (currentToken) {
          ws.send(JSON.stringify({ type: 'auth', token: currentToken }));
        }
      };

      ws.onmessage = (event: MessageEvent) => {
        let frame: TrainingWsFrame;
        try {
          frame = JSON.parse(event.data as string) as TrainingWsFrame;
        } catch {
          return;
        }

        if (frame.type === 'connected') {
          setConnectionState('connected');
          attemptsRef.current = 0;
          heartbeatRef.current = setInterval(() => {
            if (ws.readyState === WebSocket.OPEN) {
              ws.send(JSON.stringify({ type: 'ping' }));
            }
          }, CLIENT_HEARTBEAT_MS);
          return;
        }

        if (frame.type === 'error') {
          if (frame.code === 'TOKEN_EXPIRED') {
            tokenExpiredRef.current = true;
          }
          return;
        }

        const frameType = (frame as { type: string }).type;

        if (frameType === 'ping') {
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'pong' }));
          }
          return;
        }

        if (frameType === 'pong') return;

        if (frame.type === 'run_complete') {
          setExitCode(frame.exit_code);
          setConnectionState('complete');
          shouldReconnectRef.current = false;
          ws.close(1000, 'Run complete');
          return;
        }

        if (frame.type === 'run_event') {
          const entry = frame.data;
          setEvents(prev => [...prev, entry]);

          if (entry.event === 'run_start') {
            if (!entry.resumed) {
              // Fresh run — initialise from step 1.
              setCurrentStep('step_1_symbols');
              setStepStartTimes(new Map([['step_1_symbols', Date.parse(entry.ts)]]));
            }
            // Resumed run: currentStep and stepStartTimes are already correctly
            // set by the synthetic step_complete replay frames the backend emits
            // immediately after the `connected` frame.  Resetting here would
            // undo that state and revert the progress bar to 0.
          }

          if (entry.event === 'step_complete' && entry.step) {
            // Mark step done.
            setCompletedSteps(prev => { const n = new Set(prev); n.add(entry.step!); return n; });

            // Record actual step duration.
            if (entry.duration_s !== undefined) {
              setStepDurations(prev => new Map(prev).set(entry.step!, entry.duration_s!));
            }

            // Advance currentStep.
            // step_num is 1-indexed; PIPELINE_STEPS[step_num] is the NEXT step (0-indexed).
            if (entry.step_num !== undefined) {
              const nextStep = PIPELINE_STEPS[entry.step_num] ?? null;
              setCurrentStep(nextStep?.key ?? null);
              if (nextStep) {
                // Record the server timestamp at which the next step began.
                setStepStartTimes(prev => new Map(prev).set(nextStep.key, Date.parse(entry.ts)));
              }
            }
          }
        }
      };

      ws.onerror = () => { clearTimers(); };

      ws.onclose = (event) => {
        wsRef.current = null;
        clearTimers();

        if (connectionState === 'complete' || !shouldReconnectRef.current) {
          return;
        }

        if (tokenExpiredRef.current && tokenRef.current && shouldReconnectRef.current) {
          tokenExpiredRef.current = false;
          attemptsRef.current = 0;
          setConnectionState('disconnected');
          reconnectRef.current = setTimeout(connect, 100);
          return;
        }

        if (FATAL_CLOSE_CODES.has(event.code)) {
          setConnectionState('error');
          return;
        }

        if (event.code === 1000) {
          setConnectionState('disconnected');
          return;
        }

        attemptsRef.current += 1;
        const delay = backoffDelay(attemptsRef.current);
        setConnectionState('disconnected');
        reconnectRef.current = setTimeout(connect, delay);
      };
    };

    connectRef.current = connect;
    connect();

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
    };
  // token intentionally absent — handled by reauth effect below
  }, [url, enabled, clearTimers]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Token rotation effect ───────────────────────────────────────────────────
  useEffect(() => {
    if (!token) return;
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'reauth', token }));
    }
  }, [token]);

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
    setTimeout(() => connectRef.current?.(), 0);
  }, [clearTimers]);

  return {
    connectionState,
    isConnected: connectionState === 'connected',
    events,
    completedSteps,
    currentStep,
    stepDurations,
    stepStartTimes,
    exitCode,
    disconnect,
    reconnect,
  };
}
