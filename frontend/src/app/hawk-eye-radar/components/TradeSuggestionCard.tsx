"use client";

import { memo, useMemo } from "react";
import {
  TrendingUp,
  TrendingDown,
  CheckCircle2,
  Clock,
  ShieldCheck,
  ShieldX,
  ChevronRight,
} from "lucide-react";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { TradeSuggestion, StrategyComplianceInfo } from "@/types/trade_suggestions";

interface TradeSuggestionCardProps {
  suggestion: TradeSuggestion;
  onViewDetails?: (suggestionId: string) => void;
  className?: string;
}

function toTitleCase(str: string): string {
  return str
    .toLowerCase()
    .split(" ")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

function StrategyBadge({ compliance }: { compliance: StrategyComplianceInfo }) {
  const title = compliance.passed
    ? `Passes the validation criteria of ${compliance.strategy_name}`
    : `Does not pass the validation criteria of ${compliance.strategy_name}`;

  return (
    <Badge
      variant="outline"
      title={title}
      className={cn(
        "flex items-center gap-1 border text-xs cursor-default select-none",
        compliance.passed
          ? "bg-emerald-50 border-emerald-200 text-emerald-700"
          : "bg-amber-50 border-amber-200 text-amber-700"
      )}
    >
      {compliance.passed ? (
        <ShieldCheck className="h-3 w-3 shrink-0" />
      ) : (
        <ShieldX className="h-3 w-3 shrink-0" />
      )}
      <span className="max-w-[80px] truncate">{compliance.strategy_name}</span>
    </Badge>
  );
}

function DirectionPill({ direction }: { direction: "BUY" | "SELL" }) {
  const isBuy = direction === "BUY";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-xs font-bold tracking-wide uppercase",
        isBuy
          ? "bg-emerald-600 text-white"
          : "bg-rose-600 text-white"
      )}
    >
      {isBuy ? (
        <TrendingUp className="h-3.5 w-3.5 stroke-[2.5]" />
      ) : (
        <TrendingDown className="h-3.5 w-3.5 stroke-[2.5]" />
      )}
      {direction}
    </span>
  );
}

function ConfidencePill({ level }: { level: "HIGH" | "MEDIUM" | "LOW" }) {
  const styles = {
    HIGH:   "bg-emerald-50 border-emerald-300 text-emerald-700",
    MEDIUM: "bg-amber-50  border-amber-300  text-amber-700",
    LOW:    "bg-red-50    border-red-300    text-red-700",
  };
  const labels = { HIGH: "High", MEDIUM: "Medium", LOW: "Low" };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-semibold",
        styles[level]
      )}
    >
      {labels[level]}
    </span>
  );
}

function ConsensusBar({ score }: { score: number }) {
  const color =
    score >= 80 ? "bg-emerald-500" : score >= 60 ? "bg-amber-500" : "bg-rose-400";
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-slate-500">Consensus</span>
        <span className="text-xs font-bold tabular-nums text-slate-800">
          {score.toFixed(1)}%
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
        <div
          className={cn("h-full rounded-full transition-all duration-500", color)}
          style={{ width: `${score}%` }}
        />
      </div>
    </div>
  );
}

function AgentRow() {
  return (
    <div className="flex items-center gap-1.5">
      <span className="text-xs text-slate-400 mr-0.5">Signals</span>
      <Badge
        variant="outline"
        className="gap-1 border-blue-200 bg-blue-50 text-blue-700 text-[11px] py-0"
      >
        <CheckCircle2 className="h-2.5 w-2.5" />
        Scanner
      </Badge>
      <Badge
        variant="outline"
        className="gap-1 border-violet-200 bg-violet-50 text-violet-700 text-[11px] py-0"
      >
        <CheckCircle2 className="h-2.5 w-2.5" />
        AI
      </Badge>
      <Badge
        variant="outline"
        className="gap-1 border-indigo-200 bg-indigo-50 text-indigo-700 text-[11px] py-0"
      >
        <CheckCircle2 className="h-2.5 w-2.5" />
        ML
      </Badge>
    </div>
  );
}

function PriceGrid({ suggestion }: { suggestion: TradeSuggestion }) {
  if (!suggestion.entry_price) return null;
  return (
    <div className="grid grid-cols-3 gap-2 rounded-lg border border-slate-100 bg-slate-50/60 p-2.5">
      <div>
        <p className="text-[10px] uppercase tracking-wide text-slate-400 mb-0.5">Entry</p>
        <p className="text-sm font-semibold text-slate-900 tabular-nums">
          ₹{suggestion.entry_price.toFixed(2)}
        </p>
      </div>
      {suggestion.stop_loss ? (
        <div>
          <p className="text-[10px] uppercase tracking-wide text-slate-400 mb-0.5">Stop</p>
          <p className="text-sm font-semibold text-rose-600 tabular-nums">
            ₹{suggestion.stop_loss.toFixed(2)}
          </p>
        </div>
      ) : <div />}
      {suggestion.risk_reward_ratio ? (
        <div>
          <p className="text-[10px] uppercase tracking-wide text-slate-400 mb-0.5">R/R</p>
          <p className="text-sm font-semibold text-emerald-600 tabular-nums">
            1:{suggestion.risk_reward_ratio.toFixed(1)}
          </p>
        </div>
      ) : <div />}
    </div>
  );
}

