"""Tests for portfolio endpoints: trade execution, positions, P&L."""

import pytest

from app.market.cache import price_cache


@pytest.fixture(autouse=True)
def seed_prices():
    """Seed price cache with test prices."""
    price_cache.update("AAPL", 150.0)
    price_cache.update("GOOGL", 175.0)
    price_cache.update("TSLA", 250.0)
    yield
    # Clean up
    price_cache._prices.clear()


@pytest.mark.asyncio
async def test_get_portfolio_initial(client):
    resp = await client.get("/api/portfolio")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cash_balance"] == 10000.0
    assert data["total_value"] == 10000.0
    assert data["positions"] == []


@pytest.mark.asyncio
async def test_buy_reduces_cash(client):
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "buy"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ticker"] == "AAPL"
    assert data["side"] == "buy"
    assert data["quantity"] == 10
    assert data["price"] == 150.0

    # Check portfolio
    resp = await client.get("/api/portfolio")
    data = resp.json()
    assert data["cash_balance"] == 10000.0 - (10 * 150.0)
    assert len(data["positions"]) == 1
    assert data["positions"][0]["ticker"] == "AAPL"
    assert data["positions"][0]["quantity"] == 10


@pytest.mark.asyncio
async def test_sell_increases_cash(client):
    # First buy
    await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "buy"},
    )
    # Then sell
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 5, "side": "sell"},
    )
    assert resp.status_code == 200

    resp = await client.get("/api/portfolio")
    data = resp.json()
    # Bought 10 @ 150 = -1500, sold 5 @ 150 = +750, net cash = 10000 - 1500 + 750 = 9250
    assert data["cash_balance"] == 9250.0
    assert data["positions"][0]["quantity"] == 5


@pytest.mark.asyncio
async def test_sell_entire_position_removes_it(client):
    await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "buy"},
    )
    await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "sell"},
    )
    resp = await client.get("/api/portfolio")
    data = resp.json()
    assert data["positions"] == []


@pytest.mark.asyncio
async def test_buy_insufficient_cash(client):
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10000, "side": "buy"},
    )
    assert resp.status_code == 400
    assert "insufficient cash" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_sell_insufficient_shares(client):
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "sell"},
    )
    assert resp.status_code == 400
    assert "insufficient shares" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_invalid_side(client):
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 1, "side": "short"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_zero_quantity(client):
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 0, "side": "buy"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_no_price_available(client):
    resp = await client.post(
        "/api/portfolio/trade",
        json={"ticker": "ZZZZ", "quantity": 1, "side": "buy"},
    )
    assert resp.status_code == 400
    assert "no price" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_trade_creates_snapshot(client):
    await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 1, "side": "buy"},
    )
    resp = await client.get("/api/portfolio/history")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_pnl_calculation(client):
    # Buy at 150
    await client.post(
        "/api/portfolio/trade",
        json={"ticker": "AAPL", "quantity": 10, "side": "buy"},
    )

    resp = await client.get("/api/portfolio")
    data = resp.json()
    pos = data["positions"][0]
    # Current price == avg_cost == 150, so PnL should be 0
    assert pos["unrealized_pnl"] == 0.0
    assert pos["pnl_percent"] == 0.0


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------

POSITION_KEYS = {"ticker", "quantity", "avg_cost", "current_price", "unrealized_pnl", "pnl_percent"}
TRADE_KEYS = {"id", "ticker", "side", "quantity", "price", "executed_at"}


async def _trade(client, ticker, side, quantity):
    return await client.post(
        "/api/portfolio/trade", json={"ticker": ticker, "quantity": quantity, "side": side}
    )


async def test_portfolio_response_shape(client):
    await _trade(client, "AAPL", "buy", 2)
    data = (await client.get("/api/portfolio")).json()
    assert set(data) == {"cash_balance", "total_value", "positions"}
    assert set(data["positions"][0]) == POSITION_KEYS


async def test_trade_response_shape_normalizes_input(client):
    resp = await _trade(client, " aapl ", "BUY", 1)
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == TRADE_KEYS
    assert data["ticker"] == "AAPL"
    assert data["side"] == "buy"
    assert data["id"] and data["executed_at"]


@pytest.mark.parametrize(
    "body",
    [
        {"ticker": "AAPL", "side": "buy"},
        {"ticker": "AAPL", "quantity": "lots", "side": "buy"},
        {"quantity": 1, "side": "buy"},
    ],
)
async def test_trade_malformed_body_is_422(client, body):
    resp = await client.post("/api/portfolio/trade", json=body)
    assert resp.status_code == 422


async def test_negative_quantity_rejected(client):
    resp = await _trade(client, "AAPL", "buy", -5)
    assert resp.status_code == 400
    assert "positive" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# P&L and valuation
# ---------------------------------------------------------------------------


async def test_unrealized_pnl_tracks_price_moves(client):
    await _trade(client, "AAPL", "buy", 10)  # @150
    price_cache.update("AAPL", 165.0)

    data = (await client.get("/api/portfolio")).json()
    pos = data["positions"][0]
    assert pos["avg_cost"] == 150.0
    assert pos["current_price"] == 165.0
    assert pos["unrealized_pnl"] == 150.0
    assert pos["pnl_percent"] == 10.0
    assert data["cash_balance"] == 8500.0
    assert data["total_value"] == 8500.0 + 1650.0


