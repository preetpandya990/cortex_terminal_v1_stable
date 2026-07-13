'use client';

/**
 * AI Explanation Panel
 * ====================
 * Full-width card that renders the LLM-generated plain-English explanation for
 * the current instrument.  Works for both active trade suggestions and Watchlist
 * items with no current signal.
 *
 * Display states:
 *  1. Skeleton     — initial SSE load, or data.available === false (worker generating)
 *  2. Weak signal  — data.weak_signal === true; consensus_score below threshold.
 *                    Renders score context + "Request AI Explanation" CTA button.
 *                    Transitions to Content when the bypass job completes (via SSE push).
 *  3. Content      — data.available === true; full narrative + optional staleness
 *                    banner + source citations + regulatory disclaimer
 *  4. (never hidden) — the SSE 3-stage lookup always returns a payload; the
 *                    panel only hides if the parent passes data === null explicitly
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
 *
 * Reveal animation:
 *  The narrative is revealed with a simulated typewriter effect (useTypewriter)
 *  since it arrives as a single complete string rather than true token
 *  streaming. Section headings appear instantly; only body prose animates.
 *  Sources and the disclaimer are held back until typing finishes so nothing
 *  appears ahead of the text it supports. Skips animation for users who
 *  prefer reduced motion, and for content already revealed earlier this
 *  session.
 */

import { memo, useState, useCallback, useMemo } from 'react';
import { Brain, Clock, ExternalLink, AlertTriangle, Sparkles, Lightbulb, FlaskConical } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import { sanitizeUrl } from '@/lib/sanitizeUrl';
import { useTypewriter } from '@/hooks/useTypewriter';
import type { ExplanationData, ExplanationSource } from '@/types/analysis';

// Age past which a suggestion explanation earns the "Based on … · Xh ago"
// staleness banner. Below this the signal is fresh enough that the banner is
// noise; the banner is informational, the direction-mismatch banner (backend-
// computed) is the strong contradiction signal.
const STALENESS_BANNER_AFTER_MS = 60 * 60 * 1000; // 1 hour

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

/** Sentinel the explanation worker appends to suggested_action — deliberately
 *  distinct from DISCLAIMER_MARKER so the two disclaimers are independently
 *  splittable and visually distinguishable. */
const SUGGESTED_ACTION_DISCLAIMER_MARKER = '🧪';

