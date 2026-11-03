"""FINN market-data MCP server (stdio).

Exposes the typed Downstox adapters as MCP tools so any MCP client - including
the FINN Pydantic AI agent - can fetch permitted public Indian market data.
Run: python -m finn.mcp_server
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from .market_data import DownstoxClient, MarketDataError

mcp = FastMCP("finn-market-data")

# Live by default; FINN_FIXTURES points at recorded samples for offline dev/tests.
import os
_fixtures = os.environ.get("FINN_FIXTURES")
_client = DownstoxClient(fixtures_dir=Path(_fixtures) if _fixtures else None)


def _dump(model) -> dict:
    return model.model_dump(mode="json")


@mcp.tool
def get_stock_snapshot(symbol: str) -> dict:
    """Live/delayed quote + fundamentals for one NSE symbol, with upstream
    freshness (quoted_at, stale) and provenance. Third-party public data, not
    an official exchange feed."""
    try:
        return _dump(_client.get_stock(symbol))
    except MarketDataError as exc:
        return {"error": str(exc), "symbol": symbol.upper()}


@mcp.tool
def get_latest_announcements(symbol: Optional[str] = None, limit: int = 10) -> dict:
    """Merged NSE+BSE corporate announcements feed. Includes per-exchange
    source health so a failed/withheld feed is visible rather than looking
    like 'no filings'."""
    try:
        return _dump(_client.get_announcements(symbol=symbol, limit=limit))
    except MarketDataError as exc:
        return {"error": str(exc)}


@mcp.tool
def get_management_guidance(symbol: str) -> dict:
    """Management statements from filings with verbatim quote, target bounds
    (range/lower/upper), hedge wording, timeframe and source PDF URL."""
    try:
        promises = _client.get_promises(symbol)
        return {"symbol": symbol.upper(),
                "promises": [_dump(p) for p in promises],
                "count": len(promises)}
    except MarketDataError as exc:
        return {"error": str(exc), "symbol": symbol.upper()}


@mcp.tool
def get_market_status() -> dict:
    """Whether NSE is open now, with reason and last/next trading day."""
    try:
        return _dump(_client.get_market_status())
    except MarketDataError as exc:
        return {"error": str(exc)}


if __name__ == "__main__":
    mcp.run()
