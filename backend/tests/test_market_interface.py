"""Both market data providers satisfy the MarketDataProvider contract."""

import inspect

import httpx
import pytest
import pytest_asyncio

from app.market.cache import price_cache
from app.market.interface import MarketDataProvider
from app.market.massive import MassiveClient
from app.market.simulator import Simulator

PROVIDERS = [Simulator, MassiveClient]


@pytest.fixture(autouse=True)
def clean_cache():
    price_cache._prices.clear()
    yield
    price_cache._prices.clear()


def _snapshot_handler(request: httpx.Request) -> httpx.Response:
    """Fake Polygon snapshot: every requested ticker trades at 100."""
    tickers = request.url.params["tickers"].split(",")
    return httpx.Response(
        200, json={"tickers": [{"ticker": t, "lastTrade": {"p": 100.0}} for t in tickers]}
    )


@pytest_asyncio.fixture(params=PROVIDERS, ids=lambda cls: cls.__name__)
async def make_provider(request, monkeypatch):
    """Factory returning (provider, produce) where produce() runs one price round."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    cls = request.param
    made = []

    def _make(tickers):
        provider = cls(tickers=tickers)
        if isinstance(provider, MassiveClient):
            provider._client = httpx.AsyncClient(transport=httpx.MockTransport(_snapshot_handler))

            async def produce():
                await provider._poll()
        else:

            async def produce():
                provider._step()

        made.append(provider)
        return provider, produce

    yield _make
    for p in made:
        if isinstance(p, MassiveClient):
            await p._client.aclose()


@pytest.mark.parametrize("cls", PROVIDERS, ids=lambda cls: cls.__name__)
def test_is_concrete_market_data_provider(cls):
    assert issubclass(cls, MarketDataProvider)
    assert not inspect.isabstract(cls)
    for name in MarketDataProvider.__abstractmethods__:
        impl = getattr(cls, name)
        assert impl is not getattr(MarketDataProvider, name), f"{cls.__name__}.{name} not implemented"
        # async-ness must match the interface so callers can await uniformly
        assert inspect.iscoroutinefunction(impl) == inspect.iscoroutinefunction(
            getattr(MarketDataProvider, name)
        ), name


def test_interface_cannot_be_instantiated():
    with pytest.raises(TypeError):
        MarketDataProvider()


async def test_initial_tickers_deduplicated_in_order(make_provider):
    provider, _ = make_provider(["AAPL", "MSFT", "AAPL"])
    assert provider.get_tickers() == ["AAPL", "MSFT"]


async def test_get_tickers_returns_a_copy(make_provider):
    provider, _ = make_provider(["AAPL"])
    provider.get_tickers().append("HACK")
    assert provider.get_tickers() == ["AAPL"]


async def test_add_ticker_tracks_and_produces_price(make_provider):
    provider, produce = make_provider(["AAPL"])
    await provider.add_ticker("PYPL")
    assert provider.get_tickers() == ["AAPL", "PYPL"]

    await produce()
    for ticker in ("AAPL", "PYPL"):
        update = price_cache.get(ticker)
        assert update is not None and update.price > 0


async def test_add_ticker_is_idempotent(make_provider):
    provider, _ = make_provider(["AAPL"])
    await provider.add_ticker("AAPL")
    await provider.add_ticker("PYPL")
    await provider.add_ticker("PYPL")
    assert provider.get_tickers() == ["AAPL", "PYPL"]


async def test_remove_ticker_untracks_and_evicts(make_provider):
    provider, produce = make_provider(["AAPL", "MSFT"])
    await produce()
    assert price_cache.get("MSFT") is not None

    await provider.remove_ticker("MSFT")
    assert provider.get_tickers() == ["AAPL"]
    assert price_cache.get("MSFT") is None

    await produce()
    assert price_cache.get("MSFT") is None
    assert price_cache.get("AAPL") is not None


async def test_remove_untracked_ticker_is_noop(make_provider):
    provider, _ = make_provider(["AAPL"])
    await provider.remove_ticker("ZZZZ")
    assert provider.get_tickers() == ["AAPL"]


async def test_remove_then_readd(make_provider):
    provider, produce = make_provider(["AAPL"])
    await provider.remove_ticker("AAPL")
    assert provider.get_tickers() == []
    await produce()  # nothing tracked: must not fail
    await provider.add_ticker("AAPL")
    await produce()
    assert provider.get_tickers() == ["AAPL"]
    assert price_cache.get("AAPL") is not None


async def test_start_stop_lifecycle(make_provider):
    provider, _ = make_provider([])
    await provider.start()
    await provider.stop()
    await provider.stop()  # stopping twice is harmless
