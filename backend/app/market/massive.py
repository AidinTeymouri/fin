"""Massive (Polygon.io) REST API client for real market data."""

import asyncio
import logging
import math
import os

import httpx

from app.market.cache import price_cache
from app.market.interface import MarketDataProvider

logger = logging.getLogger(__name__)

POLYGON_BASE_URL = "https://api.polygon.io"
DEFAULT_POLL_INTERVAL = 15  # seconds (free tier: 5 calls/min)


def _positive_number(value) -> float | None:
    """`value` as a float if it is a finite, positive number (not a bool), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _reference_price(item: dict) -> float | None:
    """Daily-change reference from a snapshot: prior close, else today's open.

    None (missing or zero in both) lets the cache fall back to the first price seen.
    """
    for key, field in (("prevDay", "c"), ("day", "o")):
        bar = item.get(key)
        value = _positive_number(bar.get(field)) if isinstance(bar, dict) else None
        if value is not None:
            return value
    return None


class MassiveClient(MarketDataProvider):
    """Polls Polygon.io REST API for live market data on the tracked ticker set."""

    def __init__(self, tickers: list[str]):
        self._api_key = os.environ["MASSIVE_API_KEY"]
        self._tickers: list[str] = list(dict.fromkeys(tickers))
        self._poll_interval = float(os.environ.get("MASSIVE_POLL_INTERVAL", DEFAULT_POLL_INTERVAL))
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        # Set to cut the current sleep short so a newly added ticker is fetched promptly
        self._wake = asyncio.Event()

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=10.0)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            self._wake.set()

    async def remove_ticker(self, ticker: str) -> None:
        if ticker in self._tickers:
            self._tickers.remove(ticker)
        price_cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _run(self) -> None:
        """Poll loop."""
        while True:
            self._wake.clear()
            await self._poll()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _poll(self) -> None:
        """Fetch latest prices for all tracked tickers from Polygon.io snapshot endpoint."""
        if not self._tickers:
            return
        tickers_csv = ",".join(self._tickers)
        url = f"{POLYGON_BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"
        params = {"tickers": tickers_csv, "apiKey": self._api_key}

        # Any exception escaping here would kill the poll task and freeze prices,
        # so HTTP errors (incl. 429 rate limits) and malformed payloads are logged
        # and skipped until the next poll.
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.error("Massive API poll failed: %s", e)
            return

        items = data.get("tickers") if isinstance(data, dict) else None
        if not isinstance(items, list):
            logger.error("Massive API returned an unexpected payload: %.200r", data)
            return

        for ticker, price, reference in parse_snapshot_items(items):
            # Skip tickers removed while the request was in flight
            if ticker in self._tickers:
                price_cache.update(ticker, price, reference)


def parse_snapshot_items(items: list) -> list[tuple[str, float, float | None]]:
    """Extract (ticker, last trade price, daily-change reference) from Polygon snapshot items.

    Items without a usable ticker or a finite, positive last trade price are skipped.
    """
    parsed = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ticker = item.get("ticker")
        last_trade = item.get("lastTrade")
        if not isinstance(ticker, str) or not isinstance(last_trade, dict):
            continue
        price = _positive_number(last_trade.get("p"))
        if price is None:
            continue
        parsed.append((ticker, price, _reference_price(item)))
    return parsed
