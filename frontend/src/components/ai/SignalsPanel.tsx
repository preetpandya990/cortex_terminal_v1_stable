/**
 * Signals Panel — real-time trading signal list with NSE / Non-NSE segmentation,
 * filtering, and pagination.
 *
 * NSE tab (default): signals for symbols confirmed in instrument_master as NSE EQ
 *   equities — actionable, suitable for paper trading.
 * Non-NSE tab: signals for companies referenced in RSS/news feeds that are not
 *   listed on the platform — informational only, no trade execution possible.
 *
 * Uses useSignalsRealtime for WebSocket-injected live updates + 30 s poll fallback.
 * WebSocket injection is tab-aware: each tab's React Query cache receives only the
 * signals that match its is_nse_eligible filter.
 */
import * as React from "react";
import { format, differenceInHours, isPast } from "date-fns";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useSignalsRealtime } from "@/hooks/useSignalsRealtime";
import { SignalDetailModal } from "@/components/ai/SignalDetailModal";
import {
  SignalType,
  TimeHorizon,
  type SignalFilters,
  type TradingSignal,
} from "@/types/signals";
import type { ConnectionStatus } from "@/hooks/useCAIWebSocket";
import { cn } from "@/lib/utils";
import {
  TrendingUp,
  TrendingDown,
  Minus,
  Filter,
  RefreshCw,
  ChevronLeft,
  ChevronRight,
  Clock,
  AlertCircle,
} from "lucide-react";

// ── Types ──────────────────────────────────────────────────────────────────────

interface SignalsPanelProps {
  className?: string;
  onWsStatusChange?: (status: ConnectionStatus) => void;
}

type ActiveTab = "nse" | "non-nse";

// ── Helpers ────────────────────────────────────────────────────────────────────

function safeFormatDate(value: string | null | undefined, fmt: string): string {
  if (!value) return "—";
  const d = new Date(value);
  if (isNaN(d.getTime())) return "—";
  return format(d, fmt);
}

function getExpiryState(expiresAt: string | null | undefined): "active" | "soon" | "expired" {
  if (!expiresAt) return "active";
  const d = new Date(expiresAt);
  if (isNaN(d.getTime())) return "active";
  if (isPast(d)) return "expired";
  if (differenceInHours(d, new Date()) <= 2) return "soon";
  return "active";
}

function getSignalIcon(type: SignalType) {
  switch (type) {
    case SignalType.BUY:  return <TrendingUp className="size-4" />;
    case SignalType.SELL: return <TrendingDown className="size-4" />;
    case SignalType.HOLD: return <Minus className="size-4" />;
  }
}

function getSignalColor(type: SignalType): string {
  switch (type) {
    case SignalType.BUY:  return "bg-green-500/10 text-green-700 dark:text-green-400 border-green-500/20";
    case SignalType.SELL: return "bg-red-500/10 text-red-700 dark:text-red-400 border-red-500/20";
    case SignalType.HOLD: return "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400 border-yellow-500/20";
  }
}

function getConfidenceColor(confidence: number): string {
  if (confidence >= 0.8) return "text-green-600 dark:text-green-400";
  if (confidence >= 0.6) return "text-yellow-600 dark:text-yellow-400";
  return "text-red-600 dark:text-red-400";
}

// ── Sub-components ─────────────────────────────────────────────────────────────

interface ExpiryCellProps {
  expiresAt: string | null | undefined;
  state: "active" | "soon" | "expired";
}

function ExpiryCell({ expiresAt, state }: ExpiryCellProps) {
  const formatted = expiresAt ? safeFormatDate(expiresAt, "MMM d, HH:mm") : "—";

  if (state === "expired") {
    return (
      <span className="inline-flex items-center gap-1 text-red-600 dark:text-red-400">
        <Clock className="size-3" />
        <Badge variant="outline" className="border-red-400 text-red-600 dark:text-red-400 text-xs py-0">
          EXPIRED
        </Badge>
        <span className="text-muted-foreground">{formatted}</span>
      </span>
    );
  }

  if (state === "soon") {
    return (
      <span className="inline-flex items-center gap-1 text-amber-600 dark:text-amber-400">
        <Clock className="size-3" />
        {formatted}
      </span>
    );
  }

  return <span className="text-muted-foreground">{formatted}</span>;
}

// ── Filter panel ───────────────────────────────────────────────────────────────

interface FilterPanelProps {
  filters: SignalFilters;
  onFilterChange: (key: keyof SignalFilters, value: unknown) => void;
  onClear: () => void;
}

