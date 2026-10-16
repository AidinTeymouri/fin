"""Massive (Polygon.io) client: snapshot parsing and poll-loop resilience.

All HTTP goes through httpx.MockTransport — no network.
"""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.market.cache import price_cache
from app.market.massive import POLYGON_BASE_URL, MassiveClient
from app.market.models import PriceUpdate

SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "polygon_snapshot.json").read_text())
SNAPSHOT_PATH = "/v2/snapshot/locale/us/markets/stocks/tickers"


@pytest.fixture(autouse=True)
def clean_cache():
    price_cache._prices.clear()
    yield
    price_cache._prices.clear()


class Responder:
    """MockTransport handler serving a queue of canned responses, recording requests."""

    def __init__(self, *responses: httpx.Response):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # Repeat the last response once the queue is drained
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


@pytest_asyncio.fixture
async def make_client(monkeypatch):
    """Build a MassiveClient whose HTTP client is backed by a Responder."""
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    clients = []

    def _make(tickers, *responses):
        responder = Responder(*responses)
        client = MassiveClient(tickers=tickers)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(responder))
        clients.append(client)
        return client, responder

    yield _make
    for c in clients:
        await c._client.aclose()


# ---------------------------------------------------------------------------
# Parsing a recorded snapshot
# ---------------------------------------------------------------------------


async def test_parses_recorded_snapshot_into_price_updates(make_client):
    client, responder = make_client(["AAPL", "MSFT", "NVDA"], httpx.Response(200, json=SNAPSHOT))
    await client._poll()

    # last trade price, and prior close as the daily-change reference
    expected = {"AAPL": (189.72, 188.58), "MSFT": (418.45, 420.76), "NVDA": (892.5, 880.0)}
    for ticker, (price, reference) in expected.items():
        update = price_cache.get(ticker)
        assert isinstance(update, PriceUpdate)
        assert update.ticker == ticker
        assert update.price == price
        assert update.open_price == reference
        assert update.previous_price == price  # first sighting
        assert update.direction == "flat"
        assert update.timestamp

    request = responder.requests[0]
    assert str(request.url).startswith(POLYGON_BASE_URL + SNAPSHOT_PATH)
    assert request.url.params["tickers"] == "AAPL,MSFT,NVDA"
    assert request.url.params["apiKey"] == "test-key"


async def test_second_poll_sets_previous_price_and_direction(make_client):
    moved = json.loads(json.dumps(SNAPSHOT))
    moved["tickers"][0]["lastTrade"]["p"] = 190.00  # AAPL up
    moved["tickers"][1]["lastTrade"]["p"] = 418.00  # MSFT down
    client, _ = make_client(
        ["AAPL", "MSFT", "NVDA"],
        httpx.Response(200, json=SNAPSHOT),
        httpx.Response(200, json=moved),
    )
    await client._poll()
    await client._poll()

    aapl, msft, nvda = (price_cache.get(t) for t in ("AAPL", "MSFT", "NVDA"))
    assert (aapl.previous_price, aapl.price, aapl.direction) == (189.72, 190.0, "up")
    assert (msft.previous_price, msft.price, msft.direction) == (418.45, 418.0, "down")
    assert nvda.direction == "flat"


async def test_tracked_ticker_missing_from_response_is_skipped(make_client):
    client, _ = make_client(["AAPL", "MSFT", "NVDA", "ZZZZ"], httpx.Response(200, json=SNAPSHOT))
    await client._poll()

    assert price_cache.get("ZZZZ") is None
    assert price_cache.get("AAPL").price == 189.72
    assert client.get_tickers() == ["AAPL", "MSFT", "NVDA", "ZZZZ"]


async def test_no_tickers_makes_no_request(make_client):
    client, responder = make_client([], httpx.Response(200, json=SNAPSHOT))
    await client._poll()
    assert responder.requests == []


