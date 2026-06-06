'use client';

/**
 * AI Explanation Panel
 * ====================
 * Full-width card that renders the LLM-generated plain-English explanation for
 * the latest active trade suggestion on an instrument.
 *
 * Three display states:
 *  1. Skeleton  — initial SSE load, or data.available === false (worker in progress)
 *  2. Content   — data.available === true, full narrative + source citations
 *  3. Hidden    — data === null (no active suggestion for this instrument)
 *
 * Source attribution:
 *  Sources are available on the real-time push path (Redis notification) and
 *  will be empty on the periodic poll fallback.  Inline citations within
 *  full_explanation text are always present regardless of sources array.
 *
 * Disclaimer:
 *  The regulatory disclaimer appended by the explanation worker is separated
 *  from the narrative text and rendered in its own styled box.
 */

import { memo } from 'react';
import { Brain, ExternalLink, AlertTriangle, Sparkles } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import type { ExplanationData, ExplanationSource } from '@/types/analysis';

// ── Disclaimer separator ───────────────────────────────────────────────────────

/** Sentinel that the explanation worker always appends. */
const DISCLAIMER_MARKER = '⚠';

/**
 * Split the full explanation text into the narrative body and the regulatory
 * disclaimer.  The worker appends the disclaimer with a leading "\n\n⚠ …".
 */
function splitExplanation(text: string): { body: string; disclaimer: string } {
  const idx = text.lastIndexOf(`\n\n${DISCLAIMER_MARKER}`);
  if (idx === -1) return { body: text.trim(), disclaimer: '' };
  return {
    body:       text.slice(0, idx).trim(),
    disclaimer: text.slice(idx).trim(),
  };
}

// ── Relative-time helper ───────────────────────────────────────────────────────

function formatRelativeTime(iso: string): string {
  try {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60_000);
    if (mins < 1)  return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24)  return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  } catch {
    return '';
  }
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function PanelSkeleton() {
  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="h-5 w-5 rounded bg-slate-200 animate-pulse" />
            <div className="h-5 w-40 rounded bg-slate-200 animate-pulse" />
          </div>
          <div className="h-4 w-28 rounded bg-slate-100 animate-pulse" />
        </div>
        <div className="h-4 w-56 rounded bg-slate-100 animate-pulse mt-1" />
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Narrative skeleton — 6 lines with natural shortening */}
        <div className="space-y-2" aria-label="Generating explanation" role="status" aria-busy="true">
          {([100, 97, 100, 92, 96, 70] as const).map((w, i) => (
            <div
              key={i}
              className="h-3 rounded bg-slate-100 animate-pulse"
              style={{ width: `${w}%` }}
            />
          ))}
        </div>

        {/* Generating indicator */}
        <div className="flex items-center gap-2 pt-2 text-xs text-slate-400">
          <Sparkles className="h-3.5 w-3.5 animate-pulse text-violet-400" />
          <span>Generating explanation from recent news…</span>
        </div>
      </CardContent>
    </Card>
  );
}

interface SourcesListProps {
  sources: ExplanationSource[];
}

function SourcesList({ sources }: SourcesListProps) {
  if (sources.length === 0) return null;

  return (
    <div className="space-y-1 pt-3 border-t border-slate-100">
      <p className="text-[10px] uppercase tracking-widest text-slate-400 font-medium mb-2">
        Sources
      </p>
      {sources.map((src, i) => (
        <div key={i} className="flex items-center gap-1.5 text-xs text-slate-500">
          <span className="font-medium text-slate-600 truncate max-w-[200px]">
            {src.source_name}
          </span>
          <span className="text-slate-300" aria-hidden>·</span>
          <span className="shrink-0">{formatRelativeTime(src.as_of)}</span>
          {src.source_url && (
            <a
              href={src.source_url}
              target="_blank"
              rel="noopener noreferrer"
              onClick={(e) => e.stopPropagation()}
              className="ml-auto shrink-0 text-slate-400 hover:text-slate-600 transition-colors"
              aria-label={`Open source: ${src.source_name}`}
            >
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
        </div>
      ))}
    </div>
  );
}

interface ContentProps {
  data: ExplanationData & { available: true; full_explanation: string };
}

function ExplanationContent({ data }: ContentProps) {
  const { body, disclaimer } = splitExplanation(data.full_explanation);

  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-lg">
          <span className="flex items-center gap-2">
            <Brain className="h-5 w-5 text-violet-500" />
            AI Explanation
          </span>
          {data.generated_at && (
            <span className="text-xs font-normal text-slate-400">
              {formatRelativeTime(data.generated_at)}
            </span>
          )}
        </CardTitle>
        {data.model && (
          <CardDescription className="text-xs">
            {data.model}
          </CardDescription>
        )}
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Narrative body */}
        <p className="text-sm text-slate-700 leading-relaxed whitespace-pre-line">
          {body}
        </p>

        {/* Source citations */}
        <SourcesList sources={data.sources} />

        {/* Regulatory disclaimer — always rendered in its own styled box */}
        {disclaimer && (
          <div className="flex gap-2 rounded-md border border-amber-100 bg-amber-50/70 px-3 py-2.5">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-500" />
            <p className="text-[11px] leading-relaxed text-amber-700">
              {disclaimer.replace(/^⚠\s*/, '')}
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Public component ───────────────────────────────────────────────────────────

interface AIExplanationPanelProps {
  data: ExplanationData | null;
  isLoading: boolean;
  className?: string;
}

function AIExplanationPanelComponent({
  data,
  isLoading,
  className,
}: AIExplanationPanelProps) {
  // Nothing to show — no active suggestion for this instrument.
  if (!isLoading && data === null) return null;

  // Initial SSE load before first event.
  if (isLoading && data === null) {
    return (
      <div className={cn(className)}>
        <PanelSkeleton />
      </div>
    );
  }

  // Active suggestion exists but explanation is still being generated.
  if (data !== null && !data.available) {
    return (
      <div className={cn(className)}>
        <PanelSkeleton />
      </div>
    );
  }

  // Explanation is ready — full_explanation must be a non-empty string here.
  if (data !== null && data.available && data.full_explanation) {
    return (
      <div className={cn(className)}>
        <ExplanationContent
          data={data as ExplanationData & { available: true; full_explanation: string }}
        />
      </div>
    );
  }

  return null;
}

export const AIExplanationPanel = memo(AIExplanationPanelComponent);
