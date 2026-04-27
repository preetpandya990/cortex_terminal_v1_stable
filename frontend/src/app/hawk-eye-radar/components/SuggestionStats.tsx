"use client";

import { useQuery } from "@tanstack/react-query";
import { TrendingUp, TrendingDown, Target, Zap, Clock, CheckCircle2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { tradeSuggestionsAPI } from "@/lib/api";

export function SuggestionStats() {
  const { data: stats, isLoading } = useQuery({
    queryKey: ["trade-suggestions-stats"],
    queryFn: () => tradeSuggestionsAPI.getStats(),
    refetchInterval: 60000, // Refetch every minute
  });

  if (isLoading || !stats) {
    return (
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        {[...Array(4)].map((_, i) => (
          <Card key={i} className="border-slate-200 bg-white animate-pulse">
            <CardHeader className="pb-2">
              <div className="h-4 w-24 bg-slate-200 rounded" />
            </CardHeader>
            <CardContent>
              <div className="h-8 w-16 bg-slate-200 rounded" />
            </CardContent>
          </Card>
        ))}
      </div>
    );
  }

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
      {/* Total Active */}
      <Card className="border-slate-200 bg-white">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-slate-600 flex items-center gap-2">
            <Target className="h-4 w-4" />
            Active Suggestions
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold text-slate-900">{stats.total_active}</div>
          <p className="text-xs text-slate-500 mt-1">
            {stats.total_today} generated today
          </p>
        </CardContent>
      </Card>

      {/* High Confidence */}
      <Card className="border-green-200 bg-green-50">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-green-700 flex items-center gap-2">
            <CheckCircle2 className="h-4 w-4" />
            High Confidence
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold text-green-900">{stats.high_confidence_count}</div>
          <p className="text-xs text-green-600 mt-1">
            {stats.total_active > 0
              ? `${((stats.high_confidence_count / stats.total_active) * 100).toFixed(0)}% of active`
              : "No active suggestions"}
          </p>
        </CardContent>
      </Card>

      {/* Direction Split */}
      <Card className="border-slate-200 bg-white">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-slate-600 flex items-center gap-2">
            <Zap className="h-4 w-4" />
            Direction Split
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-3">
            <div className="flex items-center gap-1">
              <TrendingUp className="h-4 w-4 text-green-600" />
              <span className="text-lg font-bold text-green-700">{stats.buy_count}</span>
            </div>
            <div className="text-slate-400">/</div>
            <div className="flex items-center gap-1">
              <TrendingDown className="h-4 w-4 text-red-600" />
              <span className="text-lg font-bold text-red-700">{stats.sell_count}</span>
            </div>
          </div>
          <p className="text-xs text-slate-500 mt-1">BUY / SELL signals</p>
        </CardContent>
      </Card>

      {/* Avg Consensus */}
      <Card className="border-blue-200 bg-blue-50">
        <CardHeader className="pb-2">
          <CardTitle className="text-sm font-medium text-blue-700 flex items-center gap-2">
            <Clock className="h-4 w-4" />
            Avg Consensus
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="text-2xl font-bold text-blue-900">
            {stats.avg_consensus_score.toFixed(1)}%
          </div>
          <p className="text-xs text-blue-600 mt-1">
            {stats.avg_latency_ms.toFixed(0)}ms avg latency
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
