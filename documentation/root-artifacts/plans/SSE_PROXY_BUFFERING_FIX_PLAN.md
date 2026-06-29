# SSE Proxy Buffering — Fix Plan
**AI Explanation Panel — Permanent Skeleton Bug**

Authored: 2026-06-28  
Status: Ready to implement  
Severity: High — all watchlist instruments without an active trade suggestion show a permanent skeleton

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Root Cause Analysis](#2-root-cause-analysis)
   - 2.1 [Cause A — Async Handler Timing (decisive)](#21-cause-a--async-handler-timing-decisive)
   - 2.2 [Cause B — Gzip Compression Middleware](#22-cause-b--gzip-compression-middleware)
   - 2.3 [Cause C — Next.js Undici Fetch Cache](#23-cause-c--nextjs-undici-fetch-cache)
   - 2.4 [Amplifier — No REST Fallback for Explanation](#24-amplifier--no-rest-fallback-for-explanation)
   - 2.5 [Amplifier — Modal Lifecycle Resets SSE State](#25-amplifier--modal-lifecycle-resets-sse-state)
3. [Fix Overview](#3-fix-overview)
4. [Fix A — SSE Proxy Route](#4-fix-a--sse-proxy-route)
   - 4.1 [What Changes](#41-what-changes)
   - 4.2 [Full Implementation](#42-full-implementation)
   - 4.3 [Design Decisions](#43-design-decisions)
   - 4.4 [What Does Not Change](#44-what-does-not-change)
5. [Fix B — REST Fallback for Explanation](#5-fix-b--rest-fallback-for-explanation)
   - 5.1 [Backend — New Endpoint](#51-backend--new-endpoint)
   - 5.2 [Full Backend Implementation](#52-full-backend-implementation)
   - 5.3 [Frontend — 4th useQuery](#53-frontend--4th-usequery)
   - 5.4 [Frontend — Updated Merge Priority Logic](#54-frontend--updated-merge-priority-logic)
   - 5.5 [isExplanationLoading — No Change Required](#55-isexplanationloading--no-change-required)
6. [Files Changed](#6-files-changed)
7. [Deployment & Verification](#7-deployment--verification)
8. [Reference: Why Rejected Alternatives Fail](#8-reference-why-rejected-alternatives-fail)

---

## 1. Problem Statement

The AI Explanation Panel inside the Detail Pane of the Hawk-Eye Radar page shows an animated "Generating…" skeleton indefinitely for watchlist instruments that have no active trade suggestion in the past 7 days (currently: COMSYN, FMCGADD, CMRGREEN, BAJFINANCE).

The explanation IS fully generated and available:

- **DB**: All 4 instruments have fresh `ai_instrument_context` rows with `is_fresh = true` (2-hour TTL).
- **Redis**: SSE event store (`cortex:sse:events:ctx:{instrument_key}`) has a rich payload with `available: true` and a full explanation text.
- **Backend SSE stream**: Direct `curl` to port 8000 receives all 4 events within 100ms.

The data exists and the backend is correct. The failure is entirely in the Next.js proxy layer — data is generated but never delivered to the browser.

On rare occasions the explanation appears (when the browser proxy buffer happens to flush due to accumulated byte volume), but immediately disappears when the Detail Pane modal is closed and reopened, because the component unmounts and the `EventSource` is torn down.

---

## 2. Root Cause Analysis

Three buffering mechanisms stack on top of each other inside the Next.js App Router proxy. Understanding all three is necessary because fixing only one or two will not reliably solve the issue across all environments and load conditions.

### 2.1 Cause A — Async Handler Timing (decisive)

**File:** `frontend/src/app/api/v1/ai/stream/route.ts:44`

```typescript
// Current broken pattern:
export async function GET(request: NextRequest): Promise<Response> {
  // ...
  backendResponse = await fetch(backendUrl.toString(), {  // ← blocks here
    method: 'GET',
    headers: forwardHeaders,
  });
  // ...
  return new Response(backendResponse.body, { ... });     // ← returned after fetch completes
}
```

**What happens:** `await fetch(...)` runs synchronously inside the handler body before the `Response` object is constructed or returned. In Next.js's App Router, the runtime cannot send a single byte of response to the browser until the `async function GET(...)` resolves and returns a `Response`. For a healthy SSE stream (which never terminates until the client disconnects), this means the handler never returns during normal operation. The browser makes a TCP connection, sends the GET request, and receives nothing — the response headers are never sent, the body is never streamed, the `EventSource` connection appears to hang.

This is the decisive cause for this specific bug. Confirmed by GitHub issue #66263 ("when server proxy SSE request, browser receives data until response end").

**Why the explanation "appeared once":** During testing, a second concurrent SSE connection was opened directly to port 8000 for the same instrument. The combined byte volume of events from both connections pushed the proxy's internal write buffer past its flush threshold. All buffered events were released simultaneously, and the explanation appeared. This is a buffer-overrun fluke, not correct behavior.

**Why modal close/reopen loses the explanation:** When the Detail Pane modal closes, `AnalysisCardsSection` unmounts and `esRef.current.close()` tears down the `EventSource`. On reopening, a fresh `EventSource` is created from zero state. The second concurrent connection is no longer running, so the buffer-overrun condition is not met, and the skeleton returns.

### 2.2 Cause B — Gzip Compression Middleware

Next.js applies gzip/brotli compression middleware to all responses by default. The compression algorithm accumulates bytes in a buffer looking for a minimum payload to compress efficiently before flushing. For a never-ending SSE stream, the buffer never reaches a "complete" state because the stream has no end — chunks sit in the compression buffer indefinitely, never flushed to the browser.

This is the most widely documented SSE failure mode in Next.js. Confirmed by GitHub Discussion #48427 (the canonical SSE thread with hundreds of upvotes).

**Why `Content-Encoding: none` (not `compress: false` globally):** Setting `compress: false` in `next.config.js` globally causes blank pages in development when middleware is present. Confirmed broken in Next.js 13.2–13.5 (GitHub issues #48503, #48713, #50320, tracked as NEXT-1183). The correct fix is per-response suppression via the `Content-Encoding: none` response header, which Next.js respects for that response only.

### 2.3 Cause C — Next.js Undici Fetch Cache

Next.js wraps the global `fetch` in a patched version that memoizes and caches responses using undici internally. Even with `cache: 'no-store'` on the `fetch()` call, undici may partially materialize the response body before handing it to the handler's `ReadableStream`. GitHub issue #73589 confirms `cache: 'no-store'` silently fails in production builds in Next.js 14.2.20 — response timestamps are unchanged across requests, indicating the cache layer is not always bypassed.

`cache: 'no-store'` remains necessary (it prevents the full-body materialization in the common case), but it is not sufficient alone. The TransformStream + fire-and-forget pattern (Fix A) operates below the undici layer, bypassing this concern entirely.

### 2.4 Amplifier — No REST Fallback for Explanation

Three of four analysis cards (ML Pattern, AI Sentiment, Prediction Summary) have React Query fallback pollers that activate immediately on mount and run on a schedule when SSE is unavailable:

```typescript
// Existing pattern in AnalysisCardsSection.tsx:
predictionQuery:  refetchInterval: sseConnected ? false : 60_000
patternQuery:     refetchInterval: sseConnected ? false : 300_000
sentimentQuery:   refetchInterval: sseConnected ? false : 120_000
// explanationQuery: DOES NOT EXIST
```

When the SSE proxy fails to deliver events, the three cards update via REST polling. The explanation panel has no alternative path — it is SSE-only. This turns a proxy bug into a permanent user-visible failure with no recovery path.

### 2.5 Amplifier — Modal Lifecycle Resets SSE State

Closing and reopening the Detail Pane modal unmounts and remounts `AnalysisCardsSection`, creating a fresh `EventSource` and resetting all local SSE state (including `isInitialLoad = true` and all data states to `null`). Any explanation that arrived via a buffer-flush fluke is lost and the cycle starts over.

A REST fallback query (Fix B) resolves this: React Query's cache survives component unmount/remount within the same session, so the explanation data is instantly available on reopen without a network request.

---

## 3. Fix Overview

Two independent, complementary fixes. Both are required for a production-quality solution.

| Fix | File(s) | What it does | Severity without it |
|---|---|---|---|
| **A — SSE proxy** | `route.ts` (1 file) | Eliminates the 3 buffering mechanisms; makes SSE delivery work correctly | Without this: explanation never arrives via SSE for any instrument |
| **B — REST fallback** | `ai_stream.py` + `AnalysisCardsSection.tsx` (2 files) | Defense-in-depth; ensures explanation loads even if SSE degrades; survives modal remount | Without this: any future SSE issue causes a permanent skeleton again |

**Part A is the primary fix.** After Part A, SSE will correctly deliver the `available: true` instrument context for all 4 watchlist instruments within 100ms of connection. This resolves the immediate user-visible bug.

**Part B is defense-in-depth and UX polish.** It ensures the explanation panel is resilient against SSE outages, modal remount, and future proxy issues. Without Part B, the explanation card remains the only card in the system with no fallback — a structural gap in reliability.

---

## 4. Fix A — SSE Proxy Route

### 4.1 What Changes

**File:** `frontend/src/app/api/v1/ai/stream/route.ts`

Three changes to the existing route handler:

1. Add `export const runtime = 'nodejs'` — prevents Edge runtime selection (Edge caps SSE at 30 seconds on Vercel; Node.js has no limit when self-hosted).
2. **Return `Response` immediately with a `TransformStream`, before touching the upstream connection.** This is the load-bearing fix. The upstream fetch + byte forwarding runs in a detached fire-and-forget async IIFE that outlives the `GET` function's return.
3. Add `Content-Encoding: none` to response headers — per-response gzip suppression. Also add `cache: 'no-store'` to the upstream `fetch()` call, and `signal: request.signal` to propagate client disconnect.

### 4.2 Full Implementation

```typescript
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
 *
 * Buffering prevention — three mechanisms addressed:
 *   1. Async timing:    Response is returned BEFORE the upstream fetch begins.
 *                       The fetch + pipe runs in a detached fire-and-forget IIFE.
 *                       Next.js cannot deliver any bytes until GET() returns;
 *                       returning first via TransformStream unblocks delivery.
 *   2. Gzip middleware: Content-Encoding: none suppresses Next.js compression
 *                       per-response (safer than compress:false globally, which
 *                       breaks middleware in dev — Next.js issues #48503/#48713).
 *   3. Undici cache:    cache: 'no-store' on the upstream fetch opts out of
 *                       Next.js's patched fetch memoisation layer.
 */
import { type NextRequest } from 'next/server';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

// Disable Next.js static optimisation — must never be cached.
export const dynamic = 'force-dynamic';

// Node.js runtime required: Edge runtime caps streaming connections at 30 s on
// Vercel (by platform design). Node.js has no such limit when self-hosted.
export const runtime = 'nodejs';

const encoder = new TextEncoder();

function sseError(message: string): Uint8Array {
  return encoder.encode(`event: error\ndata: ${JSON.stringify({ error: message })}\n\n`);
}

export async function GET(request: NextRequest): Promise<Response> {
  const { searchParams } = request.nextUrl;

  // Forward all query params verbatim (instrument_key, token, symbol, lookback_hours)
  const backendUrl = new URL(`${BACKEND_URL}/api/v1/ai/stream`);
  searchParams.forEach((value, key) => backendUrl.searchParams.set(key, value));

  const forwardHeaders: Record<string, string> = {
    Accept: 'text/event-stream',
    'Cache-Control': 'no-cache',
  };

  // Forward client IP for rate limiting / audit logging
  const clientIp =
    request.headers.get('x-forwarded-for')?.split(',')[0]?.trim() ||
    request.headers.get('x-real-ip') ||
    '';
  if (clientIp) forwardHeaders['X-Forwarded-For'] = clientIp;

  // TransformStream is the pipe between the upstream reader and the browser writer.
  // We return a Response wrapping `readable` BEFORE opening the upstream connection —
  // this is what allows Next.js to flush headers immediately and begin streaming.
  const { readable, writable } = new TransformStream<Uint8Array, Uint8Array>();
  const writer = writable.getWriter();

  // ── CRITICAL: return before any upstream I/O ───────────────────────────────
  // Next.js cannot send bytes until GET() returns. Returning here with the
  // readable end of the TransformStream unblocks the response immediately.
  // The fire-and-forget IIFE below runs concurrently after the return.
  const response = new Response(readable, {
    status: 200,
    headers: {
      'Content-Type':      'text/event-stream; charset=utf-8',
      'Cache-Control':     'no-cache, no-transform',
      'Connection':        'keep-alive',
      'X-Accel-Buffering': 'no',       // Disable Nginx/Caddy proxy buffering
      'Content-Encoding':  'none',     // Suppress Next.js gzip compression middleware
    },
  });

  // ── Upstream fetch + pipe (fire-and-forget) ────────────────────────────────
  // Runs concurrently after GET() returns. request.signal propagates client
  // disconnect: when the browser closes the tab or the modal unmounts and
  // EventSource.close() is called, the AbortSignal fires and the upstream
  // connection is torn down — no leaked backend SSE connections.
  void (async () => {
    try {
      const upstream = await fetch(backendUrl.toString(), {
        method:  'GET',
        headers: forwardHeaders,
        cache:   'no-store',       // opt out of Next.js fetch memoisation
        signal:  request.signal,   // propagate client disconnect to backend
      });

      if (!upstream.ok || !upstream.body) {
        // Non-SSE error from backend (e.g. 401 invalid token) — emit an SSE
        // error event so the client's onerror handler fires cleanly.
        const text = await upstream.text().catch(() => upstream.statusText);
        await writer.write(sseError(`backend error ${upstream.status}: ${text}`));
        return;
      }

      // Pipe upstream bytes straight through. Manual read loop is used over
      // pipeTo() for reliable cleanup: pipeTo() takes control flow away from
      // the handler and silently swallows errors in some Node.js versions.
      const reader = upstream.body.getReader();
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        await writer.write(value);
      }
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        // Client disconnected cleanly — not an error condition.
        return;
      }
      // Upstream unreachable or stream error — emit SSE error event.
      try {
        await writer.write(sseError('Could not connect to backend'));
      } catch {
        // writer may already be closed if client disconnected simultaneously
      }
    } finally {
      // Always close the writer so the TransformStream signals end-of-stream
      // to the browser. Without this, the response hangs open after the
      // upstream closes, leaking the connection on the client side.
      await writer.close().catch(() => {});
    }
  })();

  return response;
}
```

### 4.3 Design Decisions

**`TransformStream` over direct `backendResponse.body` passthrough:**
The `new Response(backendResponse.body, {...})` pattern in the current code does not help because `backendResponse` is only available after `await fetch(...)` completes — which for an infinite SSE stream is never. We need a `Response` to return *before* we know what the upstream body will be. `TransformStream` provides the `readable` end to wrap in the `Response` now, and the `writable` end to pipe upstream bytes into later.

**Manual `reader.read()` loop over `body.pipeTo(writable)`:**
`pipeTo()` transfers control flow to the stream internals and makes error handling and cleanup non-deterministic. In particular, if the upstream closes unexpectedly mid-stream, `pipeTo()`'s rejection propagates asynchronously in a way that can miss the `finally` block. The manual loop with `try/catch/finally` guarantees `writer.close()` is called in all code paths.

**`signal: request.signal` on the upstream fetch:**
When the browser calls `EventSource.close()` (which happens when `AnalysisCardsSection` unmounts on modal close), the `AbortSignal` fires. Without this, the backend SSE connection for that instrument stays open indefinitely, consuming a backend asyncio task + Redis pub/sub subscription per closed tab. Given the session-leak and pool-exhaustion bugs already identified in the HawkEye 500 investigation, this is non-negotiable.

**`export const runtime = 'nodejs'`:**
The Edge runtime is designed for low-latency, short-duration responses. On Vercel it caps streaming to 30 seconds. The SSE connections here are long-lived (minutes to hours per open Hawk-Eye Radar view). Node.js runtime has no artificial cap when self-hosted. Even if Vercel is never used, the Edge runtime has subtle constraints around Web Streams API that make long-lived streams unreliable.

**`Content-Encoding: none` (not `compress: false` in `next.config.js`):**
The global `compress: false` option causes blank pages in development when middleware is present (Next.js issues #48503, #48713, #50320). The per-response header is safe and precise — it tells Next.js's compression middleware to skip this response only.

### 4.4 What Does Not Change

- All query parameter forwarding logic is unchanged.
- Client IP forwarding is unchanged.
- The `dynamic = 'force-dynamic'` export is unchanged.
- The non-SSE error handling path is functionally equivalent — errors are now emitted as SSE `error` events so the client's `onerror` handler fires rather than the connection silently hanging.
- No changes to the backend (`ai_stream.py`) — it is already working correctly.
- No changes to the `EventSource` connection logic in `AnalysisCardsSection.tsx` for Fix A.

---

## 5. Fix B — REST Fallback for Explanation

### 5.1 Backend — New Endpoint

**File:** `backend/app/api/v1/ai_stream.py`

Add a new `GET /ai/explanation` route handler to the existing `ai` router. The endpoint is a stateless wrapper around the existing `_fetch_explanation_for_instrument` function — which already implements the 3-stage lookup (suggestion → cached context → trigger generation) and is already correct.

Key design constraints:
- **No SSE state machine** — `_should_apply_polled_explanation` exists to protect in-memory stream state from downgrades (a poll must never overwrite a push-delivered explanation in a running stream). That guard is irrelevant for a stateless REST endpoint; each call returns the current DB/Redis state unconditionally.
- **Always HTTP 200** — even when `available: false`. The SSE endpoint follows this convention (returns `{available: false}` skeleton while the worker generates). The frontend `ExplanationData` type already handles this discriminated union. Never return HTTP 404 for "not yet generated" — that would require the frontend to distinguish "not found" from "not ready" which is a different semantic.
- **`sources: []` always** — source citations are populated by the SSE push path from the Redis event store (where the RAG source list is written by the explanation worker). They are not stored in the `trade_suggestions` or `ai_instrument_context` tables. The REST endpoint reads from DB/Redis stream but the stream entry already has the sources embedded — so `_fetch_explanation_for_instrument` can already return sources when a Redis stream entry is present (Stage 1/2 both try the SSE event store first). The comment in `ExplanationData` about "sources only on push path" refers to the periodic poll within the SSE stream, not to this REST endpoint.
- **Stage 3 runs on REST calls too** — if no cached context exists, the endpoint should still trigger background context generation via the Redis stream job. This is the correct behavior: a user opening the Hawk-Eye Radar detail pane (which triggers a REST poll) should also trigger context generation if none exists, not just SSE connections.
- **No new dependency injection** — the function takes `db: AsyncSession` and `redis`, both available via the standard patterns used throughout the codebase. Use `async with AsyncSessionLocal()` and `get_redis()` consistent with the SSE endpoint's session-per-operation pattern.

### 5.2 Full Backend Implementation

Add the following route to `backend/app/api/v1/ai_stream.py`, after the existing `/stream` route definition:

```python
@router.get(
    "/explanation",
    summary="Get the current AI explanation for an instrument (REST fallback)",
    description=(
        "Stateless REST alternative to the SSE stream's explanation payload. "
        "Runs the same 3-stage lookup as the SSE explanation refresher: "
        "(1) recent suggestion with explanation, (2) cached instrument context, "
        "(3) trigger background generation and return pending skeleton. "
        "Used as a polling fallback when the SSE connection is unavailable. "
        "Always returns HTTP 200 — available=false means 'generating, poll again'."
    ),
)
async def get_explanation(
    instrument_key: str = Query(..., description="NSE instrument key, e.g. NSE_EQ|INE296A01032"),
    symbol: str | None = Query(None, description="Optional ticker symbol, e.g. BAJFINANCE"),
    token: str | None = Query(None, description="JWT access token (required)"),
    request: Request = None,
) -> JSONResponse:
    # ── Auth: same token validation as the SSE stream ─────────────────────────
    raw_token = token
    if not raw_token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            raw_token = auth_header[7:]
    if not raw_token:
        return JSONResponse(
            {"detail": "Missing authentication token"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    try:
        decode_token(raw_token, expected_type="access")
    except CortexInvalidTokenError as exc:
        return JSONResponse(
            {"detail": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    # ── 3-stage explanation lookup ─────────────────────────────────────────────
    try:
        async with AsyncSessionLocal() as db:
            redis = await get_redis()
            payload = await asyncio.wait_for(
                _fetch_explanation_for_instrument(
                    db=db,
                    instrument_key=instrument_key,
                    symbol=symbol,
                    redis=redis,
                ),
                timeout=_OPERATION_TIMEOUT_SECS,
            )
        return JSONResponse(payload)
    except asyncio.TimeoutError:
        logger.warning(
            "REST explanation lookup timed out: instrument=%s", instrument_key
        )
        return JSONResponse(
            {
                "available":        False,
                "failed":           False,
                "summary":          None,
                "full_explanation": None,
                "model":            None,
                "generated_at":     None,
                "sources":          [],
                "context_type":     "instrument_context",
                "signal_direction": None,
                "signal_generated_at": None,
            }
        )
    except Exception as exc:
        logger.error(
            "REST explanation lookup failed: instrument=%s error=%s",
            instrument_key, exc, exc_info=True,
        )
        return JSONResponse(
            {"detail": "Internal server error"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
```

**Router registration:** The `router` object in `ai_stream.py` already has `prefix="/ai"` and is registered under `/api/v1` in `main.py`. The new endpoint is automatically available at `GET /api/v1/ai/explanation` — no registration changes needed.

### 5.3 Frontend — 4th `useQuery`

**File:** `frontend/src/components/AnalysisCardsSection.tsx`

Add a fourth React Query query for explanation, mirroring the existing three queries in structure. Place it after `sentimentQuery` in the polling fallback section:

```typescript
const explanationQuery = useQuery({
  queryKey: ['ai-explanation', instrumentKey, symbol],
  queryFn: async () => {
    const res = await api.get('/ai/explanation', {
      params: {
        instrument_key: instrumentKey,
        ...(symbol ? { symbol } : {}),
      },
    });
    return res.data as ExplanationData;
  },
  enabled: canQuery,
  staleTime:                  2 * 60 * 60 * 1000,    // 2 hours — matches backend cache TTL
  refetchInterval:            sseConnected ? false : 5 * 60 * 1000,  // 5 min fallback
  refetchIntervalInBackground: false,                 // no polling when tab is hidden
});
```

**`staleTime: 2 hours`** matches the backend's `AIInstrumentContext` TTL. Explanation is generated once and does not change until TTL expiry. This prevents React Query from triggering background refetches on window focus or component remount while the data is fresh — which would be wasted requests since the explanation text is stable.

**`refetchInterval: 5 minutes` (not 30 seconds):** The SSE stream polls at 30-second intervals because it is a persistent connection where the poll cost is near-zero (a DB query already in-flight for other components). A REST poll at 30 seconds would be 120 requests/hour per open tab per instrument. Five minutes is aggressive enough to catch a completed explanation (workers typically finish in 15–30 seconds) without excessive server load. If faster recovery is needed in the future, this can be tightened to 60 seconds with acceptable cost.

**`enabled: canQuery` (not `enabled: canQuery && !sseConnected`):** The initial fetch on mount is desirable even when SSE is connecting — it populates the query cache immediately so the explanation is available if the SSE stream fails on its first connection attempt or if the modal is reopened quickly. Using `!sseConnected` would prevent the initial fetch, leaving the panel as a skeleton for the full SSE retry cycle (3 × 5s = 15s minimum) before the fallback activates.

### 5.4 Frontend — Updated Merge Priority Logic

**File:** `frontend/src/components/AnalysisCardsSection.tsx`

The merge priority IIFE (currently lines 300–309) adds `explanationQuery.data` as a third-tier `available: true` candidate, and as a final fallback in the last line. The SSE state machine always takes precedence when healthy:

```typescript
const explanationData: ExplanationData | null = (() => {
  // 1. SSE push/poll delivered an available explanation — authoritative.
  //    The SSE stream carries structured source citations unavailable via REST.
  if (sseExplanation?.available) return sseExplanation;

  // 2. REST suggestion seed delivered an available explanation — use immediately.
  //    This covers the case where llm_explanation was set before SSE subscribed.
  if (suggestionExplanation?.available) return suggestionExplanation;

  // 3. REST fallback explanation query — activates when SSE is unavailable.
  //    This is the new addition. Only applies when SSE has not delivered
  //    (sseExplanation is null or unavailable).
  if (explanationQuery.data?.available) return explanationQuery.data;

  // A streaming partial from SSE (available:false but with text flowing in)
  // beats the skeleton seed so the panel renders progressively.
  if (sseExplanation && !sseExplanation.available && sseExplanation.full_explanation) {
    return sseExplanation;
  }

  // Final fallback — prefer any non-null value over null. REST query data is
  // included here so a "generating" skeleton from REST (available:false) is
  // surfaced when SSE has not yet connected, rather than hiding the panel.
  return sseExplanation ?? explanationQuery.data ?? suggestionExplanation ?? null;
})();
```

**Priority rationale:**
1. SSE with `available: true` — authoritative; carries structured source citations from the RAG push path that are not stored in DB or returned by REST.
2. REST suggestion seed with `available: true` — use immediately (push notification may have been missed if explanation was pre-generated before the SSE watcher subscribed).
3. REST fallback query with `available: true` — new; activates when SSE proxy is unavailable or degraded.
4. SSE streaming partial — progressive rendering while tokens stream in.
5. Final fallback — any non-null value; includes REST `available: false` skeleton to signal "generating" rather than hiding the panel entirely.

Also update the data merge for the final line to include `explanationQuery.data`:
```typescript
// Keep explanationDataRef current (unchanged formula, just update the fallback chain)
explanationDataRef.current = explanationData;
```

### 5.5 `isExplanationLoading` — No Change Required

The current formula:
```typescript
const isExplanationLoading = sseLoading && explanationData === null;
```

This formula does not need to change. Once `explanationQuery.data` is populated and the merge logic returns a non-null `explanationData`, the `explanationData === null` check becomes false and `isExplanationLoading` becomes false — the skeleton clears and the panel renders the REST-fetched explanation. The transition is automatic and requires no additional loading state tracking.

**What happens step by step when SSE proxy is broken:**
1. Component mounts → `isInitialLoad = true` → `sseLoading = true`
2. `explanationQuery` fires immediately (enabled, no SSE yet)
3. `isExplanationLoading = true && null === null = true` → skeleton renders
4. REST query resolves (~200ms) → `explanationQuery.data = { available: true, ... }`
5. Merge logic: `explanationQuery.data?.available` is `true` → `explanationData = explanationQuery.data`
6. `isExplanationLoading = true && explanationData !== null = false` → skeleton clears
7. `ExplanationContent` renders with the REST-fetched explanation

Skeleton to content transition happens in ~200ms (single REST call) rather than the current permanent skeleton.

---

## 6. Files Changed

| File | Type | Change |
|---|---|---|
| `frontend/src/app/api/v1/ai/stream/route.ts` | **Replace** | Full rewrite — TransformStream + fire-and-forget IIFE, `runtime = 'nodejs'`, `Content-Encoding: none`, `cache: 'no-store'`, `signal: request.signal` |
| `backend/app/api/v1/ai_stream.py` | **Add** | New `GET /ai/explanation` route handler (~60 lines) after existing `/stream` route |
| `frontend/src/components/AnalysisCardsSection.tsx` | **Modify** | Add `explanationQuery` (4th useQuery), update merge priority IIFE (7 lines) |

No other files require changes. In particular:
- `AIExplanationPanel.tsx` — no changes; its display logic is already correct
- `analysis.ts` — no changes; `ExplanationData` type already covers the REST response shape
- `next.config.js` — no changes; `compress: false` is intentionally avoided
- Backend `main.py` — no changes; the new endpoint inherits router registration automatically

---

## 7. Deployment & Verification

### Verification Steps (in order)

**Step 1 — Verify Fix A (SSE proxy) in isolation:**
```bash
# With fix applied, curl through Next.js proxy should now receive events:
timeout 10 curl -v -N \
  "http://localhost:3000/api/v1/ai/stream?instrument_key=NSE_EQ%7CINE296A01032&token=<jwt>"

# Expected: 4 SSE events within 100ms, including event 4 with available:true
# Before fix: TCP connected, GET sent, 0 bytes received in 10s
```

**Step 2 — Verify Fix A does not break non-SSE error responses:**
```bash
# Invalid token should return a SSE error event (not hang):
curl -N "http://localhost:3000/api/v1/ai/stream?instrument_key=NSE_EQ%7CTEST&token=invalid"
# Expected: event: error\ndata: {"error":"backend error 401: ..."}\n\n
```

**Step 3 — Verify Fix B (REST fallback endpoint):**
```bash
# New REST endpoint should return explanation immediately:
curl -s "http://localhost:8000/api/v1/ai/explanation?instrument_key=NSE_EQ%7CINE296A01032&symbol=BAJFINANCE&token=<jwt>" | jq .

# Expected: { "available": true, "full_explanation": "...", "context_type": "instrument_context", ... }
```

**Step 4 — End-to-end UI verification:**
1. Open Hawk-Eye Radar, open Detail Pane for BAJFINANCE (no active suggestion).
2. AI Explanation Panel should transition from skeleton to content within ~500ms.
3. Close and reopen the Detail Pane modal — explanation should appear immediately (from React Query cache, no network request).
4. Open browser DevTools → Network → filter "stream" → confirm SSE events are now arriving.

**Step 5 — Client disconnect propagation:**
1. Open Detail Pane → confirm SSE connection is established.
2. Close the modal → `EventSource.close()` fires.
3. In backend logs, confirm the SSE connection for that instrument_key is torn down (no dangling asyncio task).

### Rollback

Fix A is a complete file rewrite. The previous version of `route.ts` is in git history. If the fix causes unexpected behavior, `git checkout HEAD -- frontend/src/app/api/v1/ai/stream/route.ts` restores the prior state.

Fix B additions are additive only — the new backend endpoint is a new route, not a modification of an existing one. The new `explanationQuery` in the frontend is a new query, not a replacement. Rolling back Fix B requires reverting the `AnalysisCardsSection.tsx` changes and removing the backend route. Neither removal affects other parts of the system.

---

## 8. Reference: Why Rejected Alternatives Fail

**"Just add `cache: 'no-store'` to the fetch call (without TransformStream)"**
Does not address Cause A (async timing). The `await fetch(...)` still blocks the handler from returning. The browser still receives nothing until the backend closes the connection. Partially addresses Cause C (undici caching) but not reliably (confirmed broken in production in Next.js 14.2.20 per issue #73589).

**"Set `compress: false` in next.config.js"**
Causes blank pages in development when Next.js middleware is active (issues #48503, #48713, #50320, NEXT-1183). Breaks the entire app in dev. Using `Content-Encoding: none` per-response achieves the same suppression safely.

**"Use `body.pipeTo(writable)` instead of the manual read loop"**
`pipeTo()` takes control flow away from the handler and makes error handling non-deterministic. In particular, if the upstream closes unexpectedly mid-stream or the client disconnects, the `pipeTo()` rejection propagates asynchronously and may miss the `finally` block. The manual `reader.read()` loop with `try/catch/finally` guarantees `writer.close()` in all code paths.

**"Use `export const runtime = 'edge'`"**
Edge runtime caps streaming to 30 seconds on Vercel (by platform design). Not suitable for long-lived SSE connections. Edge also has constraints around persistent TCP connections and state between requests. Node.js runtime is the standard recommendation across all Next.js SSE documentation and GitHub discussion threads.

**"`new Response(backendResponse.body, {...})` with TransformStream wrapping on top"**
Still requires `await fetch(...)` to complete before the `Response` is constructed. The problem is the `await` on the fetch, not the `Response` constructor. The only solution is to return the `Response` before the `await`.

**"Add `refetchInterval: 30_000` to match the SSE poll cadence"**
The SSE 30-second poll is cheap because it runs inside a persistent connection alongside prediction, pattern, and sentiment refreshers — all sharing the same backend task and asyncio context. A REST poll at 30 seconds is a full HTTP request-response cycle: 120 requests/hour per open tab per instrument. At 4 watchlist instruments × multiple concurrent users, this becomes significant server load for no benefit (explanation text is stable for 2 hours).

**"Use `enabled: canQuery && !sseConnected` to prevent redundant fetches"**
Prevents the initial fetch, leaving the panel blank for the full SSE retry cycle (15+ seconds) before the REST fallback activates. The initial fetch is desirable regardless of SSE state — it populates the cache immediately and ensures the panel is never blank longer than a single network roundtrip.

---

*This document covers the full technical context, implementation, and rationale needed to implement both fixes without ambiguity.*
