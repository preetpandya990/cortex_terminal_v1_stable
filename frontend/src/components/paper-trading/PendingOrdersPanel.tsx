"use client";

/**
 * PendingOrdersPanel
 * ==================
 * Displays all OPEN (pending) LIMIT/SL/SL-M orders for the active portfolio.
 * Each row shows the key order details and a Cancel button.
 *
 * The panel is kept up-to-date via two mechanisms:
 *  1. React Query refetch interval (30 s) — baseline polling.
 *  2. `usePnLWebSocket` publishes `order_filled` / `order_expired` events that
 *     automatically trigger a React Query cache invalidation, making the list
 *     update in real-time without the user refreshing.
 */

import { AlertTriangle, Clock, X } from "lucide-react";
import type { PaperOrder, OrderStatus } from "@/types/paper_trading";
import { usePendingOrders, useCancelOrder } from "@/hooks/usePaperTrading";

// ── Formatting helpers ──────────────────────────────────────────────────────

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-IN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  });
}

// ── Sub-components ──────────────────────────────────────────────────────────

function SideBadge({ side }: { side: "BUY" | "SELL" }) {
  const isB = side === "BUY";
  return (
    <span
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-semibold tracking-wide ${
        isB
          ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400"
          : "bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400"
      }`}
    >
      {side}
    </span>
  );
}

function OrderTypeBadge({ type }: { type: string }) {
  return (
    <span className="inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400">
      {type}
    </span>
  );
}

interface OrderRowProps {
  order: PaperOrder;
  onCancel: (id: string) => void;
  cancelling: boolean;
}

function OrderRow({ order, onCancel, cancelling }: OrderRowProps) {
  const refPrice =
    order.order_type === "LIMIT"
      ? order.price
      : order.trigger_price ?? order.price;

  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800/60 px-4 py-3 shadow-sm">
      {/* Left — symbol + badges */}
      <div className="flex min-w-0 flex-col gap-1">
        <span className="truncate text-sm font-semibold text-slate-900 dark:text-slate-100">
          {order.symbol}
        </span>
        <div className="flex items-center gap-1.5">
          <SideBadge side={order.transaction_type as "BUY" | "SELL"} />
          <OrderTypeBadge type={order.order_type} />
        </div>
      </div>

      {/* Centre — price + qty + time */}
      <div className="hidden sm:flex flex-1 items-center justify-center gap-6 text-sm text-slate-600 dark:text-slate-400">
        {refPrice != null && (
          <span>
            <span className="text-xs text-slate-400 mr-1">
              {order.order_type === "LIMIT" ? "Limit" : "Trigger"}
            </span>
            <span className="font-medium text-slate-800 dark:text-slate-200">
              {INR.format(refPrice)}
            </span>
          </span>
        )}
        <span>
          <span className="text-xs text-slate-400 mr-1">Qty</span>
          <span className="font-medium text-slate-800 dark:text-slate-200">
            {order.quantity}
          </span>
        </span>
        <span className="flex items-center gap-1">
          <Clock className="h-3 w-3 text-slate-400" />
          <span className="text-xs">{formatTime(order.placed_at)}</span>
        </span>
        <span className="rounded px-1.5 py-0.5 text-xs font-medium bg-slate-100 dark:bg-slate-700 text-slate-500 dark:text-slate-400">
          {order.validity}
        </span>
      </div>

      {/* Right — cancel */}
      <button
        type="button"
        disabled={cancelling}
        onClick={() => onCancel(order.id)}
        className="flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium text-rose-600 dark:text-rose-400 border border-rose-200 dark:border-rose-800 hover:bg-rose-50 dark:hover:bg-rose-900/20 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        title="Cancel order"
      >
        <X className="h-3.5 w-3.5" />
        Cancel
      </button>
    </div>
  );
}

// ── Main component ──────────────────────────────────────────────────────────

interface Props {
  /** Called after a successful cancel so the parent can show a toast. */
  onCancelSuccess?: (symbol: string) => void;
  /** Called when a cancel fails so the parent can show an error toast. */
  onCancelError?: (error: Error) => void;
}

export default function PendingOrdersPanel({ onCancelSuccess, onCancelError }: Props) {
  const { data, isLoading, isError } = usePendingOrders();
  const cancelOrder = useCancelOrder();

  const orders: PaperOrder[] = data?.orders ?? [];

  function handleCancel(orderId: string) {
    const order = orders.find((o) => o.id === orderId);
    cancelOrder.mutate(orderId, {
      onSuccess: () => {
        onCancelSuccess?.(order?.symbol ?? "order");
      },
      onError: (err) => {
        onCancelError?.(err as Error);
      },
    });
  }

  if (isLoading) {
    return (
      <div className="space-y-2">
        {[...Array(2)].map((_, i) => (
          <div
            key={i}
            className="h-16 rounded-lg bg-slate-100 dark:bg-slate-800 animate-pulse"
          />
        ))}
      </div>
    );
  }

  if (isError) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-rose-200 dark:border-rose-800 bg-rose-50 dark:bg-rose-900/20 px-4 py-3 text-sm text-rose-600 dark:text-rose-400">
        <AlertTriangle className="h-4 w-4 flex-shrink-0" />
        Failed to load pending orders. Try refreshing.
      </div>
    );
  }

  if (orders.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/30 px-6 py-10 text-center">
        <Clock className="h-8 w-8 text-slate-300 dark:text-slate-600" />
        <p className="text-sm font-medium text-slate-500 dark:text-slate-400">
          No pending orders
        </p>
        <p className="text-xs text-slate-400 dark:text-slate-500">
          Place a LIMIT order to see it here. It will fill automatically when the market price
          reaches your limit.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {orders.map((order) => (
        <OrderRow
          key={order.id}
          order={order}
          onCancel={handleCancel}
          cancelling={
            cancelOrder.isPending && cancelOrder.variables === order.id
          }
        />
      ))}
    </div>
  );
}
