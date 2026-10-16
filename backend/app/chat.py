"""POST /api/chat — LLM chat with auto-execution of trades and watchlist changes."""

import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field, ValidationError

from litellm import acompletion

from app.database import get_db
from app.trading import TradeError, execute_trade

log = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str


class TradeAction(BaseModel):
    ticker: str
    side: Literal["buy", "sell"]
    quantity: float = Field(gt=0, description="Number of shares; must be > 0")


class WatchlistChange(BaseModel):
    ticker: str
    action: Literal["add", "remove"]


class LLMResponse(BaseModel):
    """Structured output the LLM must produce (sent to it as a JSON schema)."""

    message: str
    trades: list[TradeAction] = Field(default_factory=list)
    watchlist_changes: list[WatchlistChange] = Field(default_factory=list)


class ExecutedTrade(BaseModel):
    ticker: str
    side: str
    quantity: float
    price: float


class ChatResponse(BaseModel):
    """API response. Lists only the actions that actually executed; failures
    are explained in ``message``."""

    message: str
    trades: list[ExecutedTrade] | None = None
    watchlist_changes: list[WatchlistChange] | None = None


# ---------------------------------------------------------------------------
# LLM configuration
# ---------------------------------------------------------------------------

LLM_MODEL = "openrouter/openai/gpt-oss-120b"

# Pin inference to Cerebras (SPEC §9). allow_fallbacks is off: if Cerebras
# can't serve the request we'd rather show a friendly error than silently route
# to a slower provider. require_parameters makes OpenRouter reject the request
# instead of quietly ignoring response_format.
OPENROUTER_PROVIDER = {
    "order": ["cerebras"],
    "allow_fallbacks": False,
    "require_parameters": True,
}


def _strict_json_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema adapted for strict structured outputs.

    Strict mode requires every object to list all its properties as required
    and set ``additionalProperties: false``. Numeric bounds aren't supported by
    every strict-mode provider, so they're dropped here and enforced by pydantic
    when parsing.
    """

    def visit(node):
        if isinstance(node, dict):
            node.pop("exclusiveMinimum", None)
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    schema = model.model_json_schema()
    visit(schema)
    return schema


RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "chat_response",
        "strict": True,
        "schema": _strict_json_schema(LLMResponse),
    },
}

MISSING_KEY_MESSAGE = (
    "The AI assistant isn't configured yet: set OPENROUTER_API_KEY in the .env "
    "file and restart (or set LLM_MOCK=true to use mock responses)."
)
LLM_UNAVAILABLE_MESSAGE = (
    "Sorry, I couldn't reach the AI model just now. Please try again in a moment."
)
LLM_MALFORMED_MESSAGE = (
    "Sorry, I got a garbled response from the AI model and didn't act on it. "
    "Please try again or rephrase your request."
)


class LLMError(Exception):
    """The LLM call failed or returned unusable output. ``str(err)`` is user-facing."""


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are FinAlly, an AI trading assistant inside a simulated trading workstation.

Your capabilities:
- Analyze the user's portfolio composition, risk concentration, and P&L
- Suggest trades with clear reasoning
- Execute trades when the user asks or agrees (by including them in your response)
- Manage the watchlist (add/remove tickers)
- Be concise and data-driven

Respond with JSON matching the provided schema:
- "message": your conversational response to the user (always required).
- "trades": trades to execute now; use [] when not trading.
- "watchlist_changes": watchlist edits to apply now; use [] when none.

Rules:
- side must be "buy" or "sell"; quantity must be a positive number of shares.
- action must be "add" or "remove".
- Only execute trades when the user explicitly asks or agrees.
- Keep responses concise."""


# ---------------------------------------------------------------------------
# Portfolio context
# ---------------------------------------------------------------------------


