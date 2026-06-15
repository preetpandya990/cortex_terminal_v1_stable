"use client";

import { useMemo } from "react";
import { TrendingUp as TrendingUpIcon, TrendingDown, Clock, Brain, Cpu, Target, Shield, Sparkles, ArrowUpRight } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { cn } from "@/lib/utils";
import { FundamentalsTab } from "@/components/hawk-eye-radar/FundamentalsTab";
import { AnalysisCardsSection } from "@/components/AnalysisCardsSection";
import type { TradeSuggestion } from "@/types/trade_suggestions";

const SIGNAL_SKIP_KEYS = new Set([
  "instrument_key", "trading_symbol", "scanned_at",
]);

const SIGNAL_KEY_LABELS: Record<string, string> = {
  signal:        "Direction",
  score:         "Score",
  rsi:           "RSI",
  volume_ratio:  "Vol Ratio",
  price_change_pct: "Price Δ%",
  last_price:    "Price",
  previous_close: "Prev Close",
  volume:        "Volume",
  direction:     "Direction",
  confidence:    "Confidence",
  event_count:   "Events",
  sentiment:     "Sentiment",
  available:     "Available",
  ml_direction:  "Direction",
  buy_prob:      "Buy Prob",
  sell_prob:     "Sell Prob",
  hold_prob:     "Hold Prob",
};

function formatSignalValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  const n = Number(value);
  if (!Number.isNaN(n)) {
    if (["rsi", "price_change_pct", "buy_prob", "sell_prob", "hold_prob", "confidence"].includes(key)) {
      return `${n.toFixed(2)}${["price_change_pct"].includes(key) ? "%" : ""}`;
    }
    if (["volume"].includes(key)) return n.toLocaleString("en-IN");
    if (["score", "volume_ratio"].includes(key)) return n.toFixed(2);
  }
  return String(value);
}

