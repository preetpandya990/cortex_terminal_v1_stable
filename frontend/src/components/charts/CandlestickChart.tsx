"use client";

import { useEffect, useRef, useMemo, useCallback } from "react";
import {
  createChart,
  IChartApi,
  ISeriesApi,
  CandlestickSeries,
  CandlestickData,
  UTCTimestamp,
  ColorType,
} from "lightweight-charts";
import { transformCandlesForChart, updateLastCandleWithLivePrice } from "@/lib/candle-transforms";
import type { UpstoxCandle, UpstoxLtpTick } from "@/types/upstox";
import type { CandleUnit } from "@/lib/chart-policy";
import { Loader2 } from "lucide-react";

export interface CandlestickChartProps {
  candles: UpstoxCandle[];
  liveTick?: UpstoxLtpTick | null;
  candleUnit: CandleUnit;
  candleInterval: number;
  height?: number;
  canLoadMoreHistory?: boolean;
  isLoadingMoreHistory?: boolean;
  onLoadMoreHistory?: () => Promise<void>;
  className?: string;
}

export function CandlestickChart({
  candles,
  liveTick,
  candleUnit,
  candleInterval,
  height = 400,
  canLoadMoreHistory = false,
  isLoadingMoreHistory = false,
  onLoadMoreHistory,
  className = "",
}: CandlestickChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lastCandleCountRef = useRef(0);
  const isLoadingRef = useRef(false);

  // Transform candles to chart format
  const chartData = useMemo(() => transformCandlesForChart(candles), [candles]);

  // Apply live tick to last candle if available
  const chartDataWithLiveTick = useMemo(() => {
    if (!liveTick || !candles.length) return chartData;
    const updatedCandles = updateLastCandleWithLivePrice(candles, liveTick.last_price);
    return transformCandlesForChart(updatedCandles);
  }, [chartData, liveTick, candles]);

  // Initialize chart and series
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#9ca3af",
      },
      grid: {
        vertLines: { color: "#1f2937" },
        horzLines: { color: "#1f2937" },
      },
      crosshair: {
        vertLine: { labelBackgroundColor: "#374151" },
        horzLine: { labelBackgroundColor: "#374151" },
      },
      timeScale: {
        borderColor: "#374151",
        timeVisible: true,
        secondsVisible: false,
      },
      rightPriceScale: {
        borderColor: "#374151",
      },
      width: containerRef.current.clientWidth,
      height: containerRef.current.clientHeight || height,
    });

    chartRef.current = chart;

    // Add candlestick series using v5 API
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#10b981",
      downColor: "#ef4444",
      borderUpColor: "#10b981",
      borderDownColor: "#ef4444",
      wickUpColor: "#10b981",
      wickDownColor: "#ef4444",
    });

    seriesRef.current = series;

    // Responsive resize
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry || !chartRef.current) return;
      chartRef.current.applyOptions({
        width: entry.contentRect.width,
        height: entry.contentRect.height,
      });
    });
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      chartRef.current?.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, [height]);

  // Update chart data
  useEffect(() => {
    if (!seriesRef.current || !chartDataWithLiveTick.length) return;

    const currentCount = chartDataWithLiveTick.length;
    const previousCount = lastCandleCountRef.current;

    // If data changed significantly, replace all data
    if (Math.abs(currentCount - previousCount) > 1) {
      seriesRef.current.setData(chartDataWithLiveTick);
      chartRef.current?.timeScale().fitContent();
    } else {
      // Otherwise just update the last candle
      const lastCandle = chartDataWithLiveTick[chartDataWithLiveTick.length - 1];
      if (lastCandle) {
        seriesRef.current.update(lastCandle);
      }
    }

    lastCandleCountRef.current = currentCount;
  }, [chartDataWithLiveTick]);

  // Handle infinite scroll for historical data
  const handleVisibleLogicalRangeChange = useCallback(() => {
    if (!chartRef.current || !onLoadMoreHistory || isLoadingRef.current || !canLoadMoreHistory) {
      return;
    }

    const logicalRange = chartRef.current.timeScale().getVisibleLogicalRange();
    if (!logicalRange) return;

    // Load more when user scrolls near the left edge
    if (logicalRange.from < 5) {
      isLoadingRef.current = true;
      onLoadMoreHistory().finally(() => {
        isLoadingRef.current = false;
      });
    }
  }, [onLoadMoreHistory, canLoadMoreHistory]);

  // Subscribe to visible range changes for infinite scroll
  useEffect(() => {
    if (!chartRef.current || !onLoadMoreHistory) return;

    const timeScale = chartRef.current.timeScale();
    timeScale.subscribeVisibleLogicalRangeChange(handleVisibleLogicalRangeChange);

    return () => {
      timeScale.unsubscribeVisibleLogicalRangeChange(handleVisibleLogicalRangeChange);
    };
  }, [handleVisibleLogicalRangeChange, onLoadMoreHistory]);

  return (
    <div className={`relative ${className}`}>
      {isLoadingMoreHistory && (
        <div className="absolute left-4 top-4 z-10 flex items-center gap-2 rounded-lg bg-slate-900/90 px-3 py-2 text-xs text-slate-300 backdrop-blur-sm">
          <Loader2 className="h-3 w-3 animate-spin" />
          <span>Loading historical data...</span>
        </div>
      )}
      <div
        ref={containerRef}
        style={{ width: "100%", height }}
        className="rounded-lg"
        role="img"
        aria-label={`Candlestick chart showing ${candleInterval}${candleUnit} candles`}
      />
      {chartData.length > 0 && (
        <div className="absolute bottom-4 right-4 rounded-lg bg-slate-900/90 px-3 py-2 text-xs text-slate-400 backdrop-blur-sm">
          {chartData.length} candles • {candleInterval}
          {candleUnit}
          {liveTick && <span className="ml-2 text-emerald-400">● Live</span>}
        </div>
      )}
    </div>
  );
}
