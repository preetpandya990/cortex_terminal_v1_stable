"use client";

import { Settings, TrendingUp, TrendingDown, Wallet } from "lucide-react";
import type { PortfolioSummary } from "@/types/paper_trading";
import type { PnLPortfolioStats } from "@/hooks/usePnLWebSocket";

interface Props {
  portfolio: PortfolioSummary;
  liveStats?: PnLPortfolioStats;
  onSettingsClick?: () => void;
}

const INR_FORMAT = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

const INR_PRECISE = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function StatTile({
  label,
  value,
  sub,
  positive,
  neutral,
}: {
  label: string;
  value: string;
  sub?: string;
  positive?: boolean;
  neutral?: boolean;
}) {
  const valueColor = neutral
    ? "text-slate-900"
    : positive === true
    ? "text-emerald-600"
    : positive === false
    ? "text-rose-600"
    : "text-slate-900";

  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-1 text-lg font-bold ${valueColor} leading-tight`}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-slate-400">{sub}</div>}
    </div>
  );
}

export function PortfolioSummaryCard({ portfolio, liveStats, onSettingsClick }: Props) {
  // Prefer live WebSocket stats, fall back to REST response
  const portfolioValue = liveStats?.portfolio_value ?? portfolio.portfolio_value;
  const totalReturn = liveStats?.total_return_pct ?? portfolio.total_return_pct;
  const unrealizedPnl = liveStats?.total_unrealized_pnl ?? portfolio.total_unrealized_pnl;
  const realizedPnl = liveStats?.total_realized_pnl ?? portfolio.total_realized_pnl;
  const currentCash = liveStats?.current_cash ?? portfolio.current_cash;

  const isPositive = totalReturn >= 0;

  return (
    <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4 shadow-sm">
      {/* Header Row */}
      <div className="mb-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-blue-600 shadow-sm">
            <Wallet className="h-[18px] w-[18px] text-white" />
          </div>
          <div>
            <h2 className="text-base font-semibold text-slate-900">{portfolio.name}</h2>
            <p className="text-xs text-slate-500">
              Paper · {portfolio.currency} · Risk {portfolio.risk_per_trade_pct}%/trade · max{" "}
              {portfolio.max_open_positions} positions
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2">
          <div
            className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-sm font-bold ${
              isPositive ? "bg-emerald-100 text-emerald-700" : "bg-rose-100 text-rose-700"
            }`}
          >
            {isPositive ? (
              <TrendingUp className="h-3.5 w-3.5" />
            ) : (
              <TrendingDown className="h-3.5 w-3.5" />
            )}
            {isPositive ? "+" : ""}
            {totalReturn.toFixed(2)}%
          </div>

          {onSettingsClick && (
            <button
              onClick={onSettingsClick}
              className="rounded-lg p-2 text-slate-400 hover:bg-slate-200 hover:text-slate-600 transition-colors"
              aria-label="Portfolio settings"
            >
              <Settings className="h-4 w-4" />
            </button>
          )}
        </div>
      </div>

      {/* Stat Grid */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <StatTile
          label="Portfolio Value"
          value={INR_FORMAT.format(portfolioValue)}
          sub={`of ${INR_FORMAT.format(portfolio.initial_capital)} initial`}
          neutral
        />
        <StatTile
          label="Cash Available"
          value={INR_FORMAT.format(currentCash)}
          sub={`${((currentCash / portfolio.initial_capital) * 100).toFixed(1)}% of capital`}
          neutral
        />
        <StatTile
          label="Unrealized P&L"
          value={INR_PRECISE.format(unrealizedPnl)}
          positive={unrealizedPnl >= 0}
        />
        <StatTile
          label="Realized P&L"
          value={INR_PRECISE.format(realizedPnl)}
          positive={realizedPnl >= 0}
        />
        <StatTile
          label="Open Positions"
          value={String(portfolio.open_position_count)}
          sub={`of ${portfolio.max_open_positions} max`}
          neutral
        />
        <StatTile
          label="Win Rate"
          value={portfolio.win_rate != null ? `${portfolio.win_rate.toFixed(1)}%` : "—"}
          sub={`${portfolio.total_trades} closed trades`}
          positive={portfolio.win_rate != null ? portfolio.win_rate >= 50 : undefined}
        />
      </div>
    </div>
  );
}
