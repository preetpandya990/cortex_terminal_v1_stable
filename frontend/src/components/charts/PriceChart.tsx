/**
 * Cortex AI — PriceChart
 * =======================
 * Line chart for price history using lightweight-charts v5.
 */
"use client";

import { useEffect, useRef } from "react";
import {
  createChart,
  IChartApi,
  ISeriesApi,
  LineSeries,
  LineData,
  UTCTimestamp,
  ColorType,
} from "lightweight-charts";
import type { UpstoxCandleData } from "@/types/upstox";

interface PriceChartProps {
  data: UpstoxCandleData[];
  height?: number;
  color?: string;
}

export function PriceChart({ data, height = 300, color = "#6366f1" }: PriceChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);

  // Initialize chart and series together
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
      rightPriceScale: { borderColor: "#374151" },
      width: containerRef.current.clientWidth,
      height,
    });

    chartRef.current = chart;
    seriesRef.current = chart.addSeries(LineSeries, {
      color,
      lineWidth: 2,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 4,
      lastValueVisible: true,
      priceLineVisible: false,
    });

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
  }, [height, color]);

  // Update data
  useEffect(() => {
    if (!seriesRef.current || !data.length) return;

    const mapped: LineData[] = data
      .map((candle) => ({
        time: (new Date(candle.timestamp as string).getTime() / 1000) as UTCTimestamp,
        value: candle.close,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    seriesRef.current.setData(mapped);
    chartRef.current?.timeScale().fitContent();
  }, [data]);

  return (
    <div
      ref={containerRef}
      style={{ width: "100%", height }}
      aria-label="Price chart"
    />
  );
}
