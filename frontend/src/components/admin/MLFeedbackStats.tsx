"use client";

/**
 * MLFeedbackStats
 * ================
 * Renders the OutcomeStatsResponse for the admin audit page.
 * Designed for ML fine-tuning analysis — highlights direction accuracy,
 * TP/SL hit rates, and per-segment breakdowns.
 */

import { RefreshCw, TrendingUp, TrendingDown, Target, Brain, AlertTriangle } from "lucide-react";
import type { OutcomeStatsResponse, OutcomeStatsBreakdown } from "@/types/paper_trading";

interface Props {
  stats: OutcomeStatsResponse;
  onRefresh?: () => void;
  isRefreshing?: boolean;
}

const PCT = (v: number | null, decimals = 1) =>
  v != null ? `${(v * 100).toFixed(decimals)}%` : "—";

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function MetricCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string;
  sub?: string;
  accent?: "green" | "red" | "blue" | "amber" | "neutral";
}) {
  const valueColor =
    accent === "green"
      ? "text-emerald-600"
      : accent === "red"
      ? "text-rose-600"
      : accent === "blue"
      ? "text-blue-600"
      : accent === "amber"
      ? "text-amber-600"
      : "text-slate-900";

  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-1 text-xl font-bold leading-tight ${valueColor}`}>{value}</div>
      {sub && <div className="mt-0.5 text-[11px] text-slate-400">{sub}</div>}
    </div>
  );
}

function SegmentTable({
  title,
  data,
}: {
  title: string;
  data: Record<string, OutcomeStatsBreakdown>;
}) {
  const entries = Object.entries(data);
  if (entries.length === 0) return null;

  return (
    <div className="rounded-xl border border-slate-200 bg-white shadow-sm overflow-hidden">
      <div className="border-b border-slate-100 bg-slate-50 px-4 py-2.5">
        <h4 className="text-xs font-bold uppercase tracking-wide text-slate-500">{title}</h4>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[11px] uppercase tracking-wide text-slate-400 bg-slate-50/50">
              <th className="px-4 py-2 text-left">Segment</th>
              <th className="px-4 py-2 text-right">Trades</th>
              <th className="px-4 py-2 text-right">Win Rate</th>
              <th className="px-4 py-2 text-right">Avg P&L</th>
              <th className="px-4 py-2 text-right">Avg P&L %</th>
              <th className="px-4 py-2 text-right">ML Accuracy</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([key, seg]) => (
              <tr key={key} className="border-t border-slate-100 hover:bg-slate-50/50">
                <td className="px-4 py-2.5 font-semibold text-slate-800">{key}</td>
                <td className="px-4 py-2.5 text-right text-slate-600">{seg.count}</td>
                <td className={`px-4 py-2.5 text-right font-medium ${
                  seg.win_rate != null && seg.win_rate >= 50 ? "text-emerald-600" : "text-rose-600"
                }`}>
                  {seg.win_rate != null ? `${seg.win_rate.toFixed(1)}%` : "—"}
                </td>
                <td className={`px-4 py-2.5 text-right ${
                  (seg.avg_net_pnl ?? 0) >= 0 ? "text-emerald-600" : "text-rose-600"
                }`}>
                  {seg.avg_net_pnl != null ? INR.format(seg.avg_net_pnl) : "—"}
                </td>
                <td className={`px-4 py-2.5 text-right ${
                  (seg.avg_pnl_pct ?? 0) >= 0 ? "text-emerald-600" : "text-rose-600"
                }`}>
                  {seg.avg_pnl_pct != null ? `${seg.avg_pnl_pct >= 0 ? "+" : ""}${seg.avg_pnl_pct.toFixed(2)}%` : "—"}
                </td>
                <td className={`px-4 py-2.5 text-right font-medium ${
                  seg.ml_direction_accuracy != null && seg.ml_direction_accuracy >= 0.6
                    ? "text-emerald-600"
                    : "text-amber-600"
                }`}>
                  {PCT(seg.ml_direction_accuracy)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function HitRateBar({ label, rate, color }: { label: string; rate: number | null; color: string }) {
  const pct = rate != null ? rate * 100 : 0;
  return (
    <div>
      <div className="mb-1 flex items-center justify-between text-xs">
        <span className="font-medium text-slate-600">{label}</span>
        <span className="font-bold text-slate-900">{rate != null ? `${pct.toFixed(1)}%` : "—"}</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full transition-all ${color}`}
          style={{ width: `${Math.min(pct, 100)}%` }}
        />
      </div>
    </div>
  );
}

