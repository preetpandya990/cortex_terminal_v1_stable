"use client";

import { X, TrendingUp, TrendingDown, Clock, Loader2, AlertCircle, Brain } from "lucide-react";
import { usePositionDetail } from "@/hooks/usePaperTrading";
import type { PaperFill, PaperPosition, PaperTradeOutcome } from "@/types/paper_trading";

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

// ── SL / TP Risk Gauge (LONG positions only) ─────────────────────────────────
// Renders a segmented progress bar: SL → Entry → TP1 → TP2 → TP3
// A blue needle marks the current live price.

function RiskGaugeLong({
  entry,
  sl,
  tp1,
  tp2,
  tp3,
  currentPrice,
}: {
  entry: number;
  sl: number;
  tp1: number;
  tp2: number | null;
  tp3: number | null;
  currentPrice: number | null;
}) {
  const max = tp3 ?? tp2 ?? tp1;
  const range = max - sl;
  if (range <= 0) return null;

  const pct = (v: number) =>
    Math.max(0, Math.min(100, ((v - sl) / range) * 100));

  const entryPct = pct(entry);
  const tp1Pct   = pct(tp1);
  const tp2Pct   = tp2 != null ? pct(tp2) : null;
  const tp3Pct   = tp3 != null ? pct(tp3) : null;
  const curPct   = currentPrice != null ? pct(currentPrice) : null;

  // Determine zone label
  type Zone = "danger" | "loss" | "breakeven" | "profit1" | "profit2";
  let zone: Zone = "breakeven";
  if (currentPrice != null) {
    if (currentPrice <= sl)       zone = "danger";
    else if (currentPrice < entry) zone = "loss";
    else if (currentPrice < tp1)  zone = "breakeven";
    else if (!tp2 || currentPrice < tp2) zone = "profit1";
    else zone = "profit2";
  }

  const ZONE_META: Record<Zone, { text: string; cls: string }> = {
    danger:    { text: "⚠ At / Below Stop Loss",        cls: "text-rose-600" },
    loss:      { text: "Below Entry — Holding at Loss", cls: "text-rose-500" },
    breakeven: { text: "Between Entry and TP1",          cls: "text-amber-600" },
    profit1:   { text: "✓ Past TP1 — In Profit",         cls: "text-emerald-600" },
    profit2:   { text: "✓ Past TP2 — Strong Profit",     cls: "text-emerald-700" },
  };

  const levels = [
    { label: "SL",    val: sl,   p: 0,       cls: "text-rose-600" },
    { label: "TP1",   val: tp1,  p: tp1Pct,  cls: "text-amber-600" },
    ...(tp2 != null && tp2Pct != null
      ? [{ label: "TP2", val: tp2, p: tp2Pct, cls: "text-emerald-600" }]
      : []),
    ...(tp3 != null && tp3Pct != null
      ? [{ label: "TP3", val: tp3, p: tp3Pct, cls: "text-emerald-700" }]
      : []),
  ];

  return (
    <div className="space-y-2">
      {currentPrice != null && (
        <div className={`text-[11px] font-semibold ${ZONE_META[zone].cls}`}>
          {ZONE_META[zone].text}
          <span className="ml-2 font-normal text-slate-500">
            LTP {INR.format(currentPrice)}
          </span>
        </div>
      )}

      {/* Segmented bar */}
      <div className="relative h-3 rounded-full overflow-hidden bg-slate-100">
        {/* SL → Entry: red */}
        <div
          className="absolute inset-y-0 bg-rose-300"
          style={{ left: 0, width: `${entryPct}%` }}
        />
        {/* Entry → TP1: amber */}
        <div
          className="absolute inset-y-0 bg-amber-200"
          style={{ left: `${entryPct}%`, width: `${tp1Pct - entryPct}%` }}
        />
        {/* TP1 → TP2: light green */}
        {tp2Pct != null && (
          <div
            className="absolute inset-y-0 bg-emerald-200"
            style={{ left: `${tp1Pct}%`, width: `${tp2Pct - tp1Pct}%` }}
          />
        )}
        {/* TP2 → TP3: green */}
        {tp2Pct != null && tp3Pct != null && (
          <div
            className="absolute inset-y-0 bg-emerald-400"
            style={{ left: `${tp2Pct}%`, width: `${tp3Pct - tp2Pct}%` }}
          />
        )}
        {/* Current price needle */}
        {curPct != null && (
          <div
            className="absolute top-0 bottom-0 w-[3px] bg-blue-600 shadow"
            style={{ left: `calc(${curPct}% - 1.5px)` }}
          />
        )}
      </div>

      {/* Tick labels — only outermost + TP1 to avoid crowding */}
      <div className="relative h-8 text-[10px] select-none">
        {levels.map(({ label, val, p, cls }) => (
          <div
            key={label}
            className="absolute flex flex-col items-center"
            style={{
              left: `${p}%`,
              transform: p === 0
                ? "none"
                : p >= 95
                ? "translateX(-100%)"
                : "translateX(-50%)",
            }}
          >
            <span className={`font-semibold ${cls}`}>{label}</span>
            <span className="text-slate-400">{INR.format(val)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Risk level grid (SHORT or positions without full SL/TP data) ──────────────

function RiskLevelGrid({ position }: { position: PaperPosition }) {
  const levels = [
    { label: "Stop Loss", value: position.stop_loss,       cls: "text-rose-600" },
    { label: "Target 1",  value: position.target_price_1,  cls: "text-amber-600" },
    { label: "Target 2",  value: position.target_price_2,  cls: "text-emerald-600" },
    { label: "Target 3",  value: position.target_price_3,  cls: "text-emerald-700" },
  ];

  return (
    <div className="grid grid-cols-4 gap-2">
      {levels.map(({ label, value, cls }) => (
        <div
          key={label}
          className="rounded-xl border border-slate-100 bg-slate-50 px-2 py-2.5 text-center"
        >
          <div className="text-[10px] text-slate-400">{label}</div>
          <div className={`mt-0.5 text-sm font-bold ${value != null ? cls : "text-slate-300"}`}>
            {value != null ? INR.format(value) : "—"}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Fills table ───────────────────────────────────────────────────────────────

function FillsTable({ fills }: { fills: PaperFill[] }) {
  if (!fills.length) {
    return (
      <p className="py-4 text-center text-xs text-slate-400">
        No fill records found.
      </p>
    );
  }

  return (
    <div className="overflow-x-auto rounded-xl border border-slate-200">
      <table className="w-full min-w-[560px] text-xs">
        <thead>
          <tr className="bg-slate-50 text-[11px] uppercase tracking-wide text-slate-400">
            <th className="px-3 py-2.5 text-right w-6">#</th>
            <th className="px-3 py-2.5 text-right">Qty</th>
            <th className="px-3 py-2.5 text-right">Fill Price</th>
            <th className="px-3 py-2.5 text-right">Slippage</th>
            <th className="px-3 py-2.5 text-right">Charges</th>
            <th className="px-3 py-2.5 text-right">Net Amount</th>
            <th className="px-3 py-2.5 text-left">Executed</th>
          </tr>
        </thead>
        <tbody>
          {fills.map((fill, i) => (
            <tr key={fill.id} className="border-t border-slate-100 hover:bg-slate-50/50">
              <td className="px-3 py-2 text-right text-slate-400">{i + 1}</td>
              <td className="px-3 py-2 text-right font-medium tabular-nums">
                {fill.fill_quantity.toLocaleString("en-IN")}
              </td>
              <td className="px-3 py-2 text-right tabular-nums">
                {INR.format(fill.fill_price)}
              </td>
              <td className="px-3 py-2 text-right text-slate-500">
                {fill.slippage_bps} bps
              </td>
              <td className="px-3 py-2 text-right text-slate-500 tabular-nums">
                {INR.format(fill.total_charges)}
              </td>
              <td
                className={`px-3 py-2 text-right font-semibold tabular-nums ${
                  fill.net_amount < 0 ? "text-rose-600" : "text-emerald-600"
                }`}
              >
                {fill.net_amount >= 0 ? "+" : ""}
                {INR.format(fill.net_amount)}
              </td>
              <td className="px-3 py-2 text-left text-slate-500">
                {fmtIST(fill.executed_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Trade Outcome panel (closed positions) ────────────────────────────────────

const EXIT_REASON_DISPLAY: Record<string, { label: string; cls: string }> = {
  TP1:     { label: "Target 1 Hit",   cls: "bg-amber-100 text-amber-700" },
  TP2:     { label: "Target 2 Hit",   cls: "bg-emerald-100 text-emerald-700" },
  TP3:     { label: "Target 3 Hit",   cls: "bg-emerald-100 text-emerald-800" },
  SL:      { label: "Stop Loss Hit",  cls: "bg-rose-100 text-rose-700" },
  MANUAL:  { label: "Manual Close",   cls: "bg-slate-100 text-slate-700" },
  EXPIRED: { label: "Signal Expired", cls: "bg-slate-100 text-slate-600" },
};

function OutcomePanel({ outcome }: { outcome: PaperTradeOutcome }) {
  const isWin = outcome.net_pnl >= 0;
  const er = EXIT_REASON_DISPLAY[outcome.exit_reason] ?? {
    label: outcome.exit_reason,
    cls: "bg-slate-100 text-slate-600",
  };

  return (
    <div className="space-y-3">
      <div
        className={`rounded-xl border px-4 py-3.5 ${
          isWin ? "border-emerald-200 bg-emerald-50" : "border-rose-200 bg-rose-50"
        }`}
      >
        <div className="flex items-center justify-between mb-3">
          <span
            className={`text-[11px] font-bold uppercase tracking-wide ${
              isWin ? "text-emerald-700" : "text-rose-700"
            }`}
          >
            {isWin ? "Winning Trade" : "Losing Trade"}
          </span>
          <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${er.cls}`}>
            {er.label}
          </span>
        </div>

        <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-xs">
          {(
            [
              ["Entry Price", INR.format(outcome.entry_price)],
              ["Exit Price",  INR.format(outcome.exit_price)],
              [
                "Gross P&L",
                `${outcome.gross_pnl >= 0 ? "+" : ""}${INR.format(outcome.gross_pnl)}`,
              ],
              ["Total Charges", INR.format(outcome.total_charges)],
            ] as [string, string][]
          ).map(([label, val]) => (
            <div key={label} className="flex justify-between gap-4">
              <span className="text-slate-500">{label}</span>
              <span className="font-medium">{val}</span>
            </div>
          ))}

          <div
            className={`col-span-2 flex justify-between gap-4 border-t pt-1.5 mt-0.5 font-bold ${
              isWin ? "border-emerald-200 text-emerald-700" : "border-rose-200 text-rose-700"
            }`}
          >
            <span>Net P&L</span>
            <span>
              {outcome.net_pnl >= 0 ? "+" : ""}
              {INR.format(outcome.net_pnl)}&nbsp;(
              {outcome.pnl_pct >= 0 ? "+" : ""}
              {outcome.pnl_pct.toFixed(2)}%)
            </span>
          </div>
        </div>
      </div>

      {/* ML signal accuracy */}
      {(outcome.ml_direction_correct !== null ||
        outcome.suggestion_confidence_level ||
        outcome.market_regime_at_entry) && (
        <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-2">
            Signal Accuracy
          </div>
          <div className="space-y-1.5 text-xs">
            {outcome.ml_direction_correct !== null && (
              <div className="flex justify-between">
                <span className="text-slate-500">ML Direction</span>
                <span
                  className={`font-semibold ${
                    outcome.ml_direction_correct ? "text-emerald-600" : "text-rose-600"
                  }`}
                >
                  {outcome.ml_direction_correct ? "✓ Correct" : "✗ Incorrect"}
                </span>
              </div>
            )}
            {outcome.suggestion_confidence_level && (
              <div className="flex justify-between">
                <span className="text-slate-500">Confidence</span>
                <span className="font-medium text-slate-700">
                  {outcome.suggestion_confidence_level}
                </span>
              </div>
            )}
            {outcome.market_regime_at_entry && (
              <div className="flex justify-between">
                <span className="text-slate-500">Regime at Entry</span>
                <span className="font-medium text-slate-700">
                  {outcome.market_regime_at_entry}
                </span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Public component ──────────────────────────────────────────────────────────

export interface PositionDetailModalProps {
  position: PaperPosition;
  /** Live price from the WebSocket at click time — used as initial value */
  lastPrice?: number | null;
  onClose: () => void;
  /** Called when user wants to close the position; parent handles the flow */
  onRequestClosePosition?: () => void;
}

export function PositionDetailModal({
  position,
  lastPrice,
  onClose,
  onRequestClosePosition,
}: PositionDetailModalProps) {
  const isOpen = position.status === "OPEN";
  const isLong = position.side === "LONG";

  const { data: detail, isLoading, isError } = usePositionDetail(position.id);

  const currentPrice =
    lastPrice ?? detail?.position.last_price ?? position.last_price;
  const displayPnl = detail?.position.unrealized_pnl ?? position.unrealized_pnl;
  const displayPct = detail?.position.pnl_pct     ?? position.pnl_pct;

  const showGauge =
    isOpen && isLong && position.stop_loss != null && position.target_price_1 != null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/50 backdrop-blur-sm p-0 sm:p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="relative w-full sm:max-w-2xl max-h-[92dvh] flex flex-col rounded-t-2xl sm:rounded-2xl border border-slate-200 bg-white shadow-2xl overflow-hidden">

        {/* ── Header ──────────────────────────────────────────────────────── */}
        <div className="flex items-center justify-between border-b border-slate-200 bg-white px-5 py-4 shrink-0">
          <div className="flex items-center gap-3">
            <div
              className={`flex h-9 w-9 items-center justify-center rounded-xl ${
                isLong ? "bg-emerald-100" : "bg-rose-100"
              }`}
            >
              {isLong
                ? <TrendingUp className="h-[18px] w-[18px] text-emerald-700" />
                : <TrendingDown className="h-[18px] w-[18px] text-rose-700" />}
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h2 className="text-base font-bold text-slate-900">
                  {position.symbol}
                </h2>
                <span
                  className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                    isLong
                      ? "bg-emerald-100 text-emerald-700"
                      : "bg-rose-100 text-rose-700"
                  }`}
                >
                  {isLong ? "Long" : "Short"}
                </span>
                <span
                  className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${
                    isOpen
                      ? "bg-blue-100 text-blue-700"
                      : "bg-slate-100 text-slate-600"
                  }`}
                >
                  {isOpen ? "Open" : "Closed"}
                </span>
              </div>
              <p className="mt-0.5 text-xs text-slate-500">
                Opened {fmtIST(position.opened_at)}
              </p>
            </div>
          </div>

          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition-colors"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* ── Scrollable body ─────────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4">

          {/* Key metrics */}
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {[
              {
                label: "Quantity",
                value: position.quantity > 0
                  ? position.quantity.toLocaleString("en-IN")
                  : "—",
                colored: false,
              },
              {
                label: "Avg Entry",
                value: INR.format(position.avg_cost_price),
                colored: false,
              },
              {
                label: isOpen ? "Last Price" : "Exit Price",
                value: isOpen
                  ? (currentPrice != null ? INR.format(currentPrice) : "—")
                  : (detail?.outcome ? INR.format(detail.outcome.exit_price) : "—"),
                colored: false,
              },
              {
                label: isOpen ? "Unrealized P&L" : "Realized P&L",
                value: isOpen
                  ? (displayPnl != null
                    ? `${displayPnl >= 0 ? "+" : ""}${INR.format(displayPnl)}`
                    : "—")
                  : `${position.realized_pnl >= 0 ? "+" : ""}${INR.format(position.realized_pnl)}`,
                sub: isOpen && displayPct != null
                  ? `${displayPct >= 0 ? "+" : ""}${displayPct.toFixed(2)}%`
                  : undefined,
                colored: true,
                positive: isOpen
                  ? (displayPnl ?? 0) >= 0
                  : position.realized_pnl >= 0,
              },
            ].map(({ label, value, sub, colored, positive }) => (
              <div
                key={label}
                className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2.5"
              >
                <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                  {label}
                </div>
                <div
                  className={`mt-1 text-sm font-bold leading-tight ${
                    colored
                      ? positive
                        ? "text-emerald-600"
                        : "text-rose-600"
                      : "text-slate-900"
                  }`}
                >
                  {value}
                  {sub && (
                    <span className="ml-1 text-[11px] font-semibold">{sub}</span>
                  )}
                </div>
              </div>
            ))}
          </div>

          {/* SL/TP Risk Gauge — LONG open positions with full data */}
          {showGauge && (
            <div className="rounded-xl border border-slate-200 bg-white px-4 pt-3 pb-5">
              <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400 mb-3">
                Risk Levels
              </div>
              <RiskGaugeLong
                entry={position.avg_cost_price}
                sl={position.stop_loss!}
                tp1={position.target_price_1!}
                tp2={position.target_price_2}
                tp3={position.target_price_3}
                currentPrice={currentPrice ?? null}
              />
            </div>
          )}

          {/* Level grid — always show for open positions (also supplements gauge) */}
          {isOpen && (
            <RiskLevelGrid position={position} />
          )}

          {/* Timeline */}
          <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-3">
            <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400 mb-2.5">
              Timeline
            </div>
            <div className="space-y-1.5 text-xs">
              <div className="flex items-center gap-2">
                <Clock className="h-3 w-3 text-slate-400 shrink-0" />
                <span className="text-slate-500">Opened:</span>
                <span className="font-medium text-slate-700">
                  {fmtIST(position.opened_at)}
                </span>
              </div>
              {position.closed_at && (
                <div className="flex items-center gap-2">
                  <Clock className="h-3 w-3 text-slate-400 shrink-0" />
                  <span className="text-slate-500">Closed:</span>
                  <span className="font-medium text-slate-700">
                    {fmtIST(position.closed_at)}
                  </span>
                </div>
              )}
              <div className="mt-1 flex items-start gap-2 rounded-lg border border-violet-200 bg-violet-50 px-3 py-2">
                <Brain className="h-3.5 w-3.5 text-violet-500 shrink-0 mt-0.5" />
                <span className="text-violet-700 text-[11px]">
                  <span className="font-semibold">Holding Time:</span>{" "}
                  Feature coming soon with next version of ML
                </span>
              </div>
            </div>
          </div>

          {/* Trade Outcome — closed positions */}
          {!isOpen && (
            <div>
              <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400 mb-2">
                Trade Outcome
              </div>
              {isLoading ? (
                <div className="flex items-center gap-2 text-sm text-slate-500">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Loading outcome…
                </div>
              ) : detail?.outcome ? (
                <OutcomePanel outcome={detail.outcome} />
              ) : (
                <p className="text-xs text-slate-400 py-2">
                  No outcome record found.
                </p>
              )}
            </div>
          )}

          {/* Fills */}
          <div>
            <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400 mb-2">
              Fills{detail ? ` (${detail.fill_count})` : ""}
            </div>
            {isLoading ? (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading fills…
              </div>
            ) : isError ? (
              <div className="flex items-center gap-2 text-xs text-rose-600">
                <AlertCircle className="h-4 w-4" />
                Failed to load fills.
              </div>
            ) : detail ? (
              <FillsTable fills={detail.fills} />
            ) : null}
          </div>
        </div>

        {/* ── Footer action — open positions only ─────────────────────────── */}
        {isOpen && onRequestClosePosition && (
          <div className="shrink-0 border-t border-slate-200 px-5 py-4">
            <button
              onClick={() => {
                onClose();
                onRequestClosePosition();
              }}
              className="w-full rounded-xl bg-rose-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-rose-700 active:bg-rose-800 transition-colors"
            >
              Close Position
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
