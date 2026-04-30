"use client";

import { useEffect, useRef, useMemo, useCallback, useState } from "react";
import {
  createChart,
  IChartApi,
  ISeriesApi,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  UTCTimestamp,
  CrosshairMode,
  ColorType,
  TickMarkType,
  LineStyle,
} from "lightweight-charts";
import {
  transformCandlesForChart,
  transformVolumeForChart,
  updateLastCandleWithLivePrice,
} from "@/lib/candle-transforms";
import {
  computeIndicators,
  computePaneLayout,
  INDICATOR_META,
  OSCILLATOR_INDICATORS,
  type IndicatorId,
  type IndicatorData,
  type LinePoint,
  type BBPoint,
  type MACDPoint,
  type StochPoint,
} from "@/lib/indicators";
import type { UpstoxCandle, UpstoxLtpTick } from "@/types/upstox";
import type { CandleUnit } from "@/lib/chart-policy";
import { ChevronsRight, Loader2 } from "lucide-react";

// ─── Props ────────────────────────────────────────────────────────────────────

export interface CandlestickChartProps {
  candles: UpstoxCandle[];
  liveTick?: UpstoxLtpTick | null;
  candleUnit: CandleUnit;
  candleInterval: number;
  /**
   * Explicit pixel height. When omitted the chart fills its parent via
   * `height: "100%"` and a ResizeObserver keeps lightweight-charts in sync.
   */
  height?: number;
  canLoadMoreHistory?: boolean;
  isLoadingMoreHistory?: boolean;
  onLoadMoreHistory?: () => Promise<void>;
  /** Ordered list of active indicator IDs from ChartPreferencesContext. */
  activeIndicators?: IndicatorId[];
  /**
   * When true the market is live — the Jump-to-Latest button shows a pulsing
   * green dot and is labelled "Live".  When false it shows "Latest".
   */
  isLive?: boolean;
  className?: string;
}

// ─── OHLCV legend ─────────────────────────────────────────────────────────────

interface OhlcvLegend {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

const PRICE_FMT = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function fmtVol(v: number): string {
  if (v >= 1_000_000_000) return `${(v / 1_000_000_000).toFixed(2)}B`;
  if (v >= 1_000_000)     return `${(v / 1_000_000).toFixed(2)}M`;
  if (v >= 1_000)         return `${(v / 1_000).toFixed(1)}K`;
  return v.toLocaleString("en-IN");
}

function fmtLegendTime(ts: number, unit: CandleUnit): string {
  const d = new Date(ts * 1000);
  const datePart = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(d);
  if (unit === "days" || unit === "weeks" || unit === "months") return datePart;
  const timePart = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
  }).format(d);
  return `${datePart} · ${timePart}`;
}

// ─── Colour palette ───────────────────────────────────────────────────────────
// All colours chosen for readability on the dark chart background (slate-950).

const COLORS = {
  MACD_HIST_POS:  "rgba(16,  185, 129, 0.7)",
  MACD_HIST_NEG:  "rgba(239,  68,  68, 0.7)",
  RSI_OB:         "#ef4444",  // overbought reference line
  RSI_OS:         "#10b981",  // oversold reference line
  STOCH_OB:       "#ef4444",
  STOCH_OS:       "#10b981",
} as const;

// ─── Component ────────────────────────────────────────────────────────────────

