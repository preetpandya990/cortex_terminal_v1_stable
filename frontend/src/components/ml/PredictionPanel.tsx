"use client";

import { Brain, ChevronDown, ChevronUp } from "lucide-react";
import { useState } from "react";
import type { CachedPrediction, MLPrediction, PredictionSignal } from "@/types/ml";
import { PredictionBadge } from "./PredictionBadge";

function formatDate(iso: string): string {
  const date = new Date(iso);
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function PredictionPanel({
  prediction,
  history = [],
  isLoading,
}: {
  prediction: CachedPrediction | null;
  history?: MLPrediction[];
  isLoading: boolean;
}) {
  const [isExpanded, setIsExpanded] = useState(false);

  const signal: PredictionSignal =
    prediction?.direction === "long"
      ? "BUY"
      : prediction?.direction === "short"
        ? "SELL"
        : "HOLD";

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white/80 px-4 py-3">
        <Brain className="h-4 w-4 animate-pulse text-blue-500" />
        <span className="text-xs text-slate-500">Loading ML prediction...</span>
      </div>
    );
  }

  if (!prediction) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white/80 px-4 py-3">
        <Brain className="h-4 w-4 text-slate-400" />
        <span className="text-xs text-slate-500">
          No ML prediction available for this stock.
        </span>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white/90 shadow-sm">
      <button
        type="button"
        onClick={() => setIsExpanded((prev) => !prev)}
        className="flex w-full items-center justify-between gap-3 px-4 py-3"
      >
        <div className="flex items-center gap-3">
          <Brain className="h-4 w-4 text-blue-600" />
          <span className="text-xs font-semibold uppercase tracking-widest text-slate-600">
            ML Prediction
          </span>
          <PredictionBadge signal={signal} confidence={prediction.confidence} />
          <span className="text-[11px] text-slate-400">
            Entry @ ₹{prediction.entry_price.toLocaleString("en-IN", {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2,
            })}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-slate-400">
            {formatDate(prediction.prediction_time)}
          </span>
          {isExpanded ? (
            <ChevronUp className="h-4 w-4 text-slate-400" />
          ) : (
            <ChevronDown className="h-4 w-4 text-slate-400" />
          )}
        </div>
      </button>

      {isExpanded && (
        <div className="border-t border-slate-100 px-4 py-3 space-y-3">
          {history.length > 0 && (
            <div className="space-y-2">
              <div className="text-[11px] font-medium text-slate-600">
                Recent Signals
              </div>
              <div className="space-y-1.5">
                {history.slice(0, 6).map((item) => {
                  const itemSignal: PredictionSignal =
                    item.direction === "long"
                      ? "BUY"
                      : item.direction === "short"
                        ? "SELL"
                        : "HOLD";
                  return (
                    <div
                      key={`${item.symbol}-${item.horizon}-${item.prediction_time}`}
                      className="flex items-center justify-between rounded-md border border-slate-100 bg-slate-50/50 px-2 py-1 text-[11px]"
                    >
                      <div className="flex items-center gap-2">
                        <PredictionBadge signal={itemSignal} confidence={item.confidence} />
                        <span className="text-slate-500">
                          {formatDate(item.prediction_time)}
                        </span>
                      </div>
                      <span className="text-slate-600">
                        ₹{Number(item.entry_price ?? 0).toLocaleString("en-IN", {
                          minimumFractionDigits: 2,
                          maximumFractionDigits: 2,
                        })}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Probability meter */}
          <div className="space-y-1">
            <div className="flex items-center justify-between text-[11px]">
              <span className="font-medium text-slate-600">
                Long Probability
              </span>
              <span className="font-semibold text-slate-800">
                {(prediction.probability_long * 100).toFixed(1)}%
              </span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
              <div
                className={`h-full rounded-full transition-all ${
                  prediction.probability_long >= 0.6
                    ? "bg-emerald-500"
                    : prediction.probability_long <= 0.4
                      ? "bg-rose-500"
                      : "bg-amber-500"
                }`}
                style={{
                  width: `${Math.min(100, prediction.probability_long * 100)}%`,
                }}
              />
            </div>
          </div>

          <div className="grid grid-cols-3 gap-3 text-[11px] text-slate-600">
            <div className="space-y-1 rounded-lg border border-slate-100 bg-slate-50/60 px-3 py-2">
              <div className="text-[9px] uppercase tracking-widest text-slate-500">
                Entry
              </div>
              <div className="text-sm font-semibold text-slate-800">
                ₹{prediction.entry_price.toLocaleString("en-IN", {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })}
              </div>
            </div>
            <div className="space-y-1 rounded-lg border border-slate-100 bg-slate-50/60 px-3 py-2">
              <div className="text-[9px] uppercase tracking-widest text-slate-500">
                Target
              </div>
              <div className="text-sm font-semibold text-emerald-700">
                ₹{prediction.target_price.toLocaleString("en-IN", {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })}
              </div>
            </div>
            <div className="space-y-1 rounded-lg border border-slate-100 bg-slate-50/60 px-3 py-2">
              <div className="text-[9px] uppercase tracking-widest text-slate-500">
                Stop
              </div>
              <div className="text-sm font-semibold text-rose-700">
                ₹{prediction.stop_loss.toLocaleString("en-IN", {
                  minimumFractionDigits: 2,
                  maximumFractionDigits: 2,
                })}
              </div>
            </div>
          </div>

          <div className="space-y-1.5">
            <div className="text-[11px] font-medium text-slate-600">
              Reasoning
            </div>
            <p className="text-[12px] text-slate-700">
              {prediction.reasoning ?? "Pattern aligned with your curated strategy."}
            </p>
          </div>

          <div className="space-y-1">
            <div className="text-[11px] font-medium text-slate-600">
              Risk Score
            </div>
            <div className="text-[12px] font-semibold text-slate-800">
              {prediction.risk_score.toFixed(2)}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
