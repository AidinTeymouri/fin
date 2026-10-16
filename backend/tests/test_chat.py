"""Tests for POST /api/chat: mock mode, structured-output parsing, graceful
LLM failures, and the shared trade path. litellm is always mocked — no network."""

import json
import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app import chat, portfolio, trading
from app.chat import RESPONSE_FORMAT, LLMResponse
from app.main import app
from app.market.cache import price_cache


@pytest.fixture(autouse=True)
def seed_prices():
    price_cache.update("AAPL", 150.0)
    price_cache.update("TSLA", 250.0)
    yield
    price_cache._prices.clear()


@pytest.fixture
def mock_mode(monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "true")


@pytest.fixture
def live_mode(monkeypatch):
    """Real-LLM code path with a key set; tests must patch acompletion."""
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


@pytest_asyncio.fixture
async def client(tmp_path):
    """Async test client with a fresh per-test SQLite DB."""
    from app import database

    database.DB_PATH = str(tmp_path / "test.db")
    await database.init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def fake_llm(monkeypatch, content=None, exc=None):
    """Patch litellm.acompletion in app.chat; returns the list of call kwargs."""
    calls = []

    async def acompletion(**kwargs):
        calls.append(kwargs)
        if exc is not None:
            raise exc
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    monkeypatch.setattr(chat, "acompletion", acompletion)
    return calls


async def get_cash(client) -> float:
    return (await client.get("/api/portfolio")).json()["cash_balance"]


# ---------------------------------------------------------------------------
# Structured output schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "hi"},
        {"message": "hi", "trades": [], "watchlist_changes": []},
        {"message": "ok", "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 10}]},
        {"message": "ok", "trades": [{"ticker": "AAPL", "side": "sell", "quantity": 0.5}]},
        {"message": "ok", "watchlist_changes": [{"ticker": "PYPL", "action": "add"}]},
        {"message": "ok", "watchlist_changes": [{"ticker": "NFLX", "action": "remove"}]},
        {
            "message": "ok",
            "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 1}],
            "watchlist_changes": [{"ticker": "PYPL", "action": "add"}],
        },
    ],
)
def test_schema_parses_valid_shapes(payload):
    parsed = LLMResponse.model_validate_json(json.dumps(payload))
    assert parsed.message == payload["message"]
    assert len(parsed.trades) == len(payload.get("trades", []))
    assert len(parsed.watchlist_changes) == len(payload.get("watchlist_changes", []))


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        '{"message": "truncated',
        "[]",
        '{"trades": []}',  # missing message
        '{"message": "x", "trades": [{"ticker": "AAPL", "side": "hold", "quantity": 1}]}',
        '{"message": "x", "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 0}]}',
        '{"message": "x", "trades": [{"ticker": "AAPL", "side": "buy"}]}',
        '{"message": "x", "watchlist_changes": [{"ticker": "PYPL", "action": "pin"}]}',
    ],
)
def test_schema_rejects_malformed(content):
    with pytest.raises(ValidationError):
        LLMResponse.model_validate_json(content)


def test_response_format_is_strict_json_schema():
    assert RESPONSE_FORMAT["type"] == "json_schema"
    assert RESPONSE_FORMAT["json_schema"]["strict"] is True
    schema = RESPONSE_FORMAT["json_schema"]["schema"]

    objects = [schema, *schema["$defs"].values()]
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])

    trade = schema["$defs"]["TradeAction"]["properties"]
    assert trade["side"]["enum"] == ["buy", "sell"]
    watch = schema["$defs"]["WatchlistChange"]["properties"]
    assert watch["action"]["enum"] == ["add", "remove"]


# ---------------------------------------------------------------------------
# LLM call wiring (OpenRouter -> Cerebras, structured outputs)
# ---------------------------------------------------------------------------


async def test_llm_called_with_structured_output_and_cerebras(client, live_mode, monkeypatch):
    calls = fake_llm(monkeypatch, content='{"message": "Hi there", "trades": [], "watchlist_changes": []}')

    resp = await client.post("/api/chat", json={"message": "hello"})

    assert resp.status_code == 200
    assert resp.json() == {"message": "Hi there", "trades": None, "watchlist_changes": None}
    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["model"] == "openrouter/openai/gpt-oss-120b"
    assert kwargs["response_format"] is RESPONSE_FORMAT
    assert kwargs["extra_body"]["provider"]["order"] == ["cerebras"]
    assert kwargs["messages"][-1] == {"role": "user", "content": "hello"}


