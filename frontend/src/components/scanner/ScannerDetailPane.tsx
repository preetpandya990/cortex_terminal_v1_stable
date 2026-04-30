"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  X,
  Plus,
  Check,
  Loader2,
  TrendingUp,
  TrendingDown,
  Zap,
  AlertTriangle,
  Activity,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { CandlestickChart } from "@/components/charts/CandlestickChart";
import { TimeframeSelector } from "@/components/charts/TimeframeSelector";
import { IndicatorSelector } from "@/components/charts/IndicatorSelector";
import { useWatchlist } from "@/hooks/useWatchlist";
import { useAuth } from "@/contexts/AuthContext";
import { useChartPreferences, TIMEFRAME_OPTIONS, type TimeframeOption } from "@/contexts/ChartPreferencesContext";
import type { IndicatorId } from "@/lib/indicators";
import { upstoxAPI, WS_BASE_URL, isNetworkError } from "@/lib/api";
import { LivePriceBadge } from "@/components/shared/LivePriceBadge";
import { mergeCandles } from "@/lib/candle-transforms";
import {
  getISTTodayYMD,
  getISTMarketSession,
  includesTodayOrYesterday,
  resolveCandleSourceMode,
  diffDaysInclusive,
  addDays,
  adjustDateRangeForTimeframe,
  getNoDataMessage,
  getMinAvailableDateForUnit,
  type CandleUnit,
  type CandleSourceMode,
  type HistoricalState,
} from "@/lib/chart-policy";
import type { StockAnalysis } from "@/types/market";
import type { UpstoxCandlesResponse, UpstoxLtpTick, UpstoxTickStreamMessage } from "@/types/upstox";
import type { ScannerTab } from "./ScanResults";

// ─── Constants ────────────────────────────────────────────────────────────────

const TICK_STREAM_MAX_RECONNECTS = 10;
const TICK_STREAM_BASE_DELAY_MS = 1_000;
const TICK_STREAM_MAX_DELAY_MS = 30_000;

// Default to daily candles over 6 months — gives trend context for why the
// instrument was flagged by the scanner today.
const SCANNER_DEFAULT_TIMEFRAME =
  TIMEFRAME_OPTIONS.find((t) => t.id === "1D") ?? TIMEFRAME_OPTIONS[TIMEFRAME_OPTIONS.length - 1];

type TickStreamStatus = "idle" | "connecting" | "live" | "reconnecting" | "error";

type FetchCandlesParams = {
  unit: CandleUnit;
  interval: number;
  fromDate: string;
  toDate: string;
  mode: CandleSourceMode;
};

// ─── Formatters ───────────────────────────────────────────────────────────────

const rupeeFormatter = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const compactFormatter = new Intl.NumberFormat("en-IN", {
  notation: "compact",
  maximumFractionDigits: 2,
});

// ─── Helpers ──────────────────────────────────────────────────────────────────

function candleTimestampToDate(ts: string | number): string {
  if (typeof ts === "string") return ts.split("T")[0];
  return new Date(ts * 1000).toISOString().split("T")[0];
}

function buildTickStreamUrl(instrumentKey: string): string {
  const wsBase = WS_BASE_URL.replace(/\/$/, "");
  const hasApiV1Prefix = /\/api\/v1$/i.test(wsBase);
  const path = hasApiV1Prefix ? "/upstox/ticks/ws" : "/api/v1/upstox/ticks/ws";
  const url = new URL(`${wsBase}${path}`);
  url.searchParams.set("instrument_key", instrumentKey);
  return url.toString();
}

function getReconnectDelay(attempt: number): number {
  return Math.min(TICK_STREAM_BASE_DELAY_MS * Math.pow(2, attempt), TICK_STREAM_MAX_DELAY_MS);
}

// Derive exchange label from NSE_EQ|... instrument key
function exchangeFromKey(instrumentKey: string): string {
  const prefix = instrumentKey.split("|")[0] ?? "";
  if (prefix.startsWith("NSE")) return "NSE";
  if (prefix.startsWith("BSE")) return "BSE";
  return prefix;
}

// ─── Scanner context builder ──────────────────────────────────────────────────

interface ContextCopy {
  headline: string;
  subline: string;
  circuitFlag?: string;
}

