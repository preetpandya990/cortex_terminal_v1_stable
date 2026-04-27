'use client';

import { useEffect, useRef, useState } from 'react';
import { BarChart2, Check, ChevronDown, X } from 'lucide-react';
import { cn } from '@/lib/utils';
import {
  INDICATOR_META,
  OVERLAY_INDICATORS,
  OSCILLATOR_INDICATORS,
  type IndicatorId,
} from '@/lib/indicators';
import { useChartPreferences } from '@/contexts/ChartPreferencesContext';

export function IndicatorSelector() {
  const { activeIndicators, toggleIndicator, isIndicatorActive } = useChartPreferences();
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Close on pointer-down outside
  useEffect(() => {
    if (!open) return;
    const handler = (e: PointerEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    window.addEventListener('pointerdown', handler);
    return () => window.removeEventListener('pointerdown', handler);
  }, [open]);

  // Close on Escape
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open]);

  const activeCount = activeIndicators.length;
  const hasActive   = activeCount > 0;

  // React 18 auto-batches these into a single re-render
  const handleClearAll = () => {
    [...activeIndicators].forEach(id => toggleIndicator(id));
  };

  return (
    <div ref={containerRef} className="relative shrink-0">

      {/* ── Trigger button ─────────────────────────────────────────────────── */}
      <button
        type="button"
        onClick={() => setOpen(v => !v)}
        className={cn(
          'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium',
          'transition-all duration-150 select-none',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1',
          open || hasActive
            ? 'bg-blue-600 text-white hover:bg-blue-700'
            : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900',
        )}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="Toggle chart indicators"
      >
        <BarChart2 className="h-3.5 w-3.5 shrink-0" />
        <span>Indicators</span>

        {hasActive && (
          <span className="flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-white/25 px-1 text-[10px] font-bold tabular-nums leading-none">
            {activeCount}
          </span>
        )}

        <ChevronDown
          className={cn(
            'h-3.5 w-3.5 shrink-0 transition-transform duration-200',
            open && 'rotate-180',
          )}
        />
      </button>

      {/* ── Dropdown panel ─────────────────────────────────────────────────── */}
      {open && (
        <div
          role="listbox"
          aria-multiselectable="true"
          className={cn(
            'absolute right-0 top-full z-50 mt-2 w-56',
            'rounded-2xl border border-slate-200/80 bg-white',
            'shadow-xl shadow-slate-300/30',
            'animate-in fade-in-0 slide-in-from-top-2 duration-150',
          )}
        >
          {/* Panel header */}
          <div className="flex items-center justify-between border-b border-slate-100 px-3.5 py-2.5">
            <span className="text-[11px] font-semibold uppercase tracking-widest text-slate-400">
              Indicators
            </span>
            {hasActive && (
              <button
                type="button"
                onClick={handleClearAll}
                className={cn(
                  'flex items-center gap-1 rounded-md px-1.5 py-0.5',
                  'text-[11px] font-medium text-slate-400',
                  'hover:bg-red-50 hover:text-red-500 transition-colors duration-100',
                )}
                aria-label="Clear all indicators"
              >
                <X className="h-3 w-3" />
                Clear all
              </button>
            )}
          </div>

          {/* Overlays group */}
          <IndicatorGroup
            title="Overlays"
            ids={OVERLAY_INDICATORS}
            isActive={isIndicatorActive}
            onToggle={toggleIndicator}
          />

          <div className="mx-3.5 border-t border-slate-100" />

          {/* Oscillators group */}
          <IndicatorGroup
            title="Oscillators"
            ids={OSCILLATOR_INDICATORS}
            isActive={isIndicatorActive}
            onToggle={toggleIndicator}
          />

          <div className="pb-1.5" />
        </div>
      )}
    </div>
  );
}

// ─── Internal sub-component ───────────────────────────────────────────────────

interface IndicatorGroupProps {
  title: string;
  ids: IndicatorId[];
  isActive: (id: IndicatorId) => boolean;
  onToggle: (id: IndicatorId) => void;
}

function IndicatorGroup({ title, ids, isActive, onToggle }: IndicatorGroupProps) {
  return (
    <div className="py-1.5">
      <p className="px-3.5 pb-1 pt-1.5 text-[10px] font-semibold uppercase tracking-widest text-slate-400">
        {title}
      </p>
      {ids.map(id => {
        const meta   = INDICATOR_META[id];
        const active = isActive(id);
        return (
          <button
            key={id}
            type="button"
            role="option"
            aria-selected={active}
            onClick={() => onToggle(id)}
            className={cn(
              'flex w-full items-center gap-2.5 px-3.5 py-1.5 text-left text-sm',
              'transition-colors duration-100 hover:bg-slate-50',
              active ? 'text-slate-900 font-medium' : 'text-slate-500',
            )}
          >
            {/* Color dot — filled + slightly larger when active */}
            <span
              className={cn(
                'h-2 w-2 shrink-0 rounded-full transition-all duration-150',
                active ? 'scale-110' : 'bg-slate-200',
              )}
              style={active ? { backgroundColor: meta.color } : undefined}
            />

            <span className="flex-1 truncate">{meta.label}</span>

            {/* Check mark — visible when active */}
            <Check
              className={cn(
                'h-3.5 w-3.5 shrink-0 text-blue-600 transition-all duration-150',
                active ? 'opacity-100 scale-100' : 'opacity-0 scale-75',
              )}
            />
          </button>
        );
      })}
    </div>
  );
}