/**
 * Renders the LLM-generated 2–3 sentence summary beneath the price grid.
 *
 * Rendering contract (mirrors backend nullable semantics):
 *   undefined → field absent (old cache / pre-LLM API response) — hide
 *   null      → explanation pending (worker in progress) — show skeleton
 *               but only for active suggestions (expired will never be explained)
 *   string    → explanation ready — render text
 */
function LLMSummarySection({
  summary,
  status,
}: {
  summary: string | null | undefined;
  status: string;
}) {
  // Field absent from the API response — hide entirely
  if (summary === undefined) return null;

  // Explanation will never be generated for non-active suggestions
  if (summary === null && status !== 'active') return null;

  // Explanation pending — animated skeleton placeholder
  if (summary === null) {
    return (
      <div
        className="space-y-1.5 pt-2 border-t border-slate-100"
        role="status"
        aria-label="Loading AI explanation"
        aria-busy="true"
      >
        <div className="h-2.5 w-full animate-pulse rounded bg-slate-100" />
        <div className="h-2.5 w-[82%] animate-pulse rounded bg-slate-100" />
      </div>
    );
  }

  // Explanation ready
  return (
    <p className="text-xs text-slate-500 leading-relaxed pt-2 border-t border-slate-100 line-clamp-3">
      {summary}
    </p>
  );
}

function TradeSuggestionCardComponent({
  suggestion,
  onViewDetails,
  className,
}: TradeSuggestionCardProps) {
  const isBuy = suggestion.signal_direction === "BUY";

  const ticker = suggestion.trading_symbol ?? suggestion.symbol;
  const companyName = suggestion.company_name
    ? toTitleCase(suggestion.company_name)
    : null;

  const timeRemaining = useMemo(() => {
    const diff = new Date(suggestion.expires_at).getTime() - Date.now();
    if (diff <= 0) return "Expired";
    const h = Math.floor(diff / 3_600_000);
    const m = Math.floor((diff % 3_600_000) / 60_000);
    return h > 0 ? `${h}h ${m}m` : `${m}m`;
  }, [suggestion.expires_at]);

  const isExpired = timeRemaining === "Expired";

  return (
    <Card
      onClick={() => !isExpired && onViewDetails?.(suggestion.suggestion_id)}
      className={cn(
        "relative overflow-hidden border border-slate-200 bg-white transition-all duration-200",
        "hover:border-slate-300 hover:shadow-[0_4px_20px_-4px_rgba(0,0,0,0.12)]",
        !isExpired && "cursor-pointer",
        isExpired && "opacity-60",
        className
      )}
    >
      {/* Accent bar */}
      <div
        className={cn(
          "absolute inset-y-0 left-0 w-[3px]",
          isBuy ? "bg-emerald-500" : "bg-rose-500"
        )}
      />
      {/* ── Header ── */}
      <CardHeader className="pb-3 pt-4 px-4">
        <div className="flex items-start justify-between gap-3">
          {/* Left: ticker + company name */}
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2.5 flex-wrap">
              <span className="text-xl font-bold tracking-tight text-slate-900 leading-none">
                {ticker}
              </span>
              <DirectionPill direction={suggestion.signal_direction} />
            </div>
            {companyName ? (
              <p className="mt-1 text-xs text-slate-500 leading-snug truncate">
                {companyName}
              </p>
            ) : null}
          </div>

          {/* Right: confidence + strategy badge */}
          <div className="flex flex-col items-end gap-1.5 shrink-0">
            <ConfidencePill level={suggestion.confidence_level} />
            {suggestion.strategy_compliance ? (
              <StrategyBadge compliance={suggestion.strategy_compliance} />
            ) : null}
          </div>
        </div>
      </CardHeader>

      {/* ── Body ── */}
      <CardContent className="space-y-3 px-4 pb-3">
        <ConsensusBar score={suggestion.consensus_score} />
        <AgentRow />
        <PriceGrid suggestion={suggestion} />
        <LLMSummarySection
          summary={suggestion.llm_summary}
          status={suggestion.status}
        />
      </CardContent>

      {/* ── Footer ── */}
      <CardFooter className="flex items-center justify-between px-4 py-3 border-t border-slate-100">
        <div className="flex items-center gap-1.5 text-xs text-slate-400">
          <Clock className="h-3 w-3 shrink-0" />
          <span className={cn(isExpired ? "text-rose-600 font-semibold" : "")}>
            {isExpired ? "Expired" : `Expires in ${timeRemaining}`}
          </span>
        </div>
        <span className="flex items-center gap-0.5 text-xs text-slate-400">
          View details
          <ChevronRight className="h-3.5 w-3.5" />
        </span>
      </CardFooter>
    </Card>
  );
}

export const TradeSuggestionCard = memo(TradeSuggestionCardComponent);
