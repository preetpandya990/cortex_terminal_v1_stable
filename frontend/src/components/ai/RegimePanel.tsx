/**
 * Regime Panel Component
 * Displays current market regime with indicators, history timeline, and active strategies
 */
import * as React from "react";
import { format } from "date-fns";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  useCurrentRegime,
  useRegimeHistory,
  useActiveStrategies,
} from "@/hooks/useRegime";
import { RegimeType } from "@/types/regime";
import {
  TrendingUp,
  TrendingDown,
  Activity,
  AlertTriangle,
  Droplets,
  Newspaper,
  RefreshCw,
} from "lucide-react";

const POPULAR_SYMBOLS = [
  { value: 'RELIANCE', label: 'Reliance Industries' },
  { value: 'TCS', label: 'Tata Consultancy Services' },
  { value: 'INFY', label: 'Infosys' },
  { value: 'HDFCBANK', label: 'HDFC Bank' },
  { value: 'ICICIBANK', label: 'ICICI Bank' },
  { value: 'SBIN', label: 'State Bank of India' },
  { value: 'BHARTIARTL', label: 'Bharti Airtel' },
  { value: 'ITC', label: 'ITC Limited' },
  { value: 'KOTAKBANK', label: 'Kotak Mahindra Bank' },
  { value: 'LT', label: 'Larsen & Toubro' },
];

interface RegimePanelProps {
  className?: string;
}