async def _load_portfolio_context(db) -> str:
    """Build a text summary of the user's portfolio for the LLM."""
    # Cash balance
    cur = await db.execute(
        "SELECT cash_balance FROM users_profile WHERE id = 'default'"
    )
    row = await cur.fetchone()
    cash = row["cash_balance"] if row else 10000.0

    # Positions
    cur = await db.execute(
        "SELECT ticker, quantity, avg_cost FROM positions WHERE user_id = 'default'"
    )
    positions = await cur.fetchall()

    # Watchlist
    cur = await db.execute(
        "SELECT ticker FROM watchlist WHERE user_id = 'default'"
    )
    watchlist = [r["ticker"] for r in await cur.fetchall()]

    lines = [f"Cash: ${cash:,.2f}"]

    if positions:
        lines.append("Positions:")
        total_cost = 0.0
        for p in positions:
            value = p["quantity"] * p["avg_cost"]
            total_cost += value
            lines.append(
                f"  {p['ticker']}: {p['quantity']} shares @ avg ${p['avg_cost']:.2f}"
            )
        lines.append(f"Total invested (at cost): ${total_cost:,.2f}")
    else:
        lines.append("Positions: none")

    lines.append(f"Watchlist: {', '.join(watchlist) if watchlist else 'empty'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------


async def _load_history(db, limit: int = 20) -> list[dict]:
    """Load recent chat messages for context."""
    cur = await db.execute(
        "SELECT role, content FROM chat_messages "
        "WHERE user_id = 'default' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cur.fetchall()
    # Reverse so oldest first
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]




# ---------------------------------------------------------------------------
# Mock mode
# ---------------------------------------------------------------------------

# "buy 1 AAPL", "sell 2 shares of TSLA", "Buy 0.5 shares NVDA"
_MOCK_TRADE_RE = re.compile(
    r"\b(buy|sell)\s+(\d+(?:\.\d+)?)\s+(?:shares?\s+(?:of\s+)?)?\$?([a-z][a-z.]*)\b",
    re.IGNORECASE,
)


def _mock_response(message: str) -> LLMResponse:
    """Return deterministic mock responses (LLM_MOCK=true).

    Trades execute only for the explicit form "<buy|sell> <quantity> <TICKER>"
    (e.g. "buy 1 AAPL"), so E2E tests can assert inline trade confirmations
    without vaguer messages like "what should I buy?" moving money.
    """
    lower = message.lower()

    trade = _MOCK_TRADE_RE.search(message)
    if trade:
        side = trade.group(1).lower()
        quantity = float(trade.group(2))
        ticker = trade.group(3).upper()
        return LLMResponse(
            message=f"Placing a market order to {side} {quantity:g} {ticker}.",
            trades=[TradeAction(ticker=ticker, side=side, quantity=quantity)],
        )

    if "remove" in lower:
        return LLMResponse(
            message="Removing NFLX from your watchlist.",
            watchlist_changes=[WatchlistChange(ticker="NFLX", action="remove")],
        )

    if "watch" in lower or "add" in lower:
        return LLMResponse(
            message="Adding PYPL to your watchlist.",
            watchlist_changes=[WatchlistChange(ticker="PYPL", action="add")],
        )

    if "portfolio" in lower or "positions" in lower or "holdings" in lower:
        return LLMResponse(
            message="Your portfolio currently has $10,000.00 in cash with no open positions. "
            "You're watching AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX. "
            "Would you like to make a trade?"
        )

    if re.search(r"\b(hi|hello|hey)\b", lower):
        return LLMResponse(
            message="Hello! I'm FinAlly, your AI trading assistant. "
            "I can analyze your portfolio, suggest trades, and manage your watchlist. "
            "How can I help you today?"
        )

    return LLMResponse(
        message="I can help you trade, analyze your portfolio, or manage your watchlist. "
        'Try "buy 1 AAPL" to place a trade. What would you like to do?'
    )


# ---------------------------------------------------------------------------
# Watchlist changes
# ---------------------------------------------------------------------------


async def _execute_watchlist_change(db, ticker: str, action: str) -> str | None:
    """Add/remove a ticker from watchlist. Returns error string on failure."""
    from app.watchlist import WatchlistError, add_to_watchlist, remove_from_watchlist

    try:
        if action == "add":
            await add_to_watchlist(db, ticker)
        elif action == "remove":
            await remove_from_watchlist(db, ticker)
        else:
            return f"Invalid watchlist action: {action}"
    except WatchlistError as e:
        return str(e)

    return None


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


async def _call_llm(messages: list[dict]) -> LLMResponse:
    """Call the LLM via LiteLLM -> OpenRouter -> Cerebras with structured outputs.

    Raises LLMError (with a user-facing message) on network/provider errors or
    output that doesn't match the schema.
    """
    try:
        response = await acompletion(
            model=LLM_MODEL,
            messages=messages,
            response_format=RESPONSE_FORMAT,
            extra_body={"provider": OPENROUTER_PROVIDER},
        )
    except Exception as e:
        log.exception("LLM call failed")
        raise LLMError(LLM_UNAVAILABLE_MESSAGE) from e

    content = None
    try:
        content = response.choices[0].message.content
        return LLMResponse.model_validate_json(content or "")
    except (AttributeError, IndexError, TypeError, ValidationError) as e:
        log.warning("Malformed LLM output (%s); raw content: %r", e, content)
        raise LLMError(LLM_MALFORMED_MESSAGE) from e


async def _get_llm_response(db, user_message: str) -> LLMResponse:
    """Produce the assistant's response, degrading to a plain message on failure."""
    if os.environ.get("LLM_MOCK", "").lower() == "true":
        return _mock_response(user_message)

    if not os.environ.get("OPENROUTER_API_KEY"):
        return LLMResponse(message=MISSING_KEY_MESSAGE)

    portfolio_ctx = await _load_portfolio_context(db)
    history = await _load_history(db)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Current portfolio state:\n{portfolio_ctx}"},
        *history,
        {"role": "user", "content": user_message},
    ]
    try:
        return await _call_llm(messages)
    except LLMError as e:
        return LLMResponse(message=str(e))


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------


@router.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Send a message and receive a structured response with auto-executed actions."""
    db = await get_db()
    try:
        llm = await _get_llm_response(db, req.message)

        errors = []

        # Auto-execute trades through the same path as POST /api/portfolio/trade
        executed = []
        for trade in llm.trades:
            try:
                r = await execute_trade(db, trade.ticker, trade.side, trade.quantity)
            except TradeError as e:
                errors.append(str(e))
                continue
            executed.append(
                ExecutedTrade(
                    ticker=r.ticker, side=r.side, quantity=r.quantity, price=r.price
                )
            )

        # Auto-execute watchlist changes
        applied = []
        for change in llm.watchlist_changes:
            err = await _execute_watchlist_change(db, change.ticker, change.action)
            if err:
                errors.append(err)
            else:
                applied.append(change)

        message = llm.message
        if errors:
            message += "\n\nSome actions could not be completed:\n" + "\n".join(
                f"- {err}" for err in errors
            )

        result = ChatResponse(
            message=message,
            trades=executed or None,
            watchlist_changes=applied or None,
        )

        # Store messages
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
            "VALUES (?, 'default', 'user', ?, NULL, ?)",
            (str(uuid.uuid4()), req.message, now),
        )

        actions_json = None
        if result.trades or result.watchlist_changes:
            actions_json = json.dumps(result.model_dump(exclude={"message"}, exclude_none=True))

        await db.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
            "VALUES (?, 'default', 'assistant', ?, ?, ?)",
            (str(uuid.uuid4()), result.message, actions_json, now),
        )
        await db.commit()

        return result
    finally:
        await db.close()
