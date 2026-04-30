// PATH: frontend/src/hooks/useScanStream.ts
// ──────────────────────────────────────────
/**
 * useScanStream — real SSE-backed scanner hook.
 *
 * Opens a POST /api/v1/scanner/stream connection, parses the text/event-stream
 * response with a manual line buffer (handles chunk boundaries safely), and
 * surfaces typed progress state to the caller.
 *
 * Auth: Bearer token is injected from the in-memory access token via
 * getAccessToken().  EventSource is not used because it does not support
 * custom request headers.
 *
 * Cleanup: AbortController aborts the fetch on unmount or when trigger() is
 * called again before a prior stream has finished.
 */
'use client';

import { useCallback, useRef, useState } from 'react';

import { getAccessToken } from '@/lib/api-client';
import { RunScanResponse, ScanType } from '@/types/market';

// ── State shape ────────────────────────────────────────────────────────────────

export interface ScanStreamState {
  isStreaming: boolean;
  pct: number;
  stage: string;
  message: string;
  result: RunScanResponse | null;
  error: string | null;
}

const IDLE: ScanStreamState = {
  isStreaming: false,
  pct: 0,
  stage: '',
  message: '',
  result: null,
  error: null,
};

// ── SSE payload shapes (mirrors backend ScanProgressEvent) ─────────────────────

interface SseProgressPayload {
  pct: number;
  stage: string;
  message: string;
}

interface SseCompletePayload extends SseProgressPayload {
  response: RunScanResponse;
}

interface SseErrorPayload {
  message: string;
  stage?: string;
  code?: string;
}

// ── Hook ───────────────────────────────────────────────────────────────────────

export function useScanStream(onComplete?: (result: RunScanResponse) => void) {
  const [state, setState] = useState<ScanStreamState>(IDLE);
  const abortRef = useRef<AbortController | null>(null);

  // Keep the callback in a ref so `trigger` never needs it as a dependency,
  // avoiding stale-closure issues when the parent re-renders.
  const onCompleteRef = useRef(onComplete);
  onCompleteRef.current = onComplete;

  const trigger = useCallback(async (scanType: ScanType = 'market_close') => {
    // Cancel any in-flight stream before starting a new one.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setState({
      isStreaming: true,
      pct: 0,
      stage: 'init',
      message: 'Starting scan…',
      result: null,
      error: null,
    });

    try {
      const token = getAccessToken();
      const response = await fetch('/api/v1/scanner/stream', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify({ scan_type: scanType }),
        signal: controller.signal,
      });

      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status}`);
      }

      const reader = response.body.getReader();
      // stream: true ensures multi-byte characters split across chunks decode correctly.
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        // SSE messages are delimited by double newlines.
        // Split on \n\n but keep the last (possibly incomplete) segment in the buffer.
        const parts = buffer.split('\n\n');
        buffer = parts.pop() ?? '';

        for (const rawMessage of parts) {
          const trimmed = rawMessage.trim();
          if (!trimmed) continue;

          let eventType = 'message';
          let dataLine = '';

          for (const line of trimmed.split('\n')) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim();
            } else if (line.startsWith('data: ')) {
              dataLine = line.slice(6);
            }
            // Lines starting with ': ' are keep-alive comments — ignored.
          }

          if (!dataLine) continue;

          let parsed: unknown;
          try {
            parsed = JSON.parse(dataLine);
          } catch {
            continue;
          }

          switch (eventType) {
            case 'progress': {
              const p = parsed as SseProgressPayload;
              setState((s) => ({ ...s, pct: p.pct, stage: p.stage, message: p.message }));
              break;
            }
            case 'complete': {
              const p = parsed as SseCompletePayload;
              setState({
                isStreaming: false,
                pct: 100,
                stage: 'complete',
                message: p.message,
                result: p.response,
                error: null,
              });
              onCompleteRef.current?.(p.response);
              break;
            }
            case 'error': {
              const p = parsed as SseErrorPayload;
              setState((s) => ({
                ...s,
                isStreaming: false,
                pct: 0,
                stage: 'error',
                message: p.message,
                error: p.message,
              }));
              break;
            }
          }
        }
      }
    } catch (err) {
      // AbortError is expected when cancel() is called — treat as silent.
      if ((err as Error).name === 'AbortError') return;
      const msg = err instanceof Error ? err.message : 'Scan failed — please try again.';
      setState((s) => ({
        ...s,
        isStreaming: false,
        pct: 0,
        stage: 'error',
        error: msg,
      }));
    }
  }, []); // stable — all external values accessed via refs

  const cancel = useCallback(() => {
    abortRef.current?.abort();
    setState(IDLE);
  }, []);

  const reset = useCallback(() => {
    setState(IDLE);
  }, []);

  return { ...state, trigger, cancel, reset };
}