async def test_llm_trade_is_executed(client, live_mode, monkeypatch):
    fake_llm(
        monkeypatch,
        content='{"message": "Buying.", "trades": [{"ticker": "aapl", "side": "buy", "quantity": 2}], "watchlist_changes": []}',
    )

    resp = await client.post("/api/chat", json={"message": "buy two apple"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == "Buying."
    assert data["trades"] == [{"ticker": "AAPL", "side": "buy", "quantity": 2, "price": 150.0}]
    assert await get_cash(client) == 10000.0 - 300.0


@pytest.mark.parametrize(
    "content",
    [
        "this is not json",
        '{"trades": [{"ticker": "AAPL", "side": "buy", "quantity": 1}]}',
        '{"message": "x", "trades": [{"ticker": "AAPL", "side": "short", "quantity": 1}]}',
        None,
    ],
)
async def test_malformed_llm_output_returns_friendly_200(client, live_mode, monkeypatch, caplog, content):
    fake_llm(monkeypatch, content=content)

    with caplog.at_level(logging.WARNING, logger="app.chat"):
        resp = await client.post("/api/chat", json={"message": "buy 1 AAPL"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == chat.LLM_MALFORMED_MESSAGE
    assert data["trades"] is None
    assert data["watchlist_changes"] is None
    assert await get_cash(client) == 10000.0  # nothing executed
    assert repr(content) in caplog.text  # raw content logged


async def test_llm_exception_returns_friendly_200(client, live_mode, monkeypatch):
    fake_llm(monkeypatch, exc=ConnectionError("network down"))

    resp = await client.post("/api/chat", json={"message": "hello"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["message"] == chat.LLM_UNAVAILABLE_MESSAGE
    assert data["trades"] is None


async def test_missing_api_key_returns_setup_message(client, monkeypatch):
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    calls = fake_llm(monkeypatch, content='{"message": "should not be called"}')

    resp = await client.post("/api/chat", json={"message": "hello"})

    assert resp.status_code == 200
    assert "OPENROUTER_API_KEY" in resp.json()["message"]
    assert calls == []


async def test_mock_mode_works_without_api_key(client, mock_mode, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    calls = fake_llm(monkeypatch, content="unused")

    resp = await client.post("/api/chat", json={"message": "buy 1 AAPL"})

    assert resp.status_code == 200
    assert resp.json()["trades"][0]["ticker"] == "AAPL"
    assert calls == []


async def test_llm_trade_failures_surfaced_in_message(client, live_mode, monkeypatch):
    fake_llm(
        monkeypatch,
        content=json.dumps(
            {
                "message": "Done.",
                "trades": [
                    {"ticker": "AAPL", "side": "buy", "quantity": 1},
                    {"ticker": "TSLA", "side": "sell", "quantity": 5},
                    {"ticker": "AAPL", "side": "buy", "quantity": 1000},
                    {"ticker": "ZZZZ", "side": "buy", "quantity": 1},
                ],
                "watchlist_changes": [],
            }
        ),
    )

    resp = await client.post("/api/chat", json={"message": "do stuff"})

    assert resp.status_code == 200
    data = resp.json()
    # Only the trade that executed is reported as an action
    assert data["trades"] == [{"ticker": "AAPL", "side": "buy", "quantity": 1, "price": 150.0}]
    assert data["message"].startswith("Done.")
    assert "Insufficient shares" in data["message"]
    assert "Insufficient cash" in data["message"]
    assert "No price available for ZZZZ" in data["message"]
    assert await get_cash(client) == 10000.0 - 150.0


# ---------------------------------------------------------------------------
# Shared trade path
# ---------------------------------------------------------------------------


def test_both_endpoints_use_shared_execute_trade():
    assert chat.execute_trade is trading.execute_trade
    assert portfolio.execute_trade is trading.execute_trade


async def test_chat_and_manual_trades_share_avg_cost_and_snapshots(client, mock_mode, monkeypatch):
    spy_calls = []
    real = trading.execute_trade

    async def spy(db, ticker, side, quantity):
        spy_calls.append((ticker, side, quantity))
        return await real(db, ticker, side, quantity)

    monkeypatch.setattr(chat, "execute_trade", spy)
    monkeypatch.setattr(portfolio, "execute_trade", spy)

    resp = await client.post(
        "/api/portfolio/trade", json={"ticker": "AAPL", "quantity": 10, "side": "buy"}
    )
    assert resp.status_code == 200

    price_cache.update("AAPL", 200.0)
    resp = await client.post("/api/chat", json={"message": "buy 10 AAPL"})
    assert resp.json()["trades"][0]["price"] == 200.0

    assert spy_calls == [("AAPL", "buy", 10), ("AAPL", "buy", 10)]

    pos = (await client.get("/api/portfolio")).json()["positions"][0]
    assert pos["quantity"] == 20
    assert pos["avg_cost"] == 175.0

    history = (await client.get("/api/portfolio/history")).json()
    assert len(history) == 2  # post-trade snapshot from both endpoints


# ---------------------------------------------------------------------------
# Mock mode
# ---------------------------------------------------------------------------


async def test_health(client):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_chat_greeting(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "hello"})
    assert resp.status_code == 200
    data = resp.json()
    assert "FinAlly" in data["message"]
    assert data["trades"] is None
    assert data["watchlist_changes"] is None


async def test_mock_buy_executes_trade(client, mock_mode):
    """E2E trigger phrase: "buy 1 AAPL" executes a real trade."""
    resp = await client.post("/api/chat", json={"message": "buy 1 AAPL"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["trades"] == [{"ticker": "AAPL", "side": "buy", "quantity": 1, "price": 150.0}]
    assert "could not be completed" not in data["message"]
    assert await get_cash(client) == 10000.0 - 150.0


async def test_mock_trade_phrase_variants(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "Please buy 2 shares of tsla"})
    assert resp.json()["trades"][0] == {"ticker": "TSLA", "side": "buy", "quantity": 2, "price": 250.0}


async def test_mock_vague_buy_does_not_trade(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "What stocks should I buy?"})
    assert resp.status_code == 200
    assert resp.json()["trades"] is None
    assert await get_cash(client) == 10000.0


async def test_chat_sell_insufficient(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "sell 1 AAPL"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["trades"] is None
    assert "Insufficient shares" in data["message"]


async def test_chat_buy_then_sell(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "buy 10 TSLA"})
    assert resp.json()["trades"][0]["ticker"] == "TSLA"

    resp = await client.post("/api/chat", json={"message": "sell 10 TSLA"})
    data = resp.json()
    assert data["trades"][0]["side"] == "sell"
    assert "Insufficient" not in data["message"]
    assert await get_cash(client) == 10000.0


async def test_chat_buy_insufficient_cash(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "buy 1000 AAPL"})
    data = resp.json()
    assert data["trades"] is None
    assert "Insufficient cash" in data["message"]
    assert await get_cash(client) == 10000.0


async def test_chat_buy_unknown_ticker(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "buy 1 ZZZZ"})
    data = resp.json()
    assert data["trades"] is None
    assert "No price available for ZZZZ" in data["message"]


async def test_chat_watchlist_add(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "add PYPL to watchlist"})
    data = resp.json()
    assert data["watchlist_changes"] == [{"ticker": "PYPL", "action": "add"}]


async def test_chat_watchlist_remove(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "remove NFLX from my watchlist"})
    data = resp.json()
    assert data["watchlist_changes"] == [{"ticker": "NFLX", "action": "remove"}]


async def test_chat_portfolio_query(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "show my portfolio"})
    data = resp.json()
    assert "portfolio" in data["message"].lower()
    assert data["trades"] is None


async def test_chat_fallback(client, mock_mode):
    resp = await client.post("/api/chat", json={"message": "random nonsense xyz"})
    data = resp.json()
    assert "buy 1 AAPL" in data["message"]
