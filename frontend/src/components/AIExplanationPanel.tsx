'use client';

/**
 * AI Explanation Panel
 * ====================
 * Full-width card that renders the LLM-generated plain-English explanation for
 * the current instrument.  Works for both active trade suggestions and Watchlist
 * items with no current signal.
 *
 * Display states:
 *  1. Skeleton  — initial SSE load, or data.available === false (worker generating)
 *  2. Content   — data.available === true, full narrative + optional staleness
 *                 banner + source citations + regulatory disclaimer
 *  3. (never hidden) — the SSE 3-stage lookup always returns a payload; the
 *                 panel only hides if the parent passes data === null explicitly
 *
 * Context types (data.context_type):
 *  'suggestion_explanation' → AI explanation of a specific ML signal.  If the
 *      signal is not currently active (expired/superseded), an amber staleness
 *      banner is rendered: "Based on BUY signal · 6h ago".
 *  'instrument_context'     → Market context for a Watchlist item with no
 *      recent signal.  No staleness banner; header copy is adjusted.
 *
 * Source attribution:
 *  Sources are available on the real-time push path (Redis notification) and
 *  will be empty on the periodic poll fallback.  Inline citations within
 *  full_explanation are always present regardless of sources array.
 *
 * Disclaimer:
 *  The regulatory disclaimer appended by the explanation worker is separated
 *  from the narrative text and rendered in its own styled box.
 */

import { memo } from 'react';
import { Brain, Clock, ExternalLink, AlertTriangle, Sparkles } from 'lucide-react';
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

// ── Sectioned-narrative parser ───────────────────────────────────────────────

/** A labeled section of the explanation body. `heading` is '' for any intro text. */
interface ExplanationSection {
  heading: string;
  body:    string;
}

/**
 * Parse the explanation body into the worker's fixed "### " sections
 * (What the models saw / Technical picture / News context / What this suggests /
 * Key risks).  Returns null when no "### " header is present so the caller can
 * fall back to rendering the text as a single block — this keeps legacy rows
 * (generated before the sectioned format) and non-compliant output readable.
 */
function parseSections(text: string): ExplanationSection[] | null {
  if (!/^###\s+/m.test(text)) return null;

  const sections: ExplanationSection[] = [];
  let current: ExplanationSection = { heading: '', body: '' };

  for (const line of text.split('\n')) {
    const match = /^###\s+(.*)$/.exec(line.trim());
    if (match) {
      if (current.heading || current.body.trim()) sections.push(current);
      current = { heading: match[1].trim(), body: '' };
    } else {
      current.body += (current.body ? '\n' : '') + line;
    }
  }
  if (current.heading || current.body.trim()) sections.push(current);

  return sections
    .map((s) => ({ heading: s.heading, body: s.body.trim() }))
    .filter((s) => s.heading || s.body);
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

// ── Staleness banner ───────────────────────────────────────────────────────────

interface StalenessBannerProps {
  signalDirection: 'BUY' | 'SELL' | null;
  signalGeneratedAt: string | null;
}

/**
 * Amber banner shown when the explanation is derived from a non-active
 * (expired or superseded) trade suggestion.
 *
 * Only rendered when context_type === 'suggestion_explanation' AND the
 * signal's generated_at differs from the explanation's generated_at —
 * indicating the signal is not the current live signal.
 */
function StalenessBanner({ signalDirection, signalGeneratedAt }: StalenessBannerProps) {
  if (!signalDirection || !signalGeneratedAt) return null;

  const dirColor =
    signalDirection === 'BUY'
      ? 'text-emerald-700 bg-emerald-100'
      : 'text-red-700 bg-red-100';

  return (
    <div className="flex items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs">
      <Clock className="h-3.5 w-3.5 shrink-0 text-amber-500" />
      <span className="text-amber-700">
        Based on{' '}
        <span className={`inline-flex items-center rounded px-1.5 py-0.5 font-semibold ${dirColor}`}>
          {signalDirection}
        </span>{' '}
        signal · {formatRelativeTime(signalGeneratedAt)}
      </span>
    </div>
  );
}

interface ContentProps {
  data: ExplanationData & { full_explanation: string };
}

function ExplanationContent({ data }: ContentProps) {
  const { body, disclaimer } = splitExplanation(data.full_explanation);
  const sections = parseSections(body);

  // Streaming: text is still flowing in (available flips true on the final event).
  const isStreaming = !data.available;
  const isMarketContext = data.context_type === 'instrument_context';
  const showStalenessBanner =
    data.context_type === 'suggestion_explanation' && !!data.signal_generated_at;

  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-lg">
          <span className="flex items-center gap-2">
            <Brain className="h-5 w-5 text-violet-500" />
            {isMarketContext ? 'Market Context' : 'AI Explanation'}
          </span>
          {isStreaming ? (
            <span className="flex items-center gap-1.5 text-xs font-normal text-violet-500">
              <Sparkles className="h-3.5 w-3.5 animate-pulse" />
              Generating
            </span>
          ) : data.generated_at ? (
            <span className="text-xs font-normal text-slate-400">
              {formatRelativeTime(data.generated_at)}
            </span>
          ) : null}
        </CardTitle>
        {data.model && (
          <CardDescription className="text-xs">
            {data.model}
          </CardDescription>
        )}
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Staleness indicator — shown for non-active suggestion explanations */}
        {showStalenessBanner && (
          <StalenessBanner
            signalDirection={data.signal_direction}
            signalGeneratedAt={data.signal_generated_at}
          />
        )}

        {/* Narrative body — rendered as labeled sections when the worker emits
            the "### " sectioned format, else as a single block (legacy rows). */}
        {sections ? (
          <div className="space-y-3">
            {sections.map((section, i) => (
              <div key={i} className="space-y-1">
                {section.heading && (
                  <h4 className="text-[11px] font-semibold uppercase tracking-wide text-violet-700/80">
                    {section.heading}
                  </h4>
                )}
                <p className="text-sm text-slate-700 leading-relaxed whitespace-pre-line">
                  {section.body}
                </p>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-slate-700 leading-relaxed whitespace-pre-line">
            {body}
          </p>
        )}

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

  // Active suggestion/context exists but no explanation text has streamed yet —
  // show the skeleton until the first token arrives.
  if (data !== null && !data.available && !data.full_explanation) {
    return (
      <div className={cn(className)}>
        <PanelSkeleton />
      </div>
    );
  }

  // Streaming partial OR finished explanation — render the content (the
  // "Generating" affordance is shown while data.available is still false).
  if (data !== null && data.full_explanation) {
    return (
      <div className={cn(className)}>
        <ExplanationContent
          data={data as ExplanationData & { full_explanation: string }}
        />
      </div>
    );
  }

  return null;
}

export const AIExplanationPanel = memo(AIExplanationPanelComponent);
