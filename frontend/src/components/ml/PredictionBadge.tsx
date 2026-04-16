"use client";

import { TrendingUp, TrendingDown, Minus } from "lucide-react";
import type { PredictionSignal } from "@/types/ml";

const SIGNAL_CONFIG: Record<
  PredictionSignal,
  {
    label: string;
    bg: string;
    text: string;
    border: string;
    icon: typeof TrendingUp;
  }
> = {
  BUY: {
    label: "BUY",
    bg: "bg-emerald-50",
    text: "text-emerald-700",
    border: "border-emerald-200",
    icon: TrendingUp,
  },
  SELL: {
    label: "SELL",
    bg: "bg-rose-50",
    text: "text-rose-700",
    border: "border-rose-200",
    icon: TrendingDown,
  },
  HOLD: {
    label: "HOLD",
    bg: "bg-amber-50",
    text: "text-amber-700",
    border: "border-amber-200",
    icon: Minus,
  },
};

export function PredictionBadge({
  signal,
  confidence,
  compact = false,
}: {
  signal: PredictionSignal;
  confidence: number;
  compact?: boolean;
}) {
  const config = SIGNAL_CONFIG[signal];
  const Icon = config.icon;

  if (compact) {
    return (
      <span
        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider ${config.bg} ${config.text} ${config.border}`}
      >
        <Icon className="h-3 w-3" />
        {config.label}
      </span>
    );
  }

  return (
    <div
      className={`inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 ${config.bg} ${config.text} ${config.border}`}
    >
      <Icon className="h-4 w-4" />
      <span className="text-xs font-bold uppercase tracking-wider">
        {config.label}
      </span>
      <span className="text-[10px] font-medium opacity-75">
        {confidence.toFixed(0)}%
      </span>
    </div>
  );
}