function FilterPanel({ filters, onFilterChange, onClear }: FilterPanelProps) {
  return (
    <div className="rounded-lg border p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="font-semibold">Filters</h3>
        <Button variant="ghost" size="sm" onClick={onClear}>
          Clear All
        </Button>
      </div>
      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <div>
          <label className="text-sm font-medium">Symbol</label>
          <input
            type="text"
            placeholder="e.g., RELIANCE"
            value={filters.symbol || ""}
            onChange={(e) => onFilterChange("symbol", e.target.value || undefined)}
            className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
          />
        </div>
        <div>
          <label className="text-sm font-medium">Signal Type</label>
          <select
            value={filters.signal_type || ""}
            onChange={(e) => onFilterChange("signal_type", e.target.value || undefined)}
            className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
          >
            <option value="">All</option>
            <option value={SignalType.BUY}>Buy</option>
            <option value={SignalType.SELL}>Sell</option>
            <option value={SignalType.HOLD}>Hold</option>
          </select>
        </div>
        <div>
          <label className="text-sm font-medium">Time Horizon</label>
          <select
            value={filters.time_horizon || ""}
            onChange={(e) => onFilterChange("time_horizon", e.target.value || undefined)}
            className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
          >
            <option value="">All</option>
            <option value={TimeHorizon.INTRADAY}>Intraday</option>
            <option value={TimeHorizon.SWING}>Swing</option>
            <option value={TimeHorizon.POSITIONAL}>Positional</option>
          </select>
        </div>
        <div>
          <label className="text-sm font-medium">Min Confidence (%)</label>
          <input
            type="number"
            min="0"
            max="100"
            step="5"
            placeholder="0–100"
            value={filters.min_confidence !== undefined ? filters.min_confidence * 100 : ""}
            onChange={(e) =>
              onFilterChange(
                "min_confidence",
                e.target.value ? Number(e.target.value) / 100 : undefined,
              )
            }
            className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
          />
        </div>
      </div>
    </div>
  );
}

// ── Signal table ───────────────────────────────────────────────────────────────

const SIGNAL_ACCENT: Record<SignalType, string> = {
  [SignalType.BUY]:  "border-l-emerald-500",
  [SignalType.SELL]: "border-l-red-500",
  [SignalType.HOLD]: "border-l-yellow-500",
};

const CONFIDENCE_DOT: (c: number) => string = (c) =>
  c >= 0.8 ? "bg-emerald-500" : c >= 0.6 ? "bg-yellow-500" : "bg-red-500";

const HORIZON_STYLE: Record<string, string> = {
  intraday:   "border-blue-300/60   bg-blue-50/60   text-blue-700   dark:border-blue-700/50 dark:bg-blue-950/30 dark:text-blue-400",
  swing:      "border-purple-300/60 bg-purple-50/60 text-purple-700 dark:border-purple-700/50 dark:bg-purple-950/30 dark:text-purple-400",
  positional: "border-cyan-300/60   bg-cyan-50/60   text-cyan-700   dark:border-cyan-700/50 dark:bg-cyan-950/30 dark:text-cyan-400",
};

interface SignalTableProps {
  signals: TradingSignal[];
  isNseTab: boolean;
  onViewDetails: (signal: TradingSignal) => void;
}

