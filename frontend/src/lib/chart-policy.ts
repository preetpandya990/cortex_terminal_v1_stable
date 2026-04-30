export const IST_TIMEZONE = "Asia/Kolkata";
export const DEFAULT_HISTORICAL_PRESET_ID = "1D";
export const INTRADAY_RANGE_MAX_DAYS = 10;

export type CandleUnit = "minutes" | "hours" | "days" | "weeks" | "months";

export type HistoricalState = {
  unit: CandleUnit;
  interval: number;
  fromDate: string;
  toDate: string;
  presetId: string;
  isCustom: boolean;
};

export type RangePreset = {
  id: string;
  label: string;
  kind: "days" | "months" | "years" | "ytd" | "all";
  value: number;
  recommended: { unit: CandleUnit; interval: number };
};

export type IntervalPreset = {
  id: string;
  label: string;
  unit: CandleUnit;
  interval: number;
};

export type CandleSourceMode = "historical" | "intraday" | "hybrid";

export const RANGE_PRESETS: RangePreset[] = [
  { id: "1D", label: "1D", kind: "days", value: 1, recommended: { unit: "minutes", interval: 1 } },
  { id: "3D", label: "3D", kind: "days", value: 3, recommended: { unit: "minutes", interval: 5 } },
  { id: "5D", label: "5D", kind: "days", value: 5, recommended: { unit: "minutes", interval: 5 } },
  { id: "10D", label: "10D", kind: "days", value: 10, recommended: { unit: "minutes", interval: 15 } },
  { id: "1M", label: "1M", kind: "months", value: 1, recommended: { unit: "minutes", interval: 30 } },
  { id: "3M", label: "3M", kind: "months", value: 3, recommended: { unit: "hours", interval: 1 } },
  { id: "6M", label: "6M", kind: "months", value: 6, recommended: { unit: "days", interval: 1 } },
  { id: "1Y", label: "1Y", kind: "years", value: 1, recommended: { unit: "weeks", interval: 1 } },
  { id: "2Y", label: "2Y", kind: "years", value: 2, recommended: { unit: "weeks", interval: 1 } },
  { id: "5Y", label: "5Y", kind: "years", value: 5, recommended: { unit: "weeks", interval: 1 } },
  { id: "YTD", label: "YTD", kind: "ytd", value: 0, recommended: { unit: "days", interval: 1 } },
  { id: "ALL", label: "ALL", kind: "all", value: 0, recommended: { unit: "days", interval: 1 } },
];

export const INTERVAL_PRESETS: IntervalPreset[] = [
  { id: "1m", label: "1m", unit: "minutes", interval: 1 },
  { id: "2m", label: "2m", unit: "minutes", interval: 2 },
  { id: "3m", label: "3m", unit: "minutes", interval: 3 },
  { id: "5m", label: "5m", unit: "minutes", interval: 5 },
  { id: "10m", label: "10m", unit: "minutes", interval: 10 },
  { id: "15m", label: "15m", unit: "minutes", interval: 15 },
  { id: "30m", label: "30m", unit: "minutes", interval: 30 },
  { id: "45m", label: "45m", unit: "minutes", interval: 45 },
  { id: "1h", label: "1h", unit: "hours", interval: 1 },
  { id: "2h", label: "2h", unit: "hours", interval: 2 },
  { id: "3h", label: "3h", unit: "hours", interval: 3 },
  { id: "4h", label: "4h", unit: "hours", interval: 4 },
  { id: "1D", label: "1D", unit: "days", interval: 1 },
  { id: "1W", label: "1W", unit: "weeks", interval: 1 },
  { id: "1M", label: "1M", unit: "months", interval: 1 },
];

export function getISTTodayYMD(): string {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: IST_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  return formatter.format(new Date());
}

