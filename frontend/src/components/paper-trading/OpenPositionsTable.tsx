"use client";

/**
 * OpenPositionsTable
 * ====================
 * Real implementation replacing OpenPositionsPlaceholder.
 *
 * Architecture:
 *  - REST: usePortfolioSummary + usePositions (TanStack Query, 30 s stale)
 *  - Live P&L: usePnLWebSocket — 500 ms frames from the backend P&L worker.
 *    Each position row reads from the shared positionPnLMap ref so individual
 *    row re-renders are driven by a row-level useEffect, not parent state.
 *  - First visit (no portfolio): renders CreatePortfolioModal inline prompt.
 *  - Close: ClosePositionModal per row.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertCircle,
  ChevronDown,
  Loader2,
  Plus,
  TrendingDown,
  TrendingUp,
  X,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { usePortfolioSummary, usePositions } from "@/hooks/usePaperTrading";
import { usePnLWebSocket } from "@/hooks/usePnLWebSocket";
import { PortfolioSummaryCard } from "./PortfolioSummaryCard";
import { CreatePortfolioModal } from "./CreatePortfolioModal";
import { ClosePositionModal } from "./ClosePositionModal";
import type { LivePositionPnL, PaperPosition } from "@/types/paper_trading";

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

// ──────────────────────────────────────────────────────────────────────────────
// Live P&L Row
// Each row subscribes to the shared positionPnLMap ref via a 500 ms interval
// so only the changed cell re-renders, not the whole table.
// ──────────────────────────────────────────────────────────────────────────────

interface PositionRowProps {
  position: PaperPosition;
  positionPnLMap: React.MutableRefObject<Map<string, LivePositionPnL>>;
  onClose: (position: PaperPosition, livePrice?: number) => void;
}

function PositionRow({ position, positionPnLMap, onClose }: PositionRowProps) {
  const [livePnL, setLivePnL] = useState<LivePositionPnL | null>(null);

  // Poll the shared ref at ~500 ms — matches the backend worker cadence.
  // We avoid subscribing the parent to a state update for every tick.
  useEffect(() => {
    const id = window.setInterval(() => {
      const tick = positionPnLMap.current.get(position.id);
      if (tick) setLivePnL(tick);
    }, 500);
    return () => window.clearInterval(id);
  }, [position.id, positionPnLMap]);

  const displayPnl = livePnL?.unrealized_pnl ?? position.unrealized_pnl;
  const displayPct = livePnL?.pnl_pct ?? null;
  const lastPrice = livePnL?.last_price ?? position.last_price;
  const isLong = position.side === "LONG";
  const isPositive = (displayPnl ?? 0) >= 0;

  return (
    <tr className="border-t border-slate-100 transition-colors hover:bg-slate-50/50">
      {/* Symbol */}
      <td className="px-3 py-2.5">
        <div className="font-semibold text-slate-900">{position.symbol}</div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide">
          {position.status === "OPEN" ? "Open" : "Closed"}
        </div>
      </td>

      {/* Side */}
      <td className="px-3 py-2.5">
        <span
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${
            isLong ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"
          }`}
        >
          {isLong ? <TrendingUp className="h-3 w-3" /> : <TrendingDown className="h-3 w-3" />}
          {isLong ? "Long" : "Short"}
        </span>
      </td>

      {/* Qty */}
      <td className="px-3 py-2.5 text-right font-medium text-slate-700">
        {position.quantity.toLocaleString("en-IN")}
      </td>

      {/* Avg Cost */}
      <td className="px-3 py-2.5 text-right text-slate-600">
        {INR.format(position.avg_cost_price)}
      </td>

      {/* Last Price */}
      <td className="px-3 py-2.5 text-right font-medium text-slate-900">
        {lastPrice != null ? INR.format(lastPrice) : "—"}
      </td>

      {/* Unrealized P&L */}
      <td className={`px-3 py-2.5 text-right font-semibold ${isPositive ? "text-emerald-600" : "text-rose-600"}`}>
        {displayPnl != null ? (
          <>
            {isPositive ? "+" : ""}
            {INR.format(displayPnl)}
          </>
        ) : (
          "—"
        )}
      </td>

      {/* P&L % */}
      <td className={`px-3 py-2.5 text-right text-sm ${isPositive ? "text-emerald-600" : "text-rose-600"}`}>
        {displayPct != null ? (
          <>
            {displayPct >= 0 ? "+" : ""}
            {displayPct.toFixed(2)}%
          </>
        ) : (
          "—"
        )}
      </td>

      {/* Stop Loss */}
      <td className="px-3 py-2.5 text-right text-slate-500 text-xs">
        {position.stop_loss != null ? INR.format(position.stop_loss) : "—"}
      </td>

      {/* Target 1 */}
      <td className="px-3 py-2.5 text-right text-slate-500 text-xs">
        {position.target_price_1 != null ? INR.format(position.target_price_1) : "—"}
      </td>

      {/* Close Button */}
      <td className="px-3 py-2.5 text-right">
        <button
          onClick={() => onClose(position, livePnL?.last_price)}
          className="rounded-lg border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-semibold text-slate-600 hover:border-rose-300 hover:bg-rose-50 hover:text-rose-700 transition-colors"
        >
          Close
        </button>
      </td>
    </tr>
  );
}

// ──────────────────────────────────────────────────────────────────────────────
// Main component
// ──────────────────────────────────────────────────────────────────────────────

export function OpenPositionsTable() {
  const { accessToken, isAuthenticated, isAuthReady } = useAuth();

  const [showCreate, setShowCreate] = useState(false);
  const [closingPosition, setClosingPosition] = useState<{
    position: PaperPosition;
    livePrice?: number;
  } | null>(null);

  const {
    data: portfolio,
    isLoading: portfolioLoading,
    isError: portfolioError,
    error: portfolioErr,
    refetch: refetchPortfolio,
  } = usePortfolioSummary({
    enabled: isAuthReady && isAuthenticated,
  });

  const noPortfolio =
    portfolioError && (portfolioErr as any)?.statusCode === 404;

  const {
    data: positionsData,
    isLoading: positionsLoading,
    refetch: refetchPositions,
  } = usePositions({ status: "OPEN" }, !!portfolio?.id);

  const openPositions = positionsData?.positions ?? [];

  const {
    isConnected: wsConnected,
    connectionState: wsState,
    positionPnLMap,
    portfolioStats,
  } = usePnLWebSocket(
    portfolio?.id,
    accessToken,
    isAuthReady && isAuthenticated && !!portfolio?.id
  );

  const handleCloseRequested = useCallback(
    (position: PaperPosition, livePrice?: number) => {
      setClosingPosition({ position, livePrice });
    },
    []
  );

  const handlePositionClosed = useCallback(() => {
    setClosingPosition(null);
    refetchPositions();
    refetchPortfolio();
  }, [refetchPositions, refetchPortfolio]);

  // ── Loading state ──────────────────────────────────────────────────────────
  if (!isAuthReady || portfolioLoading) {
    return (
      <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-center gap-3 text-slate-500">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span className="text-sm">Loading paper trading…</span>
        </div>
      </section>
    );
  }

  // ── No portfolio — prompt to create ───────────────────────────────────────
  if (noPortfolio || (!portfolioLoading && !portfolio && !portfolioError)) {
    return (
      <>
        <section className="rounded-2xl border border-slate-200 bg-white p-8 shadow-sm text-center">
          <div className="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-blue-50">
            <Activity className="h-7 w-7 text-blue-600" />
          </div>
          <h2 className="mb-1 text-lg font-semibold text-slate-900">
            Start Paper Trading
          </h2>
          <p className="mb-6 text-sm text-slate-500 max-w-sm mx-auto">
            Create a virtual portfolio to simulate trades using real-time
            signals — zero risk, full analytics.
          </p>
          <button
            onClick={() => setShowCreate(true)}
            className="inline-flex items-center gap-2 rounded-xl bg-blue-600 px-5 py-2.5 text-sm font-semibold text-white hover:bg-blue-700 transition-colors shadow-sm"
          >
            <Plus className="h-4 w-4" />
            Create Portfolio
          </button>
        </section>

        {showCreate && (
          <CreatePortfolioModal
            onClose={() => setShowCreate(false)}
            onCreated={() => {
              setShowCreate(false);
              refetchPortfolio();
            }}
          />
        )}
      </>
    );
  }

  // ── API error (not 404) ────────────────────────────────────────────────────
  if (portfolioError && !noPortfolio) {
    return (
      <section className="rounded-2xl border border-rose-200 bg-rose-50 p-6 shadow-sm">
        <div className="flex items-center gap-3 text-rose-700">
          <AlertCircle className="h-4 w-4 flex-shrink-0" />
          <span className="text-sm">Failed to load paper trading portfolio.</span>
        </div>
      </section>
    );
  }

  if (!portfolio) return null;

  // ── Main render ────────────────────────────────────────────────────────────
  return (
    <>
      <section className="space-y-3">
        {/* Portfolio Summary Card with live WebSocket stats */}
        <PortfolioSummaryCard
          portfolio={portfolio}
          liveStats={wsConnected ? portfolioStats : undefined}
          onSettingsClick={() => {/* TODO: open settings modal */}}
        />

        {/* Positions Panel */}
        <div className="rounded-2xl border border-slate-200 bg-white shadow-sm">
          {/* Panel header */}
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
            <div className="flex items-center gap-2.5">
              <h3 className="text-sm font-semibold text-slate-900">Open Positions</h3>
              {positionsLoading ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-slate-400" />
              ) : (
                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-semibold text-slate-600">
                  {openPositions.length}
                </span>
              )}
            </div>

            {/* Feed status */}
            <div
              className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-wide ${
                wsConnected
                  ? "bg-emerald-100 text-emerald-700"
                  : wsState === "connecting"
                  ? "bg-amber-100 text-amber-700"
                  : "bg-slate-100 text-slate-500"
              }`}
            >
              {wsConnected ? (
                <Wifi className="h-3 w-3" />
              ) : (
                <WifiOff className="h-3 w-3" />
              )}
              {wsConnected ? "Live" : wsState === "connecting" ? "Connecting…" : "Offline"}
            </div>
          </div>

          {/* Empty state */}
          {!positionsLoading && openPositions.length === 0 && (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-slate-100">
                <ChevronDown className="h-5 w-5 text-slate-400" />
              </div>
              <p className="text-sm font-medium text-slate-600">No open positions</p>
              <p className="mt-0.5 text-xs text-slate-400">
                Place an order from a trade signal to start tracking.
              </p>
            </div>
          )}

          {/* Positions Table */}
          {openPositions.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] text-sm">
                <thead>
                  <tr className="bg-slate-50 text-xs uppercase tracking-wide text-slate-400">
                    <th className="px-3 py-2.5 text-left">Symbol</th>
                    <th className="px-3 py-2.5 text-left">Side</th>
                    <th className="px-3 py-2.5 text-right">Qty</th>
                    <th className="px-3 py-2.5 text-right">Avg Cost</th>
                    <th className="px-3 py-2.5 text-right">Last Price</th>
                    <th className="px-3 py-2.5 text-right">Unrealized P&L</th>
                    <th className="px-3 py-2.5 text-right">P&L %</th>
                    <th className="px-3 py-2.5 text-right">Stop Loss</th>
                    <th className="px-3 py-2.5 text-right">Target 1</th>
                    <th className="px-3 py-2.5 text-right"></th>
                  </tr>
                </thead>
                <tbody>
                  {openPositions.map((position) => (
                    <PositionRow
                      key={position.id}
                      position={position}
                      positionPnLMap={positionPnLMap}
                      onClose={handleCloseRequested}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Aggregate row */}
          {openPositions.length > 0 && (
            <div className="border-t border-slate-200 bg-slate-50 px-4 py-2.5 flex flex-wrap items-center justify-between gap-2 rounded-b-2xl text-xs">
              <span className="text-slate-500">
                {openPositions.length} position{openPositions.length !== 1 ? "s" : ""}
              </span>
              <div className="flex items-center gap-4">
                <span className="text-slate-500">
                  Total Unrealized:&nbsp;
                  <span
                    className={`font-semibold ${
                      (positionsData?.total_unrealized_pnl ?? 0) >= 0
                        ? "text-emerald-600"
                        : "text-rose-600"
                    }`}
                  >
                    {(positionsData?.total_unrealized_pnl ?? 0) >= 0 ? "+" : ""}
                    {new Intl.NumberFormat("en-IN", {
                      style: "currency",
                      currency: "INR",
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                    }).format(positionsData?.total_unrealized_pnl ?? 0)}
                  </span>
                </span>
              </div>
            </div>
          )}
        </div>
      </section>

      {/* Close Position Modal */}
      {closingPosition && (
        <ClosePositionModal
          position={closingPosition.position}
          livePrice={closingPosition.livePrice}
          onClose={() => setClosingPosition(null)}
          onClosed={handlePositionClosed}
        />
      )}
    </>
  );
}
