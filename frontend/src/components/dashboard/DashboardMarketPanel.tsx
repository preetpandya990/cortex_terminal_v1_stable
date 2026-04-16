/**
 * Cortex AI — DashboardMarketPanel
 * ==================================
 * Fixes vs original:
 *  - Exponential back-off on API failure (5s → 60s cap)
 *  - AbortController cancels in-flight requests before next interval
 *  - Reliable IST market hours via Intl.DateTimeFormat (no toLocaleString hack)
 *  - Cleans up on unmount — no dangling intervals or pending fetches
 */
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { LivePriceVolumeChart } from "./LivePriceVolumeChart";
import { api } from "@/lib/api-client";
import { useAuth } from "@/contexts/AuthContext";
import type { UpstoxCandle } from "@/types/upstox";

interface MarketStatus {
  isOpen: boolean;
  nextEvent: string;
}

interface LivePrice {
  instrument_key: string;
  last_price: number;
  timestamp: string | null;
}

const POLL_BASE_MS = 5_000;
const POLL_MAX_MS = 60_000;
const MARKET_OPEN_HOUR_IST = 9;
const MARKET_OPEN_MINUTE_IST = 15;
const MARKET_CLOSE_HOUR_IST = 15;
const MARKET_CLOSE_MINUTE_IST = 30;

/**
 * Returns the current time broken into parts in the Asia/Kolkata timezone.
 * Uses Intl.DateTimeFormat — spec-compliant in all modern browsers and Node.
 */
function getISTTimeParts(): { hour: number; minute: number; dayOfWeek: number } {
  const now = new Date();
  const formatter = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "numeric",
    minute: "numeric",
    weekday: "short",
    hour12: false,
  });
  const parts = formatter.formatToParts(now);
  const get = (type: string) =>
    parseInt(parts.find((p) => p.type === type)?.value ?? "0", 10);
  const weekday = parts.find((p) => p.type === "weekday")?.value ?? "Mon";
  const dayOfWeek = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(weekday);
  return { hour: get("hour"), minute: get("minute"), dayOfWeek };
}

function checkMarketStatus(): MarketStatus {
  const { hour, minute, dayOfWeek } = getISTTimeParts();
  const isWeekday = dayOfWeek >= 1 && dayOfWeek <= 5;
  if (!isWeekday) return { isOpen: false, nextEvent: "Opens Monday 9:15 AM IST" };

  const totalMinutes = hour * 60 + minute;
  const openMinutes = MARKET_OPEN_HOUR_IST * 60 + MARKET_OPEN_MINUTE_IST;
  const closeMinutes = MARKET_CLOSE_HOUR_IST * 60 + MARKET_CLOSE_MINUTE_IST;

  if (totalMinutes >= openMinutes && totalMinutes < closeMinutes) {
    return { isOpen: true, nextEvent: "Closes 3:30 PM IST" };
  }
  if (totalMinutes < openMinutes) {
  const { isAuthenticated } = useAuth();
    return { isOpen: false, nextEvent: "Opens 9:15 AM IST" };
  }
  return { isOpen: false, nextEvent: "Opens tomorrow 9:15 AM IST" };
}

