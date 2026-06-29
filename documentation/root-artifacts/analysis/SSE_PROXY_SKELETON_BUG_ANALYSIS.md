# SSE Proxy Buffering — AI Explanation Panel Skeleton Bug

**Discovered:** 2026-06-27  
**Status:** Identified, unfixed  
**Severity:** High — AI Explanation Panel shows permanent skeleton for all watchlist instruments with no active trade suggestion

---

## Problem Statement

The AI Explanation Panel (`AIExplanationPanel`) inside the Detail Pane of the Hawk-Eye Radar page shows an animated "Generating…" skeleton indefinitely for all 4 watchlist instruments (COMSYN, FMCGADD, CMRGREEN, BAJFINANCE). The explanation is fully generated and available in both the database and Redis — the data exists, but it never reaches the browser.

Additionally, on rare occasions the explanation does appear (when the browser's proxy buffer happens to flush), but immediately disappears when the Detail Pane modal is closed and reopened, reverting to skeleton.

---

## Investigation Methods

### 1. Confirmed data exists end-to-end

**DB query** — all 4 watchlist instruments have fresh `ai_instrument_context` rows:
```sql
SELECT instrument_key, symbol, generated_at, expires_at,
  expires_at > now() AS is_fresh
FROM ai_instrument_context
WHERE instrument_key IN (
  'NSE_EQ|INE073V01015','NSE_EQ|INF740KA1ZA4',
  'NSE_EQ|INE00WV01027','NSE_EQ|INE296A01032'
);
-- Result: all 4 rows, is_fresh = true (2-hour TTL, generated at 08:52–09:00 UTC)
```

**Redis query** — SSE event store has rich payload (with sources) for all 4:
```
XREVRANGE "cortex:sse:events:ctx:NSE_EQ|INE296A01032" + - COUNT 1
-- Result: 1 entry, data = { available: true, full_explanation: "...", context_type: "instrument_context" }
```

**Trade suggestions check** — all 4 watchlist instruments have **zero** trade suggestions in the past 7 days:
```sql
SELECT ts.instrument_key FROM trade_suggestions ts
JOIN watchlist_items wi ON wi.instrument_key = ts.instrument_key
WHERE wi.user_id = 1 AND ts.created_at >= now() - interval '7 days';
-- Result: 0 rows
```

### 2. Confirmed backend SSE stream is correct

Direct `curl` to port 8000 (bypassing Next.js proxy), authenticated as user 1:
```bash
timeout 38 curl -s -N \
  "http://localhost:8000/api/v1/ai/stream?instrument_key=NSE_EQ%7CINE296A01032&token=<jwt>&symbol=BAJFINANCE"
```

**Result — 4 events received within 100ms:**

| Event | t (ms) | explanation.available |
|---|---|---|
| 1 | 0 | null (pattern arrived first) |
| 2 | 0 | null (+ sentiment) |
| 3 | 100 | null (+ prediction) |
| 4 | 100 | **true** (`context_type: instrument_context`, full text present) |

Backend conclusion: **working correctly**. Stage 2 of `_fetch_explanation_for_instrument` finds the fresh `AIInstrumentContext` row, reads the richer payload from the Redis SSE event store (`cortex:sse:events:ctx:{instrument_key}`), and delivers it as `available: true` within ~100ms.

### 3. Identified Next.js proxy as the failure point

**Verbose curl through Next.js proxy (port 3000):**
```bash
timeout 8 curl -v -N \
  "http://localhost:3000/api/v1/ai/stream?instrument_key=NSE_EQ%7CINE296A01032&token=<jwt>"
```

**Result:** TCP connection established, GET request sent, **zero bytes of response received** in 8 seconds. Terminated with no output. Repeated with `--http1.1`, `--http2` — same result.

**HEAD request test (`curl -I`):**
```
HTTP/1.1 200 OK
content-type: text/event-stream
cache-control: no-cache, no-transform
x-accel-buffering: no
connection: keep-alive
vary: rsc, next-router-state-tree, next-router-prefetch, next-router-segment-prefetch
```

Headers are correct and arrive immediately for HEAD requests. The `vary: rsc` header reveals Next.js's RSC middleware is processing the response. For GET requests (which carry a body), the proxy hangs with no response at all.

**Proxy route under investigation:** `frontend/src/app/api/v1/ai/stream/route.ts`

```typescript
// Current implementation (broken)
backendResponse = await fetch(backendUrl.toString(), {
  method: 'GET',
  headers: forwardHeaders,   // no cache: 'no-store'
});
// ...
return new Response(backendResponse.body, {
  status: 200,
  headers: { 'Content-Type': 'text/event-stream', ... },
});
```

### 4. Traced the buffering through the frontend component tree

`useAnalysisStream` hook in `AnalysisCardsSection.tsx`:
```typescript
const [isInitialLoad, setIsInitialLoad] = useState(true);  // starts true

es.addEventListener('analysis_update', (e) => {
  // ...
  setIsInitialLoad(false);   // only flips when event arrives
  setIsConnected(true);
});
```

`AIExplanationPanel.tsx` guard:
```typescript
const isExplanationLoading = sseLoading && explanationData === null;
// sseLoading = isInitialLoad (stays true when proxy buffers)

// In AIExplanationPanel component:
if (isLoading && data === null) {
  return <PanelSkeleton />;   // renders indefinitely
}
```

Since the proxy never delivers `analysis_update` events, `isInitialLoad` remains `true`, `explanationData` remains `null`, and `PanelSkeleton` renders indefinitely.

### 5. Explained why prediction/pattern/sentiment show correctly

`AnalysisCardsSection.tsx` has React Query fallback pollers for all cards **except explanation**:
```typescript
// sseConnected = false (no events ever received from proxy)
// refetchInterval active when sseConnected = false:
predictionQuery  → REST poll every 60s
patternQuery     → REST poll every 300s
sentimentQuery   → REST poll every 300s
// explanationQuery → DOES NOT EXIST — SSE only
```

Prediction, pattern, and sentiment cards update correctly via REST polling. Explanation has no REST fallback.

### 6. Explained why explanation appeared once and then disappeared

During testing, a second concurrent SSE connection was opened directly to port 8000 for the same instrument. The combined byte volume of events across both connection's activity (including a large `full_explanation` text payload — several KB) pushed the proxy's internal write buffer past its flush threshold. All buffered events were released to the browser simultaneously, and the explanation appeared.

When the Detail Pane modal was closed, `AnalysisCardsSection` unmounted and the `EventSource` was torn down (`esRef.current.close()`). On reopening the modal, a new `EventSource` was created from a zero state. The second concurrent SSE connection was no longer running, so the buffer fill condition wasn't met within the user's observation window, and the skeleton returned.

---

## Root Cause

### Primary — Next.js 16 (Turbopack) buffers SSE response body

In Next.js 16's Turbopack dev server, the extended global `fetch` (which wraps Node.js's `undici` with caching and deduplication logic) buffers the response body before making it available for forwarding. For SSE streams — which never terminate — the buffer never reaches a "complete" state through normal operation. The proxy only flushes the buffer when accumulated byte volume exceeds an internal threshold (non-deterministic based on event sizes).