export function RegimePanel({ className }: RegimePanelProps) {
  const [selectedSymbol, setSelectedSymbol] = React.useState('RELIANCE');
  const {
    data: currentRegime,
    isLoading: isLoadingCurrent,
    refetch: refetchCurrent,
    isRefetching: isRefetchingCurrent,
  } = useCurrentRegime(selectedSymbol);

  const {
    data: regimeHistory,
    isLoading: isLoadingHistory,
  } = useRegimeHistory(selectedSymbol, 1);

  const {
    data: activeStrategies,
    isLoading: isLoadingStrategies,
  } = useActiveStrategies(selectedSymbol);

  const getRegimeIcon = (type: RegimeType | null) => {
    if (!type) return <Activity className="size-6" />;
    
    switch (type) {
      case "bull_trending":
        return <TrendingUp className="size-6" />;
      case "bear_trending":
        return <TrendingDown className="size-6" />;
      case "sideways_range":
        return <Activity className="size-6" />;
      case "high_volatility":
        return <AlertTriangle className="size-6" />;
      case "low_liquidity":
        return <Droplets className="size-6" />;
      case "news_driven":
        return <Newspaper className="size-6" />;
      default:
        return <Activity className="size-6" />;
    }
  };

  const getRegimeColor = (type: RegimeType | null) => {
    if (!type) return "bg-gray-500/10 text-gray-700 dark:text-gray-400 border-gray-500/20";
    
    switch (type) {
      case "bull_trending":
        return "bg-green-500/10 text-green-700 dark:text-green-400 border-green-500/20";
      case "bear_trending":
        return "bg-red-500/10 text-red-700 dark:text-red-400 border-red-500/20";
      case "sideways_range":
        return "bg-blue-500/10 text-blue-700 dark:text-blue-400 border-blue-500/20";
      case "high_volatility":
        return "bg-orange-500/10 text-orange-700 dark:text-orange-400 border-orange-500/20";
      case "low_liquidity":
        return "bg-purple-500/10 text-purple-700 dark:text-purple-400 border-purple-500/20";
      case "news_driven":
        return "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400 border-yellow-500/20";
      default:
        return "bg-gray-500/10 text-gray-700 dark:text-gray-400 border-gray-500/20";
    }
  };

  const getRegimeLabel = (type: RegimeType | null) => {
    if (!type) return "Unknown";
    return type
      .split("_")
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ");
  };

  const formatDuration = (minutes: number) => {
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const mins = minutes % 60;
    return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`;
  };

  if (isLoadingCurrent) {
    return (
      <Card className={className}>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Market Regime</CardTitle>
            <Select value={selectedSymbol} onValueChange={setSelectedSymbol}>
              <SelectTrigger className="w-[200px]">
                <SelectValue placeholder="Select symbol" />
              </SelectTrigger>
              <SelectContent>
                {POPULAR_SYMBOLS.map((symbol) => (
                  <SelectItem key={symbol.value} value={symbol.value}>
                    {symbol.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          <div className="flex items-center justify-center py-8">
            <RefreshCw className="size-6 animate-spin text-muted-foreground" />
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!currentRegime || !currentRegime.regime_type) {
    return (
      <Card className={className}>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Market Regime</CardTitle>
            <Select value={selectedSymbol} onValueChange={setSelectedSymbol}>
              <SelectTrigger className="w-[200px]">
                <SelectValue placeholder="Select symbol" />
              </SelectTrigger>
              <SelectContent>
                {POPULAR_SYMBOLS.map((symbol) => (
                  <SelectItem key={symbol.value} value={symbol.value}>
                    {symbol.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            No regime data available for {selectedSymbol}
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className={className}>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Market Regime - {selectedSymbol}</CardTitle>
          <div className="flex items-center gap-2">
            <Select value={selectedSymbol} onValueChange={setSelectedSymbol}>
              <SelectTrigger className="w-[200px]">
                <SelectValue placeholder="Select symbol" />
              </SelectTrigger>
              <SelectContent>
                {POPULAR_SYMBOLS.map((symbol) => (
                  <SelectItem key={symbol.value} value={symbol.value}>
                    {symbol.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              variant="outline"
              size="sm"
              onClick={() => refetchCurrent()}
              disabled={isRefetchingCurrent}
            >
              <RefreshCw
                className={`size-4 ${isRefetchingCurrent ? "animate-spin" : ""}`}
              />
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Current Regime Display */}
        <div className="space-y-4">
          <div className="flex items-center gap-4">
            <div
              className={`flex size-16 items-center justify-center rounded-lg border-2 ${getRegimeColor(currentRegime.regime_type)}`}
            >
              {getRegimeIcon(currentRegime.regime_type)}
            </div>
            <div className="flex-1">
              <h3 className="text-2xl font-bold">
                {getRegimeLabel(currentRegime.regime_type)}
              </h3>
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <span>Confidence: {((currentRegime.confidence ?? 0) * 100).toFixed(1)}%</span>
                {currentRegime.regime_duration_minutes && (
                  <>
                    <span>•</span>
                    <span>Duration: {formatDuration(currentRegime.regime_duration_minutes)}</span>
                  </>
                )}
              </div>
            </div>
          </div>

          {currentRegime.previous_regime && (
            <div className="text-sm text-muted-foreground">
              Previous: {getRegimeLabel(currentRegime.previous_regime)}
            </div>
          )}
        </div>

        {/* Technical Indicators */}
        {currentRegime.indicators && (
          <div>
            <h4 className="mb-3 text-sm font-semibold">Technical Indicators</h4>
            <div className="grid grid-cols-2 gap-4">
              {currentRegime.indicators.adx !== undefined && (
                <div className="rounded-lg border bg-card p-3">
                  <div className="text-xs text-muted-foreground">ADX</div>
                  <div className="text-lg font-semibold">
                    {currentRegime.indicators.adx.toFixed(2)}
                  </div>
                </div>
              )}
              {currentRegime.indicators.atr !== undefined && (
                <div className="rounded-lg border bg-card p-3">
                  <div className="text-xs text-muted-foreground">ATR</div>
                  <div className="text-lg font-semibold">
                    {currentRegime.indicators.atr.toFixed(2)}
                  </div>
                </div>
              )}
              {currentRegime.indicators.bollinger_width !== undefined && (
                <div className="rounded-lg border bg-card p-3">
                  <div className="text-xs text-muted-foreground">Bollinger Width</div>
                  <div className="text-lg font-semibold">
                    {currentRegime.indicators.bollinger_width.toFixed(2)}
                  </div>
                </div>
              )}
              {currentRegime.indicators.rsi !== undefined && (
                <div className="rounded-lg border bg-card p-3">
                  <div className="text-xs text-muted-foreground">RSI</div>
                  <div className="text-lg font-semibold">
                    {currentRegime.indicators.rsi.toFixed(2)}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Regime History Timeline */}
        {!isLoadingHistory && regimeHistory?.history && regimeHistory.history.length > 0 && (
          <div>
            <h4 className="mb-3 text-sm font-semibold">24-Hour History</h4>
            <div className="space-y-2">
              {regimeHistory.history.slice(0, 10).map((entry, index) => (
                <div
                  key={`${entry.detected_at}-${index}`}
                  className="flex items-center gap-3 rounded-lg border bg-card p-3"
                >
                  <div
                    className={`flex size-10 items-center justify-center rounded border ${getRegimeColor(entry.regime_type)}`}
                  >
                    {getRegimeIcon(entry.regime_type)}
                  </div>
                  <div className="flex-1">
                    <div className="text-sm font-medium">
                      {getRegimeLabel(entry.regime_type)}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {format(new Date(entry.detected_at), "MMM d, HH:mm")}
                      {entry.duration_minutes && (
                        <> • {formatDuration(entry.duration_minutes)}</>
                      )}
                    </div>
                  </div>
                  <div className="text-sm text-muted-foreground">
                    {((entry.confidence ?? 0) * 100).toFixed(0)}%
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Active Strategies */}
        {!isLoadingStrategies &&
          activeStrategies?.strategies &&
          activeStrategies.strategies.length > 0 && (
            <div>
              <h4 className="mb-3 text-sm font-semibold">Active Strategies</h4>
              <div className="space-y-2">
                {activeStrategies.strategies.map((strategy) => (
                  <div
                    key={strategy.strategy_id}
                    className="flex items-center justify-between rounded-lg border bg-card p-3"
                  >
                    <div>
                      <div className="text-sm font-medium">
                        {strategy.strategy_name}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        Activated: {format(new Date(strategy.activated_at), "MMM d, HH:mm")}
                      </div>
                    </div>
                    <Badge
                      variant="outline"
                      className={getRegimeColor(strategy.regime_type)}
                    >
                      {getRegimeLabel(strategy.regime_type)}
                    </Badge>
                  </div>
                ))}
              </div>
            </div>
          )}
      </CardContent>
    </Card>
  );
}
