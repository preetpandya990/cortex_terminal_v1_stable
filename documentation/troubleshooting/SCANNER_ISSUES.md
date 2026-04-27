# Market Scanner — Known Issues

## Issue 1: `trade_date` param ignored by backend
- **Files:** `backend/app/api/v1/scanner.py`, `backend/app/schemas/scanner.py`
- Frontend sends `trade_date` for market_close scans. Backend accepts it in `RunScanRequest` schema but never reads it in the handler.
- Historical scan replay is not implemented.

## Issue 2: `scannerAPI` is dead code
- **File:** `frontend/src/lib/api.ts` (lines 286–319)
- `scannerAPI` export defines `getLatestScan()`, `getContext()`, `runScan()` but is never imported anywhere.
- The page bypasses it and calls `api.get()`/`api.post()` directly.
- Also sends unsupported `include_ml: 'true'` query param that the backend ignores.

## Issue 3: Stale data warnings not surfaced to UI
- **Files:** `backend/app/services/market_scanner.py`, `backend/app/schemas/scanner.py`
- Backend adds `STALE_DATA:{age}d` warnings to raw `ScanResult` objects.
- `LatestScanResponse` does not propagate these warnings — users see stale data with no indicator.

## Issue 4: GET `/scanner/run` endpoint unused
- **File:** `backend/app/api/v1/scanner.py`
- Endpoint exists with `timeframe` and `force_refresh` params, but is never called from the frontend.
- Purpose is unclear relative to POST `/scanner/run`.

## Issue 5: Upstox token failure is silent
- **File:** `backend/app/services/market_scanner.py` (lines 195–204)
- If no Upstox token, scanner silently falls back to DB close prices with only a server-side log.
- No user-visible indicator that live prices are unavailable.

## Issue 6: Orphaned backup file
- **File:** `frontend/src/app/hawk-eye-radar/page-scanner-backup.tsx`
- Unused multi-timeframe scanner prototype. Should be deleted or promoted.

## Issue 7: Type naming inconsistency
- **File:** `frontend/src/types/market.ts`
- Frontend interface is named `ScanResults` but maps to backend's `LatestScanResponse`.
- Works at runtime (structures match) but is misleading.
