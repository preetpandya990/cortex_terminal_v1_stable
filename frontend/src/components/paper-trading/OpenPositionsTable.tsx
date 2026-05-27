"use client";

/**
 * OpenPositionsTable
 * ====================
 * Positions panel with Open / Closed tabs.
 *
 * Architecture:
 *  - Single `usePositions()` call (no status filter) — splits client-side.
 *  - Live P&L: usePnLWebSocket (500 ms frames). Rows poll the shared ref;
 *    closed positions have no live data.
 *  - Row click opens PositionDetailModal (fills, SL/TP gauge, outcome, timeline).
 *  - Close button on open rows triggers ClosePositionModal.
 *  - First visit (no portfolio): renders CreatePortfolioModal inline prompt.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertCircle,
  ChevronRight,
  Clock,
  Loader2,
  Plus,
  TrendingDown,
  TrendingUp,
  Wifi,
  WifiOff,
} from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { useToast } from "@/components/ui/toast";
import { usePortfolioSummary, usePositions, usePendingOrders } from "@/hooks/usePaperTrading";
import { usePnLWebSocket } from "@/hooks/usePnLWebSocket";
import { PortfolioSummaryCard } from "./PortfolioSummaryCard";
import { CreatePortfolioModal } from "./CreatePortfolioModal";
import { ClosePositionModal } from "./ClosePositionModal";
import { PositionDetailModal } from "./PositionDetailModal";
import { PortfolioSettingsModal } from "./PortfolioSettingsModal";
import PendingOrdersPanel from "./PendingOrdersPanel";
import type { LivePositionPnL, PaperPosition } from "@/types/paper_trading";

// ── Formatters ────────────────────────────────────────────────────────────────

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function fmtIST(iso: string): string {
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  }).format(new Date(iso));
}

// ── Open position row ─────────────────────────────────────────────────────────
// Polls the shared positionPnLMap ref at 500 ms — only this row re-renders on
// each tick, never the parent table.

interface OpenRowProps {
  position: PaperPosition;
  positionPnLMap: React.MutableRefObject<Map<string, LivePositionPnL>>;
  onClose: (position: PaperPosition, livePrice?: number) => void;
  onDetail: (position: PaperPosition, livePrice?: number) => void;
}

function OpenPositionRow({
  position,
  positionPnLMap,
  onClose,
  onDetail,
}: OpenRowProps) {
  const [livePnL, setLivePnL] = useState<LivePositionPnL | null>(null);

  useEffect(() => {
    const id = window.setInterval(() => {
      const tick = positionPnLMap.current.get(position.id);
      if (tick) setLivePnL(tick);
    }, 500);
    return () => window.clearInterval(id);
  }, [position.id, positionPnLMap]);

  const displayPnl = livePnL?.unrealized_pnl ?? position.unrealized_pnl;
  const displayPct = livePnL?.pnl_pct        ?? position.pnl_pct;
  const lastPrice  = livePnL?.last_price      ?? position.last_price;
  const isLong     = position.side === "LONG";
  const isPositive = (displayPnl ?? 0) >= 0;

  return (
    <tr
      className="border-t border-slate-100 transition-colors hover:bg-blue-50/40 cursor-pointer"
      onClick={() => onDetail(position, livePnL?.last_price)}
    >
      {/* Symbol */}
      <td className="px-3 py-3">
        <div className="font-semibold text-slate-900">{position.symbol}</div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide mt-0.5">
          NSE · EQ
        </div>
      </td>

      {/* Side */}
      <td className="px-3 py-3">
        <span
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${
            isLong
              ? "bg-emerald-100 text-emerald-700"
              : "bg-rose-100 text-rose-700"
          }`}
        >
          {isLong
            ? <TrendingUp className="h-3 w-3" />
            : <TrendingDown className="h-3 w-3" />}
          {isLong ? "Long" : "Short"}
        </span>
      </td>

      {/* Qty */}
      <td className="px-3 py-3 text-right font-medium text-slate-700 tabular-nums">
        {position.quantity.toLocaleString("en-IN")}
      </td>

      {/* Avg Cost */}
      <td className="px-3 py-3 text-right text-slate-600 tabular-nums text-sm">
        {INR.format(position.avg_cost_price)}
      </td>

      {/* Last Price */}
      <td className="px-3 py-3 text-right font-semibold text-slate-900 tabular-nums">
        {lastPrice != null ? INR.format(lastPrice) : "—"}
      </td>

      {/* Unrealized P&L */}
      <td
        className={`px-3 py-3 text-right tabular-nums ${
          isPositive ? "text-emerald-600" : "text-rose-600"
        }`}
      >
        {displayPnl != null ? (
          <>
            <div className="font-semibold">
              {isPositive ? "+" : ""}
              {INR.format(displayPnl)}
            </div>
            {displayPct != null && (
              <div className="text-[11px]">
                {displayPct >= 0 ? "+" : ""}
                {displayPct.toFixed(2)}%
              </div>
            )}
          </>
        ) : (
          "—"
        )}
      </td>

      {/* Stop Loss */}
      <td className="px-3 py-3 text-right text-xs tabular-nums">
        {position.stop_loss != null ? (
          <span className="rounded-md bg-rose-50 px-1.5 py-0.5 text-rose-700 font-medium">
            {INR.format(position.stop_loss)}
          </span>
        ) : (
          <span className="text-slate-300">—</span>
        )}
      </td>

      {/* TP1 */}
      <td className="px-3 py-3 text-right text-xs tabular-nums">
        {position.target_price_1 != null ? (
          <span className="text-amber-700 font-medium">
            {INR.format(position.target_price_1)}
          </span>
        ) : (
          <span className="text-slate-300">—</span>
        )}
      </td>

      {/* TP2 */}
      <td className="px-3 py-3 text-right text-xs tabular-nums">
        {position.target_price_2 != null ? (
          <span className="text-emerald-600 font-medium">
            {INR.format(position.target_price_2)}
          </span>
        ) : (
          <span className="text-slate-300">—</span>
        )}
      </td>

      {/* TP3 */}
      <td className="px-3 py-3 text-right text-xs tabular-nums">
        {position.target_price_3 != null ? (
          <span className="text-emerald-700 font-medium">
            {INR.format(position.target_price_3)}
          </span>
        ) : (
          <span className="text-slate-300">—</span>
        )}
      </td>

      {/* Charges */}
      <td className="px-3 py-3 text-right text-xs text-slate-400 tabular-nums">
        {INR.format(position.total_charges)}
      </td>

      {/* Actions */}
      <td className="px-3 py-3 text-right" onClick={(e) => e.stopPropagation()}>
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

// ── Closed position row ───────────────────────────────────────────────────────

function ClosedPositionRow({
  position,
  onClick,
}: {
  position: PaperPosition;
  onClick: (p: PaperPosition) => void;
}) {
  const isLong = position.side === "LONG";
  const isWin  = position.realized_pnl >= 0;

  return (
    <tr
      className="border-t border-slate-100 transition-colors hover:bg-blue-50/40 cursor-pointer"
      onClick={() => onClick(position)}
    >
      {/* Symbol */}
      <td className="px-3 py-3">
        <div className="font-semibold text-slate-900">{position.symbol}</div>
        <div className="text-[10px] text-slate-400 uppercase tracking-wide mt-0.5">
          NSE · EQ
        </div>
      </td>

      {/* Side */}
      <td className="px-3 py-3">
        <span
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${
            isLong
              ? "bg-emerald-100 text-emerald-700"
              : "bg-rose-100 text-rose-700"
          }`}
        >
          {isLong
            ? <TrendingUp className="h-3 w-3" />
            : <TrendingDown className="h-3 w-3" />}
          {isLong ? "Long" : "Short"}
        </span>
      </td>

      {/* Avg Entry */}
      <td className="px-3 py-3 text-right text-slate-600 tabular-nums text-sm">
        {INR.format(position.avg_cost_price)}
      </td>

      {/* Realized P&L */}
      <td
        className={`px-3 py-3 text-right font-semibold tabular-nums ${
          isWin ? "text-emerald-600" : "text-rose-600"
        }`}
      >
        {position.realized_pnl >= 0 ? "+" : ""}
        {INR.format(position.realized_pnl)}
      </td>

      {/* Charges */}
      <td className="px-3 py-3 text-right text-xs text-slate-400 tabular-nums">
        {INR.format(position.total_charges)}
      </td>

      {/* Closed At */}
      <td className="px-3 py-3 text-right text-xs text-slate-500">
        {position.closed_at ? fmtIST(position.closed_at) : "—"}
      </td>

      {/* Drill-down caret */}
      <td className="px-3 py-3 text-right">
        <ChevronRight className="h-4 w-4 text-slate-300 ml-auto" />
      </td>
    </tr>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