export function MLFeedbackStats({ stats, onRefresh, isRefreshing }: Props) {
  const netPnlPositive = stats.total_net_pnl >= 0;

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-slate-900">ML Feedback Analysis</h2>
          <p className="text-xs text-slate-500">
            Platform-wide · {stats.total_trades} closed trades
            {stats.from_date && stats.to_date
              ? ` · ${stats.from_date.slice(0, 10)} → ${stats.to_date.slice(0, 10)}`
              : ""}
          </p>
        </div>
        {onRefresh && (
          <button
            onClick={onRefresh}
            disabled={isRefreshing}
            className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-50 transition disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isRefreshing ? "animate-spin" : ""}`} />
            Refresh
          </button>
        )}
      </div>

      {/* Overview metrics */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <MetricCard
          label="Total Trades"
          value={String(stats.total_trades)}
          sub={`${stats.total_winners}W / ${stats.total_losers}L`}
          accent="neutral"
        />
        <MetricCard
          label="Win Rate"
          value={stats.win_rate != null ? `${stats.win_rate.toFixed(1)}%` : "—"}
          sub="of all closed trades"
          accent={stats.win_rate != null ? (stats.win_rate >= 50 ? "green" : "red") : "neutral"}
        />
        <MetricCard
          label="Total Net P&L"
          value={INR.format(stats.total_net_pnl)}
          sub={`charges: ${INR.format(stats.total_charges)}`}
          accent={netPnlPositive ? "green" : "red"}
        />
        <MetricCard
          label="Avg P&L / Trade"
          value={stats.avg_net_pnl_per_trade != null ? INR.format(stats.avg_net_pnl_per_trade) : "—"}
          sub={stats.avg_pnl_pct != null ? `${stats.avg_pnl_pct >= 0 ? "+" : ""}${stats.avg_pnl_pct.toFixed(2)}%` : ""}
          accent={
            stats.avg_net_pnl_per_trade != null
              ? stats.avg_net_pnl_per_trade >= 0 ? "green" : "red"
              : "neutral"
          }
        />
      </div>

      {/* ML accuracy + TP/SL hit rates */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        {/* ML Direction Accuracy */}
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="mb-3 flex items-center gap-2">
            <Brain className="h-4 w-4 text-blue-500" />
            <h3 className="text-sm font-bold text-slate-900">ML Direction Accuracy</h3>
          </div>
          {stats.ml_direction_accuracy != null ? (
            <>
              <div className="mb-2 text-3xl font-black text-slate-900">
                {PCT(stats.ml_direction_accuracy)}
              </div>
              <div className="h-3 overflow-hidden rounded-full bg-slate-100">
                <div
                  className={`h-full rounded-full ${
                    stats.ml_direction_accuracy >= 0.6 ? "bg-emerald-500" : "bg-amber-500"
                  }`}
                  style={{ width: `${stats.ml_direction_accuracy * 100}%` }}
                />
              </div>
              <p className="mt-2 text-[11px] text-slate-400">
                {stats.ml_direction_accuracy >= 0.6
                  ? "Model is directionally accurate."
                  : "Below 60% — consider retraining."}
              </p>
            </>
          ) : (
            <p className="text-sm text-slate-400">Insufficient data.</p>
          )}
        </div>

        {/* Target / Stop Hit Rates */}
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="mb-3 flex items-center gap-2">
            <Target className="h-4 w-4 text-emerald-500" />
            <h3 className="text-sm font-bold text-slate-900">Target &amp; Stop Hit Rates</h3>
          </div>
          <div className="space-y-2.5">
            <HitRateBar label="TP1 Hit" rate={stats.tp1_hit_rate} color="bg-emerald-500" />
            <HitRateBar label="TP2 Hit" rate={stats.tp2_hit_rate} color="bg-emerald-400" />
            <HitRateBar label="TP3 Hit" rate={stats.tp3_hit_rate} color="bg-emerald-300" />
            <HitRateBar label="SL Hit" rate={stats.sl_hit_rate} color="bg-rose-500" />
          </div>
        </div>
      </div>

      {/* Exit reason distribution */}
      {Object.keys(stats.by_exit_reason).length > 0 && (
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <h3 className="mb-3 text-sm font-bold text-slate-900">Exit Reason Distribution</h3>
          <div className="flex flex-wrap gap-2">
            {Object.entries(stats.by_exit_reason)
              .sort((a, b) => b[1] - a[1])
              .map(([reason, count]) => {
                const pct = stats.total_trades > 0
                  ? ((count / stats.total_trades) * 100).toFixed(1)
                  : "0.0";
                const isTP = reason.startsWith("TP");
                const isSL = reason === "SL";
                return (
                  <div
                    key={reason}
                    className={`rounded-lg border px-3 py-2 text-center ${
                      isTP
                        ? "border-emerald-200 bg-emerald-50"
                        : isSL
                        ? "border-rose-200 bg-rose-50"
                        : "border-slate-200 bg-slate-50"
                    }`}
                  >
                    <div className={`text-xs font-bold uppercase tracking-wide ${
                      isTP ? "text-emerald-700" : isSL ? "text-rose-700" : "text-slate-600"
                    }`}>
                      {reason}
                    </div>
                    <div className="text-lg font-black text-slate-900">{count}</div>
                    <div className="text-[10px] text-slate-400">{pct}%</div>
                  </div>
                );
              })}
          </div>
        </div>
      )}

      {/* Segmented breakdowns */}
      <SegmentTable title="By Confidence Level" data={stats.by_confidence_level} />
      <SegmentTable title="By Market Regime" data={stats.by_market_regime} />
      <SegmentTable title="By Signal Direction" data={stats.by_signal_direction} />
    </div>
  );
}
