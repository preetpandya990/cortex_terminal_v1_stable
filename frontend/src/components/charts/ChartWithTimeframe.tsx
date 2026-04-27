'use client';

/**
 * ChartWithTimeframe - Reusable wrapper for charts with timeframe selector
 * 
 * This component provides a standardized pattern for adding timeframe selection
 * to any chart view. Use this as a template when implementing new chart pages.
 * 
 * Example usage:
 * ```tsx
 * <ChartWithTimeframe
 *   instrumentKey="NSE_EQ|INE123A01012"
 *   onDataFetch={(timeframe, dateRange) => fetchChartData(...)}
 *   renderChart={(data) => <CandlestickChart candles={data} ... />}
 * />
 * ```
 */

import { useState, useEffect } from 'react';
import { TimeframeSelector } from './TimeframeSelector';
import { useChartPreferences, type TimeframeOption } from '@/contexts/ChartPreferencesContext';
import { adjustDateRangeForTimeframe, getISTTodayYMD } from '@/lib/chart-policy';

type ChartWithTimeframeProps<T> = {
  instrumentKey: string;
  onDataFetch: (
    timeframe: TimeframeOption,
    dateRange: { fromDate: string; toDate: string }
  ) => Promise<T>;
  renderChart: (data: T | null, isLoading: boolean) => React.ReactNode;
  className?: string;
};

export function ChartWithTimeframe<T>({
  instrumentKey,
  onDataFetch,
  renderChart,
  className,
}: ChartWithTimeframeProps<T>) {
  const { defaultTimeframe } = useChartPreferences();
  const [currentTimeframe, setCurrentTimeframe] = useState<TimeframeOption>(defaultTimeframe);
  const [data, setData] = useState<T | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  // Fetch data when timeframe or instrument changes
  useEffect(() => {
    const fetchData = async () => {
      setIsLoading(true);
      try {
        const today = getISTTodayYMD();
        const dateRange = adjustDateRangeForTimeframe(today, currentTimeframe.defaultRangeDays);
        const result = await onDataFetch(currentTimeframe, dateRange);
        setData(result);
      } catch (error) {
        console.error('[ChartWithTimeframe] Fetch error:', error);
        setData(null);
      } finally {
        setIsLoading(false);
      }
    };

    fetchData();
  }, [instrumentKey, currentTimeframe, onDataFetch]);

  const handleTimeframeChange = (newTimeframe: TimeframeOption) => {
    setCurrentTimeframe(newTimeframe);
    setData(null); // Clear old data
  };

  return (
    <div className={className}>
      <TimeframeSelector value={currentTimeframe} onChange={handleTimeframeChange} />
      <div className="mt-3">{renderChart(data, isLoading)}</div>
    </div>
  );
}
