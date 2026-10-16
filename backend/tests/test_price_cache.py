"""Price cache session reference ("open") price and daily change % (SPEC §10)."""

import json

import pytest
import pytest_asyncio

from app.market.cache import PriceCache, price_cache
from app.market.massive import MassiveClient
from app.market.provider import set_provider
from app.market.simulator import TICKER_CONFIG, Simulator


@pytest.fixture(autouse=True)
def clean_cache():
    price_cache._prices.clear()
    yield
    price_cache._prices.clear()


# ---------------------------------------------------------------------------
# PriceCache
# ---------------------------------------------------------------------------


def test_first_price_becomes_open_and_sticks():
    cache = PriceCache()
    first = cache.update("AAPL", 100.0)
    assert first.open_price == 100.0
    assert first.previous_price == 100.0

    cache.update("AAPL", 101.0)
    latest = cache.update("AAPL", 103.0)
    assert latest.open_price == 100.0
    assert latest.previous_price == 101.0
    assert latest.direction == "up"


def test_open_is_per_ticker():
    cache = PriceCache()
    cache.update("AAPL", 100.0)
    cache.update("TSLA", 250.0)
    cache.update("AAPL", 90.0)
    assert cache.get("AAPL").open_price == 100.0
    assert cache.get("TSLA").open_price == 250.0


def test_explicit_open_overrides_first_seen():
    cache = PriceCache()
    cache.update("AAPL", 100.0, open_price=95.0)
    assert cache.get("AAPL").open_price == 95.0
    # A later update without a reference keeps the explicit one
    cache.update("AAPL", 101.0)
    assert cache.get("AAPL").open_price == 95.0


def test_removed_ticker_gets_fresh_open_on_return():
    cache = PriceCache()
    cache.update("AAPL", 100.0)
    cache.remove("AAPL")
    assert cache.update("AAPL", 120.0).open_price == 120.0


def test_open_price_in_serialized_event():
    cache = PriceCache()
    cache.update("AAPL", 100.0)
    payload = json.loads(cache.update("AAPL", 102.0).model_dump_json())
    assert payload["open_price"] == 100.0
    assert payload["previous_price"] == 100.0
    assert payload["price"] == 102.0


# ---------------------------------------------------------------------------
# Providers set the reference
# ---------------------------------------------------------------------------


async def test_simulator_open_is_seed_price():
    sim = Simulator(tickers=["AAPL"])
    await sim.start()
    await sim.stop()
    for _ in range(5):
        sim._step()
    assert price_cache.get("AAPL").open_price == TICKER_CONFIG["AAPL"]["seed"]


class _SnapshotResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _SnapshotClient:
    def __init__(self, items):
        self.items = items

    async def get(self, url, params):
        return _SnapshotResponse({"tickers": self.items})


@pytest.fixture
def massive(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    return MassiveClient(tickers=["AAPL", "TSLA", "PYPL"])


async def test_massive_uses_prior_close_then_day_open(massive):
    massive._client = _SnapshotClient([
        {"ticker": "AAPL", "lastTrade": {"p": 110.0}, "prevDay": {"c": 100.0}, "day": {"o": 105.0}},
        {"ticker": "TSLA", "lastTrade": {"p": 260.0}, "day": {"o": 250.0}},
        {"ticker": "PYPL", "lastTrade": {"p": 70.0}, "prevDay": {"c": 0}, "day": {}},
    ])
    await massive._poll()
    assert price_cache.get("AAPL").open_price == 100.0
    assert price_cache.get("TSLA").open_price == 250.0
    # No usable reference in the snapshot: first price seen
    assert price_cache.get("PYPL").open_price == 70.0


# ---------------------------------------------------------------------------
# GET /api/watchlist
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def idle_provider():
    sim = Simulator(tickers=[])
    set_provider(sim)
    yield sim
    set_provider(None)


async def test_watchlist_change_percent_is_from_open_not_last_tick(client, idle_provider):
    price_cache.update("AAPL", 200.0)
    price_cache.update("AAPL", 209.0)
    price_cache.update("AAPL", 210.0)

    resp = await client.get("/api/watchlist")
    assert resp.status_code == 200
    aapl = next(i for i in resp.json() if i["ticker"] == "AAPL")
    assert aapl["price"] == 210.0
    assert aapl["previous_price"] == 209.0
    assert aapl["open_price"] == 200.0
    assert aapl["change_percent"] == 5.0
