"""Market data models."""

from pydantic import BaseModel


class PriceUpdate(BaseModel):
    """A single price update for a ticker."""

    ticker: str
    price: float
    previous_price: float
    open_price: float  # session reference price; daily change is measured from it
    timestamp: str
    direction: str  # "up", "down", or "flat"
