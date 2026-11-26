"""On-demand work triggered by API requests: symbol visits (first-visit history, the visit itself), cached company
data (Nasdaq for US listings; TMX Money's profile, news and insider reports for Canadian ones). Concurrent requests
for the same symbol share one fetch (per-symbol lock). On-demand chains are services.refresh's."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from ..analytics import markets
from ..analytics.calendar import now_ny
from ..models import normalize_symbol
from ..net import FetchError
from ..repo import bars as bars_repo
from ..repo import cache as cache_repo
from ..repo import views as views_repo
from ..sources import nasdaq, tmx, tmx_company
from . import prices
from .universe import UnknownSymbol, ensure_symbol

__all__ = ["visit", "ensure_history", "fetch_cached", "cache_key", "peek_cached", "source_of", "UnknownSymbol",
           "fetch_calendar", "CACHE_TTL_S", "NEGATIVE_TTL_S"]

_locks: dict[str, asyncio.Lock] = {}


def _lock(key: str) -> asyncio.Lock:
    lk = _locks.get(key)
    if lk is None:
        lk = _locks[key] = asyncio.Lock()
    return lk


VIEW_RECORD_INTERVAL_S = 3600   # a symbol's "opened" time is written at most this often
_viewed: dict[str, datetime] = {}


def _record_view(symbol: str, now: datetime) -> None:
    """Note that the user opened `symbol` (feeds the bridge's chain universe), throttled per symbol."""
    last = _viewed.get(symbol)
    if last is None or (now - last).total_seconds() >= VIEW_RECORD_INTERVAL_S:
        _viewed[symbol] = now
        views_repo.record_view(symbol, now)


async def visit(symbol: str) -> dict:
    """The user opened `symbol`: make sure it is known (symbols row, price history on first visit) and record the
    visit (it feeds the bridge's chain universe). Chains are not fetched here: the page starts a refresh round
    (services.refresh). Returns the symbols row. Raises UnknownSymbol for symbols Nasdaq does not know."""
    symbol = normalize_symbol(symbol)
    async with _lock(f"visit:{symbol}"):
        row = await ensure_symbol(symbol)
        await ensure_history(symbol, row)
        await asyncio.to_thread(_record_view, symbol, datetime.now(timezone.utc))
        return row


_history_asked: dict[str, date] = {}   # symbol -> New York day its history was last asked for here


async def ensure_history(symbol: str, row: dict | None = None) -> int:
    """Backfill 10y of daily bars if the symbol has no history (prices.lacks_history: none, or only a few bars
    stored before one was fetched); asked at most once per New York day per symbol, so a listing that really is
    that new is not refetched on every view. Returns bars stored (0 if already present)."""
    symbol = normalize_symbol(symbol)
    async with _lock(f"history:{symbol}"):
        cov = (await asyncio.to_thread(bars_repo.coverage, [symbol])).get(symbol)
        today = now_ny().date()
        if not prices.lacks_history(cov) or (cov is not None and _history_asked.get(symbol) == today):
            return 0
        row = row or await ensure_symbol(symbol)
        _history_asked[symbol] = today
        return await prices.backfill_symbol(row)


# ----------------------------------------------------------------------------- cached company data
H = 3600
CACHE_TTL_S: dict[str, int] = {
    "info": 24 * H, "summary": 24 * H, "financials": 24 * H, "earnings_surprise": 24 * H,
    "profile": 7 * 24 * H, "insiders": 12 * H, "news": 30 * 60, "extended": 5 * 60,
}
SPLITS_LOOKBACK_DAYS = 30
CALENDAR_TTL_S = 6 * H     # universe-wide Nasdaq calendars (splits, IPOs)


def covered_by_nasdaq(symbol: str) -> bool:
    """Nasdaq's quote, company, calendar and dividend endpoints cover US listings only: a Canadian symbol (RY.TO) is
    never sent to Nasdaq (its ticker can name a different US company: T.TO is TELUS, T is AT&T)."""
    return not markets.is_canadian(symbol)


async def _from_nasdaq(kind: str, symbol: str, params: dict) -> Any:
    asset = "etf" if (await ensure_symbol(symbol))["kind"] == "etf" else "stocks"
    return await {
        "info": lambda: nasdaq.fetch_info(symbol, asset),
        "summary": lambda: nasdaq.fetch_summary(symbol, asset),
        "financials": lambda: nasdaq.fetch_financials(symbol, bool(params.get("quarterly"))),
        "earnings_surprise": lambda: nasdaq.fetch_earnings_surprise(symbol),
        "profile": lambda: nasdaq.fetch_profile(symbol),
        "insiders": lambda: nasdaq.fetch_insiders(symbol),
        "news": lambda: nasdaq.fetch_news(symbol, asset),
        "extended": lambda: nasdaq.fetch_extended(symbol, params.get("market", "pre"), asset),
    }[kind]()


# Company data of Canadian listings that TMX Money has (the other kinds have no source there).
_FROM_TMX: dict[str, Callable[[str], Awaitable[Any]]] = {
    "profile": tmx_company.fetch_profile, "news": tmx_company.fetch_news, "insiders": tmx_company.fetch_insiders,
}


def source_of(kind: str, symbol: str) -> str | None:
    """Which source answers `kind` for `symbol`: nasdaq.NAME (US listings), tmx.NAME (Canadian listings, the kinds
    TMX Money has) or None (no source)."""
    if covered_by_nasdaq(symbol):
        return nasdaq.NAME
    return tmx.NAME if kind in _FROM_TMX else None


def _fetcher(kind: str, symbol: str, params: dict) -> Callable[[], Awaitable[Any]] | None:
    """The fetch of `kind` for `symbol` from the source that covers it (source_of), or None when no source does."""
    source = source_of(kind, symbol)
    if source == nasdaq.NAME:
        return lambda: _from_nasdaq(kind, symbol, params)
    return (lambda: _FROM_TMX[kind](symbol)) if source == tmx.NAME else None


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
    """Normalized payload of `kind` (see CACHE_TTL_S) for symbol from the source of its market (_fetcher), via
    http_cache with TTL; None without a request when no source covers the symbol.
    A None answer (the source has no such data for the symbol) is cached too, for NEGATIVE_TTL_S.
    params: financials quarterly=bool; extended market='pre'|'post'. On fetch failure returns the stale
    cached payload if any, else raises FetchError. Unknown kind -> KeyError."""
    ttl = CACHE_TTL_S[kind]
    symbol = normalize_symbol(symbol)
    fetch = _fetcher(kind, symbol, params)
    if fetch is None:
        return None
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
