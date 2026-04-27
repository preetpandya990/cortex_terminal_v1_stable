'use client';

/**
 * Analysis Cards Section
 * Displays ML predictions, trend analysis, and volatility metrics
 */

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { TrendingUp, Activity, BarChart3, TrendingDown, Minus, Loader2 } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";

interface AnalysisCardsSectionProps {
  instrumentKey: string | null;
  className?: string;
}

interface MLPrediction {
  symbol: string;
  available: boolean;
  unavailable_reason?: string | null;
  direction?: string | null;
  confidence?: number | null;
  entry_price?: number | null;
  stop_loss?: number | null;
  take_profit_1?: number | null;
  take_profit_2?: number | null;
  take_profit_3?: number | null;
  volatility?: number | null;
  probabilities?: { up: number; down: number; hold: number } | null;
  model_version?: string | null;
  predicted_at?: string | null;
}

interface HawkEyeAnalysis {
  instrument_key: string;
  timeframe: string;
  indicators: {
    rsi: number;
    macd: { value: number; signal: number; histogram: number };
    moving_averages: { sma_20: number; sma_50: number; ema_20: number };
    bollinger_bands: { upper: number; middle: number; lower: number };
  };
  signals: {
    trend: string;
    strength: string;
    recommendation: string;
  };
}

