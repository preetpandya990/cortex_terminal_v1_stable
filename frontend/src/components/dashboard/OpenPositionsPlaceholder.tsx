"use client";

import { useEffect, useMemo, useState } from "react";
import { PlugZap, TrendingDown, TrendingUp } from "lucide-react";

const INR_FORMAT = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

type PositionSide = "Long" | "Short";
type PositionStatus = "Open" | "Partial";

type Position = {
  id: string;
  symbol: string;
  qty: number;
  side: PositionSide;
  status: PositionStatus;
  buyPrice: number;
  currentPrice: number;
  targetPrice: number;
  pnlPct: number;
};

type SeedPosition = Omit<Position, "currentPrice">;

const BASE_POSITIONS: SeedPosition[] = [
  { id: "p1", symbol: "RELIANCE", qty: 120, side: "Long", status: "Open", buyPrice: 2860.5, targetPrice: 2980, pnlPct: 1.42 },
  { id: "p2", symbol: "HDFCBANK", qty: 200, side: "Long", status: "Partial", buyPrice: 1712.3, targetPrice: 1768, pnlPct: 0.84 },
  { id: "p3", symbol: "TCS", qty: 80, side: "Short", status: "Open", buyPrice: 4220.0, targetPrice: 4090, pnlPct: -0.38 },
  { id: "p4", symbol: "INFY", qty: 160, side: "Long", status: "Open", buyPrice: 1864.4, targetPrice: 1935, pnlPct: 1.01 },
  { id: "p5", symbol: "ICICIBANK", qty: 240, side: "Long", status: "Partial", buyPrice: 1234.2, targetPrice: 1285, pnlPct: 0.56 },
  { id: "p6", symbol: "SBIN", qty: 300, side: "Short", status: "Open", buyPrice: 786.8, targetPrice: 760, pnlPct: 0.22 },
  { id: "p7", symbol: "LT", qty: 90, side: "Long", status: "Open", buyPrice: 3580.0, targetPrice: 3695, pnlPct: -0.14 },
  { id: "p8", symbol: "BAJFINANCE", qty: 70, side: "Long", status: "Partial", buyPrice: 6955.0, targetPrice: 7210, pnlPct: 0.73 },
];

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function stepPnl(value: number): number {
  const drift = (Math.random() - 0.5) * 0.22;
  return clamp(value + drift, -4.5, 5.5);
}

function deriveCurrentPrice(side: PositionSide, buyPrice: number, pnlPct: number): number {
  const move = pnlPct / 100;
  return side === "Long" ? buyPrice * (1 + move) : buyPrice * (1 - move);
}

function getStatusTone(status: PositionStatus): string {
  return status === "Open"
    ? "bg-emerald-100 text-emerald-700"
    : "bg-amber-100 text-amber-700";
}

export function OpenPositionsPlaceholder() {
  const [positions, setPositions] = useState<Position[]>(() =>
    BASE_POSITIONS.map((position) => ({
      ...position,
      currentPrice: deriveCurrentPrice(position.side, position.buyPrice, position.pnlPct),
    }))
  );
  const [feedState] = useState<"disconnected">("disconnected");

  useEffect(() => {
    const interval = window.setInterval(() => {
      setPositions((prev) =>
        prev.map((position) => {
          const pnlPct = stepPnl(position.pnlPct);
          return {
            ...position,
            pnlPct,
            currentPrice: deriveCurrentPrice(position.side, position.buyPrice, pnlPct),
          };
        })
      );
    }, 1000);

    return () => window.clearInterval(interval);
  }, []);

  const summary = useMemo(() => {
    const openCount = positions.length;
    const grossExposure = positions.reduce((sum, position) => sum + position.qty * position.buyPrice, 0);
    const avgPnlPct = positions.reduce((sum, position) => sum + position.pnlPct, 0) / openCount;

    return {
      openCount,
      grossExposure,
      avgPnlPct,
    };
  }, [positions]);

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold tracking-tight text-slate-900">Open Positions</h2>
          <p className="text-xs text-slate-500">Websocket-ready placeholder with mocked live P&amp;L ticks.</p>
        </div>
        <div className="inline-flex items-center gap-2 rounded-full bg-slate-100 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-slate-600">
          <PlugZap className="h-3.5 w-3.5" />
          Feed {feedState}
        </div>
      </div>

      <div className="mb-4 grid grid-cols-1 gap-2 sm:grid-cols-3">
        <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
          <div className="text-[11px] uppercase tracking-wide text-slate-500">Open Count</div>
          <div className="text-sm font-semibold text-slate-900">{summary.openCount}</div>
        </div>
        <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
          <div className="text-[11px] uppercase tracking-wide text-slate-500">Gross Exposure</div>
          <div className="text-sm font-semibold text-slate-900">{INR_FORMAT.format(summary.grossExposure)}</div>
        </div>
        <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2">
          <div className="text-[11px] uppercase tracking-wide text-slate-500">Average P&amp;L %</div>
          <div className={`text-sm font-semibold ${summary.avgPnlPct >= 0 ? "text-emerald-600" : "text-rose-600"}`}>
            {summary.avgPnlPct >= 0 ? "+" : ""}
            {summary.avgPnlPct.toFixed(2)}%
          </div>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border border-slate-200">
        <table className="w-full min-w-[820px] text-sm">
          <thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th className="px-3 py-2 text-left">Symbol</th>
              <th className="px-3 py-2 text-right">Qty</th>
              <th className="px-3 py-2 text-left">Side</th>
              <th className="px-3 py-2 text-left">Status</th>
              <th className="px-3 py-2 text-right">Buy Price</th>
              <th className="px-3 py-2 text-right">Current Price</th>
              <th className="px-3 py-2 text-right">Target Price</th>
              <th className="px-3 py-2 text-right">P&amp;L %</th>
            </tr>
          </thead>
          <tbody>
            {positions.map((position) => {
              const isPositive = position.pnlPct >= 0;
              return (
                <tr key={position.id} className="border-t border-slate-200">
                  <td className="px-3 py-2 font-semibold text-slate-900">{position.symbol}</td>
                  <td className="px-3 py-2 text-right text-slate-700">{position.qty}</td>
                  <td className="px-3 py-2">
                    <span
                      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                        position.side === "Long"
                          ? "bg-emerald-100 text-emerald-700"
                          : "bg-rose-100 text-rose-700"
                      }`}
                    >
                      {position.side === "Long" ? (
                        <TrendingUp className="h-3 w-3" />
                      ) : (
                        <TrendingDown className="h-3 w-3" />
                      )}
                      {position.side}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <span className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-semibold ${getStatusTone(position.status)}`}>
                      {position.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-right text-slate-700">{INR_FORMAT.format(position.buyPrice)}</td>
                  <td className="px-3 py-2 text-right text-slate-700">{INR_FORMAT.format(position.currentPrice)}</td>
                  <td className="px-3 py-2 text-right text-slate-700">{INR_FORMAT.format(position.targetPrice)}</td>
                  <td className={`px-3 py-2 text-right font-semibold ${isPositive ? "text-emerald-600" : "text-rose-600"}`}>
                    {isPositive ? "+" : ""}
                    {position.pnlPct.toFixed(2)}%
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
