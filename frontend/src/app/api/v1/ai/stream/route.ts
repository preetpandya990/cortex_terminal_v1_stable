// PATH: frontend/src/app/api/v1/ai/stream/route.ts
// ─────────────────────────────────────────────────
/**
 * SSE pass-through proxy for GET /api/v1/ai/stream.
 *
 * Why this exists:
 *   The catch-all /api/v1/[...path] proxy buffers responses and cannot forward
 *   text/event-stream bodies. This static route takes precedence and streams
 *   the backend SSE response to the browser with zero buffering.
 *
 * Auth:
 *   The browser EventSource API cannot send custom headers, so the JWT is
 *   passed as the `token` query parameter. This proxy forwards all query
 *   params to the backend verbatim.
 *
 * Buffering — three mechanisms, all addressed:
 *
 *   1. Async handler timing (decisive):
 *      `await fetch(...)` blocks the handler body before any Response is
 *      returned. Next.js cannot flush a single byte until GET() resolves, but
 *      a live SSE stream never terminates, so the handler never returns and the
 *      browser receives nothing. Fix: construct and return a Response wrapping
 *      the readable end of a TransformStream BEFORE the upstream fetch begins.
 *      The fetch + byte-forwarding runs in a detached fire-and-forget IIFE that
 *      outlives GET()'s return, piping into the writable end concurrently.
 *      Confirmed by GitHub issue #66263 ("when server proxy SSE request, browser
 *      receives data until response end").
 *
 *   2. Gzip compression middleware:
 *      Next.js accumulates gzip/brotli chunks looking for a minimum payload to
 *      compress before flushing. A non-terminating SSE stream never completes
 *      that flush threshold. Fix: `Content-Encoding: none` suppresses compression
 *      for this response only — safer than `compress: false` in next.config.js,
 *      which breaks middleware in dev (Next.js issues #48503/#48713/#50320).
 *
 *   3. Undici fetch cache:
 *      Next.js wraps `fetch` with an undici-backed cache that can partially
 *      materialise a response body before handing it to the handler's stream.
 *      `cache: 'no-store'` opts out of this layer (confirmed silently broken in
 *      production builds without this flag — GitHub issue #73589).
 *
 * Client disconnect propagation:
 *   `signal: request.signal` on the upstream fetch aborts the backend SSE
 *   connection when the browser closes (modal unmount → EventSource.close()).
 *   Without this, every closed tab leaks a backend asyncio task + Redis
 *   pub/sub subscription indefinitely.
 */
import { type NextRequest } from 'next/server';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

// Never cache — SSE must always open a fresh connection.
export const dynamic = 'force-dynamic';

// Node.js runtime: Edge runtime caps streaming connections at 30 s on Vercel
// (by platform design) and has subtle Web Streams constraints for long-lived
// TCP connections. Node.js has no such limit when self-hosted.
export const runtime = 'nodejs';

const encoder = new TextEncoder();

function buildSseErrorChunk(message: string): Uint8Array {
  return encoder.encode(`event: error\ndata: ${JSON.stringify({ error: message })}\n\n`);
}

export async function GET(request: NextRequest): Promise<Response> {
  const { searchParams } = request.nextUrl;

  // Forward all query params verbatim: instrument_key, token, symbol, lookback_hours.
  const backendUrl = new URL(`${BACKEND_URL}/api/v1/ai/stream`);
  searchParams.forEach((value, key) => backendUrl.searchParams.set(key, value));

  const forwardHeaders: Record<string, string> = {
    Accept: 'text/event-stream',
    'Cache-Control': 'no-cache',
  };

  // Forward client IP for backend rate limiting / audit logging.
  const clientIp =
    request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ||
    request.headers.get('x-real-ip') ||
    '';
  if (clientIp) forwardHeaders['X-Forwarded-For'] = clientIp;

  // TransformStream bridges the upstream reader and the browser writer.
  // We return a Response wrapping `readable` BEFORE the upstream fetch begins —
  // this is what allows Next.js to flush response headers immediately and start
  // delivering bytes as they arrive. The fire-and-forget IIFE below feeds
  // `writable` concurrently after GET() returns.
  const { readable, writable } = new TransformStream<Uint8Array, Uint8Array>();
  const writer = writable.getWriter();

  // ── CRITICAL: return before any upstream I/O ─────────────────────────────────
  // Next.js cannot send a byte until GET() returns a Response. Returning here
  // with `readable` unblocks delivery. The IIFE below runs concurrently.
  const response = new Response(readable, {
    status: 200,
    headers: {
      'Content-Type':      'text/event-stream; charset=utf-8',
      'Cache-Control':     'no-cache, no-transform',
      'Connection':        'keep-alive',
      'X-Accel-Buffering': 'no',      // Disable Nginx / Caddy proxy buffering
      'Content-Encoding':  'none',    // Suppress Next.js gzip compression (see note 2)
    },
  });

  // ── Upstream fetch + pipe (fire-and-forget) ──────────────────────────────────
  // `request.signal` propagates the browser's disconnect: when the modal unmounts
  // and EventSource.close() fires, the AbortSignal aborts the upstream connection
  // — no leaked backend SSE tasks or Redis pub/sub subscriptions.
  void (async () => {
    try {
      const upstream = await fetch(backendUrl.toString(), {
        method:  'GET',
        headers: forwardHeaders,
        cache:   'no-store',      // Bypass Next.js undici fetch cache (see note 3)
        signal:  request.signal,  // Propagate client disconnect to backend
      });

      if (!upstream.ok || !upstream.body) {
        // Non-SSE backend error (e.g. 401 invalid token) — emit an SSE error
        // event so the client's `es.addEventListener('error', ...)` handler fires
        // cleanly instead of the connection silently hanging.
        const text = await upstream.text().catch(() => upstream.statusText);
        await writer.write(buildSseErrorChunk(`backend error ${upstream.status}: ${text}`));
        return;
      }

      // Manual read loop — preferred over `body.pipeTo(writable)` because pipeTo
      // transfers control flow to stream internals and makes cleanup non-deterministic:
      // if the upstream closes mid-stream or the client disconnects, pipeTo's
      // rejection can propagate asynchronously and miss the finally block.
      // The manual loop with try/catch/finally guarantees writer.close() in all paths.
      const reader = upstream.body.getReader();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        await writer.write(value);
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        // Client disconnected cleanly (modal close / tab navigation) — not an error.
        return;
      }
      // Upstream unreachable or stream error — emit SSE error event for client visibility.
      try {
        await writer.write(buildSseErrorChunk('Could not connect to backend'));
      } catch {
        // Writer may already be closed if client disconnected simultaneously.
      }
    } finally {
      // Always close the writer to signal end-of-stream to the browser.
      // Without this, the response hangs open after the upstream closes,
      // leaking the connection on the client side indefinitely.
      await writer.close().catch(() => {});
    }
  })();

  return response;
}