export function AnalysisCardsSection({ instrumentKey, className }: AnalysisCardsSectionProps) {
  const { isAuthenticated, isAuthReady } = useAuth();
  const canQuery = isAuthReady && isAuthenticated && !!instrumentKey;

  // Fetch ML prediction
  const mlQuery = useQuery({
    queryKey: ["ml-prediction", instrumentKey],
    queryFn: async () => {
      const response = await api.post(`/ml/predict`, {
        symbol: instrumentKey,
        timeframe: "1d",
      });
      return response.data as MLPrediction;
    },
    enabled: canQuery,
    staleTime: 60_000, // 1 minute
  });

  // Fetch Hawk-Eye analysis
  const analysisQuery = useQuery({
    queryKey: ["hawk-eye-analysis", instrumentKey],
    queryFn: async () => {
      const response = await api.get(`/hawk-eye/analyze`, {
        params: {
          instrument_key: instrumentKey,
          timeframe: "1d",
        },
      });
      return response.data as HawkEyeAnalysis;
    },
    enabled: canQuery,
    staleTime: 300_000, // 5 minutes
  });

  if (!instrumentKey) {
    return null;
  }

  // Map backend direction to display format
  const getDirectionColor = (direction: string | null | undefined) => {
    const dir = direction?.toUpperCase();
    if (dir === "BUY") return "text-emerald-600";
    if (dir === "SELL") return "text-red-600";
    return "text-slate-600";
  };

  const getDirectionBgColor = (direction: string | null | undefined) => {
    const dir = direction?.toUpperCase();
    if (dir === "BUY") return "bg-emerald-500";
    if (dir === "SELL") return "bg-red-500";
    return "bg-slate-400";
  };

  const getTrendIcon = (trend: string) => {
    if (trend === "bullish") return <TrendingUp className="h-5 w-5 text-emerald-600" />;
    if (trend === "bearish") return <TrendingDown className="h-5 w-5 text-red-600" />;
    return <Minus className="h-5 w-5 text-slate-400" />;
  };

  const getTrendColor = (trend: string) => {
    if (trend === "bullish") return "text-emerald-600";
    if (trend === "bearish") return "text-red-600";
    return "text-slate-600";
  };

  const getRecommendationColor = (rec: string) => {
    if (rec === "buy" || rec === "strong_buy") return "bg-emerald-100 text-emerald-700";
    if (rec === "sell" || rec === "strong_sell") return "bg-red-100 text-red-700";
    return "bg-slate-100 text-slate-700";
  };

  return (
    <div className={className}>
      <div className="grid gap-4 md:grid-cols-3">
        {/* Trend Analysis Card */}
        <Card className="border-slate-200/80 bg-white/90">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              {analysisQuery.data ? getTrendIcon(analysisQuery.data.signals.trend) : <TrendingUp className="h-5 w-5 text-slate-400" />}
              Trend Analysis
            </CardTitle>
            <CardDescription>AI-powered trend detection</CardDescription>
          </CardHeader>
          <CardContent>
            {analysisQuery.isLoading ? (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading analysis...
              </div>
            ) : analysisQuery.data ? (
              <div className="space-y-3 text-sm">
                <div>
                  <span className="text-slate-600">Trend: </span>
                  <span className={`font-semibold capitalize ${getTrendColor(analysisQuery.data.signals.trend)}`}>
                    {analysisQuery.data.signals.trend}
                  </span>
                </div>
                <div>
                  <span className="text-slate-600">Strength: </span>
                  <span className="font-semibold capitalize">{analysisQuery.data.signals.strength}</span>
                </div>
                <div>
                  <span className="text-slate-600">Recommendation: </span>
                  <span className={`inline-block rounded px-2 py-1 text-xs font-semibold uppercase ${getRecommendationColor(analysisQuery.data.signals.recommendation)}`}>
                    {analysisQuery.data.signals.recommendation}
                  </span>
                </div>
              </div>
            ) : (
              <p className="text-sm text-slate-400">Unable to load analysis</p>
            )}
          </CardContent>
        </Card>

        {/* Technical Indicators Card */}
        <Card className="border-slate-200/80 bg-white/90">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <Activity className="h-5 w-5 text-blue-600" />
              Technical Indicators
            </CardTitle>
            <CardDescription>Key market indicators</CardDescription>
          </CardHeader>
          <CardContent>
            {analysisQuery.isLoading ? (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading indicators...
              </div>
            ) : analysisQuery.data ? (
              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-slate-600">RSI:</span>
                  <span className="font-semibold">{analysisQuery.data.indicators.rsi.toFixed(2)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-600">MACD:</span>
                  <span className="font-semibold">{analysisQuery.data.indicators.macd.value.toFixed(2)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-600">SMA 20:</span>
                  <span className="font-semibold">₹{analysisQuery.data.indicators.moving_averages.sma_20.toFixed(2)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-600">EMA 20:</span>
                  <span className="font-semibold">₹{analysisQuery.data.indicators.moving_averages.ema_20.toFixed(2)}</span>
                </div>
              </div>
            ) : (
              <p className="text-sm text-slate-400">Unable to load indicators</p>
            )}
          </CardContent>
        </Card>

        {/* ML Predictions Card */}
        <Card className="border-slate-200/80 bg-white/90">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <BarChart3 className="h-5 w-5 text-purple-600" />
              ML Predictions
            </CardTitle>
            <CardDescription>Machine learning forecasts</CardDescription>
          </CardHeader>
          <CardContent>
            {mlQuery.isLoading ? (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading prediction...
              </div>
            ) : mlQuery.data && !mlQuery.data.available ? (
              <div className="space-y-1">
                <p className="text-sm text-slate-500">No historical data ingested yet.</p>
                <p className="text-xs text-slate-400">
                  Run the data ingestion pipeline to enable ML predictions for this symbol.
                </p>
              </div>
            ) : mlQuery.data?.available && mlQuery.data.direction != null ? (
              <div className="space-y-3 text-sm">
                <div>
                  <span className="text-slate-600">Direction: </span>
                  <span className={`font-semibold capitalize ${getDirectionColor(mlQuery.data.direction)}`}>
                    {mlQuery.data.direction}
                  </span>
                </div>
                <div>
                  <span className="text-slate-600">Confidence: </span>
                  <span className="font-semibold">{((mlQuery.data.confidence ?? 0) * 100).toFixed(1)}%</span>
                </div>
                <div className="pt-1">
                  <div className="h-2 w-full rounded-full bg-slate-200">
                    <div
                      className={`h-2 rounded-full transition-all ${getDirectionBgColor(mlQuery.data.direction)}`}
                      style={{ width: `${(mlQuery.data.confidence ?? 0) * 100}%` }}
                    />
                  </div>
                </div>
                {mlQuery.data.entry_price != null && mlQuery.data.stop_loss != null && (
                  <div className="flex justify-between text-xs text-slate-500">
                    <span>Entry ₹{mlQuery.data.entry_price.toFixed(2)}</span>
                    <span>SL ₹{mlQuery.data.stop_loss.toFixed(2)}</span>
                  </div>
                )}
                {mlQuery.data.model_version && (
                  <div className="text-xs text-slate-400">{mlQuery.data.model_version}</div>
                )}
              </div>
            ) : (
              <p className="text-sm text-slate-400">No prediction available</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
