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
