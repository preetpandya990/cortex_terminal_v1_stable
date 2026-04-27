"use client";

/**
 * Instrument Regime Detail Pane
 *
 * Floating modal showing full regime + technical indicators for a single
 * instrument.  Triggered by clicking any row in the constituent list.
 * Same scroll-safe layout as SignalDetailModal.
 */
import * as React from "react";
import {
  TrendingUp,
  TrendingDown,
  Minus,
  Activity,
  AlertTriangle,
  Droplets,
  X,
  RefreshCw,
} from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useInstrumentRegime } from "@/hooks/useRegime";
import type { InstrumentRegime, RegimeType } from "@/types/regime";

// ── Helpers ───────────────────────────────────────────────────────────────────

function regimeLabel(r: RegimeType | string): string {
  return (r as string)
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function regimeColorClass(r: RegimeType | string): string {
  switch (r) {
    case "bull_trending":    return "bg-emerald-500/10 text-emerald-700 border-emerald-500/20";
    case "bear_trending":    return "bg-red-500/10 text-red-700 border-red-500/20";
    case "sideways_range":   return "bg-blue-500/10 text-blue-700 border-blue-500/20";
    case "high_volatility":  return "bg-orange-500/10 text-orange-700 border-orange-500/20";
    case "low_liquidity":    return "bg-purple-500/10 text-purple-700 border-purple-500/20";
    default:                 return "bg-slate-500/10 text-slate-600 border-slate-500/20";
  }
}

function RegimeIcon({ regime, size = 5 }: { regime: RegimeType | string; size?: number }) {
  const cls = `size-${size}`;
  switch (regime) {
    case "bull_trending":   return <TrendingUp className={cls} />;
    case "bear_trending":   return <TrendingDown className={cls} />;
    case "high_volatility": return <AlertTriangle className={cls} />;
    case "low_liquidity":   return <Droplets className={cls} />;
    default:                return <Activity className={cls} />;
  }
}

function TrendArrow({ trend }: { trend: string }) {
  if (trend === "up")   return <span className="text-emerald-600 font-bold">↑</span>;
  if (trend === "down") return <span className="text-red-600 font-bold">↓</span>;
  return <span className="text-slate-400">↔</span>;
}

function ChangeCell({ value }: { value: number }) {
  const sign = value >= 0 ? "+" : "";
  const cls = value > 0
    ? "text-emerald-600"
    : value < 0
    ? "text-red-600"
    : "text-slate-500";
  return <span className={`font-semibold ${cls}`}>{sign}{value.toFixed(2)}%</span>;
}

function IndicatorBar({
  label,
  value,
  min,
  max,
  unit = "",
  colorFn,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  unit?: string;
  colorFn?: (v: number) => string;
}) {
  const pct = Math.min(100, Math.max(0, ((value - min) / (max - min)) * 100));
  const barColor = colorFn ? colorFn(value) : "bg-blue-500";
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className="text-slate-500">{label}</span>
        <span className="font-semibold text-slate-700">{value.toFixed(2)}{unit}</span>
      </div>
      <div className="h-1.5 w-full rounded-full bg-slate-100">
        <div
          className={`h-1.5 rounded-full transition-all ${barColor}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ── Content ───────────────────────────────────────────────────────────────────

function RegimeDetailContent({ data }: { data: InstrumentRegime }) {
  const { indicators } = data;

  return (
    <div className="space-y-5">
      {/* Current regime + price */}
      <Card className="border-slate-200/80">
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-semibold text-slate-500 uppercase tracking-wide">
            Current Regime
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center gap-4">
            <div
              className={`flex size-14 items-center justify-center rounded-xl border-2 ${regimeColorClass(data.regime)}`}
            >
              <RegimeIcon regime={data.regime} size={6} />
            </div>
            <div className="flex-1">
              <div className="flex items-center gap-2">
                <span className="text-xl font-bold text-slate-900">
                  {regimeLabel(data.regime)}
                </span>
                <Badge variant="outline" className={`text-xs ${regimeColorClass(data.regime)}`}>
                  {data.strength}
                </Badge>
              </div>
              <p className="text-sm text-slate-500 mt-0.5">
                Confidence: <span className="font-semibold text-slate-700">{(data.confidence * 100).toFixed(1)}%</span>
              </p>
            </div>
          </div>

          <div className="grid grid-cols-3 gap-3">
            <div className="rounded-lg bg-slate-50 p-3 text-center">
              <p className="text-xs text-slate-500 mb-1">Price</p>
              <p className="text-base font-bold text-slate-900">
                ₹{data.current_price.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </p>
            </div>
            <div className="rounded-lg bg-slate-50 p-3 text-center">
              <p className="text-xs text-slate-500 mb-1">1-Day</p>
              <p className="text-base">
                <TrendArrow trend={data.trend} /> <ChangeCell value={data.change_pct} />
              </p>
            </div>
            <div className="rounded-lg bg-slate-50 p-3 text-center">
              <p className="text-xs text-slate-500 mb-1">20-Day</p>
              <p className="text-base"><ChangeCell value={data.change_pct_20d} /></p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Technical indicators */}
      <Card className="border-slate-200/80">
        <CardHeader className="pb-3">
          <CardTitle className="text-sm font-semibold text-slate-500 uppercase tracking-wide">
            Technical Indicators
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Grid: ADX + RSI */}
          <div className="grid grid-cols-2 gap-4">
            <div className="rounded-lg border border-slate-200 p-3">
              <p className="text-xs text-slate-500 mb-0.5">ADX (Trend Strength)</p>
              <p className="text-2xl font-bold text-slate-900">{indicators.adx.toFixed(1)}</p>
              <p className="text-xs text-slate-400 mt-1">
                {indicators.adx > 25 ? "Strong trend" : indicators.adx > 15 ? "Weak trend" : "No trend"}
              </p>
            </div>
            <div className="rounded-lg border border-slate-200 p-3">
              <p className="text-xs text-slate-500 mb-0.5">RSI (14)</p>
              <p className={`text-2xl font-bold ${
                indicators.rsi > 70 ? "text-orange-600"
                  : indicators.rsi < 30 ? "text-blue-600"
                  : "text-slate-900"
              }`}>{indicators.rsi.toFixed(1)}</p>
              <p className="text-xs text-slate-400 mt-1">
                {indicators.rsi > 70 ? "Overbought" : indicators.rsi < 30 ? "Oversold" : "Neutral"}
              </p>
            </div>
          </div>

          {/* Bars: ATR + Bollinger */}
          <div className="space-y-3 pt-1">
            <IndicatorBar
              label="ATR% (Volatility)"
              value={indicators.atr_pct}
              min={0}
              max={5}
              unit="%"
              colorFn={(v) =>
                v > 3.5 ? "bg-orange-500" : v > 2 ? "bg-amber-400" : "bg-emerald-500"
              }
            />
            <IndicatorBar
              label="Bollinger Width"
              value={indicators.bollinger_width}
              min={0}
              max={0.12}
              colorFn={(v) =>
                v > 0.065 ? "bg-orange-500" : v > 0.035 ? "bg-blue-500" : "bg-emerald-500"
              }
            />
            <IndicatorBar
              label="ATR (Absolute)"
              value={indicators.atr}
              min={0}
              max={indicators.atr * 3}
              unit={` ₹`}
              colorFn={() => "bg-slate-400"}
            />
          </div>
        </CardContent>
      </Card>

      {/* Data info */}
      <p className="text-xs text-slate-400 text-right">
        Based on {data.data_points} daily candles • computed {new Date(data.computed_at).toLocaleTimeString()}
      </p>
    </div>
  );
}

// ── Modal ─────────────────────────────────────────────────────────────────────

interface InstrumentRegimeModalProps {
  instrumentKey: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Pre-loaded data from the constituent list — shown instantly while fresh data loads. */
  prefill?: InstrumentRegime | null;
}

export function InstrumentRegimeModal({
  instrumentKey,
  open,
  onOpenChange,
  prefill,
}: InstrumentRegimeModalProps) {
  const { data, isLoading, isError, refetch, isRefetching } =
    useInstrumentRegime(open ? instrumentKey : null);

  // Show prefill immediately, replace with fresh data when it arrives
  const display = data ?? prefill;

  if (!open) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl !flex flex-col max-h-[90vh] overflow-hidden !p-0">
        {/* ── Sticky header ── */}
        <div className="flex-shrink-0 border-b border-slate-100 px-6 pt-5 pb-4">
          <DialogHeader>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <DialogTitle className="text-lg font-bold">
                  {display?.trading_symbol ?? instrumentKey?.split("|").pop() ?? "Instrument"}
                </DialogTitle>
                {isRefetching && <RefreshCw className="size-3.5 animate-spin text-slate-400" />}
              </div>
              <Button variant="ghost" size="icon-sm" onClick={() => onOpenChange(false)}>
                <X className="size-4" />
              </Button>
            </div>
            {display && (
              <DialogDescription className="mt-0.5">
                {display.company_name}
              </DialogDescription>
            )}
          </DialogHeader>
        </div>

        {/* ── Scrollable body ── */}
        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-5">
          {isLoading && !display && (
            <div className="flex items-center justify-center py-12">
              <RefreshCw className="size-6 animate-spin text-slate-400" />
            </div>
          )}
          {isError && !display && (
            <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
              Failed to load regime data for this instrument.
              <Button variant="ghost" size="sm" onClick={() => refetch()} className="ml-2">
                Retry
              </Button>
            </div>
          )}
          {display && display.regime !== "insufficient_data" && (
            <RegimeDetailContent data={display} />
          )}
          {display && display.regime === "insufficient_data" && (
            <div className="rounded-lg border border-slate-200 bg-slate-50 px-5 py-6 text-center">
              <Activity className="size-8 text-slate-300 mx-auto mb-3" />
              <p className="text-sm font-medium text-slate-600">No historical data available</p>
              <p className="text-xs text-slate-400 mt-1">
                Regime computation requires at least 20 daily candles in the database.
              </p>
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
