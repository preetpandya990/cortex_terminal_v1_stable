"use client";

import { memo } from "react";

// ─── Formatters ───────────────────────────────────────────────────────────────

const INR_PRICE = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const INR_CHANGE = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

// ─── Types ────────────────────────────────────────────────────────────────────

export interface LivePriceBadgeProps {
  /** Current LTP or live price. Null while loading or unavailable. */
  price: number | null;
  /** Previous session close for computing day change. Omit when unknown. */
  prevClose?: number | null;
  /** True only when WS is confirmed live AND the market session is open. */
  isLive: boolean;
  /** True while WS is connecting/reconnecting and no price is available yet. */
  isConnecting?: boolean;
  /**
   * "end" — right-aligns all children; use when the badge sits as a standalone
   *   block between the company name and action buttons (DetailPane).
   * "start" — left-aligns; use when inline inside a flex-wrap with other text.
   */
  align?: "start" | "end";
  className?: string;
}

// ─── Component ────────────────────────────────────────────────────────────────

export const LivePriceBadge = memo(function LivePriceBadge({
  price,
  prevClose,
  isLive,
  isConnecting = false,
  align = "start",
  className,
}: LivePriceBadgeProps) {
  const change =
    price != null && prevClose != null && prevClose !== 0
      ? price - prevClose
      : null;
  const changePct =
    change != null && prevClose != null && prevClose !== 0
      ? (change / prevClose) * 100
      : null;

  const isUp   = change != null && change > 0;
  const isDown = change != null && change < 0;

  const priceColor = isUp
    ? "text-emerald-600"
    : isDown
    ? "text-red-500"
    : "text-slate-800";

  const alignCls = align === "end" ? "items-end" : "items-start";

  // ── Skeleton ─────────────────────────────────────────────────────────────

  if (price === null && isConnecting) {
    return (
      <div
        className={`flex flex-col gap-1.5 ${alignCls} ${className ?? ""}`}
        aria-label="Loading price"
        aria-busy="true"
      >
        <div className="h-6 w-28 animate-pulse rounded-lg bg-slate-100" />
        <div className="h-4 w-20 animate-pulse rounded-full bg-slate-100" />
      </div>
    );
  }

  if (price === null) return null;

  // ── Price display ─────────────────────────────────────────────────────────

  return (
    <div className={`flex flex-col gap-1.5 ${alignCls} ${className ?? ""}`}>

      {/* ── Row 1: price + status pill ─────────────────────────────────────── */}
      <div className="flex items-center gap-2">
        <span
          className={`text-xl font-bold tabular-nums leading-none tracking-tight ${priceColor}`}
          aria-label={`Price: ${INR_PRICE.format(price)}`}
        >
          {INR_PRICE.format(price)}
        </span>

        {isLive ? (
          // ── LIVE pill ────────────────────────────────────────────────────
          <span
            className="inline-flex items-center gap-1.5 rounded-full border border-emerald-200 bg-emerald-50 px-2.5 py-1 shadow-sm"
            aria-label="Live price"
            title="Real-time price — market is open"
          >
            <span className="relative flex h-1.5 w-1.5 shrink-0" aria-hidden="true">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-70" />
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-500" />
            </span>
            <span className="select-none text-[9px] font-bold uppercase tracking-[0.1em] text-emerald-700">
              Live
            </span>
          </span>
        ) : (
          // ── LTP pill ─────────────────────────────────────────────────────
          <span
            className="inline-flex items-center rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 select-none text-[9px] font-bold uppercase tracking-[0.1em] text-slate-400"
            aria-label="Last traded price"
            title="Last traded price — market is closed"
          >
            LTP
          </span>
        )}
      </div>

      {/* ── Row 2: day change pill ─────────────────────────────────────────── */}
      {change != null && changePct != null && (
        <span
          className={[
            "inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5",
            "text-xs font-semibold tabular-nums leading-none",
            isUp
              ? "bg-emerald-50 text-emerald-700 ring-1 ring-inset ring-emerald-200"
              : isDown
              ? "bg-red-50 text-red-600 ring-1 ring-inset ring-red-200"
              : "bg-slate-50 text-slate-400 ring-1 ring-inset ring-slate-200",
          ].join(" ")}
          aria-label={`Day change: ${isUp ? "+" : isDown ? "-" : ""}${INR_CHANGE.format(Math.abs(change))} (${Math.abs(changePct).toFixed(2)}%)`}
        >
          <span aria-hidden="true" className="text-[10px]">
            {isUp ? "▲" : isDown ? "▼" : "—"}
          </span>
          <span>{INR_CHANGE.format(Math.abs(change))}</span>
          <span className="opacity-60">({Math.abs(changePct).toFixed(2)}%)</span>
        </span>
      )}
    </div>
  );
});
