# Chart Candles Loading Error - Context Report

**Date**: 2026-04-23  
**Issue**: Chart shows "Failed to load candles" and "No candle data available"  
**Status**: 🔍 INVESTIGATION COMPLETE

---

## Problem Summary

After fixing the DetailPane to open correctly, the chart now displays but shows two error messages:
1. **"Failed to load candles."** (red error box)
2. **"No candle data available."** (gray text)

This indicates the API call to fetch candles is failing.

---

## Data Flow Analysis

### Current Flow
```
DetailPane opens
  ↓
useEffect triggers candlesMutation.mutate()
  ↓
candlesMutation calls upstoxAPI.getIntradayCandles() or getHistoricalCandles()
  ↓
API call fails (candlesMutation.isError = true)
  ↓
Shows "Failed to load candles"
  ↓
candles.data.candles is empty or null
  ↓
Shows "No candle data available"
```

### Expected Flow
```
DetailPane opens
  ↓
useEffect triggers candlesMutation.mutate()
  ↓
API call succeeds
  ↓
candles.data.candles contains array of candles
  ↓
Chart renders with candles
```

---

## Component Analysis

### DetailPane Component
**Location**: `frontend/src/app/hawk-eye-radar/components/DetailPane.tsx`

**Key Logic**:

1. **Default Historical State**:
```typescript
const [historicalState] = useState<HistoricalState>(() => getDefaultHistoricalState());

// From chart-policy.ts
export function getDefaultHistoricalState(referenceDay?: string): HistoricalState {
  const defaultTradingDay = referenceDay ?? getLatestTradingDayYMD();
  return {
    unit: "minutes",
    interval: 1,
    fromDate: defaultTradingDay,  // Today's trading day
    toDate: defaultTradingDay,    // Today's trading day
    presetId: DEFAULT_HISTORICAL_PRESET_ID,
    isCustom: false,
  };
}
```

2. **Candle Source Mode Resolution**:
```typescript
const selectedRangeDays = diffDaysInclusive(fromDate, toDate);  // = 1 (same day)
const candleSourceMode = resolveCandleSourceMode(selectedRangeDays, candleUnit, fromDate, toDate);

// From chart-policy.ts
export function resolveCandleSourceMode(
  rangeDays: number,
  unit: CandleUnit,
  fromDate: string,
  toDate: string
): CandleSourceMode {
  if (shouldUseIntradaySource(rangeDays, unit, fromDate, toDate)) {
    return "intraday";  // ← This is what gets returned
  }
  // ...
}

function shouldUseIntradaySource(
  rangeDays: number,
  unit: CandleUnit,
  fromDate: string,
  toDate: string
): boolean {
  return (
    rangeDays === 1 &&  // ✅ True (same day)
    (unit === "minutes" || unit === "hours") &&  // ✅ True (minutes)
    includesToday(fromDate, toDate)  // ✅ True (today)
  );
}
```

**Result**: `candleSourceMode = "intraday"`

3. **API Call**:
```typescript
const candlesMutation = useMutation<UpstoxCandlesResponse, Error, void>({
  mutationFn: async (): Promise<UpstoxCandlesResponse> => {
    if (candleSourceMode === "intraday") {
      return upstoxAPI.getIntradayCandles(instrument.instrument_key, {
        unit: candleUnit,      // "minutes"
        interval: candleInterval,  // 1
      });
    }
    // ...
  },
  onSuccess: (data: UpstoxCandlesResponse) => {
    setCandles(data);
  },
});
```

---

## API Call Details

### Frontend API Call
**Function**: `upstoxAPI.getIntradayCandles()`  
**Location**: `frontend/src/lib/api.ts`

```typescript
getIntradayCandles: async (
  instrumentKey: string,
  params: { unit: string; interval: number }
): Promise<UpstoxCandlesResponse> => {
  const response = await api.get('/upstox/candles/intraday', {
    params: { instrument_key: instrumentKey, ...params },
  });
  return response.data;
},
```

**Actual Request**:
```
GET /api/v1/upstox/candles/intraday?instrument_key=<key>&unit=minutes&interval=1
```

### Instrument Data Being Passed

From `detailInstrument` construction:
```typescript
const detailInstrument = selectedInstrument || (detailSuggestion ? {
  instrument_key: detailSuggestion.instrument_key,  // e.g., "NSE_EQ|INE002A01018"
  trading_symbol: detailSuggestion.trading_symbol || detailSuggestion.symbol,  // e.g., "RELIANCE-EQ" or "RELIANCE"
  name: detailSuggestion.symbol,  // e.g., "RELIANCE"
  exchange: "NSE",  // Hardcoded
} as UpstoxInstrument : null);
```

---

## Potential Issues

### Issue 1: Authentication Required
**Likelihood**: HIGH

The candles API endpoint likely requires authentication (same as trade suggestions).

**Evidence**:
- Trade suggestions API requires auth (we saw 401 errors)
- Upstox API endpoints typically require auth
- Error message is generic "Failed to load candles"

**Check**:
- Is the auth token being sent with the request?
- Does the backend endpoint require authentication?

### Issue 2: Backend Endpoint Not Implemented
**Likelihood**: MEDIUM

The backend endpoint `/api/v1/upstox/candles/intraday` might not exist or not be working.

**Check**:
- Does the backend have this endpoint?
- Is it properly configured?
- Is the Upstox service running?

### Issue 3: Invalid Instrument Key Format
**Likelihood**: LOW

The `instrument_key` from TradeSuggestion might not be in the correct format for Upstox API.

**Expected Format**: `NSE_EQ|INE002A01018` (exchange_segment|isin)

**Check**:
- What does `detailSuggestion.instrument_key` actually contain?
- Is it in the correct format?

