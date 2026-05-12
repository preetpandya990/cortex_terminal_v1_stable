// PATH: frontend/src/app/api/v1/ai/stream/route.ts
// ─────────────────────────────────────────────────
/**
 * SSE pass-through proxy for GET /api/v1/ai/stream.
 *
 * Why this exists:
 *   The catch-all /api/v1/[...path] proxy buffers responses and cannot forward
 *   text/event-stream bodies. This static route takes precedence and streams
 *   the SSE response body directly — zero buffering.
 *
 * Auth:
 *   The browser EventSource API cannot send custom headers, so the JWT is
 *   passed as the `token` query parameter. This proxy forwards all query
 *   params to the backend and converts the Authorization header if present.
 */
import { NextRequest } from 'next/server';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

// Disable Next.js static optimisation — must never be cached.
export const dynamic = 'force-dynamic';

export async function GET(request: NextRequest): Promise<Response> {
  const { searchParams } = request.nextUrl;

  // Forward all query params verbatim (instrument_key, token, symbol, lookback_hours)
  const backendUrl = new URL(`${BACKEND_URL}/api/v1/ai/stream`);
  searchParams.forEach((value, key) => backendUrl.searchParams.set(key, value));

  const forwardHeaders: HeadersInit = {
    Accept: 'text/event-stream',
    'Cache-Control': 'no-cache',
  };

  // Forward client IP for rate limiting / audit logging
  const clientIp =
    request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ||
    request.headers.get('x-real-ip') ||
    '';
  if (clientIp) forwardHeaders['X-Forwarded-For'] = clientIp;

  let backendResponse: Response;
  try {
    backendResponse = await fetch(backendUrl.toString(), {
      method: 'GET',
      headers: forwardHeaders,
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: 'Could not connect to backend', detail: String(err) }),
      { status: 503, headers: { 'Content-Type': 'application/json' } },
    );
  }

  // Non-SSE error response (e.g. 401 invalid token) — pass through as JSON
  if (!backendResponse.ok || !backendResponse.body) {
    const text = await backendResponse.text();
    return new Response(text, {
      status: backendResponse.status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // Stream SSE body straight through — zero buffering.
  return new Response(backendResponse.body, {
    status: 200,
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      'X-Accel-Buffering': 'no',      // Disable Nginx proxy buffering
      Connection: 'keep-alive',
    },
  });
}
