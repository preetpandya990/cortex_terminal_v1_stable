/**
 * Signals Panel Component
 * Displays real-time trading signals with filtering capabilities
 */
import * as React from "react";
import { format } from "date-fns";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useSignals } from "@/hooks/useSignals";
import { SignalDetailModal } from "@/components/ai/SignalDetailModal";
import { SignalType, TimeHorizon, type SignalFilters, type TradingSignal } from "@/types/signals";
import {
  TrendingUp,
  TrendingDown,
  Minus,
  Filter,
  RefreshCw,
  ChevronLeft,
  ChevronRight,
} from "lucide-react";

interface SignalsPanelProps {
  className?: string;
}

function safeFormatDate(value: string | null | undefined, fmt: string): string {
  if (!value) return "—";
  const d = new Date(value);
  if (isNaN(d.getTime())) return "—";
  return format(d, fmt);
}

export function SignalsPanel({ className }: SignalsPanelProps) {
  const [filters, setFilters] = React.useState<SignalFilters>({
    page: 1,
    limit: 10,
  });
  const [selectedSignal, setSelectedSignal] = React.useState<TradingSignal | null>(null);
  const [showFilters, setShowFilters] = React.useState(false);

  const { data, isLoading, refetch, isRefetching } = useSignals(filters);

  const getSignalIcon = (type: SignalType) => {
    switch (type) {
      case SignalType.BUY:
        return <TrendingUp className="size-4" />;
      case SignalType.SELL:
        return <TrendingDown className="size-4" />;
      case SignalType.HOLD:
        return <Minus className="size-4" />;
    }
  };

  const getSignalColor = (type: SignalType) => {
    switch (type) {
      case SignalType.BUY:
        return "bg-green-500/10 text-green-700 dark:text-green-400 border-green-500/20";
      case SignalType.SELL:
        return "bg-red-500/10 text-red-700 dark:text-red-400 border-red-500/20";
      case SignalType.HOLD:
        return "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400 border-yellow-500/20";
    }
  };

  const getConfidenceColor = (confidence: number) => {
    if (confidence >= 0.8) return "text-green-600 dark:text-green-400";
    if (confidence >= 0.6) return "text-yellow-600 dark:text-yellow-400";
    return "text-red-600 dark:text-red-400";
  };

  const handleFilterChange = (key: keyof SignalFilters, value: any) => {
    setFilters((prev) => ({
      ...prev,
      [key]: value,
      page: key === "page" ? value : 1, // Reset to page 1 when changing filters
    }));
  };

  const clearFilters = () => {
    setFilters({ page: 1, limit: 10 });
  };

  const totalPages = data ? Math.ceil(data.total / (filters.limit || 10)) : 0;

  return (
    <>
      <Card className={className}>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Trading Signals</CardTitle>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setShowFilters(!showFilters)}
              >
                <Filter className="size-4" />
                Filters
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => refetch()}
                disabled={isRefetching}
              >
                <RefreshCw
                  className={`size-4 ${isRefetching ? "animate-spin" : ""}`}
                />
                Refresh
              </Button>
            </div>
          </div>
        </CardHeader>

        <CardContent className="space-y-4">
          {/* Filters Section */}
          {showFilters && (
            <div className="rounded-lg border p-4">
              <div className="mb-3 flex items-center justify-between">
                <h3 className="font-semibold">Filters</h3>
                <Button variant="ghost" size="sm" onClick={clearFilters}>
                  Clear All
                </Button>
              </div>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
                {/* Symbol Filter */}
                <div>
                  <label className="text-sm font-medium">Symbol</label>
                  <input
                    type="text"
                    placeholder="e.g., RELIANCE"
                    value={filters.symbol || ""}
                    onChange={(e) =>
                      handleFilterChange("symbol", e.target.value || undefined)
                    }
                    className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
                  />
                </div>

                {/* Signal Type Filter */}
                <div>
                  <label className="text-sm font-medium">Signal Type</label>
                  <select
                    value={filters.signal_type || ""}
                    onChange={(e) =>
                      handleFilterChange(
                        "signal_type",
                        e.target.value || undefined
                      )
                    }
                    className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
                  >
                    <option value="">All</option>
                    <option value={SignalType.BUY}>Buy</option>
                    <option value={SignalType.SELL}>Sell</option>
                    <option value={SignalType.HOLD}>Hold</option>
                  </select>
                </div>

                {/* Time Horizon Filter */}
                <div>
                  <label className="text-sm font-medium">Time Horizon</label>
                  <select
                    value={filters.time_horizon || ""}
                    onChange={(e) =>
                      handleFilterChange(
                        "time_horizon",
                        e.target.value || undefined
                      )
                    }
                    className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
                  >
                    <option value="">All</option>
                    <option value={TimeHorizon.INTRADAY}>Intraday</option>
                    <option value={TimeHorizon.SWING}>Swing</option>
                    <option value={TimeHorizon.POSITIONAL}>Positional</option>
                  </select>
                </div>

                {/* Confidence Threshold Filter */}
                <div>
                  <label className="text-sm font-medium">
                    Min Confidence (%)
                  </label>
                  <input
                    type="number"
                    min="0"
                    max="100"
                    step="5"
                    placeholder="0-100"
                    value={
                      filters.min_confidence !== undefined
                        ? filters.min_confidence * 100
                        : ""
                    }
                    onChange={(e) =>
                      handleFilterChange(
                        "min_confidence",
                        e.target.value ? Number(e.target.value) / 100 : undefined
                      )
                    }
                    className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
                  />
                </div>
              </div>
            </div>
          )}

          {/* Signals Table */}
          {isLoading ? (
            <div className="py-8 text-center text-muted-foreground">
              Loading signals...
            </div>
          ) : data && data.signals.length > 0 ? (
            <>
              <div className="rounded-lg border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Symbol</TableHead>
                      <TableHead>Direction</TableHead>
                      <TableHead>Confidence</TableHead>
                      <TableHead>Time Horizon</TableHead>
                      <TableHead>Reasoning</TableHead>
                      <TableHead>Generated</TableHead>
                      <TableHead className="text-right">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data.signals.map((signal) => (
                      <TableRow key={signal.signal_id}>
                        <TableCell className="font-medium">
                          {signal.symbol}
                        </TableCell>
                        <TableCell>
                          <Badge className={getSignalColor(signal.signal_type)}>
                            {getSignalIcon(signal.signal_type)}
                            {signal.signal_type?.toUpperCase() || 'UNKNOWN'}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          <span
                            className={`font-semibold ${getConfidenceColor(
                              signal.calibrated_confidence
                            )}`}
                          >
                            {(signal.calibrated_confidence * 100).toFixed(1)}%
                          </span>
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline">
                            {signal.time_horizon}
                          </Badge>
                        </TableCell>
                        <TableCell className="max-w-xs truncate">
                          {signal.reasoning}
                        </TableCell>
                        <TableCell className="text-muted-foreground text-sm">
                          {safeFormatDate(signal.generated_at, "MMM d, HH:mm")}
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setSelectedSignal(signal)}
                          >
                            View Details
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>

              {/* Pagination */}
              {totalPages > 1 && (
                <div className="flex items-center justify-between">
                  <div className="text-muted-foreground text-sm">
                    Showing {data.signals.length} of {data.total} signals
                  </div>
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        handleFilterChange("page", (filters.page || 1) - 1)
                      }
                      disabled={!filters.page || filters.page <= 1}
                    >
                      <ChevronLeft className="size-4" />
                      Previous
                    </Button>
                    <span className="text-sm">
                      Page {filters.page || 1} of {totalPages}
                    </span>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        handleFilterChange("page", (filters.page || 1) + 1)
                      }
                      disabled={filters.page === totalPages}
                    >
                      Next
                      <ChevronRight className="size-4" />
                    </Button>
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="py-8 text-center text-muted-foreground">
              No signals found. Try adjusting your filters.
            </div>
          )}
        </CardContent>
      </Card>

      {/* Signal Detail Modal */}
      <SignalDetailModal
        signal={selectedSignal}
        open={!!selectedSignal}
        onOpenChange={(open) => !open && setSelectedSignal(null)}
      />
    </>
  );
}
