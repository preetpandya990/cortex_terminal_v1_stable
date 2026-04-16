# Development Environment Scripts

Quick start scripts for running Cortex AI frontend and backend with live error monitoring.

## 🚀 Quick Start

### Option 1: Tmux (Recommended - Best for monitoring)

```bash
./start-dev.sh
```

**Features:**
- Split-screen view with 4 panes
- Live error filtering for both services
- All logs saved to `/tmp/cortex-*.log`
- Easy navigation between panes

**Layout:**
```
┌─────────────────┬─────────────────┐
│  Backend (API)  │  Frontend (UI)  │
│  Port: 8000     │  Port: 3000     │
├─────────────────┼─────────────────┤
│ Backend Errors  │ Frontend Errors │
│ (filtered logs) │ (filtered logs) │
└─────────────────┴─────────────────┘
```

**Tmux Commands:**
- `Ctrl+B` then `Arrow Keys` - Navigate between panes
- `Ctrl+B` then `D` - Detach (keeps running in background)
- `tmux attach -t cortex-dev` - Re-attach to session
- `Ctrl+B` then `[` - Scroll mode (use arrow keys, `q` to exit)
- `Ctrl+B` then `z` - Zoom current pane (toggle)

**Stop:**
```bash
./stop-dev.sh
```

---

### Option 2: Simple (Separate Terminal Windows)

```bash
./start-dev-simple.sh
```

**Features:**
- Opens 2 separate terminal tabs
- One for backend, one for frontend
- Simpler but less monitoring

**Requirements:** `gnome-terminal` or `xterm`

---

## 🏥 Health Check

Check if services are running:

```bash
./health-check.sh
```

**Output:**
```
🏥 Cortex AI Health Check
==========================

Backend (http://localhost:8000): ✅ Running
  Version: 1.0.0

Frontend (http://localhost:3000): ✅ Running

Database Connection: ✅ Connected

Redis Connection: ✅ Connected
```

---

## 🔗 Service URLs

| Service | URL | Description |
|---------|-----|-------------|
| Frontend | http://localhost:3000 | Next.js UI |
| Backend API | http://localhost:8000 | FastAPI Server |
| API Docs | http://localhost:8000/docs | Swagger UI |
| ReDoc | http://localhost:8000/redoc | Alternative API docs |
| Metrics | http://localhost:8000/metrics | Prometheus metrics |

---

## 📋 Log Files

When using `start-dev.sh`, logs are saved to:

- Backend: `/tmp/cortex-backend.log`
- Frontend: `/tmp/cortex-frontend.log`

**View full logs:**
```bash
# Backend
tail -f /tmp/cortex-backend.log

# Frontend
tail -f /tmp/cortex-frontend.log

# Only errors
tail -f /tmp/cortex-backend.log | grep -E "ERROR|Exception"
```

---

## 🛠️ Troubleshooting

### Backend won't start

**Check Python environment:**
```bash
cd backend
source venv/bin/activate  # or source ../.venv/bin/activate
python --version  # Should be 3.11+
```

**Check dependencies:**
```bash
cd backend
pip install -r requirements.txt
```

**Check database:**
```bash
# Ensure PostgreSQL is running
sudo systemctl status postgresql

# Check connection
psql -U postgres -c "SELECT 1"
```

**Check Redis:**
```bash
# Ensure Redis is running
sudo systemctl status redis

# Check connection
redis-cli ping  # Should return PONG
```

---

### Frontend won't start

**Check Node.js:**
```bash
node --version  # Should be 18+
npm --version
```

**Install dependencies:**
```bash
cd frontend
npm install
```

**Clear cache:**
```bash
cd frontend
rm -rf .next node_modules
npm install
```

---

### Port already in use

**Backend (8000):**
```bash
# Find process
lsof -i :8000

# Kill process
kill -9 $(lsof -t -i :8000)
```

**Frontend (3000):**
```bash
# Find process
lsof -i :3000

# Kill process
kill -9 $(lsof -t -i :3000)
```

---

### Tmux not installed

```bash
# Ubuntu/Debian
sudo apt-get update && sudo apt-get install -y tmux

# Fedora/RHEL
sudo dnf install tmux

# macOS
brew install tmux
```

---

## 🔧 Manual Start (Without Scripts)

### Backend
```bash
cd backend
source venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Frontend
```bash
cd frontend
npm run dev
```

---

## 📊 Monitoring Tips

### Watch for specific errors

**Backend:**
```bash
tail -f /tmp/cortex-backend.log | grep -E "ERROR|CRITICAL|Exception|Traceback"
```

**Frontend:**
```bash
tail -f /tmp/cortex-frontend.log | grep -E "Error|Failed|Warning"
```

### Monitor API requests

```bash
tail -f /tmp/cortex-backend.log | grep -E "GET|POST|PUT|DELETE"
```

### Monitor database queries

```bash
tail -f /tmp/cortex-backend.log | grep -E "SELECT|INSERT|UPDATE|DELETE"
```

---

## 🎯 Development Workflow

1. **Start services:**
   ```bash
   ./start-dev.sh
   ```

2. **Check health:**
   ```bash
   ./health-check.sh
   ```

3. **Make changes** - Both services auto-reload on file changes

4. **Monitor errors** - Watch the bottom panes for any issues

5. **Stop services:**
   ```bash
   ./stop-dev.sh
   # or Ctrl+C in each pane
   ```

---

## 🚨 Common Errors

### "Address already in use"
- Another instance is running
- Use `./stop-dev.sh` or kill processes manually

### "Module not found"
- Backend: `pip install -r requirements.txt`
- Frontend: `npm install`

### "Database connection failed"
- Check PostgreSQL is running: `sudo systemctl start postgresql`
- Check credentials in `.env`

### "Redis connection failed"
- Check Redis is running: `sudo systemctl start redis`
- Check Redis URL in `.env`

### "Permission denied"
- Make scripts executable: `chmod +x *.sh`

---

## 📝 Notes

- Backend runs with `--reload` flag (auto-restart on code changes)
- Frontend runs in dev mode (hot reload enabled)
- Logs are color-coded in tmux for better visibility
- Error panes only show ERROR/Exception lines for cleaner monitoring
- All services run in foreground for easy debugging

---

**Need help?** Check the logs in `/tmp/cortex-*.log` or run `./health-check.sh`
