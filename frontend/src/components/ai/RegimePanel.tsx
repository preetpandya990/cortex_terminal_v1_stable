"use client";

/**
 * Market Regime Panel
 *
 * Three-section layout:
 *  1. Macro overview — Nifty 50 aggregate regime + market breadth
 *  2. Index grid    — 10 Nifty indices, selectable
 *  3. Constituents  — Scrollable list of stocks for the selected index
 *
 * Clicking a stock row opens InstrumentRegimeModal (detail pane).
 * All data is DB-driven via /strategy/market/overview and
 * /strategy/index/{key}/constituents — no mock, no random values.
 */
import * as React from "react";
import {
  TrendingUp,
  TrendingDown,
  Activity,
  AlertTriangle,
  Droplets,
  RefreshCw,
  Search,
  ChevronRight,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useMarketOverview, useIndexConstituents } from "@/hooks/useRegime";
import { InstrumentRegimeModal } from "@/components/ai/InstrumentRegimeModal";
import type {
  RegimeType,
  TrendDirection,
  IndexRegime,
  InstrumentRegime,
  MarketBreadth,
} from "@/types/regime";

// ── Display helpers ────────────────────────────────────────────────────────────

function regimeLabel(r: RegimeType | string): string {
  if (!r || r === "insufficient_data") return "No Data";
  return (r as string)
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

function regimeBadgeClass(r: RegimeType | string): string {
  switch (r) {
    case "bull_trending":   return "bg-emerald-500/10 text-emerald-700 border-emerald-500/20";
    case "bear_trending":   return "bg-red-500/10 text-red-700 border-red-500/20";
    case "sideways_range":  return "bg-blue-500/10 text-blue-700 border-blue-500/20";
    case "high_volatility": return "bg-orange-500/10 text-orange-700 border-orange-500/20";
    case "low_liquidity":   return "bg-purple-500/10 text-purple-700 border-purple-500/20";
    default:                return "bg-slate-100 text-slate-500 border-slate-200";
  }
}

function regimeCardBg(r: RegimeType | string): string {
  switch (r) {
    case "bull_trending":   return "border-emerald-200 bg-emerald-50/40";
    case "bear_trending":   return "border-red-200 bg-red-50/40";
    case "sideways_range":  return "border-blue-200 bg-blue-50/30";
    case "high_volatility": return "border-orange-200 bg-orange-50/30";
    case "low_liquidity":   return "border-purple-200 bg-purple-50/30";
    default:                return "border-slate-200 bg-white";
  }
}

function RegimeIcon({ regime, className = "size-5" }: { regime: RegimeType | string; className?: string }) {
  switch (regime) {
    case "bull_trending":   return <TrendingUp className={className} />;
    case "bear_trending":   return <TrendingDown className={className} />;
    case "high_volatility": return <AlertTriangle className={className} />;
    case "low_liquidity":   return <Droplets className={className} />;
    default:                return <Activity className={className} />;
  }
}

function TrendArrow({ trend, change }: { trend: TrendDirection; change: number }) {
  const sign = change >= 0 ? "+" : "";
  if (trend === "up")   return <span className="text-emerald-600 font-semibold">↑ {sign}{change.toFixed(2)}%</span>;
  if (trend === "down") return <span className="text-red-600 font-semibold">↓ {change.toFixed(2)}%</span>;
  return <span className="text-slate-400 font-semibold">↔ {sign}{change.toFixed(2)}%</span>;
}

function ChangeText({ value }: { value: number }) {
  const sign = value >= 0 ? "+" : "";
  const cls = value > 0 ? "text-emerald-600" : value < 0 ? "text-red-600" : "text-slate-400";
  return <span className={`font-semibold ${cls}`}>{sign}{value.toFixed(2)}%</span>;
}

// ── Macro overview card ────────────────────────────────────────────────────────

function MacroOverviewCard({
  nifty50,
  breadth,
  onRefresh,
  isRefetching,
}: {
  nifty50: IndexRegime;
  breadth: MarketBreadth;
  onRefresh: () => void;
  isRefetching: boolean;
}) {
  const n = nifty50;
  return (
    <div className={`rounded-2xl border-2 p-5 ${regimeCardBg(n.regime)}`}>
      <div className="flex items-start justify-between gap-4">
        {/* Icon + label */}
        <div className="flex items-center gap-4">
          <div className={`flex size-14 items-center justify-center rounded-xl border-2 ${regimeBadgeClass(n.regime)}`}>
            <RegimeIcon regime={n.regime} className="size-7" />
          </div>
          <div>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xl font-extrabold text-slate-900 tracking-tight">
                {regimeLabel(n.regime)}
              </span>
              <Badge variant="outline" className={`text-xs ${regimeBadgeClass(n.regime)}`}>
                {n.strength}
              </Badge>
            </div>
            <p className="text-sm text-slate-500 mt-0.5">
              Nifty 50 &nbsp;•&nbsp;{(n.confidence * 100).toFixed(0)}% confidence
            </p>
          </div>
        </div>

        {/* Refresh */}
        <Button
          variant="ghost"
          size="icon"
          onClick={onRefresh}
          disabled={isRefetching}
          className="text-slate-400 hover:text-slate-700 shrink-0"
        >
          <RefreshCw className={`size-4 ${isRefetching ? "animate-spin" : ""}`} />
        </Button>
      </div>

      {/* Price change row */}
      <div className="mt-4 flex items-center gap-6 text-sm">
        <span className="text-slate-500">
          Today: <TrendArrow trend={n.trend} change={n.change_pct} />
        </span>
        <span className="text-slate-400">
          {n.total_constituents} stocks tracked
        </span>
      </div>

      {/* Breadth bar */}
      <div className="mt-4 space-y-2">
        <p className="text-xs font-semibold uppercase tracking-widest text-slate-400">
          Market Breadth — Nifty 50
        </p>
        <div className="flex h-3 w-full overflow-hidden rounded-full gap-0.5">
          <div
            className="bg-emerald-500 transition-all"
            style={{ width: `${breadth.bullish_pct}%` }}
            title={`Bullish ${breadth.bullish_pct}%`}
          />
          <div
            className="bg-blue-400 transition-all"
            style={{ width: `${breadth.sideways_pct}%` }}
            title={`Sideways ${breadth.sideways_pct}%`}
          />
          <div
            className="bg-orange-400 transition-all"
            style={{ width: `${breadth.volatile_pct}%` }}
            title={`Volatile ${breadth.volatile_pct}%`}
          />
          <div
            className="bg-red-500 transition-all"
            style={{ width: `${breadth.bearish_pct}%` }}
            title={`Bearish ${breadth.bearish_pct}%`}
          />
        </div>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
          <span><span className="inline-block size-2 rounded-full bg-emerald-500 mr-1" />Bull {breadth.bullish_pct}%</span>
          <span><span className="inline-block size-2 rounded-full bg-blue-400 mr-1" />Sideways {breadth.sideways_pct}%</span>
          <span><span className="inline-block size-2 rounded-full bg-orange-400 mr-1" />Volatile {breadth.volatile_pct}%</span>
          <span><span className="inline-block size-2 rounded-full bg-red-500 mr-1" />Bear {breadth.bearish_pct}%</span>
        </div>
      </div>
    </div>
  );
}

// ── Index card (grid item) ─────────────────────────────────────────────────────

function IndexCard({
  index,
  selected,
  onClick,
}: {
  index: IndexRegime;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full rounded-xl border-2 p-3 text-left transition-all hover:shadow-sm
        ${selected
          ? `${regimeCardBg(index.regime)} border-current ring-2 ring-offset-1`
          : "border-slate-200 bg-white hover:border-slate-300"
        }
      `}
      style={selected ? { "--tw-ring-color": "rgb(99 102 241 / 0.4)" } as React.CSSProperties : undefined}
    >
      <div className="flex items-start justify-between gap-1">
        <div className={`flex size-8 items-center justify-center rounded-lg border ${regimeBadgeClass(index.regime)}`}>
          <RegimeIcon regime={index.regime} className="size-4" />
        </div>
        <span className={`text-[10px] font-semibold rounded px-1.5 py-0.5 ${regimeBadgeClass(index.regime)}`}>
          {regimeLabel(index.regime).split(" ")[0]}
        </span>
      </div>
      <p className="mt-2 text-xs font-bold text-slate-900 leading-tight">{index.name}</p>
      <div className="mt-1 flex items-center justify-between text-[10px]">
        <span className="text-slate-400">{(index.confidence * 100).toFixed(0)}% conf</span>
        <ChangeText value={index.change_pct} />
      </div>
      <div className="mt-1.5 flex gap-1 text-[9px]">
        <span className="text-emerald-600">▲{index.bullish_count}</span>
        <span className="text-red-500">▼{index.bearish_count}</span>
        <span className="text-blue-500">↔{index.sideways_count}</span>
        <span className="text-orange-400">⚡{index.volatile_count}</span>
      </div>
    </button>
  );
}

// ── Constituent row ────────────────────────────────────────────────────────────

function ConstituentRow({
  stock,
  onClick,
}: {
  stock: InstrumentRegime;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full flex items-center gap-4 rounded-xl border border-slate-200 bg-white px-4 py-3
                 hover:border-slate-300 hover:bg-slate-50 transition-all text-left group"
    >
      {/* Regime icon */}
      <div className={`flex size-9 shrink-0 items-center justify-center rounded-lg border ${regimeBadgeClass(stock.regime)}`}>
        <RegimeIcon regime={stock.regime} className="size-4" />
      </div>

      {/* Symbol + name */}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-bold text-sm text-slate-900">{stock.trading_symbol}</span>
          <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${regimeBadgeClass(stock.regime)}`}>
            {regimeLabel(stock.regime)}
          </span>
        </div>
        <p className="text-xs text-slate-400 truncate mt-0.5">{stock.company_name}</p>
      </div>

      {/* Price + change */}
      <div className="text-right shrink-0">
        <p className="text-sm font-bold text-slate-900">
          ₹{stock.current_price.toLocaleString("en-IN", { maximumFractionDigits: 2 })}
        </p>
        <p className="text-xs mt-0.5"><ChangeText value={stock.change_pct} /></p>
      </div>

      {/* Indicators */}
      <div className="text-right shrink-0 hidden sm:block min-w-[90px]">
        <p className="text-[10px] text-slate-400">ADX <span className="font-semibold text-slate-600">{stock.indicators.adx.toFixed(1)}</span></p>
        <p className="text-[10px] text-slate-400">RSI <span className={`font-semibold ${
          stock.indicators.rsi > 70 ? "text-orange-500" :
          stock.indicators.rsi < 30 ? "text-blue-500" : "text-slate-600"
        }`}>{stock.indicators.rsi.toFixed(1)}</span></p>
      </div>

      {/* Trend arrow */}
      <div className="shrink-0 text-slate-300 group-hover:text-slate-500 transition-colors">
        {stock.trend === "up" && <TrendingUp className="size-4 text-emerald-500" />}
        {stock.trend === "down" && <TrendingDown className="size-4 text-red-500" />}
        {stock.trend === "flat" && <Activity className="size-4 text-slate-400" />}
      </div>

      <ChevronRight className="size-4 text-slate-300 group-hover:text-slate-500 shrink-0" />
    </button>
  );
}

