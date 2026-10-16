"""SSE price stream: /api/stream/prices emits price events with the full payload."""

import asyncio
import json
import re

import pytest

from app.main import app
from app.market.cache import price_cache
from app.market.stream import _price_event_generator

EVENT_KEYS = {"ticker", "price", "previous_price", "open_price", "timestamp", "direction"}


@pytest.fixture(autouse=True)
def clean_cache():
    price_cache._prices.clear()
    yield
    price_cache._prices.clear()


async def _take(agen, n: int, timeout: float = 2.0) -> list[dict]:
    return [await asyncio.wait_for(anext(agen), timeout) for _ in range(n)]


async def _stream(path: str, min_events: int, timeout: float = 3.0):
    """Drive the ASGI app directly and disconnect once `min_events` price events arrived.

    httpx's ASGITransport buffers the whole body, which never ends for SSE, so we
    speak ASGI ourselves. Returns (status, headers, events).
    """
    start: dict = {}
    body = bytearray()
    disconnect = asyncio.Event()
    request_sent = False

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if body.count(b"event: price") >= min_events:
                disconnect.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), (b"accept", b"text/event-stream")],
        "client": ("test", 1234),
        "server": ("test", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout)

    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    events = []
    for block in re.split(r"\r?\n\r?\n", body.decode()):
        fields = dict(
            line.split(": ", 1) for line in re.split(r"\r?\n", block) if ": " in line
        )
        if fields.get("event") == "price":
            events.append(json.loads(fields["data"]))
    return start["status"], headers, events


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


async def test_generator_yields_price_event_per_ticker():
    price_cache.update("AAPL", 100.0)
    price_cache.update("AAPL", 101.5)
    price_cache.update("MSFT", 400.0)
    price_cache.update("MSFT", 399.0)
    price_cache.update("NVDA", 880.0)

    agen = _price_event_generator()
    try:
        events = await _take(agen, 3)
    finally:
        await agen.aclose()

    assert {e["event"] for e in events} == {"price"}
    payloads = {p["ticker"]: p for p in (json.loads(e["data"]) for e in events)}
    assert set(payloads) == {"AAPL", "MSFT", "NVDA"}
    for p in payloads.values():
        assert set(p) == EVENT_KEYS
        assert p["timestamp"]
    assert (payloads["AAPL"]["previous_price"], payloads["AAPL"]["price"]) == (100.0, 101.5)
    assert payloads["AAPL"]["direction"] == "up"
    assert payloads["MSFT"]["direction"] == "down"
    assert payloads["NVDA"]["direction"] == "flat"


async def test_generator_waits_for_first_price():
    agen = _price_event_generator()
    try:
        pending = asyncio.ensure_future(anext(agen))
        await asyncio.sleep(0.1)
        assert not pending.done()

        price_cache.update("AAPL", 150.0)
        event = await asyncio.wait_for(pending, 2.0)
        assert json.loads(event["data"])["ticker"] == "AAPL"
    finally:
        await agen.aclose()


async def test_generator_reflects_cache_changes_between_ticks():
    price_cache.update("AAPL", 150.0)
    agen = _price_event_generator()
    try:
        await _take(agen, 1)
        price_cache.remove("AAPL")
        price_cache.update("PYPL", 60.0)
        event = (await _take(agen, 1))[0]
        assert json.loads(event["data"])["ticker"] == "PYPL"
    finally:
        await agen.aclose()


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------


async def test_endpoint_streams_price_events():
    price_cache.update("AAPL", 150.0)
    price_cache.update("AAPL", 149.0)
    price_cache.update("TSLA", 250.0)

    status, headers, events = await _stream("/api/stream/prices", min_events=4)

    assert status == 200
    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-store"
    assert len(events) >= 4  # at least two ticks of both tickers
    for e in events:
        assert set(e) == EVENT_KEYS
        assert e["direction"] in {"up", "down", "flat"}
        assert isinstance(e["price"], float) and isinstance(e["previous_price"], float)
    aapl = next(e for e in events if e["ticker"] == "AAPL")
    assert (aapl["price"], aapl["previous_price"], aapl["direction"]) == (149.0, 150.0, "down")
    assert {e["ticker"] for e in events} == {"AAPL", "TSLA"}
