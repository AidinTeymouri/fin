"""Environment-driven market data provider selection and the shared instance."""

import os

from app.market.interface import MarketDataProvider

# The single running provider, set by the app lifespan. Routes reach it through
# get_provider() so ticker changes go to the live instance, never a new one.
_provider: MarketDataProvider | None = None


def create_provider(tickers: list[str]) -> MarketDataProvider:
    """Create the appropriate provider based on environment, tracking `tickers`."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from app.market.massive import MassiveClient

        return MassiveClient(tickers=tickers)

    from app.market.simulator import Simulator

    return Simulator(tickers=tickers)


def get_provider() -> MarketDataProvider | None:
    """Return the running provider, or None if none has been started."""
    return _provider


def set_provider(provider: MarketDataProvider | None) -> None:
    """Register (or clear) the running provider."""
    global _provider
    _provider = provider
