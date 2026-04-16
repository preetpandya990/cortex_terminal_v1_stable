"use client";

import { useEffect, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Radar, Loader2, Calendar } from "lucide-react";
import { WS_BASE_URL, upstoxAPI } from "@/lib/api";
import {
  addDays,
  INTERVAL_PRESETS,
  RANGE_PRESETS,
  applyPreset,
  clampRange,
  diffDaysInclusive,
  getDefaultHistoricalState,
  getMinAvailableDateForUnit,
  getISTTodayYMD,
  includesTodayOrYesterday,
  resolveCandleSourceMode,
  shouldApplyAutoUpgrade,
  type HistoricalState,
  type RangePreset,
  type IntervalPreset,
} from "@/lib/chart-policy";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { CandlestickChart } from "@/components/charts/CandlestickChart";
import { InstrumentSearchCombobox } from "@/components/market/InstrumentSearchCombobox";
import { AnalysisCardsSection } from "@/components/AnalysisCardsSection";
import type {
  UpstoxCandlesResponse,
  UpstoxInstrument,
  UpstoxLtpTick,
  UpstoxTickStreamMessage,
} from "@/types/upstox";

const TICK_STREAM_INTERVAL_MS = 500;
const TICK_RECONNECT_INITIAL_MS = 800;
const TICK_RECONNECT_MAX_MS = 8000;
const HISTORY_CHUNK_DAYS_INTRADAY = 10;
const HISTORY_CHUNK_DAYS_DAILY = 120;
const HISTORY_CHUNK_DAYS_WEEKLY = 365;
const HISTORY_CHUNK_DAYS_MONTHLY = 730;

function buildTickStreamUrl(instrumentKey: string): string {
  const wsBase = WS_BASE_URL.replace(/\/$/, "");
  const hasApiV1Prefix = /\/api\/v1$/i.test(wsBase);
  const path = hasApiV1Prefix ? "/upstox/ticks/ws" : "/api/v1/upstox/ticks/ws";
  const url = new URL(`${wsBase}${path}`);
  url.searchParams.set("instrument_key", instrumentKey);
  url.searchParams.set("interval_ms", String(TICK_STREAM_INTERVAL_MS));
  return url.toString();
}

function normalizeTimestamp(value: string | number): number {
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
    return 0;
  }
  if (!Number.isFinite(value)) return 0;
  return value > 10_000_000_000 ? value : value * 1000;
}

function getHistoryChunkDays(unit: HistoricalState["unit"]): number {
  if (unit === "minutes" || unit === "hours") return HISTORY_CHUNK_DAYS_INTRADAY;
  if (unit === "days") return HISTORY_CHUNK_DAYS_DAILY;
  if (unit === "weeks") return HISTORY_CHUNK_DAYS_WEEKLY;
  return HISTORY_CHUNK_DAYS_MONTHLY;
}

function mergeCandles(
  historicalCandles: UpstoxCandlesResponse["data"]["candles"],
  intradayCandles: UpstoxCandlesResponse["data"]["candles"]
): UpstoxCandlesResponse["data"]["candles"] {
  const byTs = new Map<number, UpstoxCandlesResponse["data"]["candles"][number]>();
  for (const candle of historicalCandles) {
    byTs.set(normalizeTimestamp(candle[0]), candle);
  }
  for (const candle of intradayCandles) {
    byTs.set(normalizeTimestamp(candle[0]), candle);
  }
  return [...byTs.values()].sort((a, b) => normalizeTimestamp(a[0]) - normalizeTimestamp(b[0]));
}

