# Hawk-Eye Radar — Known State & Issues

## 1. SuggestionStats commented out
`page.tsx:194` — `<SuggestionStats />` is commented out, so the stats dashboard is not rendering.

## 2. SuggestionDetailModal unused
`components/SuggestionDetailModal.tsx` exists but is never imported or used. `DetailPane` serves that purpose instead.

## 3. Frontend type mismatch (cursor vs offset pagination)
`SuggestionsListResponse` has `total`, `page`, `page_size` as optional/nullable (null in cursor mode).
The page reads `suggestionsData.total` and `suggestionsData.suggestions.length` directly — works only in offset mode. Needs guard for cursor mode.

## 4. Mock data endpoints
`GET /hawk-eye/analyze` and `GET /hawk-eye/fundamentals` return hardcoded mock data — not integrated with real indicators or data sources.

## 5. Multi-agent correlation engine not implemented
Planning doc (`Hawk-eye_new_Tasks.md`, April 21) describes a full event-driven correlation engine (Scanner → AI → ML → Consensus → Suggestion). The backend models/schemas/API exist, but the actual correlation/signal agents appear not yet built.

## 6. Backup files clutter
Multiple stale backup files exist in `frontend/src/app/hawk-eye-radar/`:
- `page_backup.tsx`
- `page-old-backup.tsx`
- `page-scanner-backup.tsx`
- `page_new.tsx`
