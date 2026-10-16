# FinAlly — AI Trading Workstation

A simulated trading terminal with live-streaming prices, a virtual $10k portfolio, and an LLM chat assistant that can analyze positions and place trades. FastAPI backend + static Next.js frontend, served from one Docker container on port 8000.

Full spec: [`SPEC.md`](SPEC.md) (agent copy in [`planning/PLAN.md`](planning/PLAN.md)).

## Quick start

Requires Docker.

```bash
cp .env.example .env          # then fill in OPENROUTER_API_KEY
./scripts/start_mac.sh        # macOS/Linux (add --build to force a rebuild)
./scripts/stop_mac.sh
```

Windows (PowerShell):

```powershell
.\scripts\start_windows.ps1   # add --build to force a rebuild
.\scripts\stop_windows.ps1
```

Open http://localhost:8000. Data persists in the `finally-data` Docker volume; stopping the container does not remove it.

## Environment variables

Set in `.env` at the repo root (see [`.env.example`](.env.example)):

| Variable | Required | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes (unless `LLM_MOCK=true`) | LLM chat via LiteLLM → OpenRouter |
| `MASSIVE_API_KEY` | No | Real market data from Massive (Polygon.io). Empty → built-in simulator |
| `LLM_MOCK` | No | `true` returns deterministic mock chat responses (tests, no API key) |

## Local development

Backend (port 8000). It reads env vars from the process environment, so export `.env` first:

```bash
cd backend
set -a; source ../.env; set +a
uv run uvicorn app.main:app --reload
```

Frontend (port 3000), in another terminal. `next.config.ts` rewrites `/api/*` to `http://localhost:8000` in dev:

```bash
cd frontend
npm install
npm run dev
```

Then open http://localhost:3000.

## Tests

Unit tests:

```bash
cd backend && uv run pytest
cd frontend && npm test
```

E2E (Playwright, runs against the app container with `LLM_MOCK=true`):

```bash
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
```