function SignalPanel({
  icon,
  title,
  data,
  theme,
}: {
  icon: React.ReactNode;
  title: string;
  data: Record<string, unknown>;
  theme: "blue" | "violet" | "indigo";
}) {
  const themeStyles = {
    blue:   { wrap: "bg-blue-50 border-blue-200",   label: "text-blue-900", val: "text-blue-700" },
    violet: { wrap: "bg-violet-50 border-violet-200", label: "text-violet-900", val: "text-violet-700" },
    indigo: { wrap: "bg-indigo-50 border-indigo-200", label: "text-indigo-900", val: "text-indigo-700" },
  }[theme];

  const entries = Object.entries(data).filter(([k]) => !SIGNAL_SKIP_KEYS.has(k));

  return (
    <div className={cn("rounded-lg border p-3", themeStyles.wrap)}>
      <div className="flex items-center gap-2 mb-2.5">
        {icon}
        <p className={cn("text-sm font-semibold", themeStyles.label)}>{title}</p>
      </div>
      {entries.length === 0 ? (
        <p className={cn("text-xs italic", themeStyles.val)}>No signal data</p>
      ) : (
        <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
          {entries.map(([key, value]) => (
            <div key={key} className="flex items-baseline justify-between gap-1 min-w-0">
              <span className={cn("text-[11px] truncate shrink-0", themeStyles.val)}>
                {SIGNAL_KEY_LABELS[key] ?? key.replace(/_/g, " ")}
              </span>
              <span className={cn("text-[11px] font-semibold tabular-nums", themeStyles.label)}>
                {formatSignalValue(key, value)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── ML Predictor breakdown ──────────────────────────────────────────────────
// Renders the ensemble + per-model (XGBoost / GRU) detail that the generic
// SignalPanel would otherwise dump as JSON.  Reads the ml_signal shape produced
// by SignalAssembler.gather_ml_signals (prediction + models sub-dicts).

function fmtPct(value: unknown): string {
  const n = Number(value);
  return Number.isFinite(n) ? `${(n * 100).toFixed(0)}%` : "—";
}

function ProbBar({
  probs,
  compact = false,
}: {
  probs: Record<string, unknown>;
  compact?: boolean;
}) {
  const buy = Number(probs.buy) || 0;
  const sell = Number(probs.sell) || 0;
  const hold = Number(probs.hold) || 0;
  return (
    <div className="space-y-1">
      <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div className="bg-emerald-500" style={{ width: `${buy * 100}%` }} />
        <div className="bg-rose-500" style={{ width: `${sell * 100}%` }} />
        <div className="bg-slate-300" style={{ width: `${hold * 100}%` }} />
      </div>
      {!compact && (
        <div className="flex justify-between text-[10px] tabular-nums">
          <span className="text-emerald-600">Buy {fmtPct(buy)}</span>
          <span className="text-rose-600">Sell {fmtPct(sell)}</span>
          <span className="text-slate-500">Hold {fmtPct(hold)}</span>
        </div>
      )}
    </div>
  );
}

function EnsembleStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-wide text-indigo-400">{label}</span>
      <span className="text-sm font-semibold text-indigo-900 tabular-nums">{value}</span>
    </div>
  );
}

function MLPredictorPanel({ ml }: { ml: Record<string, unknown> }) {
  const prediction = (ml?.prediction ?? {}) as Record<string, unknown>;
  const models = (ml?.models ?? {}) as Record<string, Record<string, unknown> | null>;
  const score = Number(ml?.score) || 0;
  const ensembleDir =
    (prediction.direction as string | undefined) ??
    (score > 0 ? "BUY" : score < 0 ? "SELL" : "HOLD");
  const probs = prediction.probabilities as Record<string, unknown> | undefined;

  const rows = (["xgboost", "gru"] as const)
    .map((key) => ({ key, label: key === "xgboost" ? "XGBoost" : "GRU", m: models[key] }))
    .filter((r): r is { key: "xgboost" | "gru"; label: string; m: Record<string, unknown> } => !!r.m);

  return (
    <div className="rounded-lg border border-indigo-200 bg-indigo-50 p-3">
      <div className="flex items-center gap-2 mb-2.5">
        <Cpu className="h-4 w-4 text-indigo-600" />
        <p className="text-sm font-semibold text-indigo-900">ML Predictor</p>
      </div>

      {!ml?.available ? (
        <p className="text-xs italic text-indigo-700">No ML prediction available</p>
      ) : (
        <>
          {/* Ensemble summary */}
          <div className="grid grid-cols-3 gap-x-4 gap-y-1.5 mb-3">
            <EnsembleStat label="Ensemble" value={ensembleDir} />
            <EnsembleStat label="Confidence" value={fmtPct(ml.confidence)} />
            {prediction.conviction_scale != null && (
              <EnsembleStat label="Conviction" value={fmtPct(prediction.conviction_scale)} />
            )}
          </div>

          {/* Ensemble probability distribution */}
          {probs && (
            <div className="mb-3">
              <ProbBar probs={probs} />
            </div>
          )}

          {/* Per-model breakdown */}
          {rows.length > 0 && (
            <div className="space-y-2">
              {rows.map(({ key, label, m }) => (
                <div
                  key={key}
                  className="rounded-md border border-indigo-100 bg-white/60 px-2.5 py-2"
                >
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-[11px] font-semibold text-indigo-900">{label}</span>
                    <span className="text-[11px] font-semibold text-indigo-700 tabular-nums">
                      {(m.direction as string) ?? "—"}
                      {m.weight != null && (
                        <span className="font-normal text-indigo-400">
                          {" · wt "}
                          {Number(m.weight).toFixed(2)}
                        </span>
                      )}
                    </span>
                  </div>
                  {(m.probabilities as Record<string, unknown> | undefined) && (
                    <ProbBar probs={m.probabilities as Record<string, unknown>} compact />
                  )}
                  <div className="mt-1.5 flex items-center justify-between text-[10px] tabular-nums text-indigo-500">
                    <span>Conviction {fmtPct(m.conviction_scale)}</span>
                    <span>Threshold {fmtPct(m.threshold)}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

interface SuggestionDetailModalProps {
  suggestion: TradeSuggestion | null;
  open: boolean;
  onClose: () => void;
  /**
   * Launches the full DetailPane (live price, chart, trade actions) for this
   * suggestion.  When provided, an "Open full view" action is shown in the footer.
   */
  onOpenFullView?: () => void;
}

export function SuggestionDetailModal({
  suggestion,
  open,
  onClose,
  onOpenFullView,
}: SuggestionDetailModalProps) {
  const timeRemaining = useMemo(() => {
    if (!suggestion) return null;
    
    const now = new Date().getTime();
    const expiry = new Date(suggestion.expires_at).getTime();
    const diff = expiry - now;

    if (diff <= 0) return "Expired";

    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));

    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
  }, [suggestion]);

  if (!suggestion) return null;

  const isBuy = suggestion.signal_direction === "BUY";
  const ticker = suggestion.trading_symbol ?? suggestion.symbol;
  const companyName = suggestion.company_name
    ? suggestion.company_name
        .toLowerCase()
        .split(" ")
        .map((w: string) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(" ")
    : null;

  const confidenceStyles = {
    HIGH:   "bg-emerald-50 border-emerald-300 text-emerald-700",
    MEDIUM: "bg-amber-50   border-amber-300   text-amber-700",
    LOW:    "bg-red-50     border-red-300     text-red-700",
  };
  const confidenceColor = confidenceStyles[suggestion.confidence_level];

  return (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <div className="flex items-start justify-between gap-4">
            <div className="flex-1 min-w-0">
              <DialogTitle className="text-2xl font-bold text-slate-900 leading-none mb-1">
                {ticker}
              </DialogTitle>
              {companyName ? (
                <p className="text-sm text-slate-500">{companyName}</p>
              ) : null}
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <span
                className={cn(
                  "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-bold tracking-wide uppercase",
                  isBuy ? "bg-emerald-600 text-white" : "bg-rose-600 text-white"
                )}
              >
                {isBuy ? (
                  <TrendingUpIcon className="h-4 w-4 stroke-[2.5]" />
                ) : (
                  <TrendingDown className="h-4 w-4 stroke-[2.5]" />
                )}
                {suggestion.signal_direction}
              </span>
              <span
                className={cn(
                  "inline-flex items-center rounded-md border px-2.5 py-1.5 text-sm font-semibold",
                  confidenceColor
                )}
              >
                {suggestion.confidence_level.charAt(0) + suggestion.confidence_level.slice(1).toLowerCase()}
              </span>
            </div>
          </div>
        </DialogHeader>

        <Tabs defaultValue="ai" className="mt-4">
          <TabsList className="grid w-full grid-cols-3">
            <TabsTrigger value="signal">Signal Details</TabsTrigger>
            <TabsTrigger value="ai" className="gap-1.5">
              <Sparkles className="h-3.5 w-3.5" />
              AI Analysis
            </TabsTrigger>
            <TabsTrigger value="fundamentals">Fundamentals</TabsTrigger>
          </TabsList>

          {/* ── Signal Details tab ───────────────────────────────────────── */}
          <TabsContent value="signal">
            <div className="space-y-6 mt-4">
              {/* Consensus Score */}
              <div className="space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-slate-700">Consensus Score</h3>
                  <span className="text-2xl font-bold text-slate-900">
                    {suggestion.consensus_score.toFixed(1)}%
                  </span>
                </div>
                <div className="h-3 bg-slate-100 rounded-full overflow-hidden">
                  <div
                    className={cn(
                      "h-full transition-all rounded-full",
                      suggestion.consensus_score >= 80
                        ? "bg-green-500"
                        : suggestion.consensus_score >= 60
                        ? "bg-yellow-500"
                        : "bg-orange-500",
                    )}
                    style={{ width: `${suggestion.consensus_score}%` }}
                  />
                </div>
              </div>

              {/* Agent Signals */}
              <div className="space-y-3">
                <h3 className="text-sm font-semibold text-slate-700 flex items-center gap-2">
                  <Brain className="h-4 w-4" />
                  Multi-Agent Signals
                </h3>
                <div className="grid gap-3">
                  <SignalPanel
                    icon={<TrendingUpIcon className="h-4 w-4 text-blue-600" />}
                    title="Technical Scanner"
                    data={suggestion.scanner_signal}
                    theme="blue"
                  />
                  <SignalPanel
                    icon={<Brain className="h-4 w-4 text-violet-600" />}
                    title="AI Intelligence"
                    data={suggestion.ai_signal}
                    theme="violet"
                  />
                  <MLPredictorPanel ml={suggestion.ml_signal} />
                </div>
              </div>

              {/* Trade Parameters */}
              {suggestion.entry_price && (
                <div className="space-y-3">
                  <h3 className="text-sm font-semibold text-slate-700 flex items-center gap-2">
                    <Target className="h-4 w-4" />
                    Trade Parameters
                  </h3>
                  <div className="grid grid-cols-2 gap-3">
                    <div className="p-3 bg-slate-50 border border-slate-200 rounded-lg">
                      <p className="text-xs text-slate-500 mb-1">Entry Price</p>
                      <p className="text-lg font-semibold text-slate-900">
                        ₹{suggestion.entry_price.toFixed(2)}
                      </p>
                    </div>
                    {suggestion.stop_loss && (
                      <div className="p-3 bg-red-50 border border-red-200 rounded-lg">
                        <p className="text-xs text-red-600 mb-1 flex items-center gap-1">
                          <Shield className="h-3 w-3" />
                          Stop Loss
                        </p>
                        <p className="text-lg font-semibold text-red-700">
                          ₹{suggestion.stop_loss.toFixed(2)}
                        </p>
                      </div>
                    )}
                  </div>

                  {(suggestion.take_profit_1 ||
                    suggestion.take_profit_2 ||
                    suggestion.take_profit_3) && (
                    <div className="space-y-2">
                      <p className="text-xs font-medium text-slate-600">Take Profit Targets</p>
                      <div className="grid grid-cols-3 gap-2">
                        {suggestion.take_profit_1 && (
                          <div className="p-2 bg-green-50 border border-green-200 rounded text-center">
                            <p className="text-xs text-green-600 mb-0.5">Target 1</p>
                            <p className="text-sm font-semibold text-green-700">
                              ₹{suggestion.take_profit_1.toFixed(2)}
                            </p>
                          </div>
                        )}
                        {suggestion.take_profit_2 && (
                          <div className="p-2 bg-green-50 border border-green-200 rounded text-center">
                            <p className="text-xs text-green-600 mb-0.5">Target 2</p>
                            <p className="text-sm font-semibold text-green-700">
                              ₹{suggestion.take_profit_2.toFixed(2)}
                            </p>
                          </div>
                        )}
                        {suggestion.take_profit_3 && (
                          <div className="p-2 bg-green-50 border border-green-200 rounded text-center">
                            <p className="text-xs text-green-600 mb-0.5">Target 3</p>
                            <p className="text-sm font-semibold text-green-700">
                              ₹{suggestion.take_profit_3.toFixed(2)}
                            </p>
                          </div>
                        )}
                      </div>
                    </div>
                  )}

                  {suggestion.risk_reward_ratio && (
                    <div className="p-3 bg-blue-50 border border-blue-200 rounded-lg">
                      <p className="text-xs text-blue-600 mb-1">Risk/Reward Ratio</p>
                      <p className="text-lg font-semibold text-blue-700">
                        1:{suggestion.risk_reward_ratio.toFixed(2)}
                      </p>
                    </div>
                  )}
                </div>
              )}

              {/* Temporal Metadata */}
              <div className="space-y-3">
                <h3 className="text-sm font-semibold text-slate-700 flex items-center gap-2">
                  <Clock className="h-4 w-4" />
                  Timing
                </h3>
                <div className="grid grid-cols-2 gap-3">
                  <div className="p-3 bg-slate-50 border border-slate-200 rounded-lg">
                    <p className="text-xs text-slate-500 mb-1">Generated</p>
                    <p className="text-sm font-medium text-slate-900">
                      {new Date(suggestion.generated_at).toLocaleString("en-IN", {
                        dateStyle: "medium",
                        timeStyle: "short",
                      })}
                    </p>
                  </div>
                  <div
                    className={cn(
                      "p-3 border rounded-lg",
                      timeRemaining === "Expired"
                        ? "bg-red-50 border-red-200"
                        : "bg-amber-50 border-amber-200",
                    )}
                  >
                    <p
                      className={cn(
                        "text-xs mb-1",
                        timeRemaining === "Expired" ? "text-red-600" : "text-amber-600",
                      )}
                    >
                      {timeRemaining === "Expired" ? "Expired" : "Time Remaining"}
                    </p>
                    <p
                      className={cn(
                        "text-sm font-medium",
                        timeRemaining === "Expired" ? "text-red-700" : "text-amber-700",
                      )}
                    >
                      {timeRemaining}
                    </p>
                  </div>
                </div>
                <div className="p-3 bg-slate-50 border border-slate-200 rounded-lg">
                  <p className="text-xs text-slate-500 mb-1">Trigger Pathway</p>
                  <p className="text-sm font-medium text-slate-900">
                    {suggestion.trigger_pathway.replace(/_/g, " ")}
                  </p>
                </div>
              </div>
            </div>
          </TabsContent>

          {/* ── AI Analysis tab — ML cards + sectioned LLM explanation ──────── */}
          {/* Lazy-mounted: AnalysisCardsSection opens its SSE stream only while
              this tab is active.  Seeded immediately from the suggestion's
              llm_summary / llm_explanation, then kept live via SSE. */}
          <TabsContent value="ai">
            <div className="mt-4">
              <AnalysisCardsSection
                instrumentKey={suggestion.instrument_key}
                symbol={suggestion.trading_symbol ?? suggestion.symbol}
                suggestion={suggestion}
              />
            </div>
          </TabsContent>

          {/* ── Fundamentals tab — lazy mounted (TabsContent returns null when inactive) ── */}
          <TabsContent value="fundamentals">
            <div className="mt-4">
              <FundamentalsTab instrumentKey={suggestion.instrument_key} />
            </div>
          </TabsContent>
        </Tabs>

        {/* Footer Actions */}
        <div className="flex items-center justify-between gap-3 pt-4 border-t border-slate-200 mt-6">
          {onOpenFullView ? (
            <Button variant="ghost" onClick={onOpenFullView} className="gap-1.5">
              Open full view
              <ArrowUpRight className="h-4 w-4" />
            </Button>
          ) : (
            <span />
          )}
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