function getISTNowParts(): { ymd: string; hour: number; minute: number; weekday: number } {
  const now = new Date();
  const ymdFormatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: IST_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const hourFormatter = new Intl.DateTimeFormat("en-US", {
    timeZone: IST_TIMEZONE,
    hour: "2-digit",
    hour12: false,
  });
  const minuteFormatter = new Intl.DateTimeFormat("en-US", {
    timeZone: IST_TIMEZONE,
    minute: "2-digit",
  });
  const weekdayFormatter = new Intl.DateTimeFormat("en-US", {
    timeZone: IST_TIMEZONE,
    weekday: "short",
  });
  const weekdayMap: Record<string, number> = {
    Sun: 0,
    Mon: 1,
    Tue: 2,
    Wed: 3,
    Thu: 4,
    Fri: 5,
    Sat: 6,
  };
  const weekdayLabel = weekdayFormatter.format(now);
  return {
    ymd: ymdFormatter.format(now),
    hour: Number(hourFormatter.format(now)),
    minute: Number(minuteFormatter.format(now)),
    weekday: weekdayMap[weekdayLabel] ?? 1,
  };
}

export function getLatestTradingDayYMD(): string {
  const { ymd, hour, minute, weekday } = getISTNowParts();
  let candidate = ymd;

  // Before market open, use prior trading day.
  if (hour < 9 || (hour === 9 && minute < 15)) {
    candidate = addDays(candidate, -1);
  }

  // Shift weekends to Friday.
  let day = weekday;
  if (hour < 9 || (hour === 9 && minute < 15)) {
    day = (weekday + 6) % 7;
  }
  if (day === 6) {
    candidate = addDays(candidate, -1);
  } else if (day === 0) {
    candidate = addDays(candidate, -2);
  }

  return candidate;
}

function parseYMD(value: string): { year: number; month: number; day: number } {
  const [year, month, day] = value.split("-").map(Number);
  return { year, month, day };
}

function formatYMD(parts: { year: number; month: number; day: number }): string {
  const yyyy = String(parts.year).padStart(4, "0");
  const mm = String(parts.month).padStart(2, "0");
  const dd = String(parts.day).padStart(2, "0");
  return `${yyyy}-${mm}-${dd}`;
}

function ymdToUtcDate(ymd: string): Date {
  const { year, month, day } = parseYMD(ymd);
  return new Date(Date.UTC(year, month - 1, day));
}

function utcDateToYmd(date: Date): string {
  return formatYMD({
    year: date.getUTCFullYear(),
    month: date.getUTCMonth() + 1,
    day: date.getUTCDate(),
  });
}

export function addDays(ymd: string, delta: number): string {
  const date = ymdToUtcDate(ymd);
  date.setUTCDate(date.getUTCDate() + delta);
  return utcDateToYmd(date);
}

function addMonths(ymd: string, delta: number): string {
  const { year, month, day } = parseYMD(ymd);
  const base = new Date(Date.UTC(year, month - 1 + delta, 1));
  const targetYear = base.getUTCFullYear();
  const targetMonth = base.getUTCMonth();
  const lastDay = new Date(Date.UTC(targetYear, targetMonth + 1, 0)).getUTCDate();
  const safeDay = Math.min(day, lastDay);
  return utcDateToYmd(new Date(Date.UTC(targetYear, targetMonth, safeDay)));
}

function addYears(ymd: string, delta: number): string {
  const { year, month, day } = parseYMD(ymd);
  const targetYear = year + delta;
  const lastDay = new Date(Date.UTC(targetYear, month, 0)).getUTCDate();
  const safeDay = Math.min(day, lastDay);
  return utcDateToYmd(new Date(Date.UTC(targetYear, month - 1, safeDay)));
}

export function diffDaysInclusive(fromDate: string, toDate: string): number {
  const start = ymdToUtcDate(fromDate);
  const end = ymdToUtcDate(toDate);
  const diffMs = end.getTime() - start.getTime();
  return Math.floor(diffMs / 86400000) + 1;
}

function getMaxRangeDays(unit: CandleUnit, interval: number): number | null {
  if (unit === "minutes") {
    return interval <= 15 ? 31 : 92;
  }
  if (unit === "hours") {
    return 92;
  }
  if (unit === "days") {
    return 3650;
  }
  return null;
}

export function getMinAvailableDateForUnit(unit: CandleUnit): string {
  return unit === "minutes" || unit === "hours" ? "2022-01-01" : "2000-01-01";
}

