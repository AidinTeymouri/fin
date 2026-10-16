"""Watchlist API routes and the shared add/remove helpers that keep market data in sync."""

import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_db
from app.market.cache import price_cache
from app.market.provider import get_provider

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


class WatchlistError(Exception):
    """A watchlist change that could not be applied."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Shared helpers (used by these routes and by chat watchlist_changes)
# ---------------------------------------------------------------------------


async def load_tracked_tickers(db) -> list[str]:
    """Tickers market data must track: the watchlist plus any open positions."""
    cursor = await db.execute(
        "SELECT ticker FROM watchlist WHERE user_id = 'default' "
        "UNION SELECT ticker FROM positions WHERE user_id = 'default' AND quantity > 0"
    )
    return sorted(row["ticker"] for row in await cursor.fetchall())


async def _holds_position(db, ticker: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM positions WHERE user_id = 'default' AND ticker = ? AND quantity > 0",
        (ticker,),
    )
    return await cursor.fetchone() is not None


async def add_to_watchlist(db, ticker: str) -> str:
    """Add a ticker to the watchlist and start streaming its price.

    Returns the normalized ticker. Raises WatchlistError on invalid/duplicate input.
    """
    ticker = ticker.upper().strip()
    if not ticker:
        raise WatchlistError("Ticker is required", 400)
    if not TICKER_PATTERN.match(ticker):
        raise WatchlistError(f"Invalid ticker: {ticker}", 400)

    cursor = await db.execute(
        "SELECT id FROM watchlist WHERE user_id = 'default' AND ticker = ?",
        (ticker,),
    )
    if await cursor.fetchone():
        raise WatchlistError(f"{ticker} already in watchlist", 409)

    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, 'default', ?, ?)",
        (str(uuid.uuid4()), ticker, now),
    )
    await db.commit()

    provider = get_provider()
    if provider is not None:
        await provider.add_ticker(ticker)
    return ticker


async def remove_from_watchlist(db, ticker: str) -> str:
    """Remove a ticker from the watchlist.

    Market data keeps tracking it while a position is still held; otherwise it
    is untracked and evicted from the price cache. Returns the normalized ticker.
    """
    ticker = ticker.upper().strip()
    cursor = await db.execute(
        "DELETE FROM watchlist WHERE user_id = 'default' AND ticker = ?",
        (ticker,),
    )
    await db.commit()
    if cursor.rowcount == 0:
        raise WatchlistError(f"{ticker} not in watchlist", 404)

    if not await _holds_position(db, ticker):
        provider = get_provider()
        if provider is not None:
            await provider.remove_ticker(ticker)
        price_cache.remove(ticker)
    return ticker


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class AddTickerRequest(BaseModel):
    ticker: str


class WatchlistItem(BaseModel):
    ticker: str
    price: float | None = None
    previous_price: float | None = None
    open_price: float | None = None
    change_percent: float | None = None  # daily change vs open_price


@router.get("", response_model=list[WatchlistItem])
async def get_watchlist():
    """Return watchlist tickers with latest prices from price cache."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT ticker FROM watchlist WHERE user_id = 'default' ORDER BY added_at"
        )
        rows = await cursor.fetchall()
        items = []
        for row in rows:
            ticker = row["ticker"]
            update = price_cache.get(ticker)
            if update:
                ref = update.open_price
                change_pct = ((update.price - ref) / ref * 100) if ref else None
                items.append(WatchlistItem(
                    ticker=ticker,
                    price=update.price,
                    previous_price=update.previous_price,
                    open_price=ref,
                    change_percent=round(change_pct, 2) if change_pct is not None else None,
                ))
            else:
                items.append(WatchlistItem(ticker=ticker))
        return items
    finally:
        await db.close()


@router.post("", response_model=WatchlistItem)
async def add_ticker(body: AddTickerRequest):
    """Add a ticker to the watchlist and start streaming its price."""
    db = await get_db()
    try:
        try:
            ticker = await add_to_watchlist(db, body.ticker)
        except WatchlistError as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))
        update = price_cache.get(ticker)
        return WatchlistItem(ticker=ticker, price=update.price if update else None)
    finally:
        await db.close()


@router.delete("/{ticker}")
async def remove_ticker(ticker: str):
    """Remove a ticker from the watchlist (stops its price stream unless held)."""
    db = await get_db()
    try:
        try:
            await remove_from_watchlist(db, ticker)
        except WatchlistError as e:
            raise HTTPException(status_code=e.status_code, detail=str(e))
        return {"ok": True}
    finally:
        await db.close()
