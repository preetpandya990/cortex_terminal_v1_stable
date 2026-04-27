# Watchlist Feature - Testing Checklist

## Prerequisites
1. **Database Migration**
   ```bash
   cd backend
   source .venv/bin/activate
   alembic upgrade head
   ```
   - Verify `watchlist_items` table created
   - Check constraints: `uq_user_instrument`, `uq_user_position`
   - Check indexes: `ix_watchlist_items_user_id`, `ix_watchlist_items_instrument_key`

2. **Backend Server**
   ```bash
   cd backend
   source .venv/bin/activate
   python -m uvicorn app.main:app --reload
   ```
   - Check logs for successful startup
   - Verify watchlist router registered: `/api/v1/watchlist`

3. **Frontend Server**
   ```bash
   cd frontend
   npm run dev
   ```
   - Check for compilation errors
   - Verify no TypeScript errors

## Test Cases

### 1. Authentication Flow
- [ ] **Unauthenticated User**
  - Navigate to dashboard
  - Search for a stock (e.g., "RELIANCE")
  - Click on search result
  - **Expected**: DetailPane opens WITHOUT "Add to Watchlist" button
  - Navigate to Hawk-Eye-Radar
  - **Expected**: Watchlist section NOT visible

- [ ] **Authenticated User**
  - Login with valid credentials
  - Navigate to dashboard
  - Search for a stock
  - Click on search result
  - **Expected**: DetailPane opens WITH "Add to Watchlist" button
  - Navigate to Hawk-Eye-Radar
  - **Expected**: Watchlist section visible (empty state initially)

### 2. Add to Watchlist (Dashboard)
- [ ] **Add Stock from Dashboard**
  - Login
  - Navigate to dashboard
  - Search for "RELIANCE"
  - Click on search result → DetailPane opens
  - Click "Add to Watchlist" button
  - **Expected**:
    - Button shows loading spinner
    - Button changes to "In Watchlist" with checkmark
    - No errors in console
  - Close DetailPane
  - Navigate to Hawk-Eye-Radar
  - **Expected**: RELIANCE appears in watchlist with live price

- [ ] **Duplicate Prevention**
  - Try adding the same stock again
  - **Expected**: Button already shows "In Watchlist"
  - Backend should return 409 Conflict if API called directly

### 3. Watchlist Display (Hawk-Eye-Radar)
- [ ] **Empty State**
  - Login with new user (no watchlist items)
  - Navigate to Hawk-Eye-Radar
  - **Expected**:
    - "My Watchlist" section visible
    - Empty state card with star icon
    - Message: "No Stocks in Watchlist"

- [ ] **Watchlist Cards**
  - Add 3-5 stocks to watchlist
  - Navigate to Hawk-Eye-Radar
  - **Expected**:
    - Cards displayed in grid (3 columns on desktop)
    - Each card shows:
      - Trading symbol (e.g., "RELIANCE")
      - Company name
      - LTP (Last Traded Price)
      - % change (green/red/neutral)
      - Previous close
      - Drag handle (grip icon)
      - Remove button (X icon)

### 4. Real-Time Price Updates
- [ ] **Live Prices**
  - Add stocks to watchlist
  - Navigate to Hawk-Eye-Radar
  - Wait 3-5 seconds
  - **Expected**:
    - Prices update automatically
    - % change updates
    - Color coding changes (green for positive, red for negative)
  - Check browser console
  - **Expected**: LTP fetch logs every 3 seconds

- [ ] **Price Caching**
  - Open browser DevTools → Network tab
  - Watch API calls
  - **Expected**: LTP API calls throttled (not more than once per 5 seconds per stock)

### 5. Remove from Watchlist
- [ ] **Remove via Card**
  - Navigate to Hawk-Eye-Radar
  - Click X button on a watchlist card
  - **Expected**:
    - Card disappears immediately
    - No errors in console
    - If last item removed, empty state appears

