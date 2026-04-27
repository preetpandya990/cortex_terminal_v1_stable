'use client';

import { createContext, useContext, useState, useEffect, useCallback, ReactNode } from 'react';
import type { CandleUnit } from '@/lib/chart-policy';
import type { IndicatorId } from '@/lib/indicators';

export type TimeframeOption = {
  id: string;
  label: string;
  unit: CandleUnit;
  interval: number;
  defaultRangeDays: number;
};

export const TIMEFRAME_OPTIONS: TimeframeOption[] = [
  { id: '1m',  label: '1m',  unit: 'minutes', interval: 1,   defaultRangeDays: 3   },
  { id: '5m',  label: '5m',  unit: 'minutes', interval: 5,   defaultRangeDays: 5   },
  { id: '15m', label: '15m', unit: 'minutes', interval: 15,  defaultRangeDays: 10  },
  { id: '30m', label: '30m', unit: 'minutes', interval: 30,  defaultRangeDays: 15  },
  { id: '1H',  label: '1H',  unit: 'minutes', interval: 60,  defaultRangeDays: 30  },
  { id: '4H',  label: '4H',  unit: 'minutes', interval: 240, defaultRangeDays: 90  },
  { id: '1D',  label: '1D',  unit: 'days',    interval: 1,   defaultRangeDays: 180 },
];

const DEFAULT_TIMEFRAME      = TIMEFRAME_OPTIONS[1]; // 5m
const STORAGE_KEY_TIMEFRAME  = 'chart_preferences_timeframe';
const STORAGE_KEY_INDICATORS = 'chart_preferences_indicators';

type ChartPreferencesContextType = {
  defaultTimeframe: TimeframeOption;
  setDefaultTimeframe: (timeframe: TimeframeOption) => void;
  /** Ordered set of currently enabled indicator IDs. Empty = all off. */
  activeIndicators: IndicatorId[];
  /** Toggle a single indicator on or off. Persisted to localStorage. */
  toggleIndicator: (id: IndicatorId) => void;
  isIndicatorActive: (id: IndicatorId) => boolean;
};

const ChartPreferencesContext = createContext<ChartPreferencesContextType | undefined>(undefined);

export function ChartPreferencesProvider({ children }: { children: ReactNode }) {
  const [defaultTimeframe, setDefaultTimeframeState] = useState<TimeframeOption>(DEFAULT_TIMEFRAME);
  const [activeIndicators, setActiveIndicators] = useState<IndicatorId[]>([]);
  const [isHydrated, setIsHydrated] = useState(false);

  // Rehydrate from localStorage on mount.
  useEffect(() => {
    try {
      const storedTf = localStorage.getItem(STORAGE_KEY_TIMEFRAME);
      if (storedTf) {
        const parsed = JSON.parse(storedTf);
        const found  = TIMEFRAME_OPTIONS.find(tf => tf.id === parsed.id);
        if (found) setDefaultTimeframeState(found);
      }

      const storedIndicators = localStorage.getItem(STORAGE_KEY_INDICATORS);
      if (storedIndicators) {
        const parsed = JSON.parse(storedIndicators);
        if (Array.isArray(parsed)) {
          setActiveIndicators(parsed as IndicatorId[]);
        }
      }
    } catch {
      // Corrupt localStorage — silently fall back to defaults.
    } finally {
      setIsHydrated(true);
    }
  }, []);

  const setDefaultTimeframe = useCallback((timeframe: TimeframeOption) => {
    setDefaultTimeframeState(timeframe);
    try {
      localStorage.setItem(STORAGE_KEY_TIMEFRAME, JSON.stringify({ id: timeframe.id }));
    } catch {
      // localStorage unavailable (e.g., private browsing with restrictions).
    }
  }, []);

  const toggleIndicator = useCallback((id: IndicatorId) => {
    setActiveIndicators(prev => {
      const next = prev.includes(id)
        ? prev.filter(i => i !== id)
        : [...prev, id];
      try {
        localStorage.setItem(STORAGE_KEY_INDICATORS, JSON.stringify(next));
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  const isIndicatorActive = useCallback(
    (id: IndicatorId) => activeIndicators.includes(id),
    [activeIndicators],
  );

  // Block rendering until client-side state is loaded to prevent hydration mismatches.
  if (!isHydrated) return null;

  return (
    <ChartPreferencesContext.Provider
      value={{ defaultTimeframe, setDefaultTimeframe, activeIndicators, toggleIndicator, isIndicatorActive }}
    >
      {children}
    </ChartPreferencesContext.Provider>
  );
}

export function useChartPreferences() {
  const context = useContext(ChartPreferencesContext);
  if (!context) {
    throw new Error('useChartPreferences must be used within ChartPreferencesProvider');
  }
  return context;
}
