/**
 * Events Panel Component
 * Displays processed events with impact scores, credibility, and fake news detection
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
import { useEvents } from "@/hooks/useEvents";
import { EventType, FakeNewsStatus, type EventFilters } from "@/types/events";
import {
  AlertTriangle,
  TrendingUp,
  TrendingDown,
  Filter,
  RefreshCw,
  ChevronLeft,
  ChevronRight,
  Shield,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react";

interface EventsPanelProps {
  className?: string;
}

export function EventsPanel({ className }: EventsPanelProps) {
  const [filters, setFilters] = React.useState<EventFilters>({
    page: 1,
    limit: 10,
  });
  const [showFilters, setShowFilters] = React.useState(false);

  const { data, isLoading, refetch, isRefetching } = useEvents(filters);

  const getEventTypeLabel = (type: EventType) => {
    return type
      .split("_")
      .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
      .join(" ");
  };

  const getEventTypeColor = (type: EventType) => {
    switch (type) {
      case EventType.EARNINGS_ANNOUNCEMENT:
      case EventType.DIVIDEND_ANNOUNCEMENT:
      case EventType.BUYBACK_ANNOUNCEMENT:
        return "bg-green-500/10 text-green-700 dark:text-green-400 border-green-500/20";
      case EventType.MERGER_ACQUISITION:
      case EventType.PRODUCT_LAUNCH:
        return "bg-blue-500/10 text-blue-700 dark:text-blue-400 border-blue-500/20";
      case EventType.REGULATORY_CHANGE:
      case EventType.LEGAL_ISSUE:
        return "bg-red-500/10 text-red-700 dark:text-red-400 border-red-500/20";
      case EventType.MANAGEMENT_CHANGE:
      case EventType.ANALYST_RATING:
        return "bg-purple-500/10 text-purple-700 dark:text-purple-400 border-purple-500/20";
      case EventType.MARKET_RUMOR:
        return "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400 border-yellow-500/20";
      default:
        return "bg-gray-500/10 text-gray-700 dark:text-gray-400 border-gray-500/20";
    }
  };

  const getImpactColor = (impact: number) => {
    if (impact >= 70) return "text-red-600 dark:text-red-400";
    if (impact >= 40) return "text-yellow-600 dark:text-yellow-400";
    return "text-green-600 dark:text-green-400";
  };

  const getCredibilityColor = (credibility: number) => {
    if (credibility >= 70) return "text-green-600 dark:text-green-400";
    if (credibility >= 40) return "text-yellow-600 dark:text-yellow-400";
    return "text-red-600 dark:text-red-400";
  };

  const getSentimentIcon = (sentiment: number) => {
    if (sentiment > 0.2) return <TrendingUp className="size-4 text-green-600" />;
    if (sentiment < -0.2) return <TrendingDown className="size-4 text-red-600" />;
    return <span className="text-gray-600">—</span>;
  };

  const getFakeNewsIcon = (status?: FakeNewsStatus) => {
    if (!status) return <ShieldCheck className="size-4 text-green-600" />;
    switch (status) {
      case FakeNewsStatus.CONFIRMED:
        return <ShieldAlert className="size-4 text-red-600" />;
      case FakeNewsStatus.SUSPECTED:
        return <AlertTriangle className="size-4 text-yellow-600" />;
      case FakeNewsStatus.CLEARED:
        return <ShieldCheck className="size-4 text-green-600" />;
    }
  };

  const getFakeNewsLabel = (status?: FakeNewsStatus) => {
    if (!status) return "Verified";
    switch (status) {
      case FakeNewsStatus.CONFIRMED:
        return "Fake News";
      case FakeNewsStatus.SUSPECTED:
        return "Suspected";
      case FakeNewsStatus.CLEARED:
        return "Cleared";
    }
  };

  const getDetectionLayersCount = (layers?: { layer1: boolean; layer2: boolean; layer3: boolean; layer4: boolean }) => {
    if (!layers) return 0;
    return Object.values(layers).filter(Boolean).length;
  };

  const handleFilterChange = (key: keyof EventFilters, value: any) => {
    setFilters((prev) => ({
      ...prev,
      [key]: value,
      page: key === "page" ? value : 1,
    }));
  };

  const clearFilters = () => {
    setFilters({ page: 1, limit: 10 });
  };

  const totalPages = data ? Math.ceil(data.total / (filters.limit || 10)) : 0;

  return (
    <Card className={className}>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Market Events</CardTitle>
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
            <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
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

              {/* Event Type Filter */}
              <div>
                <label className="text-sm font-medium">Event Type</label>
                <select
                  value={filters.event_type || ""}
                  onChange={(e) =>
                    handleFilterChange(
                      "event_type",
                      e.target.value || undefined
                    )
                  }
                  className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
                >
                  <option value="">All</option>
                  {Object.values(EventType).map((type) => (
                    <option key={type} value={type}>
                      {getEventTypeLabel(type)}
                    </option>
                  ))}
                </select>
              </div>

              {/* Impact Score Filter */}
              <div>
                <label className="text-sm font-medium">
                  Min Impact Score
                </label>
                <input
                  type="number"
                  min="0"
                  max="100"
                  step="5"
                  placeholder="0-100"
                  value={filters.min_impact || ""}
                  onChange={(e) =>
                    handleFilterChange(
                      "min_impact",
                      e.target.value ? Number(e.target.value) : undefined
                    )
                  }
                  className="mt-1 w-full rounded-md border px-3 py-2 text-sm"
                />
              </div>
            </div>
          </div>
        )}

        {/* Events Table */}
        {isLoading ? (
          <div className="py-8 text-center text-muted-foreground">
            Loading events...
          </div>
        ) : data && data.events.length > 0 ? (
          <>
            <div className="rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Event Type</TableHead>
                    <TableHead>Symbols</TableHead>
                    <TableHead>Impact</TableHead>
                    <TableHead>Sentiment</TableHead>
                    <TableHead>Credibility</TableHead>
                    <TableHead>Fake News</TableHead>
                    <TableHead>Detection</TableHead>
                    <TableHead>Processed</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.events.map((event) => (
                    <TableRow key={event.id}>
                      <TableCell>
                        <Badge className={getEventTypeColor(event.event_type)}>
                          {getEventTypeLabel(event.event_type)}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1">
                          {event.affected_symbols.slice(0, 3).map((symbol) => (
                            <Badge key={symbol} variant="outline" className="text-xs">
                              {symbol}
                            </Badge>
                          ))}
                          {event.affected_symbols.length > 3 && (
                            <Badge variant="outline" className="text-xs">
                              +{event.affected_symbols.length - 3}
                            </Badge>
                          )}
                        </div>
                      </TableCell>
                      <TableCell>
                        <span
                          className={`font-semibold ${getImpactColor(
                            event.impact_score
                          )}`}
                        >
                          {event.impact_score.toFixed(1)}
                        </span>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          {getSentimentIcon(event.sentiment_score)}
                          <span className="text-sm">
                            {event.sentiment_score?.toFixed(2) || "N/A"}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <span
                          className={`font-semibold ${getCredibilityColor(
                            event.credibility_score
                          )}`}
                        >
                          {event.credibility_score?.toFixed(1) || "N/A"}
                        </span>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          {getFakeNewsIcon(event.fake_news_flag?.flag_status)}
                          <span className="text-sm">
                            {getFakeNewsLabel(event.fake_news_flag?.flag_status)}
                          </span>
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          <Shield className="size-4 text-muted-foreground" />
                          <span className="text-sm">
                            {getDetectionLayersCount(event.fake_news_flag?.detection_layers)}/4
                          </span>
                        </div>
                      </TableCell>
                      <TableCell className="text-muted-foreground text-sm">
                        {format(new Date(event.timestamp), "MMM d, HH:mm")}
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
                  Showing {data.events.length} of {data.total} events
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
            No events found. Try adjusting your filters.
          </div>
        )}
      </CardContent>
    </Card>
  );
}
