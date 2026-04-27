"use client";

import { useMemo } from "react";
import { ArrowUpRight, ArrowDownRight, X, Clock, TrendingUp, Brain, Cpu, Target, Shield } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { TradeSuggestion } from "@/types/trade_suggestions";

interface SuggestionDetailModalProps {
  suggestion: TradeSuggestion | null;
  open: boolean;
  onClose: () => void;
}

export function SuggestionDetailModal({
  suggestion,
  open,
  onClose,
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
  const confidenceColor = {
    HIGH: "bg-green-100 text-green-700 border-green-200",
    MEDIUM: "bg-yellow-100 text-yellow-700 border-yellow-200",
    LOW: "bg-orange-100 text-orange-700 border-orange-200",
  }[suggestion.confidence_level];

  return (
    <Dialog open={open} onOpenChange={(isOpen) => !isOpen && onClose()}>
      <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <div className="flex items-start justify-between gap-4">
            <div className="flex-1">
              <DialogTitle className="text-2xl font-bold text-slate-900 mb-2">
                {suggestion.symbol}
              </DialogTitle>
              <p className="text-sm text-slate-500">
                {suggestion.trading_symbol || suggestion.instrument_key}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <Badge
                className={cn(
                  "flex items-center gap-1 border",
                  isBuy
                    ? "bg-green-50 text-green-700 border-green-200"
                    : "bg-red-50 text-red-700 border-red-200"
                )}
              >
                {isBuy ? (
                  <ArrowUpRight className="h-4 w-4" />
                ) : (
                  <ArrowDownRight className="h-4 w-4" />
                )}
                {suggestion.signal_direction}
              </Badge>
              <Badge className={cn("border", confidenceColor)}>
                {suggestion.confidence_level}
              </Badge>
            </div>
          </div>
        </DialogHeader>

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
                    : "bg-orange-500"
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
              {/* Scanner Signal */}
              <div className="flex items-start gap-3 p-3 bg-blue-50 border border-blue-200 rounded-lg">
                <TrendingUp className="h-5 w-5 text-blue-600 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-blue-900">Technical Scanner</p>
                  <div className="mt-1 text-xs text-blue-700 space-y-1">
                    {Object.entries(suggestion.scanner_signal).map(([key, value]) => (
                      <div key={key} className="flex items-center gap-2">
                        <span className="font-medium">{key}:</span>
                        <span>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>

              {/* AI Signal */}
              <div className="flex items-start gap-3 p-3 bg-purple-50 border border-purple-200 rounded-lg">
                <Brain className="h-5 w-5 text-purple-600 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-purple-900">AI Intelligence</p>
                  <div className="mt-1 text-xs text-purple-700 space-y-1">
                    {Object.entries(suggestion.ai_signal).map(([key, value]) => (
                      <div key={key} className="flex items-center gap-2">
                        <span className="font-medium">{key}:</span>
                        <span>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>

              {/* ML Signal */}
              <div className="flex items-start gap-3 p-3 bg-indigo-50 border border-indigo-200 rounded-lg">
                <Cpu className="h-5 w-5 text-indigo-600 mt-0.5" />
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-indigo-900">ML Predictor</p>
                  <div className="mt-1 text-xs text-indigo-700 space-y-1">
                    {Object.entries(suggestion.ml_signal).map(([key, value]) => (
                      <div key={key} className="flex items-center gap-2">
                        <span className="font-medium">{key}:</span>
                        <span>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
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

              {/* Take Profit Targets */}
              {(suggestion.take_profit_1 || suggestion.take_profit_2 || suggestion.take_profit_3) && (
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

              {/* Risk/Reward Ratio */}
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
                  {new Date(suggestion.generated_at).toLocaleString('en-IN', {
                    dateStyle: 'medium',
                    timeStyle: 'short',
                  })}
                </p>
              </div>
              <div className={cn(
                "p-3 border rounded-lg",
                timeRemaining === "Expired"
                  ? "bg-red-50 border-red-200"
                  : "bg-amber-50 border-amber-200"
              )}>
                <p className={cn(
                  "text-xs mb-1",
                  timeRemaining === "Expired" ? "text-red-600" : "text-amber-600"
                )}>
                  {timeRemaining === "Expired" ? "Expired" : "Time Remaining"}
                </p>
                <p className={cn(
                  "text-sm font-medium",
                  timeRemaining === "Expired" ? "text-red-700" : "text-amber-700"
                )}>
                  {timeRemaining}
                </p>
              </div>
            </div>
            <div className="p-3 bg-slate-50 border border-slate-200 rounded-lg">
              <p className="text-xs text-slate-500 mb-1">Trigger Pathway</p>
              <p className="text-sm font-medium text-slate-900">
                {suggestion.trigger_pathway.replace(/_/g, ' ')}
              </p>
            </div>
          </div>
        </div>

        {/* Footer Actions */}
        <div className="flex items-center justify-end gap-3 pt-4 border-t border-slate-200 mt-6">
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