/**
 * Returns the optimal calendar-day chunk size for a single load-more request,
 * sized to the active timeframe. Larger chunks mean fewer API calls for the
 * same depth of history, keeping request counts well inside Upstox rate limits.
 *
 * All values stay within Upstox V3 per-request caps:
 *   minutes ≤15 → 31 day max  |  minutes >15 → 92 day max
 *   hours        → 92 day max  |  days/weeks/months → no hard cap
 */
export function getLoadMoreChunkDays(unit: CandleUnit, interval: number): number {
  switch (unit) {
    case "minutes":
      if (interval <= 5)  return 15;  // 1m–5m:   ~10 trading days per fetch
      if (interval <= 15) return 21;  // 10m–15m: ~15 trading days per fetch
      if (interval <= 30) return 30;  // 30m:     ~21 trading days per fetch
      return 45;                       // 45m–240m: ~30 trading days per fetch
    case "hours":
      return 60;                       // 1H–4H:   ~42 trading days per fetch
    case "days":
      return 180;                      // daily:   6 months per fetch
    case "weeks":
      return 365;                      // weekly:  1 year per fetch
    case "months":
      return 730;                      // monthly: 2 years per fetch
    default:
      return 30;
  }
}

export function clampRange(
  unit: CandleUnit,
  interval: number,
  fromDate: string,
  toDate: string,
): { fromDate: string; toDate: string } {
  const today = getISTTodayYMD();
  let nextFrom = fromDate;
  const nextTo = toDate > today ? today : toDate;

  if (nextFrom > nextTo) {
    nextFrom = nextTo;
  }

  const minAvailable = getMinAvailableDateForUnit(unit);
  if (nextFrom < minAvailable) {
    nextFrom = minAvailable;
  }

  const maxDays = getMaxRangeDays(unit, interval);
  if (maxDays) {
    const currentRange = diffDaysInclusive(nextFrom, nextTo);
    if (currentRange > maxDays) {
      nextFrom = addDays(nextTo, -(maxDays - 1));
    }
  }

  if (nextFrom > nextTo) {
    nextFrom = nextTo;
  }

  return { fromDate: nextFrom, toDate: nextTo };
}

function getPresetRange(preset: RangePreset, today: string): { fromDate: string; toDate: string } {
  let fromDate = today;

  if (preset.kind === "days") {
    fromDate = addDays(today, -(preset.value - 1));
  } else if (preset.kind === "months") {
    fromDate = addMonths(today, -preset.value);
  } else if (preset.kind === "years") {
    fromDate = addYears(today, -preset.value);
  } else if (preset.kind === "ytd") {
    const { year } = parseYMD(today);
    fromDate = `${year}-01-01`;
  } else if (preset.kind === "all") {
    fromDate = getMinAvailableDateForUnit(preset.recommended.unit);
  }

  return { fromDate, toDate: today };
}

export function applyPreset(preset: RangePreset, referenceDay?: string): HistoricalState {
  const today = referenceDay ?? getISTTodayYMD();
  const presetRange = getPresetRange(preset, today);
  const range = clampRange(
    preset.recommended.unit,
    preset.recommended.interval,
    presetRange.fromDate,
    presetRange.toDate,
  );

  return {
    unit: preset.recommended.unit,
    interval: preset.recommended.interval,
    fromDate: range.fromDate,
    toDate: range.toDate,
    presetId: preset.id,
    isCustom: false,
  };
}

export function getDefaultHistoricalState(referenceDay?: string): HistoricalState {
  const defaultTradingDay = referenceDay ?? getLatestTradingDayYMD();
  return {
    unit: "minutes",
    interval: 1,
    fromDate: defaultTradingDay,
    toDate: defaultTradingDay,
    presetId: DEFAULT_HISTORICAL_PRESET_ID,
    isCustom: false,
  };
}

export function shouldAutoUpgradeInterval(unit: CandleUnit, rangeDays: number): boolean {
  return rangeDays > INTRADAY_RANGE_MAX_DAYS && (unit === "minutes" || unit === "hours");
}