@pytest.mark.parametrize(
    "item",
    [
        {"ticker": "MSFT"},  # no lastTrade at all
        {"ticker": "MSFT", "lastTrade": None},
        {"ticker": "MSFT", "lastTrade": {}},
        {"ticker": "MSFT", "lastTrade": {"p": None}},
        {"ticker": "MSFT", "lastTrade": {"p": "n/a"}},
        {"ticker": "MSFT", "lastTrade": {"p": 0}},
        {"ticker": "MSFT", "lastTrade": "garbage"},
        {"lastTrade": {"p": 1.0}},  # no ticker
        "not-an-object",
        None,
    ],
)
async def test_malformed_item_is_skipped_without_dropping_others(make_client, item):
    payload = {"status": "OK", "tickers": [SNAPSHOT["tickers"][0], item]}
    client, _ = make_client(["AAPL", "MSFT"], httpx.Response(200, json=payload))
    await client._poll()

    assert price_cache.get("AAPL").price == 189.72
    assert price_cache.get("MSFT") is None


@pytest.mark.parametrize(
    "extra, reference",
    [
        ({"prevDay": {"c": 120.0}, "day": {"o": 110.0}}, 120.0),
        ({"prevDay": {"c": 0}, "day": {"o": 110.0}}, 110.0),  # no prior close: today's open
        ({"prevDay": None, "day": {"o": 110.0}}, 110.0),
        ({"prevDay": "garbage", "day": {"o": "n/a"}}, 100.0),  # unusable: first price seen
        ({}, 100.0),
    ],
)
async def test_reference_price_fallbacks(make_client, extra, reference):
    item = {"ticker": "AAPL", "lastTrade": {"p": 100.0}, **extra}
    client, _ = make_client(["AAPL"], httpx.Response(200, json={"tickers": [item]}))
    await client._poll()
    update = price_cache.get("AAPL")
    assert update.price == 100.0
    assert update.open_price == reference


async def test_non_finite_price_is_skipped(make_client):
    # Python's json accepts NaN/Infinity literals, so they can reach the parser
    body = '{"tickers": [{"ticker": "AAPL", "lastTrade": {"p": NaN}}, {"ticker": "MSFT", "lastTrade": {"p": Infinity}}]}'
    client, _ = make_client(["AAPL", "MSFT"], httpx.Response(200, text=body))
    await client._poll()
    assert price_cache.get_all() == []


# ---------------------------------------------------------------------------
# Bad responses must not raise out of _poll (that would kill the poll task)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"status": "ERROR", "error": "You've exceeded the maximum requests per minute"}),
        httpx.Response(401, json={"status": "ERROR", "error": "Unknown API Key"}),
        httpx.Response(500, text="Internal Server Error"),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"status": "OK"}),  # no tickers key
        httpx.Response(200, json={"status": "OK", "tickers": None}),
        httpx.Response(200, json={"status": "OK", "tickers": "AAPL"}),
    ],
    ids=["429", "401", "500", "non-json", "json-list", "no-tickers", "tickers-null", "tickers-str"],
)
async def test_bad_response_is_logged_and_cache_untouched(make_client, response, caplog):
    client, _ = make_client(["AAPL"], response)
    price_cache.update("AAPL", 150.0)

    await client._poll()  # must not raise

    assert price_cache.get("AAPL").price == 150.0
    assert any(r.levelname == "ERROR" for r in caplog.records)


async def test_network_error_is_logged(make_client, caplog):
    client, _ = make_client(["AAPL"], httpx.Response(200, json=SNAPSHOT))

    def _refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(_refuse))
    await client._poll()
    assert price_cache.get("AAPL") is None
    assert any("poll failed" in r.getMessage() for r in caplog.records)


async def test_poll_loop_survives_429_and_bad_payload(make_client, monkeypatch):
    """The background loop keeps polling after failures and recovers on a good response."""
    client, responder = make_client(
        ["AAPL", "MSFT", "NVDA"],
        httpx.Response(429, json={"status": "ERROR"}),
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"tickers": [{"ticker": "AAPL", "lastTrade": None}]}),
        httpx.Response(200, json=SNAPSHOT),
    )
    monkeypatch.setattr(client, "_poll_interval", 0.01)

    task = asyncio.create_task(client._run())
    try:
        for _ in range(200):
            if price_cache.get("NVDA") is not None:
                break
            await asyncio.sleep(0.01)
        assert not task.done(), "poll loop died"
        assert len(responder.requests) >= 4
        assert price_cache.get("NVDA").price == 892.5
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_start_and_stop_manage_task_and_http_client(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    client = MassiveClient(tickers=[])  # empty set: the loop never hits the network
    await client.start()
    assert client._task is not None and not client._task.done()
    await client.stop()
    assert client._task.done()
    assert client._client.is_closed
