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
docker compose -f test/docker-compose.test.yml up --build \
  --abort-on-container-exit --exit-code-from playwright
```

## AI chat (`POST /api/chat`)

- Calls `openrouter/openai/gpt-oss-120b` via LiteLLM with a strict JSON-schema
  `response_format` generated from the pydantic `LLMResponse` model, routed to
  **Cerebras** only (`provider.order=["cerebras"]`, `allow_fallbacks=false`,
  `require_parameters=true`). If Cerebras can't serve the request the user gets a
  friendly "try again" message rather than a silent reroute to a slower provider.
- Never 500s on LLM trouble: network/provider errors, malformed or off-schema
  output (raw content is logged), and a missing `OPENROUTER_API_KEY` all return
  200 with an explanatory assistant message and no actions.
- Trades go through the same `app.trading.execute_trade` as
  `POST /api/portfolio/trade`; failures are listed in the reply and only executed
  trades/watchlist changes are returned as actions.
- `LLM_MOCK=true` (no key needed) is deterministic. Trades execute only for the
  explicit phrase `<buy|sell> <quantity> <TICKER>`, e.g. **`buy 1 AAPL`** or
  `sell 2 shares of TSLA`. "remove …" removes NFLX, "add …"/"watch …" adds PYPL.