The `export const dynamic = 'force-dynamic'` pragma disables *page-level* caching but does not disable the fetch-level response buffering.

The `new Response(backendResponse.body, {...})` pattern in the route handler does not override this behaviour in the Turbopack runtime.

### Amplifier — No REST fallback for explanation

Explanation delivery is SSE-only. All other analysis cards (prediction, pattern, sentiment) have React Query fallback pollers that activate when `sseConnected = false`. Explanation has none. When the SSE proxy fails to deliver events, explanation is permanently absent with no recovery path.

### Amplifier — Modal lifecycle resets all SSE state

Closing and reopening the Detail Pane modal unmounts and remounts `AnalysisCardsSection`, creating a fresh `EventSource` and clearing all local SSE state. Any explanation that arrived via a buffer flush is lost, and the cycle starts over.

---

## Scope of Impact

| Card | Delivery | Works without SSE? |
|---|---|---|
| ML Pattern | SSE + REST fallback (60s) | Yes |
| AI Sentiment | SSE + REST fallback (300s) | Yes |
| Prediction Summary | SSE + REST fallback (300s) | Yes |
| AI Explanation | SSE only | **No — eternal skeleton** |

Instruments WITH an active trade suggestion (where `suggestion.llm_explanation` is set) are partially shielded: `suggestionExplanation` is seeded from the REST suggestion list response and shows content immediately without SSE. Instruments without recent trade suggestions (all 4 current watchlist items) have no alternative path.

---

## Files Involved

| File | Role |
|---|---|
| `frontend/src/app/api/v1/ai/stream/route.ts` | Next.js SSE proxy — broken body streaming |
| `frontend/src/components/AnalysisCardsSection.tsx` | SSE hook + explanation priority logic + missing REST fallback |
| `frontend/src/components/AIExplanationPanel.tsx` | Skeleton condition: `isLoading && data === null` |
| `backend/app/api/v1/ai_stream.py` | Backend SSE endpoint — working correctly |
| `backend/app/ai/intelligence/explanation_worker.py` | Context generation — working correctly |

---

## Fix Direction (not yet implemented)

Two changes required:

**1. Fix the SSE proxy to stream without buffering**  
Replace `new Response(backendResponse.body, {...})` with an explicit `ReadableStream` that pipes chunks through as they arrive, bypassing the Next.js fetch caching layer. Also add `cache: 'no-store'` to the `fetch()` call to opt out of the extended fetch wrapper:

```typescript
// fetch with explicit cache bypass
backendResponse = await fetch(backendUrl.toString(), {
  method: 'GET',
  headers: forwardHeaders,
  cache: 'no-store',
});

// Explicit ReadableStream pipe — does not go through Next.js buffer layer
const { readable, writable } = new TransformStream();
backendResponse.body!.pipeTo(writable);
return new Response(readable, {
  status: 200,
  headers: { 'Content-Type': 'text/event-stream', ... },
});
```

**2. Add a REST fallback for explanation**  
Add a React Query query for instrument context that polls the explanation endpoint when `sseConnected = false`, mirroring the pattern used by prediction/pattern/sentiment. This ensures explanation loads even if SSE is unavailable.