function buildContextCopy(stock: StockAnalysis, listType: ScannerTab): ContextCopy {
  const { price_change, price_change_pct, current_price, previous_close, volume, avg_volume, volume_ratio } = stock;
  const absChange = Math.abs(price_change);
  const absPct = Math.abs(price_change_pct);
  const atUpperCircuit = price_change_pct >= 19.95;
  const atLowerCircuit = price_change_pct <= -19.95;

  if (listType === "gainers") {
    return {
      headline: `Advanced ${rupeeFormatter.format(absChange)} (+${absPct.toFixed(2)}%) from previous close`,
      subline: `Prev close ${rupeeFormatter.format(previous_close)} → Current ${rupeeFormatter.format(current_price)}`,
      circuitFlag: atUpperCircuit ? "NSE Upper Circuit (20% limit)" : undefined,
    };
  }

  if (listType === "losers") {
    return {
      headline: `Declined ${rupeeFormatter.format(absChange)} (${absPct.toFixed(2)}%) from previous close`,
      subline: `Prev close ${rupeeFormatter.format(previous_close)} → Current ${rupeeFormatter.format(current_price)}`,
      circuitFlag: atLowerCircuit ? "NSE Lower Circuit (20% limit)" : undefined,
    };
  }

  return {
    headline: `Trading at ${volume_ratio.toFixed(1)}× its 20-session average volume`,
    subline: `Today: ${compactFormatter.format(volume)} shares · 20-day avg: ${compactFormatter.format(avg_volume)} shares`,
  };
}

// ─── Sub-components ────────────────────────────────────────────────────────────

