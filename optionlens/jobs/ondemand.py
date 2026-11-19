"""On-demand fetches triggered by API requests: chain freshness, first-visit history, cached Nasdaq
fundamentals. Concurrent requests for the same symbol share one fetch (per-symbol lock)."""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable

from ..analytics.calendar import NY, close_time, is_market_open, last_completed_session, now_ny
from ..models import normalize_symbol
from ..net import FetchError
from ..repo import bars as bars_repo
from ..repo import cache as cache_repo
from ..repo import options as options_repo
from ..sources import nasdaq
from . import chains, prices
from .universe import UnknownSymbol, ensure_symbol

__all__ = ["ensure_fresh", "ensure_history", "fetch_cached", "UnknownSymbol", "CACHE_TTL_S"]

_locks: dict[str, asyncio.Lock] = {}


def _lock(key: str) -> asyncio.Lock:
    lk = _locks.get(key)
    if lk is None:
        lk = _locks[key] = asyncio.Lock()
    return lk


def is_stale(fetched_at: datetime | None, now: datetime, max_age_s: float) -> bool:
    """Missing; or older than max_age_s while the market is open; or older than the last close otherwise."""
    if fetched_at is None:
        return True
    if is_market_open(now):
        return (now - fetched_at).total_seconds() > max_age_s
    last = last_completed_session(now)
    return fetched_at < datetime.combine(last, close_time(last), NY)


async def ensure_fresh(symbol: str, max_age_s: float = 120) -> bool:
    """Make sure option_latest/quotes/metrics for symbol are fresh, fetching once if stale.
    Creates the symbols row (and backfills price history) on first visit. Returns whether a chain is stored.
    Raises UnknownSymbol for symbols neither CBOE nor Nasdaq know."""
    symbol = normalize_symbol(symbol)
    async with _lock(f"chain:{symbol}"):
        row = await ensure_symbol(symbol)
        await ensure_history(symbol, row)
        fetched = await asyncio.to_thread(options_repo.latest_fetched_at, symbol)
        if row.get("optionable") is False or not is_stale(fetched, now_ny(), max_age_s):
            return fetched is not None
        try:
            snap = await chains.refresh_chain(symbol)
        except FetchError:
            return fetched is not None  # serve what we have
        return snap is not None or fetched is not None


async def ensure_history(symbol: str, row: dict | None = None) -> int:
    """Backfill 10y of daily bars if the symbol has none. Returns bars stored (0 if already present)."""
    symbol = normalize_symbol(symbol)
    async with _lock(f"history:{symbol}"):
        if await asyncio.to_thread(bars_repo.coverage, [symbol]):
            return 0
        row = row or await ensure_symbol(symbol)
        return await prices.backfill_symbol(row)


# ----------------------------------------------------------------------------- cached Nasdaq data
H = 3600
CACHE_TTL_S: dict[str, int] = {
    "info": 24 * H, "summary": 24 * H, "financials": 24 * H, "earnings_surprise": 24 * H,
    "profile": 7 * 24 * H, "insiders": 12 * H, "news": 30 * 60, "extended": 5 * 60,
}


def _fetcher(kind: str, symbol: str, asset: str, params: dict) -> Callable[[], Awaitable[Any]]:
    return {
        "info": lambda: nasdaq.fetch_info(symbol, asset),
        "summary": lambda: nasdaq.fetch_summary(symbol, asset),
        "financials": lambda: nasdaq.fetch_financials(symbol, bool(params.get("quarterly"))),
        "earnings_surprise": lambda: nasdaq.fetch_earnings_surprise(symbol),
        "profile": lambda: nasdaq.fetch_profile(symbol),
        "insiders": lambda: nasdaq.fetch_insiders(symbol),
        "news": lambda: nasdaq.fetch_news(symbol, asset),
        "extended": lambda: nasdaq.fetch_extended(symbol, params.get("market", "pre"), asset),
    }[kind]


async def fetch_cached(kind: str, symbol: str, **params: Any) -> Any:
    """Normalized Nasdaq payload of `kind` (see CACHE_TTL_S) for symbol, via http_cache with TTL.
    params: financials quarterly=bool; extended market='pre'|'post'. On fetch failure returns the stale
    cached payload if any, else raises FetchError. Unknown kind -> KeyError."""
    ttl = CACHE_TTL_S[kind]
    symbol = normalize_symbol(symbol)
    key = ":".join([kind, symbol, *(f"{k}={params[k]}" for k in sorted(params))])
    hit = await asyncio.to_thread(cache_repo.cache_get, key, ttl)
    if hit is not None:
        return hit
    async with _lock(f"cache:{key}"):
        hit = await asyncio.to_thread(cache_repo.cache_get, key, ttl)
        if hit is not None:
            return hit
        row = await ensure_symbol(symbol)
        asset = "etf" if row["kind"] == "etf" else "stocks"
        try:
            payload = await _fetcher(kind, symbol, asset, params)()
        except FetchError:
            stale = await asyncio.to_thread(cache_repo.cache_get_any, key)
            if stale is None:
                raise
            return stale[0]
        if payload is not None:
            await asyncio.to_thread(cache_repo.cache_put, key, payload)
        return payload
