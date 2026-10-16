"""Watchlist API: response shapes, normalization, status codes."""

import pytest

from app.database import DEFAULT_TICKERS
from app.market.cache import price_cache
from app.market.provider import set_provider

ITEM_KEYS = {"ticker", "price", "previous_price", "open_price", "change_percent"}


@pytest.fixture(autouse=True)
def isolate():
    """No running provider and an empty price cache for every test."""
    set_provider(None)
    price_cache._prices.clear()
    yield
    price_cache._prices.clear()


async def _tickers(client) -> list[str]:
    resp = await client.get("/api/watchlist")
    assert resp.status_code == 200
    return [item["ticker"] for item in resp.json()]


async def test_get_returns_default_watchlist(client):
    resp = await client.get("/api/watchlist")
    assert resp.status_code == 200
    items = resp.json()
    # Defaults share one added_at, so only membership (not order) is defined
    assert sorted(i["ticker"] for i in items) == sorted(DEFAULT_TICKERS)
    for item in items:
        assert set(item) == ITEM_KEYS
        # No prices cached yet
        assert item["price"] is None
        assert item["previous_price"] is None
        assert item["open_price"] is None
        assert item["change_percent"] is None


async def test_get_includes_cached_price_and_change(client):
    price_cache.update("AAPL", 100.0)
    price_cache.update("AAPL", 110.0)
    price_cache.update("AAPL", 125.0)
    price_cache.update("MSFT", 400.0)

    items = {i["ticker"]: i for i in (await client.get("/api/watchlist")).json()}
    # Daily change is measured from the session open, not the last tick
    assert items["AAPL"] == {
        "ticker": "AAPL",
        "price": 125.0,
        "previous_price": 110.0,
        "open_price": 100.0,
        "change_percent": 25.0,
    }
    assert items["MSFT"]["price"] == 400.0
    assert items["MSFT"]["change_percent"] == 0.0
    assert items["GOOGL"]["price"] is None


async def test_post_uppercases_and_strips(client):
    resp = await client.post("/api/watchlist", json={"ticker": "  pypl \t"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == ITEM_KEYS
    assert body["ticker"] == "PYPL"
    assert body["price"] is None  # no provider running, nothing cached

    assert (await _tickers(client))[-1] == "PYPL"


async def test_post_returns_cached_price_when_available(client):
    price_cache.update("PYPL", 61.5)
    resp = await client.post("/api/watchlist", json={"ticker": "PYPL"})
    assert resp.status_code == 200
    assert resp.json()["price"] == 61.5


@pytest.mark.parametrize("ticker", ["AAPL", "aapl", " Aapl "])
async def test_post_duplicate_is_409(client, ticker):
    resp = await client.post("/api/watchlist", json={"ticker": ticker})
    assert resp.status_code == 409
    assert "AAPL" in resp.json()["detail"]
    assert (await _tickers(client)).count("AAPL") == 1


@pytest.mark.parametrize("ticker", ["", "   ", "\t\n"])
async def test_post_empty_is_400(client, ticker):
    resp = await client.post("/api/watchlist", json={"ticker": ticker})
    assert resp.status_code == 400
    assert "required" in resp.json()["detail"].lower()
    assert sorted(await _tickers(client)) == sorted(DEFAULT_TICKERS)


@pytest.mark.parametrize("ticker", ["NOT A TICKER", "1ABC", "TOOLONGTICKER", "AB$C"])
async def test_post_invalid_symbol_is_400(client, ticker):
    resp = await client.post("/api/watchlist", json={"ticker": ticker})
    assert resp.status_code == 400
    assert "invalid" in resp.json()["detail"].lower()


async def test_post_missing_field_is_422(client):
    resp = await client.post("/api/watchlist", json={})
    assert resp.status_code == 422


async def test_delete_removes_ticker(client):
    resp = await client.delete("/api/watchlist/AAPL")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert "AAPL" not in await _tickers(client)


async def test_delete_is_case_insensitive(client):
    resp = await client.delete("/api/watchlist/msft")
    assert resp.status_code == 200
    assert "MSFT" not in await _tickers(client)


async def test_delete_evicts_cached_price(client):
    price_cache.update("AAPL", 150.0)
    await client.delete("/api/watchlist/AAPL")
    assert price_cache.get("AAPL") is None


async def test_delete_missing_is_404(client):
    resp = await client.delete("/api/watchlist/ZZZZ")
    assert resp.status_code == 404
    assert "ZZZZ" in resp.json()["detail"]


async def test_delete_twice_is_404(client):
    assert (await client.delete("/api/watchlist/AAPL")).status_code == 200
    assert (await client.delete("/api/watchlist/AAPL")).status_code == 404


async def test_readd_after_delete(client):
    await client.delete("/api/watchlist/AAPL")
    assert (await client.post("/api/watchlist", json={"ticker": "AAPL"})).status_code == 200
    assert (await _tickers(client))[-1] == "AAPL"
