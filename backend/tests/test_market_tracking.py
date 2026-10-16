"""Market data tracks the live watchlist: add/remove tickers start/stop price streams."""

import asyncio

import numpy as np
import pytest
import pytest_asyncio

from app.database import get_db
from app.main import lifespan
from app.market.cache import price_cache
from app.market.massive import MassiveClient
from app.market.provider import create_provider, get_provider, set_provider
from app.market.simulator import (
    TICKER_CONFIG,
    UNKNOWN_SEED_MAX,
    UNKNOWN_SEED_MIN,
    Simulator,
)


@pytest.fixture(autouse=True)
def clean_cache():
    price_cache._prices.clear()
    yield
    price_cache._prices.clear()


@pytest_asyncio.fixture
async def provider():
    """An idle (not started) simulator registered as the app's provider."""
    sim = Simulator(tickers=[])
    set_provider(sim)
    yield sim
    set_provider(None)


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------


async def test_simulator_add_unknown_ticker_seeds_plausible_price():
    sim = Simulator(tickers=["AAPL"])
    await sim.add_ticker("PYPL")

    assert sim.get_tickers() == ["AAPL", "PYPL"]
    update = price_cache.get("PYPL")
    assert update is not None
    assert UNKNOWN_SEED_MIN <= update.price <= UNKNOWN_SEED_MAX
    assert sim._cholesky.shape == (2, 2)

    sim._step()
    assert sim._prices["PYPL"] >= 0.01


async def test_simulator_add_known_ticker_uses_config_seed():
    sim = Simulator(tickers=[])
    await sim.add_ticker("NVDA")
    assert price_cache.get("NVDA").price == TICKER_CONFIG["NVDA"]["seed"]


async def test_simulator_add_is_idempotent():
    sim = Simulator(tickers=["AAPL"])
    await sim.add_ticker("AAPL")
    await sim.add_ticker("ZM")
    await sim.add_ticker("ZM")
    assert sim.get_tickers() == ["AAPL", "ZM"]


def test_simulator_unknown_seed_is_stable_per_ticker():
    assert Simulator(tickers=["ZZZ"])._prices["ZZZ"] == Simulator(tickers=["ZZZ"])._prices["ZZZ"]


async def test_simulator_remove_stops_and_evicts():
    sim = Simulator(tickers=["AAPL", "TSLA"])
    await sim.start()
    try:
        await sim.remove_ticker("TSLA")
        assert sim.get_tickers() == ["AAPL"]
        assert price_cache.get("TSLA") is None
        assert sim._cholesky.shape == (1, 1)

        await asyncio.sleep(0.6)  # at least one more tick
        assert price_cache.get("TSLA") is None
        assert price_cache.get("AAPL") is not None
    finally:
        await sim.stop()


async def test_simulator_with_no_tickers_steps_safely():
    sim = Simulator(tickers=[])
    sim._step()
    assert price_cache.get_all() == []


async def test_running_simulator_streams_added_ticker_within_a_second():
    sim = Simulator(tickers=["AAPL"])
    await sim.start()
    try:
        await sim.add_ticker("PYPL")
        first = price_cache.get("PYPL")
        assert first is not None

        await asyncio.sleep(1.0)
        assert price_cache.get("PYPL").timestamp != first.timestamp
    finally:
        await sim.stop()


def test_correlation_with_many_unknown_tickers_is_positive_definite():
    tickers = list(TICKER_CONFIG) + [f"X{i}" for i in range(50)]
    sim = Simulator(tickers=tickers)
    assert sim._cholesky.shape == (len(tickers), len(tickers))
    assert np.all(np.isfinite(sim._cholesky))


# ---------------------------------------------------------------------------
# Massive
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    """Records requested tickers and returns a price for each."""

    def __init__(self):
        self.requested: list[str] = []

    async def get(self, url, params):
        tickers = params["tickers"].split(",")
        self.requested.append(params["tickers"])
        return _FakeResponse(
            {"tickers": [{"ticker": t, "lastTrade": {"p": 100.0}} for t in tickers]}
        )


