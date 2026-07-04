# AI Explanation Panel Stuck on Skeleton — Root Cause & Fix

**Date:** 2026-07-03
**Status:** Fixed and verified end-to-end (uncommitted)
**Files changed:** `frontend/src/app/api/v1/ai/stream/route.ts`, `frontend/package.json`, `package-lock.json`

## Symptom

The AI Explanation panel (in the trade-suggestion / watchlist "View details" modal) stayed on the "Generating…" skeleton indefinitely, even after the backend had successfully generated and stored the explanation/context. A hard refresh sometimes appeared to "fix" it (by falling through to the REST fallback poll), which made the bug look intermittent.

## Investigation trail

1. **Backend health check** — confirmed via Redis Streams (`cortex-explanation-workers` consumer group), 0 pending entries, 0 lag, no stale `instrument_context:generating:*` locks, no tripped Gemini circuit breakers, healthy RPD usage. Backend was not the problem.
2. **DB check** — `ai_instrument_context` rows were being written correctly, including a fresh on-demand generation triggered by opening a modal (confirmed via logs: `context_worker` processed the job, wrote the row, and logged `SSE push: instrument context ready` on the *same* SSE connection that requested it).
3. **Browser reproduction (Playwright + Google Chrome, installed for this investigation)** — logged in via the dev-login bypass, opened a live trade-suggestion modal, and captured the SSE connection via Chrome DevTools Protocol (`Network.eventSourceMessageReceived`). Result: the SSE request opened, but **zero frames arrived in the browser**, ever — not even the initial snapshot event that fires immediately.
4. **Raw client tests** — bypassing the browser entirely:
   - `curl` directly against the backend (port 8000): streams data instantly.
   - `curl` / Node `http.get()` through the Next.js proxy (port 3000): **hangs indefinitely**, no headers, no bytes, ever.
   - Ruled out: stale dev-server build (restarted cleanly, still hung), Turbopack-specific bug (reproduced identically under `next dev --webpack`), the `/api/v1/[...path]` catch-all route shadowing the dedicated SSE route (temporarily removed it, still hung).
5. **Isolated to the upstream HTTP client** — Node's own `fetch()` (undici), called directly against the backend from a plain Node script:
   - Got response **headers** in ~50ms.
   - `response.body.getReader().read()` (and `for await` iteration) **never resolved a single chunk**, even after 6–8 seconds, despite the backend continuously sending chunked SSE data.
   - The identical request via Node's raw `http.get()` module streamed chunks immediately (~20ms).
   - The identical request via undici's low-level `request()` API also streamed chunks immediately (~20ms).

## Root cause

`route.ts`'s upstream call to the backend used the global WHATWG `fetch()` API. In Node.js, `fetch()` is backed by undici's WHATWG-spec-compliant implementation, which:

- Negotiates `Accept-Encoding: gzip, deflate, br` by default (unless overridden), and
- Routes the response body through a decompression-aware `ReadableStream` bridge, regardless of whether the response is actually compressed.

For the backend's response — chunked transfer-encoding, **no** `Content-Encoding` header, delivered in small, irregular SSE keep-alive frames — that bridge never handed a single chunk to the stream reader, even though the raw TCP bytes were flowing correctly over the socket. Since the proxy's `Response` to the browser can't flush a byte until the first chunk is written into its `TransformStream` (by design, to enable early-return header flushing), and that first chunk depended on the hung internal read, the entire proxy stalled forever — the browser received nothing, not even a 200 status line.

This was **not** the same bug as the previously-documented (and already-committed, in `6f0f7b4`) `await fetch(...)`-blocks-`GET()`-return issue, nor the gzip-middleware issue, nor the undici-fetch-cache issue — those three were already correctly fixed. This is a fourth, previously undiscovered defect in the same file: undici's `fetch()` body-delivery pipeline itself hanging for this specific class of response (chunked, uncompressed, small/irregular frames).

## Fix

Replaced the upstream call in `route.ts` from the global `fetch()` to undici's low-level `request()` API — which bypasses the WHATWG compression-negotiation/decompression layer entirely and is undici's own documented recommendation for server-side proxying (as opposed to `fetch()`, which is meant for spec-compliant client code).

Implementation details:
- `request()`'s `body` is a Node.js `Readable` (`BodyReadable`), not a Web `ReadableStream` — bridged via Node core's `Readable.toWeb()` (stable, no new dependency) into a real `ReadableStream` before entering the same `getReader()` read loop the code already had. No other structural change — same `TransformStream`-based early-return-response architecture, same abort/cleanup guarantees via `request.signal`.
- Non-2xx backend responses now check `statusCode` instead of `.ok`, and read the error body via `BodyReadable`'s built-in `.text()` convenience method.
- Added `undici@^7.28.0` as an explicit, pinned `dependencies` entry in `frontend/package.json`. This dedupes against the copy already present transitively via `jsdom` (a test-only devDependency) — no new package was actually introduced into the tree, and no new vulnerabilities were flagged by `npm audit`.
- **Pinned to the 7.x line deliberately, not the newer 8.x line:** production ships on `node:20-alpine` (`frontend/Dockerfile`), and undici 8.x requires Node ≥22.19.0. Using 8.x would have worked on this dev box (Node 24) but silently broken in the actual production container.

## Verification

- `npx tsc --noEmit`: no errors in the changed file (confirmed 41 pre-existing, unrelated test-fixture TS errors exist on the base branch before this change too — not introduced by this fix).
- `npx eslint` on the changed file: clean.
- Restarted the frontend dev server cleanly (previous process was terminated and relaunched under both Turbopack and Webpack during diagnosis, confirmed neither was the cause).
- Raw Node `http.get()` through the fixed proxy: headers + first chunk in ~113–115ms (previously: infinite hang, tested up to 8s+).
- Playwright driving a real, installed Google Chrome instance: confirmed live `analysis_update` SSE frames (with real prediction/pattern/sentiment/explanation payloads) arriving in-browser within ~200–300ms of the connection opening.

## Flagged for follow-up (not fixed in this pass — separate scope)

1. **`frontend/src/app/api/v1/scanner/stream/route.ts`** has an identical `fetch()`-based upstream-proxying pattern for its own SSE stream (scanner run progress) and is very likely exposed to the same defect. Not touched here since it's a different feature; recommend an equivalent audit/fix pass.
2. **Production base image is past EOL:** `frontend/Dockerfile` pins `node:20-alpine`; Node 20 LTS reached end-of-life in April 2026 (current date: July 2026). Unrelated to this bug, but a pre-existing production risk worth scheduling a runtime upgrade for (would also unlock undici 8.x and its documented streaming/chunked-response edge-case fixes).
3. **RPD status at time of this fix:** 10 Gemini `generate` calls used today, 0 tripped circuit breakers — healthy, with headroom for the explanation-quality refinement work that this investigation kept interrupting.

## Diagnostic tooling note

Playwright was installed for this investigation (`npm install playwright` in a scratch directory, using the already-installed system Google Chrome via `channel: 'chrome'` rather than downloading a bundled Chromium) — not added to the project's own `package.json`/dependencies. No permanent project changes from the diagnostic tooling itself.
