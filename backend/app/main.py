"""FastAPI application entry point."""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT, load_env

# Load .env before importing modules that read the environment at import time
load_env()

from app.chat import router as chat_router  # noqa: E402
from app.database import get_db, init_db  # noqa: E402
from app.market.provider import create_provider, set_provider  # noqa: E402
from app.market.stream import router as stream_router  # noqa: E402
from app.portfolio import router as portfolio_router  # noqa: E402
from app.watchlist import load_tracked_tickers, router as watchlist_router  # noqa: E402
from app.snapshots import start_snapshot_recorder, stop_snapshot_recorder  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database, start market data provider and snapshot recorder."""
    await init_db()
    db = await get_db()
    try:
        tickers = await load_tracked_tickers(db)
    finally:
        await db.close()
    provider = create_provider(tickers)
    await provider.start()
    set_provider(provider)
    app.state.market_provider = provider
    start_snapshot_recorder()
    yield
    stop_snapshot_recorder()
    await provider.stop()
    set_provider(None)


app = FastAPI(title="FinAlly", lifespan=lifespan)
app.include_router(chat_router)

app.include_router(stream_router)
app.include_router(portfolio_router)
app.include_router(watchlist_router)


@app.get("/api/health")
async def health():
    return JSONResponse({"status": "ok"})


# Serve static files (Next.js export) at root, if the directory exists
static_dir = PROJECT_ROOT / "static"
if static_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")
