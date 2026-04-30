"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { X, Loader2, Plus, Check } from "lucide-react";
import { upstoxAPI, isNetworkError, isRateLimitError, getRetryAfterMs } from "@/lib/api";
import { mergeCandles } from "@/lib/candle-transforms";
import { useLtp } from "@/hooks/useLtp";
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
  getLoadMoreChunkDays,
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
import { LivePriceBadge } from "@/components/shared/LivePriceBadge";
import type {
  UpstoxCandlesResponse,
  UpstoxInstrument,
  UpstoxLtpTick,
} from "@/types/upstox";

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

  // Consume from the singleton context — no duplicate hook instance.
  const {
    items: watchlistItems,
    addToWatchlist,
    removeFromWatchlist,
    isAdding,
    isRemoving,
    subscribeInstrument,
    unsubscribeInstrument,
    isMarketFeedConnected,
  } = useWatchlist();

  // ── Derived watchlist state — zero extra API calls ──────────────────────────
  const watchlistEntry = watchlistItems.find(
    (i) => i.instrument_key === instrument.instrument_key,
  );
  const inWatchlist = !!watchlistEntry;
  const watchlistItemId = watchlistEntry?.id ?? null;

  // ── Live price from market feed ─────────────────────────────────────────────
  const priceSnapshot = useLtp(instrument.instrument_key);
  const displayPrice  = priceSnapshot?.ltp ?? null;

  // Subscribe this instrument to the market feed while DetailPane is mounted.
  // This handles instruments not in the watchlist (e.g. opened from search).
  useEffect(() => {
    subscribeInstrument(instrument.instrument_key);
    return () => unsubscribeInstrument(instrument.instrument_key);
  }, [instrument.instrument_key, subscribeInstrument, unsubscribeInstrument]);

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

  // Infinite-scroll state
  const [earliestLoadedDate, setEarliestLoadedDate] = useState<string | null>(null);
  const [isLoadingMoreHistory, setIsLoadingMoreHistory] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(true);
  const [timeframeSwitchMessage, setTimeframeSwitchMessage] = useState<string | null>(null);

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

    const daysToLoad  = getLoadMoreChunkDays(candleUnit, candleInterval);
    const newFromDate = addDays(earliestLoadedDate, -daysToLoad);
    const newToDate   = addDays(earliestLoadedDate, -1);
    const minDate     = getMinAvailableDateForUnit(candleUnit);

    if (newFromDate < minDate && candleUnit === "minutes") {
      setTimeframeSwitchMessage("Reached the oldest available data for this timeframe.");
      setHasMoreHistory(false);
      return; // finally handles setIsLoadingMoreHistory(false)
    }

    // Inline fetch — extracted so it can be called twice (initial + one retry).
    const fetchChunk = () =>
      upstoxAPI.getHistoricalCandles(instrument.instrument_key, {
        unit: candleUnit,
        interval: candleInterval,
        to_date: newToDate,
        from_date: newFromDate,
      });

    // Single-retry wrapper: if the backend propagates a 429, honour the
    // Retry-After header, surface a brief message, then make one more attempt.
    const fetchWithRetry = async () => {
      try {
        return await fetchChunk();
      } catch (err) {
        if (!isRateLimitError(err)) throw err;
        const waitMs = getRetryAfterMs(err);
        setTimeframeSwitchMessage(
          `Rate limit reached — retrying in ${Math.ceil(waitMs / 1_000)}s…`,
        );
        await new Promise<void>((resolve) => setTimeout(resolve, waitMs));
        setTimeframeSwitchMessage(null);
        return fetchChunk(); // second attempt; errors propagate to outer catch
      }
    };

    try {
      const result = await fetchWithRetry();

      if ((result?.data?.candles?.length ?? 0) > 0) {
        // Functional updater avoids stale closure on candles after async gap.
        setCandles((prev) => {
          if (!prev) return null;
          const merged = mergeCandles(result.data.candles, prev.data.candles ?? []);
          return { ...prev, data: { candles: merged } };
        });

        const firstTs = candleTimestampToDate(result.data.candles[0][0]);
        setEarliestLoadedDate(firstTs);
        setHasMoreHistory(new Date(firstTs) > new Date(minDate));
      } else {
        // Empty window (holiday / weekend) — step the cursor back so the
        // next trigger searches a fresh range without re-requesting this one.
        setEarliestLoadedDate(newFromDate);
      }
    } catch (error) {
      if (isRateLimitError(error)) {
        // Both attempts exhausted — leave hasMoreHistory true so the user can
        // scroll to retry once the rate-limit window resets.
        setTimeframeSwitchMessage("Rate limit reached — scroll left to retry.");
      } else {
        if (!isNetworkError(error)) {
          console.error("[DetailPane] Failed to load historical data:", error);
        }
        setHasMoreHistory(false);
      }
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


  // ── Live price derivations ─────────────────────────────────────────────────

  // liveTick adapts the PriceFeed snapshot into the shape CandlestickChart
  // expects for live candle merge. Null when no tick has arrived yet.
  const liveTick: UpstoxLtpTick | null = priceSnapshot?.ltp != null
    ? { instrument_key: instrument.instrument_key, last_price: priceSnapshot.ltp, server_timestamp: String(priceSnapshot.ts) }
    : null;

  // Previous session close — derived from the most recent pre-today daily+
  // candle. Only meaningful for daily/weekly/monthly candle units; intraday
  // candles don't carry a reliable daily baseline.
  const prevClose = useMemo(() => {
    if (!candles?.data?.candles?.length) return null;
    if (candleUnit !== "days" && candleUnit !== "weeks" && candleUnit !== "months") return null;
    const today = getISTTodayYMD();
    const arr = candles.data.candles;
    // Candles are ascending — walk from the end to find the latest pre-today candle.
    for (let i = arr.length - 1; i >= 0; i--) {
      if (candleTimestampToDate(arr[i][0]) < today) {
        return arr[i][4] as number;
      }
    }
    return null;
  }, [candles, candleUnit]);

  const isLive       = isMarketFeedConnected && getISTMarketSession() === "open";
  const isConnecting = !isMarketFeedConnected && getISTMarketSession() === "open";

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <div className="fixed inset-0 z-50 bg-slate-950/40 backdrop-blur-sm">
      <div className="absolute inset-0 overflow-y-auto">
        <div className="flex h-full flex-col">
          <div className="flex flex-1 flex-col w-full">
            <Card className="flex flex-1 flex-col rounded-none border-0 border-slate-200 bg-white shadow-2xl">
              {/* ── Header ─────────────────────────────────────────────────── */}
              <CardHeader className="flex flex-row items-center justify-between gap-6 shrink-0">
                {/* Identity + live price */}
                <div className="min-w-0 flex-1 flex items-center gap-4">
                  <div className="min-w-0">
                    <CardTitle className="text-xl font-bold leading-tight text-slate-900">
                      {instrument.name || "—"}
                    </CardTitle>
                    <div className="mt-1 flex items-center gap-2">
                      <span className="font-mono text-sm font-medium text-slate-500">
                        {instrument.trading_symbol}
                      </span>
                      {instrument.exchange && (
                        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-500">
                          {instrument.exchange}
                        </span>
                      )}
                    </div>
                  </div>

                  <LivePriceBadge
                    price={displayPrice ?? null}
                    prevClose={prevClose}
                    isLive={isLive}
                    isConnecting={isConnecting}
                    className="mt-3"
                    align="start"
                  />
                </div>

                <div className="flex shrink-0 items-center gap-2">
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
                          isLive={isLive}
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