export function shouldApplyAutoUpgrade(
  unit: CandleUnit,
  rangeDays: number,
  isCandleSizeLocked: boolean
): boolean {
  if (isCandleSizeLocked) return false;
  return shouldAutoUpgradeInterval(unit, rangeDays);
}

function includesToday(fromDate: string, toDate: string): boolean {
  const today = getISTTodayYMD();
  return fromDate <= today && toDate >= today;
}

export function includesTodayOrYesterday(fromDate: string, toDate: string): boolean {
  const today = getISTTodayYMD();
  const yesterday = addDays(today, -1);
  return fromDate <= today && toDate >= yesterday;
}

/**
 * Calculate adjusted date range when switching timeframes.
 * Preserves the center date and adjusts range based on new timeframe's default.
 * 
 * @param centerDate - The date to keep centered (typically current visible center)
 * @param newRangeDays - Number of days for the new timeframe
 * @returns Adjusted from/to dates
 */
export function adjustDateRangeForTimeframe(
  centerDate: string,
  newRangeDays: number
): { fromDate: string; toDate: string } {
  const today = getISTTodayYMD();
  const halfRange = Math.floor(newRangeDays / 2);
  
  // Calculate ideal range centered on centerDate
  let fromDate = addDays(centerDate, -halfRange);
  let toDate = addDays(centerDate, halfRange);
  
  // Clamp to not exceed today
  if (toDate > today) {
    const overflow = diffDaysInclusive(today, toDate) - 1;
    toDate = today;
    fromDate = addDays(fromDate, -overflow);
  }
  
  return { fromDate, toDate };
}

export function shouldUseIntradaySource(
  rangeDays: number,
  unit: CandleUnit,
  fromDate: string,
  toDate: string
): boolean {
  return (
    rangeDays === 1 &&
    (unit === "minutes" || unit === "hours") &&
    includesToday(fromDate, toDate)
  );
}

export function shouldUseHybridSource(
  rangeDays: number,
  unit: CandleUnit,
  fromDate: string,
  toDate: string
): boolean {
  return (
    rangeDays > 1 &&
    rangeDays <= INTRADAY_RANGE_MAX_DAYS &&
    (unit === "minutes" || unit === "hours") &&
    includesToday(fromDate, toDate)
  );
}

export function resolveCandleSourceMode(
  rangeDays: number,
  unit: CandleUnit,
  fromDate: string,
  toDate: string
): CandleSourceMode {
  if (shouldUseIntradaySource(rangeDays, unit, fromDate, toDate)) {
    return "intraday";
  }
  if (shouldUseHybridSource(rangeDays, unit, fromDate, toDate)) {
    return "hybrid";
  }
  return "historical";
}

export type MarketSession = "pre-market" | "open" | "post-market";

/** Returns the current NSE market session based on IST wall-clock time. */
export function getISTMarketSession(): MarketSession {
  const { hour, minute } = getISTNowParts();
  if (hour < 9 || (hour === 9 && minute < 15)) return "pre-market";
  if (hour < 15 || (hour === 15 && minute <= 30)) return "open";
  return "post-market";
}

/**
 * Returns a human-readable explanation for why no candle data is available.
 * Accounts for pre-market, live-market, and post-market windows.
 */
export function getNoDataMessage(
  tradingSymbol: string,
  fromDate: string,
  toDate: string
): string {
  const session = getISTMarketSession();
  const dateRange = fromDate === toDate ? fromDate : `${fromDate} to ${toDate}`;

  if (session === "pre-market") {
    return (
      "Market hasn't opened yet (opens at 9:15 AM IST). Historical data for today will be " +
      "available after market close. Previous trading day's data may be shown if available."
    );
  }
  if (session === "open") {
    return (
      "Market is currently open. If you're seeing this message, there may be a data " +
      "ingestion delay or this instrument may not be actively trading today."
    );
  }
  if (session === "post-market") {
    return (
      "Market is closed. Today's data should be available shortly. If this persists, " +
      "the instrument may not have traded today or there's a data ingestion delay."
    );
  }
  return (
    `No market data found for ${tradingSymbol} on ${dateRange}. ` +
    "This could be a market holiday or data ingestion delay."
  );
}
