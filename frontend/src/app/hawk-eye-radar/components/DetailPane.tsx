"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { X, Loader2, Plus, Check } from "lucide-react";
import { WS_BASE_URL, upstoxAPI, isNetworkError } from "@/lib/api";
import { mergeCandles } from "@/lib/candle-transforms";
import {
  getISTTodayYMD,
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
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { CandlestickChart } from "@/components/charts/CandlestickChart";
import { TimeframeSelector } from "@/components/charts/TimeframeSelector";
import { IndicatorSelector } from "@/components/charts/IndicatorSelector";
import { AnalysisCardsSection } from "@/components/AnalysisCardsSection";
import { useWatchlist } from "@/hooks/useWatchlist";
import { useAuth } from "@/contexts/AuthContext";
import { useChartPreferences, type TimeframeOption } from "@/contexts/ChartPreferencesContext";
import type {
  UpstoxCandlesResponse,
  UpstoxInstrument,
  UpstoxLtpTick,
  UpstoxTickStreamMessage,
} from "@/types/upstox";

// ─── Constants ───────────────────────────────────────────────────────────────

const TICK_STREAM_INTERVAL_MS = 500;
const TICK_STREAM_MAX_RECONNECTS = 10;
const TICK_STREAM_BASE_DELAY_MS = 1_000;
const TICK_STREAM_MAX_DELAY_MS = 30_000;

// ─── Types ────────────────────────────────────────────────────────────────────

/**
 * Explicit parameters for every candle fetch.
 * The mutation accepts these instead of reading from component state via closure,
 * which would produce stale values when called synchronously after a setState.
 */
type FetchCandlesParams = {
  unit: CandleUnit;
  interval: number;
  fromDate: string;
  toDate: string;
  mode: CandleSourceMode;
};

type TickStreamStatus = "idle" | "connecting" | "live" | "reconnecting" | "error";

// ─── Helpers ──────────────────────────────────────────────────────────────────

function buildTickStreamUrl(instrumentKey: string): string {
  const wsBase = WS_BASE_URL.replace(/\/$/, "");
  const hasApiV1Prefix = /\/api\/v1$/i.test(wsBase);
  const path = hasApiV1Prefix ? "/upstox/ticks/ws" : "/api/v1/upstox/ticks/ws";
  const url = new URL(`${wsBase}${path}`);
  url.searchParams.set("instrument_key", instrumentKey);
  url.searchParams.set("interval_ms", String(TICK_STREAM_INTERVAL_MS));
  return url.toString();
}

function getReconnectDelay(attempt: number): number {
  return Math.min(TICK_STREAM_BASE_DELAY_MS * Math.pow(2, attempt), TICK_STREAM_MAX_DELAY_MS);
}

/** Extract YYYY-MM-DD from a candle timestamp (string ISO or Unix seconds). */
function candleTimestampToDate(ts: string | number): string {
  if (typeof ts === "string") return ts.split("T")[0];
  return new Date(ts * 1000).toISOString().split("T")[0];
}

// ─── Props ────────────────────────────────────────────────────────────────────

interface DetailPaneProps {
  instrument: UpstoxInstrument;
  onClose: () => void;
  showAnalysis?: boolean;
}

// ─── Component ────────────────────────────────────────────────────────────────

export function DetailPane({ instrument, onClose, showAnalysis = true }: DetailPaneProps) {
  const { isAuthenticated } = useAuth();
  const { defaultTimeframe, activeIndicators } = useChartPreferences();

  // Use full watchlist items so we can derive watchlist status from the
  // already-cached data instead of making a separate checkInWatchlist API call.
  const { items: watchlistItems, addToWatchlist, removeFromWatchlist, isAdding, isRemoving } =
    useWatchlist();

  // ── Derived watchlist state — zero extra API calls ──────────────────────────
  const watchlistEntry = watchlistItems.find(
    (i) => i.instrument_key === instrument.instrument_key,
  );
  const inWatchlist = !!watchlistEntry;
  const watchlistItemId = watchlistEntry?.id ?? null;

  // ── Chart / data state ──────────────────────────────────────────────────────
  const [currentTimeframe, setCurrentTimeframe] = useState<TimeframeOption>(defaultTimeframe);
  const [historicalState, setHistoricalState] = useState<HistoricalState>(() => {
    const today = getISTTodayYMD();
    const { fromDate, toDate } = adjustDateRangeForTimeframe(today, defaultTimeframe.defaultRangeDays);
    return {
      unit: defaultTimeframe.unit,
      interval: defaultTimeframe.interval,
      fromDate,
      toDate,
      presetId: "1D",
      isCustom: false,
    };
  });

  const [candles, setCandles] = useState<UpstoxCandlesResponse | null>(null);
  const [liveTick, setLiveTick] = useState<UpstoxLtpTick | null>(null);
  const [tickStreamStatus, setTickStreamStatus] = useState<TickStreamStatus>("idle");

  // Infinite-scroll state
  const [earliestLoadedDate, setEarliestLoadedDate] = useState<string | null>(null);
  const [isLoadingMoreHistory, setIsLoadingMoreHistory] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(true);
  const [timeframeSwitchMessage, setTimeframeSwitchMessage] = useState<string | null>(null);

  const tickSocketRef = useRef<WebSocket | null>(null);

  // Signals that the initial candle fetch returned empty data and we should
  // attempt an automatic load-more once earliestLoadedDate is set.
  const shouldAutoLoadRef = useRef(false);

  // ── Derived params from historicalState ────────────────────────────────────
  const { unit: candleUnit, interval: candleInterval, fromDate, toDate } = historicalState;
  const selectedRangeDays = diffDaysInclusive(fromDate, toDate);
  const candleSourceMode = resolveCandleSourceMode(selectedRangeDays, candleUnit, fromDate, toDate);
  const liveMergeEnabled = includesTodayOrYesterday(fromDate, toDate);

  // ── Candle mutation ─────────────────────────────────────────────────────────
  //
  // The mutationFn accepts explicit FetchCandlesParams instead of closing over
  // component state. This prevents the stale-closure bug where
  // setHistoricalState + mutate() in the same synchronous block would fetch
  // with the previous timeframe's params.

  const candlesMutation = useMutation<UpstoxCandlesResponse, Error, FetchCandlesParams>({
    mutationFn: async ({ unit, interval, fromDate, toDate, mode }) => {
      if (mode === "intraday") {
        return upstoxAPI.getIntradayCandles(instrument.instrument_key, { unit, interval });
      }

      if (mode === "hybrid") {
        const today = getISTTodayYMD();
        const yesterday = addDays(today, -1);
        const shouldFetchHistorical = fromDate <= yesterday;

        const [historical, intraday] = await Promise.all([
          shouldFetchHistorical
            ? upstoxAPI.getHistoricalCandles(instrument.instrument_key, {
                unit,
                interval,
                to_date: yesterday,
                from_date: fromDate,
              })
            : Promise.resolve({ status: "success", data: { candles: [] } } as UpstoxCandlesResponse),
          upstoxAPI.getIntradayCandles(instrument.instrument_key, { unit, interval }),
        ]);

        const merged = mergeCandles(
          (historical?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"],
          (intraday?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"],
        );
        return { status: "success", data: { candles: merged } } as UpstoxCandlesResponse;
      }

      // Historical mode — with intelligent intraday fallback for edge cases
      // (pre-market, data ingestion delays, market holidays near today's date).
      const historical = await upstoxAPI.getHistoricalCandles(instrument.instrument_key, {
        unit,
        interval,
        to_date: toDate,
        from_date: fromDate,
      });

      const historicalCandles = historical?.data?.candles ?? [];

      if (
        historicalCandles.length === 0 &&
        fromDate <= getISTTodayYMD() &&
        toDate >= getISTTodayYMD()
      ) {
        try {
          const intraday = await upstoxAPI.getIntradayCandles(instrument.instrument_key, {
            unit,
            interval,
          });
          if ((intraday?.data?.candles?.length ?? 0) > 0) {
            return intraday;
          }
        } catch {
          // Fallback failed — return original empty historical response
        }
      }

      return historical;
    },

    onSuccess: (data, variables) => {
      // Deduplicate as a safety measure in case the backend returns overlapping candles.
      const deduped = data?.data?.candles
        ? mergeCandles(data.data.candles, [])
        : [];

      setCandles({ ...data, data: { candles: deduped } });

      if (deduped.length > 0) {
        const firstTs = candleTimestampToDate(deduped[0][0]);
        setEarliestLoadedDate(firstTs);
        setHasMoreHistory(
          new Date(firstTs) > new Date(getMinAvailableDateForUnit(variables.unit)),
        );
      } else {
        // No candles for the requested range — set earliestLoadedDate to the
        // requested toDate so handleLoadMoreHistory can step backwards from there.
        // shouldAutoLoadRef signals the auto-load effect to trigger once.
        setEarliestLoadedDate(variables.toDate);
        setHasMoreHistory(true);
        shouldAutoLoadRef.current = true;
      }
    },

    onError: (error) => {
      console.error("[DetailPane] Candle fetch failed:", error);
    },

    retry: (failureCount, error: unknown) => {
      const status = (error as { response?: { status?: number } })?.response?.status;
      if (status === 401) return false;
      return failureCount < 3;
    },
  });

  // ── Fetch candles helper ────────────────────────────────────────────────────

  const fetchCandles = useCallback(
    (params: FetchCandlesParams) => {
      candlesMutation.mutate(params);
    },
    // candlesMutation.mutate is stable; instrument.instrument_key is in params.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  // ── Instrument-change effect ────────────────────────────────────────────────

  useEffect(() => {
    // Reset all data-dependent state when the displayed instrument changes.
    shouldAutoLoadRef.current = false;
    setCandles(null);
    setEarliestLoadedDate(null);
    setHasMoreHistory(true);
    setLiveTick(null);
    setTimeframeSwitchMessage(null);

    fetchCandles({
      unit: candleUnit,
      interval: candleInterval,
      fromDate,
      toDate,
      mode: candleSourceMode,
    });
    // We intentionally only re-run on instrument change; timeframe changes use
    // handleTimeframeChange which passes fresh computed params.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [instrument.instrument_key]);

  // ── Load more historical data ───────────────────────────────────────────────

  const handleLoadMoreHistory = useCallback(async () => {
    if (!earliestLoadedDate || isLoadingMoreHistory || !hasMoreHistory) return;

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

      const result = await upstoxAPI.getHistoricalCandles(instrument.instrument_key, {
        unit: candleUnit,
        interval: candleInterval,
        to_date: newToDate,
        from_date: newFromDate,
      });

      if ((result?.data?.candles?.length ?? 0) > 0) {
        // Merge using the functional updater — safe from stale closures even
        // when called from an async context after a re-render.
        setCandles((prev) => {
          if (!prev) return null;
          const merged = mergeCandles(result.data.candles, prev.data.candles ?? []);
          return { ...prev, data: { candles: merged } };
        });

        const firstTs = candleTimestampToDate(result.data.candles[0][0]);
        setEarliestLoadedDate(firstTs);
        setHasMoreHistory(new Date(firstTs) > new Date(minDate));
      } else {
        // Empty (weekend / holiday) — step the cursor back to keep searching.
        setEarliestLoadedDate(newFromDate);
      }
    } catch (error) {
      console.error("[DetailPane] Failed to load historical data:", error);
      setHasMoreHistory(false);
    } finally {
      setIsLoadingMoreHistory(false);
    }
  }, [
    earliestLoadedDate,
    isLoadingMoreHistory,
    hasMoreHistory,
    candleUnit,
    candleInterval,
    instrument.instrument_key,
  ]);

  // Stable ref wrapper so CandlestickChart's subscription effect does not
  // re-register on every re-render caused by isLoadingMoreHistory toggling.
  const handleLoadMoreHistoryRef = useRef(handleLoadMoreHistory);
  useEffect(() => {
    handleLoadMoreHistoryRef.current = handleLoadMoreHistory;
  }, [handleLoadMoreHistory]);

  const stableHandleLoadMoreHistory = useCallback(
    async () => handleLoadMoreHistoryRef.current(),
    [],
  );

  // ── Auto-load effect ────────────────────────────────────────────────────────
  //
  // Replaces the previous setTimeout(handleLoadMoreHistory, 100) in onSuccess.
  // Triggers once, safely, after React has committed the state updates from
  // onSuccess (earliestLoadedDate set, candles set to empty).

  useEffect(() => {
    if (
      shouldAutoLoadRef.current &&
      candles !== null &&
      candles.data.candles.length === 0 &&
      earliestLoadedDate !== null &&
      hasMoreHistory &&
      !isLoadingMoreHistory
    ) {
      shouldAutoLoadRef.current = false;
      handleLoadMoreHistory();
    }
  }, [candles, earliestLoadedDate, hasMoreHistory, isLoadingMoreHistory, handleLoadMoreHistory]);

  // ── Timeframe change handler ────────────────────────────────────────────────

  const handleTimeframeChange = useCallback(
    (newTimeframe: TimeframeOption) => {
      // Compute new params synchronously so we can pass them directly to
      // mutate(), bypassing the stale-closure problem entirely.
      const currentCenter = addDays(fromDate, Math.floor(selectedRangeDays / 2));
      const { fromDate: newFromDate, toDate: newToDate } = adjustDateRangeForTimeframe(
        currentCenter,
        newTimeframe.defaultRangeDays,
      );
      const newRangeDays = diffDaysInclusive(newFromDate, newToDate);
      const newMode = resolveCandleSourceMode(
        newRangeDays,
        newTimeframe.unit,
        newFromDate,
        newToDate,
      );

      setCurrentTimeframe(newTimeframe);
      setHistoricalState({
        unit: newTimeframe.unit,
        interval: newTimeframe.interval,
        fromDate: newFromDate,
        toDate: newToDate,
        presetId: "1D",
        isCustom: false,
      });
      setCandles(null);
      setEarliestLoadedDate(null);
      setHasMoreHistory(true);
      setTimeframeSwitchMessage(null);
      shouldAutoLoadRef.current = false;

      // Pass the freshly computed params — no stale closure risk.
      fetchCandles({
        unit: newTimeframe.unit,
        interval: newTimeframe.interval,
        fromDate: newFromDate,
        toDate: newToDate,
        mode: newMode,
      });
    },
    [fromDate, selectedRangeDays, fetchCandles],
  );

  // ── Watchlist toggle ────────────────────────────────────────────────────────

  const handleToggleWatchlist = useCallback(async () => {
    if (!isAuthenticated) return;
    try {
      if (inWatchlist && watchlistItemId !== null) {
        await removeFromWatchlist(watchlistItemId);
      } else {
        await addToWatchlist({
          instrument_key: instrument.instrument_key,
          trading_symbol: instrument.trading_symbol,
          name: instrument.name,
          exchange: instrument.exchange,
        });
      }
    } catch (error) {
      if (!isNetworkError(error)) console.error("[DetailPane] Failed to toggle watchlist:", error);
    }
  }, [
    isAuthenticated,
    inWatchlist,
    watchlistItemId,
    removeFromWatchlist,
    addToWatchlist,
    instrument,
  ]);

  // ── Tick stream WebSocket ───────────────────────────────────────────────────
  //
  // Production-grade: exponential-backoff reconnection on unexpected close,
  // intentional close on unmount or auth loss (code 1000 → no reconnect).

  useEffect(() => {
    if (!isAuthenticated) {
      setTickStreamStatus("idle");
      return;
    }

    let reconnectAttempts = 0;
    let reconnectTimeoutId: ReturnType<typeof setTimeout> | null = null;
    let isCancelled = false;

    const connect = () => {
      if (isCancelled) return;
      setTickStreamStatus("connecting");

      const socket = new WebSocket(buildTickStreamUrl(instrument.instrument_key));
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
          if (payload.type === "tick") {
            setLiveTick(payload.data);
          }
        } catch {
          // Malformed message — log but don't disrupt the stream.
        }
      };

      socket.onerror = () => {
        if (isCancelled) return;
        // onclose fires after onerror; let it handle reconnection.
        setTickStreamStatus("error");
      };

      socket.onclose = (event) => {
        if (isCancelled) return;
        tickSocketRef.current = null;

        const isIntentional = event.code === 1000 || event.code === 1001;
        if (isIntentional) {
          setTickStreamStatus("idle");
          return;
        }

        if (reconnectAttempts < TICK_STREAM_MAX_RECONNECTS) {
          setTickStreamStatus("reconnecting");
          const delay = getReconnectDelay(reconnectAttempts);
          reconnectAttempts++;
          reconnectTimeoutId = setTimeout(connect, delay);
        } else {
          console.error("[DetailPane] Tick stream max reconnects reached for:", instrument.instrument_key);
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
  }, [instrument.instrument_key, isAuthenticated]);

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="fixed inset-0 z-50 bg-slate-950/40 backdrop-blur-sm">
      <div className="absolute inset-0 overflow-y-auto">
        <div className="flex h-full flex-col p-6">
          <div className="flex flex-1 flex-col w-full">
            <Card className="flex flex-1 flex-col rounded-2xl border-slate-200 bg-white shadow-2xl">
              {/* ── Header ─────────────────────────────────────────────────── */}
              <CardHeader className="flex flex-row items-center justify-between shrink-0">
                <div>
                  <CardTitle className="text-2xl">{instrument.name || "—"}</CardTitle>
                  <p className="text-sm text-slate-500">{instrument.trading_symbol}</p>
                </div>
                <div className="flex items-center gap-2">
                  {isAuthenticated && !showAnalysis && (
                    <Button
                      variant={inWatchlist ? "outline" : "default"}
                      size="sm"
                      onClick={handleToggleWatchlist}
                      disabled={isAdding || isRemoving}
                      className="gap-2"
                    >
                      {isAdding || isRemoving ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : inWatchlist ? (
                        <Check className="h-4 w-4" />
                      ) : (
                        <Plus className="h-4 w-4" />
                      )}
                      {inWatchlist ? "In Watchlist" : "Add to Watchlist"}
                    </Button>
                  )}
                  <Button variant="ghost" size="icon" onClick={onClose}>
                    <X className="h-5 w-5" />
                  </Button>
                </div>
              </CardHeader>

              {/* ── Content ────────────────────────────────────────────────── */}
              <CardContent className="flex flex-1 flex-col min-h-0 gap-4 px-6 pb-6 overflow-hidden">
                {/* Chart toolbar */}
                <div className="flex items-center gap-1">
                  <TimeframeSelector
                    value={currentTimeframe}
                    onChange={handleTimeframeChange}
                  />
                  <div className="mx-1.5 h-4 w-px bg-slate-200" aria-hidden="true" />
                  <IndicatorSelector />
                </div>

                {/* Chart section — flex-1 so it grows to fill remaining space */}
                <div className="flex flex-1 flex-col min-h-0 rounded-2xl border border-slate-200 bg-slate-950/95 p-4">
                  {candlesMutation.isPending && (
                    <div className="flex items-center gap-2 text-sm text-slate-300">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Loading candles…
                    </div>
                  )}

                  {candlesMutation.isError && (
                    <div className="rounded-lg border border-red-900/40 bg-red-950/30 px-4 py-3 text-sm text-red-400">
                      Failed to load candle data. Please try again.
                    </div>
                  )}

                  {candles?.data?.candles?.length ? (
                    <div className="flex flex-1 flex-col min-h-0">
                      {timeframeSwitchMessage && (
                        <div className="mb-2 rounded-lg border border-blue-800/40 bg-blue-950/30 px-3 py-2 text-sm text-blue-300">
                          {timeframeSwitchMessage}
                        </div>
                      )}
                      {/* Chart fills all remaining vertical space */}
                      <div className="flex-1 min-h-[700px]">
                        <CandlestickChart
                          candles={candles.data.candles}
                          liveTick={liveMergeEnabled ? liveTick : null}
                          candleUnit={candleUnit}
                          candleInterval={candleInterval}
                          canLoadMoreHistory={hasMoreHistory}
                          isLoadingMoreHistory={isLoadingMoreHistory}
                          onLoadMoreHistory={stableHandleLoadMoreHistory}
                          activeIndicators={activeIndicators}
                        />
                      </div>
                    </div>
                  ) : !candlesMutation.isPending && candles ? (
                    <div className="rounded-lg border border-yellow-800/40 bg-yellow-950/20 px-4 py-3">
                      <p className="text-sm font-medium text-yellow-200">
                        {isLoadingMoreHistory
                          ? "Loading historical data…"
                          : "No candle data available"}
                      </p>
                      {!isLoadingMoreHistory && (
                        <>
                          <p className="mt-1 text-xs text-yellow-400">
                            {getNoDataMessage(
                              instrument.trading_symbol ?? instrument.instrument_key,
                              fromDate,
                              toDate,
                            )}
                          </p>
                          <p className="mt-2 text-xs text-yellow-600">
                            {instrument.instrument_key} &bull; {fromDate}
                            {fromDate !== toDate && ` — ${toDate}`}
                          </p>
                        </>
                      )}
                      {isLoadingMoreHistory && (
                        <div className="mt-2 flex items-center gap-2 text-xs text-yellow-400">
                          <Loader2 className="h-3 w-3 animate-spin" />
                          Searching for historical data…
                        </div>
                      )}
                    </div>
                  ) : null}
                </div>

                {/* Analysis section */}
                {showAnalysis && (
                  <AnalysisCardsSection instrumentKey={instrument.instrument_key} />
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
}