function ContextBanner({ stock, listType }: { stock: StockAnalysis; listType: ScannerTab }) {
  const copy = buildContextCopy(stock, listType);
  const isGainer = listType === "gainers";
  const isLoser = listType === "losers";
  const isVolume = listType === "volume";

  const bannerCls = isGainer
    ? "border-emerald-200 bg-emerald-50"
    : isLoser
    ? "border-red-200 bg-red-50"
    : "border-amber-200 bg-amber-50";

  const badgeCls = isGainer
    ? "bg-emerald-100 text-emerald-800 border-emerald-300"
    : isLoser
    ? "bg-red-100 text-red-800 border-red-300"
    : "bg-amber-100 text-amber-800 border-amber-300";

  const headlineCls = isGainer
    ? "text-emerald-900"
    : isLoser
    ? "text-red-900"
    : "text-amber-900";

  const sublineCls = isGainer
    ? "text-emerald-700"
    : isLoser
    ? "text-red-700"
    : "text-amber-700";

  const Icon = isGainer ? TrendingUp : isLoser ? TrendingDown : Zap;
  const label = isGainer ? "Top Gainer" : isLoser ? "Top Loser" : "Volume Spike";

  return (
    <div className={`rounded-xl border px-6 py-5 ${bannerCls}`}>
      <div className="flex flex-wrap items-center gap-2">
        <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold ${badgeCls}`}>
          <Icon className="h-3 w-3" />
          {label}
        </span>
        {copy.circuitFlag && (
          <span className="inline-flex items-center gap-1 rounded-full border border-orange-300 bg-orange-100 px-2 py-0.5 text-xs font-medium text-orange-800">
            <Activity className="h-3 w-3" />
            {copy.circuitFlag}
          </span>
        )}
      </div>
      <p className={`mt-2.5 text-sm font-semibold ${headlineCls}`}>{copy.headline}</p>
      <p className={`mt-0.5 text-xs ${sublineCls}`}>{copy.subline}</p>
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
}

function StatCard({ label, value, sub }: StatCardProps) {
  return (
    <div className="flex flex-col gap-1.5 rounded-xl border border-slate-200 bg-slate-50 px-6 py-5">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</p>
      <div className="text-xl font-semibold tabular-nums text-slate-900">{value}</div>
      {sub && <div className="text-xs text-slate-500">{sub}</div>}
    </div>
  );
}

function StatsGrid({ stock, listType }: { stock: StockAnalysis; listType: ScannerTab }) {
  const { price_change, price_change_pct, current_price, volume, avg_volume, volume_ratio, rsi } = stock;
  const isPositive = price_change >= 0;

  // RSI colour
  const rsiColour =
    rsi === null
      ? "text-slate-400"
      : rsi < 30
      ? "text-emerald-600"
      : rsi > 70
      ? "text-red-600"
      : "text-slate-700";

  const rsiLabel = rsi === null ? null : rsi < 30 ? "Oversold" : rsi > 70 ? "Overbought" : "Neutral";

  // Volume ratio colour
  const volRatioColour =
    volume_ratio >= 10
      ? "text-red-600"
      : volume_ratio >= 5
      ? "text-orange-600"
      : volume_ratio >= 2
      ? "text-amber-600"
      : "text-slate-700";

  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
      <StatCard
        label="Price Change"
        value={
          <span className={isPositive ? "text-emerald-600" : "text-red-600"}>
            {isPositive ? "+" : ""}
            {rupeeFormatter.format(price_change)}
          </span>
        }
        sub={
          <span className={isPositive ? "text-emerald-600" : "text-red-600"}>
            {isPositive ? "+" : ""}
            {price_change_pct.toFixed(2)}%
          </span>
        }
      />
      <StatCard
        label="Volume"
        value={compactFormatter.format(volume)}
        sub={`avg ${compactFormatter.format(avg_volume)}`}
      />
      <StatCard
        label="Vol Ratio"
        value={<span className={volRatioColour}>{volume_ratio.toFixed(2)}×</span>}
        sub={volume_ratio >= 2 ? "Above avg" : "Near avg"}
      />
      <StatCard
        label="RSI (14)"
        value={
          rsi !== null ? (
            <span className={rsiColour}>{rsi.toFixed(1)}</span>
          ) : (
            <span className="text-slate-400">—</span>
          )
        }
        sub={rsiLabel ?? "Insufficient data"}
      />
    </div>
  );
}

// ─── Props ────────────────────────────────────────────────────────────────────

interface ScannerDetailPaneProps {
  stock: StockAnalysis | null;
  listType: ScannerTab;
  open: boolean;
  onClose: () => void;
}

// ─── Component ────────────────────────────────────────────────────────────────

export function ScannerDetailPane({ stock, listType, open, onClose }: ScannerDetailPaneProps) {
  const { isAuthenticated } = useAuth();
  const { activeIndicators: activeIndicatorsReadonly } = useChartPreferences();
  const activeIndicators = activeIndicatorsReadonly as IndicatorId[];

  // ── Watchlist ──────────────────────────────────────────────────────────────
  const { items: watchlistItems, addToWatchlist, removeFromWatchlist, isAdding, isRemoving } =
    useWatchlist();

  const watchlistEntry = stock
    ? watchlistItems.find((i) => i.instrument_key === stock.symbol)
    : undefined;
  const inWatchlist = !!watchlistEntry;
  const watchlistItemId = watchlistEntry?.id ?? null;

  // ── Chart state ────────────────────────────────────────────────────────────
  const [currentTimeframe, setCurrentTimeframe] = useState<TimeframeOption>(SCANNER_DEFAULT_TIMEFRAME);
  const [historicalState, setHistoricalState] = useState<HistoricalState>(() => {
    const today = getISTTodayYMD();
    const { fromDate, toDate } = adjustDateRangeForTimeframe(today, SCANNER_DEFAULT_TIMEFRAME.defaultRangeDays);
    return {
      unit: SCANNER_DEFAULT_TIMEFRAME.unit,
      interval: SCANNER_DEFAULT_TIMEFRAME.interval,
      fromDate,
      toDate,
      presetId: SCANNER_DEFAULT_TIMEFRAME.id,
      isCustom: false,
    };
  });

  const [candles, setCandles] = useState<UpstoxCandlesResponse | null>(null);
  const [liveTick, setLiveTick] = useState<UpstoxLtpTick | null>(null);
  const [tickStreamStatus, setTickStreamStatus] = useState<TickStreamStatus>("idle");
  const [isLoadingMoreHistory, setIsLoadingMoreHistory] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(true);
  const [earliestLoadedDate, setEarliestLoadedDate] = useState<string | null>(null);
  const [isFetchingCandles, setIsFetchingCandles] = useState(false);
  const [candleError, setCandleError] = useState<string | null>(null);
  const [timeframeSwitchMessage, setTimeframeSwitchMessage] = useState<string | null>(null);

  const tickSocketRef = useRef<WebSocket | null>(null);
  const shouldAutoLoadRef = useRef(false);

  const { unit: candleUnit, interval: candleInterval, fromDate, toDate } = historicalState;
  const selectedRangeDays = diffDaysInclusive(fromDate, toDate);
  const candleSourceMode = resolveCandleSourceMode(selectedRangeDays, candleUnit, fromDate, toDate);

  // ── Candle fetch ───────────────────────────────────────────────────────────

  const fetchCandles = useCallback(async (params: FetchCandlesParams, instrumentKey: string) => {
    setIsFetchingCandles(true);
    setCandleError(null);

    try {
      let data: UpstoxCandlesResponse;

      if (params.mode === "intraday") {
        data = await upstoxAPI.getIntradayCandles(instrumentKey, {
          unit: params.unit,
          interval: params.interval,
        });
      } else if (params.mode === "hybrid") {
        const today = getISTTodayYMD();
        const yesterday = addDays(today, -1);
        const shouldFetchHistorical = params.fromDate <= yesterday;

        const [historical, intraday] = await Promise.all([
          shouldFetchHistorical
            ? upstoxAPI.getHistoricalCandles(instrumentKey, {
                unit: params.unit,
                interval: params.interval,
                to_date: yesterday,
                from_date: params.fromDate,
              })
            : Promise.resolve({ status: "success", data: { candles: [] } } as UpstoxCandlesResponse),
          upstoxAPI.getIntradayCandles(instrumentKey, { unit: params.unit, interval: params.interval }),
        ]);

        const merged = mergeCandles(
          (historical?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"],
          (intraday?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"],
        );
        data = { status: "success", data: { candles: merged } } as UpstoxCandlesResponse;
      } else {
        data = await upstoxAPI.getHistoricalCandles(instrumentKey, {
          unit: params.unit,
          interval: params.interval,
          to_date: params.toDate,
          from_date: params.fromDate,
        });

        // Auto-fallback: if historical returns empty for today's range, try intraday
        const historicalCandles = data?.data?.candles ?? [];
        if (
          historicalCandles.length === 0 &&
          params.fromDate <= getISTTodayYMD() &&
          params.toDate >= getISTTodayYMD()
        ) {
          try {
            const intraday = await upstoxAPI.getIntradayCandles(instrumentKey, {
              unit: params.unit,
              interval: params.interval,
            });
            if ((intraday?.data?.candles?.length ?? 0) > 0) data = intraday;
          } catch {
            // Keep original empty result
          }
        }
      }

      const deduped = data?.data?.candles ? mergeCandles(data.data.candles, []) : [];
      setCandles({ ...data, data: { candles: deduped } });

      if (deduped.length > 0) {
        const firstTs = candleTimestampToDate(deduped[0][0]);
        setEarliestLoadedDate(firstTs);
        setHasMoreHistory(new Date(firstTs) > new Date(getMinAvailableDateForUnit(params.unit)));
      } else {
        setEarliestLoadedDate(params.toDate);
        setHasMoreHistory(true);
        shouldAutoLoadRef.current = true;
      }
    } catch (err) {
      console.error("[ScannerDetailPane] Candle fetch failed:", err);
      setCandleError("Failed to load chart data.");
    } finally {
      setIsFetchingCandles(false);
    }
  }, []);

  // ── Instrument-change effect ───────────────────────────────────────────────

  useEffect(() => {
    if (!stock) return;

    // Reset all chart state when the selected instrument changes
    shouldAutoLoadRef.current = false;
    setCandles(null);
    setEarliestLoadedDate(null);
    setHasMoreHistory(true);
    setLiveTick(null);
    setTimeframeSwitchMessage(null);
    setCandleError(null);

    // Reset to scanner default timeframe
    const today = getISTTodayYMD();
    const { fromDate: fd, toDate: td } = adjustDateRangeForTimeframe(today, SCANNER_DEFAULT_TIMEFRAME.defaultRangeDays);
    const newHistState: HistoricalState = {
      unit: SCANNER_DEFAULT_TIMEFRAME.unit,
      interval: SCANNER_DEFAULT_TIMEFRAME.interval,
      fromDate: fd,
      toDate: td,
      presetId: SCANNER_DEFAULT_TIMEFRAME.id,
      isCustom: false,
    };
    setCurrentTimeframe(SCANNER_DEFAULT_TIMEFRAME);
    setHistoricalState(newHistState);

    fetchCandles(
      {
        unit: newHistState.unit,
        interval: newHistState.interval,
        fromDate: newHistState.fromDate,
        toDate: newHistState.toDate,
        mode: resolveCandleSourceMode(
          diffDaysInclusive(newHistState.fromDate, newHistState.toDate),
          newHistState.unit,
          newHistState.fromDate,
          newHistState.toDate,
        ),
      },
      stock.symbol,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stock?.symbol]);

  // ── Auto-load (empty range → step backwards) ───────────────────────────────

  useEffect(() => {
    if (
      !stock ||
      !shouldAutoLoadRef.current ||
      candles === null ||
      candles.data.candles.length !== 0 ||
      earliestLoadedDate === null ||
      !hasMoreHistory ||
      isLoadingMoreHistory
    ) return;

    shouldAutoLoadRef.current = false;
    handleLoadMoreHistory();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candles, earliestLoadedDate, hasMoreHistory, isLoadingMoreHistory]);

  // ── Load more history ──────────────────────────────────────────────────────

  const handleLoadMoreHistory = useCallback(async () => {
    if (!stock || !earliestLoadedDate || isLoadingMoreHistory || !hasMoreHistory) return;

    setIsLoadingMoreHistory(true);
    setTimeframeSwitchMessage(null);

    try {
      const daysToLoad = candleUnit === "minutes" && candleInterval <= 15 ? 2 : 5;
      const newFromDate = addDays(earliestLoadedDate, -daysToLoad);
      const newToDate = addDays(earliestLoadedDate, -1);
      const minDate = getMinAvailableDateForUnit(candleUnit);

      if (newFromDate < minDate && candleUnit === "minutes") {
        setTimeframeSwitchMessage("Reached the oldest available data for this timeframe.");
        setHasMoreHistory(false);
        return;
      }

      const result = await upstoxAPI.getHistoricalCandles(stock.symbol, {
        unit: candleUnit,
        interval: candleInterval,
        to_date: newToDate,
        from_date: newFromDate,
      });

      if ((result?.data?.candles?.length ?? 0) > 0) {
        setCandles((prev) => {
          if (!prev) return null;
          const merged = mergeCandles(result.data.candles, prev.data.candles ?? []);
          return { ...prev, data: { candles: merged } };
        });
        const firstTs = candleTimestampToDate(result.data.candles[0][0]);
        setEarliestLoadedDate(firstTs);
        setHasMoreHistory(new Date(firstTs) > new Date(getMinAvailableDateForUnit(candleUnit)));
      } else {
        setEarliestLoadedDate(newFromDate);
      }
    } catch (err) {
      console.error("[ScannerDetailPane] Failed to load historical data:", err);
      setHasMoreHistory(false);
    } finally {
      setIsLoadingMoreHistory(false);
    }
  }, [stock, earliestLoadedDate, isLoadingMoreHistory, hasMoreHistory, candleUnit, candleInterval]);

  const handleLoadMoreHistoryRef = useRef(handleLoadMoreHistory);
  useEffect(() => { handleLoadMoreHistoryRef.current = handleLoadMoreHistory; }, [handleLoadMoreHistory]);
  const stableHandleLoadMoreHistory = useCallback(async () => handleLoadMoreHistoryRef.current(), []);

  // ── Timeframe change ───────────────────────────────────────────────────────

  const handleTimeframeChange = useCallback(
    (newTf: TimeframeOption) => {
      if (!stock) return;

      const currentCenter = addDays(fromDate, Math.floor(selectedRangeDays / 2));
      const { fromDate: newFrom, toDate: newTo } = adjustDateRangeForTimeframe(
        currentCenter,
        newTf.defaultRangeDays,
      );
      const newRangeDays = diffDaysInclusive(newFrom, newTo);
      const newMode = resolveCandleSourceMode(newRangeDays, newTf.unit, newFrom, newTo);

      setCurrentTimeframe(newTf);
      setHistoricalState({
        unit: newTf.unit,
        interval: newTf.interval,
        fromDate: newFrom,
        toDate: newTo,
        presetId: newTf.id,
        isCustom: false,
      });
      setCandles(null);
      setEarliestLoadedDate(null);
      setHasMoreHistory(true);
      setTimeframeSwitchMessage(null);
      setCandleError(null);
      shouldAutoLoadRef.current = false;

      fetchCandles(
        { unit: newTf.unit, interval: newTf.interval, fromDate: newFrom, toDate: newTo, mode: newMode },
        stock.symbol,
      );
    },
    [stock, fromDate, selectedRangeDays, fetchCandles],
  );

  // ── Tick stream WebSocket ──────────────────────────────────────────────────
  //
  // Always connect regardless of timeframe so the header price badge receives
  // live ticks even when viewing a daily chart.

  useEffect(() => {
    if (!isAuthenticated || !stock) {
      setTickStreamStatus("idle");
      return;
    }

    let reconnectAttempts = 0;
    let reconnectTimeoutId: ReturnType<typeof setTimeout> | null = null;
    let isCancelled = false;

    const connect = () => {
      if (isCancelled) return;
      setTickStreamStatus("connecting");

      const socket = new WebSocket(buildTickStreamUrl(stock.symbol));
      tickSocketRef.current = socket;

      socket.onopen = () => {
        if (isCancelled) return;
        reconnectAttempts = 0;
        setTickStreamStatus("live");
      };

      socket.onmessage = (event) => {
        if (isCancelled) return;
        try {
          const payload = JSON.parse(event.data) as UpstoxTickStreamMessage;
          if (payload.type === "tick") setLiveTick(payload.data);
        } catch { /* malformed message */ }
      };

      socket.onerror = () => {
        if (!isCancelled) setTickStreamStatus("error");
      };

      socket.onclose = (event) => {
        if (isCancelled) return;
        tickSocketRef.current = null;
        const isIntentional = event.code === 1000 || event.code === 1001;
        if (isIntentional) { setTickStreamStatus("idle"); return; }

        if (reconnectAttempts < TICK_STREAM_MAX_RECONNECTS) {
          setTickStreamStatus("reconnecting");
          const delay = getReconnectDelay(reconnectAttempts++);
          reconnectTimeoutId = setTimeout(connect, delay);
        } else {
          console.error("[ScannerDetailPane] Tick stream max reconnects reached:", stock.symbol);
          setTickStreamStatus("error");
        }
      };
    };

    connect();

    return () => {
      isCancelled = true;
      if (reconnectTimeoutId !== null) clearTimeout(reconnectTimeoutId);
      if (tickSocketRef.current) {
        tickSocketRef.current.close(1000, "Component unmounted");
        tickSocketRef.current = null;
      }
    };
  }, [stock?.symbol, isAuthenticated]);

  // ── Watchlist toggle ───────────────────────────────────────────────────────

  const handleToggleWatchlist = useCallback(async () => {
    if (!isAuthenticated || !stock) return;
    try {
      if (inWatchlist && watchlistItemId !== null) {
        await removeFromWatchlist(watchlistItemId);
      } else {
        await addToWatchlist({
          instrument_key: stock.symbol,
          trading_symbol: stock.trading_symbol ?? stock.symbol,
          name: stock.name ?? undefined,
          exchange: exchangeFromKey(stock.symbol),
        });
      }
    } catch (err) {
      if (!isNetworkError(err)) console.error("[ScannerDetailPane] Watchlist toggle failed:", err);
    }
  }, [isAuthenticated, stock, inWatchlist, watchlistItemId, addToWatchlist, removeFromWatchlist]);

  // ── ESC key ────────────────────────────────────────────────────────────────

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [open, onClose]);

  // ── Body scroll lock ───────────────────────────────────────────────────────

  useEffect(() => {
    if (open) document.body.style.overflow = "hidden";
    else document.body.style.overflow = "";
    return () => { document.body.style.overflow = ""; };
  }, [open]);

  // ── Render ─────────────────────────────────────────────────────────────────

  return (
    <>
      {/* Backdrop — subtle so the scanner list remains readable behind */}
      <div
        aria-hidden="true"
        className={`fixed inset-0 z-40 bg-slate-950/25 backdrop-blur-[2px] transition-opacity duration-300 ${
          open ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none"
        }`}
        onClick={onClose}
      />

      {/* Panel */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={stock ? `${stock.name ?? stock.trading_symbol} details` : "Stock details"}
        className={`fixed right-0 top-0 z-50 flex h-full w-full flex-col bg-white shadow-2xl
          transition-transform duration-300 ease-in-out w-full md:w-[min(55vw,960px)]
          ${open ? "translate-x-0" : "translate-x-full"}`}
      >
        {stock ? <PaneContent
          stock={stock}
          listType={listType}
          currentTimeframe={currentTimeframe}
          candles={candles}
          liveTick={liveTick}
          tickStreamStatus={tickStreamStatus}
          candleUnit={candleUnit}
          candleInterval={candleInterval}
          isFetchingCandles={isFetchingCandles}
          candleError={candleError}
          isLoadingMoreHistory={isLoadingMoreHistory}
          hasMoreHistory={hasMoreHistory}
          timeframeSwitchMessage={timeframeSwitchMessage}
          fromDate={fromDate}
          toDate={toDate}
          activeIndicators={activeIndicators}
          inWatchlist={inWatchlist}
          isAdding={isAdding}
          isRemoving={isRemoving}
          isAuthenticated={isAuthenticated}
          onClose={onClose}
          onTimeframeChange={handleTimeframeChange}
          onToggleWatchlist={handleToggleWatchlist}
          onLoadMoreHistory={stableHandleLoadMoreHistory}
        /> : null}
      </div>
    </>
  );
}

// ─── PaneContent (separated to keep effect deps clean) ────────────────────────

interface PaneContentProps {
  stock: StockAnalysis;
  listType: ScannerTab;
  currentTimeframe: TimeframeOption;
  candles: UpstoxCandlesResponse | null;
  liveTick: UpstoxLtpTick | null;
  tickStreamStatus: TickStreamStatus;
  candleUnit: CandleUnit;
  candleInterval: number;
  isFetchingCandles: boolean;
  candleError: string | null;
  isLoadingMoreHistory: boolean;
  hasMoreHistory: boolean;
  timeframeSwitchMessage: string | null;
  fromDate: string;
  toDate: string;
  activeIndicators: IndicatorId[];
  inWatchlist: boolean;
  isAdding: boolean;
  isRemoving: boolean;
  isAuthenticated: boolean;
  onClose: () => void;
  onTimeframeChange: (tf: TimeframeOption) => void;
  onToggleWatchlist: () => void;
  onLoadMoreHistory: () => Promise<void>;
}

function PaneContent({
  stock,
  listType,
  currentTimeframe,
  candles,
  liveTick,
  tickStreamStatus,
  candleUnit,
  candleInterval,
  isFetchingCandles,
  candleError,
  isLoadingMoreHistory,
  hasMoreHistory,
  timeframeSwitchMessage,
  fromDate,
  toDate,
  activeIndicators,
  inWatchlist,
  isAdding,
  isRemoving,
  isAuthenticated,
  onClose,
  onTimeframeChange,
  onToggleWatchlist,
  onLoadMoreHistory,
}: PaneContentProps) {
  const exchange = exchangeFromKey(stock.symbol);

  // Apply live-merge condition here so the header badge always sees raw ticks.
  const liveMergeEnabled = includesTodayOrYesterday(fromDate, toDate);

  // Live price: WS tick wins; fall back to scanner snapshot when WS hasn't fired.
  const displayPrice = liveTick?.last_price ?? stock.current_price;

  const isLive =
    tickStreamStatus === "live" && getISTMarketSession() === "open";
  const isConnecting =
    (tickStreamStatus === "connecting" || tickStreamStatus === "reconnecting") &&
    getISTMarketSession() === "open";

  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <div className="flex shrink-0 items-start justify-between gap-4 border-b border-slate-100 px-8 py-6">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h2 className="truncate text-lg font-bold leading-tight text-slate-900">
              {stock.name ?? stock.trading_symbol ?? stock.symbol}
            </h2>
            {stock.sector && (
              <Badge variant="secondary" className="shrink-0 text-xs">
                {stock.sector}
              </Badge>
            )}
            <LivePriceBadge
              price={displayPrice}
              prevClose={stock.previous_close}
              isLive={isLive}
              isConnecting={isConnecting}
            />
          </div>
          <div className="mt-0.5 flex items-center gap-2">
            <span className="font-mono text-sm font-medium text-slate-500">
              {stock.trading_symbol ?? stock.symbol}
            </span>
            <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-500">
              {exchange}
            </span>
          </div>
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {isAuthenticated && (
            <Button
              variant={inWatchlist ? "outline" : "default"}
              size="sm"
              onClick={onToggleWatchlist}
              disabled={isAdding || isRemoving}
              className="gap-1.5"
            >
              {isAdding || isRemoving ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : inWatchlist ? (
                <Check className="h-3.5 w-3.5" />
              ) : (
                <Plus className="h-3.5 w-3.5" />
              )}
              {inWatchlist ? "Watchlisted" : "Watchlist"}
            </Button>
          )}
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close detail pane">
            <X className="h-4 w-4" />
          </Button>
        </div>
      </div>

      {/* ── Scrollable body ─────────────────────────────────────────────────── */}
      <div className="flex flex-1 flex-col gap-7 overflow-y-auto px-8 py-6">
        {/* Scanner context */}
        <ContextBanner stock={stock} listType={listType} />

        {/* Stats */}
        <StatsGrid stock={stock} listType={listType} />

        {/* Warnings */}
        {stock.warnings.length > 0 && (
          <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
            <div className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-amber-700">
              <AlertTriangle className="h-3.5 w-3.5" />
              Data Notice
            </div>
            <ul className="mt-1.5 space-y-0.5">
              {stock.warnings.map((w, i) => (
                <li key={i} className="text-xs text-amber-700">
                  {w}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Chart */}
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-1">
            <TimeframeSelector value={currentTimeframe} onChange={onTimeframeChange} />
            <div className="mx-1.5 h-4 w-px bg-slate-200" aria-hidden="true" />
            <IndicatorSelector />
          </div>

          <div className="flex min-h-[520px] flex-col rounded-xl border border-slate-200 bg-slate-950/95 p-5">
            {isFetchingCandles && (
              <div className="flex items-center gap-2 text-sm text-slate-300">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading chart…
              </div>
            )}

            {candleError && !isFetchingCandles && (
              <div className="rounded-lg border border-red-900/40 bg-red-950/30 px-4 py-3 text-sm text-red-400">
                {candleError}
              </div>
            )}

            {candles?.data?.candles?.length ? (
              <div className="flex flex-1 flex-col">
                {timeframeSwitchMessage && (
                  <div className="mb-2 rounded-lg border border-blue-800/40 bg-blue-950/30 px-3 py-2 text-xs text-blue-300">
                    {timeframeSwitchMessage}
                  </div>
                )}
                <div className="flex-1" style={{ minHeight: 460 }}>
                  <CandlestickChart
                    candles={candles.data.candles}
                    liveTick={liveMergeEnabled ? liveTick : null}
                    candleUnit={candleUnit}
                    candleInterval={candleInterval}
                    canLoadMoreHistory={hasMoreHistory}
                    isLoadingMoreHistory={isLoadingMoreHistory}
                    onLoadMoreHistory={onLoadMoreHistory}
                    activeIndicators={activeIndicators}
                  />
                </div>
              </div>
            ) : !isFetchingCandles && candles ? (
              <div className="rounded-lg border border-yellow-800/40 bg-yellow-950/20 px-4 py-3">
                <p className="text-sm font-medium text-yellow-200">
                  {isLoadingMoreHistory ? "Searching for historical data…" : "No chart data available"}
                </p>
                {!isLoadingMoreHistory && (
                  <p className="mt-1 text-xs text-yellow-500">
                    {getNoDataMessage(stock.trading_symbol ?? stock.symbol, fromDate, toDate)}
                  </p>
                )}
                {isLoadingMoreHistory && (
                  <div className="mt-2 flex items-center gap-2 text-xs text-yellow-400">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    Searching…
                  </div>
                )}
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
