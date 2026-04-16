# Cortex AI - Manual Startup Instructions

## Terminal 1: Backend (FastAPI)

```bash
cd /home/preet/code/Cortex_Merge_AI-ML/backend
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

**Backend will be available at:**
- API: http://localhost:8000
- Swagger Docs: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

---

## Terminal 2: Frontend (Next.js)

```bash
cd /home/preet/code/Cortex_Merge_AI-ML/frontend
npm run dev
```

**Frontend will be available at:**
- App: http://localhost:3000

---

## Quick Health Check

Once both are running, test:

```bash
# Test backend
curl http://localhost:8000/health

# Test frontend
curl http://localhost:3000
```

---

## To Stop

- Press `Ctrl+C` in each terminal

---

## Docker Services (should already be running)

Check if PostgreSQL and Redis are running:
```bash
docker ps
```

If not running:
```bash
cd /home/preet/code/Cortex_Merge_AI-ML
docker-compose up -d
```

---

## Troubleshooting

**Backend won't start:**
- Check if port 8000 is free: `lsof -i :8000`
- Check database connection in `.env`
- View logs in the terminal

**Frontend won't start:**
- Check if port 3000 is free: `lsof -i :3000`
- Check `.env.local` configuration
- Try: `rm -rf .next && npm run dev`

**Database connection issues:**
- Ensure Docker services are running
- Check DATABASE_URL in `backend/.env`
- Default: `postgresql+asyncpg://cortex:cortex_pg@localhost:5433/cortex_db`