- [ ] **Remove via DetailPane**
  - Add stock to watchlist from dashboard
  - Navigate to Hawk-Eye-Radar
  - Click on watchlist card → DetailPane opens
  - **Expected**: DetailPane shows "In Watchlist" button (not "Add to Watchlist")
  - Note: Remove from DetailPane only works on dashboard, not Hawk-Eye-Radar

### 6. View Details
- [ ] **Click Watchlist Card**
  - Navigate to Hawk-Eye-Radar
  - Click on a watchlist card (not the X button)
  - **Expected**:
    - DetailPane opens
    - Chart loads for selected stock
    - Analysis cards visible (showAnalysis=true on Hawk-Eye-Radar)
    - URL updates with `?instrument_key=...`

- [ ] **Deep Linking**
  - Copy URL with instrument_key parameter
  - Open in new tab
  - **Expected**: DetailPane opens automatically with correct stock

### 7. Persistence
- [ ] **Cross-Device Sync**
  - Add stocks to watchlist on one browser
  - Login on different browser/device
  - **Expected**: Same watchlist appears

- [ ] **Refresh Persistence**
  - Add stocks to watchlist
  - Refresh page (F5)
  - **Expected**: Watchlist persists

- [ ] **Logout/Login**
  - Add stocks to watchlist
  - Logout
  - Login again
  - **Expected**: Watchlist persists

### 8. Error Handling
- [ ] **Network Errors**
  - Disconnect internet
  - Try adding to watchlist
  - **Expected**: Error message in console, button returns to normal state

- [ ] **Backend Down**
  - Stop backend server
  - Try adding to watchlist
  - **Expected**: Graceful error handling, no crashes

- [ ] **Invalid Token**
  - Expire access token
  - Try adding to watchlist
  - **Expected**: 401 error, redirect to login

### 9. Performance
- [ ] **Large Watchlist**
  - Add 20+ stocks to watchlist
  - Navigate to Hawk-Eye-Radar
  - **Expected**:
    - Page loads quickly
    - No lag when scrolling
    - Price updates don't cause jank

- [ ] **Memory Leaks**
  - Add stocks to watchlist
  - Navigate between pages multiple times
  - Check browser DevTools → Memory
  - **Expected**: No memory leaks, proper cleanup

### 10. UI/UX Polish
- [ ] **Loading States**
  - All buttons show loading spinners during mutations
  - Skeleton loaders during initial fetch

- [ ] **Hover Effects**
  - Cards have hover shadow
  - Buttons have hover states
  - Drag handle changes cursor

- [ ] **Responsive Design**
  - Test on mobile (< 768px)
  - Test on tablet (768px - 1024px)
  - Test on desktop (> 1024px)
  - **Expected**: Grid adjusts (1 col → 2 col → 3 col)

- [ ] **Accessibility**
  - Tab navigation works
  - Screen reader announces changes
  - Proper ARIA labels

## Known Limitations (Document for User)
1. **Reordering**: Drag-and-drop not implemented yet (UI shows grip handle but no functionality)
2. **Bulk Operations**: No "Remove All" or "Add Multiple" features
3. **Watchlist Limit**: No limit enforced (consider adding max 50 items)
4. **Price Source**: Uses Upstox API (requires valid token)
5. **Market Hours**: Prices only update during market hours (9:15 AM - 3:30 PM IST)

## Success Criteria
- ✅ All test cases pass
- ✅ No console errors
- ✅ No TypeScript errors
- ✅ No memory leaks
- ✅ Smooth 60fps animations
- ✅ < 100ms response time for mutations
- ✅ < 3s initial load time
- ✅ Works on Chrome, Firefox, Safari, Edge

## Rollback Plan
If critical issues found:
1. Remove watchlist router from `backend/app/main.py`
2. Revert migration: `alembic downgrade -1`
3. Remove watchlist section from Hawk-Eye-Radar page
4. Remove "Add to Watchlist" button from DetailPane
