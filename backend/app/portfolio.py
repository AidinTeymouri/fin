"""Portfolio API routes: positions, trading, snapshots."""

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.database import get_db
from app.market.cache import price_cache
from app.trading import TradeError, execute_trade

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class TradeRequest(BaseModel):
    ticker: str
    quantity: float
    side: str  # "buy" or "sell"


class Position(BaseModel):
    ticker: str
    quantity: float
    avg_cost: float
    current_price: float
    unrealized_pnl: float
    pnl_percent: float


class PortfolioResponse(BaseModel):
    cash_balance: float
    total_value: float
    positions: list[Position]


class TradeResponse(BaseModel):
    id: str
    ticker: str
    side: str
    quantity: float
    price: float
    executed_at: str


class SnapshotResponse(BaseModel):
    total_value: float
    recorded_at: str


@router.get("", response_model=PortfolioResponse)
async def get_portfolio():
    """Return current positions, cash, total value, unrealized P&L."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT cash_balance FROM users_profile WHERE id = 'default'"
        )
        user = await cursor.fetchone()
        cash = user["cash_balance"]

        cursor = await db.execute(
            "SELECT ticker, quantity, avg_cost FROM positions WHERE user_id = 'default'"
        )
        rows = await cursor.fetchall()

        positions = []
        total_value = cash
        for row in rows:
            ticker = row["ticker"]
            qty = row["quantity"]
            avg_cost = row["avg_cost"]
            update = price_cache.get(ticker)
            current_price = update.price if update else avg_cost
            unrealized_pnl = (current_price - avg_cost) * qty
            pnl_percent = ((current_price - avg_cost) / avg_cost * 100) if avg_cost else 0
            total_value += current_price * qty
            positions.append(Position(
                ticker=ticker,
                quantity=qty,
                avg_cost=round(avg_cost, 2),
                current_price=round(current_price, 2),
                unrealized_pnl=round(unrealized_pnl, 2),
                pnl_percent=round(pnl_percent, 2),
            ))

        return PortfolioResponse(
            cash_balance=round(cash, 2),
            total_value=round(total_value, 2),
            positions=positions,
        )
    finally:
        await db.close()


@router.post("/trade", response_model=TradeResponse)
async def trade(body: TradeRequest):
    """Execute a market order at current cached price."""
    db = await get_db()
    try:
        result = await execute_trade(db, body.ticker, body.side, body.quantity)
    except TradeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        await db.close()
    return TradeResponse(**asdict(result))


@router.get("/history", response_model=list[SnapshotResponse])
async def get_portfolio_history():
    """Return portfolio snapshots for P&L chart."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT total_value, recorded_at FROM portfolio_snapshots WHERE user_id = 'default' ORDER BY recorded_at"
        )
        rows = await cursor.fetchall()
        return [
            SnapshotResponse(total_value=row["total_value"], recorded_at=row["recorded_at"])
            for row in rows
        ]
    finally:
        await db.close()
