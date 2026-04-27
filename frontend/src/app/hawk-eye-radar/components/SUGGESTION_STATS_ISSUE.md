# SuggestionStats — Broken API Call

## Issue
`SuggestionStats.tsx:11` calls `tradeSuggestionsAPI.getStats()` which does not exist.
`tradeSuggestionsAPI` in `src/lib/api.ts` only exposes `getSuggestions`.
The component is currently commented out in `hawk-eye-radar/page.tsx:207` so there is no runtime impact, but the TypeScript compiler reports a hard error.

## Root cause
`getStats` was never added to `tradeSuggestionsAPI`. There is also no corresponding backend endpoint (`GET /api/v1/trade-suggestions/stats`).

## Fix options

### Option A — Implement the full feature (preferred)
1. Add a `GET /api/v1/trade-suggestions/stats` endpoint to the backend returning:
   ```json
   {
     "total_active": 12,
     "total_today": 34,
     "high_confidence_count": 5,
     "buy_count": 8,
     "sell_count": 4,
     "avg_consensus_score": 72.4,
     "avg_latency_ms": 210.0
   }
   ```
2. Add `getStats` to `tradeSuggestionsAPI` in `src/lib/api.ts`:
   ```ts
   getStats: async () => {
     return requestData(api.get('/trade-suggestions/stats'), 'Failed to fetch suggestion stats');
   },
   ```
3. Uncomment `<SuggestionStats />` in `hawk-eye-radar/page.tsx:207`.

### Option B — Remove the dead code (quick cleanup)
Delete `SuggestionStats.tsx` and remove its import from `hawk-eye-radar/page.tsx` if the stats feature is not planned.
