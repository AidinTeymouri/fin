"""Shared trade execution service.

Both POST /api/portfolio/trade and the chat endpoint's auto-executed trades go
through ``execute_trade`` so validation, pricing, avg-cost math and the
post-trade snapshot are identical regardless of where the order came from.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from app.market.cache import price_cache
from app.market.provider import get_provider

# Fractional quantities are floats, so 0.1 + 0.2 != 0.3. Holdings within this
# tolerance of the sell size count as enough, and a remainder this small is zero.
QUANTITY_EPSILON = 1e-9


class TradeError(Exception):
    """A trade was rejected by validation. ``str(err)`` is user-facing."""


@dataclass
class TradeResult:
    id: str
    ticker: str
    side: str
    quantity: float
    price: float
    executed_at: str


async def take_snapshot(db):
    """Calculate portfolio value and insert a snapshot row."""
    cursor = await db.execute(
        "SELECT cash_balance FROM users_profile WHERE id = 'default'"
    )
    user = await cursor.fetchone()
    cash = user["cash_balance"]

    cursor = await db.execute(
        "SELECT ticker, quantity, avg_cost FROM positions WHERE user_id = 'default'"
    )
    positions = await cursor.fetchall()

    total = cash
    for pos in positions:
        update = price_cache.get(pos["ticker"])
        price = update.price if update else pos["avg_cost"]
        total += pos["quantity"] * price

    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO portfolio_snapshots (id, user_id, total_value, recorded_at) VALUES (?, 'default', ?, ?)",
        (str(uuid.uuid4()), round(total, 2), now),
    )


async def _on_watchlist(db, ticker: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM watchlist WHERE user_id = 'default' AND ticker = ?",
        (ticker,),
    )
    return await cursor.fetchone() is not None


async def execute_trade(db, ticker: str, side: str, quantity: float) -> TradeResult:
    """Execute a market order at the current cached price and commit it.

    Raises TradeError (before any write) if the order is invalid.
    """
    ticker = ticker.upper().strip()
    side = side.lower().strip()

    if side not in ("buy", "sell"):
        raise TradeError("side must be 'buy' or 'sell'")
    if quantity <= 0:
        raise TradeError("quantity must be positive")

    update = price_cache.get(ticker)
    if update is None:
        raise TradeError(f"No price available for {ticker}")
    current_price = update.price

    cursor = await db.execute(
        "SELECT cash_balance FROM users_profile WHERE id = 'default'"
    )
    user = await cursor.fetchone()
    cash = user["cash_balance"]

    now = datetime.now(timezone.utc).isoformat()
    trade_id = str(uuid.uuid4())
    closed_position = False

    if side == "buy":
        total_cost = quantity * current_price
        if cash < total_cost:
            raise TradeError(
                f"Insufficient cash: need ${total_cost:.2f} to buy {quantity:g} {ticker}, have ${cash:.2f}"
            )

        # Update or create position
        cursor = await db.execute(
            "SELECT quantity, avg_cost FROM positions WHERE user_id = 'default' AND ticker = ?",
            (ticker,),
        )
        existing = await cursor.fetchone()
        if existing:
            old_qty = existing["quantity"]
            old_avg = existing["avg_cost"]
            new_qty = old_qty + quantity
            new_avg = ((old_qty * old_avg) + (quantity * current_price)) / new_qty
            await db.execute(
                "UPDATE positions SET quantity = ?, avg_cost = ?, updated_at = ? WHERE user_id = 'default' AND ticker = ?",
                (new_qty, new_avg, now, ticker),
            )
        else:
            await db.execute(
                "INSERT INTO positions (id, user_id, ticker, quantity, avg_cost, updated_at) VALUES (?, 'default', ?, ?, ?, ?)",
                (str(uuid.uuid4()), ticker, quantity, current_price, now),
            )

        # Deduct cash
        await db.execute(
            "UPDATE users_profile SET cash_balance = cash_balance - ? WHERE id = 'default'",
            (total_cost,),
        )

    else:  # sell
        cursor = await db.execute(
            "SELECT quantity FROM positions WHERE user_id = 'default' AND ticker = ?",
            (ticker,),
        )
        existing = await cursor.fetchone()
        if not existing or existing["quantity"] < quantity - QUANTITY_EPSILON:
            held = existing["quantity"] if existing else 0
            raise TradeError(
                f"Insufficient shares: want to sell {quantity:g} {ticker}, hold {held:g}"
            )

        new_qty = existing["quantity"] - quantity
        closed_position = new_qty <= QUANTITY_EPSILON
        if closed_position:
            await db.execute(
                "DELETE FROM positions WHERE user_id = 'default' AND ticker = ?",
                (ticker,),
            )
        else:
            await db.execute(
                "UPDATE positions SET quantity = ?, updated_at = ? WHERE user_id = 'default' AND ticker = ?",
                (new_qty, now, ticker),
            )

        # Add proceeds to cash
        proceeds = quantity * current_price
        await db.execute(
            "UPDATE users_profile SET cash_balance = cash_balance + ? WHERE id = 'default'",
            (proceeds,),
        )

    # Log the trade
    await db.execute(
        "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) VALUES (?, 'default', ?, ?, ?, ?, ?)",
        (trade_id, ticker, side, quantity, current_price, now),
    )

    # Take post-trade snapshot
    await take_snapshot(db)

    await db.commit()

    # A closed position no longer needs prices unless the watchlist still wants them.
    if closed_position and not await _on_watchlist(db, ticker):
        provider = get_provider()
        if provider is not None:
            await provider.remove_ticker(ticker)
        price_cache.remove(ticker)

    return TradeResult(
        id=trade_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        price=current_price,
        executed_at=now,
    )