// ── Skeleton ───────────────────────────────────────────────────────────────────

function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`animate-pulse rounded-lg bg-slate-100 ${className}`} />;
}

// ── Main panel ────────────────────────────────────────────────────────────────

interface RegimePanelProps {
  className?: string;
}

export function RegimePanel({ className }: RegimePanelProps) {
  const [selectedIndex, setSelectedIndex] = React.useState<string | null>("niftyit");
  const [selectedInstrument, setSelectedInstrument] = React.useState<InstrumentRegime | null>(null);
  const [modalOpen, setModalOpen] = React.useState(false);
  const [searchQuery, setSearchQuery] = React.useState("");

  const {
    data: overview,
    isLoading: overviewLoading,
    refetch: refetchOverview,
    isRefetching: overviewRefetching,
  } = useMarketOverview();

  const {
    data: constituents,
    isLoading: constituentsLoading,
  } = useIndexConstituents(selectedIndex);

  const handleInstrumentClick = (stock: InstrumentRegime) => {
    setSelectedInstrument(stock);
    setModalOpen(true);
  };

  const filteredConstituents = React.useMemo(() => {
    const list = constituents?.constituents ?? [];
    if (!searchQuery.trim()) return list;
    const q = searchQuery.toUpperCase();
    return list.filter(
      (s) => s.trading_symbol.includes(q) || s.company_name.toUpperCase().includes(q)
    );
  }, [constituents, searchQuery]);

  return (
    <>
      <Card className={`border-slate-200/80 bg-white/90 ${className ?? ""}`}>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-lg font-bold text-slate-900">
            <Activity className="size-5 text-indigo-600" />
            Market Regime
          </CardTitle>
          <p className="text-xs text-slate-400 mt-0.5">
            Computed from 60-day daily OHLCV · refreshes every 15 min
          </p>
        </CardHeader>

        <CardContent className="space-y-6">
          {/* ── 1. Macro overview ── */}
          {overviewLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-40 w-full" />
            </div>
          ) : overview ? (
            <MacroOverviewCard
              nifty50={overview.nifty50}
              breadth={overview.breadth}
              onRefresh={refetchOverview}
              isRefetching={overviewRefetching}
            />
          ) : (
            <div className="rounded-2xl border-2 border-dashed border-slate-200 px-6 py-8 text-center">
              <Activity className="size-8 text-slate-300 mx-auto mb-3" />
              <p className="text-sm font-medium text-slate-500">Regime data unavailable</p>
              <p className="text-xs text-slate-400 mt-1">
                Ensure OHLCV data has been ingested for Nifty 50 constituents.
              </p>
              <Button variant="outline" size="sm" onClick={() => refetchOverview()} className="mt-4">
                <RefreshCw className="size-3.5 mr-2" /> Retry
              </Button>
            </div>
          )}

          {/* ── 2. Index grid ── */}
          {overviewLoading ? (
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2">
              {Array.from({ length: 10 }).map((_, i) => (
                <Skeleton key={i} className="h-24" />
              ))}
            </div>
          ) : overview && overview.all_indices.length > 0 ? (
            <div>
              <p className="text-xs font-semibold uppercase tracking-widest text-slate-400 mb-3">
                Nifty Indices
              </p>
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 gap-2">
                {overview.all_indices.map((idx) => (
                  <IndexCard
                    key={idx.index_key}
                    index={idx}
                    selected={selectedIndex === idx.index_key}
                    onClick={() => {
                      setSelectedIndex(idx.index_key);
                      setSearchQuery("");
                    }}
                  />
                ))}
              </div>
            </div>
          ) : null}

          {/* ── 3. Constituents ── */}
          {selectedIndex && (
            <div>
              <div className="flex items-center justify-between gap-3 mb-3">
                <p className="text-xs font-semibold uppercase tracking-widest text-slate-400">
                  {overview?.all_indices.find((i) => i.index_key === selectedIndex)?.name ?? selectedIndex} — Stocks
                </p>
                <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs text-slate-500 min-w-[160px]">
                  <Search className="size-3.5 shrink-0 text-slate-400" />
                  <input
                    type="text"
                    placeholder="Filter stocks…"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    className="bg-transparent outline-none flex-1 text-slate-700 placeholder:text-slate-400"
                  />
                </div>
              </div>

              {constituentsLoading ? (
                <div className="space-y-2">
                  {Array.from({ length: 6 }).map((_, i) => (
                    <Skeleton key={i} className="h-14" />
                  ))}
                </div>
              ) : filteredConstituents.length > 0 ? (
                <div className="max-h-[480px] overflow-y-auto space-y-2 pr-0.5">
                  {filteredConstituents.map((stock) => (
                    <ConstituentRow
                      key={stock.instrument_key}
                      stock={stock}
                      onClick={() => handleInstrumentClick(stock)}
                    />
                  ))}
                </div>
              ) : (
                <div className="rounded-xl border border-slate-200 px-5 py-8 text-center text-sm text-slate-400">
                  {searchQuery
                    ? `No stocks matching "${searchQuery}"`
                    : "No constituent data available for this index."}
                </div>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── Instrument detail pane ── */}
      <InstrumentRegimeModal
        instrumentKey={selectedInstrument?.instrument_key ?? null}
        open={modalOpen}
        onOpenChange={(open) => {
          setModalOpen(open);
          if (!open) setSelectedInstrument(null);
        }}
        prefill={selectedInstrument}
      />
    </>
  );
}
