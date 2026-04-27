# Chart Data Fallback - Quick Reference

**Last Updated**: 2026-04-23  
**Component**: DetailPane (Hawk-Eye Radar)

---

## How It Works

### Data Fetching Strategy

```
┌─────────────────────────────────────────┐
│  1. Try Primary Source (Historical)     │
│     ↓                                    │
│  2. If empty for recent dates:          │
│     Try Fallback (Intraday)             │
│     ↓                                    │
│  3. If still empty:                      │
│     Show context-aware message          │
└─────────────────────────────────────────┘
```

---

## When Fallback Triggers

**Condition**: Historical API returns empty AND date range includes today

**Examples**:
- ✅ Pre-market hours (before 9:15 AM IST) - today's date
- ✅ Data ingestion delay - today's date
- ✅ Market holiday - today's date
- ❌ Historical date in past (no fallback needed)
- ❌ Historical data available (no fallback needed)

---

## Error Messages

### Pre-Market (before 9:15 AM IST)
```
Market hasn't opened yet (opens at 9:15 AM IST). 
Historical data for today will be available after market close. 
Previous trading day's data may be shown if available.
```

### Market Hours (9:15 AM - 3:30 PM IST)
```
Market is currently open. If you're seeing this message, 
there may be a data ingestion delay or this instrument 
may not be actively trading today.
```

### After Market (after 3:30 PM IST)
```
Market is closed. Today's data should be available shortly. 
If this persists, the instrument may not have traded today 
or there's a data ingestion delay.
```

### Other Times
```
No market data found for [SYMBOL] on [DATE]. 
This could be a market holiday or data ingestion delay.
```

---

## Debugging

### Console Logs to Check

```javascript
[DetailPane] Fetching candles: { instrument_key, mode, unit, interval, fromDate, toDate }
[DetailPane] Historical candles response: { ... }
[DetailPane] Historical empty for recent date, attempting intraday fallback
[DetailPane] Intraday fallback successful: X candles
[DetailPane] Intraday fallback also empty, no data available
[DetailPane] Intraday fallback failed: [error]
```

### Common Issues

**Issue**: Fallback not triggering
- **Check**: Is date range including today?
- **Check**: Is historical response actually empty?

**Issue**: Both APIs returning empty
- **Likely**: Market not open + no previous data
- **Action**: Check if market holiday or weekend

**Issue**: Fallback failing with error
- **Check**: Network connectivity
- **Check**: Authentication token valid
- **Action**: Review error logs

---

## Performance

### API Call Count

| Scenario | API Calls | Performance |
|----------|-----------|-------------|
| Historical data available | 1 | ✅ Optimal |
| Historical empty, intraday available | 2 | ✅ Acceptable |
| Both empty | 2 | ✅ Acceptable |

**Note**: Fallback only triggers when necessary (empty historical for recent dates)

---

## Testing Scenarios

### Quick Test Cases

1. **Pre-market (before 9:15 AM IST)**
   - Open chart for today
   - Should show previous day's data OR pre-market message

2. **Market hours (9:15 AM - 3:30 PM IST)**
   - Open chart for today
   - Should show live intraday data

3. **After market (after 3:30 PM IST)**
   - Open chart for today
   - Should show today's complete data

4. **Weekend**
   - Open chart for today
   - Should show Friday's data

5. **Historical date**
   - Open chart for past date (e.g., 2026-04-15)
   - Should show historical data (no fallback)

---

## Code Location

**File**: `frontend/src/app/hawk-eye-radar/components/DetailPane.tsx`

**Function**: `candlesMutation.mutationFn`

**Lines**: ~60-145 (fallback logic in HISTORICAL MODE section)

---

## Related Documentation

- **Full Implementation Guide**: `CHART_EMPTY_DATA_FIX_COMPLETE.md`
- **Context Report**: `CHART_EMPTY_DATA_CONTEXT_REPORT.md`
- **Original Issue**: `CHART_CANDLES_ERROR_REPORT.md`

---

## Support

**If users report "No data" issues**:

1. Check console logs for fallback attempts
2. Verify current IST time vs market hours
3. Check if market holiday or weekend
4. Verify instrument is actively trading
5. Check backend logs for API errors

**Common User Questions**:

Q: "Why am I seeing yesterday's data?"  
A: Market hasn't opened yet. We show previous day's data for analysis.

Q: "Why is the chart empty?"  
A: Could be market holiday, weekend, or data ingestion delay. Check the message for details.

Q: "When will today's data be available?"  
A: Historical data is available after market close (after 3:30 PM IST).

---

**Quick Reference Version**: 1.0  
**Last Updated**: 2026-04-23