async def test_buying_more_averages_cost(client):
    await _trade(client, "AAPL", "buy", 10)  # @150
    price_cache.update("AAPL", 180.0)
    await _trade(client, "AAPL", "buy", 5)  # @180

    pos = (await client.get("/api/portfolio")).json()["positions"][0]
    assert pos["quantity"] == 15
    assert pos["avg_cost"] == 160.0  # (10*150 + 5*180) / 15


async def test_total_value_with_multiple_positions(client):
    await _trade(client, "AAPL", "buy", 10)  # 1500
    await _trade(client, "TSLA", "buy", 4)  # 1000
    price_cache.update("TSLA", 200.0)

    data = (await client.get("/api/portfolio")).json()
    assert {p["ticker"] for p in data["positions"]} == {"AAPL", "TSLA"}
    assert data["cash_balance"] == 7500.0
    assert data["total_value"] == 7500.0 + 1500.0 + 800.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_insufficient_cash_leaves_state_unchanged(client):
    await _trade(client, "AAPL", "buy", 10)
    before = (await client.get("/api/portfolio")).json()

    resp = await _trade(client, "AAPL", "buy", 1000)
    assert resp.status_code == 400
    assert "$150000.00" in resp.json()["detail"]
    assert (await client.get("/api/portfolio")).json() == before


async def test_buy_exactly_all_cash_allowed(client):
    price_cache.update("AAPL", 100.0)
    resp = await _trade(client, "AAPL", "buy", 100)
    assert resp.status_code == 200
    assert (await client.get("/api/portfolio")).json()["cash_balance"] == 0.0


async def test_oversell_held_position_rejected(client):
    await _trade(client, "AAPL", "buy", 5)
    resp = await _trade(client, "AAPL", "sell", 6)
    assert resp.status_code == 400
    detail = resp.json()["detail"].lower()
    assert "insufficient shares" in detail and "hold 5" in detail

    data = (await client.get("/api/portfolio")).json()
    assert data["positions"][0]["quantity"] == 5
    assert data["cash_balance"] == 9250.0


async def test_sell_at_a_loss(client):
    await _trade(client, "AAPL", "buy", 10)  # @150
    price_cache.update("AAPL", 120.0)

    pos = (await client.get("/api/portfolio")).json()["positions"][0]
    assert pos["unrealized_pnl"] == -300.0
    assert pos["pnl_percent"] == -20.0

    resp = await _trade(client, "AAPL", "sell", 4)
    assert resp.status_code == 200
    assert resp.json()["price"] == 120.0

    data = (await client.get("/api/portfolio")).json()
    # 10000 - 1500 + 4*120
    assert data["cash_balance"] == 8980.0
    pos = data["positions"][0]
    assert pos["quantity"] == 6
    assert pos["avg_cost"] == 150.0  # selling does not change cost basis
    assert pos["unrealized_pnl"] == -180.0


async def test_sell_all_at_a_loss_realizes_loss(client):
    await _trade(client, "AAPL", "buy", 10)
    price_cache.update("AAPL", 120.0)
    await _trade(client, "AAPL", "sell", 10)

    data = (await client.get("/api/portfolio")).json()
    assert data["positions"] == []
    assert data["cash_balance"] == 9700.0
    assert data["total_value"] == 9700.0


async def test_sell_to_zero_then_rebuy_starts_fresh_cost_basis(client):
    await _trade(client, "AAPL", "buy", 10)  # @150
    await _trade(client, "AAPL", "sell", 10)
    price_cache.update("AAPL", 100.0)
    await _trade(client, "AAPL", "buy", 1)

    pos = (await client.get("/api/portfolio")).json()["positions"][0]
    assert pos["quantity"] == 1
    assert pos["avg_cost"] == 100.0


async def test_fractional_sell_to_zero_removes_position(client):
    """0.1 + 0.2 != 0.3 in floats: selling 0.3 must still close the position."""
    await _trade(client, "AAPL", "buy", 0.1)
    await _trade(client, "AAPL", "buy", 0.2)
    resp = await _trade(client, "AAPL", "sell", 0.3)
    assert resp.status_code == 200
    assert (await client.get("/api/portfolio")).json()["positions"] == []


async def test_fractional_partial_sells_can_close_position(client):
    """0.3 - 0.1 < 0.2 in floats: the last 0.2 must still be sellable."""
    await _trade(client, "AAPL", "buy", 0.3)
    assert (await _trade(client, "AAPL", "sell", 0.1)).status_code == 200
    resp = await _trade(client, "AAPL", "sell", 0.2)
    assert resp.status_code == 200, resp.json()
    assert (await client.get("/api/portfolio")).json()["positions"] == []


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


async def test_history_empty_initially(client):
    resp = await client.get("/api/portfolio/history")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_history_records_post_trade_values_in_order(client):
    await _trade(client, "AAPL", "buy", 10)  # value stays 10000
    price_cache.update("AAPL", 160.0)
    await _trade(client, "AAPL", "sell", 5)  # 8500 + 800 + 5*160 = 10100

    history = (await client.get("/api/portfolio/history")).json()
    assert [set(h) for h in history] == [{"total_value", "recorded_at"}] * 2
    assert [h["total_value"] for h in history] == [10000.0, 10100.0]
    assert history[0]["recorded_at"] <= history[1]["recorded_at"]


async def test_rejected_trade_records_no_snapshot(client):
    await _trade(client, "AAPL", "sell", 1)
    assert (await client.get("/api/portfolio/history")).json() == []
