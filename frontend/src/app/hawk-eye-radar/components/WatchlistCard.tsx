"use client";

import { memo, useMemo } from "react";
import { TrendingUp, TrendingDown, Minus, X, GripVertical } from "lucide-react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useLtp } from "@/hooks/useLtp";
import type { WatchlistItem } from "@/lib/api";

interface GripHandlers {
  onPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
}

interface WatchlistCardProps {
  item: WatchlistItem;
  onRemove?: (itemId: number) => void;
  onViewDetails?: (instrumentKey: string) => void;
  className?: string;
  /** True while this card is the one being dragged. */
  isDragging?: boolean;
  /** True while this card is the current drag drop-target. */
  isOver?: boolean;
  /** Pointer handlers for the grip handle; omit to disable drag on this card. */
  gripHandlers?: GripHandlers;
}

function WatchlistCardComponent({
  item,
  onRemove,
  onViewDetails,
  className,
  isDragging = false,
  isOver = false,
  gripHandlers,
}: WatchlistCardProps) {
  const snapshot   = useLtp(item.instrument_key);
  const ltp        = snapshot?.ltp ?? null;
  const prevClose  = snapshot?.cp ?? null;

  const { percentChange, isPositive, isNeutral } = useMemo(() => {
    if (!ltp || !prevClose || prevClose === 0) {
      return { percentChange: 0, isPositive: false, isNeutral: true };
    }
    const change = ((ltp - prevClose) / prevClose) * 100;
    return {
      percentChange: change,
      isPositive: change > 0,
      isNeutral: change === 0,
    };
  }, [ltp, prevClose]);

  const changeColor = isNeutral
    ? "text-slate-600"
    : isPositive
      ? "text-green-600"
      : "text-red-600";

  const changeBg = isNeutral
    ? "bg-slate-50"
    : isPositive
      ? "bg-green-50"
      : "bg-red-50";

  return (
    <Card
      // `data-drag-id` is read by useDragReorder's elementFromPoint hit-test.
      data-drag-id={item.id}
      className={cn(
        "border-slate-200 bg-white transition-all duration-150 cursor-pointer select-none",
        // Idle hover
        !isDragging && !isOver && "hover:shadow-md",
        // Being dragged: dim and shrink slightly so the target slot is visible
        isDragging && "opacity-40 scale-[0.97] shadow-lg ring-1 ring-slate-300",
        // Drop target: subtle highlight ring
        isOver && !isDragging && "ring-2 ring-slate-400/60 shadow-md bg-slate-50/60",
        className,
      )}
      onClick={() => onViewDetails?.(item.instrument_key)}
    >
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2 flex-1 min-w-0">
            {/* Grip handle — pointer events are handled here exclusively so that
                a tap anywhere else on the card still registers as a click/view. */}
            <button
              type="button"
              aria-label="Drag to reorder"
              className={cn(
                "touch-none shrink-0 transition-colors text-slate-300 hover:text-slate-500",
                gripHandlers ? (isDragging ? "cursor-grabbing" : "cursor-grab") : "cursor-default opacity-50",
              )}
              onPointerDown={(e) => {
                e.stopPropagation();
                gripHandlers?.onPointerDown(e);
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <GripVertical className="h-4 w-4" />
            </button>

            <div className="flex-1 min-w-0">
              <h3 className="text-lg font-semibold text-slate-900 truncate">
                {item.trading_symbol}
              </h3>
              <p className="text-xs text-slate-500 truncate">
                {item.name || item.exchange || "—"}
              </p>
            </div>
          </div>

          <Button
            size="icon"
            variant="ghost"
            onClick={(e) => {
              e.stopPropagation();
              onRemove?.(item.id);
            }}
            className="h-7 w-7 shrink-0 text-slate-400 hover:text-red-600 hover:bg-red-50"
          >
            <X className="h-4 w-4" />
          </Button>
        </div>
      </CardHeader>

      <CardContent className="space-y-3 pb-4">
        {/* LTP and Change */}
        <div className="flex items-end justify-between">
          <div>
            <p className="text-xs text-slate-500 mb-0.5">Last Price</p>
            <p className="text-2xl font-bold text-slate-900">
              {ltp !== null ? `₹${ltp.toFixed(2)}` : "—"}
            </p>
          </div>
          {ltp !== null && prevClose !== null && (
            <div className={cn("flex items-center gap-1.5 px-2.5 py-1 rounded-md", changeBg)}>
              {isPositive ? (
                <TrendingUp className={cn("h-4 w-4", changeColor)} />
              ) : isNeutral ? (
                <Minus className={cn("h-4 w-4", changeColor)} />
              ) : (
                <TrendingDown className={cn("h-4 w-4", changeColor)} />
              )}
              <span className={cn("text-sm font-semibold", changeColor)}>
                {isPositive && "+"}
                {percentChange.toFixed(2)}%
              </span>
            </div>
          )}
        </div>

        {/* Previous Close */}
        {prevClose !== null && (
          <div className="pt-2 border-t border-slate-100">
            <div className="flex items-center justify-between">
              <p className="text-xs text-slate-500">Prev. Close</p>
              <p className="text-sm font-medium text-slate-700">
                ₹{prevClose.toFixed(2)}
              </p>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export const WatchlistCard = memo(WatchlistCardComponent);