function SignalTable({ signals, isNseTab, onViewDetails }: SignalTableProps) {
  return (
    <div className="rounded-xl border overflow-hidden shadow-sm">
      <Table>
        <TableHeader>
          <TableRow className="bg-muted/40 hover:bg-muted/40">
            <TableHead className="pl-4 font-semibold text-xs uppercase tracking-wider text-muted-foreground">
              Symbol
            </TableHead>
            <TableHead className="font-semibold text-xs uppercase tracking-wider text-muted-foreground">
              Direction
            </TableHead>
            <TableHead className="font-semibold text-xs uppercase tracking-wider text-muted-foreground">
              Confidence
            </TableHead>
            <TableHead className="font-semibold text-xs uppercase tracking-wider text-muted-foreground">
              Horizon
            </TableHead>
            <TableHead className="font-semibold text-xs uppercase tracking-wider text-muted-foreground">
              Reasoning
            </TableHead>
            <TableHead className="font-semibold text-xs uppercase tracking-wider text-muted-foreground">
              Generated
            </TableHead>
            <TableHead className="font-semibold text-xs uppercase tracking-wider text-muted-foreground">
              Expires
            </TableHead>
            {/* Chevron hint column — no label */}
            <TableHead className="w-8" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {signals.map((signal) => {
            const expiryState = getExpiryState(signal.expires_at);
            const confidence = signal.calibrated_confidence;

            return (
              <TableRow
                key={signal.signal_id}
                onClick={() => onViewDetails(signal)}
                className={cn(
                  "group cursor-pointer transition-colors",
                  "hover:bg-muted/50 dark:hover:bg-muted/30",
                  expiryState === "expired" && "opacity-50",
                )}
              >
                {/* Symbol — left accent border by signal direction */}
                <TableCell
                  className={cn(
                    "pl-4 border-l-[3px] py-3",
                    SIGNAL_ACCENT[signal.signal_type] ?? "border-l-transparent",
                  )}
                >
                  <div className="flex flex-col gap-0.5">
                    <span className="font-semibold leading-tight tracking-tight">
                      {signal.symbol}
                    </span>
                    {signal.company_name && (
                      <span className="text-xs text-muted-foreground leading-tight truncate max-w-[150px]">
                        {signal.company_name}
                      </span>
                    )}
                  </div>
                </TableCell>

                {/* Direction */}
                <TableCell className="py-3">
                  <div className="flex flex-col gap-1.5">
                    <Badge
                      className={cn(
                        "w-fit gap-1.5 font-semibold",
                        getSignalColor(signal.signal_type),
                      )}
                    >
                      {getSignalIcon(signal.signal_type)}
                      {signal.signal_type?.toUpperCase() ?? "UNKNOWN"}
                    </Badge>
                    {!isNseTab && (
                      <Badge
                        variant="outline"
                        className="w-fit gap-1 border-amber-400/60 bg-amber-50 text-amber-700 dark:bg-amber-950/30 dark:text-amber-400 text-xs"
                      >
                        <AlertCircle className="size-3" />
                        Informational
                      </Badge>
                    )}
                  </div>
                </TableCell>

                {/* Confidence — dot + percentage */}
                <TableCell className="py-3">
                  <div className="flex items-center gap-2">
                    <span
                      className={cn(
                        "size-2 rounded-full shrink-0",
                        CONFIDENCE_DOT(confidence),
                      )}
                    />
                    <span
                      className={cn(
                        "font-semibold tabular-nums text-sm",
                        getConfidenceColor(confidence),
                      )}
                    >
                      {(confidence * 100).toFixed(1)}%
                    </span>
                  </div>
                </TableCell>

                {/* Horizon — color-coded badge */}
                <TableCell className="py-3">
                  <Badge
                    variant="outline"
                    className={cn(
                      "capitalize text-xs font-medium",
                      HORIZON_STYLE[signal.time_horizon] ?? "",
                    )}
                  >
                    {signal.time_horizon}
                  </Badge>
                </TableCell>

                {/* Reasoning */}
                <TableCell className="py-3 max-w-xs">
                  <p className="truncate text-sm text-muted-foreground leading-snug">
                    {signal.reasoning}
                  </p>
                </TableCell>

                {/* Generated */}
                <TableCell className="py-3">
                  <span className="text-sm text-muted-foreground tabular-nums">
                    {safeFormatDate(signal.generated_at, "MMM d, HH:mm")}
                  </span>
                </TableCell>

                {/* Expires */}
                <TableCell className="py-3 text-sm">
                  <ExpiryCell expiresAt={signal.expires_at} state={expiryState} />
                </TableCell>

                {/* Row chevron — appears on hover to signal clickability */}
                <TableCell className="py-3 pr-4 w-8">
                  <ChevronRight
                    className={cn(
                      "size-4 text-muted-foreground/50 transition-all",
                      "opacity-0 group-hover:opacity-100 group-hover:translate-x-0.5",
                    )}
                  />
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

// ── Tab content (data fetching + rendering) ────────────────────────────────────

interface TabContentProps {
  /** Common filters from the filter panel — eligibility is injected by the tab. */
  baseFilters: SignalFilters;
  isNseTab: boolean;
  onViewDetails: (signal: TradingSignal) => void;
  onPageChange: (page: number) => void;
  onWsStatusChange?: (status: ConnectionStatus) => void;
}

function TabContent({ baseFilters, isNseTab, onViewDetails, onPageChange, onWsStatusChange }: TabContentProps) {
  const filters: SignalFilters = { ...baseFilters, is_nse_eligible: isNseTab };

  const { data, isLoading, refetch, isRefetching, wsStatus } = useSignalsRealtime(filters);

  React.useEffect(() => {
    onWsStatusChange?.(wsStatus);
  }, [wsStatus, onWsStatusChange]);

  const totalPages = data ? Math.ceil(data.total / (filters.limit || 10)) : 0;

  if (isLoading) {
    return (
      <div className="py-8 text-center text-muted-foreground">Loading signals…</div>
    );
  }

  if (!data || data.signals.length === 0) {
    return (
      <div className="py-8 text-center text-muted-foreground">
        {isNseTab
          ? "No NSE signals found. Try adjusting your filters."
          : "No Non-NSE signals found. These appear when news references companies not listed on the platform."}
      </div>
    );
  }

  const page = filters.page || 1;
  const limit = filters.limit || 10;

  return (
    <div className="space-y-4">
      <SignalTable signals={data.signals} isNseTab={isNseTab} onViewDetails={onViewDetails} />

      {/* Footer: count + pagination + refresh */}
      <div className="flex items-center justify-between">
        <div className="text-muted-foreground text-sm">
          {data.total} signal{data.total !== 1 ? "s" : ""}
          {totalPages > 1 && ` · page ${page} of ${totalPages}`}
        </div>
        <div className="flex items-center gap-2">
          {totalPages > 1 && (
            <>
              <Button
                variant="outline"
                size="sm"
                onClick={() => onPageChange(page - 1)}
                disabled={page <= 1}
              >
                <ChevronLeft className="size-4" />
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => onPageChange(page + 1)}
                disabled={page >= totalPages}
              >
                Next
                <ChevronRight className="size-4" />
              </Button>
            </>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => refetch()}
            disabled={isRefetching}
            className="text-muted-foreground"
          >
            <RefreshCw className={cn("size-4", isRefetching && "animate-spin")} />
          </Button>
        </div>
      </div>
    </div>
  );
}

// ── Main panel ─────────────────────────────────────────────────────────────────

export function SignalsPanel({ className, onWsStatusChange }: SignalsPanelProps) {
  const [activeTab, setActiveTab] = React.useState<ActiveTab>("nse");
  const [showFilters, setShowFilters] = React.useState(false);
  const [selectedSignal, setSelectedSignal] = React.useState<TradingSignal | null>(null);

  // Common (tab-independent) filters — page resets to 1 whenever the tab or a
  // filter value changes to avoid stale page offsets.
  const [filters, setFilters] = React.useState<Omit<SignalFilters, "is_nse_eligible">>({
    page: 1,
    limit: 10,
  });

  const handleFilterChange = (key: keyof SignalFilters, value: unknown) => {
    setFilters((prev) => ({
      ...prev,
      [key]: value,
      page: key === "page" ? (value as number) : 1,
    }));
  };

  const handleTabChange = (tab: string) => {
    setActiveTab(tab as ActiveTab);
    // Reset to page 1 on tab switch so results are never offset-misaligned.
    setFilters((prev) => ({ ...prev, page: 1 }));
  };

  const clearFilters = () => setFilters({ page: 1, limit: 10 });

  return (
    <>
      <Card className={className}>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle>Trading Signals</CardTitle>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setShowFilters(!showFilters)}
            >
              <Filter className="size-4" />
              Filters
            </Button>
          </div>
        </CardHeader>

        <CardContent className="space-y-4">
          {showFilters && (
            <FilterPanel
              filters={filters}
              onFilterChange={handleFilterChange}
              onClear={clearFilters}
            />
          )}

          <Tabs value={activeTab} onValueChange={handleTabChange}>
            <TabsList className="mb-4">
              <TabsTrigger value="nse">
                NSE
              </TabsTrigger>
              <TabsTrigger value="non-nse">
                Non-NSE
                <span className="ml-1.5 rounded-full bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-950/40 dark:text-amber-400">
                  Info
                </span>
              </TabsTrigger>
            </TabsList>

            <TabsContent value="nse">
              <TabContent
                baseFilters={filters}
                isNseTab={true}
                onViewDetails={setSelectedSignal}
                onPageChange={(p) => handleFilterChange("page", p)}
                onWsStatusChange={onWsStatusChange}
              />
            </TabsContent>

            <TabsContent value="non-nse">
              {/* Non-NSE disclaimer — always visible above the table */}
              <div className="mb-4 flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 dark:border-amber-800/40 dark:bg-amber-950/20">
                <AlertCircle className="mt-0.5 size-4 shrink-0 text-amber-600 dark:text-amber-400" />
                <p className="text-sm text-amber-800 dark:text-amber-300">
                  These signals are generated from news and market events for companies that are
                  not listed or tracked on this platform. They are{" "}
                  <strong>informational only</strong> — no trade execution is available for
                  these symbols.
                </p>
              </div>
              <TabContent
                baseFilters={filters}
                isNseTab={false}
                onViewDetails={setSelectedSignal}
                onPageChange={(p) => handleFilterChange("page", p)}
                onWsStatusChange={onWsStatusChange}
              />
            </TabsContent>
          </Tabs>
        </CardContent>
      </Card>

      {selectedSignal && (
        <SignalDetailModal
          signal={selectedSignal}
          onClose={() => setSelectedSignal(null)}
        />
      )}
    </>
  );
}
