# 🚀 Data Ingestion Worker - Quick Start

**Status:** ✅ Ready to Deploy  
**Time Required:** 5 minutes

---

## ⚡ 3-Step Deployment

### **Step 1: Install (30 seconds)**
```bash
cd backend
pip install watchdog==4.0.0
```

### **Step 2: Test (2 minutes)**
```bash
python scripts/test_data_ingestion.py
```

**Expected:**
```
✅ Database connection successful
✅ Found 2415 NSE instruments
✅ Upstox API connection successful
✅ Gap detection working
✅ Configuration valid
✅ All tests passed!
```

### **Step 3: Deploy (1 minute)**
```bash
python -m app.worker
```

**Expected:**
```
🚀 Data Ingestion Worker Starting
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Check interval: 3600s
Rate limit: 12s between requests
Recent window: 30 days
Backfill enabled: True
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔍 Running initial scan (recent data)...
📊 Detected 14490 data gaps
📈 Processing 14490 gaps...
✅ [RELIANCE] 1hour | 2026-03-18 to 2026-04-16 | 150 candles
✅ [TCS] 1hour | 2026-03-18 to 2026-04-16 | 150 candles
...
```

---

## 📊 Monitor Progress

```bash
# Watch database growth (updates every minute)
watch -n 60 'psql $DATABASE_URL -c "SELECT timeframe, COUNT(*) FROM upstox_ohlcv GROUP BY timeframe;"'
```

---

## 🚨 Daily Task: Token Update

**When:** ~3:30 AM daily  
**Alert:** You'll see this in logs:
```
🚨 UPSTOX TOKEN EXPIRED 🚨
Action Required: Update UPSTOX_ACCESS_TOKEN in .env file
```

**Action:**
1. Get new token from Upstox dashboard
2. Update `backend/.env`: `UPSTOX_ACCESS_TOKEN=<new_token>`
3. Save file → Worker auto-resumes in 10 seconds

---

## 📈 Expected Results

| Timeframe | After 24h | After 7d | After 30d |
|-----------|-----------|----------|-----------|
| 1 hour    | 50%       | 90%      | 100%      |
| 1 day     | 80%       | 100%     | 100%      |
| 30 minute | 30%       | 70%      | 100%      |
| 15 minute | 20%       | 60%      | 100%      |
| 5 minute  | 10%       | 40%      | 90%       |
| 1 minute  | 5%        | 30%      | 80%       |

**Total Candles:** 40k → 280k → 2-5M

---

## 🐛 Troubleshooting

### Worker not starting?
```bash
# Check configuration
python -c "from app.core.config import get_settings; print(get_settings().DATA_INGESTION_ENABLED)"
```

### No data being ingested?
```bash
# Check logs
tail -100 logs/worker.log | grep "data_ingestion"
```

### Token expired?
```bash
# Update .env file
nano backend/.env
# Change: UPSTOX_ACCESS_TOKEN=<new_token>
# Save and exit → Worker auto-resumes
```

---

## 📚 Full Documentation

- **Implementation Summary:** `DATA_INGESTION_IMPLEMENTATION_SUMMARY.md`
- **Deployment Checklist:** `DATA_INGESTION_DEPLOYMENT_CHECKLIST.md`
- **Executive Summary:** `DATA_INGESTION_EXECUTIVE_SUMMARY.md`

---

**That's it!** 🎉 Worker is now running and ingesting data automatically.
