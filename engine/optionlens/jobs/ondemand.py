"""On-demand work triggered by API requests: symbol visits (chain freshness from an available server-side
ChainSource, e.g. Questrade), first-visit history, cached Nasdaq fundamentals. Concurrent requests for the same symbol
share one fetch (per-symbol lock)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ..analytics.calendar import now_ny
from ..models import normalize_symbol
from ..net import FetchError
from ..repo import bars as bars_repo
from ..repo import cache as cache_repo
from ..repo import options as options_repo
from ..repo import views as views_repo
from ..sources import chain_source, nasdaq
from . import chains, prices
from ._util import final_since, session_date
from .universe import UnknownSymbol, ensure_symbol

__all__ = ["ensure_fresh", "ensure_history", "fetch_cached", "cache_key", "peek_cached", "UnknownSymbol",
           "fetch_calendar", "CACHE_TTL_S", "NEGATIVE_TTL_S"]

_locks: dict[str, asyncio.Lock] = {}


def _lock(key: str) -> asyncio.Lock:
    lk = _locks.get(key)
    if lk is None:
        lk = _locks[key] = asyncio.Lock()
    return lk


def is_stale(fetched_at: datetime | None, now: datetime, max_age_s: float) -> bool:
    """Missing; or, until the current session's closing data has arrived (open, or close..final_since),
    older than max_age_s; afterwards, fetched before final_since (i.e. not the closing snapshot)."""
    if fetched_at is None:
        return True
    final = final_since(session_date(now))
    if now < final:
        return (now - fetched_at).total_seconds() > max_age_s
    return fetched_at < final


VIEW_RECORD_INTERVAL_S = 3600   # a symbol's "opened" time is written at most this often
_viewed: dict[str, datetime] = {}


def _record_view(symbol: str, now: datetime) -> None:
    """Note that the user opened `symbol` (feeds the bridge's chain universe), throttled per symbol."""
    last = _viewed.get(symbol)
    if last is None or (now - last).total_seconds() >= VIEW_RECORD_INTERVAL_S:
        _viewed[symbol] = now
        views_repo.record_view(symbol, now)


async def ensure_fresh(symbol: str, max_age_s: float = 120) -> bool:
    """Make sure the symbol is known (symbols row, price history on first visit) and record the visit.
    With an available server-side ChainSource (the first in settings.chain_sources), a stale chain (see is_stale)
    is refetched once from it; without one nothing is fetched: the stored chain (pushed by the IBKR bridge) is
    served.
    Returns whether a chain is stored. Raises UnknownSymbol for symbols Nasdaq does not know."""
    symbol = normalize_symbol(symbol)
    async with _lock(f"chain:{symbol}"):
        row = await ensure_symbol(symbol)
        await ensure_history(symbol, row)
        await asyncio.to_thread(_record_view, symbol, datetime.now(timezone.utc))
        fetched = await asyncio.to_thread(options_repo.latest_fetched_at, symbol)
        srcs = chain_source.available()
        if not srcs or row.get("optionable") is False or not is_stale(fetched, now_ny(), max_age_s):
            return fetched is not None
        try:
            snap = await chains.refresh_chain(srcs[0], symbol)
        except (FetchError, chain_source.NotListed):
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
SPLITS_LOOKBACK_DAYS = 30
CALENDAR_TTL_S = 6 * H     # universe-wide Nasdaq calendars (splits, IPOs)


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


NEGATIVE_TTL_S = 6 * H     # "no data" answers (None, e.g. ETF financials/profile) are re-asked after 6 h


def cache_key(kind: str, symbol: str, **params: Any) -> str:
    """http_cache key of a `fetch_cached` payload (the one place that knows the key format)."""
    return ":".join([kind, normalize_symbol(symbol), *(f"{k}={params[k]}" for k in sorted(params))])


def peek_cached(kind: str, symbol: str, **params: Any) -> tuple[Any, datetime] | None:
    """(payload, fetched_at) stored for kind/symbol regardless of age, without fetching (sync)."""
    return cache_repo.cache_get_any(cache_key(kind, symbol, **params))


def _fresh(hit: tuple[Any, datetime] | None, ttl: float) -> bool:
    if hit is None:
        return False
    max_age = NEGATIVE_TTL_S if hit[0] is None else ttl
    return (datetime.now(timezone.utc) - hit[1]).total_seconds() <= max_age


async def _cached(key: str, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> Any:
    """http_cache read-through with TTL, a per-key lock and stale-on-FetchError fallback."""
    hit = await asyncio.to_thread(cache_repo.cache_get_any, key)
    if _fresh(hit, ttl):
        return hit[0]
    async with _lock(f"cache:{key}"):
        hit = await asyncio.to_thread(cache_repo.cache_get_any, key)
        if _fresh(hit, ttl):
            return hit[0]
        try:
            payload = await fetch()
        except FetchError:
            if hit is None:
                raise
            return hit[0]
        if payload is None and hit is not None and hit[0] is not None:
            return hit[0]           # keep serving older real data over a "no data" answer
        await asyncio.to_thread(cache_repo.cache_put, key, payload)
        return payload


async def fetch_cached(kind: str, symbol: str, **params: Any) -> Any:
    """Normalized Nasdaq payload of `kind` (see CACHE_TTL_S) for symbol, via http_cache with TTL.
    A None answer (Nasdaq has no such data for the symbol) is cached too, for NEGATIVE_TTL_S.
    params: financials quarterly=bool; extended market='pre'|'post'. On fetch failure returns the stale
    cached payload if any, else raises FetchError. Unknown kind -> KeyError."""
    ttl = CACHE_TTL_S[kind]
    symbol = normalize_symbol(symbol)

    async def fetch() -> Any:
        row = await ensure_symbol(symbol)
        asset = "etf" if row["kind"] == "etf" else "stocks"
        return await _fetcher(kind, symbol, asset, params)()

    return await _cached(cache_key(kind, symbol, **params), ttl, fetch)


async def fetch_calendar(kind: str, **params: Any) -> list[dict]:
    """Universe-wide Nasdaq calendar rows via http_cache (6 h TTL, stale fallback on FetchError).
    kind 'splits' (no params; window starts 30 days ago) or 'ipos' (month='YYYY-MM'). Unknown kind -> KeyError."""
    fetch = {
        "splits": lambda: nasdaq.fetch_splits(now_ny().date() - timedelta(days=SPLITS_LOOKBACK_DAYS)),
        "ipos": lambda: nasdaq.fetch_ipos(params["month"]),
    }[kind]
    return await _cached(":".join(["calendar", kind, *(f"{k}={params[k]}" for k in sorted(params))]),
                         CALENDAR_TTL_S, fetch)