export default function HawkEyeRadarPage() {
  const calendarRef = useRef<HTMLDivElement | null>(null);
  const [selected, setSelected] = useState<UpstoxInstrument | null>(null);
  const [candles, setCandles] = useState<UpstoxCandlesResponse | null>(null);
  const [historicalState, setHistoricalState] = useState<HistoricalState>(() => getDefaultHistoricalState());
  const [isCalendarOpen, setIsCalendarOpen] = useState(false);
  const [calendarMode, setCalendarMode] = useState<"day" | "range">("range");
  const [singleDate, setSingleDate] = useState(() => getISTTodayYMD());
  const [rangeStart, setRangeStart] = useState(() => getISTTodayYMD());
  const [rangeEnd, setRangeEnd] = useState(() => getISTTodayYMD());
  const [liveTick, setLiveTick] = useState<UpstoxLtpTick | null>(null);
  const [tickStreamStatus, setTickStreamStatus] = useState<"idle" | "connecting" | "live" | "reconnecting" | "error">("idle");
  const [tickStreamError, setTickStreamError] = useState<string | null>(null);
  const [intervalNotice, setIntervalNotice] = useState<string | null>(null);
  const [isCandleSizeLocked, setIsCandleSizeLocked] = useState(false);
  const [isLoadingOlderCandles, setIsLoadingOlderCandles] = useState(false);
  const [hasMoreHistory, setHasMoreHistory] = useState(true);

  const tickSocketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef(0);
  const historyCursorToDateRef = useRef<string | null>(null);
  const currentCandleQueryKeyRef = useRef<string>("");

  const candleUnit = historicalState.unit;
  const candleInterval = historicalState.interval;
  const fromDate = historicalState.fromDate;
  const toDate = historicalState.toDate;
  const selectedRangeDays = diffDaysInclusive(fromDate, toDate);
  const candleSourceMode = resolveCandleSourceMode(selectedRangeDays, candleUnit, fromDate, toDate);
  const liveMergeEnabled = includesTodayOrYesterday(fromDate, toDate);
  const candleQueryKey = selected
    ? `${selected.instrument_key}|${candleUnit}|${candleInterval}|${fromDate}|${toDate}|${candleSourceMode}`
    : "";

  const candlesMutation = useMutation({
    mutationFn: async () => {
      if (!selected) {
        throw new Error("No instrument selected");
      }
      if (candleSourceMode === "intraday") {
        return upstoxAPI.getIntradayCandles(selected.instrument_key, {
          unit: candleUnit,
          interval: candleInterval,
        });
      }
      if (candleSourceMode === "hybrid") {
        const today = getISTTodayYMD();
        const yesterday = addDays(today, -1);
        const shouldLoadHistorical = fromDate <= yesterday;

        const [historical, intraday] = await Promise.all([
          shouldLoadHistorical
            ? upstoxAPI.getHistoricalCandles(selected.instrument_key, {
                unit: candleUnit,
                interval: candleInterval,
                to_date: yesterday,
                from_date: fromDate,
              })
            : Promise.resolve({ status: "success", data: { candles: [] } }),
          upstoxAPI.getIntradayCandles(selected.instrument_key, {
            unit: candleUnit,
            interval: candleInterval,
          }),
        ]);

        const merged = mergeCandles(
          (historical?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"],
          (intraday?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"],
        );
        return { status: "success", data: { candles: merged } } as UpstoxCandlesResponse;
      }

      const historical = await upstoxAPI.getHistoricalCandles(selected.instrument_key, {
        unit: candleUnit,
        interval: candleInterval,
        to_date: toDate,
        from_date: fromDate,
      });
      const historicalCandles = (historical?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"];

      // Fallback requested: if recent-day historical is empty, try intraday.
      if (historicalCandles.length === 0 && fromDate <= getISTTodayYMD() && toDate >= getISTTodayYMD()) {
        const intraday = await upstoxAPI.getIntradayCandles(selected.instrument_key, {
          unit: candleUnit,
          interval: candleInterval,
        });
        const intradayCandles = (intraday?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"];
        if (intradayCandles.length > 0) {
          return { status: "success", data: { candles: intradayCandles } } as UpstoxCandlesResponse;
        }
      }

      return historical;
    },
    onSuccess: (data: UpstoxCandlesResponse) => {
      setCandles((prev) => {
        const incoming = (data?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"];
        if (currentCandleQueryKeyRef.current !== candleQueryKey) {
          currentCandleQueryKeyRef.current = candleQueryKey;
          return { status: "success", data: { candles: incoming } };
        }
        const merged = mergeCandles(
          (prev?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"],
          incoming,
        );
        return { status: "success", data: { candles: merged } };
      });
    },
  });

  const handleSelect = (instrument: UpstoxInstrument) => {
    setSelected(instrument);
    setIsCandleSizeLocked(false);
    setIntervalNotice(null);
    setHistoricalState(() => getDefaultHistoricalState());
  };

  const applyHistoricalPreset = (preset: RangePreset) => {
    setIsCandleSizeLocked(false);
    setHistoricalState(() => applyPreset(preset));
  };

  const updateHistoricalState = (updater: (prev: HistoricalState) => HistoricalState) => {
    setHistoricalState((prev) => {
      const next = updater(prev);
      const maxInterval =
        next.unit === "minutes" ? 300 : next.unit === "hours" ? 5 : 1;
      const normalizedInterval = Math.min(Math.max(next.interval, 1), maxInterval);
      const range = clampRange(next.unit, normalizedInterval, next.fromDate, next.toDate);
      return {
        ...next,
        interval: normalizedInterval,
        fromDate: range.fromDate,
        toDate: range.toDate,
      };
    });
  };

  useEffect(() => {
    if (!shouldApplyAutoUpgrade(candleUnit, selectedRangeDays, isCandleSizeLocked)) return;

    setHistoricalState((prev) => {
      if (!shouldApplyAutoUpgrade(prev.unit, diffDaysInclusive(prev.fromDate, prev.toDate), isCandleSizeLocked)) {
        return prev;
      }
      return {
        ...prev,
        unit: "days",
        interval: 1,
      };
    });
    setIntervalNotice("Interval auto-adjusted to 1D for long-range historical view.");

    const timeoutId = window.setTimeout(() => {
      setIntervalNotice(null);
    }, 5000);
    return () => window.clearTimeout(timeoutId);
  }, [candleUnit, selectedRangeDays, isCandleSizeLocked]);

  useEffect(() => {
    if (!selected) return;
    currentCandleQueryKeyRef.current = candleQueryKey;
    const minDate = getMinAvailableDateForUnit(candleUnit);
    const initialCursor = addDays(fromDate, -1);
    historyCursorToDateRef.current = initialCursor >= minDate ? initialCursor : null;
    setHasMoreHistory(historyCursorToDateRef.current !== null);
    setIsLoadingOlderCandles(false);
    setCandles(null);
    candlesMutation.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    selected,
    candleUnit,
    candleInterval,
    fromDate,
    toDate,
    candleSourceMode,
    candleQueryKey,
  ]);

  useEffect(() => {
    if (!selected) return;
    const intervalId = window.setInterval(() => {
      candlesMutation.mutate();
    }, 45_000);
    return () => window.clearInterval(intervalId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, candleUnit, candleInterval, fromDate, toDate, candleSourceMode]);

  const loadMoreHistory = async () => {
    if (!selected || isLoadingOlderCandles || !hasMoreHistory) {
      return;
    }
    const cursorToDate = historyCursorToDateRef.current;
    if (!cursorToDate) {
      setHasMoreHistory(false);
      return;
    }

    const minDate = getMinAvailableDateForUnit(candleUnit);
    if (cursorToDate < minDate) {
      historyCursorToDateRef.current = null;
      setHasMoreHistory(false);
      return;
    }

    const chunkDays = getHistoryChunkDays(candleUnit);
    const chunkFrom = addDays(cursorToDate, -(chunkDays - 1)) < minDate
      ? minDate
      : addDays(cursorToDate, -(chunkDays - 1));

    setIsLoadingOlderCandles(true);
    try {
      const historical = await upstoxAPI.getHistoricalCandles(selected.instrument_key, {
        unit: candleUnit,
        interval: candleInterval,
        to_date: cursorToDate,
        from_date: chunkFrom,
      });

      const olderCandles = (historical?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"];
      if (olderCandles.length > 0) {
        setCandles((prev) => {
          const merged = mergeCandles(
            olderCandles,
            (prev?.data?.candles ?? []) as UpstoxCandlesResponse["data"]["candles"],
          );
          return { status: "success", data: { candles: merged } };
        });
      }
    } finally {
      const nextCursor = addDays(chunkFrom, -1);
      historyCursorToDateRef.current = nextCursor >= minDate ? nextCursor : null;
      setHasMoreHistory(historyCursorToDateRef.current !== null);
      setIsLoadingOlderCandles(false);
    }
  };

  useEffect(() => {
    if (!selected) {
      setTickStreamStatus("idle");
      setTickStreamError(null);
      setLiveTick(null);
      if (tickSocketRef.current) {
        tickSocketRef.current.close();
        tickSocketRef.current = null;
      }
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      reconnectAttemptRef.current = 0;
      return;
    }

    let cancelled = false;

    const clearReconnectTimer = () => {
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
    };

    const closeSocket = () => {
      if (tickSocketRef.current) {
        tickSocketRef.current.onopen = null;
        tickSocketRef.current.onmessage = null;
        tickSocketRef.current.onerror = null;
        tickSocketRef.current.onclose = null;
        tickSocketRef.current.close();
        tickSocketRef.current = null;
      }
    };

    const scheduleReconnect = () => {
      if (cancelled) return;
      clearReconnectTimer();
      reconnectAttemptRef.current += 1;
      const delay = Math.min(
        TICK_RECONNECT_MAX_MS,
        TICK_RECONNECT_INITIAL_MS * 2 ** (reconnectAttemptRef.current - 1)
      );
      setTickStreamStatus("reconnecting");
      reconnectTimerRef.current = window.setTimeout(() => {
        connect();
      }, delay);
    };

    const connect = () => {
      if (cancelled) return;
      closeSocket();
      setTickStreamStatus(reconnectAttemptRef.current > 0 ? "reconnecting" : "connecting");
      setTickStreamError(null);

      const socket = new WebSocket(buildTickStreamUrl(selected.instrument_key));
      tickSocketRef.current = socket;

      socket.onopen = () => {
        if (cancelled) return;
        reconnectAttemptRef.current = 0;
        setTickStreamStatus("live");
        setTickStreamError(null);
      };

      socket.onmessage = (event) => {
        if (cancelled) return;
        try {
          const payload = JSON.parse(event.data) as UpstoxTickStreamMessage;
          if (payload.type === "tick") {
            setLiveTick(payload.data);
            return;
          }
          if (payload.type === "error") {
            setTickStreamStatus("error");
            setTickStreamError(payload.data.message || "Tick stream error");
          }
        } catch {
          setTickStreamStatus("error");
          setTickStreamError("Invalid tick stream payload");
        }
      };

      socket.onerror = () => {
        if (cancelled) return;
        setTickStreamStatus("error");
        setTickStreamError("Tick stream socket error");
      };

      socket.onclose = () => {
        if (cancelled) return;
        scheduleReconnect();
      };
    };

    setLiveTick(null);
    connect();

    return () => {
      cancelled = true;
      clearReconnectTimer();
      closeSocket();
    };
  }, [selected]);

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (calendarRef.current && !calendarRef.current.contains(target)) {
        setIsCalendarOpen(false);
      }
    };

    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  useEffect(() => {
    if (!isCalendarOpen) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setIsCalendarOpen(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isCalendarOpen]);

  const intervalPresets = INTERVAL_PRESETS;

  const applyIntervalPreset = (preset: IntervalPreset) => {
    setIsCandleSizeLocked(true);
    updateHistoricalState((prev) => ({
      ...prev,
      unit: preset.unit,
      interval: preset.interval,
    }));
    setIntervalNotice("Candle size locked by manual selection. Date range auto-clamped to provider limits.");
    window.setTimeout(() => {
      setIntervalNotice(null);
    }, 5000);
  };

  return (
    <div className="space-y-8">
      <Card className="border-slate-200/80 bg-white/90">
        <CardHeader>
          <CardTitle className="flex items-center gap-3 text-2xl">
            <Radar className="h-6 w-6 text-blue-600" />
            Hawk-Eye Radar
          </CardTitle>
          <CardDescription>
            Unified chart mode: historical candles with live tick overlays on recent windows.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          <div className="grid gap-4 md:grid-cols-[1fr]">
            <InstrumentSearchCombobox
              onSelect={handleSelect}
              selectedInstrumentKey={selected?.instrument_key ?? null}
              placeholder="Type symbol or company name (e.g. RELIANCE)"
              helperText="Minimum 1 character. NSE equities only."
              segment="NSE_EQ"
              limit={12}
              showQuickLtp
            />
          </div>

          <div className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="space-y-1">
                <div className="text-sm font-semibold uppercase tracking-widest text-slate-500">
                  Candlestick Chart
                </div>
                {selected && (
                  <div className="text-sm text-slate-600">
                    <span className="font-semibold text-slate-900">
                      {selected.trading_symbol}
                    </span>
                    <span className="text-slate-400"> · </span>
                    <span>{selected.name || "—"}</span>
                  </div>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <div className="flex items-center gap-2">
                  <label className="text-xs font-medium text-slate-500">Candle Size</label>
                  <select
                    value={`${candleUnit}:${candleInterval}`}
                    onChange={(event) => {
                      const [unitValue, intervalValue] = event.target.value.split(":");
                      const preset = intervalPresets.find(
                        (item) => item.unit === unitValue && item.interval === Number(intervalValue)
                      );
                      if (preset) {
                        applyIntervalPreset(preset);
                      }
                    }}
                    className="h-9 rounded-lg border border-slate-200 px-3 text-xs shadow-sm"
                  >
                    {intervalPresets.map((preset) => (
                      <option key={preset.id} value={`${preset.unit}:${preset.interval}`}>
                        {preset.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="rounded-full bg-slate-100 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-slate-700">
                  {candleSourceMode === "intraday"
                    ? "Data: intraday"
                    : candleSourceMode === "hybrid"
                      ? "Data: hybrid"
                      : "Data: historical"}
                </div>
                <div className="rounded-full bg-slate-100 px-3 py-1 text-[11px] font-semibold uppercase tracking-wide text-slate-700">
                  {liveMergeEnabled ? "Live merge: on" : "Live merge: off"}
                </div>
                <div
                  className={`rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-wide ${
                    tickStreamStatus === "live"
                      ? "bg-emerald-100 text-emerald-700"
                      : tickStreamStatus === "connecting" || tickStreamStatus === "reconnecting"
                        ? "bg-amber-100 text-amber-700"
                        : tickStreamStatus === "error"
                          ? "bg-rose-100 text-rose-700"
                          : "bg-slate-100 text-slate-600"
                  }`}
                >
                  {tickStreamStatus}
                </div>
              </div>
            </div>

            <div className="rounded-2xl border border-slate-200 bg-slate-950/95 p-4 shadow-sm">
              {!selected && (
                <div className="text-sm text-slate-400">
                  Search and select a stock to render its candlestick chart.
                </div>
              )}
              {selected && candlesMutation.isPending && (
                <div className="flex items-center gap-2 text-sm text-slate-300">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Loading candles...
                </div>
              )}
              {selected && candlesMutation.isError && (
                <div className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-600">
                  Failed to load candles. Confirm Upstox credentials and try again.
                </div>
              )}
              {selected && intervalNotice && (
                <div className="mb-3 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-xs text-blue-700">
                  {intervalNotice}
                </div>
              )}
              {selected && tickStreamError && (
                <div className="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-xs text-rose-700">
                  {tickStreamError}
                </div>
              )}
              {selected && candles?.data?.candles?.length ? (
                <div className="h-[460px]">
                  <CandlestickChart
                    candles={candles.data.candles}
                    liveTick={liveMergeEnabled ? liveTick : null}
                    candleUnit={candleUnit}
                    candleInterval={candleInterval}
                    canLoadMoreHistory={hasMoreHistory}
                    isLoadingMoreHistory={isLoadingOlderCandles}
                    onLoadMoreHistory={loadMoreHistory}
                    height={460}
                  />
                </div>
              ) : selected && !candlesMutation.isPending ? (
                <div className="text-sm text-slate-400">No candle data available.</div>
              ) : null}
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-semibold uppercase tracking-[0.2em] text-slate-400">
                  Time Frame
                </span>
                <div className="flex flex-wrap items-center gap-2">
                  {RANGE_PRESETS.map((preset) => {
                    const isActive =
                      !historicalState.isCustom && historicalState.presetId === preset.id;
                    return (
                      <button
                        key={preset.id}
                        type="button"
                        onClick={() => applyHistoricalPreset(preset)}
                        className={`rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-wide transition ${
                          isActive
                            ? "bg-slate-900 text-white"
                            : "border border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                        }`}
                        aria-pressed={isActive}
                      >
                        {preset.label}
                      </button>
                    );
                  })}
                  <button
                    type="button"
                    onClick={() => {
                      setCalendarMode("range");
                      setSingleDate(historicalState.fromDate);
                      setRangeStart(historicalState.fromDate);
                      setRangeEnd(historicalState.toDate);
                      setIsCalendarOpen(true);
                    }}
                    className={`flex items-center gap-2 rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-wide transition ${
                      historicalState.isCustom
                        ? "bg-blue-600 text-white"
                        : "border border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                    }`}
                  >
                    <Calendar className="h-3.5 w-3.5" />
                    Custom
                  </button>
                </div>
              </div>
            </div>
            {isCalendarOpen && (
              <div className="fixed inset-0 z-40" ref={calendarRef}>
                <div className="absolute inset-0 bg-slate-950/40 backdrop-blur-sm" />
                <div className="relative mx-auto mt-24 w-full max-w-lg rounded-2xl border border-slate-200 bg-white p-5 shadow-2xl">
                  <div className="flex items-center justify-between">
                    <div className="text-sm font-semibold text-slate-900">Custom Time Frame</div>
                    <button
                      type="button"
                      onClick={() => setIsCalendarOpen(false)}
                      className="text-xs font-semibold text-slate-500 hover:text-slate-800"
                    >
                      Close
                    </button>
                  </div>
                  <div className="mt-4 flex gap-2">
                    <button
                      type="button"
                      onClick={() => setCalendarMode("day")}
                      className={`rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-wide transition ${
                        calendarMode === "day"
                          ? "bg-slate-900 text-white"
                          : "border border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                      }`}
                    >
                      Single Day
                    </button>
                    <button
                      type="button"
                      onClick={() => setCalendarMode("range")}
                      className={`rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-wide transition ${
                        calendarMode === "range"
                          ? "bg-slate-900 text-white"
                          : "border border-slate-200 bg-white text-slate-600 hover:border-slate-300"
                      }`}
                    >
                      Custom Range
                    </button>
                  </div>
                  {calendarMode === "day" ? (
                    <div className="mt-4 space-y-2">
                      <label className="text-xs font-medium text-slate-500">Select Date</label>
                      <input
                        type="date"
                        value={singleDate}
                        max={getISTTodayYMD()}
                        onChange={(event) => setSingleDate(event.target.value)}
                        className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm shadow-sm"
                      />
                    </div>
                  ) : (
                    <div className="mt-4 grid gap-3 md:grid-cols-2">
                      <div className="space-y-2">
                        <label className="text-xs font-medium text-slate-500">From</label>
                        <input
                          type="date"
                          value={rangeStart}
                          max={rangeEnd}
                          onChange={(event) => setRangeStart(event.target.value)}
                          className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm shadow-sm"
                        />
                      </div>
                      <div className="space-y-2">
                        <label className="text-xs font-medium text-slate-500">To</label>
                        <input
                          type="date"
                          value={rangeEnd}
                          max={getISTTodayYMD()}
                          onChange={(event) => setRangeEnd(event.target.value)}
                          className="h-10 w-full rounded-lg border border-slate-200 px-3 text-sm shadow-sm"
                        />
                      </div>
                    </div>
                  )}
                  <div className="mt-4 flex justify-end gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setIsCalendarOpen(false)}
                    >
                      Cancel
                    </Button>
                    <Button
                      size="sm"
                      onClick={() => {
                        setIsCandleSizeLocked(false);
                        if (calendarMode === "day") {
                          updateHistoricalState((prev) => ({
                            ...prev,
                            fromDate: singleDate,
                            toDate: singleDate,
                            isCustom: true,
                            presetId: "CUSTOM",
                          }));
                        } else {
                          updateHistoricalState((prev) => ({
                            ...prev,
                            fromDate: rangeStart,
                            toDate: rangeEnd,
                            isCustom: true,
                            presetId: "CUSTOM",
                          }));
                        }
                        setIsCalendarOpen(false);
                      }}
                    >
                      Apply
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Analysis Cards Section */}
      <AnalysisCardsSection
        symbol={selected?.trading_symbol ?? null}
        exchange="NSE_EQ"
        className="mt-8"
      />
    </div>
  );
}
