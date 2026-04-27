"use client";

import { Filter, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { SignalDirection, ConfidenceLevel, SuggestionFilters as Filters } from "@/types/trade_suggestions";

interface SuggestionFiltersProps {
  filters: Filters;
  onFiltersChange: (filters: Filters) => void;
  className?: string;
}

export function SuggestionFilters({
  filters,
  onFiltersChange,
  className,
}: SuggestionFiltersProps) {
  const activeFilterCount = [
    filters.direction,
    filters.confidence_level,
    filters.min_confidence,
    filters.symbol,
  ].filter(Boolean).length;

  const handleDirectionChange = (value: string) => {
    onFiltersChange({
      ...filters,
      direction: value === "all" ? undefined : (value as SignalDirection),
    });
  };

  const handleConfidenceChange = (value: string) => {
    onFiltersChange({
      ...filters,
      confidence_level: value === "all" ? undefined : (value as ConfidenceLevel),
    });
  };

  const handleMinScoreChange = (value: string) => {
    onFiltersChange({
      ...filters,
      min_confidence: value === "all" ? undefined : parseFloat(value),
    });
  };

  const handleClearFilters = () => {
    onFiltersChange({
      page: 1,
      page_size: filters.page_size || 50,
    });
  };

  return (
    <div className={`flex flex-wrap items-center gap-3 ${className || ""}`}>
      <div className="flex items-center gap-2 text-sm font-medium text-slate-700">
        <Filter className="h-4 w-4" />
        <span>Filters</span>
        {activeFilterCount > 0 && (
          <Badge variant="secondary" className="h-5 px-1.5 text-xs">
            {activeFilterCount}
          </Badge>
        )}
      </div>

      {/* Direction Filter */}
      <Select
        value={filters.direction || "all"}
        onValueChange={handleDirectionChange}
      >
        <SelectTrigger className="w-[140px] h-9">
          <SelectValue placeholder="Direction" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All Directions</SelectItem>
          <SelectItem value="BUY">BUY</SelectItem>
          <SelectItem value="SELL">SELL</SelectItem>
        </SelectContent>
      </Select>

      {/* Confidence Filter */}
      <Select
        value={filters.confidence_level || "all"}
        onValueChange={handleConfidenceChange}
      >
        <SelectTrigger className="w-[140px] h-9">
          <SelectValue placeholder="Confidence" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">All Confidence</SelectItem>
          <SelectItem value="HIGH">HIGH</SelectItem>
          <SelectItem value="MEDIUM">MEDIUM</SelectItem>
          <SelectItem value="LOW">LOW</SelectItem>
        </SelectContent>
      </Select>

      {/* Min Score Filter */}
      <Select
        value={filters.min_confidence?.toString() || "all"}
        onValueChange={handleMinScoreChange}
      >
        <SelectTrigger className="w-[140px] h-9">
          <SelectValue placeholder="Min Score" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="all">Any Score</SelectItem>
          <SelectItem value="90">≥ 90%</SelectItem>
          <SelectItem value="80">≥ 80%</SelectItem>
          <SelectItem value="70">≥ 70%</SelectItem>
          <SelectItem value="60">≥ 60%</SelectItem>
        </SelectContent>
      </Select>

      {/* Clear Filters */}
      {activeFilterCount > 0 && (
        <Button
          variant="ghost"
          size="sm"
          onClick={handleClearFilters}
          className="h-9 px-3 text-slate-600 hover:text-slate-900"
        >
          <X className="h-4 w-4 mr-1" />
          Clear
        </Button>
      )}
    </div>
  );
}
