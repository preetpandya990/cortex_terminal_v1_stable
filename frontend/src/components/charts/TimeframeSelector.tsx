'use client';

import { TIMEFRAME_OPTIONS, type TimeframeOption } from '@/contexts/ChartPreferencesContext';
import { cn } from '@/lib/utils';

type TimeframeSelectorProps = {
  value: TimeframeOption;
  onChange: (timeframe: TimeframeOption) => void;
  className?: string;
};

export function TimeframeSelector({ value, onChange, className }: TimeframeSelectorProps) {
  return (
    <div className={cn('flex items-center gap-1', className)}>
      {TIMEFRAME_OPTIONS.map((timeframe) => {
        const isActive = timeframe.id === value.id;
        return (
          <button
            key={timeframe.id}
            onClick={() => onChange(timeframe)}
            className={cn(
              'px-3 py-1.5 text-sm font-medium rounded-md transition-colors',
              'hover:bg-slate-100 dark:hover:bg-slate-800',
              'focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2',
              isActive && 'bg-blue-600 text-white hover:bg-blue-700 dark:hover:bg-blue-700'
            )}
            aria-label={`Switch to ${timeframe.label} timeframe`}
            aria-pressed={isActive}
          >
            {timeframe.label}
          </button>
        );
      })}
    </div>
  );
}
