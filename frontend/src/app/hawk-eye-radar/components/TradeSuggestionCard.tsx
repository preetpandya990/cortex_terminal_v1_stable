"use client";

import { memo, useMemo } from "react";
import { ArrowUpRight, ArrowDownRight, CheckCircle2, Clock } from "lucide-react";
import { Card, CardContent, CardFooter, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { TradeSuggestion } from "@/types/trade_suggestions";
import { CONFIDENCE_COLORS, DIRECTION_COLORS } from "@/types/trade_suggestions";

interface TradeSuggestionCardProps {
  suggestion: TradeSuggestion;
  onViewDetails?: (suggestionId: string) => void;
  className?: string;
}

function TradeSuggestionCardComponent({
  suggestion,
  onViewDetails,
  className,
}: TradeSuggestionCardProps) {
  const isBuy = suggestion.signal_direction === "BUY";
  const directionColor = isBuy ? "text-green-600" : "text-red-600";
  const directionBg = isBuy ? "bg-green-50" : "bg-red-50";
  const directionBorder = isBuy ? "border-green-200" : "border-red-200";

  const confidenceColor = useMemo(() => {
    const level = suggestion.confidence_level;
    if (level === "HIGH") return "bg-green-100 text-green-700 border-green-200";
    if (level === "MEDIUM") return "bg-yellow-100 text-yellow-700 border-yellow-200";
    return "bg-orange-100 text-orange-700 border-orange-200";
  }, [suggestion.confidence_level]);

  const timeRemaining = useMemo(() => {
    const now = new Date().getTime();
    const expiry = new Date(suggestion.expires_at).getTime();
    const diff = expiry - now;

    if (diff <= 0) return "Expired";

    const hours = Math.floor(diff / (1000 * 60 * 60));
    const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));

    if (hours > 0) return `${hours}h ${minutes}m`;
    return `${minutes}m`;
  }, [suggestion.expires_at]);

  const isExpired = timeRemaining === "Expired";

  return (
    <Card
      className={cn(
        "border-slate-200 bg-white transition-all hover:shadow-md",
        isExpired && "opacity-60",
        className
      )}
    >
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 mb-1">
              <h3 className="text-lg font-semibold text-slate-900 truncate">
                {suggestion.symbol}
              </h3>
              <Badge
                className={cn(
                  "flex items-center gap-1 border",
                  isBuy
                    ? "bg-green-50 text-green-700 border-green-200"
                    : "bg-red-50 text-red-700 border-red-200"
                )}
              >
                {isBuy ? (
                  <ArrowUpRight className="h-3 w-3" />
                ) : (
                  <ArrowDownRight className="h-3 w-3" />
                )}
                {suggestion.signal_direction}
              </Badge>
            </div>
            <p className="text-xs text-slate-500 truncate">
              {suggestion.trading_symbol || suggestion.instrument_key}
            </p>
          </div>
          <Badge className={cn("border", confidenceColor)}>
            {suggestion.confidence_level}
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="space-y-4 pb-3">
        {/* Consensus Score Bar */}
        <div className="space-y-1.5">
          <div className="flex items-center justify-between text-xs">
            <span className="font-medium text-slate-600">Consensus Score</span>
            <span className="font-semibold text-slate-900">
              {suggestion.consensus_score.toFixed(1)}%
            </span>
          </div>
          <div className="h-2 w-full rounded-full bg-slate-100 overflow-hidden">
            <div
              className={cn(
                "h-full rounded-full transition-all",
                suggestion.consensus_score >= 80
                  ? "bg-green-500"
                  : suggestion.consensus_score >= 60
                    ? "bg-yellow-500"
                    : "bg-orange-500"
              )}
              style={{ width: `${suggestion.consensus_score}%` }}
            />
          </div>
        </div>

        {/* Agent Signals */}
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-slate-500">Validated by:</span>
          <div className="flex items-center gap-1.5">
            <Badge variant="outline" className="text-xs gap-1 bg-blue-50 border-blue-200 text-blue-700">
              <CheckCircle2 className="h-3 w-3" />
              Scanner
            </Badge>
            <Badge variant="outline" className="text-xs gap-1 bg-purple-50 border-purple-200 text-purple-700">
              <CheckCircle2 className="h-3 w-3" />
              AI
            </Badge>
            <Badge variant="outline" className="text-xs gap-1 bg-indigo-50 border-indigo-200 text-indigo-700">
              <CheckCircle2 className="h-3 w-3" />
              ML
            </Badge>
          </div>
        </div>

        {/* Trade Parameters */}
        {suggestion.entry_price && (
          <div className="grid grid-cols-2 gap-3 pt-2 border-t border-slate-100">
            <div>
              <p className="text-xs text-slate-500 mb-0.5">Entry Price</p>
              <p className="text-sm font-semibold text-slate-900">
                ₹{suggestion.entry_price.toFixed(2)}
              </p>
            </div>
            {suggestion.stop_loss && (
              <div>
                <p className="text-xs text-slate-500 mb-0.5">Stop Loss</p>
                <p className="text-sm font-semibold text-slate-900">
                  ₹{suggestion.stop_loss.toFixed(2)}
                </p>
              </div>
            )}
            {suggestion.risk_reward_ratio && (
              <div className="col-span-2">
                <p className="text-xs text-slate-500 mb-0.5">Risk/Reward</p>
                <p className="text-sm font-semibold text-green-600">
                  1:{suggestion.risk_reward_ratio.toFixed(1)}
                </p>
              </div>
            )}
          </div>
        )}
      </CardContent>

      <CardFooter className="flex items-center justify-between pt-3 border-t border-slate-100">
        <div className="flex items-center gap-1.5 text-xs text-slate-500">
          <Clock className="h-3.5 w-3.5" />
          <span className={cn(isExpired && "text-red-600 font-medium")}>
            {isExpired ? "Expired" : `Expires in ${timeRemaining}`}
          </span>
        </div>
        <Button
          size="sm"
          variant="outline"
          onClick={() => onViewDetails?.(suggestion.suggestion_id)}
          disabled={isExpired}
          className="text-xs"
        >
          View Details
        </Button>
      </CardFooter>
    </Card>
  );
}

export const TradeSuggestionCard = memo(TradeSuggestionCardComponent);