interface DashboardMarketPanelProps {
  instrumentKey: string;
}
export function DashboardMarketPanel({ instrumentKey }: DashboardMarketPanelProps) {
  const { isAuthenticated } = useAuth();
  const [livePrice, setLivePrice] = useState<LivePrice | null>(null);
  const [marketStatus, setMarketStatus] = useState<MarketStatus>(checkMarketStatus);
  const [error, setError] = useState<string | null>(null);
  const [candleError, setCandleError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [candles, setCandles] = useState<UpstoxCandle[]>([]);
  const [isCandleLoading, setIsCandleLoading] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const candleAbortRef = useRef<AbortController | null>(null);
  const intervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryDelayRef = useRef(POLL_BASE_MS);
  const candleIntervalRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const fetchLivePrice = useCallback(async () => {
    if (!isAuthenticated) return;
    
    // Cancel any in-flight request
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setIsLoading(true);
    try {
      const response = await api.get(`/market-data/live/${instrumentKey}`, {
        signal: controller.signal
      });
      setLivePrice(response.data);
      setError(null);
      retryDelayRef.current = POLL_BASE_MS; // reset on success
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setError("Unable to fetch live price");
      // Exponential back-off: double the delay, cap at max
      retryDelayRef.current = Math.min(retryDelayRef.current * 2, POLL_MAX_MS);
    } finally {
      setIsLoading(false);
    }
  }, [instrumentKey, isAuthenticated]);

  const fetchCandles = useCallback(async () => {
    if (!isAuthenticated) return;
    candleAbortRef.current?.abort();
    const controller = new AbortController();
    candleAbortRef.current = controller;
    setIsCandleLoading(true);

    try {
      const response = await api.get(`/upstox/candles/intraday`, {
        params: {
          instrument_key: instrumentKey,
          unit: 'minute',
          interval: 1,
          timeframe: '1m',
          limit: 180
        },
        signal: controller.signal,
      });
      const nextCandles = Array.isArray(response.data?.candles) ? response.data.candles : [];
      setCandles(nextCandles);
      setCandleError(null);
    } catch (err) {
      if ((err as Error).name === "AbortError") return;
      setCandleError("Unable to load recent candle data");
    } finally {
      setIsCandleLoading(false);
    }
  }, [instrumentKey]);

  // Poll with dynamic delay based on back-off
  useEffect(() => {
    if (!isAuthenticated) return;
    
    let alive = true;

    const poll = async () => {
      if (!alive) return;
      await fetchLivePrice();
      if (alive) {
        intervalRef.current = setTimeout(poll, retryDelayRef.current);
      }
    };

    poll();

    // Update market status every minute
    const statusInterval = setInterval(() => {
      setMarketStatus(checkMarketStatus());
    }, 60_000);

    return () => {
      alive = false;
      abortRef.current?.abort();
      if (intervalRef.current) clearTimeout(intervalRef.current);
      clearInterval(statusInterval);
    };
  }, [fetchLivePrice, isAuthenticated]);

  useEffect(() => {
    if (!isAuthenticated) return;
    
    let alive = true;

    const refresh = async () => {
      if (!alive) return;
      await fetchCandles();
      if (alive) {
        candleIntervalRef.current = setTimeout(refresh, 60_000);
      }
    };

    refresh();

    return () => {
      alive = false;
      candleAbortRef.current?.abort();
      if (candleIntervalRef.current) clearTimeout(candleIntervalRef.current);
    };
  }, [fetchCandles]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <span
          className={`h-2 w-2 rounded-full ${marketStatus.isOpen ? "bg-green-500" : "bg-gray-400"}`}
          aria-hidden="true"
        />
        <span className="text-sm text-gray-400">
          {marketStatus.isOpen ? "Market Open" : "Market Closed"} — {marketStatus.nextEvent}
        </span>
      </div>

      {livePrice && (
        <div className="flex items-baseline gap-2">
          <span className="text-3xl font-semibold tabular-nums">
            ₹{livePrice.last_price.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
          </span>
          {livePrice.timestamp && (
            <span className="text-xs text-gray-500">
              {new Date(livePrice.timestamp).toLocaleTimeString("en-IN", {
                timeZone: "Asia/Kolkata",
              })}
            </span>
          )}
        </div>
      )}

      {error && (
        <p className="text-sm text-red-400" role="alert">
          {error}
          {retryDelayRef.current > POLL_BASE_MS && (
            <span className="ml-1 text-gray-500">
              (retrying in {retryDelayRef.current / 1000}s)
            </span>
          )}
        </p>
      )}

      {candleError && (
        <p className="text-xs text-amber-400" role="status">
          {candleError}
        </p>
      )}
      
      {!isAuthenticated ? (
        <div className="rounded-lg border bg-muted p-8 text-center">
          <h3 className="text-lg font-semibold mb-2">Authentication Required</h3>
          <p className="text-sm text-muted-foreground">
            Please log in to view live market data and charts.
          </p>
        </div>
      ) : (
        <LivePriceVolumeChart candles={candles} />
      )}
    </div>
  );
}
