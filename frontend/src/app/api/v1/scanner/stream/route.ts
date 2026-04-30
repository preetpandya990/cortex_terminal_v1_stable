// PATH: frontend/src/app/api/v1/scanner/stream/route.ts
// ─────────────────────────────────────────────────────
/**
 * SSE pass-through proxy for POST /api/v1/scanner/run/stream.
 *
 * This static route takes precedence over the catch-all /api/v1/[...path]
 * proxy, which buffers responses and cannot forward text/event-stream bodies.
 * Here we stream the backend response body directly — zero buffering.
 */
import { NextRequest } from 'next/server';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

// Disable Next.js static optimisation — this route must never be cached.
export const dynamic = 'force-dynamic';

export async function POST(request: NextRequest): Promise<Response> {
  const authHeader = request.headers.get('authorization');
  const clientIp =
    request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ||
    request.headers.get('x-real-ip') ||
    '';

  const forwardHeaders: HeadersInit = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  };
  if (authHeader) forwardHeaders['Authorization'] = authHeader;
  if (clientIp) forwardHeaders['X-Forwarded-For'] = clientIp;

  let bodyText: string;
  try {
    bodyText = JSON.stringify(await request.json());
  } catch {
    bodyText = '{}';
  }

  let backendResponse: Response;
  try {
    backendResponse = await fetch(`${BACKEND_URL}/api/v1/scanner/run/stream`, {
      method: 'POST',
      headers: forwardHeaders,
      body: bodyText,
      // Node 18+ requires this flag when sending a body with a streaming response.
      // @ts-expect-error — Node fetch extension not in DOM types
      duplex: 'half',
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: 'Could not connect to backend', detail: String(err) }),
      { status: 503, headers: { 'Content-Type': 'application/json' } },
    );
  }

  if (!backendResponse.ok || !backendResponse.body) {
    const text = await backendResponse.text();
    return new Response(text, {
      status: backendResponse.status,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  // Pass the SSE stream body straight through — no buffering.
  return new Response(backendResponse.body, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      'X-Accel-Buffering': 'no',
      'Connection': 'keep-alive',
    },
  });
}
