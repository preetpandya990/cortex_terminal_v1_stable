"use client";

/**
 * AuditOutcomesTable
 * ====================
 * Cursor-paginated trade outcomes table for the admin audit page.
 * Shows every closed trade across all users with full ML feedback fields.
 */

import { useState } from "react";
import { format } from "date-fns";
import {
  CheckCircle,
  XCircle,
  ChevronRight,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { useOutcomes } from "@/hooks/usePaperTrading";
import type { PaperTradeOutcome } from "@/types/paper_trading";
import { EXIT_REASON_LABELS } from "@/types/paper_trading";

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function BoolBadge({ value }: { value: boolean | null }) {
  if (value === null || value === undefined)
    return <span className="text-slate-300">—</span>;
  return value ? (
    <CheckCircle className="h-4 w-4 text-emerald-500" />
  ) : (
    <XCircle className="h-4 w-4 text-slate-300" />
  );
}

function ExitReasonBadge({ reason }: { reason: PaperTradeOutcome["exit_reason"] }) {
  const isTP = reason.startsWith("TP");
  const isSL = reason === "SL";
  return (
    <span
      className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-semibold ${
        isTP
          ? "bg-emerald-100 text-emerald-700"
          : isSL
          ? "bg-rose-100 text-rose-700"
          : "bg-slate-100 text-slate-600"
      }`}
    >
      {EXIT_REASON_LABELS[reason] ?? reason}
    </span>
  );
}

export function AuditOutcomesTable() {
  const [cursor, setCursor] = useState<string | undefined>(undefined);
  const [history, setHistory] = useState<string[]>([]);

  const { data, isLoading, isRefetching, refetch } = useOutcomes({
    cursor,
    limit: 25,
  });

  const outcomes = data?.outcomes ?? [];

  function handleNext() {
    if (!data?.next_cursor) return;
    setHistory((h) => [...h, cursor ?? ""]);
    setCursor(data.next_cursor);
  }

  function handlePrev() {
    const prev = history[history.length - 1];
    setHistory((h) => h.slice(0, -1));
    setCursor(prev || undefined);
  }

  return (
    <div className="rounded-2xl border border-slate-200 bg-white shadow-sm">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
        <div className="flex items-center gap-2.5">
          <h3 className="text-sm font-bold text-slate-900">Trade Outcomes</h3>
          {data?.total != null && (
            <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-semibold text-slate-600">
              {data.total} total
            </span>
          )}
          {isRefetching && <Loader2 className="h-3.5 w-3.5 animate-spin text-slate-400" />}
        </div>
        <button
          onClick={() => refetch()}
          disabled={isRefetching}
          className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-50 transition disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isRefetching ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      {/* Loading */}
      {isLoading && (
        <div className="flex items-center justify-center py-12 text-slate-400">
          <Loader2 className="h-5 w-5 animate-spin" />
        </div>
      )}

      {/* Empty */}
      {!isLoading && outcomes.length === 0 && (
        <div className="py-12 text-center text-sm text-slate-400">
          No closed trades yet.
        </div>
      )}

      {/* Table */}
      {outcomes.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[1100px] text-sm">
            <thead>
              <tr className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-400">
                <th className="px-3 py-2.5 text-left">Symbol</th>
                <th className="px-3 py-2.5 text-left">Direction</th>
                <th className="px-3 py-2.5 text-right">Entry</th>
                <th className="px-3 py-2.5 text-right">Exit</th>
                <th className="px-3 py-2.5 text-right">Qty</th>
                <th className="px-3 py-2.5 text-right">Net P&L</th>
                <th className="px-3 py-2.5 text-right">P&L %</th>
                <th className="px-3 py-2.5 text-left">Exit Reason</th>
                <th className="px-3 py-2.5 text-center">ML Dir ✓</th>
                <th className="px-3 py-2.5 text-center">TP1</th>
                <th className="px-3 py-2.5 text-center">TP2</th>
                <th className="px-3 py-2.5 text-center">SL</th>
                <th className="px-3 py-2.5 text-left">Confidence</th>
                <th className="px-3 py-2.5 text-left">Regime</th>
                <th className="px-3 py-2.5 text-right">Hold</th>
                <th className="px-3 py-2.5 text-right">Closed</th>
              </tr>
            </thead>
            <tbody>
              {outcomes.map((o) => {
                const isPos = o.net_pnl >= 0;
                const holdHours = (o.hold_duration_seconds / 3600).toFixed(1);
                return (
                  <tr
                    key={o.id}
                    className="border-t border-slate-100 transition-colors hover:bg-slate-50/50"
                  >
                    <td className="px-3 py-2.5 font-semibold text-slate-900">
                      {o.symbol}
                    </td>
                    <td className="px-3 py-2.5">
                      <span
                        className={`inline-flex rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                          o.signal_direction === "BUY"
                            ? "bg-emerald-100 text-emerald-700"
                            : "bg-rose-100 text-rose-700"
                        }`}
                      >
                        {o.signal_direction}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-right text-slate-600">
                      {INR.format(o.entry_price)}
                    </td>
                    <td className="px-3 py-2.5 text-right text-slate-600">
                      {INR.format(o.exit_price)}
                    </td>
                    <td className="px-3 py-2.5 text-right text-slate-600">
                      {o.quantity.toLocaleString("en-IN")}
                    </td>
                    <td className={`px-3 py-2.5 text-right font-semibold ${isPos ? "text-emerald-600" : "text-rose-600"}`}>
                      {isPos ? "+" : ""}
                      {INR.format(o.net_pnl)}
                    </td>
                    <td className={`px-3 py-2.5 text-right ${isPos ? "text-emerald-600" : "text-rose-600"}`}>
                      {o.pnl_pct >= 0 ? "+" : ""}
                      {o.pnl_pct.toFixed(2)}%
                    </td>
                    <td className="px-3 py-2.5">
                      <ExitReasonBadge reason={o.exit_reason} />
                    </td>
                    <td className="px-3 py-2.5 text-center">
                      <BoolBadge value={o.ml_direction_correct} />
                    </td>
                    <td className="px-3 py-2.5 text-center">
                      <BoolBadge value={o.hit_tp1} />
                    </td>
                    <td className="px-3 py-2.5 text-center">
                      <BoolBadge value={o.hit_tp2} />
                    </td>
                    <td className="px-3 py-2.5 text-center">
                      <BoolBadge value={o.hit_sl} />
                    </td>
                    <td className="px-3 py-2.5 text-xs text-slate-500">
                      {o.suggestion_confidence_level ?? "—"}
                    </td>
                    <td className="px-3 py-2.5 text-xs text-slate-500">
                      {o.market_regime_at_entry ?? "—"}
                    </td>
                    <td className="px-3 py-2.5 text-right text-xs text-slate-500">
                      {holdHours}h
                    </td>
                    <td className="px-3 py-2.5 text-right text-xs text-slate-400">
                      {format(new Date(o.created_at), "MMM d, HH:mm")}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {(data?.has_more || history.length > 0) && (
        <div className="flex items-center justify-between border-t border-slate-200 px-4 py-3">
          <button
            onClick={handlePrev}
            disabled={history.length === 0}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-50 transition disabled:opacity-40"
          >
            ← Previous
          </button>
          <span className="text-xs text-slate-400">
            Page {history.length + 1}
          </span>
          <button
            onClick={handleNext}
            disabled={!data?.has_more}
            className="flex items-center gap-1 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-600 hover:bg-slate-50 transition disabled:opacity-40"
          >
            Next <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
    </div>
  );
}