@pytest.fixture
def massive(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    client = MassiveClient(tickers=["AAPL"])
    client._client = _FakeClient()
    return client


async def test_massive_polls_current_tracked_set(massive):
    await massive.add_ticker("PYPL")
    await massive.add_ticker("PYPL")
    assert massive.get_tickers() == ["AAPL", "PYPL"]

    await massive._poll()
    assert massive._client.requested[-1] == "AAPL,PYPL"
    assert price_cache.get("PYPL").price == 100.0

    await massive.remove_ticker("AAPL")
    assert price_cache.get("AAPL") is None
    await massive._poll()
    assert massive._client.requested[-1] == "PYPL"
    assert price_cache.get("AAPL") is None


async def test_massive_ignores_results_for_untracked_tickers(massive):
    class _StaleClient(_FakeClient):
        async def get(self, url, params):
            return _FakeResponse({"tickers": [{"ticker": "TSLA", "lastTrade": {"p": 1.0}}]})

    massive._client = _StaleClient()
    await massive._poll()
    assert price_cache.get("TSLA") is None


async def test_massive_add_wakes_poll_loop(massive, monkeypatch):
    monkeypatch.setattr(massive, "_poll_interval", 60.0)
    task = asyncio.create_task(massive._run())
    try:
        await asyncio.sleep(0.05)
        await massive.add_ticker("PYPL")
        await asyncio.sleep(0.05)
        assert massive._client.requested[-1] == "AAPL,PYPL"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_create_provider_passes_tickers(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    assert create_provider(["PYPL"]).get_tickers() == ["PYPL"]

    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    provider = create_provider(["PYPL", "AAPL"])
    assert isinstance(provider, MassiveClient)
    assert provider.get_tickers() == ["PYPL", "AAPL"]


# ---------------------------------------------------------------------------
# Watchlist routes / chat use the shared provider
# ---------------------------------------------------------------------------


async def test_add_ticker_route_starts_price_stream(client, provider):
    resp = await client.post("/api/watchlist", json={"ticker": "pypl"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticker"] == "PYPL"
    assert data["price"] is not None

    assert "PYPL" in provider.get_tickers()
    assert price_cache.get("PYPL") is not None

    resp = await client.get("/api/watchlist")
    pypl = next(i for i in resp.json() if i["ticker"] == "PYPL")
    assert pypl["price"] is not None


async def test_add_duplicate_and_invalid_tickers_rejected(client, provider):
    assert (await client.post("/api/watchlist", json={"ticker": "AAPL"})).status_code == 409
    assert (await client.post("/api/watchlist", json={"ticker": "NOT A TICKER"})).status_code == 400
    assert (await client.post("/api/watchlist", json={"ticker": "  "})).status_code == 400
    assert provider.get_tickers() == []


async def test_remove_ticker_route_stops_price_stream(client, provider):
    await client.post("/api/watchlist", json={"ticker": "PYPL"})
    assert price_cache.get("PYPL") is not None

    resp = await client.delete("/api/watchlist/PYPL")
    assert resp.status_code == 200
    assert "PYPL" not in provider.get_tickers()
    assert price_cache.get("PYPL") is None

    assert (await client.delete("/api/watchlist/PYPL")).status_code == 404


async def test_remove_ticker_keeps_tracking_held_position(client, provider):
    await provider.add_ticker("AAPL")
    resp = await client.post(
        "/api/portfolio/trade", json={"ticker": "AAPL", "quantity": 1, "side": "buy"}
    )
    assert resp.status_code == 200

    resp = await client.delete("/api/watchlist/AAPL")
    assert resp.status_code == 200
    assert "AAPL" in provider.get_tickers()
    assert price_cache.get("AAPL") is not None


async def _trade(client, ticker, side, quantity):
    resp = await client.post(
        "/api/portfolio/trade", json={"ticker": ticker, "quantity": quantity, "side": side}
    )
    assert resp.status_code == 200, resp.text


async def test_selling_out_unwatched_ticker_stops_price_stream(client, provider):
    await provider.add_ticker("PYPL")  # tracked, but not on the watchlist
    await _trade(client, "PYPL", "buy", 3)

    await _trade(client, "PYPL", "sell", 1)
    assert "PYPL" in provider.get_tickers()
    assert price_cache.get("PYPL") is not None

    await _trade(client, "PYPL", "sell", 2)
    assert "PYPL" not in provider.get_tickers()
    assert price_cache.get("PYPL") is None


async def test_selling_out_watched_ticker_keeps_price_stream(client, provider):
    await provider.add_ticker("AAPL")  # on the default watchlist
    await _trade(client, "AAPL", "buy", 2)
    await _trade(client, "AAPL", "sell", 2)

    assert "AAPL" in provider.get_tickers()
    assert price_cache.get("AAPL") is not None


async def test_chat_sell_out_of_unwatched_ticker_stops_price_stream(client, provider, monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "true")
    await provider.add_ticker("PYPL")
    await _trade(client, "PYPL", "buy", 1)

    resp = await client.post("/api/chat", json={"message": "sell 1 PYPL"})
    assert resp.status_code == 200
    assert "PYPL" not in provider.get_tickers()
    assert price_cache.get("PYPL") is None


async def test_chat_watchlist_changes_update_provider(client, provider, monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "true")

    resp = await client.post("/api/chat", json={"message": "add PYPL to watchlist"})
    assert resp.status_code == 200
    assert "PYPL" in provider.get_tickers()
    assert price_cache.get("PYPL") is not None

    await provider.add_ticker("NFLX")
    resp = await client.post("/api/chat", json={"message": "remove NFLX"})
    assert resp.status_code == 200
    assert "NFLX" not in provider.get_tickers()
    assert price_cache.get("NFLX") is None


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


async def test_startup_tracks_db_watchlist_and_positions(db, monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    conn = await get_db()
    try:
        await conn.execute("DELETE FROM watchlist")
        await conn.execute(
            "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES ('w1', 'default', 'PYPL', '')"
        )
        await conn.execute(
            "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) "
            "VALUES ('p1', 'default', 'ZM', 5, 70.0, '')"
        )
        await conn.commit()
    finally:
        await conn.close()

    from app.main import app

    async with lifespan(app):
        provider = get_provider()
        assert provider is app.state.market_provider
        assert sorted(provider.get_tickers()) == ["PYPL", "ZM"]
        assert price_cache.get("PYPL") is not None
        assert price_cache.get("ZM") is not None
        assert price_cache.get("AAPL") is None
    assert get_provider() is None