/** Same split mechanics as splitExplanation, independent sentinel. */
function splitSuggestedAction(text: string): { body: string; disclaimer: string } {
  const idx = text.lastIndexOf(`\n\n${SUGGESTED_ACTION_DISCLAIMER_MARKER}`);
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

interface PanelFailedProps {
  /** When provided (data carries a suggestion_id), renders a retry button
      that re-fires the bypass endpoint — the WS7 failed state is terminal
      only until the user asks again. */
  onRetry?:   () => void;
  retrying?:  boolean;
}

/**
 * Rendered when the explanation pipeline exhausted all retries and moved the
 * job to the dead-letter queue (``data.failed === true`` / status "failed").
 * Shows a static non-animated state so the user is not left with an eternal
 * skeleton, plus a retry CTA when the backing suggestion is known.
 */
function PanelFailed({ onRetry, retrying = false }: PanelFailedProps) {
  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base font-semibold text-slate-700">
          <Brain className="h-5 w-5 text-slate-400" />
          AI Analysis
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div
          className="flex flex-col gap-3 rounded-md border border-slate-200 bg-slate-50/80 px-4 py-3"
          role="status"
          aria-label="Analysis unavailable"
        >
          <div className="flex items-center gap-3">
            <AlertTriangle className="h-4 w-4 shrink-0 text-slate-400" />
            <p className="text-sm text-slate-500">
              Analysis unavailable for this signal.
            </p>
          </div>
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              disabled={retrying}
              aria-label="Retry AI explanation generation"
              aria-busy={retrying}
              className={cn(
                'self-start flex items-center gap-1.5 rounded-md px-3 py-1.5',
                'text-xs font-medium transition-colors',
                'focus-visible:outline-none focus-visible:ring-2',
                'focus-visible:ring-violet-500 focus-visible:ring-offset-1',
                retrying
                  ? 'cursor-not-allowed bg-slate-100 text-slate-400'
                  : 'border border-violet-200 bg-violet-50 text-violet-700 hover:bg-violet-100 active:bg-violet-200',
              )}
            >
              <Sparkles className={cn('h-3.5 w-3.5', retrying && 'animate-pulse')} aria-hidden />
              {retrying ? 'Retrying…' : 'Retry Analysis'}
            </button>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ── Weak-signal card ──────────────────────────────────────────────────────────

interface PanelWeakSignalProps {
  data:       ExplanationData;
  onRefresh:  () => void;
  refreshing: boolean;
}

/**
 * Rendered when the suggestion's consensus_score is below
 * EXPLANATION_CONSENSUS_THRESHOLD and the explanation pipeline was intentionally
 * bypassed.  Shows score context and a user-driven CTA that fires the bypass
 * endpoint — the result arrives asynchronously via SSE push.
 */
function PanelWeakSignal({ data, onRefresh, refreshing }: PanelWeakSignalProps) {
  const score = data.consensus_score != null ? Math.round(data.consensus_score) : null;

  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base font-semibold text-slate-700">
          <Brain className="h-5 w-5 text-slate-400" />
          AI Analysis
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div
          className="flex flex-col gap-3 rounded-md border border-amber-100 bg-amber-50/60 px-4 py-3"
          role="status"
          aria-label="Signal confidence below AI analysis threshold"
        >
          <div className="flex items-start gap-3">
            <AlertTriangle
              className="mt-0.5 h-4 w-4 shrink-0 text-amber-500"
              aria-hidden
            />
            <div className="space-y-1">
              <p className="text-sm font-medium text-slate-700">
                Signal detected — confidence below AI analysis threshold
              </p>
              <p className="text-xs leading-relaxed text-slate-500">
                {score != null ? `Consensus score: ${score}/100. ` : ''}
                AI explanations are reserved for high-conviction signals. You can
                request a full explanation if this signal looks valid to you.
              </p>
            </div>
          </div>

          <button
            type="button"
            onClick={onRefresh}
            disabled={refreshing}
            aria-label="Request AI explanation for this signal"
            aria-busy={refreshing}
            className={cn(
              'self-start flex items-center gap-1.5 rounded-md px-3 py-1.5',
              'text-xs font-medium transition-colors',
              'focus-visible:outline-none focus-visible:ring-2',
              'focus-visible:ring-violet-500 focus-visible:ring-offset-1',
              refreshing
                ? 'cursor-not-allowed bg-slate-100 text-slate-400'
                : 'border border-violet-200 bg-violet-50 text-violet-700 hover:bg-violet-100 active:bg-violet-200',
            )}
          >
            <Sparkles
              className={cn('h-3.5 w-3.5', refreshing && 'animate-pulse')}
              aria-hidden
            />
            {refreshing ? 'Requesting…' : 'Request AI Explanation'}
          </button>
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
      {sources.map((src, i) => {
        // Scheme allowlist (WS8): source URLs are third-party RSS content —
        // anything but http(s) renders as plain text, never as an href.
        const safeUrl = sanitizeUrl(src.source_url);
        return (
          <div key={i} className="flex items-center gap-1.5 text-xs text-slate-500">
            <span className="font-medium text-slate-600 truncate max-w-[200px]">
              {src.source_name}
            </span>
            <span className="text-slate-300" aria-hidden>·</span>
            <span className="shrink-0">{formatRelativeTime(src.as_of)}</span>
            {safeUrl && (
              <a
                href={safeUrl}
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
        );
      })}
    </div>
  );
}

// ── Staleness banner ───────────────────────────────────────────────────────────

interface StalenessBannerProps {
  signalDirection: 'BUY' | 'SELL' | null;
  signalGeneratedAt: string | null;
}

/**
 * Amber banner shown when a suggestion explanation is based on an AGED
 * signal (older than STALENESS_BANNER_AFTER_MS) — informational ("Based on
 * BUY signal · 6h ago"). The previous condition was presence-based and fired
 * for every suggestion explanation regardless of age (WS8 fix).
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

/**
 * Blinking caret rendered at the point where text is actively being typed.
 * Purely decorative (`aria-hidden`) — the underlying text node already
 * carries the real content for assistive tech.
 */
function TypingCursor() {
  return (
    <span
      aria-hidden
      className="ml-0.5 inline-block h-[1em] w-[2px] -mb-[0.15em] animate-pulse bg-violet-400 align-middle"
    />
  );
}

// ── Suggested Action callout ───────────────────────────────────────────────────

interface SuggestedActionCalloutProps {
  action: string;
}

/**
 * Visually distinct callout for the learning-phase suggested_action field —
 * deliberately NOT blended into the 5-section narrative list above it, and
 * styled differently (indigo, not amber) from the regulatory disclaimer box
 * so the two warnings read as textually and visually separate concerns: one
 * is a standing legal notice, the other is a live caveat about this specific,
 * experimental suggestion.
 */
function SuggestedActionCallout({ action }: SuggestedActionCalloutProps) {
  const { body, disclaimer } = useMemo(() => splitSuggestedAction(action), [action]);
  if (!body) return null;

  return (
    <div
      className="animate-in fade-in duration-300 space-y-2 rounded-lg border border-indigo-200 bg-gradient-to-br from-indigo-50 to-violet-50 px-4 py-3"
      role="note"
      aria-label="Suggested action"
    >
      <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-indigo-700">
        <Lightbulb className="h-3.5 w-3.5" aria-hidden />
        Suggested Action
      </div>
      <p className="text-sm leading-relaxed text-indigo-900 whitespace-pre-line">
        {body}
      </p>
      {disclaimer && (
        <div className="flex gap-2 rounded-md border border-indigo-200 bg-white/70 px-3 py-2">
          <FlaskConical className="mt-0.5 h-3.5 w-3.5 shrink-0 text-indigo-500" aria-hidden />
          <p className="text-[11px] leading-relaxed text-indigo-600">
            {disclaimer.replace(new RegExp(`^${SUGGESTED_ACTION_DISCLAIMER_MARKER}\\s*`), '')}
          </p>
        </div>
      )}
    </div>
  );
}

interface ContentProps {
  data: ExplanationData & { full_explanation: string };
}

function ExplanationContent({ data }: ContentProps) {
  // Parsing runs only when the underlying text actually changes — not on
  // every animation frame, which would otherwise re-run these regexes up
  // to 60x/sec while the typewriter is revealing text.
  const { body, disclaimer } = useMemo(
    () => splitExplanation(data.full_explanation),
    [data.full_explanation],
  );
  const sections = useMemo(() => parseSections(body), [body]);

  // Normalize to a uniform block list so sectioned and legacy (single-block)
  // narratives share one rendering + typewriter-offset path.
  const blocks = useMemo(
    () => sections ?? [{ heading: '', body }],
    [sections, body],
  );
  const blockOffsets = useMemo(
    () => blocks.map((_, i) => blocks.slice(0, i).reduce((sum, b) => sum + b.body.length, 0)),
    [blocks],
  );
  // Headings render instantly when reached; only body prose is typed out,
  // so the concatenated target excludes heading text.
  const typewriterTarget = useMemo(() => blocks.map((b) => b.body).join(''), [blocks]);
  const { revealedLength, isComplete: isTypingComplete } = useTypewriter(typewriterTarget);

  // Streaming: text is still flowing in (available flips true on the final event).
  const isStreaming = !data.available;
  const isMarketContext = data.context_type === 'instrument_context';
  // Age-based (WS8): banner only when the underlying signal is genuinely old —
  // the old presence-based condition fired for every suggestion explanation.
  const signalAgeMs = data.signal_generated_at
    ? Date.now() - new Date(data.signal_generated_at).getTime()
    : null;
  const showStalenessBanner =
    data.context_type === 'suggestion_explanation' &&
    signalAgeMs !== null &&
    signalAgeMs > STALENESS_BANNER_AFTER_MS;
  // Backend-computed contradiction flag — the strong banner, independent of age.
  const showMismatchBanner = data.direction_mismatch === true;

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
        {/* Direction-mismatch banner (Proposal B) — the live prediction now
            contradicts the direction this narrative was written for. Rendered
            ALONGSIDE (not replacing) the age banner. */}
        {showMismatchBanner && (
          <div
            className="flex items-center gap-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs"
            role="alert"
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-red-500" aria-hidden />
            <span className="font-medium text-red-700">
              Signal has changed since this explanation was generated — the live
              prediction now points the other way.
            </span>
          </div>
        )}

        {/* Staleness indicator — shown when the underlying signal has aged */}
        {showStalenessBanner && (
          <StalenessBanner
            signalDirection={data.signal_direction}
            signalGeneratedAt={data.signal_generated_at}
          />
        )}

        {/* Narrative body — rendered as labeled sections when the worker emits
            the "### " sectioned format, else as a single block (legacy rows).
            Body prose is revealed via the typewriter watermark; headings
            appear as soon as their block is reached. */}
        <div className="space-y-3">
          {blocks.map((block, i) => {
            const startOffset = blockOffsets[i];
            const hasStarted = revealedLength > startOffset;
            const visibleBody = block.body.slice(
              0,
              Math.max(0, Math.min(revealedLength - startOffset, block.body.length)),
            );
            const isActiveBlock = !isTypingComplete && hasStarted && visibleBody.length < block.body.length;

            if (!hasStarted && startOffset > 0) return null;

            return (
              <div key={i} className="space-y-1">
                {block.heading && (
                  <h4 className="text-[11px] font-semibold uppercase tracking-wide text-violet-700/80">
                    {block.heading}
                  </h4>
                )}
                <p className="text-sm text-slate-700 leading-relaxed whitespace-pre-line">
                  {visibleBody}
                  {isActiveBlock && <TypingCursor />}
                </p>
              </div>
            );
          })}
        </div>

        {/* Suggested Action — learning-phase feature, gated server-side. Held
            back until the narrative finishes typing, same reveal timing as
            sources/disclaimer below, so it never appears ahead of the text
            it's an addendum to. */}
        {isTypingComplete && data.suggested_action && (
          <SuggestedActionCallout action={data.suggested_action} />
        )}

        {/* Source citations — held back until the narrative finishes typing
            so citations don't appear ahead of the text they support. */}
        {isTypingComplete && <SourcesList sources={data.sources} />}

        {/* Regulatory disclaimer — always rendered in its own styled box,
            once the narrative has finished typing. */}
        {isTypingComplete && disclaimer && (
          <div className="flex gap-2 rounded-md border border-amber-100 bg-amber-50/70 px-3 py-2.5 animate-in fade-in duration-300">
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
  data:      ExplanationData | null;
  isLoading: boolean;
  className?: string;
  /**
   * Called when the user clicks "Request AI Explanation" on the weak-signal card.
   * The parent constructs this callback (capturing the access token and suggestion_id)
   * and passes it only when ``data.weak_signal === true``.  Passing ``undefined``
   * suppresses the CTA so the panel is a pure presentation component with no
   * direct auth or network dependencies.
   */
  onRequestExplanation?: () => Promise<void>;
}

function AIExplanationPanelComponent({
  data,
  isLoading,
  className,
  onRequestExplanation,
}: AIExplanationPanelProps) {
  const [refreshing, setRefreshing] = useState(false);

  const handleWeakSignalRefresh = useCallback(async () => {
    if (!onRequestExplanation || refreshing) return;
    setRefreshing(true);
    try {
      await onRequestExplanation();
    } catch {
      // Non-fatal: the backend deletes the debounce key on all error paths,
      // so the user can retry immediately.  The button resets to its idle state.
    } finally {
      setRefreshing(false);
    }
  }, [onRequestExplanation, refreshing]);

  // Nothing to show — no active suggestion for this instrument.
  if (!isLoading && data === null) return null;

  // Initial SSE load before first event.
  if (isLoading && data === null) {
    return <div className={cn(className)}><PanelSkeleton /></div>;
  }

  // Permanent failure — explanation pipeline exhausted all retries (DLQ fired).
  // Render a static "unavailable" state so the user is not left with an eternal
  // skeleton; the retry CTA re-fires the bypass endpoint when the suggestion is known.
  if (data !== null && data.failed) {
    return (
      <div className={cn(className)}>
        <PanelFailed
          onRetry={onRequestExplanation ? handleWeakSignalRefresh : undefined}
          retrying={refreshing}
        />
      </div>
    );
  }

  // Weak signal — consensus_score below threshold; explanation was intentionally
  // skipped.  Render the CTA card so the user can request analysis on demand.
  // This check must precede the generic skeleton branch: weak_signal payloads
  // have available=false and full_explanation=null, matching the skeleton guard.
  if (data !== null && data.weak_signal) {
    return (
      <div className={cn(className)}>
        <PanelWeakSignal
          data={data}
          onRefresh={handleWeakSignalRefresh}
          refreshing={refreshing}
        />
      </div>
    );
  }

  // Active suggestion/context — no explanation text has arrived yet.
  // Show the generating skeleton until the first token streams in.
  if (data !== null && !data.available && !data.full_explanation) {
    return <div className={cn(className)}><PanelSkeleton /></div>;
  }

  // Streaming partial OR finished explanation.  ExplanationContent renders the
  // "Generating" affordance while data.available is still false (streaming phase).
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
