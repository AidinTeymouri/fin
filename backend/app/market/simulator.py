"""Geometric Brownian Motion market data simulator."""

import asyncio
import math
import random

import numpy as np

from app.market.cache import price_cache
from app.market.interface import MarketDataProvider

# Seed prices and per-ticker parameters (annual drift, annual volatility)
TICKER_CONFIG = {
    "AAPL": {"seed": 190.0, "drift": 0.08, "vol": 0.25},
    "GOOGL": {"seed": 175.0, "drift": 0.10, "vol": 0.28},
    "MSFT": {"seed": 420.0, "drift": 0.09, "vol": 0.24},
    "AMZN": {"seed": 185.0, "drift": 0.12, "vol": 0.30},
    "TSLA": {"seed": 250.0, "drift": 0.05, "vol": 0.50},
    "NVDA": {"seed": 880.0, "drift": 0.15, "vol": 0.40},
    "META": {"seed": 500.0, "drift": 0.10, "vol": 0.32},
    "JPM": {"seed": 195.0, "drift": 0.06, "vol": 0.20},
    "V": {"seed": 280.0, "drift": 0.07, "vol": 0.18},
    "NFLX": {"seed": 630.0, "drift": 0.11, "vol": 0.35},
}

# Correlation matrix sectors: tech stocks correlated, finance separate
TECH_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "NFLX"]
FINANCE_TICKERS = ["JPM", "V"]

# Parameters for tickers not in TICKER_CONFIG
DEFAULT_DRIFT = 0.08
DEFAULT_VOL = 0.30
UNKNOWN_SEED_MIN = 20.0
UNKNOWN_SEED_MAX = 500.0

UPDATE_INTERVAL = 0.5  # seconds
EVENT_PROBABILITY = 0.005  # per ticker per update
EVENT_MIN_PCT = 0.02
EVENT_MAX_PCT = 0.05


def _build_correlation_matrix(tickers: list[str]) -> np.ndarray:
    """Build a correlation matrix with tech and finance clusters."""
    n = len(tickers)
    corr = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            ti, tj = tickers[i], tickers[j]
            if ti in TECH_TICKERS and tj in TECH_TICKERS:
                corr[i, j] = corr[j, i] = 0.6
            elif ti in FINANCE_TICKERS and tj in FINANCE_TICKERS:
                corr[i, j] = corr[j, i] = 0.5
            else:
                corr[i, j] = corr[j, i] = 0.2
    return corr


def _ticker_params(ticker: str) -> dict:
    """Seed price, drift and vol for a ticker.

    Unknown tickers get a plausible seed price that is stable per symbol (so a
    ticker re-added later starts near the same level) and default drift/vol.
    """
    if ticker in TICKER_CONFIG:
        return TICKER_CONFIG[ticker]
    seed = random.Random(ticker).uniform(UNKNOWN_SEED_MIN, UNKNOWN_SEED_MAX)
    return {"seed": round(seed, 2), "drift": DEFAULT_DRIFT, "vol": DEFAULT_VOL}


class Simulator(MarketDataProvider):
    """GBM-based market data simulator."""

    def __init__(self, tickers: list[str] | None = None):
        self._task: asyncio.Task | None = None
        self._dt = UPDATE_INTERVAL / (252 * 6.5 * 3600)  # fraction of trading year
        self._tickers: list[str] = []
        self._params: dict[str, dict] = {}
        self._prices: dict[str, float] = {}
        self._cholesky = np.zeros((0, 0))

        initial = list(TICKER_CONFIG.keys()) if tickers is None else tickers
        for ticker in initial:
            self._track(ticker)
        self._rebuild_correlation()

    def _track(self, ticker: str) -> bool:
        """Add ticker to the simulated set. Returns False if already tracked."""
        if ticker in self._params:
            return False
        params = _ticker_params(ticker)
        self._tickers.append(ticker)
        self._params[ticker] = params
        self._prices[ticker] = params["seed"]
        return True

    def _rebuild_correlation(self) -> None:
        """Precompute Cholesky decomposition for correlated random draws."""
        corr = _build_correlation_matrix(self._tickers)
        self._cholesky = np.linalg.cholesky(corr)

    async def start(self) -> None:
        """Start the simulation loop."""
        # Seed initial prices into cache
        for ticker, price in self._prices.items():
            price_cache.update(ticker, price)
        self._task = asyncio.create_task(self._run())

    async def add_ticker(self, ticker: str) -> None:
        """Start simulating a ticker; its seed price is published immediately."""
        if self._track(ticker):
            self._rebuild_correlation()
        if price_cache.get(ticker) is None:
            price_cache.update(ticker, self._prices[ticker])

    async def remove_ticker(self, ticker: str) -> None:
        """Stop simulating a ticker and evict it from the price cache."""
        if ticker in self._params:
            self._tickers.remove(ticker)
            del self._params[ticker]
            del self._prices[ticker]
            self._rebuild_correlation()
        price_cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def stop(self) -> None:
        """Stop the simulation loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        """Main simulation loop."""
        while True:
            self._step()
            await asyncio.sleep(UPDATE_INTERVAL)

    def _step(self) -> None:
        """Advance all prices by one GBM step with correlation."""
        n = len(self._tickers)
        if n == 0:
            return
        # Correlated normal draws
        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent

        for i, ticker in enumerate(self._tickers):
            cfg = self._params[ticker]
            drift = cfg["drift"]
            vol = cfg["vol"]
            s = self._prices[ticker]

            # GBM: dS = S * (mu*dt + sigma*sqrt(dt)*Z)
            ds = s * (drift * self._dt + vol * math.sqrt(self._dt) * z_correlated[i])
            new_price = s + ds

            # Random event: sudden 2-5% move
            if random.random() < EVENT_PROBABILITY:
                pct = random.uniform(EVENT_MIN_PCT, EVENT_MAX_PCT)
                sign = random.choice([-1, 1])
                new_price *= 1 + sign * pct

            new_price = max(new_price, 0.01)  # floor at 1 cent
            self._prices[ticker] = new_price
            price_cache.update(ticker, new_price)
