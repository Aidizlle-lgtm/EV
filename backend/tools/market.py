"""Read-only market-data tools for F.R.I.D.A.Y.

Tagged `specialist_only`, so E.V. never calls them herself (she delegates), and
level READ. Each result wraps the data with a provenance object built in code.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from ..integrations import market_data
from .registry import Level, ToolContext, ToolError, tool

TAGS = {"specialist_only", "market"}


class SymbolArgs(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=10, description="Ticker symbol, e.g. MSFT")

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        v = v.strip().upper()
        if not v or not v[0].isalpha() or not all(c.isalnum() or c in ".-" for c in v):
            raise ValueError("Use a ticker symbol of letters/numbers, e.g. MSFT or BRK.B")
        return v


class HistoryArgs(SymbolArgs):
    range: Literal["1m", "3m", "6m", "1y", "5y"] = Field("6m", description="How far back")


class NewsArgs(SymbolArgs):
    limit: int = Field(5, ge=1, le=10)


def _src() -> market_data.MarketSource:
    src = market_data.active()
    if src is None:
        raise ToolError("unavailable", "No market-data source is selected.")
    return src


def _wrap(symbol: str, src: market_data.MarketSource, data: dict) -> dict:
    return {"symbol": symbol, "data": data, "provenance": src.provenance(data.get("as_of"))}


@tool("market_quote", level=Level.READ, args=SymbolArgs, category="markets", tags=TAGS, requires=market_data.setup_issue,
      description="Latest price quote for a stock symbol, with source and timestamp (provenance).")
async def market_quote(a: SymbolArgs, ctx: ToolContext):
    src = _src()
    return _wrap(a.symbol, src, await src.quote(a.symbol))


@tool("market_company_overview", level=Level.READ, args=SymbolArgs, category="markets", tags=TAGS,
      requires=market_data.setup_issue,
      description="Company profile and key metrics (market cap, P/E, EPS, 52-week range) for a stock symbol.")
async def market_company_overview(a: SymbolArgs, ctx: ToolContext):
    src = _src()
    return _wrap(a.symbol, src, await src.overview(a.symbol))


@tool("market_price_history", level=Level.READ, args=HistoryArgs, category="markets", tags=TAGS,
      requires=market_data.setup_issue,
      description="Daily closing-price history for a symbol over a range (1m, 3m, 6m, 1y, 5y), with summary stats.")
async def market_price_history(a: HistoryArgs, ctx: ToolContext):
    src = _src()
    return _wrap(a.symbol, src, await src.history(a.symbol, a.range))


@tool("market_news", level=Level.READ, args=NewsArgs, category="markets", tags=TAGS, requires=market_data.setup_issue,
      description="Recent news headlines about a symbol, each with its publisher and time.")
async def market_news(a: NewsArgs, ctx: ToolContext):
    src = _src()
    return _wrap(a.symbol, src, await src.news(a.symbol, a.limit))