type ActiveTab = "open" | "closed" | "pending";

export function OpenPositionsTable() {
  const { accessToken, isAuthenticated, isAuthReady } = useAuth();
  const toast = useToast();

  const [activeTab,         setActiveTab]         = useState<ActiveTab>("open");
  const [showCreate,        setShowCreate]         = useState(false);
  const [showSettings,      setShowSettings]       = useState(false);
  const [closingPosition,   setClosingPosition]    = useState<{
    position: PaperPosition;
    livePrice?: number;
  } | null>(null);
  const [detailPosition,    setDetailPosition]     = useState<{
    position: PaperPosition;
    lastPrice?: number;
  } | null>(null);

  // ── Data ──────────────────────────────────────────────────────────────────

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

  // Single call — no status filter — returns all positions.
  // split client-side to avoid two round-trips.
  const {
    data: allPositionsData,
    isLoading: positionsLoading,
    refetch: refetchPositions,
  } = usePositions(undefined, !!portfolio?.id);

  const openPositions   = allPositionsData?.positions.filter((p) => p.status === "OPEN")   ?? [];
  const closedPositions = allPositionsData?.positions.filter((p) => p.status === "CLOSED") ?? [];

  const { data: pendingOrdersData } = usePendingOrders();
  const pendingCount = pendingOrdersData?.orders?.length ?? portfolio?.pending_order_count ?? 0;

  // ── WebSocket live P&L ────────────────────────────────────────────────────

  const {
    isConnected: wsConnected,
    connectionState: wsState,
    positionPnLMap,
    portfolioStats,
    onOrderFilled,
    onOrderExpired,
  } = usePnLWebSocket(
    portfolio?.id,
    accessToken,
    isAuthReady && isAuthenticated && !!portfolio?.id,
  );

  // Attach WS order lifecycle callbacks — stable ref assignment, no re-render.
  useEffect(() => {
    onOrderFilled.current = (event) => {
      const side = event.transaction_type === "BUY" ? "Buy" : "Sell";
      const price = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", minimumFractionDigits: 2 }).format(event.fill_price);
      toast.success(
        `${side} order filled`,
        `${event.quantity} × ${event.symbol} at ${price}`,
      );
    };
    onOrderExpired.current = (event) => {
      toast.warning(`Order expired`, `DAY order for ${event.symbol} was cancelled at market close (15:30 IST).`);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Callbacks ─────────────────────────────────────────────────────────────

  const handleCloseRequested = useCallback(
    (position: PaperPosition, livePrice?: number) => {
      setClosingPosition({ position, livePrice });
    },
    [],
  );

  const handleDetailRequested = useCallback(
    (position: PaperPosition, lastPrice?: number) => {
      setDetailPosition({ position, lastPrice });
    },
    [],
  );

  const handlePositionClosed = useCallback(() => {
    setClosingPosition(null);
    refetchPositions();
    refetchPortfolio();
  }, [refetchPositions, refetchPortfolio]);

  // When user clicks "Close Position" inside the detail modal, open close modal
  const handleRequestCloseFromDetail = useCallback(() => {
    if (!detailPosition) return;
    const livePrice = positionPnLMap.current.get(detailPosition.position.id)?.last_price;
    setDetailPosition(null);
    setClosingPosition({ position: detailPosition.position, livePrice });
  }, [detailPosition, positionPnLMap]);

  // ── Loading ───────────────────────────────────────────────────────────────

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

  // ── No portfolio ──────────────────────────────────────────────────────────

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

  // ── API error (non-404) ───────────────────────────────────────────────────

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

  // ── Derived tab stats ─────────────────────────────────────────────────────

  const activePositions = activeTab === "open" ? openPositions : closedPositions;
  const totalUnrealized = allPositionsData?.total_unrealized_pnl ?? 0;
  const totalRealized   = closedPositions.reduce((s, p) => s + p.realized_pnl, 0);

  // ── Main render ───────────────────────────────────────────────────────────

  return (
    <>
      <section className="space-y-3">
        {/* Portfolio Summary Card */}
        <PortfolioSummaryCard
          portfolio={portfolio}
          liveStats={wsConnected ? portfolioStats : undefined}
          onSettingsClick={() => setShowSettings(true)}
        />

        {/* Positions Panel */}
        <div className="rounded-2xl border border-slate-200 bg-white shadow-sm">

          {/* Panel header with tabs */}
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
            {/* Tabs */}
            <div className="flex items-center gap-1 rounded-xl bg-slate-100 p-1">
              <TabButton
                active={activeTab === "open"}
                label="Open"
                count={openPositions.length}
                loading={positionsLoading}
                onClick={() => setActiveTab("open")}
              />
              <TabButton
                active={activeTab === "pending"}
                label="Pending"
                count={pendingCount}
                loading={false}
                onClick={() => setActiveTab("pending")}
                accent={pendingCount > 0}
              />
              <TabButton
                active={activeTab === "closed"}
                label="Closed"
                count={closedPositions.length}
                loading={positionsLoading}
                onClick={() => setActiveTab("closed")}
              />
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
              {wsConnected
                ? <Wifi className="h-3 w-3" />
                : <WifiOff className="h-3 w-3" />}
              {wsConnected
                ? "Live"
                : wsState === "connecting"
                ? "Connecting…"
                : "Offline"}
            </div>
          </div>

          {/* ── Pending Orders Panel ──────────────────────────────────── */}
          {activeTab === "pending" && (
            <div className="p-4">
              <PendingOrdersPanel
                onCancelSuccess={(symbol) =>
                  toast.success("Order cancelled", `Pending order for ${symbol} has been cancelled.`)
                }
                onCancelError={(err) =>
                  toast.error(err.message ?? "Failed to cancel order.")
                }
              />
            </div>
          )}

          {/* Empty state — only shown for open / closed tabs */}
          {activeTab !== "pending" && !positionsLoading && activePositions.length === 0 && (
            <div className="flex flex-col items-center justify-center py-14 text-center">
              <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-slate-100">
                <Activity className="h-5 w-5 text-slate-400" />
              </div>
              <p className="text-sm font-medium text-slate-600">
                No {activeTab} positions
              </p>
              <p className="mt-0.5 text-xs text-slate-400">
                {activeTab === "open"
                  ? "Place an order from a trade signal to start tracking."
                  : "Closed positions will appear here after you close a trade."}
              </p>
            </div>
          )}

          {/* ── Open Positions Table ───────────────────────────────────── */}
          {activeTab === "open" && openPositions.length > 0 && !positionsLoading && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[1120px] text-sm">
                <thead>
                  <tr className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-400">
                    <th className="px-3 py-2.5 text-left">Symbol</th>
                    <th className="px-3 py-2.5 text-left">Side</th>
                    <th className="px-3 py-2.5 text-right">Qty</th>
                    <th className="px-3 py-2.5 text-right">Avg Cost</th>
                    <th className="px-3 py-2.5 text-right">Last Price</th>
                    <th className="px-3 py-2.5 text-right">P&L</th>
                    <th className="px-3 py-2.5 text-right">Stop Loss</th>
                    <th className="px-3 py-2.5 text-right">TP1</th>
                    <th className="px-3 py-2.5 text-right">TP2</th>
                    <th className="px-3 py-2.5 text-right">TP3</th>
                    <th className="px-3 py-2.5 text-right">Charges</th>
                    <th className="px-3 py-2.5 text-right"></th>
                  </tr>
                </thead>
                <tbody>
                  {openPositions.map((position) => (
                    <OpenPositionRow
                      key={position.id}
                      position={position}
                      positionPnLMap={positionPnLMap}
                      onClose={handleCloseRequested}
                      onDetail={handleDetailRequested}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* ── Closed Positions Table ─────────────────────────────────── */}
          {activeTab === "closed" && closedPositions.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[700px] text-sm">
                <thead>
                  <tr className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-400">
                    <th className="px-3 py-2.5 text-left">Symbol</th>
                    <th className="px-3 py-2.5 text-left">Side</th>
                    <th className="px-3 py-2.5 text-right">Avg Entry</th>
                    <th className="px-3 py-2.5 text-right">Realized P&L</th>
                    <th className="px-3 py-2.5 text-right">Charges</th>
                    <th className="px-3 py-2.5 text-right">Closed</th>
                    <th className="px-3 py-2.5 text-right"></th>
                  </tr>
                </thead>
                <tbody>
                  {closedPositions.map((position) => (
                    <ClosedPositionRow
                      key={position.id}
                      position={position}
                      onClick={(p) => handleDetailRequested(p)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Aggregate footer — only for open / closed position tabs */}
          {activeTab !== "pending" && activePositions.length > 0 && (
            <div className="border-t border-slate-200 bg-slate-50 px-4 py-2.5 flex flex-wrap items-center justify-between gap-2 rounded-b-2xl text-xs">
              <span className="text-slate-500">
                {activePositions.length}{" "}
                {activeTab === "open" ? "open" : "closed"}{" "}
                position{activePositions.length !== 1 ? "s" : ""}
              </span>
              <div className="flex items-center gap-4">
                {activeTab === "open" && (
                  <span className="text-slate-500">
                    Total Unrealized:&nbsp;
                    <span
                      className={`font-semibold ${
                        totalUnrealized >= 0 ? "text-emerald-600" : "text-rose-600"
                      }`}
                    >
                      {totalUnrealized >= 0 ? "+" : ""}
                      {INR.format(totalUnrealized)}
                    </span>
                  </span>
                )}
                {activeTab === "closed" && (
                  <span className="text-slate-500">
                    Total Realized:&nbsp;
                    <span
                      className={`font-semibold ${
                        totalRealized >= 0 ? "text-emerald-600" : "text-rose-600"
                      }`}
                    >
                      {totalRealized >= 0 ? "+" : ""}
                      {INR.format(totalRealized)}
                    </span>
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      </section>

      {/* Portfolio Settings Modal */}
      {showSettings && (
        <PortfolioSettingsModal
          portfolio={portfolio}
          onClose={() => setShowSettings(false)}
          onUpdated={() => {
            setShowSettings(false);
            refetchPortfolio();
          }}
        />
      )}

      {/* Position Detail Modal */}
      {detailPosition && (
        <PositionDetailModal
          position={detailPosition.position}
          lastPrice={detailPosition.lastPrice}
          onClose={() => setDetailPosition(null)}
          onRequestClosePosition={
            detailPosition.position.status === "OPEN"
              ? handleRequestCloseFromDetail
              : undefined
          }
        />
      )}

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

// ── Tab button ────────────────────────────────────────────────────────────────

function TabButton({
  active,
  label,
  count,
  loading,
  onClick,
  accent = false,
}: {
  active: boolean;
  label: string;
  count: number;
  loading: boolean;
  onClick: () => void;
  /** When true, renders an amber badge to draw attention to pending orders. */
  accent?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-semibold transition-all ${
        active
          ? "bg-white text-slate-900 shadow-sm"
          : "text-slate-500 hover:text-slate-700"
      }`}
    >
      {label}
      {loading ? (
        <Loader2 className="h-3 w-3 animate-spin text-slate-400" />
      ) : (
        <span
          className={`rounded-full px-1.5 py-0.5 text-[10px] font-bold ${
            accent && count > 0
              ? active
                ? "bg-amber-100 text-amber-700"
                : "bg-amber-200 text-amber-700"
              : active
              ? "bg-slate-100 text-slate-600"
              : "bg-slate-200 text-slate-500"
          }`}
        >
          {count}
        </span>
      )}
    </button>
  );
}
