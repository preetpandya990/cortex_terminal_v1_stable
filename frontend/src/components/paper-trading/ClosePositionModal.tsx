"use client";

import { useState } from "react";
import { X, AlertTriangle, TrendingDown, TrendingUp } from "lucide-react";
import { useClosePosition } from "@/hooks/usePaperTrading";
import type {
  ClosePositionRequest,
  ExitReason,
  OrderType,
  PaperPosition,
} from "@/types/paper_trading";

interface Props {
  position: PaperPosition;
  livePrice?: number;
  onClose: () => void;
  onClosed?: () => void;
}

const INR_FORMAT = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const EXIT_REASON_OPTIONS: { value: ExitReason; label: string }[] = [
  { value: "MANUAL", label: "Manual Close" },
  { value: "TP1", label: "Reached Target 1" },
  { value: "TP2", label: "Reached Target 2" },
  { value: "TP3", label: "Reached Target 3" },
  { value: "SL", label: "Hit Stop Loss" },
  { value: "EXPIRED", label: "Signal Expired" },
];

export function ClosePositionModal({ position, livePrice, onClose, onClosed }: Props) {
  const { mutateAsync: closePosition, isPending, error } = useClosePosition();

  const [quantity, setQuantity] = useState(position.quantity);
  const [orderType, setOrderType] = useState<OrderType>("MARKET");
  const [limitPrice, setLimitPrice] = useState<string>(
    livePrice ? livePrice.toFixed(2) : position.avg_cost_price.toFixed(2)
  );
  const [exitReason, setExitReason] = useState<ExitReason>("MANUAL");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const currentPrice = livePrice ?? position.last_price ?? position.avg_cost_price;
  const isLong = position.side === "LONG";
  const estimatedPnl =
    orderType === "MARKET"
      ? (currentPrice - position.avg_cost_price) *
        quantity *
        (isLong ? 1 : -1)
      : (parseFloat(limitPrice) - position.avg_cost_price) *
        quantity *
        (isLong ? 1 : -1);

  function validate(): boolean {
    const errors: Record<string, string> = {};
    if (quantity <= 0 || quantity > position.quantity) {
      errors.quantity = `Quantity must be between 1 and ${position.quantity}.`;
    }
    if (orderType === "LIMIT") {
      const price = parseFloat(limitPrice);
      if (isNaN(price) || price <= 0) {
        errors.limitPrice = "Limit price must be a positive number.";
      }
    }
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate()) return;

    const payload: ClosePositionRequest = {
      quantity,
      order_type: orderType,
      exit_reason: exitReason,
      ...(orderType === "LIMIT" && { price: parseFloat(limitPrice) }),
    };

    try {
      await closePosition({ positionId: position.id, payload });
      onClosed?.();
      onClose();
    } catch {
      // error surfaced via `error` from useMutation
    }
  }

  const errorMessage = error
    ? ((error as any)?.message ?? "Failed to close position.")
    : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-200 px-6 py-4">
          <div className="flex items-center gap-2.5">
            <span
              className={`flex h-8 w-8 items-center justify-center rounded-lg ${
                isLong ? "bg-rose-50" : "bg-emerald-50"
              }`}
            >
              {isLong ? (
                <TrendingDown className="h-4 w-4 text-rose-600" />
              ) : (
                <TrendingUp className="h-4 w-4 text-emerald-600" />
              )}
            </span>
            <div>
              <h2 className="text-base font-semibold text-slate-900">
                Close {position.side === "LONG" ? "Long" : "Short"} — {position.symbol}
              </h2>
              <p className="text-xs text-slate-500">
                {position.quantity} shares @ avg {INR_FORMAT.format(position.avg_cost_price)}
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition-colors"
            aria-label="Close modal"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="px-6 py-5 space-y-4">
          {errorMessage && (
            <div className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2.5 text-sm text-rose-700">
              <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0" />
              {errorMessage}
            </div>
          )}

          {/* Live Price Badge */}
          <div className="rounded-lg bg-slate-50 border border-slate-200 px-3 py-2.5 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Live Price
            </span>
            <span className="text-sm font-bold text-slate-900">
              {INR_FORMAT.format(currentPrice)}
            </span>
          </div>

          {/* Quantity */}
          <div className="space-y-1.5">
            <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
              Quantity to Close
            </label>
            <div className="flex items-center gap-2">
              <input
                type="number"
                value={quantity}
                onChange={(e) => setQuantity(Number(e.target.value))}
                min={1}
                max={position.quantity}
                className="flex-1 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 transition"
              />
              <button
                type="button"
                onClick={() => setQuantity(position.quantity)}
                className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs font-semibold text-slate-600 hover:bg-slate-100 transition"
              >
                All ({position.quantity})
              </button>
            </div>
            {fieldErrors.quantity && (
              <p className="text-xs text-rose-600">{fieldErrors.quantity}</p>
            )}
          </div>

          {/* Order Type */}
          <div className="space-y-1.5">
            <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
              Order Type
            </label>
            <div className="flex gap-2">
              {(["MARKET", "LIMIT"] as OrderType[]).map((type) => (
                <button
                  key={type}
                  type="button"
                  onClick={() => setOrderType(type)}
                  className={`flex-1 rounded-lg border px-3 py-2 text-xs font-semibold transition ${
                    orderType === type
                      ? "border-blue-500 bg-blue-600 text-white"
                      : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50"
                  }`}
                >
                  {type}
                </button>
              ))}
            </div>
          </div>

          {/* Limit Price (conditional) */}
          {orderType === "LIMIT" && (
            <div className="space-y-1.5">
              <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
                Limit Price (₹)
              </label>
              <input
                type="number"
                value={limitPrice}
                onChange={(e) => setLimitPrice(e.target.value)}
                min={0.01}
                step={0.05}
                className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 transition"
              />
              {fieldErrors.limitPrice && (
                <p className="text-xs text-rose-600">{fieldErrors.limitPrice}</p>
              )}
            </div>
          )}

          {/* Exit Reason */}
          <div className="space-y-1.5">
            <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
              Exit Reason
            </label>
            <select
              value={exitReason}
              onChange={(e) => setExitReason(e.target.value as ExitReason)}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 transition"
            >
              {EXIT_REASON_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>

          {/* Estimated P&L */}
          <div
            className={`rounded-lg border px-3 py-2.5 ${
              estimatedPnl >= 0
                ? "border-emerald-200 bg-emerald-50"
                : "border-rose-200 bg-rose-50"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                Estimated P&L
              </span>
              <span
                className={`text-sm font-bold ${
                  estimatedPnl >= 0 ? "text-emerald-700" : "text-rose-700"
                }`}
              >
                {estimatedPnl >= 0 ? "+" : ""}
                {INR_FORMAT.format(estimatedPnl)}
              </span>
            </div>
            <p className="mt-0.5 text-[11px] text-slate-500">
              {quantity} × ({INR_FORMAT.format(currentPrice)} − {INR_FORMAT.format(position.avg_cost_price)})
              {orderType === "MARKET" ? " at market price" : " at limit price"} · excludes charges
            </p>
          </div>

          {/* Actions */}
          <div className="flex gap-3 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50 transition"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isPending}
              className={`flex-1 rounded-xl px-4 py-2.5 text-sm font-semibold text-white transition disabled:opacity-60 disabled:cursor-not-allowed ${
                isLong
                  ? "bg-rose-600 hover:bg-rose-700"
                  : "bg-emerald-600 hover:bg-emerald-700"
              }`}
            >
              {isPending ? "Closing…" : `Close ${quantity < position.quantity ? "Partial" : "Full"} Position`}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
