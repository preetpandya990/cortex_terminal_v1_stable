/**
 * Cortex AI — useChart Hook
 * ==========================
 * Shared chart lifecycle hook that eliminates the duplicated setup boilerplate
 * from CandlestickChart.tsx and PriceChart.tsx.
 *
 * Manages:
 *  - Chart creation with consistent options
 *  - ResizeObserver for responsive sizing
 *  - Cleanup on unmount
 *
 * Usage:
 *  const { chartRef, containerRef } = useChart({ layout: { ... } })
 *  // then attach series to chartRef.current
 */
import { useEffect, useRef } from "react";
import {
  createChart,
  IChartApi,
  DeepPartial,
  ChartOptions,
  ColorType,
} from "lightweight-charts";

interface UseChartOptions {
  chartOptions?: DeepPartial<ChartOptions>;
}

export function useChart({ chartOptions = {} }: UseChartOptions = {}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

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
      height: containerRef.current.clientHeight || 400,
      ...chartOptions,
    });

    chartRef.current = chart;

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
    };
  }, []); // intentionally empty — chart created once

  return { chartRef, containerRef };
}