export function CandlestickChart({
  candles,
  liveTick,
  candleUnit,
  candleInterval,
  height,
  canLoadMoreHistory = false,
  isLoadingMoreHistory = false,
  onLoadMoreHistory,
  activeIndicators = [],
  isLive = false,
  className = "",
}: CandlestickChartProps) {
  const containerRef    = useRef<HTMLDivElement>(null);
  const chartRef        = useRef<IChartApi | null>(null);
  const seriesRef       = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<"Histogram">  | null>(null);
  const lastCandleCountRef  = useRef(0);
  const isLoadingMoreRef    = useRef(false);
  // Minimum gap between consecutive load-more triggers (ms).
  // Prevents burst-firing while the chart is still rendering newly fetched data.
  const lastTriggerAtRef    = useRef(0);

  // OHLCV legend — crosshair-driven, falls back to the latest candle.
  const [ohlcvLegend, setOhlcvLegend] = useState<OhlcvLegend | null>(null);
  const latestOhlcvRef = useRef<OhlcvLegend | null>(null);
  const isHoveringRef  = useRef(false);

  // Jump-to-Latest — shown whenever the user has scrolled away from the right edge.
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);

  // Per-indicator series map: each entry is an ordered array of series (some
  // indicators — BB, MACD, STOCH — need more than one series).
  const indicatorSeriesMapRef = useRef<Map<IndicatorId, ISeriesApi<any>[]>>(new Map());

  // Keep callbacks stable in refs so effects don't re-register on every render.
  const onLoadMoreHistoryRef = useRef(onLoadMoreHistory);
  useEffect(() => { onLoadMoreHistoryRef.current = onLoadMoreHistory; }, [onLoadMoreHistory]);

  // ── Transform candles ───────────────────────────────────────────────────────

  const chartData = useMemo(
    () => transformCandlesForChart(candles),
    [candles],
  );

  const chartDataWithLiveTick = useMemo(() => {
    if (!liveTick || !candles.length) return chartData;
    return transformCandlesForChart(updateLastCandleWithLivePrice(candles, liveTick.last_price));
  }, [chartData, liveTick, candles]);

  const volumeData = useMemo(() => transformVolumeForChart(candles), [candles]);

  // Compute all active indicator values; recomputes only when candles or
  // indicator selection changes (not on every live tick).
  const indicatorData = useMemo(
    () => computeIndicators(candles, activeIndicators),
    [candles, activeIndicators],
  );

  // ── Chart initialisation ────────────────────────────────────────────────────

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
        mode: CrosshairMode.Normal,
        vertLine: { labelBackgroundColor: "#374151" },
        horzLine: { labelBackgroundColor: "#374151" },
      },
      timeScale: {
        borderColor: "#374151",
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: (time: number, tickMarkType: TickMarkType) => {
          const date = new Date(time * 1000);
          if (
            tickMarkType === TickMarkType.DayOfMonth ||
            tickMarkType === TickMarkType.Month ||
            tickMarkType === TickMarkType.Year
          ) {
            return new Intl.DateTimeFormat("en-GB", {
              timeZone: "Asia/Kolkata",
              day: "2-digit",
              month: "2-digit",
              year: "numeric",
            }).format(date).replace(/\//g, "-");
          }
          return new Intl.DateTimeFormat("en-IN", {
            timeZone: "Asia/Kolkata",
            hour: "numeric",
            minute: "2-digit",
            hour12: true,
          }).format(date);
        },
      },
      localization: {
        timeFormatter: (timestamp: number) => {
          const date     = new Date(timestamp * 1000);
          const datePart = new Intl.DateTimeFormat("en-GB", {
            timeZone: "Asia/Kolkata",
            day: "2-digit",
            month: "2-digit",
            year: "numeric",
          }).format(date);
          const timePart = new Intl.DateTimeFormat("en-IN", {
            timeZone: "Asia/Kolkata",
            hour: "numeric",
            minute: "2-digit",
            hour12: true,
          }).format(date);
          return `${datePart} ${timePart}`;
        },
      },
      rightPriceScale: { borderColor: "#374151" },
      watermark:       { visible: false },
      width:  containerRef.current.clientWidth,
      height: height ?? (containerRef.current.clientHeight || 400),
    });

    chartRef.current = chart;

    // Candlestick series
    const series = chart.addSeries(CandlestickSeries, {
      upColor:        "#10b981",
      downColor:      "#ef4444",
      borderUpColor:  "#10b981",
      borderDownColor:"#ef4444",
      wickUpColor:    "#10b981",
      wickDownColor:  "#ef4444",
    });
    seriesRef.current = series;

    // Volume histogram
    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceScaleId:    "volume",
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 },
    });
    volumeSeriesRef.current = volumeSeries;

    // ── Crosshair OHLCV legend ──────────────────────────────────────────────
    // Reads from refs so the handler is never stale across data updates.

    chart.subscribeCrosshairMove((param) => {
      const cs = seriesRef.current;
      const vs = volumeSeriesRef.current;

      if (!param.time || !cs) {
        // Crosshair left the chart — snap back to the latest candle.
        isHoveringRef.current = false;
        if (latestOhlcvRef.current) setOhlcvLegend(latestOhlcvRef.current);
        return;
      }

      isHoveringRef.current = true;

      const cd = param.seriesData.get(cs);
      const vd = vs ? param.seriesData.get(vs) : undefined;

      if (cd && "open" in cd) {
        setOhlcvLegend({
          time:   param.time as number,
          open:   cd.open,
          high:   cd.high,
          low:    cd.low,
          close:  cd.close,
          volume: vd && "value" in vd ? (vd as { value: number }).value : 0,
        });
      }
    });

    // Jump-to-Latest: show the button whenever the right edge scrolls away from
    // the last bar.  Tolerance of 1.5 bars avoids false positives from partial
    // right-side padding.  Reads lastCandleCountRef so it never goes stale.
    const handleJumpDetect = () => {
      const range = chart.timeScale().getVisibleLogicalRange();
      setShowJumpToLatest(!!range && range.to < lastCandleCountRef.current - 1.5);
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(handleJumpDetect);

    const resizeObserver = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry || !chartRef.current) return;
      chartRef.current.applyOptions({
        width:  entry.contentRect.width,
        height: entry.contentRect.height,
      });
    });
    resizeObserver.observe(containerRef.current);

    return () => {
      resizeObserver.disconnect();
      chartRef.current?.remove(); // also unsubscribes all crosshairMove listeners
      chartRef.current        = null;
      seriesRef.current       = null;
      volumeSeriesRef.current = null;
      indicatorSeriesMapRef.current.clear();
      lastCandleCountRef.current = 0;
      isHoveringRef.current      = false;
      latestOhlcvRef.current     = null;
      setShowJumpToLatest(false);
    };
  }, [height]);

  // ── Price + volume data update ──────────────────────────────────────────────

  useEffect(() => {
    if (!seriesRef.current || !chartDataWithLiveTick.length) return;

    const currentCount  = chartDataWithLiveTick.length;
    const previousCount = lastCandleCountRef.current;

    // Helper: sync the legend ref and state from the last candle + volume bar.
    const syncLegendToLatest = () => {
      const lastCandle = chartDataWithLiveTick[chartDataWithLiveTick.length - 1];
      const lastVol    = volumeData[volumeData.length - 1];
      if (!lastCandle) return;
      const latest: OhlcvLegend = {
        time:   lastCandle.time as number,
        open:   lastCandle.open,
        high:   lastCandle.high,
        low:    lastCandle.low,
        close:  lastCandle.close,
        volume: lastVol?.value ?? 0,
      };
      latestOhlcvRef.current = latest;
      if (!isHoveringRef.current) setOhlcvLegend(latest);
    };

    if (Math.abs(currentCount - previousCount) > 1) {
      try {
        let savedRange: { from: number; to: number } | null = null;
        if (chartRef.current && previousCount > 0 && currentCount > previousCount) {
          const logical = chartRef.current.timeScale().getVisibleLogicalRange();
          if (logical) savedRange = { from: logical.from, to: logical.to };
        }

        seriesRef.current.setData(chartDataWithLiveTick);

        if (volumeSeriesRef.current && volumeData.length) {
          volumeSeriesRef.current.setData(volumeData);
        }

        syncLegendToLatest();

        if (savedRange && chartRef.current && currentCount > previousCount) {
          const candlesAdded = currentCount - previousCount;
          const adjusted = {
            from: savedRange.from + candlesAdded,
            to:   savedRange.to   + candlesAdded,
          };
          setTimeout(() => {
            chartRef.current?.timeScale().setVisibleLogicalRange(adjusted);
          }, 0);
        } else {
          chartRef.current?.timeScale().fitContent();
        }
      } catch (error) {
        console.error("[CandlestickChart] setData failed:", error);
      }
    } else {
      const lastCandle = chartDataWithLiveTick[chartDataWithLiveTick.length - 1];
      if (lastCandle) {
        try {
          seriesRef.current.update(lastCandle);
        } catch (error) {
          console.error("[CandlestickChart] update failed:", error);
        }
        syncLegendToLatest();
      }
    }

    lastCandleCountRef.current = currentCount;
  }, [chartDataWithLiveTick, volumeData]);

  // ── Indicator series lifecycle + data ───────────────────────────────────────
  //
  // Runs whenever activeIndicators or indicatorData changes (which covers both
  // indicator toggle and candle data updates).
  // Does NOT run on live ticks since indicatorData is derived from `candles`.

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    const seriesMap   = indicatorSeriesMapRef.current;
    const activeSet   = new Set(activeIndicators);

    // Step 1: Remove series for deactivated indicators.
    for (const [id, seriesList] of seriesMap) {
      if (!activeSet.has(id)) {
        seriesList.forEach(s => { try { chart.removeSeries(s); } catch {} });
        seriesMap.delete(id);
      }
    }

    // Step 2: Create series for newly activated indicators that don't have one yet.
    for (const id of activeIndicators) {
      if (!seriesMap.has(id)) {
        const created = createIndicatorSeries(chart, id);
        seriesMap.set(id, created);
      }
    }

    // Step 3: Push the latest data to all active series.
    for (const id of activeIndicators) {
      const seriesList = seriesMap.get(id);
      if (seriesList) setIndicatorSeriesData(id, seriesList, indicatorData);
    }

    // Step 4: Apply pane layout to keep all scales non-overlapping.
    const activeOscillators = OSCILLATOR_INDICATORS.filter(id => activeSet.has(id));
    applyPaneLayout(chart, activeOscillators, volumeSeriesRef.current, seriesMap);

  }, [activeIndicators, indicatorData]);

  // ── Infinite scroll subscription ────────────────────────────────────────────

  const handleVisibleRangeChange = useCallback(() => {
    if (!chartRef.current || !onLoadMoreHistoryRef.current || isLoadingMoreRef.current || !canLoadMoreHistory) return;
    const logicalRange = chartRef.current.timeScale().getVisibleLogicalRange();
    if (!logicalRange) return;

    // Trigger when 10 bars remain on the left — earlier than the previous threshold
    // of 5, so data is ready before the user reaches the left edge (TradingView standard).
    if (logicalRange.from < 10) {
      const now = Date.now();
      // 800 ms cooldown prevents burst-triggering during fast panning even after
      // a fetch completes (isLoadingMoreRef resets in .finally()).
      if (now - lastTriggerAtRef.current < 800) return;
      lastTriggerAtRef.current = now;
      isLoadingMoreRef.current = true;
      onLoadMoreHistoryRef.current().finally(() => { isLoadingMoreRef.current = false; });
    }
  }, [canLoadMoreHistory]);

  useEffect(() => {
    if (!chartRef.current) return;
    const timeScale = chartRef.current.timeScale();
    timeScale.subscribeVisibleLogicalRangeChange(handleVisibleRangeChange);
    return () => { timeScale.unsubscribeVisibleLogicalRangeChange(handleVisibleRangeChange); };
  }, [handleVisibleRangeChange]);

  // ── Render ──────────────────────────────────────────────────────────────────

  const isBullish = ohlcvLegend ? ohlcvLegend.close >= ohlcvLegend.open : true;

  return (
    <div className={`relative h-full w-full ${className}`}>
      {/* ── OHLCV crosshair legend ─────────────────────────────────────────── */}
      {ohlcvLegend && (
        <div
          className={`pointer-events-none absolute left-3 top-3 z-10 select-none overflow-hidden rounded-xl border ${
            isBullish ? "border-emerald-500/25" : "border-red-500/25"
          }`}
          aria-live="polite"
          aria-label="OHLCV data for hovered candle"
        >
          {/* Accent bar */}
          <div className={`h-0.5 w-full ${isBullish ? "bg-emerald-500" : "bg-red-500"}`} />

          <div className="bg-transparent px-4 py-3">
            {/* Date / time */}
            <p className="mb-3 text-[10px] font-semibold uppercase tracking-widest text-slate-300">
              {fmtLegendTime(ohlcvLegend.time, candleUnit)}
            </p>

            {/* O H L C — stacked label + value columns */}
            <div className="flex gap-5">
              {(
                [
                  { label: "O", value: ohlcvLegend.open,  color: "text-white" },
                  { label: "H", value: ohlcvLegend.high,  color: "text-emerald-300" },
                  { label: "L", value: ohlcvLegend.low,   color: "text-red-300" },
                  { label: "C", value: ohlcvLegend.close, color: isBullish ? "text-emerald-300" : "text-red-300" },
                ] as const
              ).map(({ label, value, color }) => (
                <div key={label} className="flex flex-col gap-1">
                  <span className="text-[9px] font-bold uppercase tracking-widest text-slate-400">
                    {label}
                  </span>
                  <span className={`font-mono text-sm font-bold tabular-nums ${color}`}>
                    {PRICE_FMT.format(value)}
                  </span>
                </div>
              ))}
            </div>

            {/* Volume */}
            {ohlcvLegend.volume > 0 && (
              <div className="mt-2.5 flex items-center gap-2 border-t border-white/10 pt-2.5">
                <span className="text-[9px] font-bold uppercase tracking-widest text-slate-400">
                  Vol
                </span>
                <span className="font-mono text-xs font-semibold tabular-nums text-slate-200">
                  {fmtVol(ohlcvLegend.volume)}
                </span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Loading more indicator — top-right to avoid legend overlap ─────── */}
      {isLoadingMoreHistory && (
        <div className="absolute right-4 top-3 z-10 flex items-center gap-2 rounded-lg bg-slate-900/90 px-3 py-2 text-xs text-slate-300 backdrop-blur-sm">
          <Loader2 className="h-3 w-3 animate-spin" />
          <span>Loading historical data…</span>
        </div>
      )}

      {/* ── Jump to Latest / Live button — bottom-right, industry-standard position */}
      {showJumpToLatest && (
        <button
          onClick={() => chartRef.current?.timeScale().scrollToRealTime()}
          className="absolute bottom-4 right-4 z-10 flex items-center gap-1.5 rounded-lg border border-slate-700 bg-slate-900/90 px-3 py-1.5 text-xs font-medium text-slate-200 backdrop-blur-sm transition-colors hover:border-slate-600 hover:bg-slate-800 hover:text-white"
          aria-label={isLive ? "Jump to live price" : "Jump to latest candle"}
        >
          {isLive && (
            <span className="relative flex h-2 w-2 shrink-0">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-emerald-500" />
            </span>
          )}
          <ChevronsRight className="h-3.5 w-3.5 shrink-0" />
          {isLive ? "Live" : "Latest"}
        </button>
      )}

      <div
        ref={containerRef}
        style={{ width: "100%", height: height ?? "100%" }}
        className="rounded-lg"
        role="img"
        aria-label={`Candlestick chart — ${candleInterval}${candleUnit}`}
      />
    </div>
  );
}

// ─── Indicator series factory ─────────────────────────────────────────────────

function createIndicatorSeries(
  chart: IChartApi,
  id: IndicatorId,
): ISeriesApi<any>[] {
  const meta = INDICATOR_META[id];

  switch (id) {
    // ── Single line overlays ────────────────────────────────────────────────
    case 'SMA_20':
    case 'SMA_50':
    case 'EMA_20':
    case 'EMA_50':
    case 'VWAP': {
      const s = chart.addSeries(LineSeries, {
        color:            meta.color,
        lineWidth:        1,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: true,
      });
      return [s];
    }

    // ── Bollinger Bands — three lines sharing the price scale ───────────────
    case 'BB': {
      const opts = {
        color:            meta.color,
        lineWidth:        1 as const,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      };
      const upper  = chart.addSeries(LineSeries, { ...opts, lineStyle: LineStyle.Dashed });
      const middle = chart.addSeries(LineSeries, { ...opts, lineWidth: 1 });
      const lower  = chart.addSeries(LineSeries, { ...opts, lineStyle: LineStyle.Dashed });
      return [upper, middle, lower];
    }

    // ── RSI — dedicated pane ────────────────────────────────────────────────
    case 'RSI': {
      const s = chart.addSeries(LineSeries, {
        color:            meta.color,
        lineWidth:        1,
        priceScaleId:     'rsi',
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: true,
      });
      s.priceScale().applyOptions({
        scaleMargins:     { top: 0.1, bottom: 0.1 },
        borderVisible:    false,
        drawTicks:        false,
        entireTextOnly:   true,
      });
      // Overbought / oversold reference lines
      s.createPriceLine({ price: 70, color: COLORS.RSI_OB, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false });
      s.createPriceLine({ price: 30, color: COLORS.RSI_OS, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false });
      s.createPriceLine({ price: 50, color: '#475569',     lineWidth: 1, lineStyle: LineStyle.Dotted,  axisLabelVisible: false });
      return [s];
    }

    // ── MACD — MACD line + signal line + histogram ──────────────────────────
    case 'MACD': {
      const macdLine = chart.addSeries(LineSeries, {
        color:            meta.color,          // blue
        lineWidth:        1,
        priceScaleId:     'macd',
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: true,
      });
      const signalLine = chart.addSeries(LineSeries, {
        color:            '#f59e0b',           // amber
        lineWidth:        1,
        priceScaleId:     'macd',
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      const histogram = chart.addSeries(HistogramSeries, {
        priceScaleId:     'macd',
        lastValueVisible: false,
        priceLineVisible: false,
      });
      macdLine.priceScale().applyOptions({
        scaleMargins:   { top: 0.1, bottom: 0.1 },
        borderVisible:  false,
        drawTicks:      false,
        entireTextOnly: true,
      });
      return [macdLine, signalLine, histogram];
    }

    // ── Stochastic — %K + %D lines ──────────────────────────────────────────
    case 'STOCH': {
      const kLine = chart.addSeries(LineSeries, {
        color:            meta.color,          // cyan
        lineWidth:        1,
        priceScaleId:     'stoch',
        priceLineVisible: false,
        lastValueVisible: true,
        crosshairMarkerVisible: true,
      });
      const dLine = chart.addSeries(LineSeries, {
        color:            '#f59e0b',           // amber
        lineWidth:        1,
        priceScaleId:     'stoch',
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      kLine.priceScale().applyOptions({
        scaleMargins:   { top: 0.1, bottom: 0.1 },
        borderVisible:  false,
        drawTicks:      false,
        entireTextOnly: true,
      });
      kLine.createPriceLine({ price: 80, color: COLORS.STOCH_OB, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false });
      kLine.createPriceLine({ price: 20, color: COLORS.STOCH_OS, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: false });
      return [kLine, dLine];
    }

    default:
      return [];
  }
}

// ─── Data pusher ──────────────────────────────────────────────────────────────

function setIndicatorSeriesData(
  id: IndicatorId,
  seriesList: ISeriesApi<any>[],
  data: IndicatorData,
): void {
  try {
    switch (id) {
      case 'SMA_20': setLineData(seriesList[0], data.SMA_20); break;
      case 'SMA_50': setLineData(seriesList[0], data.SMA_50); break;
      case 'EMA_20': setLineData(seriesList[0], data.EMA_20); break;
      case 'EMA_50': setLineData(seriesList[0], data.EMA_50); break;
      case 'VWAP':   setLineData(seriesList[0], data.VWAP);   break;
      case 'RSI':    setLineData(seriesList[0], data.RSI);    break;

      case 'BB': {
        const bb = data.BB ?? [];
        seriesList[0]?.setData(bb.map(d => ({ time: d.time as UTCTimestamp, value: d.upper  })));
        seriesList[1]?.setData(bb.map(d => ({ time: d.time as UTCTimestamp, value: d.middle })));
        seriesList[2]?.setData(bb.map(d => ({ time: d.time as UTCTimestamp, value: d.lower  })));
        break;
      }

      case 'MACD': {
        const macd = data.MACD ?? [];
        seriesList[0]?.setData(macd.map(d => ({ time: d.time as UTCTimestamp, value: d.macd    })));
        seriesList[1]?.setData(macd.map(d => ({ time: d.time as UTCTimestamp, value: d.signal  })));
        seriesList[2]?.setData(macd.map(d => ({
          time:  d.time as UTCTimestamp,
          value: d.histogram,
          color: d.histogram >= 0 ? COLORS.MACD_HIST_POS : COLORS.MACD_HIST_NEG,
        })));
        break;
      }

      case 'STOCH': {
        const stoch = data.STOCH ?? [];
        seriesList[0]?.setData(stoch.map(d => ({ time: d.time as UTCTimestamp, value: d.k })));
        seriesList[1]?.setData(stoch.map(d => ({ time: d.time as UTCTimestamp, value: d.d })));
        break;
      }
    }
  } catch (error) {
    console.error(`[CandlestickChart] Failed to update indicator ${id}:`, error);
  }
}

function setLineData(series: ISeriesApi<any> | undefined, data: LinePoint[] | undefined) {
  if (!series || !data) return;
  series.setData(data.map(d => ({ time: d.time as UTCTimestamp, value: d.value })));
}

// ─── Pane layout applicator ───────────────────────────────────────────────────

function applyPaneLayout(
  chart: IChartApi,
  activeOscillators: IndicatorId[],
  volumeSeries: ISeriesApi<"Histogram"> | null,
  seriesMap: Map<IndicatorId, ISeriesApi<any>[]>,
): void {
  const layout = computePaneLayout(activeOscillators);

  // Resize the main price pane to make room for oscillators below.
  chart.priceScale('right').applyOptions({ scaleMargins: layout.price });

  // Reposition volume within the (now possibly smaller) price zone.
  if (volumeSeries) {
    volumeSeries.priceScale().applyOptions({ scaleMargins: layout.volume });
  }

  // Position each active oscillator in its designated zone.
  for (const { id, top, bottom } of layout.oscillators) {
    const seriesList = seriesMap.get(id);
    // The first series in the list owns the price scale for that oscillator.
    if (seriesList?.[0]) {
      seriesList[0].priceScale().applyOptions({ scaleMargins: { top, bottom } });
    }
  }
}
