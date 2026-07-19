'use client';

/**
 * PnLStreamProvider — single shared paper-trading P&L WebSocket
 * ==============================================================
 * Runs `usePnLWebSocket` exactly once for the dashboard and exposes it via
 * context, so every consumer (OpenPositionsTable, PortfolioInsightSection)
 * reads the same live frame from one connection instead of each opening its
 * own. Single source of truth; no duplicate sockets on a hot real-time path.
 *
 * The one-shot auth-recovery on a 4001 fatal close lives here (moved verbatim
 * from OpenPositionsTable) so the shared stream self-heals once per connection.
 */

import { createContext, useContext, useEffect, useRef, type ReactNode } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { usePortfolioSummary } from '@/hooks/usePaperTrading';
import { usePnLWebSocket, type UsePnLWebSocketReturn } from '@/hooks/usePnLWebSocket';

const PnLStreamContext = createContext<UsePnLWebSocketReturn | null>(null);

export function PnLStreamProvider({ children }: { children: ReactNode }) {
  const { accessToken, isAuthenticated, isAuthReady, refreshToken } = useAuth();

  // react-query cached — shares the same portfolio fetch as OpenPositionsTable.
  const { data: portfolio } = usePortfolioSummary({
    enabled: isAuthReady && isAuthenticated,
  });

  const stream = usePnLWebSocket(
    portfolio?.id,
    accessToken,
    isAuthReady && isAuthenticated && !!portfolio?.id,
  );

  // Single recovery attempt on auth failure (4001): refresh the token once and
  // reconnect. If the refresh itself fails the feed stays "Offline" — the
  // session has genuinely expired. The flag resets on every successful
  // connection so a later auth error (e.g. after a long idle) can self-heal too.
  const authRecoveredRef = useRef(false);
  const { connectionState, reconnect } = stream;
  useEffect(() => {
    if (connectionState === 'connected') {
      authRecoveredRef.current = false;
      return;
    }
    if (connectionState !== 'error' || authRecoveredRef.current) return;
    authRecoveredRef.current = true;
    void refreshToken().then((ok) => {
      if (ok) reconnect();
    });
  }, [connectionState, refreshToken, reconnect]);

  return <PnLStreamContext.Provider value={stream}>{children}</PnLStreamContext.Provider>;
}

export function usePnLStream(): UsePnLWebSocketReturn {
  const ctx = useContext(PnLStreamContext);
  if (ctx === null) {
    throw new Error('usePnLStream must be used within a <PnLStreamProvider>');
  }
  return ctx;
}