### Issue 4: Market Hours / No Data Available
**Likelihood**: LOW

If market is closed or it's a weekend, intraday data might not be available.

**Check**:
- What time is it in IST?
- Is the market open?
- Is it a trading day?

### Issue 5: CORS or Network Error
**Likelihood**: LOW

The request might be blocked by CORS or network issues.

**Check**:
- Browser console network tab
- Any CORS errors?
- Request reaching backend?

---

## Debug Information Needed

To diagnose the exact issue, we need to check:

### 1. Browser Console
```javascript
// Check for errors
console.error logs

// Check network requests
Network tab → Filter by "candles"
  - Request URL
  - Request headers (Authorization?)
  - Response status code
  - Response body
```

### 2. Backend Logs
```bash
# Check if request is reaching backend
docker-compose logs backend | grep "candles"

# Check for errors
docker-compose logs backend | grep -i "error"
```

### 3. Instrument Key Value
```javascript
// In DetailPane, add console.log
console.log('Instrument:', instrument);
console.log('Instrument Key:', instrument.instrument_key);
```

### 4. API Response
```javascript
// In DetailPane, add error logging
const candlesMutation = useMutation<UpstoxCandlesResponse, Error, void>({
  mutationFn: async (): Promise<UpstoxCandlesResponse> => {
    console.log('Fetching candles for:', instrument.instrument_key);
    console.log('Mode:', candleSourceMode);
    console.log('Unit:', candleUnit, 'Interval:', candleInterval);
    
    try {
      const result = await upstoxAPI.getIntradayCandles(instrument.instrument_key, {
        unit: candleUnit,
        interval: candleInterval,
      });
      console.log('Candles response:', result);
      return result;
    } catch (error) {
      console.error('Candles error:', error);
      throw error;
    }
  },
  onSuccess: (data: UpstoxCandlesResponse) => {
    console.log('Candles loaded:', data.data?.candles?.length, 'candles');
    setCandles(data);
  },
  onError: (error: Error) => {
    console.error('Mutation error:', error);
  },
});
```

---

## Most Likely Root Causes (Ranked)

### 1. Authentication Not Configured (90% probability)
**Symptom**: 401 Unauthorized response  
**Cause**: Auth token not being sent with candles API request  
**Fix**: Ensure axios interceptor adds Authorization header

### 2. Backend Endpoint Issue (70% probability)
**Symptom**: 404 Not Found or 500 Internal Server Error  
**Cause**: Backend endpoint not implemented or broken  
**Fix**: Check backend route configuration

### 3. Upstox Service Not Running (50% probability)
**Symptom**: Backend returns error about Upstox service  
**Cause**: Upstox client not initialized or API key missing  
**Fix**: Check backend Upstox service configuration

### 4. Invalid Instrument Key (30% probability)
**Symptom**: Backend returns "Invalid instrument" error  
**Cause**: instrument_key format incorrect  
**Fix**: Validate instrument_key format

### 5. Market Closed / No Data (20% probability)
**Symptom**: Empty candles array but no error  
**Cause**: Market closed, no intraday data available  
**Fix**: Fallback to historical data or show appropriate message

---

## Backend Endpoint Check

### Expected Backend Route
```python
# backend/app/api/v1/upstox.py or similar

@router.get("/candles/intraday")
async def get_intraday_candles(
    instrument_key: str,
    unit: str,
    interval: int,
    user_id: str = Depends(get_current_user_id),  # ← Authentication required?
):
    # Fetch candles from Upstox
    # Return UpstoxCandlesResponse
```

**Questions**:
1. Does this endpoint exist?
2. Does it require authentication?
3. Is the Upstox client configured?
4. Are there any rate limits?

---

## API Response Structure

### Expected Response
```typescript
interface UpstoxCandlesResponse {
  status: "success" | "error";
  data: {
    candles: UpstoxCandle[];  // Array of [timestamp, open, high, low, close, volume, oi]
  };
}

type UpstoxCandle = [string, number, number, number, number, number, number];
// [timestamp, open, high, low, close, volume, open_interest]
```

### Error Response
```typescript
{
  status: "error",
  message: "Error message here",
  code: "ERROR_CODE"
}
```

---

## Next Steps for Diagnosis

1. **Check Browser Console**:
   - Open DevTools → Console
   - Look for error messages
   - Check Network tab for failed requests

2. **Check Backend Logs**:
   ```bash
   docker-compose logs backend --tail=100 | grep -i "candles\|error"
   ```

3. **Verify Authentication**:
   - Check if Authorization header is present in request
   - Verify token is valid

4. **Test Backend Endpoint Directly**:
   ```bash
   curl -H "Authorization: Bearer <token>" \
     "http://localhost:8000/api/v1/upstox/candles/intraday?instrument_key=NSE_EQ|INE002A01018&unit=minutes&interval=1"
   ```

5. **Check Instrument Key**:
   - Add console.log in DetailPane
   - Verify format is correct

---

## Temporary Workaround

If authentication is the issue, we can temporarily disable auth for the candles endpoint in development:

```python
# backend/app/api/v1/upstox.py

@router.get("/candles/intraday")
async def get_intraday_candles(
    instrument_key: str,
    unit: str,
    interval: int,
    # user_id: str = Depends(get_current_user_id),  # ← Comment out for dev
):
    # ...
```

---

**Status**: 🔍 AWAITING DEBUG INFORMATION  
**Priority**: HIGH (Core feature broken)  
**Complexity**: Medium (likely auth or backend config issue)

---

## Information Required

Please provide:
1. **Browser console errors** (screenshot or copy/paste)
2. **Network tab** - candles request details (status code, response)
3. **Backend logs** - any errors related to candles
4. **Instrument key value** - what's being passed to the API

This will help pinpoint the exact issue and implement the correct fix.
